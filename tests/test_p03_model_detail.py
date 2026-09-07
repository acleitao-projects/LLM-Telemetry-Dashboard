"""P03 Model Detail backend SQL-aggregation regression tests.

Contract: the /api/model/{mid} payload must be byte-identical between the
historical Python chain (fetch_samples + accumulate + Python bucketing)
and the SQL path now wired into model_detail.  Fixtures cover reset
handling, sparse data, counter gaps, NULL gauges, future-stamped rows,
multi-GPU data, session/today ranges, and missing models.
"""
import calendar
import json
import unittest
from datetime import datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from observatory import metrics
from observatory.models import (GpuTelemetrySample, Model, ModelConfig,
                                Provider, SessionRow, TelemetrySample)

NOW = calendar.timegm((2026, 9, 1, 14, 30, 0, 0, 0, 0)) * 1000
MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000


class PinnedNow:
    """Pin metrics.now_ms for deterministic payloads."""

    def __init__(self, now=NOW):
        self.now = now

    def __enter__(self):
        self._token = metrics.now_ms
        metrics.now_ms = lambda: self.now
        return self

    def __exit__(self, *a):
        metrics.now_ms = self._token


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def _provider_model(s, name="m", with_config=False):
    p = Provider(name="router", base_url="http://router", status="LIVE")
    s.add(p)
    s.commit()
    s.refresh(p)
    m = Model(provider_id=p.id, key=name, name=name, family="fam",
              first_seen_at=NOW - 30 * DAY)
    s.add(m)
    s.commit()
    s.refresh(m)
    if with_config:
        s.add(ModelConfig(model_id=m.id, fingerprint="fp", context=8192,
                          threads=4, created_at=NOW - DAY))
        s.commit()
    return p, m


class _OldPath:
    """Reference implementations of the historical Python chains."""

    @staticmethod
    def model_detail(s, model_id, range_key):
        now = metrics.now_ms()
        m = s.get(Model, model_id)
        if m is None:
            return {}
        provider = s.get(Provider, m.provider_id)
        start = metrics.range_start_ms(range_key, now)
        samples = metrics.fetch_samples(s, [m.provider_id], start,
                                        model_ids=[model_id])
        acc: dict[int, metrics.ModelAcc] = {}
        metrics.accumulate(samples, acc, start, now, now)
        a = acc.get(model_id) or metrics.ModelAcc()
        sess = metrics.sessions_in_range(s, [m.provider_id], start,
                                         model_ids=[model_id])
        inference = a.prompt_time + a.gen_time
        dts = _OldPath._range_detail_py(s, model_id, range_key, samples,
                                        start, now)
        gpu_start = now - 60_000 if range_key == "session" else start
        gpu_bucket = metrics.RANGE_BUCKETS["1m"] if range_key == "session" \
            else metrics.RANGE_BUCKETS.get(range_key, metrics.RANGE_BUCKETS["24h"])
        dts["gpus"] = metrics._gpu_series(
            metrics._gpu_rows(s, m.provider_id, gpu_start, now,
                              model_id=model_id),
            gpu_start, now, gpu_bucket,
        )
        cfgs = list(s.exec(select(ModelConfig)
                           .where(ModelConfig.model_id == model_id)
                           .order_by(ModelConfig.created_at.desc())).all())
        mtp_acc = (a.d_accepted / a.d_proposed * 100.0) if a.d_proposed > 0 else None
        avg_gen = (a.gen_tokens / a.gen_time) if a.gen_time > 0 else None
        avg_prompt = (a.prompt_tokens / a.prompt_time) if a.prompt_time > 0 else None
        return {
            "accounting": {
                "loaded_s": round(a.loaded_time),
                "idle_s": round(a.idle_time),
                "prompt_s": round(a.prompt_time),
                "gen_s": round(a.gen_time),
                "inference_s": round(inference),
                "utilization": round(inference / a.loaded_time * 100.0, 1)
                if a.loaded_time > 0 else None,
            },
            "sessions": len(sess),
            "tokens": {"prompt": round(a.prompt_tokens),
                       "generated": round(a.gen_tokens),
                       "total": round(a.tokens)},
            "speeds": {
                "avg_gen_tps": round(avg_gen, 1) if avg_gen else None,
                "peak_gen_tps": round(a.peak_gen, 1) if a.peak_gen else None,
                "avg_prompt_tps": round(avg_prompt, 1) if avg_prompt else None,
                "peak_prompt_tps": round(a.peak_prompt, 1) if a.peak_prompt else None,
            },
            "mtp_acc": round(mtp_acc, 1) if mtp_acc is not None else None,
            "mtp_proposed": round(a.d_proposed),
            "mtp_accepted": round(a.d_accepted),
            "context": {"max_used": a.context_max},
            "graphs": dts,
            "range": range_key,
            "now": now,
        }

    @staticmethod
    def _range_detail_py(s, model_id, range_key, samples, start, now):
        if range_key == "session":
            bucket = metrics.RANGE_BUCKETS["1m"]
            start = now - 60_000
            samples = [r for r in samples if r.ts >= start]
        else:
            bucket = metrics.RANGE_BUCKETS.get(
                range_key, metrics.RANGE_BUCKETS["24h"])
        span = max(1, now - start)
        nb = max(2, int(span / 1000 / bucket))
        series = {
            "gen_tps": [None] * nb, "prompt_tps": [None] * nb,
            "tokens": [0] * nb, "context": [None] * nb,
            "mtp_acc": [None] * nb, "inference_s": [0] * nb,
            "gpu_util": [None] * nb, "vram_mb": [None] * nb,
        }
        sums = {k: [[0.0, 0] for _ in range(nb)] for k in
                ("gen_tps", "prompt_tps", "context", "mtp_acc",
                 "gpu_util", "vram_mb")}
        for r in samples:
            i = min(nb - 1, max(0, int((r.ts - start) / 1000 / bucket)))
            for key, attr in (("gen_tps", "gen_tps"),
                              ("prompt_tps", "prompt_tps"),
                              ("context", "context_used"),
                              ("mtp_acc", "mtp_acc"),
                              ("gpu_util", "gpu_util"),
                              ("vram_mb", "vram_used_mb")):
                v = getattr(r, attr)
                if v is not None:
                    sums[key][i][0] += v
                    sums[key][i][1] += 1
        for key, lst in sums.items():
            for i, (tot, n) in enumerate(lst):
                series[key][i] = round(tot / n, 1) if n else None
        prev = None
        for r in samples:
            i = min(nb - 1, max(0, int((r.ts - start) / 1000 / bucket)))
            if prev is not None:
                d = metrics._delta(r, prev, "tokens_total")
                if d == 0:
                    d = (metrics._delta(r, prev, "prompt_total")
                         + metrics._delta(r, prev, "gen_total"))
                series["tokens"][i] += int(d)
                phase_s = metrics._phase_delta(
                    r, prev, "prompt_total", "prompt_seconds_total",
                    "prompt_tps")
                phase_s += metrics._phase_delta(
                    r, prev, "gen_total", "gen_seconds_total", "gen_tps")
                series["inference_s"][i] += round(phase_s)
            prev = r
        labels = [datetime.fromtimestamp(
            (start + i * bucket * 1000) / 1000.0).strftime(
            "%H:%M" if bucket < 3600 else "%m-%d %H:%M") for i in range(nb)]
        return {"labels": labels, "series": {k: v for k, v in series.items()}}


