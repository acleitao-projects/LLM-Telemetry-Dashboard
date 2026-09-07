"""Provider status must age out, and a standby collector must be visible.

``Provider.status`` is only ever written by the collector.  When the collector
stops writing -- killed, crashed, or demoted to standby by the single-writer
lease -- the column keeps its last value, so a provider last polled a day ago
still reported LIVE and a non-collecting process looked identical to a
collecting one.
"""
from __future__ import annotations

import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import metrics
from observatory.models import Provider, now_ms
from observatory.settings import STALE_AFTER_S


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def provider(**kw):
    base = dict(name="Local llama.cpp", base_url="http://router", enabled=True,
                status="LIVE", poll_interval_s=1.0)
    base.update(kw)
    return Provider(**base)


class EffectiveProviderStatusTests(unittest.TestCase):
    def test_recent_success_keeps_the_stored_status(self):
        now = now_ms()
        p = provider(last_success_at=now - 2_000)
        self.assertEqual(metrics.effective_provider_status(p, now), "LIVE")

    def test_a_day_old_success_is_not_live(self):
        # The exact production symptom: collector stopped 26 h ago, column
        # still says LIVE, dashboard shows a green pill.
        now = now_ms()
        p = provider(last_success_at=now - 26 * 3600 * 1000)
        self.assertEqual(metrics.effective_provider_status(p, now), "OFFLINE")

    def test_just_past_the_grace_window_reports_stale_before_offline(self):
        now = now_ms()
        p = provider(last_success_at=now - int(STALE_AFTER_S * 1000) - 5_000)
        self.assertEqual(metrics.effective_provider_status(p, now), "STALE")

    def test_slow_poll_interval_is_not_reported_stale_between_polls(self):
        # A provider deliberately polled every 30 s is healthy at 45 s.
        now = now_ms()
        p = provider(poll_interval_s=30.0, last_success_at=now - 45_000)
        self.assertEqual(metrics.effective_provider_status(p, now), "LIVE")

    def test_collector_written_bad_status_is_preserved(self):
        now = now_ms()
        p = provider(status="STALE", last_success_at=now - 1_000)
        self.assertEqual(metrics.effective_provider_status(p, now), "STALE")

    def test_never_polled_provider_keeps_its_stored_status(self):
        now = now_ms()
        p = provider(status="OFFLINE", last_success_at=None)
        self.assertEqual(metrics.effective_provider_status(p, now), "OFFLINE")

    def test_disabled_provider_is_left_alone(self):
        # A disabled provider is never polled; ageing it out says nothing.
        now = now_ms()
        p = provider(enabled=False, status="LIVE", last_success_at=now - 86_400_000)
        self.assertEqual(metrics.effective_provider_status(p, now), "LIVE")


class SnapshotReportsAgedStatusTests(unittest.TestCase):
    def test_realtime_snapshot_does_not_report_a_dead_collector_as_live(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add(provider(last_success_at=now_ms() - 26 * 3600 * 1000))
            session.commit()
            snapshot = metrics.realtime_snapshot(session)
            self.assertEqual(snapshot["providers"][0]["status"], "OFFLINE")

    def test_status_endpoint_agrees_with_the_snapshot(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add(provider(last_success_at=now_ms() - 26 * 3600 * 1000))
            session.commit()
            self.assertEqual(metrics.status(session)["providers"][0]["status"],
                             "OFFLINE")

    def test_agent_status_follows_the_aged_provider_status(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add(provider(agent_url="http://agent",
                                 last_success_at=now_ms() - 26 * 3600 * 1000))
            session.commit()
            row = metrics.status(session)["providers"][0]
            self.assertEqual(row["agent_status"], "OFFLINE")


if __name__ == "__main__":
    unittest.main()
