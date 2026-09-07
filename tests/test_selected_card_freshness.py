"""Selected-model card freshness and bounded runtime lookups.

Covers two defects observed against a live multi-model router:

* retained "last live" observations were re-served for up to 120 s carrying the
  ``age_s`` computed when they were captured, so a two-minute-old slot snapshot
  reported "observed 0.9s ago" and the card looked live while frozen;
* the ``_selected_realtime`` fallback lookups selected without ``LIMIT 1``, so
  every call hydrated whole tables to keep a single row.
"""
from __future__ import annotations

import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app
from observatory import metrics
from observatory.models import Model, Provider, SessionRow, TelemetrySample


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class AgedObservationTests(unittest.TestCase):
    """``age_s`` must be derived from ``observed_at`` at publish time."""

    def test_age_is_recomputed_against_the_current_clock(self):
        captured = {"observed_at": 1_000_000, "age_s": 0.9, "gen_tokens": 41}
        aged = app._aged_observation(captured, 1_090_000)
        self.assertEqual(aged["age_s"], 90.0)

    def test_captured_entry_is_not_mutated(self):
        # The cache keeps one dict and re-publishes it every second; mutating it
        # would make the recomputation compound instead of being absolute.
        captured = {"observed_at": 1_000_000, "age_s": 0.9}
        app._aged_observation(captured, 1_090_000)
        app._aged_observation(captured, 1_120_000)
        self.assertEqual(captured["age_s"], 0.9)
        self.assertEqual(app._aged_observation(captured, 1_120_000)["age_s"], 120.0)

    def test_other_fields_are_preserved(self):
        captured = {"observed_at": 1_000_000, "age_s": 0.4, "status": "LAST LIVE",
                    "gen_tokens": 41, "session_id": 7}
        aged = app._aged_observation(captured, 1_005_000)
        self.assertEqual(aged["status"], "LAST LIVE")
        self.assertEqual(aged["gen_tokens"], 41)
        self.assertEqual(aged["session_id"], 7)

    def test_entry_without_observed_at_is_left_alone(self):
        captured = {"status": "NO DATA", "age_s": None}
        self.assertIs(app._aged_observation(captured, 1_000_000), captured)

    def test_clock_skew_never_reports_a_negative_age(self):
        captured = {"observed_at": 1_000_000, "age_s": 0.0}
        self.assertEqual(app._aged_observation(captured, 999_000)["age_s"], 0.0)


class BoundedRuntimeLookupTests(unittest.TestCase):
    """``LIMIT 1`` must not change which row the fallbacks pick."""

    def _seed(self, session):
        provider = Provider(name="router", base_url="http://router")
        session.add(provider)
        session.commit()
        session.refresh(provider)
        model = Model(provider_id=provider.id, key="model-a", name="model-a")
        session.add(model)
        session.commit()
        session.refresh(model)
        return provider.id, model.id

    def test_returns_the_newest_slot_backed_session(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        with Session(engine) as session:
            provider_id, model_id = self._seed(session)
            for offset in (500_000, 20_000, 300_000):
                session.add(SessionRow(
                    provider_id=provider_id, model_id=model_id,
                    start_at=now - offset - 1_000, status="CLOSED",
                    live_seen_at=now - offset, live_gen_tokens=offset // 1000,
                ))
            session.commit()
            # the row whose live_seen_at is closest to now
            newest = session.exec(select(SessionRow)
                                  .order_by(SessionRow.live_seen_at.desc())
                                  .limit(1)).first()
            models = {model_id: session.get(Model, model_id)}
            runtime = metrics._selected_realtime(session, models, now)
            self.assertEqual(runtime["session_id"], newest.id)

    def test_falls_back_to_the_newest_sample_when_no_slot_history(self):
        engine = memory_engine()
        now = 1_800_000_000_000
        with Session(engine) as session:
            provider_id, model_id = self._seed(session)
            for offset, ctx in ((600_000, 10), (60_000, 99), (300_000, 50)):
                session.add(TelemetrySample(
                    provider_id=provider_id, model_id=model_id,
                    ts=now - offset, state="IDLE", context_used=ctx,
                ))
            session.commit()
            models = {model_id: session.get(Model, model_id)}
            runtime = metrics._selected_realtime(session, models, now)
            # context comes from the newest sample, not an arbitrary one
            self.assertEqual(runtime["context"], 99)


if __name__ == "__main__":
    unittest.main()
