from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app
from observatory.collector import Collector
from observatory.models import Provider


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ProviderSettingsTests(unittest.TestCase):
    def test_promoting_provider_sets_exactly_one_default(self):
        engine = memory_engine()
        with Session(engine) as session:
            first = Provider(name="first", base_url="http://first", is_default=True)
            second = Provider(name="second", base_url="http://second")
            session.add(first)
            session.add(second)
            session.commit()
            session.refresh(second)

            app._apply_provider(session, second, {"is_default": True})
            session.commit()

            providers = session.exec(select(Provider).order_by(Provider.id)).all()
            self.assertEqual(
                [(provider.name, provider.is_default) for provider in providers],
                [("first", False), ("second", True)],
            )

    def test_connection_test_uses_loaded_router_model_for_metrics(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/health"):
                return Response({"status": "ok"})
            if url.endswith("/v1/models"):
                return Response({"data": [
                    {"id": "cold", "status": {"value": "unloaded"}},
                    {"id": "hot model", "status": {"value": "loaded"}},
                ]})
            if url.endswith("/metrics"):
                self.assertEqual(kwargs.get("params"), {"model": "hot model"})
                return Response({})
            if url.endswith("/props"):
                return Response({})
            raise AssertionError(f"unexpected URL: {url}")

        provider = Provider(name="router", base_url="http://router")
        with patch("httpx.get", side_effect=fake_get):
            result = app._test_provider(provider)

        self.assertTrue(result["ok"])
        self.assertTrue(result["endpoints"]["metrics"])
        self.assertEqual(result["model"], "hot model")
        self.assertEqual(sum(url.endswith("/v1/models") for url, _ in calls), 1)


class FailingMetricsClient:
    base_url = "http://router"

    def health(self):
        return {"status": "ok"}

    def props(self):
        return {}

    def models(self):
        return [{"id": "model-a", "status": {"value": "loaded"}}]

    def metrics(self, model=None):
        raise RuntimeError(f"metrics unavailable for {model}")

    def close(self):
        return None


class CollectorFailureTests(unittest.TestCase):
    def test_metrics_failure_count_is_not_reset_between_polls(self):
        engine = memory_engine()
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            collector = Collector(lambda _: FailingMetricsClient())
            entry = {"key": "model-a", "loaded": True, "args": [], "meta": {}}

            state = None
            for expected in range(1, 4):
                error = collector._poll_model(
                    session, provider, entry, {}, state, FailingMetricsClient(),
                    {"status": "ok"}, {}, int(time.time() * 1000), time.time(),
                )
                state = collector.model_states[(provider.id, "model-a")]
                self.assertEqual(state.metrics_fail, expected)
                self.assertIn("metrics failed for model-a", error)

    def test_all_loaded_model_metrics_failures_mark_provider_offline(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add(Provider(
                name="router", base_url="http://router", enabled=True,
                poll_interval_s=0.25,
            ))
            session.commit()

        collector = Collector(lambda _: FailingMetricsClient())
        with patch(
            "observatory.collector.db.new_session",
            side_effect=lambda: Session(engine),
        ):
            collector._tick()

        with Session(engine) as session:
            provider = session.exec(select(Provider)).one()
            self.assertEqual(provider.status, "OFFLINE")
            self.assertEqual(provider.fail_streak, 1)
            self.assertIn("metrics failed for model-a", provider.last_error)


if __name__ == "__main__":
    unittest.main()
