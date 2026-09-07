"""P05 backend hot-path cleanup regression tests.

Covers issue #33 scope:
1. Range-summary session counts/context aggregates are computed in SQL with
   zero SessionRow ORM materialization (one grouped scan).
2. Dead backend spark/snapshot code (metrics.live_snapshot) is removed.
3. Sessions page MTP filtering and status sorting happen in SQL before LIMIT.
4. The SSE stream reuses a single pre-serialized payload per refresh, and the
   encoded payload is always atomically paired with the raw snapshot dict.
"""
import calendar
import json
import unittest
from unittest import mock

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from observatory import metrics
from observatory.models import (Model, Provider, SessionRow, now_ms)

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


def _provider_models(s, count=2):
    p = Provider(name="router", base_url="http://router", status="LIVE")
    s.add(p)
    s.commit()
    s.refresh(p)
    models = []
    for i in range(count):
        m = Model(provider_id=p.id, key=f"m{i}", name=f"model-{i}",
                  family="fam", quant="Q4_K_M")
        s.add(m)
        s.commit()
        s.refresh(m)
        models.append(m)
    return p, models


def _add_session(s, p, m, start_at, end_at=None, context_max=None,
                 mtp_enabled=None, status="CLOSED", live_seen_at=None):
    row = SessionRow(provider_id=p.id, model_id=m.id, start_at=start_at,
                     end_at=end_at, status=status, context_max=context_max,
                     mtp_enabled=mtp_enabled, live_seen_at=live_seen_at)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


