"""G09 issue #24: FX adapter unit tests (observatory/fx.py).

Step 1 of the build order -- proves observatory/fx.py is correct and float-free
before anything depends on it. Every HTTP interaction is mocked; this suite
never calls Frankfurter.
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import fx
from observatory.models import Setting


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def client_for(handler) -> httpx.Client:
    """httpx.Client whose requests are served by `handler(request) -> Response`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_client(payload: dict, status: int = 200) -> httpx.Client:
    def handler(request):
        assert request.url.path.endswith("/v1/latest")
        assert request.url.params["base"] == "USD"
        return httpx.Response(status, text=json.dumps(payload))
    return client_for(handler)


def frankfurter_payload(code="BRL", rate="5.1114", date="2026-09-04",
                        base="USD", amount=1.0):
    body = {"amount": amount, "base": base, "date": date,
            "rates": {code: rate}}
    return body


# --------------------------------------------------------------------------- #
# fetch_rate: happy path and the float-free guarantee
# --------------------------------------------------------------------------- #
class FetchRateHappyPath(unittest.TestCase):
    def test_parses_rate_to_exact_decimal_string(self):
        # 5.1114 as a JSON number: the bug this guards against is
        # Decimal(5.1114) == 5.11139999999999972...
        payload = json.loads('{"amount":1.0,"base":"USD","date":"2026-09-04",'
                             '"rates":{"BRL":5.1114}}')
        out = fx.fetch_rate("BRL", client=json_client(payload))
        self.assertEqual(out["rate"], "5.11140000")
        self.assertEqual(out["currency"], "BRL")
        self.assertEqual(out["source"], "frankfurter")
        self.assertEqual(out["quote_date"], "2026-09-04")

    def test_rate_reaches_normalize_as_decimal_not_float(self):
        # Structural proof that parse_float=Decimal took effect: the value
        # handed to _normalize_rate must never be a float.
        seen = {}
        real = fx._normalize_rate

        def spy(raw):
            seen["type"] = type(raw)
            return real(raw)

        fx._normalize_rate = spy
        try:
            payload = json.loads('{"amount":1.0,"base":"USD","date":"2026-09-04",'
                                 '"rates":{"BRL":5.1114}}')
            fx.fetch_rate("BRL", client=json_client(payload))
        finally:
            fx._normalize_rate = real
        self.assertIs(seen["type"], Decimal)
        self.assertNotIsInstance(seen["type"], float)

    def test_integer_rate_is_accepted(self):
        payload = json.loads('{"amount":1,"base":"USD","date":"2026-09-04",'
                             '"rates":{"JPY":150}}')
        out = fx.fetch_rate("JPY", client=json_client(payload))
        self.assertEqual(out["rate"], "150.00000000")

    def test_missing_date_degrades_to_none(self):
        payload = {"amount": 1.0, "base": "USD", "rates": {"BRL": "5.1114"}}
        out = fx.fetch_rate("BRL", client=json_client(payload))
        self.assertIsNone(out["quote_date"])

    def test_malformed_date_degrades_to_none(self):
        payload = frankfurter_payload(date=12345)
        out = fx.fetch_rate("BRL", client=json_client(payload))
        self.assertIsNone(out["quote_date"])

    def test_amount_absent_is_tolerated(self):
        payload = {"base": "USD", "date": "2026-09-04", "rates": {"BRL": "5.1114"}}
        out = fx.fetch_rate("BRL", client=json_client(payload))
        self.assertEqual(out["rate"], "5.11140000")


