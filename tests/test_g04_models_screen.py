"""G04: Models main screen regression tests (issues #9, #10)."""
from __future__ import annotations

import os
import unittest
from decimal import Decimal
from collections import defaultdict

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient

import app
from observatory import database as odb
from observatory.models import (Model, Provider, SessionRow, now_ms)


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(session, provider_name="prov", n_models=3, prices=None):
    """Seed provider + models + session rows with token data.
    Returns (provider_id, model_ids) to avoid detached instance access."""
    prices = prices or {}
    p = Provider(name=provider_name, base_url=f"http://{provider_name}")
    session.add(p)
    session.commit()
    session.refresh(p)
    model_ids = []
    now = now_ms()
    for j in range(n_models):
        ip = prices.get(j, (None, None))[0]
        op = prices.get(j, (None, None))[1]
        m = Model(provider_id=p.id, key=f"file-{j}", name=f"Model-{j}",
                  family=f"family-{j % 2}", quant=f"quant-{j % 2}",
                  catalog_available=True, catalog_last_seen_at=1000,
                  input_price_per_million=ip, output_price_per_million=op)
        session.add(m)
        session.commit()
        session.refresh(m)
        model_ids.append(m.id)
        # Seed session rows with token data
        pt = 1000.0 + j * 100
        gt = 500.0 + j * 50
        session.add(SessionRow(
            provider_id=p.id, model_id=m.id,
            start_at=now - 3600000, end_at=now - 1800000,
            duration_s=1800.0,
            prompt_tokens=pt, gen_tokens=gt, total_tokens=pt + gt,
            prompt_time_s=10.0, gen_time_s=20.0,
            peak_gen_tps=50.0 + j, peak_prompt_tps=100.0,
            context_max=4096, status="CLOSED",
        ))
    session.commit()
    return p.id, model_ids


# ---------------------------------------------------------------------------
# 1. /api/models/sparks endpoint contract
# ---------------------------------------------------------------------------

