"""P07 Hardware SQL-aggregation regression tests.

Contract: the /api/hardware payload must be semantically identical to the
historical Python chain (fetch_samples + Python bucketing + _gpu_rows) for
every existing field: labels and their order, all six host series values
including per-bucket rounding and None placement, graphs.gpus order and
content, inventory enrichment, and the surrounding response shape.  The
comparison is structured-dict equality against a reference implementation
of the old chain — no assertion depends on JSON serialization byte order.

Fixtures cover dense data, multi-GPU grouping, sparse gaps, NULL gauges,
future-stamped rows, boundary samples, malformed inventory JSON, and empty
databases.  An architectural gate asserts the new path never calls
fetch_samples/_gpu_rows.
"""
import json
import unittest
from datetime import datetime
from sqlmodel import Session, select

from observatory import metrics
from observatory.models import (BuildInfo, GpuTelemetrySample, HardwareInfo,
                                Model, Provider, TelemetrySample)

from tests.test_p03_model_detail import NOW, MINUTE, HOUR, PinnedNow, \
    memory_engine


def _add_provider(s, name="router"):
    p = Provider(name=name, base_url=f"http://{name}", status="LIVE")
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _add_models_and_multi_gpu(s, p):
    """Model + HardwareInfo(2 GPUs) + two GpuTelemetrySample rows + build."""
    m = Model(provider_id=p.id, key="m", name="model", family="fam",
              first_seen_at=NOW - HOUR)
    s.add(m)
    s.commit()
    s.refresh(m)
    s.add(HardwareInfo(provider_id=p.id, hostname="host", os_name="linux",
                       gpus=json.dumps([
                           {"index": 0, "uuid": "GPU-aaa", "name": "RTX 4090",
                            "vram_mb": 24576},
                           {"index": 1, "uuid": "GPU-bbb", "name": "RTX 4090",
                            "vram_mb": 24576},
                       ]), last_seen_at=NOW))
    s.add(BuildInfo(provider_id=p.id, version="b10700", commit="d355b48",
                    last_seen_at=NOW))
    for offset, util in ((-30_000, 25.0), (-10_000, 35.0)):
        for index, key in ((0, "GPU-aaa"), (1, "GPU-bbb")):
            s.add(GpuTelemetrySample(
                provider_id=p.id, ts=NOW + offset, gpu_key=key,
                gpu_index=index, gpu_uuid=key, name="RTX 4090",
                util=util + index, vram_used_mb=6_000.0 + index * 100,
                vram_total_mb=24576.0, temp_c=50.0 - index,
                power_w=40.0 - index, pcie="0000:01:00.0",
                active_model_ids=json.dumps([m.id]),
                active_session_ids="[]"))
    s.commit()
    return m


def _hardware_info(s, p):
    return s.exec(select(HardwareInfo).where(
        HardwareInfo.provider_id == p.id)).first()


