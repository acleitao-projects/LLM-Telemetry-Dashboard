"""Models page range math and top-card aggregates.

Two defects reported by the maintainer and reproduced against production data:

* A fixed range divided by its nominal length regardless of how much history
  existed. With 8 days of data, 30d and all-time returned identical token
  totals -- the same number to the digit -- while reporting average daily
  figures that differed by more than 3x.
* The top cards exposed no absolute generated/input counts, no costs and no
  slowest-decode figure.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import metrics
from observatory.models import Model, Provider, SessionRow, now_ms

DAY_MS = 86_400_000


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def seed(session, *, days_of_history: int):
    provider = Provider(name="prov", base_url="http://prov")
    session.add(provider)
    session.commit()
    session.refresh(provider)
    model = Model(provider_id=provider.id, key="m", name="m")
    session.add(model)
    session.commit()
    session.refresh(model)
    start = now_ms() - days_of_history * DAY_MS
    session.add(SessionRow(provider_id=provider.id, model_id=model.id,
                           start_at=start, end_at=start + 1000, status="CLOSED"))
    session.commit()
    return provider.id


class RangeDayCountTests(unittest.TestCase):
    def test_range_longer_than_history_uses_the_history(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid = seed(s, days_of_history=8)
            now = now_ms()
            thirty = metrics.range_day_count(s, [pid], "30d", 0, now)
            everything = metrics.range_day_count(s, [pid], "all", 0, now)
            self.assertEqual(thirty, everything,
                             "30d covers the same data as all-time here")
            self.assertLessEqual(thirty, 10)

    def test_range_shorter_than_history_is_unchanged(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid = seed(s, days_of_history=30)
            self.assertEqual(metrics.range_day_count(s, [pid], "7d", 0, now_ms()), 7)

    def test_today_is_always_one_day(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid = seed(s, days_of_history=30)
            self.assertEqual(metrics.range_day_count(s, [pid], "today", 0, now_ms()), 1)

    def test_no_history_falls_back_to_the_requested_length(self):
        # Nothing recorded yet: dividing by 1 would inflate every per-day
        # figure, so keep the nominal length.
        engine = memory_engine()
        with Session(engine) as s:
            provider = Provider(name="p", base_url="http://p")
            s.add(provider)
            s.commit()
            s.refresh(provider)
            self.assertEqual(
                metrics.range_day_count(s, [provider.id], "30d", 0, now_ms()), 30)

    def test_never_returns_zero(self):
        engine = memory_engine()
        with Session(engine) as s:
            pid = seed(s, days_of_history=0)
            for rk in ("today", "7d", "30d", "all"):
                self.assertGreaterEqual(
                    metrics.range_day_count(s, [pid], rk, 0, now_ms()), 1, rk)


class TopCardFieldTests(unittest.TestCase):
    """The card values must exist and agree with each other."""

    def _top(self):
        from observatory.snapshot import build_snapshot
        engine = memory_engine()
        with Session(engine) as s:
            pid = seed(s, days_of_history=3)
            summary = build_snapshot(s, None, "7d")
            return metrics.models_page(s, None, "7d", "model", summary,
                                       include_sparks=False)["top"]

    def test_new_fields_are_present(self):
        top = self._top()
        for field in ("gen_tokens", "prompt_tokens", "input_pct",
                      "generated_cost", "input_cost", "total_cost",
                      "slowest", "slowest_name"):
            self.assertIn(field, top, f"{field} missing from top cards")

    def test_costs_reconcile(self):
        top = self._top()
        self.assertEqual(
            Decimal(top["generated_cost"]) + Decimal(top["input_cost"]),
            Decimal(top["total_cost"]),
            "total cost must be generated + input")

    def test_slowest_has_no_token_threshold(self):
        # The maintainer asked for slowest decode "no matter how many tokens
        # it generated", so nothing may filter candidates by volume.
        import inspect
        source = inspect.getsource(metrics.models_page)
        self.assertIn("slowest", source)
        self.assertNotIn("min_tokens", source)


if __name__ == "__main__":
    unittest.main()