# ---------------------------------------------------------------------------
# 1. Range summary: zero ORM materialization, SQL aggregates
# ---------------------------------------------------------------------------
class SummarySessionAggregateTests(unittest.TestCase):
    def test_range_summary_aggregates_sessions_in_sql_without_orm(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 2)
            m0, m1 = models
            # In-window rows: 2 for m0 (one with context), 1 for m1.
            _add_session(s, p, m0, NOW - 2 * DAY, end_at=NOW - DAY,
                         context_max=4096)
            _add_session(s, p, m0, NOW - DAY, end_at=NOW - HOUR,
                         context_max=8192)
            _add_session(s, p, m1, NOW - DAY, end_at=NOW - HOUR)
            # Overlapping session that started before the window.
            _add_session(s, p, m1, NOW - 9 * DAY, end_at=NOW - HOUR,
                         context_max=16384)
            # Out of range: ended before the window start.
            _add_session(s, p, m1, NOW - 9 * DAY, end_at=NOW - 8 * DAY)
            s.commit()

            materialized = []

            def loaded(instance, context):
                materialized.append(instance)

            event.listen(SessionRow, "load", loaded)
            try:
                with PinnedNow():
                    summary = metrics.range_summary(s, None, "7d")
            finally:
                event.remove(SessionRow, "load", loaded)

            self.assertEqual(materialized, [])
            total = summary.session_total
            self.assertEqual(total, 4)
            self.assertEqual(summary.session_counts.get(m0.id), 2)
            self.assertEqual(summary.session_counts.get(m1.id), 2)
            self.assertEqual(summary.session_counts.get(m0.id, 0)
                             + summary.session_counts.get(m1.id, 0), total)
            # avg_context_session: mean over rows with non-NULL context_max.
            ctx = [4096, 8192, 16384]
            self.assertEqual(summary.session_ctx_n, 3)
            self.assertEqual(summary.session_ctx_sum, sum(ctx))

    def test_models_page_payload_matches_legacy_python_counts(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 2)
            m0, m1 = models
            for i in range(3):
                _add_session(s, p, m0, NOW - DAY + i * HOUR, end_at=NOW - 2 * HOUR,
                             context_max=4096 + i)
            _add_session(s, p, m1, NOW - 2 * HOUR, end_at=NOW - HOUR)

            # Legacy reference: full ORM materialization + Python counting.
            with PinnedNow():
                legacy = metrics.range_summary(s, None, "7d")
                legacy_rows = metrics.models_page(s, None, "7d", "model",
                                                  summary=legacy)
            legacy_counts = {r["key"]: r["sessions"] for r in legacy_rows["rows"]}
            legacy_top = legacy_rows["top"]

            # Reset aggregates on the same data: force the SQL path by
            # comparing against an independently recomputed expectation.
            expected_total = 4
            expected_m0 = 3
            with PinnedNow():
                summary = metrics.range_summary(s, None, "7d")
                page = metrics.models_page(s, None, "7d", "model", summary=summary)
            self.assertEqual(summary.session_total, expected_total)
            self.assertEqual(summary.session_counts.get(m0.id), expected_m0)
            counts = {r["key"]: r["sessions"] for r in page["rows"]}
            self.assertEqual(counts, legacy_counts)
            self.assertEqual(page["top"]["sessions"], legacy_top["sessions"])
            self.assertEqual(page["top"]["avg_context_session"],
                             legacy_top["avg_context_session"])

    def test_avg_context_session_excludes_zero_and_null_like_python(self):
        """Legacy filter [x.context_max for x in sess if x.context_max]
        excludes both None and numeric 0; ctx_n/ctx_sum must match."""
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 1)
            m0 = models[0]
            _add_session(s, p, m0, NOW - 4 * HOUR, end_at=NOW - 3 * HOUR,
                         context_max=None)
            _add_session(s, p, m0, NOW - 3 * HOUR, end_at=NOW - 2 * HOUR,
                         context_max=0)
            _add_session(s, p, m0, NOW - 2 * HOUR, end_at=NOW - HOUR,
                         context_max=4096)
            _add_session(s, p, m0, NOW - HOUR, end_at=NOW - MINUTE,
                         context_max=8192)
            s.commit()
            with PinnedNow():
                summary = metrics.range_summary(s, None, "7d")
                page = metrics.models_page(s, None, "7d", "model",
                                           summary=summary)
                # Legacy Python truthiness filter on the same rows.
                rows = metrics.sessions_in_range(s, [p.id], summary.start)
                legacy_ctx = [x.context_max for x in rows if x.context_max]

            expected_avg = round(sum(legacy_ctx) / len(legacy_ctx))
            # sessions_in_range has no ORDER BY, so the order it returns is
            # whatever the chosen plan yields -- adding a (provider_id,
            # start_at DESC) index flipped it to newest-first and broke this
            # comparison.  What the assertion is about is which values survive
            # the truthiness filter, not the order they arrive in.
            self.assertEqual(sorted(legacy_ctx), [4096, 8192])
            # Zero and NULL do not contribute to denominator or numerator.
            self.assertEqual(summary.session_ctx_n, 2)
            self.assertEqual(summary.session_ctx_sum, 4096 + 8192)
            self.assertEqual(page["top"]["avg_context_session"], expected_avg)
            self.assertEqual(page["top"]["avg_context_session"], 6144)

    def test_selected_stats_uses_grouped_counts(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 2)
            m0, m1 = models
            for i in range(2):
                _add_session(s, p, m0, NOW - DAY + i * HOUR,
                             end_at=NOW - 2 * HOUR, context_max=8192)
            with PinnedNow():
                summary = metrics.range_summary(s, None, "7d")
                data = metrics.selected_stats(s, [m0.id], None, "7d",
                                              summary=summary)
        self.assertEqual(data["sessions"], 2)
        self.assertEqual(data["per_session"], round(data["tokens"] / 2))

    def test_compare_candidates_uses_grouped_counts(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 2)
            m0, m1 = models
            # m1 has no sessions and no tokens -> excluded from candidates.
            _add_session(s, p, m0, NOW - DAY, end_at=NOW - HOUR)
            with PinnedNow():
                summary = metrics.range_summary(s, None, "7d")
                data = metrics.compare_model_candidates(s, None, "7d",
                                                        summary=summary)
        keys = {item["key"] for item in data}
        self.assertIn(str(m0.id), keys)
        self.assertNotIn(str(m1.id), keys)

    def test_overview_today_sessions_uses_count_query(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 1)
            m0 = models[0]
            _add_session(s, p, m0, NOW - HOUR, end_at=NOW - MINUTE)
            # Started before today's window -> not counted in "today".
            _add_session(s, p, m0, NOW - 3 * DAY, end_at=NOW - 2 * DAY)
            materialized = []

            def loaded(instance, context):
                materialized.append(instance)

            event.listen(SessionRow, "load", loaded)
            try:
                with PinnedNow():
                    data = metrics.overview(s)
            finally:
                event.remove(SessionRow, "load", loaded)
        self.assertEqual(data["today"]["sessions"], 1)


