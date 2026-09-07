"""Issue #47: retention must stay bounded and never stop collector progress."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory.collector import Collector, _bucketize, _bucketize_gpu
from observatory.models import GpuTelemetrySample, TelemetrySample


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class RetentionBoundedWorkTests(unittest.TestCase):
    def _loaded_during(self, model, operation) -> list[int]:
        loaded: list[int] = []

        def record(row, _context):
            loaded.append(row.ts)

        event.listen(model, "load", record)
        try:
            operation()
        finally:
            event.remove(model, "load", record)
        return loaded

    def test_telemetry_bucketize_materializes_only_unbucketed_rows(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add_all([
                TelemetrySample(provider_id=1, model_id=1, ts=ts,
                                tokens_total=ts, state="IDLE")
                for ts in range(1000, 201000, 1000)
            ])
            session.add_all([
                TelemetrySample(provider_id=1, model_id=1, ts=200101,
                                tokens_total=1, state="IDLE"),
                TelemetrySample(provider_id=1, model_id=1, ts=200901,
                                tokens_total=2, state="IDLE"),
            ])
            session.commit()

        with Session(engine) as session:
            loaded = self._loaded_during(
                TelemetrySample,
                lambda: _bucketize(session, 1000, 202000, 1000),
            )
        self.assertEqual(sorted(loaded), [200101, 200901])

    def test_gpu_bucketize_materializes_only_unbucketed_rows(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add_all([
                GpuTelemetrySample(
                    provider_id=1, ts=ts, gpu_key="gpu", gpu_index=0,
                    util=10, active_model_ids="[]", active_session_ids="[]",
                )
                for ts in range(1000, 201000, 1000)
            ])
            session.add_all([
                GpuTelemetrySample(
                    provider_id=1, ts=200101, gpu_key="gpu", gpu_index=0,
                    util=20, active_model_ids=json.dumps([1]),
                    active_session_ids=json.dumps([1]),
                ),
                GpuTelemetrySample(
                    provider_id=1, ts=200901, gpu_key="gpu", gpu_index=0,
                    util=30, active_model_ids=json.dumps([2]),
                    active_session_ids=json.dumps([2]),
                ),
            ])
            session.commit()

        with Session(engine) as session:
            loaded = self._loaded_during(
                GpuTelemetrySample,
                lambda: _bucketize_gpu(session, 1000, 202000, 1000),
            )
        self.assertEqual(sorted(loaded), [200101, 200901])


class RetentionCollectorProgressTests(unittest.TestCase):
    def test_long_retention_pass_does_not_block_collector_ticks(self):
        retention_started = threading.Event()
        release_retention = threading.Event()
        ticks = 0
        ticks_lock = threading.Lock()

        def slow_retention():
            retention_started.set()
            self.assertTrue(release_retention.wait(timeout=5))

        collector = Collector(lambda _provider: None)

        def tick():
            nonlocal ticks
            with ticks_lock:
                ticks += 1

        collector._tick = tick
        collector._last_retention = 0
        with patch("observatory.collector.run_retention", slow_retention):
            collector.start()
            try:
                self.assertTrue(retention_started.wait(timeout=2))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with ticks_lock:
                        if ticks >= 3:
                            break
                    time.sleep(0.01)
                with ticks_lock:
                    self.assertGreaterEqual(ticks, 3)
                self.assertTrue(collector._retention_thread.is_alive())
            finally:
                release_retention.set()
                collector.stop()


if __name__ == "__main__":
    unittest.main()
