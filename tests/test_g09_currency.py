"""G09 issue #24: secondary-currency plumbing (steps 2-6).

Covers the API contract the dual-currency UI depends on: the /api/meta guard,
the three /api/settings/currency endpoints, the once-per-day gate, the stale
guard, and the "conversion is display-only" invariant. Every FX fetch is
patched; this suite never calls Frankfurter.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient

import app
from observatory import database as odb, fx
from observatory.models import Model, Provider, Setting, SessionRow, now_ms


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class _FakeFetch:
    """Stand-in for fx.fetch_rate: counts calls, returns a canned block or
    raises FxError."""

    def __init__(self, rate="5.11140000", quote_date="2026-09-04", fail=False):
        self.rate = rate
        self.quote_date = quote_date
        self.fail = fail
        self.calls = []

    def __call__(self, code, *, client=None, timeout=None):
        self.calls.append((code, timeout))
        if self.fail:
            raise fx.FxError("simulated fetch failure")
        return {"currency": code, "rate": self.rate, "source": "frankfurter",
                "quote_date": self.quote_date}


class _CurrencyClientBase(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            p = Provider(name="provA", base_url="http://provA", status="LIVE",
                         enabled=True, is_default=True)
            s.add(p)
            s.commit()
            s.refresh(p)
            self.prov_id = p.id
            mdl = Model(provider_id=p.id, key="file-0", name="Model file-0",
                        family="fam", quant="Q4_K_M", catalog_available=True,
                        catalog_last_seen_at=now_ms(),
                        input_price_per_million="0.50000000",
                        output_price_per_million="2.00000000")
            s.add(mdl)
            s.commit()
            s.refresh(mdl)
            self.model_id = mdl.id
            sess = SessionRow(provider_id=p.id, model_id=mdl.id,
                              start_at=now_ms() - 60_000, end_at=now_ms(),
                              prompt_tokens=1234.0, gen_tokens=5678.0,
                              total_tokens=6912.0, status="CLOSED")
            s.add(sess)
            s.commit()
            s.refresh(sess)
            self.sess_id = sess.id
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def _setting(self, key):
        with Session(self.engine) as s:
            row = s.get(Setting, key)
            return row.value if row else None


class MetaGuard(_CurrencyClientBase):
    def test_meta_currency_null_when_off(self):
        meta = self.client.get("/api/meta").json()
        self.assertIn("currency", meta)
        self.assertIsNone(meta["currency"])

    def test_meta_currency_null_when_selected_but_no_rate(self):
        fake = _FakeFetch(fail=True)
        with patch.object(fx, "fetch_rate", fake):
            r = self.client.put("/api/settings/currency", json={"code": "BRL"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["currency"]["code"], "BRL")
        self.assertFalse(r.json()["currency"]["enabled"])
        self.assertIsNone(self.client.get("/api/meta").json()["currency"])

    def test_meta_currency_block_when_rate_live(self):
        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
        cur = self.client.get("/api/meta").json()["currency"]
        self.assertTrue(cur["enabled"])
        self.assertEqual(cur["code"], "BRL")
        self.assertEqual(cur["rate"], "5.11140000")
        self.assertEqual(cur["symbol"], "R$")
        self.assertEqual(cur["decimals"], 2)


class CurrencyEndpoints(_CurrencyClientBase):
    def test_get_lists_fifteen_options_with_brl(self):
        body = self.client.get("/api/settings/currency").json()
        self.assertEqual(len(body["options"]), 15)
        codes = [o["code"] for o in body["options"]]
        self.assertIn("BRL", codes)
        self.assertEqual(body["currency"]["code"], None)
        self.assertFalse(body["currency"]["enabled"])

    def test_put_unsupported_code_400(self):
        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            r = self.client.put("/api/settings/currency", json={"code": "XYZ"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(fake.calls, [])

    def test_put_null_disables_and_makes_no_fetch(self):
        ok = _FakeFetch()
        with patch.object(fx, "fetch_rate", ok):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
        after = _FakeFetch()
        with patch.object(fx, "fetch_rate", after):
            r = self.client.put("/api/settings/currency", json={"code": None})
        self.assertEqual(after.calls, [])
        self.assertFalse(r.json()["currency"]["enabled"])
        self.assertIsNone(r.json()["currency"]["code"])
        self.assertIsNone(self.client.get("/api/meta").json()["currency"])

    def test_put_uses_five_second_timeout(self):
        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
        self.assertEqual(fake.calls, [("BRL", 5.0)])

    def test_currency_change_forces_a_fetch(self):
        first = _FakeFetch(rate="5.11140000")
        with patch.object(fx, "fetch_rate", first):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
        second = _FakeFetch(rate="0.92000000")
        with patch.object(fx, "fetch_rate", second):
            r = self.client.put("/api/settings/currency", json={"code": "EUR"})
        self.assertEqual([c[0] for c in second.calls], ["EUR"])
        self.assertEqual(r.json()["currency"]["code"], "EUR")
        self.assertEqual(r.json()["currency"]["rate"], "0.92000000")


class StaleGuard(_CurrencyClientBase):
    def test_failed_currency_change_retains_previous_rate_but_hides_it(self):
        good = _FakeFetch(rate="5.11140000")
        with patch.object(fx, "fetch_rate", good):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
        bad = _FakeFetch(fail=True)
        with patch.object(fx, "fetch_rate", bad):
            r = self.client.put("/api/settings/currency", json={"code": "EUR"})
        # The EUR selection took effect, but with no EUR rate it is not shown.
        self.assertEqual(r.json()["currency"]["code"], "EUR")
        self.assertFalse(r.json()["currency"]["enabled"])
        self.assertIsNotNone(r.json()["error"])
        self.assertIsNone(self.client.get("/api/meta").json()["currency"])
        # The BRL rate is retained in the setting table, never rendered as EUR.
        with Session(self.engine) as s:
            stored = fx.stored_quote(s)
        self.assertEqual(stored["currency"], "BRL")
        self.assertEqual(stored["rate"], "5.11140000")


class ManualRefresh(_CurrencyClientBase):
    def test_refresh_rejected_within_60s_without_calling_out(self):
        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
            r = self.client.post("/api/settings/currency/refresh")
        self.assertEqual(r.json()["error"], "too soon")
        self.assertEqual(len(fake.calls), 1)  # only the PUT called out

    def test_refresh_bypasses_the_day_gate(self):
        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
        # Age the stored fetch past the 60s manual guard but well within 24h.
        with Session(self.engine) as s:
            row = s.get(Setting, "fx_fetched_at")
            row.value = str(int(now_ms() / 1000) - 3600)
            s.add(row)
            s.commit()
        again = _FakeFetch(rate="5.20000000")
        with patch.object(fx, "fetch_rate", again):
            r = self.client.post("/api/settings/currency/refresh")
        self.assertEqual([c[0] for c in again.calls], ["BRL"])
        self.assertEqual(r.json()["currency"]["rate"], "5.20000000")

    def test_refresh_with_no_currency_selected(self):
        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            r = self.client.post("/api/settings/currency/refresh")
        self.assertEqual(fake.calls, [])
        self.assertEqual(r.json()["error"], "no currency selected")


class DisplayOnly(_CurrencyClientBase):
    def test_setting_a_currency_never_rewrites_stored_figures(self):
        with Session(self.engine) as s:
            m0 = s.get(Model, self.model_id)
            before_prices = (m0.input_price_per_million, m0.output_price_per_million)
            sess0 = s.get(SessionRow, self.sess_id)
            before_tokens = (sess0.prompt_tokens, sess0.gen_tokens, sess0.total_tokens)

        fake = _FakeFetch()
        with patch.object(fx, "fetch_rate", fake):
            self.client.put("/api/settings/currency", json={"code": "BRL"})
            self.client.post("/api/settings/currency/refresh")

        with Session(self.engine) as s:
            m1 = s.get(Model, self.model_id)
            self.assertEqual(
                (m1.input_price_per_million, m1.output_price_per_million),
                before_prices)
            sess1 = s.get(SessionRow, self.sess_id)
            self.assertEqual(
                (sess1.prompt_tokens, sess1.gen_tokens, sess1.total_tokens),
                before_tokens)


class NoMigration(_CurrencyClientBase):
    def test_no_fx_table_fx_rides_on_the_setting_table(self):
        # #24 (FX) added no schema of its own. It shipped at SCHEMA_VERSION 14;
        # later features (e.g. #23 automatic pricing at 15) may raise it, but
        # the FX rate must still live in `setting`, not a table of its own.
        import sqlalchemy as sa
        self.assertGreaterEqual(odb.SCHEMA_VERSION, 14)
        with self.engine.connect() as conn:
            tables = set(sa.inspect(conn).get_table_names())
        self.assertFalse(any(t.startswith("fx") for t in tables),
                         "FX must ride on the setting table, not a new one")
        self.assertIn("setting", tables)


if __name__ == "__main__":
    unittest.main()
