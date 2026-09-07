"""Secondary-currency FX adapter (G09, issue #24).

USD stays authoritative everywhere. This module fetches one latest USD->XXX
rate from Frankfurter (ECB-backed, free, no key) and stores it as a canonical
fixed-point decimal string. There is no history: one latest rate is applied to
every displayed figure, client-side.

No FastAPI imports live here. ``fetch_rate`` never touches the database, so it
unit-tests against a fake ``httpx`` transport; ``refresh_quote`` takes a session
and only ever writes the ``setting`` key/value rows.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation

import httpx

SOURCE = "frankfurter"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
TIMEOUT_S = 10.0

# Once-per-day gate. ``refresh_quote`` compares this against the persisted
# ``fx_fetched_at`` so the limit survives process restarts.
MIN_AGE_S = 86_400

# A plausible-rate window. Rejecting zero here (``pricing.validate_price``
# accepts it) matters: a zero rate would render every price as ``R$0.00``.
_RATE_MIN = Decimal("0.000001")
_RATE_MAX = Decimal("1000000")
_EIGHT_DP = Decimal("0.00000001")

# The 15 supported secondary currencies: display symbol, decimal places and a
# human name for the Settings dropdown. JPY and KRW are zero-decimal currencies
# and are flagged as such here so the UI never shows a bogus fractional yen.
SUPPORTED: dict[str, dict] = {
    "BRL": {"symbol": "R$", "decimals": 2, "name": "Brazilian real"},
    "EUR": {"symbol": "€", "decimals": 2, "name": "Euro"},
    "GBP": {"symbol": "£", "decimals": 2, "name": "British pound"},
    "JPY": {"symbol": "¥", "decimals": 0, "name": "Japanese yen"},
    "CNY": {"symbol": "CN¥", "decimals": 2, "name": "Chinese yuan"},
    "CAD": {"symbol": "C$", "decimals": 2, "name": "Canadian dollar"},
    "AUD": {"symbol": "A$", "decimals": 2, "name": "Australian dollar"},
    "CHF": {"symbol": "CHF", "decimals": 2, "name": "Swiss franc"},
    "INR": {"symbol": "₹", "decimals": 2, "name": "Indian rupee"},
    "MXN": {"symbol": "MX$", "decimals": 2, "name": "Mexican peso"},
    "KRW": {"symbol": "₩", "decimals": 0, "name": "South Korean won"},
    "SEK": {"symbol": "kr", "decimals": 2, "name": "Swedish krona"},
    "NOK": {"symbol": "kr", "decimals": 2, "name": "Norwegian krone"},
    "PLN": {"symbol": "zł", "decimals": 2, "name": "Polish złoty"},
    "ZAR": {"symbol": "R", "decimals": 2, "name": "South African rand"},
}


class FxError(Exception):
    """Raised when an FX rate cannot be fetched or is not usable.

    Sibling to ``pricing.PricingValidationError``: any instance means the
    caller must keep whatever rate it already had.
    """


def _normalize_rate(raw) -> str:
    """Validate a rate and return it as an 8-dp canonical decimal string.

    ``raw`` is whatever ``json.loads(..., parse_float=Decimal)`` produced for
    the rate field -- normally a ``Decimal`` already, sometimes an ``int``.
    Raises :class:`FxError` for anything unusable.
    """
    try:
        d = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise FxError(f"unparseable rate: {raw!r}")
    if d.is_nan() or d.is_infinite():
        raise FxError(f"non-finite rate: {raw!r}")
    if d <= 0:
        raise FxError(f"non-positive rate: {raw!r}")
    if d < _RATE_MIN or d > _RATE_MAX:
        raise FxError(f"rate out of range: {raw!r}")
    return format(d.quantize(_EIGHT_DP), "f")


def fetch_rate(code: str, *, client: httpx.Client | None = None,
               timeout: float | None = None) -> dict:
    """Fetch the latest USD->``code`` rate from Frankfurter.

    Returns ``{"currency", "rate", "source", "quote_date"}`` where ``rate`` is
    an 8-dp decimal string and ``quote_date`` is the ECB quote date (weekdays
    only) or ``None`` if the payload omitted or mangled it. Raises
    :class:`FxError` on any transport, HTTP, parse or validation failure.

    Pass ``client`` (e.g. one built on ``httpx.MockTransport``) to test without
    network access; otherwise a short-lived client is used, at ``timeout``
    seconds or :data:`TIMEOUT_S` if unset.
    """
    if code not in SUPPORTED:
        raise FxError(f"unsupported currency: {code!r}")

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout or TIMEOUT_S)
    try:
        resp = client.get(FRANKFURTER_URL, params={"base": "USD", "symbols": code})
    except httpx.HTTPError as exc:
        raise FxError(f"transport error: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise FxError(f"HTTP {resp.status_code} from {SOURCE}")

    # NOT resp.json(): stdlib json turns 5.1114 into a binary float, and
    # Decimal(5.1114) is then 5.11139999999999972... . parse_float=Decimal
    # keeps the rate exact from the first moment it exists. This is the single
    # most important line in the feature.
    try:
        body = json.loads(resp.text, parse_float=Decimal)
    except (ValueError, TypeError) as exc:
        raise FxError(f"unparseable body: {exc}") from exc

    if not isinstance(body, dict):
        raise FxError("response body is not an object")
    if body.get("base") != "USD":
        raise FxError(f"unexpected base: {body.get('base')!r}")

    amount = body.get("amount")
    try:
        if amount is not None and Decimal(amount) != Decimal("1"):
            raise FxError(f"unexpected amount: {amount!r}")
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FxError(f"unparseable amount: {amount!r}") from exc

    rates = body.get("rates")
    if not isinstance(rates, dict) or code not in rates:
        raise FxError(f"rate for {code} missing from response")

    rate = _normalize_rate(rates[code])

    # The rate is the payload; the date is garnish. A missing or malformed
    # date degrades to None rather than failing the fetch.
    quote_date = body.get("date")
    if not isinstance(quote_date, str) or not quote_date.strip():
        quote_date = None

    return {
        "currency": code,
        "rate": rate,
        "source": SOURCE,
        "quote_date": quote_date,
    }


# --------------------------------------------------------------------------- #
# Persistence: the existing ``setting`` key/value table, JSON-encoded scalars.
# No new table, no migration.
# --------------------------------------------------------------------------- #
_RATE_KEY = "fx_rate"
_CURRENCY_KEY = "fx_rate_currency"
_SOURCE_KEY = "fx_source"
_QUOTE_DATE_KEY = "fx_quote_date"
_FETCHED_AT_KEY = "fx_fetched_at"


def _get(session, key):
    from observatory.models import Setting

    row = session.get(Setting, key)
    if row is None or row.value == "":
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        return None


def _set(session, key, value) -> None:
    from observatory.models import Setting

    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value = json.dumps(value)


def stored_quote(session) -> dict | None:
    """Return the persisted rate block, or ``None`` if nothing was ever fetched."""
    rate = _get(session, _RATE_KEY)
    currency = _get(session, _CURRENCY_KEY)
    if rate is None or currency is None:
        return None
    return {
        "currency": currency,
        "rate": rate,
        "source": _get(session, _SOURCE_KEY),
        "quote_date": _get(session, _QUOTE_DATE_KEY),
        "fetched_at": _get(session, _FETCHED_AT_KEY),
    }


def refresh_quote(session, code: str, *, force: bool = False,
                  client: httpx.Client | None = None,
                  timeout: float | None = None,
                  now: float | None = None) -> dict | None:
    """Fetch and persist the USD->``code`` rate, subject to the once-per-day gate.

    Returns the freshly stored block on a successful fetch, or ``None`` when the
    gate short-circuits the call. Propagates :class:`FxError` on failure --
    nothing is written and ``fx_fetched_at`` is not advanced, so the last
    known-good rate survives, ages into staleness, and the next tick retries.

    The gate opens when any of: ``force`` is set; the stored rate is for a
    different currency; no rate was ever fetched; the stored rate is older than
    24h.
    """
    if code not in SUPPORTED:
        raise FxError(f"unsupported currency: {code!r}")

    now = time.time() if now is None else now
    fetched_at = _get(session, _FETCHED_AT_KEY)
    stored_currency = _get(session, _CURRENCY_KEY)

    stale = (
        force
        or stored_currency != code
        or not isinstance(fetched_at, (int, float))
        or (now - fetched_at) >= MIN_AGE_S
    )
    if not stale:
        return None

    result = fetch_rate(code, client=client, timeout=timeout)

    _set(session, _RATE_KEY, result["rate"])
    _set(session, _CURRENCY_KEY, result["currency"])
    _set(session, _SOURCE_KEY, result["source"])
    _set(session, _QUOTE_DATE_KEY, result["quote_date"])
    _set(session, _FETCHED_AT_KEY, int(now))
    session.commit()

    result["fetched_at"] = int(now)
    return result