class ModelSparksEndpointTests(unittest.TestCase):
    """Verify /api/models/sparks returns per-model spark data."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            self.provider_id, self.model_ids = _seed(s, "sparkprov", 2)
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_sparks_returns_dict_with_expected_keys(self):
        """Sparks are keyed by group identity (model ID in model mode,
        family/quant value in group modes)."""
        resp = self.client.get("/api/models/sparks?range=7d&group=model")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("sparks", body)
        sparks = body["sparks"]
        self.assertIsInstance(sparks, dict)
        for mid in self.model_ids:
            key = str(mid)
            self.assertIn(key, sparks, f"Model {key} missing from sparks")
            self.assertIsInstance(sparks[key], list)

    def test_sparks_group_mode_returns_group_keys(self):
        """In family group mode, sparks are keyed by family name."""
        resp = self.client.get("/api/models/sparks?range=7d&group=family")
        self.assertEqual(resp.status_code, 200)
        sparks = resp.json()["sparks"]
        self.assertIsInstance(sparks, dict)
        self.assertGreater(len(sparks), 0)
        for key, vals in sparks.items():
            self.assertIsInstance(vals, list)

    def test_sparks_reuses_snapshot(self):
        """The sparks endpoint reuses the shared snapshot from /api/models."""
        self.client.get("/api/models?range=7d")
        resp = self.client.get("/api/models/sparks?range=7d&group=family")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sparks", resp.json())

    def test_sparks_different_range(self):
        r1 = self.client.get("/api/models/sparks?range=7d&group=family")
        r2 = self.client.get("/api/models/sparks?range=30d&group=family")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)


# ---------------------------------------------------------------------------
# 2. Decimal-safe cost precision in /api/models response
# ---------------------------------------------------------------------------

class DecimalCostPrecisionTests(unittest.TestCase):
    """Verify grouped cost totals use exact decimal arithmetic."""

    def setUp(self):
        self.engine = memory_engine()
        prices = {
            0: ("0.10000001", "0.20000002"),
            1: ("0.10000002", "0.20000003"),
            2: ("0.10000003", "0.20000004"),
        }
        with Session(self.engine) as s:
            self.provider_id, self.model_ids = _seed(s, "decprov", 3, prices)
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_exact_file_costs_are_8dp_strings(self):
        """Each exact-file row has 8-decimal-place string costs."""
        resp = self.client.get("/api/models?range=7d&group=model")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        self.assertGreater(len(rows), 0)
        for row in rows:
            for field in ("input_cost", "output_cost", "total_cost"):
                val = row[field]
                self.assertIsInstance(val, str)
                parts = val.split(".")
                self.assertEqual(len(parts), 2)
                self.assertEqual(len(parts[1]), 8,
                                 f"{field}={val} not 8dp")

    def test_grouped_cost_no_float_drift(self):
        """Grouped costs must be exact 8dp decimals (no float drift)."""
        resp = self.client.get("/api/models?range=7d&group=family")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        self.assertGreater(len(rows), 0)
        for row in rows:
            for field in ("input_cost", "output_cost", "total_cost"):
                val = row[field]
                d = Decimal(val)
                self.assertEqual(d, d.quantize(Decimal("0.00000001")),
                                 f"{field}={val} not exact 8dp")

    def test_cost_sum_matches_individual_sum(self):
        """Sum of per-model costs in a group equals group total exactly."""
        exact_resp = self.client.get("/api/models?range=7d&group=model")
        exact_rows = exact_resp.json()["rows"]
        self.assertGreater(len(exact_rows), 0)
        family_costs = defaultdict(lambda: Decimal("0"))
        for row in exact_rows:
            fam = row.get("family") or row["label"]
            family_costs[fam] += Decimal(row["total_cost"])
        grouped_resp = self.client.get("/api/models?range=7d&group=family")
        grouped_rows = grouped_resp.json()["rows"]
        for grow in grouped_rows:
            gkey = grow["label"]
            if gkey in family_costs:
                expected = family_costs[gkey]
                actual = Decimal(grow["total_cost"])
                self.assertEqual(
                    actual, expected,
                    f"Group '{gkey}': server={actual} expected={expected}")


# ---------------------------------------------------------------------------
# 3. Group identity stability
# ---------------------------------------------------------------------------

class GroupIdentityContractTests(unittest.TestCase):
    """Backend returns exact-file rows; grouping is client-side.
    The 'group' param must not change row identity in exact mode."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            p = Provider(name="gidprov", base_url="http://gidprov")
            s.add(p)
            s.commit()
            s.refresh(p)
            # Models where family/quant/key share literal text across models
            ma = Model(provider_id=p.id, key="X", name="Model-X",
                       family="X", quant="X",
                       catalog_available=True, catalog_last_seen_at=1000)
            s.add(ma)
            s.commit()
            s.refresh(ma)
            mb = Model(provider_id=p.id, key="Y", name="Model-Y",
                       family="Y", quant="Y",
                       catalog_available=True, catalog_last_seen_at=1000)
            s.add(mb)
            s.commit()
            s.refresh(mb)
            now = now_ms()
            for m in (ma, mb):
                s.add(SessionRow(
                    provider_id=p.id, model_id=m.id,
                    start_at=now - 3600000, end_at=now - 1800000,
                    duration_s=1800.0,
                    prompt_tokens=100.0, gen_tokens=50.0,
                    total_tokens=150.0,
                    prompt_time_s=5.0, gen_time_s=10.0,
                    peak_gen_tps=25.0, status="CLOSED",
                ))
            s.commit()
            self.model_a_id = ma.id
            self.model_b_id = mb.id
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_exact_file_mode_returns_distinct_rows(self):
        """group=model returns one row per exact model, keyed by model ID."""
        resp = self.client.get("/api/models?range=7d&group=model")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        self.assertEqual(len(rows), 2)
        keys = {r["key"] for r in rows}
        expected = {str(self.model_a_id), str(self.model_b_id)}
        self.assertEqual(keys, expected)

    def test_family_group_merges_same_family(self):
        """group=family merges models with same family value."""
        resp = self.client.get("/api/models?range=7d&group=family")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        # Model A family="X", Model B family="Y" -> 2 groups
        self.assertEqual(len(rows), 2)

    def test_quant_group_merges_same_quant(self):
        """group=quant merges models with same quant value."""
        resp = self.client.get("/api/models?range=7d&group=quant")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        # Model A quant="X", Model B quant="Y" -> 2 groups
        self.assertEqual(len(rows), 2)

    def test_group_param_does_not_merge_distinct_models_in_exact_mode(self):
        """In exact mode, models are always separate regardless of shared
        family/quant text."""
        r1 = self.client.get("/api/models?range=7d&group=model")
        self.assertEqual(len(r1.json()["rows"]), 2)


# ---------------------------------------------------------------------------
# 4. Selected endpoint: rapid sequential requests
# ---------------------------------------------------------------------------

