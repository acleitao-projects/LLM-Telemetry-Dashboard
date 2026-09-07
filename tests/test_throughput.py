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
    def test_models_daily_average_uses_selected_calendar_days(self):
        engine = memory_engine()
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            now = int(time.time() * 1000)
            self.assertEqual(metrics.range_day_count(session, [provider.id], "today", now, now), 1)
            self.assertEqual(metrics.range_day_count(session, [provider.id], "7d", now, now), 7)
            self.assertEqual(metrics.range_day_count(session, [provider.id], "30d", now, now), 30)

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
                mtp_proposed=8000, mtp_accepted=6000,
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
                    session.add(SessionRow(
                        provider_id=provider.id, model_id=model.id,
                        start_at=now - 3000 + j * 1000,
                        end_at=now - 2000 + j * 1000,
                        mtp_proposed=proposed, mtp_accepted=accepted,
                    ))
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

    def test_range_summary_sums_sessions_across_router_counter_resets(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="qwen-27b", name="Qwen 27B")
            session.add(model)
            session.commit()
            session.refresh(model)
            first = SessionRow(
                provider_id=provider.id, model_id=model.id,
                start_at=now - 6000, end_at=now - 4000,
                prompt_tokens=20_000, gen_tokens=2_000_000, total_tokens=2_020_000,
                prompt_time_s=100, gen_time_s=1_000, mtp_proposed=500_000,
                mtp_accepted=400_000,
            )
            second = SessionRow(
                provider_id=provider.id, model_id=model.id,
                start_at=now - 3000, end_at=now - 1000,
                prompt_tokens=642, gen_tokens=500_000, total_tokens=500_642,
                prompt_time_s=4, gen_time_s=250, mtp_proposed=120_000,
                mtp_accepted=90_000,
            )
            session.add_all([first, second])
            # These deliberately reset between sessions.  A max-minus-min
            # telemetry aggregate would report only the latter run.
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=now - 5000, tokens_total=2_020_000),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=now - 2500, tokens_total=0),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=now - 1500, tokens_total=500_642),
            ])
            session.commit()

            summary = metrics.range_summary(session, provider.id, "today")
            page = metrics.models_page(session, provider.id, "today", "model", summary)
            selected = metrics.selected_stats(
                session, [model.id], provider.id, "today", summary,
            )
            compared = metrics.compare_models(
                session, [str(model.id)], provider.id, "today", summary=summary,
            )
            sparks = metrics.model_sparks(session, summary, "model")

            self.assertEqual(summary.acc[model.id].tokens, 2_520_642)
            self.assertEqual(summary.acc[model.id].prompt_tokens, 20_642)
            self.assertEqual(summary.acc[model.id].gen_tokens, 2_500_000)
            self.assertEqual(page["rows"][0]["tokens"], 2_520_642)
            self.assertEqual(selected["tokens"], 2_520_642)
            self.assertEqual(compared["models"][0]["tokens"], 2_520_642)
            self.assertEqual(sum(sparks[str(model.id)]), 2_520_642)


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
            self.assertEqual(version, str(database.SCHEMA_VERSION))
            self.assertIn("ix_telemetrysample_provider_ts", indexes)
            self.assertIn("ix_telemetrysample_model_ts", indexes)
            self.assertEqual(run.gen_time_s, 60)
            self.assertEqual(run.avg_gen_tps, 50)
            self.assertEqual(run.peak_gen_tps, 50)


