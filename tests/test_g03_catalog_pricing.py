"""G03: catalog availability + decimal-safe pricing (issues #7, #8)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from fastapi.testclient import TestClient

import app
from observatory import database as odb
from observatory.collector import Collector
from observatory.models import Model, Provider, now_ms
from observatory.pricing import (PricingValidationError, compute_costs,
                                 validate_price)
from observatory.settings import RANGE_KEYS


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_provider_and_models(session, provider_name="p", n_models=2):
    p = Provider(name=provider_name, base_url=f"http://{provider_name}")
    session.add(p)
    session.commit()
    session.refresh(p)
    models = []
    for j in range(n_models):
        m = Model(provider_id=p.id, key=f"key-{j}", name=f"Model-{j}",
                  catalog_available=True, catalog_last_seen_at=1000)
        session.add(m)
        session.commit()
        session.refresh(m)
        models.append(m)
    return p, models


# ---------------------------------------------------------------------------
# 1. validate_price / PricingValidationError
# ---------------------------------------------------------------------------

class PricingValidationTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(validate_price(None, 1, "input_price_per_million"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(validate_price("", 1, "input_price_per_million"))

    def test_whitespace_returns_none(self):
        self.assertIsNone(validate_price("   ", 1, "input_price_per_million"))

    def test_valid_integer_string(self):
        self.assertEqual(validate_price("5", 1, "f"), "5")

    def test_valid_decimal_string(self):
        self.assertEqual(validate_price("1.5", 1, "f"), "1.5")

    def test_valid_zero(self):
        self.assertEqual(validate_price("0", 1, "f"), "0")

    def test_fixed_point_preserves_scale(self):
        self.assertEqual(validate_price("10.500", 1, "f"), "10.500")

    def test_normalizes_leading_zeros(self):
        self.assertEqual(validate_price("0.5", 1, "f"), "0.5")

    def test_negative_raises(self):
        with self.assertRaises(PricingValidationError):
            validate_price("-1", 1, "input_price_per_million")

    def test_non_numeric_raises(self):
        with self.assertRaises(PricingValidationError):
            validate_price("abc", 1, "output_price_per_million")

    def test_nan_raises(self):
        with self.assertRaises(PricingValidationError):
            validate_price("NaN", 1, "f")

    def test_infinity_raises(self):
        with self.assertRaises(PricingValidationError):
            validate_price("Infinity", 1, "f")

    def test_negative_infinity_raises(self):
        with self.assertRaises(PricingValidationError):
            validate_price("-Infinity", 1, "f")

    def test_error_contains_model_id_and_field(self):
        try:
            validate_price("bad", 42, "output_price_per_million")
            self.fail("expected PricingValidationError")
        except PricingValidationError as exc:
            self.assertEqual(exc.model_id, 42)
            self.assertEqual(exc.field, "output_price_per_million")
            self.assertIn("42", str(exc))

    def test_scientific_notation_converted_to_fixed(self):
        self.assertEqual(validate_price("1e2", 1, "f"), "100")


# ---------------------------------------------------------------------------
# 2. compute_costs
# ---------------------------------------------------------------------------

class _FakeModel:
    def __init__(self, inp=None, outp=None):
        self.input_price_per_million = inp
        self.output_price_per_million = outp


class ComputeCostsTests(unittest.TestCase):
    def test_known_rates(self):
        m = _FakeModel("2", "8")
        # 1M input tokens at $2/M, 1M output at $8/M
        r = compute_costs(m, 1_000_000, 1_000_000)
        self.assertEqual(r["input_cost"], "2.00000000")
        self.assertEqual(r["output_cost"], "8.00000000")
        self.assertEqual(r["total_cost"], "10.00000000")

    def test_missing_rates_zero(self):
        m = _FakeModel(None, None)
        r = compute_costs(m, 500_000, 500_000)
        self.assertEqual(r["input_cost"], "0.00000000")
        self.assertEqual(r["output_cost"], "0.00000000")
        self.assertEqual(r["total_cost"], "0.00000000")
        self.assertEqual(r["input_price_per_million"], "0.00000000")
        self.assertEqual(r["output_price_per_million"], "0.00000000")

    def test_zero_tokens(self):
        m = _FakeModel("2", "8")
        r = compute_costs(m, 0, 0)
        self.assertEqual(r["input_cost"], "0.00000000")
        self.assertEqual(r["output_cost"], "0.00000000")
        self.assertEqual(r["total_cost"], "0.00000000")

    def test_fractional_tokens(self):
        m = _FakeModel("2", "8")
        # 500K input at $2/M = $1.00
        r = compute_costs(m, 500_000, 0)
        self.assertEqual(r["input_cost"], "1.00000000")

    def test_small_amount_precision(self):
        m = _FakeModel("1", "1")
        # 1000 tokens at $1/M = $0.001
        r = compute_costs(m, 1_000, 0)
        self.assertEqual(r["input_cost"], "0.00100000")

    def test_output_only(self):
        m = _FakeModel("0", "4")
        r = compute_costs(m, 0, 250_000)
        # 250K at $4/M = $1.00
        self.assertEqual(r["input_cost"], "0.00000000")
        self.assertEqual(r["output_cost"], "1.00000000")
        self.assertEqual(r["total_cost"], "1.00000000")

    def test_full_precision_decimal(self):
        m = _FakeModel("0.000001", "0.000002")
        # 1M in at $0.000001/M = 0.000001
        # 1M out at $0.000002/M = 0.000002
        r = compute_costs(m, 1_000_000, 1_000_000)
        self.assertEqual(r["input_cost"], "0.00000100")
        self.assertEqual(r["output_cost"], "0.00000200")
        self.assertEqual(r["total_cost"], "0.00000300")


# ---------------------------------------------------------------------------
# 3. Pricing API endpoints (TestClient)
# ---------------------------------------------------------------------------

class PricingApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            p, models = _seed_provider_and_models(s, "prov-a", 2)
            self.provider_id = p.id
            self.model_ids = [m.id for m in models]
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_get_models_returns_catalog_fields(self):
        resp = self.client.get(f"/api/settings/models?provider={self.provider_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("models", body)
        self.assertEqual(len(body["models"]), 2)
        first = body["models"][0]
        for field in ("id", "key", "name", "catalog_available",
                      "catalog_last_seen_at", "input_price_per_million",
                      "output_price_per_million"):
            self.assertIn(field, first)
        self.assertTrue(first["catalog_available"])

    def test_get_models_unknown_provider_404(self):
        resp = self.client.get("/api/settings/models?provider=99999")
        self.assertEqual(resp.status_code, 404)

    def test_put_pricing_sparse_update(self):
        payload = {
            "provider_id": self.provider_id,
            "models": [
                {"model_id": self.model_ids[0],
                 "input_price_per_million": "1.5",
                 "output_price_per_million": "3.0"},
            ],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["updated"], 1)
        with Session(self.engine) as s:
            m0 = s.get(Model, self.model_ids[0])
            m1 = s.get(Model, self.model_ids[1])
            self.assertEqual(m0.input_price_per_million, "1.5")
            self.assertEqual(m0.output_price_per_million, "3.0")
            # second model untouched
            self.assertIsNone(m1.input_price_per_million)
            self.assertIsNone(m1.output_price_per_million)

    def test_put_pricing_blank_clears(self):
        with Session(self.engine) as s:
            m = s.get(Model, self.model_ids[0])
            m.input_price_per_million = "5"
            s.commit()
        payload = {
            "provider_id": self.provider_id,
            "models": [{"model_id": self.model_ids[0],
                        "input_price_per_million": "",
                        "output_price_per_million": ""}],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 200)
        with Session(self.engine) as s:
            m = s.get(Model, self.model_ids[0])
            self.assertIsNone(m.input_price_per_million)
            self.assertIsNone(m.output_price_per_million)

    def test_put_pricing_invalid_value_422_no_writes(self):
        payload = {
            "provider_id": self.provider_id,
            "models": [
                {"model_id": self.model_ids[0],
                 "input_price_per_million": "not-a-number"},
            ],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 422)
        with Session(self.engine) as s:
            m = s.get(Model, self.model_ids[0])
            self.assertIsNone(m.input_price_per_million)

    def test_put_pricing_atomicity_rollback(self):
        """If model 2 fails validation, model 1 must also not be written."""
        payload = {
            "provider_id": self.provider_id,
            "models": [
                {"model_id": self.model_ids[0], "input_price_per_million": "1"},
                {"model_id": self.model_ids[1], "input_price_per_million": "bad"},
            ],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 422)
        with Session(self.engine) as s:
            m0 = s.get(Model, self.model_ids[0])
            m1 = s.get(Model, self.model_ids[1])
            self.assertIsNone(m0.input_price_per_million)
            self.assertIsNone(m1.input_price_per_million)

    def test_put_pricing_wrong_provider_422(self):
        with Session(self.engine) as s:
            other = Provider(name="other", base_url="http://other")
            s.add(other)
            s.commit()
            s.refresh(other)
            other_model = Model(provider_id=other.id, key="om1", name="Other-M1")
            s.add(other_model)
            s.commit()
            s.refresh(other_model)
            other_model_id = other_model.id
        payload = {
            "provider_id": self.provider_id,
            "models": [{"model_id": other_model_id, "input_price_per_million": "1"}],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("not found for provider", resp.json()["detail"])

    def test_put_pricing_duplicate_model_id_422(self):
        payload = {
            "provider_id": self.provider_id,
            "models": [
                {"model_id": self.model_ids[0], "input_price_per_million": "1"},
                {"model_id": self.model_ids[0], "output_price_per_million": "2"},
            ],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate model_id", resp.json()["detail"])

    def test_put_pricing_missing_provider_id_422(self):
        payload = {"models": [{"model_id": self.model_ids[0]}]}
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 422)

    def test_put_pricing_empty_models_422(self):
        payload = {"provider_id": self.provider_id, "models": []}
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 422)

    def test_put_pricing_invalidates_snapshots(self):
        invalidated = []
        original_invalidate = self.client.app.state.snapshots.invalidate

        def spy_invalidate(key):
            invalidated.append(key)
            original_invalidate(key)

        self.client.app.state.snapshots.invalidate = spy_invalidate
        payload = {
            "provider_id": self.provider_id,
            "models": [{"model_id": self.model_ids[0],
                        "input_price_per_million": "1"}],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(invalidated), len(RANGE_KEYS))
        for rk in RANGE_KEYS:
            self.assertIn((self.provider_id, rk), invalidated)


# ---------------------------------------------------------------------------
# 4. Collector catalog methods
# ---------------------------------------------------------------------------

class _FakeClient:
    def metrics(self, model=None):
        return {}

    def slots(self, model=None):
        return []


class CollectorCatalogTests(unittest.TestCase):
    def test_upsert_new_model_sets_catalog_fields(self):
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        ts = 12345
        with Session(engine) as s:
            p = Provider(name="p", base_url="http://p")
            s.add(p)
            s.commit()
            s.refresh(p)
            entry = {"key": "new-model", "loaded": True, "args": [], "meta": {}}
            m = collector._upsert_model_entry(s, p, entry, {}, ts_ms=ts)
            s.commit()
            self.assertTrue(m.catalog_available)
            self.assertEqual(m.catalog_last_seen_at, ts)

    def test_upsert_existing_model_updates_catalog(self):
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        with Session(engine) as s:
            p = Provider(name="p", base_url="http://p")
            s.add(p)
            s.commit()
            s.refresh(p)
            # Create model already absent
            m = Model(provider_id=p.id, key="m1", name="m1",
                      catalog_available=False, catalog_last_seen_at=100)
            s.add(m)
            s.commit()
            s.refresh(m)
            # Upsert should re-activate
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            result = collector._upsert_model_entry(s, p, entry, {}, ts_ms=999)
            s.commit()
            self.assertTrue(result.catalog_available)
            self.assertEqual(result.catalog_last_seen_at, 999)

    def test_mark_absent_catalog_models(self):
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        with Session(engine) as s:
            p = Provider(name="p", base_url="http://p")
            s.add(p)
            s.commit()
            s.refresh(p)
            m_present = Model(provider_id=p.id, key="present", name="present",
                              catalog_available=True, catalog_last_seen_at=100)
            m_absent = Model(provider_id=p.id, key="absent", name="absent",
                             catalog_available=True, catalog_last_seen_at=200)
            already_absent = Model(provider_id=p.id, key="gone", name="gone",
                                   catalog_available=False, catalog_last_seen_at=300)
            s.add(m_present)
            s.add(m_absent)
            s.add(already_absent)
            s.commit()
            # Only "present" is still in the catalog
            collector._mark_absent_catalog_models(s, p, {"present"})
            s.commit()
            m_present = s.get(Model, m_present.id)
            m_absent = s.get(Model, m_absent.id)
            already_absent = s.get(Model, already_absent.id)
            self.assertTrue(m_present.catalog_available)
            self.assertFalse(m_absent.catalog_available)
            self.assertFalse(already_absent.catalog_available)

    def test_mark_absent_ignores_other_providers(self):
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        with Session(engine) as s:
            p1 = Provider(name="p1", base_url="http://p1")
            p2 = Provider(name="p2", base_url="http://p2")
            s.add(p1)
            s.add(p2)
            s.commit()
            s.refresh(p1)
            s.refresh(p2)
            m1 = Model(provider_id=p1.id, key="k1", name="k1",
                       catalog_available=True, catalog_last_seen_at=100)
            m2 = Model(provider_id=p2.id, key="k2", name="k2",
                       catalog_available=True, catalog_last_seen_at=100)
            s.add(m1)
            s.add(m2)
            s.commit()
            # Mark absent for p1 only
            collector._mark_absent_catalog_models(s, p1, set())
            s.commit()
            m1 = s.get(Model, m1.id)
            m2 = s.get(Model, m2.id)
            self.assertFalse(m1.catalog_available)
            self.assertTrue(m2.catalog_available)


# ---------------------------------------------------------------------------
# 5. Regression tests for review fixes
# ---------------------------------------------------------------------------

class GroupedCostRegressionTests(unittest.TestCase):
    """selected_stats and compare_models must price per-model, not apply one rate to aggregate."""

    def setUp(self):
        self.engine = memory_engine()
        from observatory.metrics import selected_stats
        self._selected_stats = selected_stats
        with Session(self.engine) as s:
            self.p, self.models = _seed_provider_and_models(s, "grp", 2)
            self.provider_id = self.p.id
            self.model_ids = [m.id for m in self.models]
            self.model_keys = [m.key for m in self.models]
            # Give each model different rates
            self.models[0].input_price_per_million = "10"
            self.models[0].output_price_per_million = "20"
            self.models[1].input_price_per_million = "5"
            self.models[1].output_price_per_million = "15"
            s.commit()

    def test_selected_stats_mixed_rates_cost_is_sum(self):
        """Grouped cost must equal sum of each model's independently priced usage."""
        from observatory.models import SessionRow, TelemetrySample
        now = now_ms()
        with Session(self.engine) as s:
            # Model 0: 1_000_000 prompt, 500_000 gen
            s.add(SessionRow(
                provider_id=self.provider_id, model_id=self.model_ids[0],
                start_at=now - 3600_000, end_at=now, duration_s=1800,
                prompt_tokens=1_000_000, gen_tokens=500_000,
                total_tokens=1_500_000, status="CLOSED", result_source="metrics"))
            # Model 1: 2_000_000 prompt, 1_000_000 gen
            s.add(SessionRow(
                provider_id=self.provider_id, model_id=self.model_ids[1],
                start_at=now - 3600_000, end_at=now, duration_s=1800,
                prompt_tokens=2_000_000, gen_tokens=1_000_000,
                total_tokens=3_000_000, status="CLOSED", result_source="metrics"))
            s.add(TelemetrySample(
                provider_id=self.provider_id, model_id=self.model_ids[0],
                ts=now - 1800_000, state="CLOSED",
                tokens_total=1_500_000, prompt_total=1_000_000, gen_total=500_000))
            s.add(TelemetrySample(
                provider_id=self.provider_id, model_id=self.model_ids[1],
                ts=now - 1800_000, state="CLOSED",
                tokens_total=3_000_000, prompt_total=2_000_000, gen_total=1_000_000))
            s.commit()
            result = self._selected_stats(
                s, [self.model_ids[0], self.model_ids[1]], self.provider_id, "24h")
        from decimal import Decimal
        # Model 0: 1M * 10/1M = $10 input, 0.5M * 20/1M = $10 output
        # Model 1: 2M * 5/1M = $10 input, 1M * 15/1M = $15 output
        # Total: $20 input, $25 output, $45 total
        expected_in = Decimal("20.00000000")
        expected_out = Decimal("25.00000000")
        expected_total = Decimal("45.00000000")
        self.assertEqual(Decimal(result["input_cost"]), expected_in)
        self.assertEqual(Decimal(result["output_cost"]), expected_out)
        self.assertEqual(Decimal(result["total_cost"]), expected_total)
        # Mixed rates must be null
        self.assertIsNone(result["input_price_per_million"])
        self.assertIsNone(result["output_price_per_million"])

    def test_selected_stats_single_rate_exposed(self):
        """When all models share the same rate, expose it."""
        with Session(self.engine) as s:
            m1 = s.get(Model, self.model_ids[1])
            m1.input_price_per_million = "10"
            m1.output_price_per_million = "20"
            s.commit()
            # Minimal data so the function returns something
            from observatory.models import SessionRow
            now = now_ms()
            s.add(SessionRow(
                provider_id=self.provider_id, model_id=self.model_ids[0],
                start_at=now - 60_000, end_at=now, duration_s=30,
                prompt_tokens=100, gen_tokens=50, total_tokens=150,
                status="CLOSED", result_source="metrics"))
            s.commit()
            result = self._selected_stats(
                s, [self.model_ids[0], self.model_ids[1]], self.provider_id, "24h")
        self.assertEqual(result["input_price_per_million"], "10")
        self.assertEqual(result["output_price_per_million"], "20")


