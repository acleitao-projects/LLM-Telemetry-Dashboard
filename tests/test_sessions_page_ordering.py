"""Sessions page ordering and row selection.

The page used to order by a CASE over status and freshness, then by start_at.
No index can satisfy an ORDER BY on a parameterised expression, so SQLite
sorted every matching session in a temp B-tree before applying LIMIT 500 --
work proportional to the whole table rather than to the rows returned. The two
groups are now fetched separately. These tests pin the behaviour that must not
change while doing so.
"""
from __future__ import annotations

import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import metrics
from observatory.models import Model, Provider, SessionRow, now_ms


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def seed(session, count=12):
    provider = Provider(name="prov", base_url="http://p")
    session.add(provider)
    session.commit()
    session.refresh(provider)
    model = Model(provider_id=provider.id, key="m", name="m")
    session.add(model)
    session.commit()
    session.refresh(model)
    now = now_ms()
    for i in range(count):
        session.add(SessionRow(provider_id=provider.id, model_id=model.id,
                               start_at=now - (i + 1) * 60_000,
                               end_at=now - (i + 1) * 60_000 + 1000,
                               status="CLOSED"))
    session.commit()
    return provider.id, model.id, now


class SessionsOrderingTests(unittest.TestCase):
    def test_live_sessions_come_first_even_when_older(self):
        # The whole reason the ordering was expression-led: a live session must
        # reach the page even when newer finished ones exist.
        engine = memory_engine()
        with Session(engine) as s:
            pid, mid, now = seed(s, count=20)
            old_live = SessionRow(provider_id=pid, model_id=mid,
                                  start_at=now - 10 * 86_400_000, end_at=None,
                                  status="ACTIVE", live_seen_at=now - 1000)
            s.add(old_live)
            s.commit()
            s.refresh(old_live)
            out = metrics.sessions_page(s, None, None, None, None, None, "30d")
            self.assertEqual(out["sessions"][0]["id"], old_live.id,
                             "a fresh live session must sort first")

    def test_stale_active_session_is_not_treated_as_live(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid, mid, now = seed(s, count=5)
            stale = SessionRow(provider_id=pid, model_id=mid,
                               start_at=now - 10 * 86_400_000, end_at=None,
                               status="ACTIVE", live_seen_at=now - 600_000)
            s.add(stale)
            s.commit()
            s.refresh(stale)
            out = metrics.sessions_page(s, None, None, None, None, None, "30d")
            self.assertNotEqual(out["sessions"][0]["id"], stale.id)

    def test_null_live_seen_at_is_not_lost(self):
        # The live test is negated for the remainder; a NULL live_seen_at must
        # fail it cleanly rather than making the row vanish from both halves.
        engine = memory_engine()
        with Session(engine) as s:
            pid, mid, now = seed(s, count=3)
            row = SessionRow(provider_id=pid, model_id=mid, start_at=now - 5000,
                             end_at=None, status="ACTIVE", live_seen_at=None)
            s.add(row)
            s.commit()
            s.refresh(row)
            out = metrics.sessions_page(s, None, None, None, None, None, "7d")
            self.assertIn(row.id, [r["id"] for r in out["sessions"]])

    def test_ties_on_start_at_order_deterministically(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid, mid, now = seed(s, count=2)
            for _ in range(4):
                s.add(SessionRow(provider_id=pid, model_id=mid,
                                 start_at=now - 30_000, end_at=now - 29_000,
                                 status="CLOSED"))
            s.commit()
            first = [r["id"] for r in
                     metrics.sessions_page(s, None, None, None, None, None, "7d")["sessions"]]
            second = [r["id"] for r in
                      metrics.sessions_page(s, None, None, None, None, None, "7d")["sessions"]]
            self.assertEqual(first, second, "identical starts must not reorder")

    def test_result_is_capped_at_500(self):
        engine = memory_engine()
        with Session(engine) as s:
            seed(s, count=520)
            out = metrics.sessions_page(s, None, None, None, None, None, "7d")
            self.assertEqual(len(out["sessions"]), 500)


if __name__ == "__main__":
    unittest.main()
