"""Shared, single-flight, stale-while-revalidate snapshot registry (G02).

The registry caches immutable :class:`observatory.metrics.RangeSummary` snapshots
keyed by ``(provider_id, range_key)``.  All landing endpoints read from the same
registry, so a given range is aggregated at most once per refresh window and the
result is reused by every consumer.

Semantics:

* A *fresh* entry (age <= ``revalidate_s``) is served immediately.
* A *stale* entry is served immediately (stale-while-revalidate) and revalidated
  in the background; at most one build is in flight per key (single-flight).
* A key with no entry yet is built synchronously on the first request, and
  concurrent first-time requests for the same key share a single build.

The registry opens its own short-lived sessions via ``session_factory``, so it is
safe to call from request handlers without passing their session in.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

log = logging.getLogger("observatory.snapshot")


class _SnapshotEntry:
    __slots__ = ("value", "completed_at", "data_version", "completion_marker")

    def __init__(self, value, completed_at: float, data_version: int,
                 completion_marker: int):
        self.value = value
        self.completed_at = completed_at
        self.data_version = data_version
        self.completion_marker = completion_marker


class SnapshotRegistry:
    def __init__(self, builder: Callable, session_factory: Callable, *,
                 revalidate_s: float = 5.0, max_entries: int = 64,
                 data_version_fn: Optional[Callable[[], int]] = None):
        self._builder = builder
        self._session_factory = session_factory
        self._revalidate_s = revalidate_s
        self._max_entries = max_entries
        self._data_version_fn = data_version_fn or (lambda: 0)
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, _SnapshotEntry] = OrderedDict()
        self._completion_marker = 0
        self._key_locks: dict[tuple, threading.RLock] = {}
        self._refreshing: set[tuple] = set()
        # Instrumentation counters (read by /api/meta and tests).
        self.hits = 0
        self.misses = 0
        self.swr_serves = 0
        self.refreshes = 0
        self.builds = 0
        self.waits = 0
        self.failures = 0
        self.last_duration_ms = 0.0

    # -- key / lock helpers ------------------------------------------------
    def _key(self, provider_id, range_key) -> tuple:
        return (provider_id, range_key)

    def _lock_for(self, key: tuple) -> threading.RLock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[key] = lock
            return lock

    def _data_version(self) -> int:
        try:
            return int(self._data_version_fn())
        except Exception:
            return 0

    def _is_fresh(self, entry: _SnapshotEntry, data_version: int) -> bool:
        age = time.monotonic() - entry.completed_at
        return age <= self._revalidate_s and data_version == entry.data_version

    # -- storage -----------------------------------------------------------
    def _store(self, key: tuple, value, data_version: int) -> None:
        with self._lock:
            self._completion_marker += 1
            self._cache[key] = _SnapshotEntry(
                value, time.monotonic(), data_version, self._completion_marker)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                evicted_key, _ = self._cache.popitem(last=False)
                self._key_locks.pop(evicted_key, None)

    def _build(self, key: tuple, data_version: int):
        started = time.perf_counter()
        try:
            with self._session_factory() as session:
                value = self._builder(session, key[0], key[1])
        except Exception:
            self.failures += 1
            raise
        self.builds += 1
        self.last_duration_ms = (time.perf_counter() - started) * 1000.0
        self._store(key, value, data_version)
        return value

    # -- public API --------------------------------------------------------
    def get(self, provider_id, range_key):
        """Return the snapshot for (provider_id, range_key), building if needed.

        Serves a fresh or stale (last-good) entry immediately and refreshes in
        the background when stale.  Only the very first request for a key blocks.
        """
        key = self._key(provider_id, range_key)
        data_version = self._data_version()
        with self._lock:
            entry = self._cache.get(key)
            miss_observed_at = self._completion_marker
            if entry is not None:
                self._cache.move_to_end(key)
        if entry is not None:
            if self._is_fresh(entry, data_version):
                self.hits += 1
                return entry.value
            self.swr_serves += 1
            self._start_background_refresh(key, data_version)
            return entry.value
        self.misses += 1
        return self._build_blocking(key, data_version, miss_observed_at)

    def invalidate(self, key: Optional[tuple] = None) -> None:
        """Drop one entry, or the whole cache when ``key`` is None."""
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(self._key(*key), None)

    def stats(self) -> dict:
        with self._lock:
            size = len(self._cache)
            in_flight = len(self._refreshing)
        return {
            "size": size,
            "in_flight": in_flight,
            "hits": self.hits,
            "misses": self.misses,
            "swr_serves": self.swr_serves,
            "refreshes": self.refreshes,
            "builds": self.builds,
            "failures": self.failures,
            "last_duration_ms": round(self.last_duration_ms, 2),
        }

    # -- internals ---------------------------------------------------------
    def _build_blocking(self, key: tuple, data_version: int,
                        miss_observed_at: int):
        lock = self._lock_for(key)
        with lock:
            self.waits += 1
            # Re-check: another thread may have completed the build while we
            # waited for the per-key lock (single-flight).
            with self._lock:
                entry = self._cache.get(key)
            if (entry is not None and
                    entry.completion_marker > miss_observed_at):
                return entry.value
            data_version = self._data_version()
            return self._build(key, data_version)

    def _start_background_refresh(self, key: tuple, data_version: int) -> None:
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)

        def _run() -> None:
            try:
                self._build(key, self._data_version())
                self.refreshes += 1
            except Exception as exc:
                log.warning("background refresh failed for %s: %s", key, exc)
            finally:
                with self._lock:
                    self._refreshing.discard(key)

        threading.Thread(target=_run, name="snapshot-refresh", daemon=True).start()


def build_snapshot(session, provider_id, range_key):
    """Thin builder used by :class:`SnapshotRegistry`.

    Returns an immutable :class:`observatory.metrics.RangeSummary` for the range.
    """
    from observatory import metrics
    return metrics.range_summary(session, provider_id, range_key)