class OverviewPerformanceRegressionTests(unittest.TestCase):
    def test_overview_aggregates_history_in_sql_without_python_materialization(self):
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
            legacy = Model(provider_id=provider.id, key="legacy", name="Legacy", color="#333333")
            session.add_all([first, second, legacy])
            session.commit()
            session.refresh(first)
            session.refresh(second)
            session.refresh(legacy)
            run = SessionRow(provider_id=provider.id, model_id=first.id,
                              start_at=day + 50, end_at=day + 500,
                              duration_s=0.45, prompt_tokens=5, gen_tokens=10, total_tokens=15,
                              prompt_time_s=2, gen_time_s=3,
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
                TelemetrySample(provider_id=provider.id, model_id=legacy.id,
                                ts=day + 160, state="GENERATING", tokens_total=50),
                TelemetrySample(provider_id=provider.id, model_id=first.id,
                                session_id=run.id, ts=day + 200, state="IDLE",
                                tokens_total=30, gen_total=30, gen_seconds_total=3),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=day + 250, state="IDLE", tokens_total=5,
                                gen_total=5, gen_seconds_total=1),
                TelemetrySample(provider_id=provider.id, model_id=legacy.id,
                                ts=day + 260, state="IDLE", tokens_total=120),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=day + 350, state="IDLE", tokens_total=12,
                                gen_total=12, gen_seconds_total=2),
            ])
            session.commit()

            # Architectural contract: Overview historical chart generation must
            # not materialize sample history through fetch_overview_samples.
            with patch.object(metrics, "fetch_overview_samples",
                              side_effect=AssertionError(
                                  "overview must not materialize history rows")):
                with patch("observatory.metrics.now_ms", return_value=now):
                    result = metrics.overview(session)

            self.assertEqual(result["today"]["tokens"], 87)
            self.assertEqual(result["today"]["sessions"], 1)
            daily = result["daily_volume"]
            self.assertEqual(len(daily["labels"]), 30)
            self.assertEqual(len(daily["inference_seconds"]), 30)
            self.assertEqual(daily["inference_seconds"][-1], 2)
            self.assertEqual(daily["prompt_tokens"][-1], 0)
            self.assertEqual(daily["generated_tokens"][-1], 17)
            self.assertEqual(daily["unclassified_tokens"][-1], 70)
            self.assertTrue(all(value == 0 for value in daily["generated_tokens"][:-1]))
            self.assertEqual(result["recent_sessions"][0]["model"], "A")
            self.assertEqual(result["current"]["model"], "B")

    def test_overview_today_sessions_excludes_prior_day_sessions_that_are_still_open(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            session.add_all([
                SessionRow(provider_id=provider.id, start_at=day - 1, end_at=None),
                SessionRow(provider_id=provider.id, start_at=day + 1, end_at=None),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            self.assertEqual(result["today"]["sessions"], 1)

    def test_overview_daily_sql_clamps_counter_resets_to_zero(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            session.add(model)
            session.commit()
            session.refresh(model)
            hour = 3_600_000
            # Rising counters, a hard reset, then growth again.  Positive-clamped
            # deltas must keep only the increases after each new baseline.
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 1 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40,
                                gen_seconds_total=10),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 2 * hour, tokens_total=200,
                                prompt_total=120, gen_total=80,
                                gen_seconds_total=12),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 3 * hour, tokens_total=7,
                                prompt_total=4, gen_total=3),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 4 * hour, tokens_total=17,
                                prompt_total=10, gen_total=7),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            daily = result["daily_volume"]
            self.assertEqual(daily["prompt_tokens"][-1], 66)     # 60 + 0 + 6
            self.assertEqual(daily["generated_tokens"][-1], 44)  # 40 + 0 + 4
            self.assertEqual(daily["unclassified_tokens"][-1], 0)
            # inference seconds: gen_seconds delta 2, then no gen delta after reset
            self.assertEqual(daily["inference_seconds"][-1], 2)

    def test_overview_daily_sql_falls_back_to_tps_when_phase_seconds_missing(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            session.add(model)
            session.commit()
            session.refresh(model)
            hour = 3_600_000
            # No phase-second counters at all; the current row's tps gauges
            # convert token deltas into seconds (120 tokens / 60 tps = 2.0).
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 1 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40,
                                prompt_tps=30.0, gen_tps=50.0),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 2 * hour, tokens_total=220,
                                prompt_total=120, gen_total=100,
                                prompt_tps=60.0, gen_tps=20.0),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            daily = result["daily_volume"]
            # prompt 60/60=1.0 + gen 60/20=3.0
            self.assertEqual(daily["inference_seconds"][-1], 4)

    def test_overview_daily_sql_preserves_legacy_total_only_counters(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            session.add(model)
            session.commit()
            session.refresh(model)
            hour = 3_600_000
            # Legacy telemetry recorded only tokens_total.
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 1 * hour, tokens_total=100),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 2 * hour, tokens_total=260),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            daily = result["daily_volume"]
            self.assertEqual(daily["prompt_tokens"][-1], 0)
            self.assertEqual(daily["generated_tokens"][-1], 0)
            self.assertEqual(daily["unclassified_tokens"][-1], 160)

    def test_overview_daily_sql_null_model_rows_never_advance_chains(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            session.add(model)
            session.commit()
            session.refresh(model)
            hour = 3_600_000
            # NULL-model rows between real samples must not break the model chain
            # and must not contribute deltas themselves.
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 1 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40),
                TelemetrySample(provider_id=provider.id, model_id=None,
                                ts=day + 1 * hour + 1, tokens_total=5),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 2 * hour, tokens_total=180,
                                prompt_total=100, gen_total=80),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            daily = result["daily_volume"]
            self.assertEqual(daily["prompt_tokens"][-1], 40)
            self.assertEqual(daily["generated_tokens"][-1], 40)
            self.assertEqual(daily["unclassified_tokens"][-1], 0)
            self.assertEqual(daily["prompt_tokens"][-2], 0)

    def test_overview_hourly_sql_attributes_deltas_to_prev_hour_bucket(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            session.add(model)
            session.commit()
            session.refresh(model)
            hour = 3_600_000
            # now - 24h is the window start; a delta whose previous row sits in
            # the first hour must land in bucket 0, clamped, and a delta whose
            # previous row straddles the 24h edge must not spill negative.
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=now - 23 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=now - 22 * hour, tokens_total=160,
                                prompt_total=100, gen_total=60),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=now - 1, tokens_total=300,
                                prompt_total=200, gen_total=100),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            usage = result["usage_24h"]
            self.assertEqual(len(usage["series"]), 1)
            data = usage["series"][0]["data"]
            self.assertEqual(len(data), 24)
            # Delta 60 (prev at now-23h -> bucket 1) + 140 (prev at now-22h -> bucket 2)
            self.assertEqual(data[0], 0)
            self.assertEqual(data[1], 60)
            self.assertEqual(data[2], 140)
            self.assertEqual(sum(data), 200)

    def test_overview_hourly_sql_requires_consecutive_same_model_rows(self):
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
            hour = 3_600_000
            # Interleaved models: consecutive rows never share a model, so the
            # 24h chain (consecutive-row semantics) produces no deltas.
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=first.id,
                                ts=now - 2 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=now - 2 * hour + 1, tokens_total=10,
                                prompt_total=6, gen_total=4),
                TelemetrySample(provider_id=provider.id, model_id=first.id,
                                ts=now - 1 * hour, tokens_total=200,
                                prompt_total=120, gen_total=80),
                TelemetrySample(provider_id=provider.id, model_id=second.id,
                                ts=now - 1 * hour + 1, tokens_total=30,
                                prompt_total=16, gen_total=14),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            self.assertEqual(result["usage_24h"]["series"], [])
            # Daily chart (per-model partitioning) still accumulates.
            daily = result["daily_volume"]
            self.assertEqual(daily["prompt_tokens"][-1], 70)
            self.assertEqual(daily["generated_tokens"][-1], 50)

    def test_overview_daily_sql_keeps_orphan_model_ids_without_model_rows(self):
        """Telemetry with a non-NULL model_id that has no Model row (historical
        or orphan ids) must still participate in the daily per-model chain,
        exactly as the previous Python loop aggregated every non-NULL id."""
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            hour = 3_600_000
            orphan_id = 999_999  # no Model row ever created for this id
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=orphan_id,
                                ts=day + 1 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40),
                TelemetrySample(provider_id=provider.id, model_id=orphan_id,
                                ts=day + 2 * hour, tokens_total=220,
                                prompt_total=130, gen_total=90),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            daily = result["daily_volume"]
            self.assertEqual(daily["prompt_tokens"][-1], 70)
            self.assertEqual(daily["generated_tokens"][-1], 50)
            self.assertEqual(daily["unclassified_tokens"][-1], 0)

    def test_overview_daily_sql_rounds_half_ties_like_python(self):
        """Python round() resolves .5 ties to even; SQLite ROUND() rounds away
        from zero.  Per-row rounding must reproduce the Python behavior, most
        visibly for tps-derived inference_seconds."""
        engine = memory_engine()
        now = 1_800_000_000_000
        day = metrics.local_day_start_ms(now)
        with Session(engine) as session:
            provider = Provider(name="p", base_url="http://p", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="a", name="A", color="#111111")
            session.add(model)
            session.commit()
            session.refresh(model)
            hour = 3_600_000
            # Three rows whose gen_tps fall back produces exact .5 values:
            #   row2: gen delta 1 token / tps 2.0 = 0.5 -> Python round(0.5) = 0
            #   row3: gen delta 3 tokens / tps 2.0 = 1.5 -> Python round(1.5) = 2
            #   row4: gen delta 1 token / tps 2.0 = 0.5 -> 0
            # Old Python total = 0 + 2 + 0 = 2 (SQLite ROUND would give 1+2+1=4).
            session.add_all([
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 1 * hour, tokens_total=100,
                                prompt_total=60, gen_total=40,
                                gen_tps=2.0),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 2 * hour, tokens_total=101,
                                prompt_total=60, gen_total=41,
                                gen_tps=2.0),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 3 * hour, tokens_total=104,
                                prompt_total=60, gen_total=44,
                                gen_tps=2.0),
                TelemetrySample(provider_id=provider.id, model_id=model.id,
                                ts=day + 4 * hour, tokens_total=105,
                                prompt_total=60, gen_total=45,
                                gen_tps=2.0),
            ])
            session.commit()
            with patch("observatory.metrics.now_ms", return_value=now):
                result = metrics.overview(session)
            daily = result["daily_volume"]
            self.assertEqual(daily["prompt_tokens"][-1], 0)
            self.assertEqual(daily["generated_tokens"][-1], 5)
            self.assertEqual(daily["inference_seconds"][-1], 2)

    def test_overview_py_round_sql_helper_matches_python(self):
        """The SQL tie expression must agree with Python round() on the cases
        that distinguish them, for the non-negative delta values used here."""
        engine = memory_engine()
        with Session(engine) as session:
            conn = session.connection()
            for value in (0.5, 1.5, 2.5, 3.5, 4.5, 7.5, 10.5, 0.4, 0.6,
                          2.49999, 2.50001, 100.5, 0.0, 1.0, 123.456):
                got = conn.exec_driver_sql(
                    f"SELECT {metrics._py_round_sql(str(value))}").scalar()
                self.assertEqual(got, round(value), f"mismatch at {value}")


if __name__ == "__main__":
    unittest.main()
