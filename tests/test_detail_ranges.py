"""Model Detail ranges select the window they name (issue #70).

`range_start_ms` parsed only the "Nd" form, "today" and "all". Every other key
fell through to a seven-day default -- and the Model Detail range control
offers "1m", "5m", "15m", "1h", "session" and "24h", none of which matched. So
six of its eight buttons returned a week of data while `RANGE_BUCKETS` picked a
bucket size for the window the user had actually asked for. Selecting "1h"
spanned 604,800 s at a 60 s bucket: 10,080 chart points for one hour, and a
query over a week of telemetry to build them.
"""
from __future__ import annotations

import unittest

from observatory.metrics import range_start_ms
from observatory.settings import RANGE_BUCKETS, RANGE_KEYS

NOW = 1_788_743_222_202
DAY = 86_400_000


class RangeStartTests(unittest.TestCase):

    def test_sub_day_keys_select_their_own_window(self):
        for key, expected_ms in [
            ("1m", 60_000),
            ("5m", 5 * 60_000),
            ("15m", 15 * 60_000),
            ("1h", 3_600_000),
            ("24h", DAY),
        ]:
            with self.subTest(key=key):
                self.assertEqual(NOW - expected_ms, range_start_ms(key, NOW))

    def test_day_keys_are_unchanged(self):
        for days in (2, 3, 5, 7, 30):
            with self.subTest(days=days):
                self.assertEqual(NOW - days * DAY, range_start_ms("%dd" % days, NOW))

    def test_all_and_unknown_keys_are_unchanged(self):
        self.assertEqual(0, range_start_ms("all", NOW))
        # An unrecognised key still falls back to a week rather than raising.
        self.assertEqual(NOW - 7 * DAY, range_start_ms("nonsense", NOW))
        self.assertEqual(NOW - 7 * DAY, range_start_ms("", NOW))

    def test_session_uses_the_live_window(self):
        self.assertEqual(NOW - 60_000, range_start_ms("session", NOW))

    def test_no_detail_range_silently_falls_back_to_a_week(self):
        """The regression itself: every offered key must be understood."""
        offered = ["1m", "5m", "15m", "1h", "session", "24h", "7d", "30d"]
        week = NOW - 7 * DAY
        unhandled = [k for k in offered
                     if k != "7d" and range_start_ms(k, NOW) == week]
        self.assertEqual([], unhandled,
                         "these keys silently mean 7d: %s" % ", ".join(unhandled))

    def test_every_bucketed_key_is_parseable(self):
        """RANGE_BUCKETS and range_start_ms must agree on the vocabulary."""
        week = NOW - 7 * DAY
        for key in RANGE_BUCKETS:
            if key == "7d":
                continue
            with self.subTest(key=key):
                self.assertNotEqual(week, range_start_ms(key, NOW),
                                    "%s is bucketed but not parsed" % key)

    def test_page_range_keys_still_parse(self):
        for key in RANGE_KEYS:
            with self.subTest(key=key):
                self.assertLessEqual(range_start_ms(key, NOW), NOW)

    def test_a_shorter_window_starts_later_than_a_longer_one(self):
        ordered = ["1m", "5m", "15m", "1h", "24h", "7d", "30d"]
        starts = [range_start_ms(k, NOW) for k in ordered]
        self.assertEqual(starts, sorted(starts, reverse=True),
                         "windows must nest: %s" % list(zip(ordered, starts)))


if __name__ == "__main__":
    unittest.main()