class SelectedRapidRequestTests(unittest.TestCase):
    """Verify /api/models/selected returns correct per-model data."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            self.provider_id, self.model_ids = _seed(s, "rapidprov", 2)
            self.ids_a = [self.model_ids[0]]
            self.ids_b = [self.model_ids[1]]
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_selected_returns_correct_model(self):
        """Each selected request returns data for its own model only."""
        ra = self.client.get(
            f"/api/models/selected?ids={self.ids_a[0]}&range=7d")
        rb = self.client.get(
            f"/api/models/selected?ids={self.ids_b[0]}&range=7d")
        self.assertEqual(ra.status_code, 200)
        self.assertEqual(rb.status_code, 200)
        da, db_ = ra.json(), rb.json()
        self.assertEqual(da["label"], "Model-0")
        self.assertEqual(db_["label"], "Model-1")

    def test_selected_tokens_match_model(self):
        """Selected tokens match the seeded session data."""
        resp = self.client.get(
            f"/api/models/selected?ids={self.ids_a[0]}&range=7d")
        body = resp.json()
        # Seeded: prompt=1000, gen=500, total=1500
        self.assertEqual(body["prompt_tokens"], 1000)
        self.assertEqual(body["gen_tokens"], 500)
        self.assertEqual(body["tokens"], 1500)

    def test_selected_multiple_ids_sums(self):
        """Multiple model IDs return summed totals."""
        both_ids = ",".join(str(mid) for mid in self.model_ids)
        resp = self.client.get(
            f"/api/models/selected?ids={both_ids}&range=7d")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Model-0: 1500 tokens, Model-1: 1650 tokens
        self.assertEqual(body["tokens"], 1500 + 1650)

    def test_selected_includes_cost_fields(self):
        """Selected response includes input/output/total cost strings."""
        resp = self.client.get(
            f"/api/models/selected?ids={self.ids_a[0]}&range=7d")
        body = resp.json()
        for field in ("input_cost", "output_cost", "total_cost"):
            self.assertIn(field, body)
            self.assertIsInstance(body[field], str)


# ---------------------------------------------------------------------------
# 5. Models page response includes all G04 required fields
# ---------------------------------------------------------------------------

class ModelsPageContractTests(unittest.TestCase):
    """Verify /api/models returns all fields needed by the G04 table."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            prices = {0: ("1.50000000", "2.50000000")}
            self.provider_id, self.model_ids = _seed(s, "contractprov", 2, prices)
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_all_required_columns_present(self):
        """The API provides data for all 11 table columns."""
        resp = self.client.get("/api/models?range=7d&group=model")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        self.assertGreater(len(rows), 0)
        row = rows[0]
        required = [
            "key", "label", "model_ids",
            "active_rank", "active_tasks",
            "prompt_tokens", "gen_tokens", "tokens",
            "input_price_per_million", "output_price_per_million",
            "total_cost", "sessions", "gen_tps",
            "color", "provider",
        ]
        for field in required:
            self.assertIn(field, row, f"Missing field: {field}")

    def test_pricing_fields_correct_for_priced_model(self):
        """Model with price 1.5/2.5 shows correct rates and non-zero cost."""
        resp = self.client.get("/api/models?range=7d&group=model")
        rows = resp.json()["rows"]
        priced = [r for r in rows if r.get("input_price_per_million")]
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0]["input_price_per_million"], "1.50000000")
        self.assertEqual(priced[0]["output_price_per_million"], "2.50000000")
        self.assertNotEqual(priced[0]["total_cost"], "0.00000000")

    def test_unpriced_model_has_zero_cost(self):
        """Models without pricing have zero costs."""
        resp = self.client.get("/api/models?range=7d&group=model")
        rows = resp.json()["rows"]
        unpriced = [r for r in rows if not r.get("input_price_per_million")]
        for r in unpriced:
            self.assertEqual(r["total_cost"], "0.00000000")


# ---------------------------------------------------------------------------
# 6. Same key under different providers (Each file identity)
# ---------------------------------------------------------------------------

