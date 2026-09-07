"""The snapshot window must outlast the dashboard's poll interval (#47).

The registry is stale-while-revalidate, so a stale entry never blocks a
request -- but it does kick off a background rebuild. With revalidate_s at 5.0
and the Models page polling every 7 s, every single poll arrived just after the
entry went stale, so every poll rebuilt a snapshot that nothing had
invalidated. Production ran 193 cache hits against 1,723 stale serves and 891
rebuilds at ~655 ms each, which is the load #47 describes as starving the
collector.

The correctness bound is the data version, not the clock: a data_generation
bump makes an entry stale immediately whatever its age. The age bound only has
to sit above the poll interval.
"""
from __future__ import annotations

import os
import re
import unittest

from observatory.settings import SNAPSHOT_REVALIDATE_S
from observatory.snapshot import SnapshotRegistry

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _session_factory():
    return _FakeSession()


def _counting_builder():
    calls = []

    def builder(session, provider_id, range_key):
        calls.append((provider_id, range_key))
        return {"n": len(calls)}
    return builder, calls


class PollCadenceTests(unittest.TestCase):

    def _frontend_poll_intervals_s(self):
        with open(os.path.join(BASE_DIR, "static", "js", "app.js"),
                  encoding="utf-8") as source:
            js = source.read()
        found = re.findall(r"const \w*REFRESH_MS\s*=\s*(\d+)", js)
        self.assertTrue(found, "no refresh interval found in app.js")
        return [int(v) / 1000.0 for v in found]

    def test_window_outlasts_every_frontend_poll_interval(self):
        for interval in self._frontend_poll_intervals_s():
            with self.subTest(interval_s=interval):
                self.assertGreater(
                    SNAPSHOT_REVALIDATE_S, interval,
                    "a poll every %.1fs against a %.1fs window rebuilds the "
                    "snapshot on every request" % (interval, SNAPSHOT_REVALIDATE_S))

    def test_window_stays_under_the_collector_data_bump(self):
        """The collector bumps data_generation at most once a minute."""
        self.assertLess(SNAPSHOT_REVALIDATE_S, 60.0)

    def test_polling_at_the_page_cadence_does_not_rebuild(self):
        """The regression itself, at the registry level."""
        builder, calls = _counting_builder()
        interval = max(self._frontend_poll_intervals_s())
        reg = SnapshotRegistry(builder, _session_factory,
                               revalidate_s=SNAPSHOT_REVALIDATE_S)
        reg.get(1, "7d")
        # Simulate polls at the page's cadence without sleeping through them.
        entry = reg._cache[(1, "7d")]
        for poll in range(1, 4):
            entry.completed_at -= interval
            reg.get(1, "7d")
        self.assertEqual(len(calls), 1,
                         "polling rebuilt the snapshot %d times" % (len(calls) - 1))
        self.assertEqual(reg.refreshes, 0)
        self.assertEqual(reg.hits, 3)

    def test_the_old_window_shows_the_bug(self):
        """Guards the explanation: 5.0 s really did go stale before the poll.

        Asserted against the freshness rule rather than by driving get(), whose
        background refresh replaces the entry object mid-loop.
        """
        builder, _ = _counting_builder()
        interval = max(self._frontend_poll_intervals_s())
        old_reg = SnapshotRegistry(builder, _session_factory, revalidate_s=5.0)
        old_reg.get(1, "7d")
        entry = old_reg._cache[(1, "7d")]
        entry.completed_at -= interval
        self.assertFalse(old_reg._is_fresh(entry, 0),
                         "a %.1fs-old entry was fresh in a 5.0s window" % interval)

        new_reg = SnapshotRegistry(builder, _session_factory,
                                   revalidate_s=SNAPSHOT_REVALIDATE_S)
        new_reg.get(1, "7d")
        entry = new_reg._cache[(1, "7d")]
        entry.completed_at -= interval
        self.assertTrue(new_reg._is_fresh(entry, 0),
                        "the same entry must survive one poll interval now")

    def test_a_data_bump_still_invalidates_immediately(self):
        """Age is not the correctness bound; the data version is."""
        builder, calls = _counting_builder()
        version = {"v": 1}
        reg = SnapshotRegistry(builder, _session_factory,
                               revalidate_s=SNAPSHOT_REVALIDATE_S,
                               data_version_fn=lambda: version["v"])
        reg.get(1, "7d")
        reg.get(1, "7d")
        self.assertEqual(reg.hits, 1)
        version["v"] = 2
        reg.get(1, "7d")
        self.assertEqual(reg.swr_serves, 1,
                         "a data change must not be served as fresh")


if __name__ == "__main__":
    unittest.main()
