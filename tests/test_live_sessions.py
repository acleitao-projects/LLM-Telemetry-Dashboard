from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from observatory import metrics
from observatory.collector import Collector, _slot_snapshots
from observatory.models import (CollectorLease, Model, ModelConfig, Provider,
                                SessionRow, TelemetrySample)
from observatory.session_tracker import ProviderState


def memory_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class SequenceClient:
    base_url = "http://router"

    def __init__(self):
        self.gen_total = 0
        self.gen_seconds = 0
        self.slot_payload = []

    def metrics(self, model=None):
        return {
            "llamacpp:prompt_tokens_total": 0,
            "llamacpp:prompt_seconds_total": 0,
            "llamacpp:tokens_predicted_total": self.gen_total,
            "llamacpp:tokens_predicted_seconds_total": self.gen_seconds,
            "llamacpp:requests_processing": 1 if self.slot_payload else 0,
        }

    def slots(self, model=None):
        return self.slot_payload


class NoSlotsClient(SequenceClient):
    def slots(self, model=None):
        raise RuntimeError("slots endpoint disabled")

    def metrics(self, model=None):
        values = super().metrics(model)
        values["llamacpp:requests_processing"] = 1
        return values


def live_slot(decoded: int, task: int = 10, slot: int = 0):
    return [{
        "id": slot, "id_task": task, "is_processing": True,
        "n_prompt_tokens_processed": 20,
        "n_prompt_tokens": 20 + decoded,
        "next_token": [{"n_decoded": decoded}],
        "params": {"prompt": "must never be retained"},
    }]


class SlotSessionTests(unittest.TestCase):
    def test_slot_payload_is_sanitized_and_speed_is_live_only(self):
        parsed = _slot_snapshots(live_slot(100))
        self.assertEqual(parsed, [{
            "slot_id": 0, "task_id": 10, "prompt_tokens": 20.0,
            "gen_tokens": 100.0, "context": 120,
        }])
        self.assertNotIn("params", parsed[0])

    def test_completion_reconciles_metrics_without_adding_live_tokens(self):
        engine = memory_engine()
        client = SequenceClient()
        collector = Collector(lambda _: client)
        now = time.time()
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            entry = {"key": "model-a", "loaded": True, "args": [], "meta": {}}

            client.slot_payload = live_slot(100)
            collector._poll_model(session, provider, entry, {}, None, client,
                                  {"status": "ok"}, {}, int(now * 1000), now)
            state = collector.model_states[(provider.id, "model-a")]
            run = session.exec(select(SessionRow)).one()
            self.assertEqual(run.status, "ACTIVE")
            self.assertEqual(run.live_gen_tokens, 100)
            self.assertEqual(run.gen_tokens, 0)

            client.slot_payload = []
            client.gen_total = 100
            client.gen_seconds = 10
            collector._poll_model(session, provider, entry, {}, state, client,
                                  {"status": "ok"}, {}, int((now + 1) * 1000), now + 1)
            session.expire_all()
            run = session.exec(select(SessionRow)).one()
            self.assertEqual(run.status, "CLOSED")
            self.assertEqual(run.result_source, "metrics")
            self.assertEqual(run.gen_tokens, 100)
            self.assertIsNone(run.live_gen_tokens)
            self.assertEqual(run.avg_gen_tps, 10)

    def test_parallel_slots_create_distinct_sessions(self):
        engine = memory_engine()
        collector = Collector(lambda _: None)
        now = time.time()
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            model = Model(provider_id=1, key="model", name="model")
            session.add(provider)
            session.commit()
            model.provider_id = provider.id
            session.add(model)
            session.commit()
            session.refresh(model)
            state = ProviderState(model_id=model.id, model_key=model.key)
            slots = _slot_snapshots(live_slot(10, 1, 0) + live_slot(20, 2, 1))
            collector._sync_live_tasks(session, provider, state, {}, slots,
                                       int(now * 1000), now)
            runs = session.exec(select(SessionRow).order_by(SessionRow.source_slot_id)).all()
            self.assertEqual([(run.source_slot_id, run.source_task_id) for run in runs],
                             [(0, 1), (1, 2)])
            self.assertIsNone(state.active_session_id)

    def test_restart_reattaches_to_existing_task(self):
        engine = memory_engine()
        now = time.time()
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="model", name="model")
            session.add(model)
            session.commit()
            session.refresh(model)
            slots = _slot_snapshots(live_slot(25, task=44))

            first = Collector(lambda _: None)
            first_state = ProviderState(model_id=model.id, model_key=model.key)
            first._sync_live_tasks(session, provider, first_state, {}, slots,
                                   int(now * 1000), now)
            original_id = session.exec(select(SessionRow)).one().id

            restarted = Collector(lambda _: None)
            restarted_state = ProviderState(model_id=model.id, model_key=model.key)
            restarted._sync_live_tasks(session, provider, restarted_state, {}, slots,
                                       int((now + 1) * 1000), now + 1)
            self.assertEqual(len(session.exec(select(SessionRow)).all()), 1)
            self.assertEqual(restarted_state.active_session_id, original_id)

    def test_missing_slots_falls_back_without_inventing_progress(self):
        engine = memory_engine()
        client = NoSlotsClient()
        collector = Collector(lambda _: client)
        now = time.time()
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            entry = {"key": "model-a", "loaded": True, "args": [], "meta": {}}
            collector._poll_model(session, provider, entry, {}, None, client,
                                  {"status": "ok"}, {}, int(now * 1000), now)
            state = collector.model_states[(provider.id, "model-a")]
            run = session.exec(select(SessionRow)).one()
            self.assertFalse(state.slots_available)
            self.assertEqual(run.status, "ACTIVE")
            self.assertIsNone(run.source_task_id)
            self.assertIsNone(run.live_gen_tokens)

    def test_legacy_active_row_is_displayed_as_interrupted_without_mutation(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="model", name="model")
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(provider_id=provider.id, model_id=model.id,
                             start_at=now - 60_000, status="ACTIVE")
            session.add(run)
            session.commit()
            data = metrics.sessions_page(session, None, None, None, None, None, "7d")
            self.assertEqual(data["sessions"][0]["status"], "INTERRUPTED")
            session.refresh(run)
            self.assertEqual(run.status, "ACTIVE")