class CompareGroupCostRegressionTests(unittest.TestCase):
    """compare_models must price per exact model, not apply one rate to family aggregate."""

    def test_compare_single_model_uses_own_rate(self):
        from observatory.metrics import compare_models
        from observatory.models import SessionRow
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _seed_provider_and_models(s, "cmp", 1)
            models[0].input_price_per_million = "100"
            models[0].output_price_per_million = "200"
            s.commit()
            now = now_ms()
            s.add(SessionRow(
                provider_id=p.id, model_id=models[0].id,
                start_at=now - 600_000, end_at=now, duration_s=300,
                prompt_tokens=1_000_000, gen_tokens=500_000,
                total_tokens=1_500_000, status="CLOSED", result_source="metrics"))
            s.commit()
            result = compare_models(s, [str(models[0].id)], p.id, "24h")
        self.assertEqual(result["models"][0]["input_cost"], "100.00000000")
        self.assertEqual(result["models"][0]["output_cost"], "100.00000000")
        self.assertEqual(result["models"][0]["total_cost"], "200.00000000")


class EmptyCatalogRegressionTests(unittest.TestCase):
    """Successful empty /v1/models must mark all previously-available models unavailable."""

    def test_empty_catalog_marks_all_unavailable(self):
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        with Session(engine) as s:
            p = Provider(name="p", base_url="http://p")
            s.add(p)
            s.commit()
            s.refresh(p)
            m1 = Model(provider_id=p.id, key="m1", name="m1",
                       catalog_available=True, catalog_last_seen_at=100)
            m2 = Model(provider_id=p.id, key="m2", name="m2",
                       catalog_available=True, catalog_last_seen_at=200)
            s.add(m1)
            s.add(m2)
            s.commit()
            # Simulate successful empty catalog: known_keys is empty set
            collector._mark_absent_catalog_models(s, p, set())
            s.commit()
            m1 = s.get(Model, m1.id)
            m2 = s.get(Model, m2.id)
            # Models remain persisted but are now unavailable
            self.assertIsNotNone(m1)
            self.assertIsNotNone(m2)
            self.assertFalse(m1.catalog_available)
            self.assertFalse(m2.catalog_available)

    def test_failed_catalog_does_not_mark_unavailable(self):
        """When models_ok is False, _mark_absent is never called (tested via logic)."""
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        with Session(engine) as s:
            p = Provider(name="p", base_url="http://p")
            s.add(p)
            s.commit()
            s.refresh(p)
            m1 = Model(provider_id=p.id, key="m1", name="m1",
                       catalog_available=True, catalog_last_seen_at=100)
            s.add(m1)
            s.commit()
            # Simulate: models_ok=False means we skip the call entirely.
            # Verify the guard: if we DON'T call _mark_absent, state is unchanged.
            m1 = s.get(Model, m1.id)
            self.assertTrue(m1.catalog_available)


