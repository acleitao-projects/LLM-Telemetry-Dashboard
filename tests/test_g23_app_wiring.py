"""G09 issue #23, step 4: the app.py wiring for automatic pricing sync.

Verifies the dedicated worker thread starts and stops with the app, the
single-flight lock exists, and the interrupted-run startup sweep works.
No network: pricing_sync.fetch_catalog is patched.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app as app_module
from observatory import database as odb
from observatory import pricing_sync as ps
from observatory.models import PricingSyncRun, now_ms


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class AppWiringTests(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        self._orig_engine, self._orig_path = odb._engine, odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"

    def tearDown(self):
        odb._engine, odb._db_path = self._orig_engine, self._orig_path

    def test_worker_thread_starts_and_stops_and_makes_no_call_when_disabled(self):
        calls = []

        def fake_fetch(*a, **k):
            calls.append(1)
            raise ps.PricingSyncError("should not be called")

        with patch.object(ps, "fetch_catalog", fake_fetch):
            client = TestClient(app_module.create_app(demo=True))
            with client:
                names = {t.name for t in threading.enumerate()}
                self.assertIn("pricing-sync", names)
                self.assertIsInstance(
                    client.app.state.pricing_run_lock, type(threading.Lock()))
                time.sleep(0.2)  # let one loop tick run
            # after context exit the app shut down; the thread is joined
            time.sleep(0.1)
            self.assertNotIn(
                "pricing-sync", {t.name for t in threading.enumerate()})
        # disabled by default -> the loop never reached the catalog
        self.assertEqual(calls, [])

    def test_startup_sweeps_a_dangling_run_to_interrupted(self):
        with Session(self.engine) as s:
            s.add(PricingSyncRun(trigger="scheduled", started_at=now_ms(),
                                 result="running", finished_at=None))
            s.commit()
        with patch.object(ps, "fetch_catalog",
                          lambda *a, **k: (_ for _ in ()).throw(
                              ps.PricingSyncError("x"))):
            client = TestClient(app_module.create_app(demo=True))
            with client:
                pass
        with Session(self.engine) as s:
            row = s.get(PricingSyncRun, 1)
            self.assertEqual(row.result, "interrupted")
            self.assertIsNotNone(row.finished_at)


if __name__ == "__main__":
    unittest.main()