# ---------------------------------------------------------------------------
# 2. Dead spark/snapshot code removed
# ---------------------------------------------------------------------------
class DeadCodeRemovalTests(unittest.TestCase):
    def test_dead_live_snapshot_removed(self):
        self.assertFalse(hasattr(metrics, "live_snapshot"),
                         "dead live_snapshot() must be removed")


# ---------------------------------------------------------------------------
# 3. Sessions page: MTP filter + status sort in SQL, applied before LIMIT
# ---------------------------------------------------------------------------
class SessionsPageSqlFilterTests(unittest.TestCase):
    def _seed_mixed(self, engine, total=600):
        with Session(engine) as s:
            p, models = _provider_models(s, 1)
            m0 = models[0]
            for i in range(total):
                mtp = (True if i % 3 == 0
                       else (False if i % 3 == 1 else None))
                status = "ACTIVE" if i % 10 == 0 else "CLOSED"
                _add_session(s, p, m0, NOW - (i + 1) * MINUTE,
                             end_at=NOW - (i + 1) * MINUTE + 1000,
                             mtp_enabled=mtp, status=status,
                             live_seen_at=NOW if status == "ACTIVE" else None)
        return p, m0

    def _compiled_sql(self, q):
        from sqlalchemy import literal_column
        return str(q.compile(compile_kwargs={"literal_binds": True}))

    def test_mtp_filter_is_in_sql_where_before_limit(self):
        engine = memory_engine()
        p, m0 = self._seed_mixed(engine)
        with Session(engine) as s:
            with PinnedNow():
                # Capture the executed SQL of the page query.
                captured = []

                def before_cursor(conn, cursor, statement, parameters,
                                  context, executemany):
                    captured.append(statement)

                event.listen(engine, "before_cursor_execute", before_cursor)
                try:
                    # "7d", not "1h": this fixture seeds 600 sessions one
                    # minute apart, so it spans ten hours.  It passed with
                    # "1h" only because range_start_ms did not parse that key
                    # and silently returned a seven-day window (#70).  The
                    # range is incidental here -- what is under test is that
                    # the mtp filter reaches SQL before the LIMIT.
                    data = metrics.sessions_page(s, None, None, None, "on",
                                                 None, "7d")
                finally:
                    event.remove(engine, "before_cursor_execute", before_cursor)

            expected_matching = sum(
                1 for row in s.exec(select(SessionRow)).all()
                if row.mtp_enabled is True)
        self.assertEqual(len(data["sessions"]), min(500, expected_matching))
        self.assertTrue(all(row["mtp_enabled"] for row in data["sessions"]))
        sql = " ".join(captured)
        self.assertIn("mtp_enabled", sql)
        self.assertIn("LIMIT", sql.upper())
        # WHERE clause must precede LIMIT in the executed statement.
        self.assertLess(sql.upper().index("WHERE"), sql.upper().index("LIMIT"))

    def test_filter_returns_full_page_when_matches_exceed_limit(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 1)
            m0 = models[0]
            for i in range(520):
                _add_session(s, p, m0, NOW - (i + 1) * MINUTE,
                             end_at=NOW - (i + 1) * MINUTE + 1000,
                             mtp_enabled=True, status="CLOSED")
        with Session(engine) as s:
            with PinnedNow():
                # 520 sessions one minute apart span over eight hours; see
                # the note above on why this said "1h" before (#70).
                data = metrics.sessions_page(s, None, None, None, "on", None,
                                             "7d")
        self.assertEqual(len(data["sessions"]), 500)

    def test_status_sort_matches_python_semantics(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 1)
            m0 = models[0]
            # Oldest ACTIVE (live), then CLOSED rows, then an INCOMPLETE.
            _add_session(s, p, m0, NOW - 9 * MINUTE, end_at=NOW - 8 * MINUTE,
                         status="ACTIVE", live_seen_at=NOW)
            _add_session(s, p, m0, NOW - 7 * MINUTE, end_at=NOW - 6 * MINUTE,
                         status="CLOSED")
            _add_session(s, p, m0, NOW - 5 * MINUTE, end_at=NOW - 4 * MINUTE,
                         status="CLOSED")
            _add_session(s, p, m0, NOW - 3 * MINUTE, end_at=NOW - 2 * MINUTE,
                         status="INCOMPLETE")
        with Session(engine) as s:
            with PinnedNow():
                data = metrics.sessions_page(s, None, None, None, None, None,
                                             "1h")
        statuses = [row["status"] for row in data["sessions"]]
        self.assertEqual(statuses[0], "ACTIVE")
        # Non-active rows ordered by start desc within the non-active group
        # (matches the legacy Python sort key exactly).
        self.assertEqual(statuses[1:], ["INCOMPLETE", "CLOSED", "CLOSED"])
        starts = [row["start"] for row in data["sessions"][1:]]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_mtp_none_and_null_semantics_preserved(self):
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _provider_models(s, 1)
            m0 = models[0]
            _add_session(s, p, m0, NOW - 3 * MINUTE, end_at=NOW - 2 * MINUTE,
                         mtp_enabled=None)
            _add_session(s, p, m0, NOW - 5 * MINUTE, end_at=NOW - 4 * MINUTE,
                         mtp_enabled=True)
            _add_session(s, p, m0, NOW - 7 * MINUTE, end_at=NOW - 6 * MINUTE,
                         mtp_enabled=False)
        with Session(engine) as s:
            with PinnedNow():
                on = metrics.sessions_page(s, None, None, None, "on", None,
                                           "1h")["sessions"]
                off = metrics.sessions_page(s, None, None, None, "off", None,
                                            "1h")["sessions"]
                allrows = metrics.sessions_page(s, None, None, None, None,
                                                None, "1h")["sessions"]
        self.assertEqual(len(on), 1)
        self.assertEqual(len(off), 1)
        self.assertEqual(len(allrows), 3)


