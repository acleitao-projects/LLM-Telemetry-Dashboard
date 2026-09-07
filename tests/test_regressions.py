from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from fastapi.testclient import TestClient

import app
from observatory import metrics
from observatory.collector import Collector
from observatory.models import Model, Provider, SessionRow


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
            if url.endswith("/slots"):
                self.assertEqual(kwargs.get("params"), {"model": "hot model"})
                return Response([])
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


class UnloadModelsTests(unittest.TestCase):
    def test_unloads_each_loaded_model_and_ignores_unloaded_entries(self):
        calls = []

        class Client:
            def __init__(self, base_url, timeout):
                self.base_url = base_url

            def models(self):
                return [
                    {"id": "cold", "status": {"value": "unloaded"}},
                    {"id": "hot-a", "status": {"value": "loaded"}},
                    {"name": "hot-b", "status": {"value": "loaded"}},
                ]

            def unload(self, model):
                calls.append(model)
                return {"success": True}

            def close(self):
                calls.append("closed")

        with patch("app.LlamaClient", Client):
            result = app._unload_provider_models(Provider(
                id=4, name="router", base_url="http://router"))

        self.assertEqual(result["status"], "unloaded")
        self.assertEqual(result["models"], ["hot-a", "hot-b"])
        self.assertEqual(calls, ["hot-a", "hot-b", "closed"])

    def test_provider_failure_is_reported_without_raising(self):
        class Client:
            def __init__(self, base_url, timeout):
                pass

            def models(self):
                raise RuntimeError("connection refused")

            def close(self):
                pass

        with patch("app.LlamaClient", Client):
            result = app._unload_provider_models(Provider(
                id=5, name="offline", base_url="http://offline"))

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["models"], [])
        self.assertIn("connection refused", result["error"])


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


class StatusNoOutboundTests(unittest.TestCase):
    def test_status_does_not_make_outbound_httpx_calls(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add(Provider(name="router", base_url="http://router",
                                 agent_url="http://agent", status="LIVE"))
            session.commit()
        with patch("httpx.get") as mock_get:
            with Session(engine) as session:
                data = metrics.status(session)
            mock_get.assert_not_called()
        self.assertEqual(data["providers"][0]["agent_status"], "LIVE")

    def test_status_agent_offline_when_provider_not_live(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.add(Provider(name="router", base_url="http://router",
                                  agent_url="http://agent", status="OFFLINE"))
            session.commit()
        with Session(engine) as session:
            data = metrics.status(session)
        self.assertEqual(data["providers"][0]["agent_status"], "OFFLINE")


class CacheLockTests(unittest.TestCase):
    def test_models_endpoint_no_deadlock_on_nested_key_lock(self):
        """cached_models_payload acquires _key_lock(key) then calls
        cached_range_summary which acquires _key_lock(same_key).
        A non-reentrant Lock deadlocks; RLock allows re-entry."""
        engine = memory_engine()
        with Session(engine) as s:
            s.add(Provider(name="router", base_url="http://router"))
            s.commit()
        with patch("observatory.database._engine", engine), \
             patch("observatory.database._db_path", "test.db"):
            with TestClient(app.create_app(demo=True)) as client:
                resp = client.get("/api/models?range=7d")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("rows", resp.json())


class SelectedStatsMissingProviderTests(unittest.TestCase):
    """selected_stats must not 500 when a Model references a deleted Provider."""

    def test_selected_stats_survives_missing_provider(self):
        engine = memory_engine()
        with Session(engine) as s:
            s.add(Provider(name="alive", base_url="http://alive"))
            orphan = Model(provider_id=999, key="orphan", name="Orphan-Model",
                           color="#fff", first_seen_at=0)
            s.add(orphan)
            s.commit()
            s.refresh(orphan)
            model_id = orphan.id
        with Session(engine) as s:
            summary = metrics.range_summary(s, None, "7d")
            result = metrics.selected_stats(s, [model_id], None, "7d", summary, None)
        self.assertIsInstance(result, dict)
        self.assertIsNone(result["provider"])
        self.assertEqual(result["label"], "Orphan-Model")


if __name__ == "__main__":
    unittest.main()