class ReferenceHardware:
    """Reference implementation of the historical Python chain."""

    @staticmethod
    def hardware(s, provider_id=None):
        provs = list(s.exec(select(Provider)).all())
        if provider_id:
            provs = [p for p in provs if p.id == provider_id]
        now = metrics.now_ms()
        return {"providers": [ReferenceHardware._provider(s, p, now)
                              for p in provs], "now": now}

    @staticmethod
    def _provider(s, p, now):
        hw = s.exec(select(HardwareInfo).where(
            HardwareInfo.provider_id == p.id).order_by(
            HardwareInfo.id.desc())).first()
        b = metrics._latest_build(s, p.id)
        h1 = now - 3600_000
        samples = metrics.fetch_samples(s, [p.id], h1)
        bucket = 30
        nb = max(2, 3600 // bucket)
        series = {k: [None] * nb for k in ("gpu_util", "vram_mb", "gpu_temp",
                                           "gpu_power", "cpu_pct", "ram_mb")}
        sums = {k: [[0.0, 0] for _ in range(nb)] for k in series}
        for r in samples:
            i = min(nb - 1, max(0, int((r.ts - h1) / 1000 / bucket)))
            for key, attr in (("gpu_util", "gpu_util"),
                              ("vram_mb", "vram_used_mb"),
                              ("gpu_temp", "gpu_temp"),
                              ("gpu_power", "gpu_power_w"),
                              ("cpu_pct", "cpu_pct"),
                              ("ram_mb", "ram_used_mb")):
                v = getattr(r, attr)
                if v is not None:
                    sums[key][i][0] += v
                    sums[key][i][1] += 1
        for key, lst in sums.items():
            for i, (tot, n) in enumerate(lst):
                series[key][i] = round(tot / n, 1) if n else None
        labels = [datetime.fromtimestamp(
            (h1 + i * bucket * 1000) / 1000.0).strftime("%H:%M")
            for i in range(nb)]
        gpus = []
        if hw:
            try:
                gpus = json.loads(hw.gpus or "[]")
            except ValueError:
                gpus = []
        gpu_data = metrics._gpu_series(
            metrics._gpu_rows(s, p.id, h1, now), h1, now, bucket)
        latest_by_index = {gpu["index"]: gpu for gpu in gpu_data}
        for fallback_index, gpu in enumerate(gpus):
            index = int(gpu.get("index", fallback_index))
            live = latest_by_index.get(index)
            gpu["index"] = index
            gpu["uuid"] = gpu.get("uuid") or (live.get("uuid") if live else None)
            gpu["color"] = live.get("color") if live else metrics._gpu_color(
                gpu.get("uuid") or f"index:{index}")
            if live:
                current = live["current"]
                gpu.update({"util": current["util"],
                            "vram_used_mb": current["vram_mb"],
                            "temp_c": current["temp_c"],
                            "power_w": current["power_w"]})
        return {
            "provider": p.name,
            "status": p.status,
            "hardware": {
                "hostname": hw.hostname, "os": hw.os_name, "kernel": hw.kernel,
                "cpu": hw.cpu_model, "cpu_threads": hw.cpu_threads,
                "ram_mb": hw.ram_mb, "gpus": gpus,
                "nvidia_driver": hw.nvidia_driver, "cuda": hw.cuda,
                "pcie": hw.pcie, "source": hw.source,
                "updated": metrics._fmt_ago(hw.last_seen_at, now) if hw else None,
            } if hw else None,
            "build": {
                "version": b.version, "commit": b.commit,
                "docker_image": b.docker_image, "container_id": b.container_id,
                "source": "llama.cpp /props",
                "updated": metrics._fmt_ago(b.last_seen_at, now) if b else None,
            } if b else None,
            "graphs": {"labels": labels, "series": series, "gpus": gpu_data},
        }


def assert_semantic_equal(testcase, expected, actual, path="$"):
    """Deep equality with a path-annotated failure message."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        testcase.assertEqual(sorted(expected.keys()), sorted(actual.keys()),
                             f"key set mismatch at {path}")
        for key in expected:
            assert_semantic_equal(testcase, expected[key], actual[key],
                                  f"{path}.{key}")
    elif isinstance(expected, list) and isinstance(actual, list):
        testcase.assertEqual(len(expected), len(actual),
                             f"length mismatch at {path}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            assert_semantic_equal(testcase, e, a, f"{path}[{i}]")
    else:
        testcase.assertEqual(expected, actual, f"value mismatch at {path}")


class GoldenContractTests(unittest.TestCase):
    """hardware() output must equal the historical Python chain."""

    def _compare(self, s, provider_id=None):
        with PinnedNow():
            expected = ReferenceHardware.hardware(s, provider_id)
            actual = metrics.hardware(s, provider_id)
        assert_semantic_equal(self, expected, actual)
        return actual

    def test_empty_database(self):
        engine = memory_engine()
        with Session(engine) as s:
            payload = self._compare(s)
        self.assertEqual(payload["providers"], [])

    def test_provider_without_telemetry(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s, "idle")
            payload = self._compare(s)
        self.assertEqual(payload["providers"][0]["hardware"], None)
        self.assertEqual(payload["providers"][0]["build"], None)
        self.assertTrue(all(v is None for v in
                            payload["providers"][0]["graphs"]
                            ["series"]["gpu_util"]))

    def test_dense_samples_match_python_bucketing(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            for i in range(180):
                ts = NOW - (180 - i) * 10_000
                s.add(TelemetrySample(
                    provider_id=p.id, ts=ts, state="GENERATING",
                    gpu_util=30.0 + i % 40, vram_used_mb=8_000.0 + i,
                    gpu_temp=50.0 + (i % 20), gpu_power_w=200.0 + i % 50,
                    cpu_pct=10.0 + i % 30, ram_used_mb=40_000.0 + i * 3))
            s.commit()
            payload = self._compare(s)
        prov = payload["providers"][0]
        self.assertEqual(len(prov["graphs"]["labels"]), 120)
        self.assertTrue(any(v is not None
                            for v in prov["graphs"]["series"]["gpu_util"]))

    def test_multi_gpu_grouping_and_inventory_enrichment(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            payload = self._compare(s)
        prov = payload["providers"][0]
        self.assertEqual(len(prov["graphs"]["gpus"]), 2)
        self.assertEqual([g["index"] for g in prov["graphs"]["gpus"]], [0, 1])
        inv = prov["hardware"]["gpus"]
        self.assertEqual([g["index"] for g in inv], [0, 1])
        self.assertEqual(inv[0]["uuid"], "GPU-aaa")
        self.assertIsNotNone(inv[0]["util"])
        self.assertEqual(inv[0]["color"], prov["graphs"]["gpus"][0]["color"])
        self.assertEqual(prov["build"]["source"], "llama.cpp /props")

    def test_sparse_gaps_and_null_gauges(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            for offset, util in ((-30 * MINUTE, 40.0), (-20 * MINUTE, None),
                                 (-10 * MINUTE, 55.5), (-2 * MINUTE, 60.0)):
                s.add(TelemetrySample(
                    provider_id=p.id, ts=NOW + offset, state="IDLE",
                    gpu_util=util, vram_used_mb=None,
                    cpu_pct=12.0 if util is not None else None))
            s.commit()
            payload = self._compare(s)
        series = payload["providers"][0]["graphs"]["series"]
        self.assertEqual(series["vram_mb"], [None] * 120)
        self.assertEqual(series["gpu_util"][60], 40.0)
        self.assertEqual(series["gpu_util"][100], 55.5)

    def test_future_stamped_rows_clamp_into_last_bucket(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            s.add(TelemetrySample(provider_id=p.id, ts=NOW + 5 * MINUTE,
                                  state="IDLE", gpu_util=77.0, cpu_pct=33.0))
            s.commit()
            payload = self._compare(s)
        series = payload["providers"][0]["graphs"]["series"]
        self.assertEqual(series["gpu_util"][119], 77.0)
        self.assertEqual(series["cpu_pct"][119], 33.0)

    def test_boundary_sample_at_window_start(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            s.add(TelemetrySample(provider_id=p.id, ts=NOW - HOUR,
                                  state="IDLE", gpu_util=11.0))
            s.commit()
            self._compare(s)

    def test_malformed_inventory_json_survives(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            hw = _hardware_info(s, p)
            hw.gpus = "{not json"
            s.add(hw)
            s.commit()
            payload = self._compare(s)
        self.assertEqual(payload["providers"][0]["hardware"]["gpus"], [])

    def test_provider_filter_matches_full_payload(self):
        engine = memory_engine()
        with Session(engine) as s:
            second = None
            for name in ("a", "b"):
                p = _add_provider(s, name)
                _add_models_and_multi_gpu(s, p)
                for k in range(5):
                    s.add(TelemetrySample(
                        provider_id=p.id, ts=NOW - (60 - k * 5) * MINUTE,
                        state="IDLE", gpu_util=10.0 + k,
                        ram_used_mb=30_000.0 + k))
                s.commit()
                if name == "b":
                    second = p
            with PinnedNow():
                full = metrics.hardware(s)
                scoped = metrics.hardware(s, second.id)
        self.assertEqual(len(full["providers"]), 2)
        self.assertEqual(len(scoped["providers"]), 1)
        self.assertEqual(scoped["providers"][0]["provider"], "b")
        assert_semantic_equal(self, full["providers"][1], scoped["providers"][0])

    def test_hardware_does_not_materialize_raw_sample_set(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = _add_provider(s)
            _add_models_and_multi_gpu(s, p)
            for i in range(150):
                s.add(TelemetrySample(provider_id=p.id,
                                      ts=NOW - (150 - i) * 20_000,
                                      state="IDLE", gpu_util=40.0))
            s.commit()

            def spy(*args, **kwargs):
                raise AssertionError("hardware must not materialize raw rows")

            with PinnedNow():
                with Session(engine) as s2:
                    for name in ("fetch_samples", "_gpu_rows"):
                        original = getattr(metrics, name)
                        setattr(metrics, name, spy)
                        try:
                            metrics.hardware(s2)
                        finally:
                            setattr(metrics, name, original)


if __name__ == "__main__":
    unittest.main()