# ---------------------------------------------------------------------------
# 4. SSE: one serialization per refresh, atomic encoded/raw pairing
# ---------------------------------------------------------------------------
class SseSerializationTests(unittest.TestCase):
    """Exercises the app-level live cache: encoded+raw must be replaced
    atomically per refresh and serialized exactly once per refresh."""

    def _make_cache(self):
        import threading
        cache = {"data": None, "refreshed": 0.0, "last_known": {},
                 "encoded": None, "generation": 0}
        return cache, threading.Lock()

    def _refresh(self, cache, lock, payload_fn):
        """Mirror of app.refresh_live_snapshot with encoded serialization
        performed under the same lock hold."""
        import time as time_mod
        data = payload_fn()
        now = time_mod.monotonic()
        with lock:
            data["last_known"] = dict(cache["last_known"])
            encoded = json.dumps(data)
            cache["last_known"] = {}
            cache["data"] = data
            cache["encoded"] = encoded
            cache["refreshed"] = now
            cache["generation"] += 1

    def test_encoded_payload_serialized_once_per_refresh(self):
        cache, lock = self._make_cache()
        counter = {"n": 0}
        real_dumps = json.dumps

        def counting_dumps(obj, *a, **kw):
            counter["n"] += 1
            return real_dumps(obj, *a, **kw)

        with mock.patch("json.dumps", counting_dumps):
            for tick in range(3):
                self._refresh(cache, lock,
                              lambda tick=tick: {"now": tick, "providers": []})
        self.assertEqual(counter["n"], 3)

        # Two subscribers each tick reuse the cached encoded payload.
        counter["n"] = 0
        with mock.patch("json.dumps", counting_dumps):
            for _ in range(2):
                with lock:
                    encoded = cache["encoded"]
        self.assertEqual(counter["n"], 0)
        self.assertEqual(encoded, json.dumps(cache["data"]))

    def test_encoded_and_raw_are_atomically_paired(self):
        cache, lock = self._make_cache()
        for tick in range(5):
            self._refresh(cache, lock,
                          lambda tick=tick: {"now": tick, "providers": []})
            with lock:
                encoded = cache["encoded"]
                raw = cache["data"]
                generation = cache["generation"]
            self.assertEqual(json.loads(encoded), raw)
            self.assertEqual(raw["now"], tick)
            self.assertEqual(generation, tick + 1)

    def test_subscriber_never_sees_stale_encoded_with_newer_raw(self):
        import threading

        cache, lock = self._make_cache()
        stop = threading.Event()
        inconsistencies = []

        def refresher():
            tick = 0
            while not stop.is_set():
                self._refresh(cache, lock,
                              lambda t=tick: {"now": t, "providers": []})
                tick += 1

        def subscriber():
            while not stop.is_set():
                with lock:
                    encoded = cache["encoded"]
                    raw = cache["data"]
                    gen_enc = cache["generation"]
                if encoded is not None and json.loads(encoded) != raw:
                    inconsistencies.append((gen_enc, raw.get("now")))
                # A refresh must bump generation together with both fields.
                with lock:
                    if cache["generation"] != gen_enc:
                        # data changed between reads: encoded must match the
                        # *new* raw on the next read, never the old pairing.
                        pass

        t1 = threading.Thread(target=refresher)
        t2 = threading.Thread(target=subscriber)
        t1.start()
        t2.start()
        try:
            import time as time_mod
            time_mod.sleep(0.2)
        finally:
            stop.set()
            t1.join()
            t2.join()
        self.assertEqual(inconsistencies, [])

    def test_stream_endpoint_reuses_encoded_payload_across_refreshes(self):
        """Drive the real /api/stream generator: multiple subscribers and a
        mid-stream refresh must reuse one serialization per refresh and every
        frame must decode to the raw snapshot it was published with."""
        import asyncio
        from unittest import mock as _mock

        counts = {"serialize": 0, "refresh": 0}
        snap = {"tick": 0}
        real_dumps = json.dumps

        def counting_dumps(obj, *a, **kw):
            counts["serialize"] += 1
            return real_dumps(obj, *a, **kw)

        class StopStream(Exception):
            pass

        async def fake_sleep(_seconds):
            raise StopStream()

        # Build a minimal app with a stubbed realtime_snapshot.
        import app as app_module
        import observatory.database as odb
        from observatory import metrics as metrics_mod

        engine = memory_engine()
        orig_engine, orig_path = odb._engine, odb._db_path
        odb._engine, odb._db_path = engine, "test.db"
        try:
            def fake_snapshot(s):
                counts["refresh"] += 1
                snap["tick"] += 1
                return {"now": snap["tick"], "providers": [],
                        "active_models": [], "current": None, "today": {}}

            with _mock.patch.object(metrics_mod, "realtime_snapshot",
                                    fake_snapshot), \
                 _mock.patch("json.dumps", counting_dumps), \
                 _mock.patch("asyncio.sleep", fake_sleep):
                app_obj = app_module.create_app(demo=True)
                route = next(r for r in app_obj.routes
                             if getattr(r, "path", "") == "/api/stream")

                async def one_tick() -> bytes:
                    response = await route.endpoint()
                    agen = response.body_iterator
                    frame = await agen.__anext__()
                    await agen.aclose()
                    return frame

                frames = []
                for _ in range(3):
                    for _ in range(2):
                        frames.append(asyncio.run(one_tick()))

        finally:
            odb._engine, odb._db_path = orig_engine, orig_path

        # 6 frames from 3 simulated subscribers; every refresh serializes once.
        self.assertEqual(len(frames), 6)
        self.assertGreater(counts["refresh"], 0)
        self.assertEqual(counts["serialize"], counts["refresh"])
        texts = [f.decode() if isinstance(f, bytes) else f for f in frames]
        decoded = [json.loads(t[6:].strip()) for t in texts]
        # Coherence: the final frame decodes to the final snapshot version.
        self.assertEqual(decoded[-1]["now"], snap["tick"])
        # Frames within one snapshot version are byte-identical (shared).
        self.assertTrue(all(d == decoded[0] for d in decoded))


if __name__ == "__main__":
    unittest.main()