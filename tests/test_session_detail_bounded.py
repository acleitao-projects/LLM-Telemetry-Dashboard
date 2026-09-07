"""Session Detail must stay bounded in a session's duration, not just its rows.

The chart used to be built at a fixed 2 s bucket derived from the session's
span, so the point count grew with *duration*.  A session left open by a crash
or an interrupted provider has no ``end_at``, so its span is "now minus start":
one spanning 30 h produced 54,286 points per series -- and the same again per
GPU -- taking roughly 16 s of CPU per request.
"""
from __future__ import annotations

import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import metrics
from observatory.models import (GpuTelemetrySample, Model, Provider, SessionRow,
                                TelemetrySample, now_ms)

HOUR_MS = 3_600_000


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class BucketSizingTests(unittest.TestCase):
    def test_short_sessions_keep_the_original_two_second_bucket(self):
        # Behaviour must be unchanged for ordinary sessions.
        for span_s in (1, 10, 120, 600):
            self.assertEqual(
                metrics.session_detail_bucket_s(0, span_s * 1000),
                metrics.SESSION_DETAIL_MIN_BUCKET_S,
                f"{span_s}s session should keep the 2s bucket")

    def test_bucket_widens_so_the_point_cap_holds(self):
        for span_s in (1_200, 7_200, 108_600, 30 * 86_400):
            bucket = metrics.session_detail_bucket_s(0, span_s * 1000)
            points = span_s // bucket
            self.assertLessEqual(points, metrics.SESSION_DETAIL_MAX_POINTS,
                                 f"{span_s}s session produced {points} points")

    def test_zero_length_session_does_not_divide_by_zero(self):
        self.assertGreaterEqual(metrics.session_detail_bucket_s(1000, 1000),
                                metrics.SESSION_DETAIL_MIN_BUCKET_S)


class SessionDetailBoundedTests(unittest.TestCase):
    def _seed(self, session, *, start_offset_ms, end_at, status):
        provider = Provider(name="prov", base_url="http://prov")
        session.add(provider)
        session.commit()
        session.refresh(provider)
        model = Model(provider_id=provider.id, key="m", name="m")
        session.add(model)
        session.commit()
        session.refresh(model)
        start = now_ms() - start_offset_ms
        row = SessionRow(provider_id=provider.id, model_id=model.id,
                         start_at=start, end_at=end_at, status=status)
        session.add(row)
        session.commit()
        session.refresh(row)
        for offset in (0, 1_000):
            session.add(TelemetrySample(
                provider_id=provider.id, model_id=model.id, session_id=row.id,
                ts=start + offset, state="GENERATING", gen_tps=10.0,
                context_used=100, tokens_total=float(offset)))
            session.add(GpuTelemetrySample(
                provider_id=provider.id, ts=start + offset, gpu_key="gpu:0",
                gpu_index=0, name="GPU0", util=50.0, vram_used_mb=1024.0))
        session.commit()
        return row.id

    def test_unterminated_30h_session_stays_within_the_point_cap(self):
        engine = memory_engine()
        with Session(engine) as session:
            sid = self._seed(session, start_offset_ms=30 * HOUR_MS,
                             end_at=None, status="ACTIVE")
            graphs = metrics.session_detail(session, sid)["graphs"]
            cap = metrics.SESSION_DETAIL_MAX_POINTS
            self.assertLessEqual(len(graphs["labels"]), cap)
            for name, values in graphs["series"].items():
                self.assertLessEqual(len(values), cap, f"series {name} unbounded")
            for gpu in graphs["gpus"]:
                self.assertLessEqual(len(gpu["labels"]), cap, "gpu labels unbounded")
                for name, values in gpu["series"].items():
                    self.assertLessEqual(len(values), cap,
                                         f"gpu series {name} unbounded")

    def test_series_and_labels_stay_aligned(self):
        # A shorter series than labels would silently misplot the chart.
        engine = memory_engine()
        with Session(engine) as session:
            sid = self._seed(session, start_offset_ms=30 * HOUR_MS,
                             end_at=None, status="ACTIVE")
            graphs = metrics.session_detail(session, sid)["graphs"]
            expected = len(graphs["labels"])
            for name, values in graphs["series"].items():
                self.assertEqual(len(values), expected, f"series {name} misaligned")

    def test_short_closed_session_is_bucketed_as_before(self):
        engine = memory_engine()
        with Session(engine) as session:
            start_offset = 60_000
            sid = self._seed(session, start_offset_ms=start_offset,
                             end_at=now_ms(), status="CLOSED")
            graphs = metrics.session_detail(session, sid)["graphs"]
            # 60 s at the unchanged 2 s bucket
            self.assertAlmostEqual(len(graphs["labels"]), 30, delta=2)

    def test_missing_session_still_returns_empty(self):
        engine = memory_engine()
        with Session(engine) as session:
            self.assertEqual(metrics.session_detail(session, 999_999), {})


if __name__ == "__main__":
    unittest.main()


