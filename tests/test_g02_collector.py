"""G02 collector: single-commit, minute rollover, reset handling."""
from __future__ import annotations

import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from observatory.collector import Collector
from observatory.models import (Model, ModelResidency, ModelUsageBucket,
                                Provider, TelemetrySample, now_ms)

MINUTE_MS = 60_000


def memory_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class _FakeClient:
    """Returns controllable cumulative counters."""

    def __init__(self):
        self.prompt = 0.0
        self.gen = 0.0
        self.prompt_s = 0.0
        self.gen_s = 0.0
        self.prop = 0.0
        self.acc = 0.0

    def metrics(self, model=None):
        tokens = self.prompt + self.gen
        return {
            "llamacpp:tokens_total": tokens,
            "llamacpp:prompt_tokens_total": self.prompt,
            "llamacpp:tokens_predicted_total": self.gen,
            "llamacpp:prompt_seconds_total": self.prompt_s,
            "llamacpp:tokens_predicted_seconds_total": self.gen_s,
            "llamacpp:mtp_tokens_proposed_total": self.prop,
            "llamacpp:mtp_tokens_accepted_total": self.acc,
            "llamacpp:requests_processing": 0,
        }

    def slots(self, model=None):
        return []


def _seed(session, name="p"):
    p = Provider(name=name, base_url=f"http://{name}")
    session.add(p)
    session.commit()
    session.refresh(p)
    m = Model(provider_id=p.id, key="m1", name="m1")
    session.add(m)
    session.commit()
    session.refresh(m)
    return p, m


def _poll(collector, s, p, entry, client, st, ts_ms):
    """Convenience: call _poll_model with standard args."""
    collector._poll_model(s, p, entry, {}, st, client,
                          {"status": "ok"}, {}, ts_ms, ts_ms / 1000.0)


class CollectorBucketTests(unittest.TestCase):
    def test_single_commit_no_intermediate(self):
        """_poll_model must not call s.commit(); data visible only after caller commits."""
        engine = memory_engine()
        client = _FakeClient()
        collector = Collector(lambda _: client)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            p, m = _seed(s)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            # First poll: establish baseline.
            client.prompt, client.gen, client.prompt_s, client.gen_s = 100, 50, 1.0, 1.0
            _poll(collector, s, p, entry, client, None, base + 1000)
            s.commit()
            # Second poll: produces a delta.
            client.prompt, client.gen, client.prompt_s, client.gen_s = 200, 100, 2.0, 2.0
            st = collector.model_states[(p.id, "m1")]
            _poll(collector, s, p, entry, client, st, base + 5000)
            # Rollback: the flush from _poll_model is undone, no bucket persists.
            s.rollback()
            self.assertEqual(s.exec(select(ModelUsageBucket)).all(), [])

    def test_bucket_written_after_commit(self):
        engine = memory_engine()
        client = _FakeClient()
        collector = Collector(lambda _: client)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            p, m = _seed(s)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            # Baseline poll.
            client.prompt, client.gen, client.prompt_s, client.gen_s = 100, 50, 1.0, 1.0
            _poll(collector, s, p, entry, client, None, base + 1000)
            s.commit()
            # Delta poll.
            client.prompt, client.gen, client.prompt_s, client.gen_s = 300, 150, 3.0, 5.0
            st = collector.model_states[(p.id, "m1")]
            _poll(collector, s, p, entry, client, st, base + 5000)
            s.commit()
            buckets = s.exec(select(ModelUsageBucket)).all()
            self.assertEqual(len(buckets), 1)
            self.assertAlmostEqual(buckets[0].input_tokens, 200.0)
            self.assertAlmostEqual(buckets[0].output_tokens, 100.0)
            self.assertAlmostEqual(buckets[0].prompt_time_s, 2.0)
            self.assertAlmostEqual(buckets[0].gen_time_s, 4.0)

    def test_minute_rollover_creates_new_bucket(self):
        engine = memory_engine()
        client = _FakeClient()
        collector = Collector(lambda _: client)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            p, m = _seed(s)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            # Baseline in minute 0.
            client.prompt, client.gen, client.prompt_s, client.gen_s = 100, 50, 1.0, 1.0
            _poll(collector, s, p, entry, client, None, base + 1000)
            s.commit()
            # Delta in minute 0.
            client.prompt, client.gen, client.prompt_s, client.gen_s = 200, 100, 2.0, 2.0
            st = collector.model_states[(p.id, "m1")]
            _poll(collector, s, p, entry, client, st, base + 30_000)
            s.commit()
            # Delta in minute 1 (rollover).
            client.prompt, client.gen, client.prompt_s, client.gen_s = 500, 250, 5.0, 5.0
            _poll(collector, s, p, entry, client, st, base + MINUTE_MS + 10_000)
            s.commit()
            buckets = (s.exec(select(ModelUsageBucket)
                              .order_by(ModelUsageBucket.bucket_start)).all())
            self.assertEqual(len(buckets), 2)
            self.assertEqual(buckets[0].bucket_start, base)
            self.assertEqual(buckets[1].bucket_start, base + MINUTE_MS)
            self.assertAlmostEqual(buckets[0].input_tokens, 100.0)
            self.assertAlmostEqual(buckets[0].output_tokens, 50.0)
            self.assertAlmostEqual(buckets[1].input_tokens, 300.0)
            self.assertAlmostEqual(buckets[1].output_tokens, 150.0)

    def test_reset_produces_no_negative_delta(self):
        engine = memory_engine()
        client = _FakeClient()
        collector = Collector(lambda _: client)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            p, m = _seed(s)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            # Baseline: counters at 1000.
            client.prompt, client.gen = 800, 200
            client.prompt_s, client.gen_s = 10.0, 5.0
            _poll(collector, s, p, entry, client, None, base + 1000)
            s.commit()
            # Reset: counters drop to lower values.
            client.prompt, client.gen = 10, 5
            client.prompt_s, client.gen_s = 0.1, 0.05
            st = collector.model_states[(p.id, "m1")]
            _poll(collector, s, p, entry, client, st, base + 30_000)
            s.commit()
            buckets = s.exec(select(ModelUsageBucket)).all()
            for b in buckets:
                self.assertGreaterEqual(b.input_tokens, 0.0)
                self.assertGreaterEqual(b.output_tokens, 0.0)
                self.assertGreaterEqual(b.unclassified_tokens, 0.0)

    def test_residency_opened_on_first_poll(self):
        engine = memory_engine()
        client = _FakeClient()
        client.prompt, client.gen = 50, 25
        collector = Collector(lambda _: client)
        ts = (now_ms() // MINUTE_MS) * MINUTE_MS + 1000
        with Session(engine) as s:
            p, m = _seed(s)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            _poll(collector, s, p, entry, client, None, ts)
            s.commit()
            resid = s.exec(select(ModelResidency)).all()
            self.assertEqual(len(resid), 1)
            self.assertEqual(resid[0].model_id, m.id)
            self.assertIsNone(resid[0].unloaded_at)
            self.assertEqual(resid[0].source, "collector")

    def test_data_generation_bumped(self):
        collector = Collector(lambda _: _FakeClient())
        initial = collector.data_generation
        collector._bump_data_generation()
        collector._bump_data_generation()
        self.assertEqual(collector.data_generation, initial + 2)


if __name__ == "__main__":
    unittest.main()
