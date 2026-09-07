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


def live_slot(decoded: int, task: int = 10, slot: int = 0, context_max: int = 4096):
    return [{
        "id": slot, "id_task": task, "is_processing": True,
        "n_prompt_tokens_processed": 20,
        "n_prompt_tokens": 20 + decoded,
        "n_ctx": context_max,
        "next_token": [{"n_decoded": decoded}],
        "params": {"prompt": "must never be retained"},
    }]


class SlotSessionTests(unittest.TestCase):
    def test_slot_payload_is_sanitized_and_speed_is_live_only(self):
        parsed = _slot_snapshots(live_slot(100))
        self.assertEqual(parsed, [{
            "slot_id": 0, "task_id": 10, "prompt_tokens": 20.0,
            "gen_tokens": 100.0, "context": 120, "context_max": 4096,
        }])
        self.assertNotIn("params", parsed[0])

    def test_live_timing_exposes_observed_average_and_three_second_rate(self):
        engine = memory_engine()
        collector = Collector(lambda _: None)
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
            state = ProviderState(model_id=model.id, model_key=model.key)
            for elapsed, decoded in ((0, 100), (1, 108), (4, 132)):
                collector._sync_live_tasks(
                    session, provider, state, {},
                    _slot_snapshots(live_slot(decoded)),
                    int((now + elapsed) * 1000), now + elapsed,
                )
            session.expire_all()
            run = session.exec(select(SessionRow)).one()
            self.assertAlmostEqual(run.live_gen_tps_avg, 8.0)
            self.assertAlmostEqual(run.live_gen_tps_3s, 8.0)
            live = metrics._session_live(run, int((now + 4) * 1000))
            self.assertEqual(live["gen_tps_avg"], 8.0)
            self.assertEqual(live["gen_tps_3s"], 8.0)
            self.assertEqual(live["context_max"], 4096)
            self.assertEqual(live["context_pct"], 3.7)
            run.live_context = 5000
            self.assertEqual(metrics._session_live(run, int((now + 4) * 1000))["context_pct"], 100.0)
            run.live_context_max = None
            self.assertIsNone(metrics._session_live(run, int((now + 4) * 1000))["context_pct"])

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
            self.assertEqual(run.live_gen_tokens, 100)
            self.assertEqual(run.live_context_max, 4096)
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
            session.refresh(run)
            data = metrics.sessions_page(session, None, None, None, None, None, "7d")
            self.assertEqual(data["sessions"][0]["status"], "INTERRUPTED")
            session.refresh(run)
            self.assertEqual(run.status, "ACTIVE")

    def test_active_zero_history_model_is_listed_and_selected_runtime_uses_exact_variant(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router",
                                status="LIVE", last_success_at=now)
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="family-q4", name="family-q4",
                          family="family")
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(
                provider_id=provider.id, model_id=model.id, start_at=now - 1000,
                status="ACTIVE", source_slot_id=0, source_task_id=77,
                live_prompt_tokens=20, live_gen_tokens=120, live_context=140,
                live_context_max=1000, live_gen_tps_avg=8.25,
                live_gen_tps_3s=8.2, live_seen_at=now,
            )
            session.add(run)
            session.commit()

            page = metrics.models_page(session, None, "today", "family")
            self.assertEqual(len(page["rows"]), 1)
            self.assertTrue(page["rows"][0]["active"])
            self.assertEqual(page["rows"][0]["active_tasks"], 1)
            selected = metrics.selected_stats(session, [model.id], None, "today")
            self.assertEqual(selected["realtime"]["status"], "LIVE")
            self.assertEqual(selected["realtime"]["model"], "family-q4")
            self.assertEqual(selected["realtime"]["context_pct"], 14.0)
            self.assertEqual(selected["realtime"]["session_id"], run.id)
            self.assertEqual(selected["realtime"]["session_started_at"], run.start_at)

            snapshot = metrics.realtime_snapshot(session)
            self.assertEqual(len(snapshot["active_models"]), 1)
            shared_live = snapshot["active_models"][0]["realtime"]
            self.assertEqual(shared_live["gen_tps_3s"], 8.2)
            with patch.object(metrics, "_selected_realtime",
                              side_effect=AssertionError("historical lookup not allowed")):
                shared = metrics.selected_stats(
                    session, [model.id], None, "today",
                    realtime_override=shared_live,
                )
            self.assertEqual(shared["realtime"]["status"], "LIVE")

    def test_closed_session_preserves_provisional_snapshot_for_selected_runtime(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="model", name="model")
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(
                provider_id=provider.id, model_id=model.id, start_at=now - 5000,
                end_at=now - 1000, status="CLOSED", live_gen_tokens=90,
                live_context=110, live_context_max=2048, live_seen_at=now - 1000,
                source_slot_id=0, source_task_id=88, result_source="metrics",
            )
            session.add(run)
            session.commit()
            session.refresh(run)

            selected = metrics.selected_stats(session, [model.id], None, "today")
            realtime = selected["realtime"]
            self.assertEqual(realtime["source"], "slots")
            self.assertEqual(realtime["snapshot"], "LAST SLOT")
            self.assertEqual(realtime["gen_tokens"], 90)
            self.assertTrue(realtime["provisional"])
            self.assertEqual(realtime["session_id"], run.id)

    def test_selected_runtime_uses_honest_metrics_fallback_and_provider_health(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="model", name="model")
            session.add(model)
            session.commit()
            session.refresh(model)
            session.add(TelemetrySample(
                provider_id=provider.id, model_id=model.id, ts=now,
                state="IDLE", context_used=500, context_max=1000,
            ))
            session.commit()

            realtime = metrics.selected_stats(session, [model.id], None, "today")["realtime"]
            self.assertEqual(realtime["status"], "IDLE")
            self.assertEqual(realtime["snapshot"], "LAST METRICS")
            self.assertEqual(realtime["context_pct"], 50.0)
            self.assertFalse(realtime["provisional"])

            provider.status = "STALE"
            session.add(provider)
            session.commit()
            realtime = metrics.selected_stats(session, [model.id], None, "today")["realtime"]
            self.assertEqual(realtime["status"], "STALE")

    def test_runtime_history_preserves_nulls_and_live_point(self):
        engine = memory_engine()
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
                             start_at=1000, status="ACTIVE")
            session.add(run)
            session.commit()
            session.refresh(run)
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                session_id=run.id, ts=1000, gen_tps=None,
                                context_used=100),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                session_id=run.id, ts=2000, gen_tps=10,
                                context_used=None),
            ])
            session.commit()

            history = metrics._runtime_history(session, {
                "session_id": run.id, "session_started_at": run.start_at,
                "observed_at": 3000, "gen_tps": None, "context": 300,
            })
            self.assertEqual(history["timestamps"], [1000, 2000, 3000])
            self.assertEqual(history["gen_tps"], [None, 10, None])
            self.assertEqual(history["context"], [100, None, 300])

    def test_selected_runtime_history_is_bounded_across_current_session(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="model", name="model")
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(
                provider_id=provider.id, model_id=model.id, start_at=now - 240_000,
                status="ACTIVE", source_slot_id=0, source_task_id=1,
                live_context=4000, live_context_max=8192, live_gen_tps=42,
                live_seen_at=now,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            session.add_all([
                TelemetrySample(
                    provider_id=provider.id, model_id=model.id, session_id=run.id,
                    ts=run.start_at + index * 1000, state="GENERATING",
                    gen_tps=20 + index / 10, context_used=1000 + index * 10,
                )
                for index in range(240)
            ])
            session.commit()

            realtime = metrics.selected_stats(
                session, [model.id], provider.id, "today",
            )["realtime"]
            history = realtime["history"]
            self.assertEqual(realtime["session_id"], run.id)
            self.assertLessEqual(len(history["timestamps"]), 120)
            self.assertEqual(len(history["timestamps"]), len(history["gen_tps"]))
            self.assertEqual(len(history["timestamps"]), len(history["context"]))
            self.assertEqual(history["timestamps"][-1], now)
            self.assertEqual(history["gen_tps"][-1], 42)
            self.assertEqual(history["context"][-1], 4000)


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


class ModelFileCompareTests(unittest.TestCase):
    def test_file_compare_keeps_model_files_separate(self):
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
                session.add(SessionRow(provider_id=provider.id, model_id=model.id,
                                       start_at=now - 2500, end_at=now - 500,
                                       status="COMPLETE", gen_tokens=tokens,
                                       total_tokens=tokens, gen_time_s=seconds,
                                       peak_gen_tps=tokens / seconds))
                session.add(TelemetrySample(provider_id=provider.id, model_id=model.id,
                                            ts=now - 2000, state="GENERATING",
                                            gen_total=0, gen_seconds_total=0))
                session.add(TelemetrySample(provider_id=provider.id, model_id=model.id,
                                            ts=now - 1000, state="IDLE",
                                            gen_total=tokens, gen_seconds_total=seconds,
                                            gen_tps=tokens / seconds))
            session.commit()
            candidates = metrics.compare_model_candidates(session, None, "7d")
            self.assertEqual({row["label"] for row in candidates},
                             {"family-a-q8", "family-a-q4"})
            self.assertEqual({row["quant"] for row in candidates}, {"Q8_0", "Q4_0"})
            self.assertEqual({row["key"] for row in candidates},
                             {str(models[0].id), str(models[1].id)})

            with patch.object(metrics, "aggregate_range_summary",
                              wraps=metrics.aggregate_range_summary) as aggregate, \
                    patch.object(metrics, "_gpu_rows",
                                 wraps=metrics._gpu_rows) as fetch_gpu:
                data = metrics.compare_models(
                    session, [str(models[0].id), str(models[1].id)], None, "7d")
            self.assertEqual(aggregate.call_count, 1)
            self.assertEqual(fetch_gpu.call_count, 0)
            first, second = data["models"]
            self.assertEqual(first["model"], "family-a-q4")
            self.assertEqual(first["avg_gen_tps"], 10.0)
            self.assertEqual(first["gen_tokens"], 100)
            self.assertEqual(first["quant"], "Q4_0")
            self.assertEqual(first["configuration"], "single")
            self.assertEqual(first["split_mode"], "tensor")
            self.assertEqual(second["model"], "family-a-q8")
            self.assertEqual(second["avg_gen_tps"], 20.0)
            self.assertEqual(second["gen_tokens"], 300)

            with patch.object(metrics, "_gpu_rows",
                              wraps=metrics._gpu_rows) as fetch_gpu:
                metrics.compare_model_gpus(
                    session, [str(models[0].id), str(models[1].id)], None, "7d")
            # The deferred table decoration is a SQLite aggregate, not a
            # full GPU-history load for every compared model.
            self.assertEqual(fetch_gpu.call_count, 0)


class SqlFreshnessTests(unittest.TestCase):
    def test_active_models_excludes_stale_rows_via_sql(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            fresh = SessionRow(provider_id=provider.id, model_id=model.id,
                               status="ACTIVE", start_at=now - 5000,
                               live_seen_at=now - 1000)
            stale = SessionRow(provider_id=provider.id, model_id=model.id,
                               status="ACTIVE", start_at=now - 60000,
                               live_seen_at=now - 30000)
            fresh_fin = SessionRow(provider_id=provider.id, model_id=model.id,
                                   status="FINALIZING", start_at=now - 5000,
                                   live_seen_at=now - 5000)
            stale_fin = SessionRow(provider_id=provider.id, model_id=model.id,
                                   status="FINALIZING", start_at=now - 60000,
                                   live_seen_at=now - 20000)
            session.add_all([fresh, stale, fresh_fin, stale_fin])
            session.commit()
            result = metrics._active_models(session, now)
            self.assertIn(model.id, result)
            self.assertEqual(result[model.id]["task_count"], 2)

    def test_reconcile_stale_sessions_marks_interrupted_and_incomplete(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            stale_active = SessionRow(provider_id=provider.id, model_id=model.id,
                                      status="ACTIVE", start_at=now - 200_000,
                                      live_seen_at=now - 150_000)
            stale_fin = SessionRow(provider_id=provider.id, model_id=model.id,
                                   status="FINALIZING", start_at=now - 200_000,
                                   live_seen_at=now - 150_000)
            fresh_active = SessionRow(provider_id=provider.id, model_id=model.id,
                                      status="ACTIVE", start_at=now - 5000,
                                      live_seen_at=now - 1000)
            session.add_all([stale_active, stale_fin, fresh_active])
            session.commit()
            sa_id = stale_active.id
            sf_id = stale_fin.id
            fa_id = fresh_active.id
            collector = Collector(lambda _: None)
            from observatory.collector import _PollEvidence
            evidence = {provider.id: _PollEvidence(
                provider_id=provider.id, gen=0, models_ok=True,
                loaded_model_keys={"m1"},
                slot_evidence={"m1": (True, set())},
            )}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            rows = {r.id: r for r in session.exec(select(SessionRow)).all()}
            self.assertEqual(rows[sa_id].status, "INTERRUPTED")
            self.assertEqual(rows[sf_id].status, "INCOMPLETE")
            self.assertEqual(rows[fa_id].status, "ACTIVE")

    def test_last_used_at_only_on_token_activity(self):
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
            client.slot_payload = []
            client.gen_total = 0
            collector._poll_model(session, provider, entry, {}, None, client,
                                  {"status": "ok"}, {}, int(now * 1000), now)
            state = collector.model_states[(provider.id, "model-a")]
            session.expire_all()
            model = session.exec(select(Model)).one()
            self.assertIsNotNone(model.first_seen_at)
            self.assertIsNone(model.last_used_at)


class ReconciliationSafetyTests(unittest.TestCase):
    """Production-data-safety regression tests for startup reconciliation."""

    def _make_collector_with_evidence(self, engine, provider_ids, model_key="m1"):
        """Create a collector with startup evidence for the given providers."""
        collector = Collector(lambda _: None)
        from observatory.collector import _PollEvidence
        evidence = {}
        with Session(engine) as s:
            providers = s.exec(select(Provider)).all()
        for p in providers:
            if p.id in provider_ids:
                evidence[p.id] = _PollEvidence(
                    provider_id=p.id, gen=0, models_ok=True,
                    loaded_model_keys={model_key},
                    slot_evidence={model_key: (True, set())},
                )
        return collector, evidence

    def test_standby_start_performs_no_session_writes(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            stale = SessionRow(provider_id=provider.id, model_id=model.id,
                               status="ACTIVE", start_at=now - 300_000,
                               live_seen_at=now - 200_000)
            session.add(stale)
            session.commit()
            stale_id = stale.id
        collector = Collector(lambda _: None)
        with patch("observatory.collector.db.new_session",
                   side_effect=lambda: Session(engine)):
            collector.start()
            time.sleep(0.1)
            collector.stop()
        with Session(engine) as session:
            row = session.get(SessionRow, stale_id)
            self.assertEqual(row.status, "ACTIVE")
            self.assertEqual(row.end_at, None)

    def test_active_session_survives_restart_below_grace(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 5000,
                              live_seen_at=now - 11_000,
                              source_slot_id=0, source_task_id=7)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {
                "m1": (True, {(0, 7)})
            }
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "ACTIVE")
            self.assertIsNone(row.end_at)

    def test_finalizing_session_survives_restart_below_grace(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="FINALIZING", start_at=now - 5000,
                              live_seen_at=now - 16_000,
                              source_slot_id=0, source_task_id=7)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {
                "m1": (True, {(0, 7)})
            }
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "FINALIZING")

    def test_authoritative_absence_terminalizes(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000,
                              source_slot_id=0, source_task_id=7)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {
                "m1": (True, set())
            }
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "INTERRUPTED")
            self.assertEqual(row.result_source, "interrupted")

    def test_slots_poll_failed_preserves_session(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000,
                              source_slot_id=0, source_task_id=7)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {
                "m1": (False, set())
            }
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "ACTIVE")

    def test_model_list_not_authoritative_preserves(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].models_ok = False
            evidence[provider.id].loaded_model_keys = set()
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "ACTIVE")

    def test_provider_poll_failed_preserves_session(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector = Collector(lambda _: None)
            from observatory.collector import _PollEvidence
            evidence = {}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "ACTIVE")

    def test_boundary_at_grace_threshold(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        grace = 120_000
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            below = SessionRow(provider_id=provider.id, model_id=model.id,
                               status="ACTIVE", start_at=now - 200_000,
                               live_seen_at=now - (grace - 5000))
            above = SessionRow(provider_id=provider.id, model_id=model.id,
                               status="ACTIVE", start_at=now - 200_000,
                               live_seen_at=now - (grace + 5000))
            session.add_all([below, above])
            session.commit()
            below_id = below.id
            above_id = above.id
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {"m1": (True, set())}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            rows = {r.id: r for r in session.exec(select(SessionRow)).all()}
            self.assertEqual(rows[below_id].status, "ACTIVE")
            self.assertEqual(rows[above_id].status, "INTERRUPTED")

    def test_null_live_seen_at_old_start_terminalized(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=None)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {"m1": (True, set())}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "INTERRUPTED")
            self.assertIsNone(row.end_at)
            self.assertIsNone(row.duration_s)

    def test_null_live_seen_at_recent_start_preserved(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 5_000,
                              live_seen_at=None)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "ACTIVE")

    def test_null_live_seen_at_finalizing(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="FINALIZING", start_at=now - 200_000,
                              live_seen_at=None)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {"m1": (True, set())}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "INCOMPLETE")
            self.assertIsNone(row.duration_s)

    def test_token_bearing_zero_duration_not_fabricated(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 200_000,
                              gen_tokens=500, prompt_tokens=100,
                              total_tokens=600)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {"m1": (True, set())}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "INTERRUPTED")
            self.assertIsNone(row.end_at)
            self.assertIsNone(row.duration_s)

    def test_restart_idempotency(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector, evidence = self._make_collector_with_evidence(
                engine, {provider.id})
            evidence[provider.id].slot_evidence = {"m1": (True, set())}
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            first_status = session.get(SessionRow, sess.id).status
            first_end = session.get(SessionRow, sess.id).end_at
            collector._reconcile_after_startup(session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, first_status)
            self.assertEqual(row.end_at, first_end)
            count = len(session.exec(select(SessionRow)).all())
            self.assertEqual(count, 1)

    def test_new_task_does_not_resurrect_terminal_session(self):
        engine = memory_engine()
        now = time.time()
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            old = SessionRow(provider_id=provider.id, model_id=model.id,
                             status="INTERRUPTED", start_at=now - 60_000,
                             source_slot_id=0, source_task_id=5,
                             end_at=now - 50_000, duration_s=10.0)
            session.add(old)
            session.commit()
            session.refresh(old)
            state = ProviderState(model_id=model.id, model_key=model.key)
            slots = _slot_snapshots(live_slot(100, task=5, slot=0))
            collector = Collector(lambda _: None)
            collector._sync_live_tasks(session, provider, state, {},
                                       slots, int(now * 1000), now)
            session.expire_all()
            rows = session.exec(select(SessionRow).order_by(SessionRow.id)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].status, "INTERRUPTED")
            self.assertEqual(rows[0].id, old.id)
            self.assertEqual(rows[1].status, "ACTIVE")
            self.assertNotEqual(rows[1].id, old.id)

    def test_production_shaped_file_copy(self):
        import tempfile
        import os
        from sqlalchemy import create_engine as sa_create_engine
        from sqlalchemy.pool import StaticPool as SAStaticPool
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test.db")
        try:
            eng = sa_create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
                poolclass=SAStaticPool,
            )
            SQLModel.metadata.create_all(eng)
            now = int(time.time() * 1000)
            with Session(eng) as s:
                p1 = Provider(name="p1", base_url="http://a")
                p2 = Provider(name="p2", base_url="http://b")
                s.add_all([p1, p2])
                s.commit()
                s.refresh(p1)
                s.refresh(p2)
                p1_id, p2_id = p1.id, p2.id
                m1 = Model(provider_id=p1_id, key="m1", name="m1")
                m2 = Model(provider_id=p2_id, key="m2", name="m2")
                s.add_all([m1, m2])
                s.commit()
                s.refresh(m1)
                s.refresh(m2)
                m1_id, m2_id = m1.id, m2.id
                s.add_all([
                    SessionRow(provider_id=p1_id, model_id=m1_id,
                               status="ACTIVE", start_at=now - 200_000,
                               live_seen_at=now - 150_000,
                               gen_tokens=100, prompt_tokens=50,
                               total_tokens=150),
                    SessionRow(provider_id=p1_id, model_id=m1_id,
                               status="CLOSED", start_at=now - 500_000,
                               end_at=now - 400_000, duration_s=100.0,
                               gen_tokens=200, total_tokens=250),
                    SessionRow(provider_id=p2_id, model_id=m2_id,
                               status="ACTIVE", start_at=now - 300_000,
                               live_seen_at=None,
                               gen_tokens=50, prompt_tokens=20,
                               total_tokens=70),
                ])
                s.add(TelemetrySample(provider_id=p1_id, model_id=m1_id,
                                      ts=now - 100_000, state="GENERATING",
                                      gen_total=100))
                s.commit()
            with Session(eng) as s:
                collector = Collector(lambda _: None)
                from observatory.collector import _PollEvidence
                evidence = {
                    p1_id: _PollEvidence(
                        provider_id=p1_id, gen=0, models_ok=True,
                        loaded_model_keys={"m1"},
                        slot_evidence={"m1": (True, set())}),
                    p2_id: _PollEvidence(
                        provider_id=p2_id, gen=0, models_ok=True,
                        loaded_model_keys={"m2"},
                        slot_evidence={"m2": (True, set())}),
                }
                collector._reconcile_after_startup(
                    s, evidence, {p1_id, p2_id})
                s.expire_all()
                sessions = s.exec(select(SessionRow)).all()
                telemetry = s.exec(select(TelemetrySample)).all()
                provs = s.exec(select(Provider)).all()
                models = s.exec(select(Model)).all()
                session_data = [(r.id, r.provider_id, r.status, r.end_at,
                                 r.duration_s, r.gen_tokens, r.total_tokens)
                                for r in sessions]
            self.assertEqual(len(sessions), 3)
            self.assertEqual(len(telemetry), 1)
            self.assertEqual(len(provs), 2)
            self.assertEqual(len(models), 2)
            active_count = sum(1 for r in sessions if r.status == "ACTIVE")
            self.assertEqual(active_count, 0)
            self.assertEqual(
                sum(1 for r in sessions if r.status == "INTERRUPTED"), 2)
            self.assertEqual(
                sum(1 for r in sessions if r.status == "CLOSED"), 1)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stale_evidence_invalidated_on_subsequent_failure(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            p1 = Provider(name="p1", base_url="http://a")
            p2 = Provider(name="p2", base_url="http://b")
            session.add_all([p1, p2])
            session.commit()
            session.refresh(p1)
            session.refresh(p2)
            m1 = Model(provider_id=p1.id, key="m1", name="m1")
            m2 = Model(provider_id=p2.id, key="m2", name="m2")
            session.add_all([m1, m2])
            session.commit()
            session.refresh(m1)
            session.refresh(m2)
            sess1 = SessionRow(provider_id=p1.id, model_id=m1.id,
                               status="ACTIVE", start_at=now - 200_000,
                               live_seen_at=now - 150_000)
            sess2 = SessionRow(provider_id=p2.id, model_id=m2.id,
                               status="ACTIVE", start_at=now - 200_000,
                               live_seen_at=now - 150_000)
            session.add_all([sess1, sess2])
            session.commit()
            collector = Collector(lambda _: None)
            from observatory.collector import _PollEvidence
            collector._startup_evidence[p1.id] = _PollEvidence(
                provider_id=p1.id, gen=0, models_ok=True,
                loaded_model_keys={"m1"},
                slot_evidence={"m1": (True, set())})
            # p1 fails: evidence cleared, gen incremented
            collector._startup_evidence.clear()
            collector._startup_gen += 1
            # p2 succeeds with new gen
            collector._startup_evidence[p2.id] = _PollEvidence(
                provider_id=p2.id, gen=1, models_ok=True,
                loaded_model_keys={"m2"},
                slot_evidence={"m2": (True, set())})
            # p1 not in evidence -> reconciliation should NOT run
            self.assertFalse(
                {p1.id, p2.id} <= set(collector._startup_evidence.keys()))
            # Both sessions preserved
            session.expire_all()
            self.assertEqual(session.get(SessionRow, sess1.id).status, "ACTIVE")
            self.assertEqual(session.get(SessionRow, sess2.id).status, "ACTIVE")

    def test_only_newest_evidence_used_after_failure_recovery(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            p1 = Provider(name="p1", base_url="http://a")
            session.add(p1)
            session.commit()
            session.refresh(p1)
            m1 = Model(provider_id=p1.id, key="m1", name="m1")
            session.add(m1)
            session.commit()
            session.refresh(m1)
            sess = SessionRow(provider_id=p1.id, model_id=m1.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000,
                              source_slot_id=0, source_task_id=9)
            session.add(sess)
            session.commit()
            collector = Collector(lambda _: None)
            from observatory.collector import _PollEvidence
            # gen 0: task present
            collector._startup_evidence[p1.id] = _PollEvidence(
                provider_id=p1.id, gen=0, models_ok=True,
                loaded_model_keys={"m1"},
                slot_evidence={"m1": (True, {(0, 9)})})
            # failure: clear, gen -> 1
            collector._startup_evidence.clear()
            collector._startup_gen = 1
            # gen 1: task absent
            collector._startup_evidence[p1.id] = _PollEvidence(
                provider_id=p1.id, gen=1, models_ok=True,
                loaded_model_keys={"m1"},
                slot_evidence={"m1": (True, set())})
            # Only gen-1 evidence should be used
            collector._reconcile_after_startup(
                session, collector._startup_evidence, {p1.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "INTERRUPTED")

    def test_models_cache_stale_when_current_call_fails(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m1", name="m1")
            session.add(model)
            session.commit()
            session.refresh(model)
            sess = SessionRow(provider_id=provider.id, model_id=model.id,
                              status="ACTIVE", start_at=now - 200_000,
                              live_seen_at=now - 150_000)
            session.add(sess)
            session.commit()
            session.refresh(sess)
            collector = Collector(lambda _: None)
            from observatory.collector import _PollEvidence
            # models_ok=False simulates /v1/models failing in this poll
            evidence = {provider.id: _PollEvidence(
                provider_id=provider.id, gen=0, models_ok=False,
                loaded_model_keys=set(),
                slot_evidence={})}
            collector._reconcile_after_startup(
                session, evidence, {provider.id})
            session.expire_all()
            row = session.get(SessionRow, sess.id)
            self.assertEqual(row.status, "ACTIVE")


if __name__ == "__main__":
    unittest.main()