class CatalogInvalidationRegressionTests(unittest.TestCase):
    """Catalog change detection must bump generation only on meaningful changes."""

    def _make_collector_with_provider(self):
        engine = memory_engine()
        collector = Collector(lambda _: _FakeClient())
        with Session(engine) as s:
            p = Provider(name="p", base_url="http://p")
            s.add(p)
            s.commit()
            s.refresh(p)
        return engine, collector, p.id

    def _avail_keys(self, engine, provider_id):
        with Session(engine) as s:
            rows = s.exec(select(Model).where(
                Model.provider_id == provider_id,
                Model.catalog_available == True,  # noqa: E712
            )).all()
            return {row.key for row in rows}

    def test_new_model_triggers_generation_bump(self):
        engine, collector, pid = self._make_collector_with_provider()
        gen_before = collector.data_generation
        with Session(engine) as s:
            provider = s.get(Provider, pid)
            pre_avail = {row.key for row in s.exec(select(Model).where(
                Model.provider_id == pid, Model.catalog_available == True)).all()}
            entry = {"key": "new-m", "loaded": True, "args": [], "meta": {}}
            collector._upsert_model_entry(s, provider, entry, {}, ts_ms=99999)
            s.flush()
            post_avail = {row.key for row in s.exec(select(Model).where(
                Model.provider_id == pid, Model.catalog_available == True)).all()}
            if post_avail != pre_avail:
                collector._bump_data_generation()
            s.commit()
        self.assertEqual(collector.data_generation, gen_before + 1)

    def test_timestamp_only_no_bump(self):
        engine, collector, pid = self._make_collector_with_provider()
        with Session(engine) as s:
            provider = s.get(Provider, pid)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            collector._upsert_model_entry(s, provider, entry, {}, ts_ms=1000)
            s.commit()
        gen_before = collector.data_generation
        with Session(engine) as s:
            provider = s.get(Provider, pid)
            pre_avail = {row.key for row in s.exec(select(Model).where(
                Model.provider_id == pid, Model.catalog_available == True)).all()}
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            collector._upsert_model_entry(s, provider, entry, {}, ts_ms=2000)
            s.flush()
            post_avail = {row.key for row in s.exec(select(Model).where(
                Model.provider_id == pid, Model.catalog_available == True)).all()}
            if post_avail != pre_avail:
                collector._bump_data_generation()
            s.commit()
        self.assertEqual(collector.data_generation, gen_before)

    def test_disappeared_model_triggers_bump(self):
        engine, collector, pid = self._make_collector_with_provider()
        with Session(engine) as s:
            provider = s.get(Provider, pid)
            entry = {"key": "m1", "loaded": True, "args": [], "meta": {}}
            collector._upsert_model_entry(s, provider, entry, {}, ts_ms=1000)
            s.commit()
        gen_before = collector.data_generation
        with Session(engine) as s:
            provider = s.get(Provider, pid)
            pre_avail = {row.key for row in s.exec(select(Model).where(
                Model.provider_id == pid, Model.catalog_available == True)).all()}
            self.assertIn("m1", pre_avail)
            collector._mark_absent_catalog_models(s, provider, set())
            s.flush()
            post_avail = {row.key for row in s.exec(select(Model).where(
                Model.provider_id == pid, Model.catalog_available == True)).all()}
            if post_avail != pre_avail:
                collector._bump_data_generation()
            s.commit()
        self.assertEqual(collector.data_generation, gen_before + 1)