# --------------------------------------------------------------------------- #
# fetch_rate: rejection paths
# --------------------------------------------------------------------------- #
class FetchRateRejections(unittest.TestCase):
    def _reject(self, payload, code="BRL", status=200):
        with self.assertRaises(fx.FxError):
            fx.fetch_rate(code, client=json_client(payload, status=status))

    def test_rejects_zero_rate(self):
        self._reject(frankfurter_payload(rate="0"))

    def test_rejects_zero_float_rate(self):
        self._reject(frankfurter_payload(rate=0.0))

    def test_rejects_negative_rate(self):
        self._reject(frankfurter_payload(rate="-5.1114"))

    def test_rejects_nan_rate(self):
        self._reject(frankfurter_payload(rate="NaN"))

    def test_rejects_infinity_rate(self):
        self._reject(frankfurter_payload(rate="Infinity"))

    def test_rejects_rate_above_ceiling(self):
        self._reject(frankfurter_payload(rate="1000001"))

    def test_rejects_rate_below_floor(self):
        self._reject(frankfurter_payload(rate="0.0000009"))

    def test_rejects_non_200(self):
        self._reject(frankfurter_payload(), status=503)

    def test_rejects_wrong_base(self):
        self._reject(frankfurter_payload(base="EUR"))

    def test_rejects_wrong_amount(self):
        self._reject(frankfurter_payload(amount=100.0))

    def test_rejects_wrong_pair(self):
        # asked for BRL, got a EUR quote
        self._reject({"amount": 1.0, "base": "USD", "date": "2026-09-04",
                      "rates": {"EUR": "0.92"}})

    def test_rejects_empty_rates(self):
        self._reject({"amount": 1.0, "base": "USD", "date": "2026-09-04",
                      "rates": {}})

    def test_rejects_unparseable_body(self):
        client = client_for(lambda r: httpx.Response(200, text="not json {"))
        with self.assertRaises(fx.FxError):
            fx.fetch_rate("BRL", client=client)

    def test_rejects_non_object_body(self):
        client = client_for(lambda r: httpx.Response(200, text="[1, 2, 3]"))
        with self.assertRaises(fx.FxError):
            fx.fetch_rate("BRL", client=client)

    def test_transport_error_becomes_fxerror(self):
        def boom(request):
            raise httpx.ConnectError("no route to host")
        with self.assertRaises(fx.FxError):
            fx.fetch_rate("BRL", client=client_for(boom))

    def test_timeout_becomes_fxerror(self):
        def slow(request):
            raise httpx.ReadTimeout("timed out")
        with self.assertRaises(fx.FxError):
            fx.fetch_rate("BRL", client=client_for(slow))

    def test_unsupported_currency_rejected_before_any_call(self):
        called = []

        def handler(request):
            called.append(request.url)
            return httpx.Response(200, text="{}")

        with self.assertRaises(fx.FxError):
            fx.fetch_rate("XYZ", client=client_for(handler))
        self.assertEqual(called, [])


# --------------------------------------------------------------------------- #
# SUPPORTED table
# --------------------------------------------------------------------------- #
class SupportedTable(unittest.TestCase):
    def test_fifteen_currencies_with_brl_present(self):
        self.assertEqual(len(fx.SUPPORTED), 15)
        self.assertIn("BRL", fx.SUPPORTED)

    def test_zero_decimal_currencies_flagged(self):
        self.assertEqual(fx.SUPPORTED["JPY"]["decimals"], 0)
        self.assertEqual(fx.SUPPORTED["KRW"]["decimals"], 0)
        self.assertEqual(fx.SUPPORTED["BRL"]["decimals"], 2)

    def test_every_entry_has_symbol_and_decimals(self):
        for code, meta in fx.SUPPORTED.items():
            self.assertTrue(meta["symbol"], code)
            self.assertIn(meta["decimals"], (0, 2), code)


