"""G05: Settings Model Pricing UI regression tests (issue #11).

Covers the API behavior the bulk-save UI depends on (offline/historical
listing, single atomic bulk write, blank/zero semantics, cross-provider
isolation of duplicate filenames) plus source-contract assertions for the
browser side (one bulk PUT, stale-response guarding, edit preservation on
failure, keyboard access, programmatic labels, responsive layout).
"""
from __future__ import annotations

import os
import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient

import app
from observatory import database as odb
from observatory.models import Model, Provider, now_ms

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_models(session, provider, specs):
    """specs: list of (key, available, last_seen, in_price, out_price)."""
    ids = []
    for key, available, last_seen, in_p, out_p in specs:
        m = Model(
            provider_id=provider.id, key=key, name="Model " + key,
            family="fam-" + key, quant="Q4_K_M",
            catalog_available=available, catalog_last_seen_at=last_seen,
            input_price_per_million=in_p, output_price_per_million=out_p,
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        ids.append(m.id)
    return ids


class _PricingClientBase(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            pa = Provider(name="provA", base_url="http://provA", status="LIVE",
                          enabled=True, is_default=True)
            pb = Provider(name="provB", base_url="http://provB", status="OFFLINE",
                          enabled=True, is_default=False)
            s.add(pa)
            s.add(pb)
            s.commit()
            s.refresh(pa)
            s.refresh(pb)
            self.prov_a = pa.id
            self.prov_b = pb.id
            # provA: 2 available, 2 offline/historical
            self.a_ids = _seed_models(s, pa, [
                ("file-0", True, now_ms() - 60_000, "0.50000000", None),
                ("file-1", True, now_ms() - 120_000, None, None),
                ("file-2", False, now_ms() - 86_400_000, None, "2.00000000"),
                ("file-3", False, now_ms() - 172_800_000, None, None),
            ])
            # provB: duplicate filename of provA's file-0, distinct model row
            self.b_ids = _seed_models(s, pb, [
                ("file-0", True, now_ms() - 30_000, "9.00000000", "8.00000000"),
            ])
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def _prices(self, mid):
        with Session(self.engine) as s:
            row = s.get(Model, mid)
            return row.input_price_per_million, row.output_price_per_million


class SettingsModelListingTests(_PricingClientBase):
    """The UI must list reported AND historical exact files with availability."""

    def test_get_lists_available_and_offline_models(self):
        resp = self.client.get("/api/settings/models?provider=%d" % self.prov_a)
        self.assertEqual(resp.status_code, 200)
        models = resp.json()["models"]
        self.assertEqual(len(models), 4)
        by_key = {m["key"]: m for m in models}
        for key in ("file-0", "file-1", "file-2", "file-3"):
            self.assertIn(key, by_key)
        self.assertTrue(by_key["file-0"]["catalog_available"])
        self.assertFalse(by_key["file-2"]["catalog_available"])
        self.assertIsNotNone(by_key["file-2"]["catalog_last_seen_at"])
        for m in models:
            for field in ("id", "key", "name", "family", "quant",
                          "catalog_available", "catalog_last_seen_at",
                          "input_price_per_million", "output_price_per_million"):
                self.assertIn(field, m)

    def test_get_is_scoped_to_selected_provider(self):
        ra = self.client.get("/api/settings/models?provider=%d" % self.prov_a).json()["models"]
        rb = self.client.get("/api/settings/models?provider=%d" % self.prov_b).json()["models"]
        a_ids = {m["id"] for m in ra}
        b_ids = {m["id"] for m in rb}
        self.assertEqual(len(rb), 1)
        self.assertTrue(a_ids.isdisjoint(b_ids))
        # duplicate filename, distinct identities
        self.assertEqual(ra[0]["key"], rb[0]["key"])
        self.assertNotEqual(ra[0]["id"], rb[0]["id"])

    def test_get_unknown_provider_404(self):
        resp = self.client.get("/api/settings/models?provider=99999")
        self.assertEqual(resp.status_code, 404)


class BulkSaveUiContractTests(_PricingClientBase):
    """One atomic bulk save for all dirty rows; failure writes nothing."""

    def test_bulk_save_multiple_dirty_rows_one_request(self):
        resp = self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[0],
                 "input_price_per_million": "0.15",
                 "output_price_per_million": "2"},
                {"model_id": self.a_ids[2],
                 "input_price_per_million": "",
                 "output_price_per_million": "3.5"},
            ],
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["updated"], 2)
        saved = {r["model_id"]: r for r in body["models"]}
        self.assertEqual(saved[self.a_ids[0]]["input_price_per_million"], "0.15")
        self.assertEqual(saved[self.a_ids[0]]["output_price_per_million"], "2")
        self.assertIsNone(saved[self.a_ids[2]]["input_price_per_million"])
        self.assertEqual(saved[self.a_ids[2]]["output_price_per_million"], "3.5")
        self.assertEqual(self._prices(self.a_ids[0]), ("0.15", "2"))
        self.assertEqual(self._prices(self.a_ids[2]), (None, "3.5"))
        # untouched rows keep their prices
        self.assertEqual(self._prices(self.a_ids[1]), (None, None))
        self.assertEqual(self._prices(self.a_ids[3]), (None, None))

    def test_bulk_save_blank_null_zero_explicit(self):
        self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[1],
                 "input_price_per_million": "",
                 "output_price_per_million": "0"},
            ],
        })
        self.assertEqual(self._prices(self.a_ids[1]), (None, "0"))

    def test_bulk_save_normalizes_decimal_strings(self):
        self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[1],
                 "input_price_per_million": "00.250",
                 "output_price_per_million": ".5"},
            ],
        })
        self.assertEqual(self._prices(self.a_ids[1]), ("0.250", "0.5"))

    def test_offline_historical_model_is_editable(self):
        resp = self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[3],
                 "input_price_per_million": "1",
                 "output_price_per_million": "4"},
            ],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._prices(self.a_ids[3]), ("1", "4"))

    def test_bulk_save_failure_writes_nothing(self):
        before = [self._prices(mid) for mid in self.a_ids]
        resp = self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[0],
                 "input_price_per_million": "0.25",
                 "output_price_per_million": "0.75"},
                {"model_id": self.a_ids[1],
                 "input_price_per_million": "-1",
                 "output_price_per_million": "0.5"},
                {"model_id": self.a_ids[2],
                 "input_price_per_million": "1",
                 "output_price_per_million": "1"},
            ],
        })
        self.assertEqual(resp.status_code, 422)
        after = [self._prices(mid) for mid in self.a_ids]
        self.assertEqual(before, after, "no row may change on a failed bulk save")

    def test_invalid_negative_nonfinite_rejected(self):
        for bad in ("abc", "NaN", "Infinity", "-0.001"):
            with self.subTest(value=bad):
                resp = self.client.put("/api/settings/model-pricing", json={
                    "provider_id": self.prov_a,
                    "models": [
                        {"model_id": self.a_ids[0],
                         "input_price_per_million": bad,
                         "output_price_per_million": "0.5"},
                    ],
                })
                self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._prices(self.a_ids[0]), ("0.50000000", None))

    def test_duplicate_model_id_in_request_rejected(self):
        resp = self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[0],
                 "input_price_per_million": "1",
                 "output_price_per_million": "1"},
                {"model_id": self.a_ids[0],
                 "input_price_per_million": "2",
                 "output_price_per_million": "2"},
            ],
        })
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._prices(self.a_ids[0]), ("0.50000000", None))

    def test_duplicate_filenames_across_providers_stay_isolated(self):
        self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.a_ids[0],
                 "input_price_per_million": "0.11",
                 "output_price_per_million": "0.22"},
            ],
        })
        self.assertEqual(self._prices(self.a_ids[0]), ("0.11", "0.22"))
        self.assertEqual(self._prices(self.b_ids[0]), ("9.00000000", "8.00000000"))
        self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_b,
            "models": [
                {"model_id": self.b_ids[0],
                 "input_price_per_million": "7",
                 "output_price_per_million": "6"},
            ],
        })
        self.assertEqual(self._prices(self.b_ids[0]), ("7", "6"))
        self.assertEqual(self._prices(self.a_ids[0]), ("0.11", "0.22"))

    def test_model_from_other_provider_rejected(self):
        resp = self.client.put("/api/settings/model-pricing", json={
            "provider_id": self.prov_a,
            "models": [
                {"model_id": self.b_ids[0],
                 "input_price_per_million": "1",
                 "output_price_per_million": "1"},
            ],
        })
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._prices(self.b_ids[0]), ("9.00000000", "8.00000000"))


