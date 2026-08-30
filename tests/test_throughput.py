from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from observatory import database
from observatory.collector import _bucketize, _phase_duration
from observatory.llama_provider import map_metrics
from observatory import metrics
from observatory.metrics import ModelAcc, accumulate
from observatory.models import Model, Provider, SessionRow, TelemetrySample


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class ThroughputCalculationTests(unittest.TestCase):
    def test_native_aggregate_matches_python_accumulator(self):
        engine = memory_engine()
        samples = [
            TelemetrySample(provider_id=1, model_id=1, ts=10_000, state="IDLE",
                            tokens_total=0, prompt_total=0, gen_total=0,
                            prompt_seconds_total=0, gen_seconds_total=0),
            TelemetrySample(provider_id=1, model_id=2, ts=20_000, state="IDLE",
                            tokens_total=10, prompt_total=10, gen_total=0),
            TelemetrySample(provider_id=1, model_id=1, ts=30_000, state="GENERATING",
                            tokens_total=100, prompt_total=20, gen_total=80,
                            prompt_seconds_total=2, gen_seconds_total=4,
                            gen_tps=20, context_used=2048),
            TelemetrySample(provider_id=1, model_id=2, ts=40_000, state="IDLE",
                            tokens_total=5, prompt_total=2, gen_total=3,
                            mtp_proposed_total=10, mtp_accepted_total=7),
            TelemetrySample(provider_id=1, model_id=1, ts=50_000, state="IDLE",
                            tokens_total=120, prompt_total=20, gen_total=100,
                            prompt_seconds_total=2, gen_seconds_total=5,
                            gen_tps=25, context_used=4096),
        ]
        with Session(engine) as session:
            session.add_all(samples)
            session.commit()
            ordered = list(session.exec(select(TelemetrySample).order_by(
                TelemetrySample.ts)).all())
            expected: dict[int, ModelAcc] = {}
            accumulate(ordered, expected, 0, 100_000, 100_000)
            actual = metrics.aggregate_samples(session, [1], 0, 100_000, 100_000)

        self.assertEqual(set(actual), set(expected))
        for model_id in expected:
            for field in ("tokens", "prompt_tokens", "gen_tokens", "d_proposed",
                          "d_accepted", "prompt_time", "gen_time", "loaded_time",
                          "idle_time", "peak_gen", "peak_prompt", "context_max"):
                self.assertAlmostEqual(getattr(actual[model_id], field),
                                       getattr(expected[model_id], field))

    def test_accumulate_interleaved_models_is_linear_and_preserves_tails(self):
        class CountingSample:
            model_id_reads = 0

            def __init__(self, **values):
                defaults = {
                    "state": "IDLE", "tokens_total": 0, "prompt_total": 0,
                    "gen_total": 0, "prompt_seconds_total": 0,
                    "gen_seconds_total": 0, "prompt_tps": None, "gen_tps": None,
                    "mtp_proposed_total": 0, "mtp_accepted_total": 0,
                    "context_used": None,
                }
                defaults.update(values)
                self.__dict__.update(defaults)

            def __getattribute__(self, name):
                if name == "model_id":
                    type(self).model_id_reads += 1
                return object.__getattribute__(self, name)

        samples = [
            CountingSample(model_id=1, ts=10_000),
            CountingSample(model_id=2, ts=20_000),
            CountingSample(model_id=2, ts=30_000, tokens_total=20,
                           gen_total=20, gen_seconds_total=2),
            CountingSample(model_id=1, ts=50_000, state="GENERATING",
                           tokens_total=100, gen_total=100, gen_seconds_total=4),
            CountingSample(model_id=3, ts=90_000),
        ]
        acc: dict[int, ModelAcc] = {}
        accumulate(samples, acc, 0, 100_000, 100_000)

        self.assertEqual(CountingSample.model_id_reads, len(samples))
        self.assertEqual(acc[1].loaded_time, 90)
        self.assertEqual(acc[1].gen_time, 4)
        self.assertEqual(acc[1].idle_time, 86)
        self.assertEqual(acc[2].loaded_time, 10)
        self.assertEqual(acc[2].idle_time, 8)
        self.assertEqual(acc[3].loaded_time, 10)
        self.assertEqual(acc[3].idle_time, 10)

    def test_maps_authoritative_llama_duration_counters(self):
        mapped = map_metrics({
            "llamacpp:prompt_seconds_total": 8.24881,
            "llamacpp:tokens_predicted_seconds_total": 293.979,
        })
        self.assertEqual(mapped["prompt_seconds_total"], 8.24881)
        self.assertEqual(mapped["gen_seconds_total"], 293.979)

    def test_long_request_uses_native_duration_not_poll_interval(self):
        samples = [
            TelemetrySample(provider_id=1, model_id=1, ts=1000, state="IDLE",
                            gen_total=0, gen_seconds_total=0, gen_tps=0),
            TelemetrySample(provider_id=1, model_id=1, ts=2200, state="GENERATING",
                            gen_total=14577, gen_seconds_total=293.979, gen_tps=49.9505),
            TelemetrySample(provider_id=1, model_id=1, ts=3400, state="IDLE",
                            gen_total=14577, gen_seconds_total=293.979, gen_tps=0),
        ]
        acc: dict[int, ModelAcc] = {}
        accumulate(samples, acc, 1000, 3400, 3400)
        avg = acc[1].gen_tokens / acc[1].gen_time
        self.assertAlmostEqual(avg, 49.5852, places=3)
        self.assertAlmostEqual(acc[1].peak_gen, 49.9505, places=4)
        self.assertLessEqual(avg, acc[1].peak_gen)

    def test_legacy_samples_use_positive_gauge_and_missing_data_stays_unknown(self):
        self.assertAlmostEqual(_phase_duration(3000, 0, 50), 60)
        self.assertEqual(_phase_duration(3000, 0, 0), 0)
        samples = [
            TelemetrySample(provider_id=1, model_id=1, ts=1000, gen_total=0),
            TelemetrySample(provider_id=1, model_id=2, ts=1100, gen_total=0),
            TelemetrySample(provider_id=1, model_id=1, ts=2200,
                            gen_total=3000, gen_tps=50),
            TelemetrySample(provider_id=1, model_id=2, ts=2300,
                            gen_total=1000, gen_tps=None),
        ]
        acc: dict[int, ModelAcc] = {}
        accumulate(samples, acc, 1000, 2300, 2300)
        self.assertEqual(acc[1].gen_time, 60)
        self.assertEqual(acc[1].gen_tokens / acc[1].gen_time, 50)
        self.assertEqual(acc[2].gen_tokens, 1000)
        self.assertEqual(acc[2].gen_time, 0)

    def test_downsampling_preserves_counters_and_ignores_zero_gauges(self):
        engine = memory_engine()
        with Session(engine) as session:
            for ts, tps, seconds in ((1100, 0, 0), (1200, 50, 60), (1300, 0, 60)):
                session.add(TelemetrySample(
                    provider_id=1, model_id=1, ts=ts, gen_total=3000,
                    gen_seconds_total=seconds, gen_tps=tps,
                ))
            session.commit()
            _bucketize(session, 1000, 2000, 1000)
            row = session.exec(select(TelemetrySample)).one()
            self.assertEqual(row.gen_seconds_total, 60)
            self.assertEqual(row.gen_tps, 50)

    def test_summary_and_detail_apis_share_corrected_throughput(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(
                provider_id=provider.id, key="m", name="Model File", family="Family",
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(
                provider_id=provider.id, model_id=model.id, start_at=now - 2000,
                end_at=now - 1000, duration_s=1, gen_tokens=14577,
                total_tokens=14577, gen_time_s=293.979,
                avg_gen_tps=49.5852, peak_gen_tps=50,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            session.add(TelemetrySample(
                provider_id=provider.id, model_id=model.id, ts=now - 3000,
                state="IDLE", tokens_total=0, gen_total=0, gen_seconds_total=0,
                mtp_proposed_total=0, mtp_accepted_total=0,
            ))
            session.add(TelemetrySample(
                provider_id=provider.id, model_id=model.id, session_id=run.id,
                ts=now - 2000, state="GENERATING", tokens_total=14577,
                gen_total=14577, gen_seconds_total=293.979, gen_tps=50,
                mtp_proposed_total=8000, mtp_accepted_total=6000,
            ))
            session.commit()

            with patch.object(
                metrics, "fetch_samples",
                side_effect=AssertionError("Models summaries must use projected rows"),
            ):
                page = metrics.models_page(session, provider.id, "today", "family")
                selected = metrics.selected_stats(
                    session, [model.id], provider.id, "today",
                )
            detail = metrics.model_detail(session, model.id, "24h")
            session_data = metrics.session_detail(session, run.id)
            compared = metrics.compare(session, [run.id])

            self.assertEqual(page["rows"][0]["gen_tps"], 49.6)
            self.assertEqual(page["rows"][0]["peak_gen"], 50)
            self.assertEqual(selected["gen_tps"], 49.6)
            self.assertEqual(selected["peak_gen"], 50)
            self.assertEqual(selected["mtp_proposed"], 8000)
            self.assertEqual(selected["mtp_accepted"], 6000)
            self.assertEqual(selected["mtp_rejected"], 2000)
            self.assertEqual(selected["mtp_acc"], 75)
            self.assertEqual(detail["speeds"]["avg_gen_tps"], 49.6)
            self.assertEqual(detail["speeds"]["peak_gen_tps"], 50)
            self.assertEqual(session_data["session"]["avg_gen_tps"], 49.6)
            self.assertEqual(compared["sessions"][0]["avg_gen_tps"], 49.6)

    def test_selected_mtp_aggregates_models_and_preserves_empty_state(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            models = [Model(provider_id=provider.id, key=f"m{i}", name=f"m{i}")
                      for i in range(3)]
            session.add_all(models)
            session.commit()
            for model in models:
                session.refresh(model)
            counters = ((0, 0), (8000, 6000), (0, 0), (2000, 500), (0, 0), (0, 0))
            for i, model in enumerate(models):
                for j in range(2):
                    proposed, accepted = counters[i * 2 + j]
                    session.add(TelemetrySample(
                        provider_id=provider.id, model_id=model.id,
                        ts=now - 3000 + j * 1000,
                        mtp_proposed_total=proposed, mtp_accepted_total=accepted,
                    ))
            session.commit()

            combined = metrics.selected_stats(
                session, [models[0].id, models[1].id], provider.id, "today",
            )
            empty = metrics.selected_stats(
                session, [models[2].id], provider.id, "today",
            )
            self.assertEqual(combined["mtp_proposed"], 10000)
            self.assertEqual(combined["mtp_accepted"], 6500)
            self.assertEqual(combined["mtp_rejected"], 3500)
            self.assertEqual(combined["mtp_acc"], 65)
            self.assertEqual(empty["mtp_proposed"], 0)
            self.assertEqual(empty["mtp_accepted"], 0)
            self.assertEqual(empty["mtp_rejected"], 0)
            self.assertIsNone(empty["mtp_acc"])


class ThroughputMigrationTests(unittest.TestCase):
    def test_v3_repairs_recent_session_from_retained_gauge(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            session.exec(text("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"))
            session.exec(text("INSERT INTO meta VALUES ('schema_version', '2')"))
            provider = Provider(name="p", base_url="http://p")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="m", name="m")
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(
                provider_id=provider.id, model_id=model.id, start_at=now - 1000,
                gen_tokens=3000, total_tokens=3000, gen_time_s=1.2,
                avg_gen_tps=2500, peak_gen_tps=2500,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            session.add(TelemetrySample(
                provider_id=provider.id, model_id=model.id, ts=now - 2200,
                gen_total=0, gen_tps=0,
            ))
            session.add(TelemetrySample(
                provider_id=provider.id, model_id=model.id, session_id=run.id,
                ts=now - 1000, gen_total=3000, gen_tps=50,
            ))
            session.commit()

        database._migrate(engine)

        with Session(engine) as session:
            run = session.exec(select(SessionRow)).one()
            version = session.exec(text(
                "SELECT value FROM meta WHERE key='schema_version'"
            )).one()[0]
            indexes = {row[1] for row in session.exec(text(
                "PRAGMA index_list(telemetrysample)"
            )).all()}
            self.assertEqual(version, "7")
            self.assertIn("ix_telemetrysample_provider_ts", indexes)
            self.assertEqual(run.gen_time_s, 60)
            self.assertEqual(run.avg_gen_tps, 50)
            self.assertEqual(run.peak_gen_tps, 50)


class OverviewPerformanceRegressionTests(unittest.TestCase):
    def test_overview_reuses_one_projected_history_query_without_semantic_drift(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            first = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            second = Model(provider_id=provider.id, key="b", name="B", color="#222222")
            session.add_all([first, second])
            session.commit()
            session.refresh(first)
            session.refresh(second)
            run = SessionRow(provider_id=provider.id, model_id=first.id,
                             start_at=day + 50, end_at=day + 500,
                             duration_s=0.45, gen_tokens=10, total_tokens=10,
                             status="CLOSED")
            session.add(run)
            session.commit()
            session.refresh(run)
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=first.id,
                                ts=day + 100, state="GENERATING", tokens_total=20,
                                gen_total=20, gen_seconds_total=2),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=day + 150, state="GENERATING", tokens_total=100,
                                gen_total=100, gen_seconds_total=10),
                TelemetrySample(provider_id=provider.id, model_id=first.id,
                                session_id=run.id, ts=day + 200, state="IDLE",
                                tokens_total=30, gen_total=30, gen_seconds_total=3),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=day + 250, state="IDLE", tokens_total=5,
                                gen_total=5, gen_seconds_total=1),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=day + 350, state="IDLE", tokens_total=12,
                                gen_total=12, gen_seconds_total=2),
            ])
            session.commit()

            telemetry_selects = []

            def count_query(_conn, _cursor, statement, _parameters, _context, _many):
                if statement.lstrip().upper().startswith("SELECT") and "telemetrysample" in statement:
                    telemetry_selects.append(statement)

            event.listen(engine, "before_cursor_execute", count_query)
            try:
                with patch("observatory.metrics.now_ms", return_value=now):
                    result = metrics.overview(session)
            finally:
                event.remove(engine, "before_cursor_execute", count_query)

            self.assertEqual(len(telemetry_selects), 2)
            history_sql = telemetry_selects[-1].lower()
            self.assertNotIn("telemetrysample.id", history_sql)
            self.assertNotIn("telemetrysample.extra", history_sql)
            self.assertEqual(result["today"]["tokens"], 17)
            self.assertEqual(result["today"]["sessions"], 1)
            self.assertEqual(
                [(row["name"], row["tokens"]) for row in result["tokens_by_model"]],
                [("A", 10), ("B", 7)],
            )
            self.assertEqual(result["recent_sessions"][0]["model"], "A")
            self.assertEqual(result["current"]["model"], "B")


if __name__ == "__main__":
    unittest.main()
