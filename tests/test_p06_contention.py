"""P06: lease heartbeat throttling and model identity caching."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from observatory.collector import LEASE_HEARTBEAT_S, Collector
from observatory.models import CollectorLease, Model, Provider, now_ms


def memory_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class _CountingEngine:
    """Wraps an engine and counts statements touching collectorlease / model."""

    def __init__(self, engine):
        self.engine = engine
        self.lease_writes = 0
        self.model_selects = 0
        self.model_updates = 0
        event.listen(engine, "before_cursor_execute", self._on_stmt)

    def _on_stmt(self, conn, cursor, stmt, params, *args):
        low = " ".join(stmt.lower().split())
        kind = low[:6]
        if "collectorlease" in low and kind == "insert":
            self.lease_writes += 1
        elif " model " in f" {low} " and "modelresidency" not in low \
                and "modelusagebucket" not in low and "modelconfig" not in low:
            if kind == "select":
                self.model_selects += 1
            elif kind == "update":
                self.model_updates += 1


class _IdleClient:
    base_url = "http://router"

    def health(self):
        return {"status": "ok"}

    def props(self):
        return {}

    def models(self):
        return []

    def metrics(self, model=None):
        return {"llamacpp:prompt_tokens_total": 0,
                "llamacpp:prompt_seconds_total": 0,
                "llamacpp:tokens_predicted_total": 0,
                "llamacpp:tokens_predicted_seconds_total": 0,
                "llamacpp:requests_processing": 0}

    def slots(self, model=None):
        return []


class LeaseHeartbeatTests(unittest.TestCase):
    def _patched(self, engine):
        return patch("observatory.collector.db.new_session",
                     side_effect=lambda: Session(engine))

    def test_active_heartbeat_skips_db_writes_inside_window(self):
        engine = memory_engine()
        counter = _CountingEngine(engine)
        c = Collector(lambda _: None)
        with self._patched(engine):
            self.assertTrue(c._ensure_lease(100.0))
            self.assertEqual(c.role, "active")
            writes_after_acquire = counter.lease_writes
            # Ticks inside the throttle window: no lease writes at all.
            for i in range(1, 6):
                self.assertTrue(c._ensure_lease(100.0 + i * 0.5))
            self.assertEqual(counter.lease_writes, writes_after_acquire)
            self.assertTrue(c._last_heartbeat > 0.0)
        # Exactly one UPSERT total (the acquire).
        self.assertEqual(counter.lease_writes, 1)

    def test_heartbeat_resumes_after_interval(self):
        engine = memory_engine()
        counter = _CountingEngine(engine)
        c = Collector(lambda _: None)
        with self._patched(engine):
            self.assertTrue(c._ensure_lease(100.0))
            writes_after_acquire = counter.lease_writes
            # Just inside the window: skipped.
            self.assertTrue(c._ensure_lease(100.0 + LEASE_HEARTBEAT_S - 0.5))
            self.assertEqual(counter.lease_writes, writes_after_acquire)
            # Past the window: a real heartbeat write happens and refreshes
            # the persisted timestamp.
            self.assertTrue(c._ensure_lease(100.0 + LEASE_HEARTBEAT_S + 0.1))
            self.assertEqual(counter.lease_writes, writes_after_acquire + 1)
            with Session(engine) as s:
                row = s.get(CollectorLease, "collector")
                self.assertEqual(row.heartbeat_at,
                                 int((100.0 + LEASE_HEARTBEAT_S + 0.1) * 1000))

    def test_lease_failure_and_role_change_invalidate_fast_path(self):
        engine = memory_engine()
        counter = _CountingEngine(engine)
        c = Collector(lambda _: None)
        with self._patched(engine):
            self.assertTrue(c._ensure_lease(100.0))
            self.assertTrue(c._last_heartbeat > 0.0)
            # Simulate a contested write: another owner holds the lease with a
            # fresh-but-foreign timestamp, so the guarded UPSERT can neither
            # update our own row nor steal a stale one, and ownership fails.
            with Session(engine) as s:
                row = s.get(CollectorLease, "collector")
                row.owner_id = "someone-else"
                row.heartbeat_at = int(110.0 * 1000)  # fresh, foreign: no steal
                s.add(row)
                s.commit()
            self.assertFalse(c._ensure_lease(110.0))
            self.assertEqual(c.role, "standby")
            self.assertEqual(c._last_heartbeat, 0.0)
            # Fast path must not serve while standby.
            with patch("observatory.collector.db.new_session") as mock_sess:
                mock_sess.side_effect = AssertionError(
                    "standby retry path must not use the fast path")
                self.assertFalse(c._ensure_lease(110.6))
            # Re-acquiring after staleness re-arms the fast path only on
            # verified ownership.
            with Session(engine) as s:
                row = s.get(CollectorLease, "collector")
                row.heartbeat_at = int(80.0 * 1000)  # make it stale
                s.add(row)
                s.commit()
            self.assertTrue(c._ensure_lease(120.0))
            self.assertEqual(c.role, "active")
            self.assertTrue(c._last_heartbeat > 0.0)

    def test_release_lease_resets_heartbeat(self):
        engine = memory_engine()
        c = Collector(lambda _: None)
        with self._patched(engine):
            self.assertTrue(c._ensure_lease(100.0))
            self.assertTrue(c._last_heartbeat > 0.0)
            c._release_lease()
            self.assertEqual(c._last_heartbeat, 0.0)
            self.assertEqual(c.role, "standby")
            # A fresh instance (restart/reinit) has never heartbeated.
            fresh = Collector(lambda _: None)
            self.assertEqual(fresh._last_heartbeat, 0.0)
            self.assertEqual(fresh.role, "standby")

    def test_stale_owner_takeover_still_works(self):
        engine = memory_engine()
        first = Collector(lambda _: None)
        second = Collector(lambda _: None)
        with self._patched(engine):
            self.assertTrue(first._ensure_lease(100.0))
            # First collector keeps heartbeating inside its throttle window;
            # none of these may hand ownership to anyone else.
            for i in range(1, 4):
                self.assertTrue(first._ensure_lease(100.0 + i))
            self.assertFalse(second._ensure_lease(101.0))
            self.assertEqual(second.role, "standby")
            # First stops heartbeating (crash) after its last verified write
            # at t=103.0; the row goes stale LEASE_STALE_MS later and second
            # takes over.
            self.assertFalse(second._ensure_lease(103.5))
            self.assertTrue(second._ensure_lease(113.5))
            self.assertEqual(second.role, "active")
            with Session(engine) as s:
                row = s.get(CollectorLease, "collector")
                self.assertEqual(row.owner_id, second.owner_id)
            # The deposed owner's cached fast-path state is stale-expired; its
            # next attempt must go to the DB, lose, and clear the fast path.
            self.assertFalse(first._ensure_lease(114.0))
            self.assertEqual(first.role, "standby")
            self.assertEqual(first._last_heartbeat, 0.0)


class _ModelsClient(_IdleClient):
    """Client whose /models payload can be mutated between polls."""

    def __init__(self):
        self.payload = [{"id": "m1", "status": {
            "value": "loaded", "args": ["-m", "m1.gguf"]}, "meta": {}}]

    def models(self):
        return self.payload


class ModelIdentityCacheTests(unittest.TestCase):
    def _setup(self, client):
        engine = memory_engine()
        counter = _CountingEngine(engine)
        c = Collector(lambda _: client)
        with Session(engine) as s:
            p = Provider(name="router", base_url="http://router", enabled=True)
            s.add(p)
            s.commit()
            s.refresh(p)
            pid = p.id
        return engine, counter, c, pid

    def _poll_once(self, c, pid, now, catalog_refresh):
        """Invoke _poll with controlled /v1/models freshness.

        catalog_refresh=True simulates the MODELS_POLL_S tick (fresh fetch);
        False simulates the intermediate ticks serving the cached payload.
        """
        from observatory.session_tracker import ProviderState
        st = c.states.get(pid)
        if st is None:
            st = c.states[pid] = ProviderState()
        if catalog_refresh:
            c._model_cache[pid] = c.make_client(self.provider).models()
            c._last_models[pid] = now
        with patch("observatory.collector.db.new_session",
                   side_effect=lambda: Session(self.engine)):
            c._poll(self.provider, st, now)

    def test_cached_identity_skips_model_select_between_catalog_refreshes(self):
        client = _ModelsClient()
        engine, counter, c, pid = self._setup(client)
        self.engine = engine
        with Session(engine) as s:
            self.provider = s.get(Provider, pid)
        # First poll with a fresh catalog fetch resolves identity authoritatively.
        self._poll_once(c, pid, 1000.0, catalog_refresh=True)
        selects_after_first = counter.model_selects
        # Subsequent polls (cached catalog): the hot path must not re-SELECT
        # the Model row for identity.
        for i in range(1, 6):
            self._poll_once(c, pid, 1000.0 + i, catalog_refresh=False)
        self.assertEqual(counter.model_selects, selects_after_first)
        mk_state = c.model_states[(pid, "m1")]
        self.assertEqual(mk_state.model_key, "m1")
        self.assertIsNotNone(mk_state.model_id)

    def test_catalog_refresh_revalidates_identity(self):
        client = _ModelsClient()
        engine, counter, c, pid = self._setup(client)
        self.engine = engine
        with Session(engine) as s:
            self.provider = s.get(Provider, pid)
        self._poll_once(c, pid, 1000.0, catalog_refresh=True)
        old_id = c.model_states[(pid, "m1")].model_id
        # Model is renamed/replaced in the catalog: the next catalog refresh
        # tick must observe the change and re-resolve identity.
        client.payload = [{"id": "m1-renamed", "status": {
            "value": "loaded", "args": ["-m", "m1.gguf"]}, "meta": {}}]
        self._poll_once(c, pid, 1100.0, catalog_refresh=True)
        self.assertNotIn((pid, "m1"), c.model_states)
        st_new = c.model_states[(pid, "m1-renamed")]
        self.assertEqual(st_new.model_key, "m1-renamed")
        self.assertIsNotNone(st_new.model_id)
        with Session(engine) as s:
            row = s.get(Model, st_new.model_id)
            self.assertEqual(row.key, "m1-renamed")

    def test_no_stale_model_id_after_invalidation(self):
        """A cleared (invalidated) identity must never be silently reused.

        After _poll_fail OFFLINE clears st_m.model_id, the next poll must
        re-resolve identity authoritatively (upsert), not trust the stale id.
        """
        engine = memory_engine()
        c = Collector(lambda _: None)
        with Session(engine) as s:
            p = Provider(name="router", base_url="http://router")
            s.add(p)
            s.commit()
            s.refresh(p)
            m = Model(provider_id=p.id, key="m1", name="m1")
            s.add(m)
            s.commit()
            s.refresh(m)
            from observatory.session_tracker import ProviderState
            st_m = ProviderState()
            st_m.model_key = None        # invalidated (simulating _poll_fail)
            st_m.model_id = 999999       # stale id that must not be trusted
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            client = _IdleClient()
            err = c._poll_model(s, p, entry, {}, st_m, client,
                                {"status": "ok"}, {}, now_ms(), time.time(),
                                refreshed=None, identity_fresh=False)
            self.assertIsNone(err)
            # The stale id must not survive: identity re-resolves to m1.
            self.assertEqual(st_m.model_key, "m1")
            self.assertNotEqual(st_m.model_id, 999999)
            self.assertEqual(st_m.model_id, m.id)

    def test_poll_fail_offline_clears_cached_identity(self):
        engine = memory_engine()
        c = Collector(lambda _: None)
        with Session(engine) as s:
            p = Provider(name="router", base_url="http://router",
                         last_success_at=now_ms() - 60_000)
            s.add(p)
            s.commit()
            s.refresh(p)
            from observatory.session_tracker import ProviderState
            st = ProviderState()
            st.model_key = "m1"
            st.model_id = 1234
            c.model_states[(p.id, "m1")] = st
            with patch("observatory.collector.db.new_session",
                       side_effect=lambda: Session(engine)):
                c._poll_fail(p, st, RuntimeError("boom"))
            self.assertIsNone(st.model_key)
            self.assertIsNone(st.model_id)

    def test_unload_clears_cached_identity(self):
        engine = memory_engine()
        c = Collector(lambda _: None)
        with Session(engine) as s:
            p = Provider(name="router", base_url="http://router")
            s.add(p)
            s.commit()
            s.refresh(p)
            m = Model(provider_id=p.id, key="m1", name="m1")
            s.add(m)
            s.commit()
            s.refresh(m)
            from observatory.session_tracker import ProviderState
            st_m = ProviderState()
            st_m.model_key = "m1"
            st_m.model_id = m.id
            st_m.was_loaded = True
            entry = {"key": "m1", "loaded": False, "args": [], "meta": {}}
            c._unload_model(s, p, entry, st_m, now_ms())
            self.assertIsNone(st_m.model_key)
            self.assertIsNone(st_m.model_id)

    def test_last_used_at_written_on_activity_with_cached_identity(self):
        engine = memory_engine()
        c = Collector(lambda _: None)
        with Session(engine) as s:
            p = Provider(name="router", base_url="http://router")
            s.add(p)
            s.commit()
            s.refresh(p)
            m = Model(provider_id=p.id, key="m1", name="m1")
            s.add(m)
            s.commit()
            s.refresh(m)
            from observatory.session_tracker import ProviderState
            st_m = ProviderState()
            st_m.model_key = "m1"
            st_m.model_id = m.id

            class ActiveClient(_IdleClient):
                def __init__(self):
                    self.gen = 0.0

                def metrics(self, model=None):
                    self.gen += 100.0
                    return {"llamacpp:prompt_tokens_total": 0,
                            "llamacpp:prompt_seconds_total": 0,
                            "llamacpp:tokens_predicted_total": self.gen,
                            "llamacpp:tokens_predicted_seconds_total":
                                self.gen / 20.0,
                            "llamacpp:requests_processing": 0}

            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            client = ActiveClient()
            # First poll establishes the counter baseline (no activity yet).
            err = c._poll_model(s, p, entry, {}, st_m, client,
                                {"status": "ok"}, {}, now_ms(), time.time(),
                                refreshed=None, identity_fresh=False)
            self.assertIsNone(err)
            # Second poll sees positive deltas -> real activity.
            err = c._poll_model(s, p, entry, {}, st_m, client,
                                {"status": "ok"}, {}, now_ms() + 1000,
                                time.time() + 1,
                                refreshed=None, identity_fresh=False)
            self.assertIsNone(err)
            s.expire_all()
            row = s.get(Model, m.id)
            self.assertIsNotNone(row.last_used_at)


if __name__ == "__main__":
    unittest.main()