def _read(name):
    with open(os.path.join(BASE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


class SettingsPricingUiSourceTests(unittest.TestCase):
    """Browser-side contract: one atomic bulk save, stale-safe loads,
    edit preservation on failure, keyboard access, and labels."""

    def setUp(self):
        self.template = _read("templates/settings.html")
        self.script = _read("static/js/app.js")
        self.styles = _read("static/css/app.css")

    def test_template_has_labeled_provider_selector_and_status(self):
        self.assertIn('<label for="mp_provider">Provider</label>', self.template)
        self.assertIn('id="mp_provider"', self.template)
        self.assertIn('id="mpSave"', self.template)
        self.assertIn('id="mpStatus"', self.template)
        self.assertIn('role="status"', self.template)
        self.assertIn('aria-live="polite"', self.template)
        self.assertIn('scope="col"', self.template)
        self.assertIn('id="mpBody"', self.template)

    def test_bulk_save_is_one_atomic_request(self):
        self.assertIn('fetch("/api/settings/model-pricing"', self.script)
        self.assertIn(
            'JSON.stringify({ provider_id: mp.providerId, models: rows })',
            self.script)
        # no per-row pricing writes
        self.assertNotIn('"/api/settings/model-pricing/" +', self.script)

    def test_provider_load_aborts_and_guards_stale_responses(self):
        self.assertIn('mp.abort.abort()', self.script)
        self.assertIn('new AbortController()', self.script)
        self.assertIn("signal: signal", self.script)
        # a response may render only if it is still the active request/provider
        self.assertIn("signal.aborted || seq !== mp.seq || mp.providerId !== providerId",
                      self.script)

    def test_failed_save_preserves_edits_and_allows_retry(self):
        # failure keeps user input and dirty markers, surfaces the request
        # error, and leaves the save actionable for retry
        self.assertEqual(self.script.count("all edits preserved, fix and retry"), 3)
        # a synchronous failure while building the request must not strand
        # the button in the "Saving…" state
        self.assertIn("try {\n      req = fetch(\"/api/settings/model-pricing\"",
                      self.script)
        # only the success path re-renders rows with normalized values
        self.assertIn("normalized values applied", self.script)
        success_idx = self.script.index("normalized values applied")
        self.assertLess(self.script.index("mpRenderRows();\n        mpUpdateButtons();\n        mpSetStatus(\"saved"),
                        success_idx)
        self.assertIn('btn.textContent = mp.saving ? "Saving…" : "Bulk Save";',
                      self.script)

    def test_row_errors_only_from_structured_response(self):
        # individual rows are marked only when the API response identifies them
        self.assertIn("const errs = res.data && res.data.errors;", self.script)
        self.assertIn("Array.isArray(errs)", self.script)
        self.assertIn("e.model_id != null", self.script)
        # never parse model IDs out of the free-form detail string
        self.assertNotIn("detail.match(", self.script)
        self.assertNotIn("detail.replace(", self.script)
        self.assertNotIn("parseInt(detail", self.script)

    def test_unsaved_changes_are_protected(self):
        self.assertIn('window.addEventListener("beforeunload"', self.script)
        self.assertIn("e.preventDefault()", self.script)
        self.assertIn('confirm("You have unsaved price changes', self.script)
        # provider selector is locked while a save is in flight
        self.assertIn("sel.disabled = mp.saving;", self.script)

    def test_inputs_have_programmatic_labels(self):
        self.assertIn('aria-label="Input price per million USD for', self.script)
        self.assertIn('aria-label="Output price per million USD for', self.script)
        # the label includes the exact file key so duplicate filenames across
        # providers are unambiguous to screen readers
        self.assertIn('esc(m.name) + " (" + esc(m.key) + \')', self.script)

    def test_inline_validation_states(self):
        self.assertIn("MP_DEC_RE", self.script)
        self.assertIn('inp.setAttribute("aria-invalid"', self.script)
        self.assertIn("mp-invalid", self.script)
        self.assertIn(".mp-invalid", self.styles)
        # invalid input explains itself on the row and in the live status,
        # so a disabled Bulk Save is never unexplained
        self.assertIn("input must be 0 or a non-negative number", self.script)
        self.assertIn("output must be 0 or a non-negative number", self.script)
        self.assertIn("with an invalid price", self.script)

    def test_disabled_bulk_save_never_uses_busy_cursor(self):
        # A normally-disabled button must not show the "busy/wait" cursor
        # (that cursor was being mistaken for a save in flight). Ordinary
        # disabled is not-allowed; only the in-flight is-saving state is wait.
        self.assertIn(".btn:disabled { cursor: not-allowed", self.styles)
        self.assertIn(".btn.is-saving:disabled { cursor: wait; }", self.styles)
        # the save button opts into is-saving only while a save is running
        self.assertIn('btn.classList.toggle("is-saving", mp.saving);', self.script)

    def test_bulk_save_explains_why_disabled(self):
        # The button tooltip states the reason it is disabled so a user is
        # never left guessing between "nothing to save", "invalid price" and
        # "still saving".
        self.assertIn('!dirtyN ? "No changes to save"', self.script)
        self.assertIn('invalidN > 0 ? "Fix invalid price"', self.script)

    def test_price_inputs_disabled_during_inflight_save(self):
        # Race guard: while a bulk save is in flight the price inputs are
        # disabled, so a newer edit typed after the click cannot be silently
        # overwritten by the returning (older) response, which re-renders every
        # row from the just-saved originals. The same mp.saving flag that locks
        # the save button and provider select must drive the input lock.
        self.assertIn("inp.disabled = mp.saving", self.script)
        # exactly one site binds the inputs to the saving flag (the shared
        # button/row updater), so load, save and failure paths all stay in sync
        self.assertEqual(self.script.count("inp.disabled = mp.saving"), 1)
        self.assertIn("btn.disabled = !dirtyN || invalidN > 0 || mp.saving", self.script)
        self.assertIn("sel.disabled = mp.saving", self.script)

    def test_comma_decimal_is_accepted_for_pt_br(self):
        # A single comma with no dot is treated as the decimal separator
        # ("0,10" -> "0.10"); ambiguous input (both separators or several
        # commas) is left as-is so the decimal regex rejects it fail-closed.
        self.assertIn('s.replace(",", ".");', self.script)
        self.assertIn('s.indexOf(",") >= 0 && s.indexOf(".") < 0', self.script)
        # the accepted format is surfaced in the UI, not just enforced
        self.assertIn("0,5 also works", self.script)
        self.assertIn("decimal point or comma", self.template)

    def test_price_fields_show_usd_and_accept_currency_markers(self):
        # every price input carries a visible US$ prefix (input, output, and the
        # two G09 cache columns -- the latter share one templated cell)
        self.assertIn('<span class="mp-cur">US$</span>', self.script)
        self.assertGreaterEqual(
            self.script.count('<span class="mp-cur">US$</span>'), 2)
        self.assertIn(".mp-cur", self.styles)
        # a typed US$/ $ prefix is stripped before validation, not rejected
        self.assertIn("MP_CUR_RE", self.script)
        self.assertIn("mpClean(inp.value)", self.script)

    def test_keyboard_operable_native_controls(self):
        self.assertIn('<button class="btn primary" id="mpSave" disabled>Bulk Save</button>',
                      self.template)
        self.assertIn('<select id="mp_provider"', self.template)
        self.assertIn('inputmode="decimal"', self.script)

    def test_responsive_pricing_table(self):
        self.assertIn(".mp-scroll { overflow-x: auto; }", self.styles)
        self.assertIn(".mp-table { min-width: 560px; }", self.styles)
        self.assertIn(".mp-row.mp-dirty td:first-child { box-shadow: inset 2px 0 0 var(--amber); }",
                      self.styles)


if __name__ == "__main__":
    unittest.main()
