"""Model Detail's GPU series is bucketed in SQL, identically (issue #70).

`_gpu_series` bucketed in Python from every row in the window: a seven-day
Model Detail request fetched 34,078 GPU samples to draw 168 points per GPU,
about 4.4 s of the endpoint. `_gpu_series_sql_for_model` does the same
arithmetic in SQLite and returns the same structure.

Four other surfaces still use the Python `_gpu_series`, so the two must agree
exactly. That is what these tests assert -- not that the SQL path looks
plausible, but that it is indistinguishable from the path it replaces.
"""
from __future__ import annotations

import json
import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import metrics
from observatory.models import (GpuTelemetrySample, Model, Provider, now_ms)

MINUTE_MS = 60_000


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class GpuSeriesEquivalenceTests(unittest.TestCase):

    def _seed(self, session, now, minutes=240, gpus=(0, 1),
              model_ids="[1]", sparse_from=None):
        provider = Provider(name="p", base_url="http://x")
        session.add(provider)
        session.commit()
        session.refresh(provider)
        model = Model(provider_id=provider.id, key="m", name="m")
        session.add(model)
        session.commit()
        session.refresh(model)
        for i in range(minutes):
            ts = now - (minutes - i) * MINUTE_MS
            for index in gpus:
                # Leave some metrics null so the None-vs-average branches and
                # the per-metric counts are actually exercised.
                sparse = sparse_from is not None and i >= sparse_from
                session.add(GpuTelemetrySample(
                    provider_id=provider.id, ts=ts,
                    gpu_key=f"gpu:{index}", gpu_index=index,
                    gpu_uuid=f"uuid-{index}", name=f"GPU{index}",
                    pcie="8.0 GT/s", vram_total_mb=24576.0,
                    util=None if sparse else float(40 + index * 5 + i % 7),
                    vram_used_mb=float(1000 + i),
                    temp_c=None if sparse else float(60 + i % 3),
                    power_w=float(120 + i % 11),
                    active_model_ids=model_ids))
        session.commit()
        return provider.id, model.id

    def _both(self, session, provider_id, model_id, start, end, bucket_s):
        python_path = metrics._gpu_series(
            metrics._gpu_rows_for_model(session, provider_id, start, end, model_id),
            start, end, bucket_s)
        sql_path = metrics._gpu_series_sql_for_model(
            session, provider_id, model_id, start, end, bucket_s)
        return python_path, sql_path

    def _assert_identical(self, a, b, label):
        self.assertEqual(json.dumps(a, sort_keys=True, default=str),
                         json.dumps(b, sort_keys=True, default=str),
                         "SQL and Python GPU series differ for %s" % label)

    def test_identical_across_bucket_sizes(self):
        engine = memory_engine()
        with Session(engine) as session:
            now = now_ms()
            provider_id, model_id = self._seed(session, now)
            for window_min, bucket_s in ((60, 60), (240, 300), (240, 3600)):
                start = now - window_min * MINUTE_MS
                with self.subTest(window_min=window_min, bucket_s=bucket_s):
                    py, sql = self._both(session, provider_id, model_id,
                                         start, now, bucket_s)
                    self.assertTrue(sql, "expected GPU rows")
                    self._assert_identical(py, sql, f"{window_min}m/{bucket_s}s")

    def test_identical_with_null_metrics(self):
        engine = memory_engine()
        with Session(engine) as session:
            now = now_ms()
            provider_id, model_id = self._seed(session, now, sparse_from=120)
            start = now - 240 * MINUTE_MS
            py, sql = self._both(session, provider_id, model_id, start, now, 300)
            self._assert_identical(py, sql, "null metrics")
            # The nulls must actually reach the output, or this proves nothing.
            self.assertIn(None, sql[0]["series"]["util"])

    def test_identical_when_rows_predate_the_window(self):
        """Samples before start clamp into bucket 0 in both paths."""
        engine = memory_engine()
        with Session(engine) as session:
            now = now_ms()
            provider_id, model_id = self._seed(session, now, minutes=240)
            start = now - 60 * MINUTE_MS
            py, sql = self._both(session, provider_id, model_id,
                                 now - 240 * MINUTE_MS, now, 300)
            self._assert_identical(py, sql, "clamped")
            py2, sql2 = self._both(session, provider_id, model_id, start, now, 60)
            self._assert_identical(py2, sql2, "narrow window")

    def test_model_membership_is_respected(self):
        engine = memory_engine()
        with Session(engine) as session:
            now = now_ms()
            provider_id, model_id = self._seed(session, now, model_ids="[999]")
            start = now - 240 * MINUTE_MS
            py, sql = self._both(session, provider_id, model_id, start, now, 300)
            self.assertEqual([], sql, "a model that owns no sample gets nothing")
            self._assert_identical(py, sql, "non-member")

    def test_empty_window_returns_nothing_in_both(self):
        engine = memory_engine()
        with Session(engine) as session:
            now = now_ms()
            provider_id, model_id = self._seed(session, now)
            start = now + 10 * MINUTE_MS
            py, sql = self._both(session, provider_id, model_id,
                                 start, start + MINUTE_MS, 60)
            self.assertEqual([], sql)
            self._assert_identical(py, sql, "empty")

    def test_latest_sample_supplies_the_current_reading(self):
        engine = memory_engine()
        with Session(engine) as session:
            now = now_ms()
            provider_id, model_id = self._seed(session, now)
            start = now - 240 * MINUTE_MS
            py, sql = self._both(session, provider_id, model_id, start, now, 300)
            self._assert_identical(py, sql, "current")
            for gpu in sql:
                self.assertIsNotNone(gpu["current"]["util"])
                self.assertEqual(gpu["uuid"], "uuid-%d" % gpu["index"])


if __name__ == "__main__":
    unittest.main()