# --------------------------------------------------------------------------- #
# refresh_quote: the gate and the failure policy
# --------------------------------------------------------------------------- #
class RefreshQuoteGate(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()

    def _session(self):
        return Session(self.engine)

    def test_first_fetch_writes_all_keys(self):
        with self._session() as s:
            out = fx.refresh_quote(s, "BRL",
                                   client=json_client(frankfurter_payload()),
                                   now=1_000_000)
            self.assertEqual(out["rate"], "5.11140000")
            self.assertEqual(out["fetched_at"], 1_000_000)
        with self._session() as s:
            stored = fx.stored_quote(s)
            self.assertEqual(stored["currency"], "BRL")
            self.assertEqual(stored["rate"], "5.11140000")
            self.assertEqual(stored["source"], "frankfurter")
            self.assertEqual(stored["quote_date"], "2026-09-04")
            self.assertEqual(stored["fetched_at"], 1_000_000)

    def test_second_call_within_a_day_makes_no_request(self):
        with self._session() as s:
            fx.refresh_quote(s, "BRL", client=json_client(frankfurter_payload()),
                             now=1_000_000)

        def boom(request):
            raise AssertionError("must not hit the network inside the day gate")

        with self._session() as s:
            out = fx.refresh_quote(s, "BRL", client=client_for(boom),
                                   now=1_000_000 + 3600)
            self.assertIsNone(out)

    def test_force_bypasses_the_day_gate(self):
        with self._session() as s:
            fx.refresh_quote(s, "BRL", client=json_client(frankfurter_payload()),
                             now=1_000_000)
        with self._session() as s:
            out = fx.refresh_quote(
                s, "BRL", force=True,
                client=json_client(frankfurter_payload(rate="5.2000")),
                now=1_000_000 + 60)
            self.assertEqual(out["rate"], "5.20000000")

    def test_currency_change_bypasses_the_day_gate(self):
        with self._session() as s:
            fx.refresh_quote(s, "BRL", client=json_client(frankfurter_payload()),
                             now=1_000_000)
        with self._session() as s:
            out = fx.refresh_quote(
                s, "EUR",
                client=json_client(frankfurter_payload(code="EUR", rate="0.92")),
                now=1_000_000 + 60)
            self.assertEqual(out["currency"], "EUR")
            self.assertEqual(out["rate"], "0.92000000")

    def test_stale_rate_after_24h_refetches(self):
        with self._session() as s:
            fx.refresh_quote(s, "BRL", client=json_client(frankfurter_payload()),
                             now=1_000_000)
        with self._session() as s:
            out = fx.refresh_quote(
                s, "BRL",
                client=json_client(frankfurter_payload(rate="5.3000")),
                now=1_000_000 + fx.MIN_AGE_S)
            self.assertEqual(out["rate"], "5.30000000")

    def test_failed_refresh_leaves_previous_rate_untouched(self):
        with self._session() as s:
            fx.refresh_quote(s, "BRL", client=json_client(frankfurter_payload()),
                             now=1_000_000)
        with self._session() as s:
            with self.assertRaises(fx.FxError):
                fx.refresh_quote(s, "EUR",
                                 client=json_client(frankfurter_payload(), status=503),
                                 now=1_000_000 + 60)
        with self._session() as s:
            stored = fx.stored_quote(s)
            self.assertEqual(stored["currency"], "BRL")
            self.assertEqual(stored["rate"], "5.11140000")
            self.assertEqual(stored["fetched_at"], 1_000_000)

    def test_stored_quote_is_none_before_any_fetch(self):
        with self._session() as s:
            self.assertIsNone(fx.stored_quote(s))

    def test_refresh_rejects_unsupported_currency(self):
        with self._session() as s:
            with self.assertRaises(fx.FxError):
                fx.refresh_quote(s, "XYZ", now=1_000_000)


# --------------------------------------------------------------------------- #
# _normalize_rate directly
# --------------------------------------------------------------------------- #
class NormalizeRate(unittest.TestCase):
    def test_rounds_to_eight_places(self):
        self.assertEqual(fx._normalize_rate(Decimal("5.111400009")), "5.11140001")

    def test_pads_to_eight_places(self):
        self.assertEqual(fx._normalize_rate(Decimal("5")), "5.00000000")

    def test_accepts_boundary_values(self):
        self.assertEqual(fx._normalize_rate(Decimal("0.000001")), "0.00000100")
        self.assertEqual(fx._normalize_rate(Decimal("1000000")), "1000000.00000000")

    def test_rejects_zero(self):
        with self.assertRaises(fx.FxError):
            fx._normalize_rate(Decimal("0"))


if __name__ == "__main__":
    unittest.main()
