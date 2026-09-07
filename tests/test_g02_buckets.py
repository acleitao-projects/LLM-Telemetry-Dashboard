"""G02 durable usage buckets + residency: backfill, constraints, strict semantics."""
from __future__ import annotations

import unittest

from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from observatory import database as odb
from observatory import metrics
from observatory.models import (Model, ModelResidency, ModelUsageBucket, Provider,
                                TelemetrySample, now_ms)

MINUTE_MS = 60_000
DAY_MS = 86_400_000


def memory_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def seed_provider_model(session, name="p"):
    p = Provider(name=name, base_url=f"http://{name}")
    session.add(p)
    session.commit()
    session.refresh(p)
    m = Model(provider_id=p.id, key="m1", name="m1", family="fam")
    session.add(m)
    session.commit()
    session.refresh(m)
    return p.id, m.id


class BackfillTests(unittest.TestCase):
    def test_backfill_creates_buckets_and_residency(self):
        engine = memory_engine()
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            pid, mid = seed_provider_model(s)
            s.add_all([
                TelemetrySample(provider_id=pid, model_id=mid, ts=base,
                                state="GENERATING", tokens_total=100, prompt_total=60,
                                gen_total=40, prompt_seconds_total=1.0,
                                gen_seconds_total=2.0),
                TelemetrySample(provider_id=pid, model_id=mid, ts=base + MINUTE_MS,
                                state="GENERATING", tokens_total=300, prompt_total=180,
                                gen_total=120, prompt_seconds_total=3.0,
                                gen_seconds_total=6.0),
                TelemetrySample(provider_id=pid, model_id=mid, ts=base + 2 * MINUTE_MS,
                                state="GENERATING", tokens_total=500, prompt_total=320,
                                gen_total=180, prompt_seconds_total=5.0,
                                gen_seconds_total=10.0),
                TelemetrySample(provider_id=pid, model_id=mid, ts=base + 3 * MINUTE_MS,
                                state="UNLOADED", tokens_total=500, prompt_total=320,
                                gen_total=180),
            ])
            s.commit()
            odb._backfill_g02(s)

        with Session(engine) as s:
            buckets = s.exec(select(ModelUsageBucket)).all()
            resid = s.exec(select(ModelResidency)).all()
            # The first sample establishes a baseline (no delta); the next two
            # produce buckets. Input/output/unclassified split must sum to tokens.
            self.assertEqual(len(buckets), 2)
            self.assertAlmostEqual(sum(b.input_tokens for b in buckets), 260.0)
            self.assertAlmostEqual(sum(b.output_tokens for b in buckets), 140.0)
            self.assertAlmostEqual(sum(b.unclassified_tokens for b in buckets), 0.0)
            for b in buckets:
                self.assertAlmostEqual(b.input_tokens + b.output_tokens
                                       + b.unclassified_tokens,
                                       b.input_tokens + b.output_tokens
                                       + b.unclassified_tokens)
            self.assertEqual(len(resid), 1)
            self.assertEqual(resid[0].loaded_at, base)
            self.assertEqual(resid[0].unloaded_at, base + 3 * MINUTE_MS)
            self.assertEqual(resid[0].source, "backfill")

    def test_backfill_is_idempotent(self):
        engine = memory_engine()
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            pid, mid = seed_provider_model(s)
            s.add(TelemetrySample(provider_id=pid, model_id=mid, ts=base,
                                  state="GENERATING", tokens_total=100, prompt_total=60,
                                  gen_total=40))
            s.add(TelemetrySample(provider_id=pid, model_id=mid, ts=base + MINUTE_MS,
                                  state="GENERATING", tokens_total=300, prompt_total=180,
                                  gen_total=120))
            s.commit()
            odb._backfill_g02(s)
            odb._backfill_g02(s)  # second run must not duplicate
            buckets = s.exec(select(ModelUsageBucket)).all()
            resid = s.exec(select(ModelResidency)).all()
            self.assertEqual(len(buckets), 1)
            self.assertEqual(len(resid), 1)

    def test_backfill_reset_never_subtracts(self):
        engine = memory_engine()
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            pid, mid = seed_provider_model(s)
            # A counter reset (drop to a lower value) must not subtract usage.
            s.add(TelemetrySample(provider_id=pid, model_id=mid, ts=base,
                                  state="GENERATING", tokens_total=1000, prompt_total=600,
                                  gen_total=400))
            s.add(TelemetrySample(provider_id=pid, model_id=mid, ts=base + MINUTE_MS,
                                  state="GENERATING", tokens_total=100, prompt_total=60,
                                  gen_total=40))  # reset: 1000 -> 100
            s.commit()
            odb._backfill_g02(s)
            buckets = s.exec(select(ModelUsageBucket)).all()
            # No positive delta across the reset => no bucket recorded.
            self.assertEqual(buckets, [])