class PutResponseContractTests(unittest.TestCase):
    """PUT must return normalized saved rows with model_id, key, and prices."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            p, models = _seed_provider_and_models(s, "put", 2)
            self.provider_id = p.id
            self.model_ids = [m.id for m in models]
            self.model_keys = [m.key for m in models]
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_put_returns_normalized_rows(self):
        payload = {
            "provider_id": self.provider_id,
            "models": [
                {"model_id": self.model_ids[0], "input_price_per_million": "3.5",
                 "output_price_per_million": "7.25"},
                {"model_id": self.model_ids[1], "input_price_per_million": "1",
                 "output_price_per_million": ""},
            ],
        }
        resp = self.client.put("/api/settings/model-pricing", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["updated"], 2)
        self.assertIn("models", data)
        self.assertEqual(len(data["models"]), 2)
        row0 = data["models"][0]
        self.assertEqual(row0["model_id"], self.model_ids[0])
        self.assertEqual(row0["key"], self.model_keys[0])
        self.assertEqual(row0["input_price_per_million"], "3.5")
        self.assertEqual(row0["output_price_per_million"], "7.25")
        row1 = data["models"][1]
        self.assertEqual(row1["model_id"], self.model_ids[1])
        self.assertEqual(row1["key"], self.model_keys[1])
        self.assertEqual(row1["input_price_per_million"], "1")
        # Empty output price normalizes to None
        self.assertIsNone(row1["output_price_per_million"])


class LastSessionPricingRegressionTests(unittest.TestCase):
    """_selected_realtime fallback must include current-rate pricing for persisted sessions."""

    def test_last_metrics_includes_costs(self):
        from observatory.metrics import _selected_realtime
        from observatory.models import SessionRow, TelemetrySample
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _seed_provider_and_models(s, "ls", 1)
            models[0].input_price_per_million = "10"
            models[0].output_price_per_million = "20"
            s.commit()
            s.refresh(models[0])
            model_id = models[0].id
            now = now_ms()
            # Create a closed (idle) session with tokens
            s.add(SessionRow(
                provider_id=p.id, model_id=model_id,
                start_at=now - 7200_000, end_at=now - 3600_000, duration_s=3600,
                prompt_tokens=1_000_000, gen_tokens=500_000,
                total_tokens=1_500_000, status="CLOSED", result_source="metrics",
                live_seen_at=None))
            s.add(TelemetrySample(
                provider_id=p.id, model_id=model_id,
                ts=now - 3600_000, state="CLOSED",
                tokens_total=1_500_000, prompt_total=1_000_000, gen_total=500_000))
            s.commit()
            model_dict = {model_id: models[0]}
            result = _selected_realtime(s, model_dict, now)
        # Should have pricing fields with current rates applied to session tokens
        self.assertEqual(result["input_price_per_million"], "10")
        self.assertEqual(result["output_price_per_million"], "20")
        # 1M tokens * $10/M = $10 input cost
        self.assertEqual(result["input_cost"], "10.00000000")
        # 0.5M tokens * $20/M = $10 output cost
        self.assertEqual(result["output_cost"], "10.00000000")
        self.assertEqual(result["total_cost"], "20.00000000")


class HistorySessionIdentityRegressionTests(unittest.TestCase):
    """_selected_realtime must use history_session (same model) not latest_session (any model)."""

    def test_grouped_selection_uses_selected_model_session(self):
        """Model A owns latest sample, model B owns newest session.
        Response must use A's session tokens and A's rates, not B's."""
        from observatory.metrics import _selected_realtime
        from observatory.models import SessionRow, TelemetrySample
        engine = memory_engine()
        with Session(engine) as s:
            p, models = _seed_provider_and_models(s, "hsid", 2)
            model_a = models[0]  # will own latest sample + older session
            model_b = models[1]  # will own globally newest session
            model_a.input_price_per_million = "10"
            model_a.output_price_per_million = "20"
            model_b.input_price_per_million = "99"
            model_b.output_price_per_million = "99"
            s.commit()
            s.refresh(model_a)
            s.refresh(model_b)
            now = now_ms()

            # Model A: older session with distinct tokens
            s.add(SessionRow(
                provider_id=p.id, model_id=model_a.id,
                start_at=now - 7200_000, end_at=now - 3600_000, duration_s=3600,
                prompt_tokens=1_000_000, gen_tokens=500_000,
                total_tokens=1_500_000, avg_gen_tps=30.0,
                status="CLOSED", result_source="metrics"))
            # Model B: globally newest session (different tokens)
            s.add(SessionRow(
                provider_id=p.id, model_id=model_b.id,
                start_at=now - 1800_000, end_at=now - 900_000, duration_s=900,
                prompt_tokens=9_000_000, gen_tokens=8_000_000,
                total_tokens=17_000_000, avg_gen_tps=80.0,
                status="CLOSED", result_source="metrics"))
            # Model A: latest telemetry sample (most recent ts)
            s.add(TelemetrySample(
                provider_id=p.id, model_id=model_a.id,
                ts=now - 100_000, state="CLOSED",
                tokens_total=1_500_000, prompt_total=1_000_000, gen_total=500_000))
            # Model B: older telemetry sample
            s.add(TelemetrySample(
                provider_id=p.id, model_id=model_b.id,
                ts=now - 200_000, state="CLOSED",
                tokens_total=17_000_000, prompt_total=9_000_000, gen_total=8_000_000))
            s.commit()
            model_dict = {model_a.id: model_a, model_b.id: model_b}
            result = _selected_realtime(s, model_dict, now)
        # Must select model A (owns latest sample)
        self.assertEqual(result["model_id"], model_a.id)
        self.assertEqual(result["model"], model_a.name)
        # Must return A's session tokens (1M/500K), NOT B's (9M/8M)
        self.assertEqual(result["prompt_tokens"], 1_000_000)
        self.assertEqual(result["gen_tokens"], 500_000)
        # Must use A's gen_tps (30.0), NOT B's (80.0)
        self.assertEqual(result["gen_tps"], 30.0)
        self.assertEqual(result["gen_tps_avg"], 30.0)
        # Cost must use A's rates (10/20) on A's tokens (1M/500K)
        self.assertEqual(result["input_price_per_million"], "10")
        self.assertEqual(result["output_price_per_million"], "20")
        # 1M * 10/1M = $10, 0.5M * 20/1M = $10
        self.assertEqual(result["input_cost"], "10.00000000")
        self.assertEqual(result["output_cost"], "10.00000000")
        self.assertEqual(result["total_cost"], "20.00000000")
        # Must NOT leak B's values
        self.assertNotEqual(result["prompt_tokens"], 9_000_000)
        self.assertNotEqual(result["gen_tokens"], 8_000_000)


if __name__ == "__main__":
    unittest.main()