class GoldenContractTests(unittest.TestCase):
    """model_detail output must equal the historical Python chain."""

    def _detail_matches(self, engine, model_id, range_key):
        with PinnedNow():
            with Session(engine) as s:
                old = _OldPath.model_detail(s, model_id, range_key)
                new = metrics.model_detail(s, model_id, range_key)
        # the reference path covers the aggregation contract keys; the full
        # payload (model identity, config, costs) is shape-verified in the
        # existing test_throughput / test_gpu_telemetry suites
        old_blob = json.dumps(old, sort_keys=True, default=repr)
        new_blob = json.dumps(
            {k: new[k] for k in old if k in new}, sort_keys=True, default=repr)
        self.assertEqual(
            old_blob, new_blob,
            f"{range_key} payload diverges:\nold={old_blob[:600]}"
            f"\nnew={new_blob[:600]}")

    def test_dense_counters_and_gauges_match_old_chain(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "dense")
            tok = 0.0
            for i in range(200):
                ts = NOW - (200 - i) * 5 * MINUTE
                pt, gt = 100 + i % 7, 40 + i % 5
                tok += pt + gt
                s.add(TelemetrySample(
                    provider_id=p.id, model_id=m.id, ts=ts,
                    state="GENERATING", tokens_total=tok,
                    prompt_total=tok * 0.7, gen_total=tok * 0.3,
                    prompt_seconds_total=i * 12.5,
                    gen_seconds_total=i * 40.5,
                    prompt_tps=240.0, gen_tps=45.5 + (i % 11),
                    context_used=1000 + (i % 40) * 50,
                    mtp_proposed_total=tok * 0.4,
                    mtp_accepted_total=tok * 0.3,
                    gpu_util=30.0 + (i % 20), vram_used_mb=6000 + (i % 9),
                ))
            s.commit()
            mid = m.id

        for range_key in ("24h", "7d", "today"):
            self._detail_matches(engine, mid, range_key)

    def test_session_range_matches_old_chain(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "live")
            tok = 0.0
            for i in range(30):
                ts = NOW - (30 - i) * 2_000
                tok += 50
                s.add(TelemetrySample(
                    provider_id=p.id, model_id=m.id, ts=ts,
                    state="GENERATING", tokens_total=tok,
                    gen_total=tok, gen_seconds_total=i * 1.5,
                    gen_tps=50.0, gpu_util=40.0 + i % 7,
                ))
            s.commit()
            mid = m.id
        self._detail_matches(engine, mid, "session")

    def test_counter_resets_and_sparse_gaps_match_old_chain(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "reset")
            rows = []
            # steady climb, reset, climb, NULL counters, sparse gap, future row
            tok = 5000.0
            for i in range(30):
                tok += 120
                rows.append((NOW - (35 - i) * 5 * MINUTE, tok, 40 + i))
            # reset: totals drop
            tok = 7.0
            for i in range(10):
                tok += 90
                rows.append((NOW - (5 - i) * 5 * MINUTE, tok, 33 + i))
            # NULL counters (gauge only)
            rows.append((NOW - 2 * HOUR - 30_000, None, 5))
            # sparse gap then a lone sample
            rows.append((NOW - 20 * HOUR, 400.0, 60))
            # future-stamped row (clock skew) participates like the old path
            rows.append((NOW + 3 * MINUTE, 900.0, 70))
            for ts, tok_v, util in rows:
                s.add(TelemetrySample(
                    provider_id=p.id, model_id=m.id, ts=ts,
                    state="GENERATING",
                    tokens_total=tok_v,
                    gen_total=tok_v,
                    prompt_total=tok_v * 0.6 if tok_v is not None else None,
                    prompt_seconds_total=(tok_v or 0) * 0.001,
                    gen_seconds_total=(tok_v or 0) * 0.002,
                    gen_tps=48.0 if tok_v is not None else None,
                    gpu_util=float(util),
                ))
            s.commit()
            mid = m.id

        for range_key in ("24h", "7d", "today"):
            self._detail_matches(engine, mid, range_key)

    def test_multi_gpu_series_match_python_bucketing(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "gpu")
            for i in range(60):
                ts = NOW - (60 - i) * 5 * MINUTE
                s.add(GpuTelemetrySample(
                    provider_id=p.id, ts=ts, gpu_key="GPU-aaa",
                    gpu_index=0, gpu_uuid="GPU-aaa", name="RTX 3060",
                    util=10.0 + (i % 30), vram_used_mb=6000.0 + (i % 5),
                    vram_total_mb=12288, temp_c=50.0, power_w=40.0,
                    active_model_ids=json.dumps([m.id, 777]),
                ))
                s.add(GpuTelemetrySample(
                    provider_id=p.id, ts=ts, gpu_key="GPU-bbb",
                    gpu_index=1, gpu_uuid="GPU-bbb", name="RTX 3060",
                    util=55.0 - (i % 20), vram_used_mb=7000.0,
                    vram_total_mb=12288, temp_c=49.0, power_w=39.0,
                    active_model_ids=json.dumps([777]),
                ))
            s.commit()

            with PinnedNow():
                start = NOW - DAY
                new = metrics._gpu_series(
                    metrics._gpu_rows_for_model(s, p.id, start, NOW, m.id),
                    start, NOW, 300)
                old = metrics._gpu_series(
                    metrics._gpu_rows(s, p.id, start, NOW, model_id=m.id),
                    start, NOW, 300)
        self.assertEqual(json.dumps(old, sort_keys=True),
                         json.dumps(new, sort_keys=True))

    def test_gpu_json_membership_is_exact_integer_match(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "member")
            # id 1's array contains 11 and 111 — neither equals m.id when the
            # filter is a plain string/substring match
            s.add(GpuTelemetrySample(
                provider_id=p.id, ts=NOW - MINUTE, gpu_key="GPU-x",
                gpu_index=0, gpu_uuid="GPU-x", util=10.0,
                active_model_ids=json.dumps([m.id * 10 + m.id, 42]),
            ))
            s.add(GpuTelemetrySample(
                provider_id=p.id, ts=NOW - MINUTE, gpu_key="GPU-y",
                gpu_index=1, gpu_uuid="GPU-y", util=20.0,
                active_model_ids=json.dumps([m.id]),
            ))
            s.commit()
            with PinnedNow():
                rows = metrics._gpu_rows_for_model(s, p.id, NOW - HOUR, NOW,
                                                   m.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].gpu_key, "GPU-y")

    def test_missing_model_returns_empty_dict(self):
        engine = memory_engine()
        with Session(engine) as s:
            self.assertEqual(metrics.model_detail(s, 9999, "24h"), {})

    def test_model_detail_survives_malformed_gpu_json(self):
        """model_detail must not crash on legacy/malformed active_model_ids."""
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "legacygpu")
            for ts, raw in ((NOW - 5 * MINUTE, "not json"),
                            (NOW - 4 * MINUTE, "[]"),
                            (NOW - 3 * MINUTE, "[1, 2"),
                            (NOW - 2 * MINUTE, "[{}]"),
                            (NOW - MINUTE, json.dumps([m.id]))):
                s.add(GpuTelemetrySample(
                    provider_id=p.id, ts=ts, gpu_key="GPU-l", gpu_index=0,
                    gpu_uuid="GPU-l", util=10.0, vram_used_mb=500.0,
                    active_model_ids=raw))
            s.commit()
            mid = m.id
        with PinnedNow():
            with Session(engine) as s:
                d = metrics.model_detail(s, mid, "24h")
        gpus = d["graphs"]["gpus"]
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["key"], "GPU-l")
        self.assertEqual(
            [v for v in gpus[0]["series"]["util"] if v is not None],
            [10.0])

    def test_empty_telemetry_yields_zeroed_accounting_and_empty_series(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "empty")
            with PinnedNow():
                d = metrics.model_detail(s, m.id, "24h")
        self.assertEqual(d["sessions"], 0)
        self.assertEqual(d["tokens"], {"prompt": 0, "generated": 0, "total": 0})
        self.assertEqual(d["accounting"]["inference_s"], 0)
        self.assertIsNone(d["accounting"]["utilization"])
        self.assertIsNone(d["speeds"]["avg_gen_tps"])
        self.assertEqual(d["graphs"]["series"]["tokens"],
                         [0] * len(d["graphs"]["labels"]))
        self.assertEqual(d["graphs"]["series"]["gen_tps"],
                         [None] * len(d["graphs"]["labels"]))

    def test_session_count_uses_sql_without_full_materialization(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "sess")
            for i in range(5):
                s.add(SessionRow(provider_id=p.id, model_id=m.id,
                                 start_at=NOW - 10 * HOUR + i * HOUR,
                                 end_at=NOW - 9 * HOUR + i * HOUR,
                                 duration_s=3600))
            # session that started before the window but overlaps it
            s.add(SessionRow(provider_id=p.id, model_id=m.id,
                             start_at=NOW - 9 * DAY, end_at=NOW - HOUR,
                             duration_s=8 * DAY))
            s.commit()
            with PinnedNow():
                count = metrics._count_sessions_in_range(
                    s, [p.id], NOW - 2 * DAY, model_ids=[m.id])
        self.assertEqual(count, 6)

    def test_config_projection_preserves_shape_without_payload_load(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "cfg")
            s.add(ModelConfig(model_id=m.id, fingerprint="fp1", context=4096,
                              threads=2, created_at=NOW - 2 * DAY,
                              payload='{"raw": true}'))
            s.add(ModelConfig(model_id=m.id, fingerprint="fp2", context=8192,
                              threads=8, created_at=NOW - DAY,
                              payload='{"raw": false}'))
            s.commit()
            with PinnedNow():
                d = metrics.model_detail(s, m.id, "24h")
        self.assertEqual(d["config"]["fingerprint"], "fp2")
        self.assertEqual(d["config"]["payload"], {})
        self.assertEqual([c["fingerprint"] for c in d["configs"]],
                         ["fp2", "fp1"])
        for c in d["configs"]:
            self.assertEqual(c["payload"], {})

    def test_model_detail_does_not_materialize_raw_sample_set(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, m = _provider_model(s, "arch")
            for i in range(150):
                s.add(TelemetrySample(
                    provider_id=p.id, model_id=m.id,
                    ts=NOW - (150 - i) * 5 * MINUTE, state="GENERATING",
                    tokens_total=float(100 * i), gen_total=float(40 * i),
                    gen_tps=50.0, gpu_util=30.0))
            s.commit()

            fetched = []

            def spy(*args, **kwargs):
                fetched.append(1)
                return metrics.fetch_samples(*args, **kwargs)

            with PinnedNow():
                with Session(engine) as s2:
                    original = metrics.fetch_samples
                    metrics.fetch_samples = spy
                    try:
                        metrics.model_detail(s2, m.id, "24h")
                    finally:
                        metrics.fetch_samples = original
        self.assertEqual(fetched, [])


if __name__ == "__main__":
    unittest.main()