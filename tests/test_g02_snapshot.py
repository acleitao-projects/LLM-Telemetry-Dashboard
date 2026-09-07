"""G02 snapshot registry: single-flight, SWR, data_version, invalidate, stats."""
from __future__ import annotations

import threading
import time
import unittest

from observatory.snapshot import SnapshotRegistry


class _FakeSession:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass


def _session_factory():
    return _FakeSession()


def _make_builder(results=None, delay=0.0):
    """Return a builder function that records calls."""
    calls = []

    def builder(session, provider_id, range_key):
        calls.append((provider_id, range_key))
        if delay:
            time.sleep(delay)
        if results:
            return results[min(len(calls) - 1, len(results) - 1)]
        return {"provider_id": provider_id, "range_key": range_key, "n": len(calls)}
    return builder, calls


class SnapshotRegistryTests(unittest.TestCase):
    def test_fresh_hit_no_rebuild(self):
        builder, calls = _make_builder()
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0)
        v1 = reg.get(1, "7d")
        v2 = reg.get(1, "7d")
        self.assertIs(v1, v2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reg.hits, 1)
        self.assertEqual(reg.misses, 1)
        self.assertEqual(reg.builds, 1)

    def test_single_flight_concurrent_first_build(self):
        """Concurrent first-time requests for the same key share a single build."""
        calls = []
        lock = threading.Lock()
        ready = threading.Event()

        def builder(session, provider_id, range_key):
            with lock:
                calls.append(1)
            ready.set()
            time.sleep(0.05)
            return {"n": len(calls)}

        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0)
        results = [None] * 4
        errors = []

        def worker(i):
            try:
                results[i] = reg.get(1, "1h")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(reg.builds, 1)
        for r in results:
            self.assertEqual(r["n"], 1)

    def test_cold_waiters_share_completed_build_across_version_change(self):
        """Cold waiters accept their flight, then later callers use SWR."""
        calls = []
        calls_lock = threading.Lock()
        build_started = threading.Event()
        release_build = threading.Event()
        version = [0]

        def builder(session, provider_id, range_key):
            with calls_lock:
                calls.append(1)
                call_number = len(calls)
            if call_number == 1:
                build_started.set()
                self.assertTrue(release_build.wait(timeout=10))
            return {"n": call_number}

        reg = SnapshotRegistry(
            builder, _session_factory, revalidate_s=60.0,
            data_version_fn=lambda: version[0])
        worker_count = 6
        results = [None] * worker_count
        errors = []

        def worker(i):
            try:
                results[i] = reg.get(1, "1h")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(worker_count)]
        for thread in threads:
            thread.start()
        self.assertTrue(build_started.wait(timeout=10))

        deadline = time.monotonic() + 10.0
        while reg.misses < worker_count and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(reg.misses, worker_count)
        version[0] = 1
        release_build.set()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, [{"n": 1}] * worker_count)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reg.builds, 1)

        # A request that starts after the cold flight follows normal freshness
        # rules: serve stale immediately and start one background refresh.
        later = reg.get(1, "1h")
        self.assertEqual(later, {"n": 1})
        self.assertEqual(reg.swr_serves, 1)
        deadline = time.monotonic() + 10.0
        while reg.refreshes == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(reg.refreshes, 1)
        self.assertEqual(len(calls), 2)

    def test_stale_serves_immediately_and_refreshes(self):
        builder, calls = _make_builder(results=[{"v": 1}, {"v": 2}])
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=0.0)
        v1 = reg.get(1, "7d")
        self.assertEqual(v1["v"], 1)
        time.sleep(0.01)
        v2 = reg.get(1, "7d")
        self.assertEqual(v2["v"], 1)
        self.assertEqual(reg.swr_serves, 1)
        deadline = time.monotonic() + 5.0
        while reg.refreshes == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(reg.refreshes, 1)
        v3 = reg.get(1, "7d")
        self.assertEqual(v3["v"], 2)

    def test_data_version_change_triggers_revalidation(self):
        """A data version change makes the entry stale even within revalidate_s."""
        builder, calls = _make_builder(results=[{"v": 1}, {"v": 2}])
        version = [0]
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0,
                               data_version_fn=lambda: version[0])
        v1 = reg.get(1, "7d")
        self.assertEqual(v1["v"], 1)
        # Same version, within window: fresh hit
        v1b = reg.get(1, "7d")
        self.assertEqual(v1b["v"], 1)
        self.assertEqual(reg.hits, 1)
        # Version changes: entry is stale -> SWR + background refresh
        version[0] = 1
        v2 = reg.get(1, "7d")
        self.assertEqual(v2["v"], 1)
        self.assertEqual(reg.swr_serves, 1)
        deadline = time.monotonic() + 5.0
        while reg.refreshes == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(reg.refreshes, 1)
        v3 = reg.get(1, "7d")
        self.assertEqual(v3["v"], 2)

    def test_invalidate_single_key(self):
        builder, calls = _make_builder()
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0)
        reg.get(1, "7d")
        reg.get(2, "7d")
        self.assertEqual(len(calls), 2)
        reg.invalidate((1, "7d"))
        reg.get(1, "7d")
        self.assertEqual(len(calls), 3)
        reg.get(2, "7d")
        self.assertEqual(len(calls), 3)

    def test_invalidate_all(self):
        builder, calls = _make_builder()
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0)
        reg.get(1, "7d")
        reg.get(2, "24h")
        reg.invalidate()
        reg.get(1, "7d")
        reg.get(2, "24h")
        self.assertEqual(len(calls), 4)

    def test_key_churn_bounded_state(self):
        """Evicting cache entries also evicts their locks (bounded registry)."""
        builder, calls = _make_builder()
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0,
                               max_entries=4)
        # Create more keys than max_entries to force evictions
        for i in range(20):
            reg.get(i, "7d")
        # Cache is bounded
        self.assertLessEqual(len(reg._cache), 4)
        # Locks are bounded (evicted keys' locks are removed)
        self.assertLessEqual(len(reg._key_locks), 4)

    def test_stats_fields(self):
        builder, calls = _make_builder(results=[{"v": 1}, {"v": 2}])
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0)
        reg.get(1, "7d")
        v = reg.get(1, "7d")
        stats = reg.stats()
        self.assertEqual(stats["size"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["swr_serves"], 0)
        self.assertEqual(stats["builds"], 1)
        self.assertGreaterEqual(stats["last_duration_ms"], 0.0)
        self.assertEqual(stats["in_flight"], 0)

    def test_max_entries_bounded(self):
        builder, calls = _make_builder()
        reg = SnapshotRegistry(builder, _session_factory, revalidate_s=60.0,
                               max_entries=3)
        for i in range(5):
            reg.get(i, "7d")
        self.assertEqual(reg.stats()["size"], 3)

    def test_failure_increments_counter(self):
        def bad_builder(session, provider_id, range_key):
            raise RuntimeError("boom")

        reg = SnapshotRegistry(bad_builder, _session_factory)
        with self.assertRaises(RuntimeError):
            reg.get(1, "7d")
        self.assertEqual(reg.failures, 1)
        self.assertEqual(reg.builds, 0)

    def test_background_refresh_failure(self):
        calls = [0]

        def flaky_builder(session, provider_id, range_key):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("bg fail")
            return {"v": 1}

        reg = SnapshotRegistry(flaky_builder, _session_factory, revalidate_s=0.0)
        reg.get(1, "7d")
        self.assertEqual(calls[0], 1)
        time.sleep(0.01)
        reg.get(1, "7d")
        deadline = time.monotonic() + 5.0
        while calls[0] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        self.assertEqual(reg.failures, 1)
        self.assertEqual(reg.refreshes, 0)


if __name__ == "__main__":
    unittest.main()
