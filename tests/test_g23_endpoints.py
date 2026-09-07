"""G09 issue #23, step 5: the Settings/API endpoints for automatic pricing."""
from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app as app_module
from observatory import database as odb
from observatory import pricing_sync as ps
from observatory.models import Model, PricingSyncRun, Provider, Setting, now_ms


def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


CATALOG = {
    "entries": {
        "claude-sonnet-4-5": {
            "input_cost_per_token": Decimal("3e-06"),
            "output_cost_per_token": Decimal("1.5e-05"),
            "cache_creation_input_token_cost": Decimal("3.75e-06"),
            "cache_read_input_token_cost": Decimal("3e-07"),
        },
    },
    "etag": '"abc"', "commit": "deadbeef", "fetched_at": 1_000, "not_modified": False,
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            p = Provider(name="provA", base_url="http://provA", status="LIVE",
                         enabled=True, is_default=True,
                         last_success_at=now_ms() - 1000)
            s.add(p)
            s.commit()
            s.refresh(p)
            self.prov_id = p.id
            m = Model(provider_id=p.id, key="m-a",
                      name="Claude-Sonnet-4-5-Q4_K_M.gguf")
            s.add(m)
            s.commit()
            s.refresh(m)
            self.model_id = m.id
        self._oe, self._op = odb._engine, odb._db_path
        odb._engine, odb._db_path = self.engine, "test.db"
        self.client = TestClient(app_module.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine, odb._db_path = self._oe, self._op

    def _get(self, key):
        with Session(self.engine) as s:
            row = s.get(Setting, key)
            return row.value if row else None


class ConfigEndpoint(_Base):
    def test_get_defaults(self):
        b = self.client.get("/api/settings/pricing-sync").json()
        self.assertFalse(b["enabled"])
        self.assertEqual(b["run_time"], "04:00")
        self.assertEqual(b["prompt"], ps.DEFAULT_PROMPT)
        self.assertEqual(b["recent_runs"], [])
        self.assertTrue(any(o["id"] == self.prov_id
                            for o in b["provider_options"]))

    def test_put_validates_run_time(self):
        self.assertEqual(
            self.client.put("/api/settings/pricing-sync",
                            json={"run_time": "9:5"}).status_code, 422)
        self.assertEqual(
            self.client.put("/api/settings/pricing-sync",
                            json={"run_time": "25:00"}).status_code, 422)
        r = self.client.put("/api/settings/pricing-sync",
                            json={"run_time": "04:30"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["run_time"], "04:30")

    def test_put_validates_prompt_placeholders(self):
        bad = "no placeholders here"
        self.assertEqual(
            self.client.put("/api/settings/pricing-sync",
                            json={"prompt": bad}).status_code, 422)
        good = "Name: {model_name}\nOptions:\n{candidates}\nReply JSON."
        r = self.client.put("/api/settings/pricing-sync", json={"prompt": good})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["prompt"], good)

    def test_put_validates_match_provider(self):
        self.assertEqual(
            self.client.put("/api/settings/pricing-sync",
                            json={"match_provider_id": 9999}).status_code, 422)
        r = self.client.put("/api/settings/pricing-sync",
                            json={"match_provider_id": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["match_provider_id"])

    def test_put_stores_flags(self):
        r = self.client.put("/api/settings/pricing-sync",
                            json={"enabled": True, "use_inference_match": True,
                                  "match_provider_id": self.prov_id})
        b = r.json()
        self.assertTrue(b["enabled"])
        self.assertTrue(b["use_inference_match"])
        self.assertEqual(b["match_provider_id"], self.prov_id)

    def test_get_exposes_default_prompt(self):
        b = self.client.get("/api/settings/pricing-sync").json()
        self.assertIn("default_prompt", b)
        self.assertEqual(b["default_prompt"], b["prompt"])
        self.assertIn("{model_name}", b["default_prompt"])

    def test_match_model_round_trips_and_clears(self):
        r = self.client.put("/api/settings/pricing-sync",
                            json={"match_model": "Qwen3.6-35B-A3B-Uncensored"})
        self.assertEqual(r.json()["match_model"], "Qwen3.6-35B-A3B-Uncensored")
        r = self.client.put("/api/settings/pricing-sync", json={"match_model": ""})
        self.assertIsNone(r.json()["match_model"])

    def test_provider_models_endpoint(self):
        r = self.client.get("/api/settings/pricing-sync/provider-models?provider=999999")
        self.assertEqual(r.status_code, 404)
        r = self.client.get(
            f"/api/settings/pricing-sync/provider-models?provider={self.prov_id}")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("models", d)
        # provA points at a dead URL -> a clean error, never a 500
        self.assertIn("error", d)


class RunNow(_Base):
    """Run-now is fire-and-forget: the work happens in a worker thread. A
    file-backed DB (not shared :memory:) gives that thread its own connection
    so its commits are visible to the assertions."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="g23_runnow_")
        path = os.path.join(self._tmp, "t.db")
        self.engine = create_engine(f"sqlite:///{path}",
                                    connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            p = Provider(name="provA", base_url="http://provA", status="LIVE",
                         enabled=True, is_default=True,
                         last_success_at=now_ms() - 1000)
            s.add(p); s.commit(); s.refresh(p)
            self.prov_id = p.id
            m = Model(provider_id=p.id, key="m-a",
                      name="Claude-Sonnet-4-5-Q4_K_M.gguf")
            s.add(m); s.commit(); s.refresh(m)
            self.model_id = m.id
        self._oe, self._op = odb._engine, odb._db_path
        odb._engine, odb._db_path = self.engine, "test.db"
        self.client = TestClient(app_module.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine, odb._db_path = self._oe, self._op
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _wait_done(self, timeout=15.0):
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            b = self.client.get("/api/settings/pricing-sync").json()
            runs = b.get("recent_runs") or []
            if (not b["running"] and runs and runs[0]["finished_at"]
                    and runs[0]["result"] != "running"):
                return runs[0]
            _t.sleep(0.05)
        raise AssertionError("pricing run did not finish")

    def test_run_now_prices_a_model_and_logs(self):
        with patch.object(ps, "fetch_catalog", lambda *a, **k: dict(CATALOG)):
            r = self.client.post("/api/settings/pricing-sync/run")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["started"])
            run = self._wait_done()
        self.assertEqual(run["trigger"], "manual")
        self.assertEqual(run["result"], "ok")
        with Session(self.engine) as s:
            m = s.get(Model, self.model_id)
            self.assertEqual(m.input_price_per_million, "3.00000000")
            self.assertEqual(m.cache_read_price_per_million, "0.30000000")
            runs = list(s.exec(__import__("sqlmodel").select(PricingSyncRun)))
            self.assertEqual(len(runs), 1)

    def test_run_now_too_soon_then_force(self):
        with patch.object(ps, "fetch_catalog", lambda *a, **k: dict(CATALOG)):
            self.client.post("/api/settings/pricing-sync/run")
            self._wait_done()
            again = self.client.post("/api/settings/pricing-sync/run")
            self.assertEqual(again.json().get("error"), "too soon")
            forced = self.client.post("/api/settings/pricing-sync/run?force=1")
            self.assertEqual(forced.status_code, 200)
            self.assertTrue(forced.json().get("started"))
            self._wait_done()

    def test_run_now_conflicts_when_locked(self):
        self.client.app.state.pricing_run_lock.acquire()
        try:
            r = self.client.post("/api/settings/pricing-sync/run?force=1")
        finally:
            self.client.app.state.pricing_run_lock.release()
        self.assertEqual(r.status_code, 409)

    def test_runs_list_capped(self):
        with Session(self.engine) as s:
            for i in range(5):
                s.add(PricingSyncRun(trigger="scheduled",
                                     started_at=now_ms() + i, result="ok"))
            s.commit()
        r = self.client.get("/api/settings/pricing-sync/runs?limit=2")
        self.assertEqual(len(r.json()["runs"]), 2)
        r = self.client.get("/api/settings/pricing-sync/runs?limit=99999")
        self.assertLessEqual(len(r.json()["runs"]), 200)


class ModelPricingExtensions(_Base):
    def test_models_endpoint_carries_new_fields(self):
        rows = self.client.get(
            f"/api/settings/models?provider={self.prov_id}").json()["models"]
        row = rows[0]
        for f in ("cache_write_price_per_million", "cache_read_price_per_million",
                  "pricing_mode", "pricing_stale", "pricing_source"):
            self.assertIn(f, row)
        self.assertEqual(row["pricing_mode"], "auto")

    def test_manual_edit_flips_mode_and_saves_cache_fields(self):
        r = self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_id,
            "models": [{"model_id": self.model_id,
                        "input_price_per_million": "1.0",
                        "output_price_per_million": "2.0",
                        "cache_write_price_per_million": "0.5"}],
        })
        self.assertEqual(r.status_code, 200)
        saved = r.json()["models"][0]
        self.assertEqual(saved["pricing_mode"], "manual")
        # validate_price canonicalises but does not pad
        self.assertEqual(saved["cache_write_price_per_million"], "0.5")

    def test_mode_endpoint(self):
        self.assertEqual(self.client.put(
            "/api/settings/model-pricing/mode",
            json={"model_id": self.model_id, "mode": "bogus"}).status_code, 422)
        self.assertEqual(self.client.put(
            "/api/settings/model-pricing/mode",
            json={"model_id": 999999, "mode": "auto"}).status_code, 422)
        r = self.client.put("/api/settings/model-pricing/mode",
                            json={"model_id": self.model_id, "mode": "manual"})
        self.assertEqual(r.status_code, 200)
        with Session(self.engine) as s:
            self.assertEqual(s.get(Model, self.model_id).pricing_mode, "manual")


if __name__ == "__main__":
    unittest.main()