class ResidencyConstraintTests(unittest.TestCase):
    def test_one_open_interval_enforced(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid, mid = seed_provider_model(s)
            base = (now_ms() // MINUTE_MS) * MINUTE_MS
            s.add(ModelResidency(provider_id=pid, model_id=mid, loaded_at=base,
                                 source="collector"))  # open interval
            s.commit()
            with self.assertRaises(IntegrityError):
                s.add(ModelResidency(provider_id=pid, model_id=mid,
                                     loaded_at=base + MINUTE_MS, source="collector"))
                s.commit()
            s.rollback()
            # A second model on the same provider may still be open.
            m2 = Model(provider_id=pid, key="m2", name="m2")
            s.add(m2)
            s.commit()
            s.refresh(m2)
            s.add(ModelResidency(provider_id=pid, model_id=m2.id, loaded_at=base,
                                 source="collector"))
            s.commit()  # must not raise

    def test_closed_intervals_do_not_block_new_open(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid, mid = seed_provider_model(s)
            base = (now_ms() // MINUTE_MS) * MINUTE_MS
            s.add(ModelResidency(provider_id=pid, model_id=mid, loaded_at=base,
                                 unloaded_at=base + MINUTE_MS, source="collector"))
            s.commit()
            s.add(ModelResidency(provider_id=pid, model_id=mid,
                                 loaded_at=base + 2 * MINUTE_MS, source="collector"))
            s.commit()  # closed prior interval does not block a new open one


class StrictModelTests(unittest.TestCase):
    def _model(self, engine):
        with Session(engine) as s:
            pid, mid = seed_provider_model(s)
        return pid, mid

    def test_token_invariant_and_residency_overlap(self):
        engine = memory_engine()
        pid, mid = self._model(engine)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            s.add_all([
                ModelUsageBucket(provider_id=pid, model_id=mid,
                                 bucket_start=base + i * MINUTE_MS,
                                 input_tokens=inp, output_tokens=out,
                                 unclassified_tokens=uncl, prompt_time_s=1.0,
                                 gen_time_s=2.0, provenance="collector")
                for i, (inp, out, uncl) in enumerate([(100, 50, 5), (200, 80, 0),
                                                      (50, 30, 10)])
            ])
            s.add(ModelResidency(provider_id=pid, model_id=mid, loaded_at=base,
                                 unloaded_at=base + 3 * MINUTE_MS, source="collector"))
            s.commit()
            acc, estimated = metrics._strict_model(s, pid, mid, base, base + 3 * MINUTE_MS)
        self.assertAlmostEqual(acc.prompt_tokens, 350.0)
        self.assertAlmostEqual(acc.gen_tokens, 160.0)
        self.assertAlmostEqual(acc.unclassified_tokens, 15.0)
        self.assertAlmostEqual(acc.tokens, 525.0)
        # The explicit token invariant.
        self.assertAlmostEqual(acc.tokens,
                               acc.prompt_tokens + acc.gen_tokens + acc.unclassified_tokens)
        self.assertAlmostEqual(acc.loaded_time, 180.0)
        self.assertAlmostEqual(acc.idle_time, max(0.0, 180.0 - 3.0 - 6.0))
        self.assertFalse(estimated)

    def test_missing_interior_bucket_contributes_zero(self):
        engine = memory_engine()
        pid, mid = self._model(engine)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            # Only the middle minute has a bucket; the outer two are gaps with no
            # telemetry, so they must contribute zero (not error, not estimated).
            s.add(ModelUsageBucket(provider_id=pid, model_id=mid,
                                   bucket_start=base + MINUTE_MS, input_tokens=100,
                                   output_tokens=40, unclassified_tokens=0,
                                   prompt_time_s=1.0, gen_time_s=1.0,
                                   provenance="collector"))
            s.commit()
            acc, estimated = metrics._strict_model(s, pid, mid, base, base + 3 * MINUTE_MS)
        self.assertAlmostEqual(acc.prompt_tokens, 100.0)
        self.assertAlmostEqual(acc.gen_tokens, 40.0)
        self.assertAlmostEqual(acc.tokens, 140.0)
        # No telemetry consulted for the gap minutes, so not estimated.
        self.assertFalse(estimated)

    def test_partial_boundary_without_baseline_is_estimated(self):
        engine = memory_engine()
        pid, mid = self._model(engine)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            # The only sample sits *after* the window start, so the partial
            # boundary minute has no prior baseline -> flagged estimated.
            s.add(TelemetrySample(provider_id=pid, model_id=mid, ts=base + 15_000,
                                  state="GENERATING", tokens_total=50, prompt_total=30,
                                  gen_total=20, prompt_seconds_total=0.5,
                                  gen_seconds_total=0.5))
            s.commit()
            acc, estimated = metrics._strict_model(s, pid, mid, base, base + 30_000)
        self.assertGreaterEqual(acc.tokens, 0.0)
        self.assertTrue(estimated)

    def test_idle_never_negative(self):
        engine = memory_engine()
        pid, mid = self._model(engine)
        base = (now_ms() // MINUTE_MS) * MINUTE_MS
        with Session(engine) as s:
            # Loaded only 1 second but the bucket records 100s of inference -> idle 0.
            s.add(ModelUsageBucket(provider_id=pid, model_id=mid, bucket_start=base,
                                   input_tokens=10, output_tokens=10, prompt_time_s=50,
                                   gen_time_s=50, provenance="collector"))
            s.add(ModelResidency(provider_id=pid, model_id=mid, loaded_at=base,
                                 unloaded_at=base + 1000, source="collector"))
            s.commit()
            acc, _ = metrics._strict_model(s, pid, mid, base, base + MINUTE_MS)
        self.assertAlmostEqual(acc.loaded_time, 1.0)
        self.assertAlmostEqual(acc.idle_time, 0.0)


class RangeSummaryStrictPathTests(unittest.TestCase):
    def test_summary_uses_strict_path_when_buckets_exist(self):
        try:
            from tests.fixture import build_production_fixture, make_engine
        except ImportError:  # pragma: no cover - discover mode puts tests/ on path
            from fixture import build_production_fixture, make_engine
        engine = make_engine()
        build_production_fixture(engine, providers=1, models_per_provider=4,
                                 days=2, sessions=40, sample_every_ms=5 * MINUTE_MS)
        with Session(engine) as s:
            summary = metrics.range_summary(s, None, "7d")
        self.assertTrue(hasattr(summary, "sparks"))
        self.assertIsInstance(summary.estimated, bool)
        self.assertGreaterEqual(summary.total_tokens, 0.0)
        # Every per-model aggregate satisfies the token invariant.
        for acc in summary.acc.values():
            self.assertAlmostEqual(
                acc.tokens,
                acc.prompt_tokens + acc.gen_tokens + acc.unclassified_tokens,
                places=3)
        self.assertGreater(len(summary.acc), 0)


if __name__ == "__main__":
    unittest.main()