class CollectorLeaseTests(unittest.TestCase):
    def test_only_one_collector_writes_and_stale_lease_can_be_taken(self):
        engine = memory_engine()
        first = Collector(lambda _: None)
        second = Collector(lambda _: None)
        with patch("observatory.collector.db.new_session",
                   side_effect=lambda: Session(engine)):
            self.assertTrue(first._ensure_lease(100.0))
            self.assertFalse(second._ensure_lease(100.0))
            self.assertTrue(second._ensure_lease(111.0))
        with Session(engine) as session:
            lease = session.get(CollectorLease, "collector")
            self.assertEqual(lease.owner_id, second.owner_id)


class ModelFamilyCompareTests(unittest.TestCase):
    def test_family_compare_uses_weighted_native_durations_and_mixed_config(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            models = []
            for name, quant in (("family-a-q4", "Q4_0"), ("family-a-q8", "Q8_0")):
                model = Model(provider_id=provider.id, key=name, name=name,
                              family="family-a", quant=quant)
                session.add(model)
                session.commit()
                session.refresh(model)
                models.append(model)
                session.add(ModelConfig(model_id=model.id, fingerprint=name,
                                        split_mode="tensor" if quant == "Q4_0" else "layer"))
            for model, tokens, seconds in ((models[0], 100, 10), (models[1], 300, 15)):
                session.add(TelemetrySample(provider_id=provider.id, model_id=model.id,
                                            ts=now - 2000, state="GENERATING",
                                            gen_total=0, gen_seconds_total=0))
                session.add(TelemetrySample(provider_id=provider.id, model_id=model.id,
                                            ts=now - 1000, state="IDLE",
                                            gen_total=tokens, gen_seconds_total=seconds,
                                            gen_tps=tokens / seconds))
            session.commit()
            data = metrics.compare_models(session, ["family-a"], None, "7d")
            item = data["models"][0]
            self.assertEqual(item["avg_gen_tps"], 16.0)
            self.assertEqual(item["gen_tokens"], 400)
            self.assertEqual(item["quants"], ["Q4_0", "Q8_0"])
            self.assertEqual(item["configuration"], "mixed")
            self.assertEqual(item["split_mode"], "mixed")


if __name__ == "__main__":
    unittest.main()