class SessionGpuLoaderTests(unittest.TestCase):
    """The session GPU loader must filter in SQL, not by hydrating every row.

    On production, a session left open for 195 h made the Python-filtered
    loader hydrate 70,394 GPU ORM entities and json-decode each one, only to
    discard every single one -- 32 s of pure waste that the chart point cap
    cannot reach.
    """

    def _seed(self, session):
        provider = Provider(name="prov", base_url="http://prov")
        session.add(provider)
        session.commit()
        session.refresh(provider)
        model = Model(provider_id=provider.id, key="m", name="m")
        session.add(model)
        session.commit()
        session.refresh(model)
        row = SessionRow(provider_id=provider.id, model_id=model.id,
                         start_at=now_ms() - 10_000, end_at=None, status="ACTIVE")
        session.add(row)
        session.commit()
        session.refresh(row)
        return provider.id, row.id

    def test_matches_the_python_filtered_loader(self):
        engine = memory_engine()
        with Session(engine) as session:
            pid, sid = self._seed(session)
            base = now_ms() - 10_000
            # rows in the session, rows belonging to another session, and a row
            # with malformed legacy JSON that must not crash or widen results
            for offset, ids in ((0, f"[{sid}]"), (1000, "[99]"),
                                (2000, f"[99,{sid}]"), (3000, "not json")):
                session.add(GpuTelemetrySample(
                    provider_id=pid, ts=base + offset, gpu_key="gpu:0",
                    gpu_index=0, name="GPU0", util=10.0,
                    active_session_ids=ids))
            session.commit()
            start, end = base - 1, base + 10_000
            expected = metrics._gpu_rows(session, pid, start, end, session_id=sid)
            actual = metrics._gpu_rows_for_session(session, pid, start, end, sid)
            self.assertEqual([r.ts for r in actual], [r.ts for r in expected])
            self.assertEqual(len(actual), 2, "should match only the two member rows")

    def test_unrelated_session_returns_nothing(self):
        engine = memory_engine()
        with Session(engine) as session:
            pid, sid = self._seed(session)
            base = now_ms() - 10_000
            session.add(GpuTelemetrySample(
                provider_id=pid, ts=base, gpu_key="gpu:0", gpu_index=0,
                name="GPU0", util=10.0, active_session_ids="[12345]"))
            session.commit()
            rows = metrics._gpu_rows_for_session(session, pid, base - 1,
                                                 base + 1000, sid)
            self.assertEqual(rows, [])


class InterruptedSessionWindowTests(unittest.TestCase):
    """A missing end_at is normal, not damage.

    Stopping a request from the dashboard leaves an INTERRUPTED session with no
    end recorded. Treating that as "still running" stretched the chart window
    from the session's start to now, so a request stopped days ago charted days
    of nothing after the work finished.
    """

    def _seed(self, session, *, status, live_seen_offset_ms, sample_offset_ms):
        provider = Provider(name="prov", base_url="http://p")
        session.add(provider)
        session.commit()
        session.refresh(provider)
        model = Model(provider_id=provider.id, key="m", name="m")
        session.add(model)
        session.commit()
        session.refresh(model)
        now = now_ms()
        start = now - 8 * 24 * HOUR_MS
        row = SessionRow(provider_id=provider.id, model_id=model.id,
                         start_at=start, end_at=None, status=status,
                         live_seen_at=now - live_seen_offset_ms)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.add(TelemetrySample(
            provider_id=provider.id, model_id=model.id, session_id=row.id,
            ts=now - sample_offset_ms, state="GENERATING", gen_tps=10.0))
        session.commit()
        return row.id

    def test_interrupted_session_ends_when_it_was_last_seen(self):
        engine = memory_engine()
        with Session(engine) as s:
            # stopped a week ago, last seen 10 minutes after it started
            sid = self._seed(s, status="INTERRUPTED",
                             live_seen_offset_ms=8 * 24 * HOUR_MS - 600_000,
                             sample_offset_ms=8 * 24 * HOUR_MS - 600_000)
            graphs = metrics.session_detail(s, sid)["graphs"]
            span_hours = (graphs.get("span_s") or 0) / 3600
            self.assertLess(span_hours, 2,
                            f"window still stretches to now ({span_hours:.1f} h)")

    def test_running_session_still_extends_to_now(self):
        # A genuinely live session has no end yet and must keep doing so.
        engine = memory_engine()
        with Session(engine) as s:
            sid = self._seed(s, status="ACTIVE", live_seen_offset_ms=1_000,
                             sample_offset_ms=1_000)
            graphs = metrics.session_detail(s, sid)["graphs"]
            self.assertGreater((graphs.get("span_s") or 0) / 3600, 100,
                               "a live session should still run to now")

    def test_no_live_seen_at_falls_back_to_the_last_sample(self):
        engine = memory_engine()
        with Session(engine) as s:
            sid = self._seed(s, status="INCOMPLETE",
                             live_seen_offset_ms=8 * 24 * HOUR_MS,
                             sample_offset_ms=8 * 24 * HOUR_MS - 300_000)
            with Session(engine) as s2:
                row = s2.get(SessionRow, sid)
                row.live_seen_at = None
                s2.add(row)
                s2.commit()
            graphs = metrics.session_detail(s, sid)["graphs"]
            self.assertLess((graphs.get("span_s") or 0) / 3600, 2)