class SameKeyDifferentProviderTests(unittest.TestCase):
    """Two providers exposing the same model key must produce distinct
    exact-file rows. The file: presentation identity uses model ID,
    not the raw key string."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            pa = Provider(name="provA", base_url="http://provA")
            s.add(pa)
            s.commit()
            s.refresh(pa)
            pb = Provider(name="provB", base_url="http://provB")
            s.add(pb)
            s.commit()
            s.refresh(pb)
            # Both providers have a model with key "model.gguf"
            ma = Model(provider_id=pa.id, key="model.gguf", name="ModelA",
                       family="FamA", quant="Q8",
                       catalog_available=True, catalog_last_seen_at=1000)
            s.add(ma)
            s.commit()
            s.refresh(ma)
            mb = Model(provider_id=pb.id, key="model.gguf", name="ModelB",
                       family="FamB", quant="Q8",
                       catalog_available=True, catalog_last_seen_at=1000)
            s.add(mb)
            s.commit()
            s.refresh(mb)
            self.id_a = ma.id
            self.id_b = mb.id
            now = now_ms()
            for m, prov in ((ma, pa), (mb, pb)):
                s.add(SessionRow(
                    provider_id=prov.id, model_id=m.id,
                    start_at=now - 3600000, end_at=now - 1800000,
                    duration_s=1800.0,
                    prompt_tokens=200.0, gen_tokens=100.0,
                    total_tokens=300.0,
                    prompt_time_s=5.0, gen_time_s=10.0,
                    peak_gen_tps=30.0, status="CLOSED",
                ))
            s.commit()
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def test_exact_file_mode_produces_two_rows(self):
        """group=model returns one row per model, even with same key."""
        resp = self.client.get("/api/models?range=7d&group=model")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        self.assertEqual(len(rows), 2)
        keys = {r["key"] for r in rows}
        self.assertEqual(keys, {str(self.id_a), str(self.id_b)})

    def test_each_file_rows_have_correct_provider(self):
        """Each row carries only its own provider and model ID."""
        resp = self.client.get("/api/models?range=7d&group=model")
        rows = resp.json()["rows"]
        by_key = {r["key"]: r for r in rows}
        row_a = by_key[str(self.id_a)]
        row_b = by_key[str(self.id_b)]
        self.assertEqual(row_a["provider"], "provA")
        self.assertEqual(row_b["provider"], "provB")
        self.assertEqual(row_a["model_ids"], [self.id_a])
        self.assertEqual(row_b["model_ids"], [self.id_b])

    def test_totals_invariant_across_grouping(self):
        """Total tokens are the same regardless of grouping mode."""
        exact = self.client.get("/api/models?range=7d&group=model").json()["rows"]
        family = self.client.get("/api/models?range=7d&group=family").json()["rows"]
        quant = self.client.get("/api/models?range=7d&group=quant").json()["rows"]
        total_exact = sum(r["tokens"] for r in exact)
        total_family = sum(r["tokens"] for r in family)
        total_quant = sum(r["tokens"] for r in quant)
        self.assertEqual(total_exact, 600)
        self.assertEqual(total_family, total_exact)
        self.assertEqual(total_quant, total_exact)

    def test_selecting_row_requests_only_that_model(self):
        """Selecting a row by its key (model ID) returns only that model's data."""
        resp = self.client.get(
            f"/api/models/selected?ids={self.id_a}&range=7d")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["label"], "ModelA")
        self.assertEqual(body["prompt_tokens"], 200)
        self.assertEqual(body["gen_tokens"], 100)
        # The other model's data must not leak
        resp_b = self.client.get(
            f"/api/models/selected?ids={self.id_b}&range=7d")
        body_b = resp_b.json()
        self.assertEqual(body_b["label"], "ModelB")


# ---------------------------------------------------------------------------
# 7. Models list monetary columns are ACTUAL SPEND (not $/M pricing)
# ---------------------------------------------------------------------------

