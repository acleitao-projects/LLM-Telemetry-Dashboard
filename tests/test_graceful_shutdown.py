"""The SSE stream must not hold the process open through shutdown.

An SSE response never completes on its own, and uvicorn's graceful shutdown
waits for in-flight requests to finish.  ``/api/stream`` used to loop forever,
so a single left-open dashboard tab kept the process alive until systemd's
``TimeoutStopSec`` elapsed and SIGKILLed it.  That skipped the collector's lease
release, which in turn made the replacement process wait out the stale-lease
window before it could poll -- a silent gap in provider polling after every
restart.
"""
from __future__ import annotations

import pathlib
import re
import threading
import time
import unittest

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app
from observatory import database as odb
from observatory.models import Model, Provider


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class _StubServer:
    """Stands in for ``uvicorn.Server``; only ``should_exit`` is consulted."""

    def __init__(self, should_exit: bool = False):
        self.should_exit = should_exit


class GracefulShutdownStreamTests(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as session:
            provider = Provider(name="prov", base_url="http://prov")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            session.add(Model(provider_id=provider.id, key="m", name="m"))
            session.commit()
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.app = app.create_app(demo=True)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_server_slot_defaults_to_none(self):
        # Tests and TestClient never run a real server; the stream must cope.
        self.assertIsNone(self.app.state.server)

    def test_stream_ends_at_once_when_shutdown_already_started(self):
        self.app.state.server = _StubServer(should_exit=True)
        started = time.monotonic()
        response = self.client.get("/api/stream")
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "")
        self.assertLess(elapsed, 5.0, "stream did not end on shutdown")

    def test_stream_ends_promptly_when_shutdown_starts_mid_stream(self):
        server = _StubServer(should_exit=False)
        self.app.state.server = server
        # Without the fix this request never returns and the test times out.
        threading.Timer(0.5, lambda: setattr(server, "should_exit", True)).start()
        started = time.monotonic()
        response = self.client.get("/api/stream")
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 200)
        self.assertIn("data:", response.text)
        self.assertLess(elapsed, 10.0,
                        "stream kept running after shutdown began")

    def test_stream_yields_before_shutdown(self):
        # Guard against "fixing" the hang by never streaming anything.
        server = _StubServer(should_exit=False)
        self.app.state.server = server
        # Generous margin over the 2 s cadence: on a loaded machine a tight
        # window could clip the second frame and fail for the wrong reason.
        threading.Timer(5.0, lambda: setattr(server, "should_exit", True)).start()
        response = self.client.get("/api/stream")
        self.assertGreaterEqual(response.text.count("data:"), 2,
                                "stream should keep emitting until shutdown")


if __name__ == "__main__":
    unittest.main()