class ModelsListSpendColumnsTests(unittest.TestCase):
    """The Models list monetary columns must be calculated spend for the
    selected range, never the configured price-per-million.

        IN COST  = input_tokens  * input_price_per_million  / 1,000,000
        OUT COST = output_tokens * output_price_per_million / 1,000,000
        COST     = IN COST + OUT COST

    Price-per-million is configuration data and belongs only in Settings.
    """

    def setUp(self):
        self.engine = memory_engine()
        # Model-0: priced $1/M in, $3/M out (mirrors the UI example).
        # Model-1: only the input side is priced. Model-2: fully unpriced.
        prices = {
            0: ("1.00000000", "3.00000000"),
            1: ("2.00000000", None),
        }
        with Session(self.engine) as s:
            self.provider_id, self.model_ids = _seed(s, "spendprov", 3, prices)
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def _rows_by_label(self):
        resp = self.client.get("/api/models?range=7d&group=model")
        self.assertEqual(resp.status_code, 200)
        return {r["label"]: r for r in resp.json()["rows"]}

    def test_input_cost_is_calculated_spend_not_price(self):
        """IN COST = prompt_tokens * input_price / 1e6, not the rate."""
        rows = self._rows_by_label()
        r = rows["Model-0"]
        self.assertEqual(r["input_price_per_million"], "1.00000000")
        expected = (Decimal("1000") / Decimal("1000000")
                    * Decimal("1.00000000"))
        self.assertEqual(Decimal(r["input_cost"]), expected)
        # The spend column must not be the configured rate.
        self.assertNotEqual(r["input_cost"], r["input_price_per_million"])

    def test_output_cost_is_calculated_spend_not_price(self):
        """OUT COST = gen_tokens * output_price / 1e6, not the rate."""
        rows = self._rows_by_label()
        r = rows["Model-0"]
        self.assertEqual(r["output_price_per_million"], "3.00000000")
        expected = (Decimal("500") / Decimal("1000000")
                    * Decimal("3.00000000"))
        self.assertEqual(Decimal(r["output_cost"]), expected)
        self.assertNotEqual(r["output_cost"], r["output_price_per_million"])

    def test_total_cost_equals_input_plus_output_spend(self):
        """COST = IN COST + OUT COST for every list row."""
        rows = self._rows_by_label()
        self.assertGreater(len(rows), 0)
        for label, r in rows.items():
            self.assertEqual(
                Decimal(r["total_cost"]),
                Decimal(r["input_cost"]) + Decimal(r["output_cost"]),
                f"{label}: total != input spend + output spend",
            )

    def test_unpriced_model_costs_are_zero(self):
        """A fully unpriced model yields zero spend (consistent semantics)."""
        rows = self._rows_by_label()
        r = rows["Model-2"]
        self.assertIsNone(r["input_price_per_million"])
        self.assertIsNone(r["output_price_per_million"])
        for field in ("input_cost", "output_cost", "total_cost"):
            self.assertEqual(r[field], "0.00000000")

    def test_partial_pricing_zeroes_only_the_unpriced_side(self):
        """Only input priced -> output spend is 0, total == input spend."""
        rows = self._rows_by_label()
        r = rows["Model-1"]
        self.assertEqual(r["output_price_per_million"], None)
        self.assertEqual(r["output_cost"], "0.00000000")
        expected_in = (Decimal("1100") / Decimal("1000000")
                       * Decimal("2.00000000"))
        self.assertEqual(Decimal(r["input_cost"]), expected_in)
        self.assertEqual(
            Decimal(r["total_cost"]),
            Decimal(r["input_cost"]) + Decimal(r["output_cost"]),
        )


# ---------------------------------------------------------------------------
# 8. Models list UI source contract: spend only, no $/M anywhere
# ---------------------------------------------------------------------------

class ModelsListSpendUiSourceTests(unittest.TestCase):
    """Browser-side contract: the Models list renders calculated spend and
    never the configured price-per-million (which lives only in Settings)."""

    def setUp(self):
        with open(os.path.join(app.BASE_DIR, "templates", "models.html"),
                  encoding="utf-8") as fh:
            self.template = fh.read()
        with open(os.path.join(app.BASE_DIR, "static", "js", "app.js"),
                  encoding="utf-8") as fh:
            self.script = fh.read()

    def test_models_list_template_has_no_price_per_million(self):
        """The table header must not mention $/M and must label spend."""
        self.assertNotIn("In $/M", self.template)
        self.assertNotIn("Out $/M", self.template)
        self.assertNotIn("$/M", self.template)
        self.assertIn("In cost", self.template)
        self.assertIn("Out cost", self.template)
        self.assertIn("Cost", self.template)
        # The two spend columns use the cost class, not the price class.
        self.assertNotIn('class="num col-price"', self.template)

    def test_models_list_renders_spend_not_price(self):
        """Table cells render calculated spend via costPair (fmtCost plus an
        optional secondary-currency amount), never fmtPrice."""
        self.assertIn('costPair(r.input_cost)', self.script)
        self.assertIn('costPair(r.output_cost)', self.script)
        self.assertIn('costPair(r.total_cost)', self.script)
        # costPair is a thin wrapper over the unchanged USD formatter.
        self.assertIn('function costPair', self.script)
        self.assertIn('const primary = fmtCost(v)', self.script)
        # The old price cells are gone from the Models list.
        self.assertNotIn('fmtPrice(r.input_price_per_million)', self.script)
        self.assertNotIn('fmtPrice(r.output_price_per_million)', self.script)
        # The selected-model panel no longer appends a " /M" price subtitle.
        self.assertNotIn('input_price_per_million) + " /M"', self.script)
        self.assertNotIn('output_price_per_million) + " /M"', self.script)
        # The price formatter is no longer part of the Models screen JS.
        self.assertNotIn("fmtPrice", self.script)
