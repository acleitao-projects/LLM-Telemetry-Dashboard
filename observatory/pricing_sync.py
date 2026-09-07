"""Automatic model-pricing sync adapter (G09, issue #23).

USD stays authoritative everywhere. Once per day this module fetches the
public LiteLLM pricing catalog (``model_prices_and_context_window.json``,
MIT-licensed, free, no key) and updates ``Model`` rows that are in ``auto``
pricing mode, recording provenance. Manual prices are never touched; a failed
or empty extraction never clears or zeroes a good rate.

Design mirrors ``observatory/fx.py``:

* No FastAPI imports. ``fetch_catalog`` never touches the database, so it
  unit-tests against a fake ``httpx`` transport.
* ``run_sync`` / ``refresh`` take a session and write only ``model`` rows,
  ``pricingsyncrun`` rows and ``setting`` key/value rows.

Prompt carve-out
----------------
``observatory/llama_provider.py`` states Observatory never sends prompts. This
module is the one documented exception: when ``pricing_use_inference_match`` is
enabled (default OFF), an *ambiguous* name match may send a single short chat
completion to a configured, LIVE, >=30-min-idle provider, against a model that
is already loaded. It never loads a model and never uses the reply as a price
-- the reply only selects a catalog key; the price is always read from the
parsed catalog.
"""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation

import httpx

from .settings import FAMILY_TAG_TOKENS, QUANT_TOKENS

SOURCE = "litellm"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
LITELLM_COMMITS_API = (
    "https://api.github.com/repos/BerriAI/litellm/commits"
    "?path=model_prices_and_context_window.json&per_page=1"
)

CATALOG_TIMEOUT_S = 30.0
MATCH_TIMEOUT_S = 60.0

# Once-per-day gate. ``refresh`` compares this against the persisted
# ``pricing_last_run_at`` so the limit survives process restarts.
MIN_AGE_S = 86_400
# A provider must have had no inference activity for this long before the
# optional inference-assisted match may call it.
IDLE_MIN_S = 1800

PER_MILLION = Decimal("1000000")
_EIGHT_DP = Decimal("0.00000001")
# USD-per-million ceiling. A published token price above this is a unit error.
_PRICE_MAX = Decimal("1000000")

MATCH_CANDIDATE_LIMIT = 30

# LiteLLM per-token cost key -> our Model column. Every value is multiplied by
# PER_MILLION to reach USD per 1,000,000 tokens.
FIELD_MAP = {
    "input_cost_per_token": "input_price_per_million",
    "output_cost_per_token": "output_price_per_million",
    "cache_creation_input_token_cost": "cache_write_price_per_million",
    "cache_read_input_token_cost": "cache_read_price_per_million",
}

DEFAULT_PROMPT = (
    "You are matching a locally-served model file to its entry in a public LLM "
    "pricing catalog.\n\n"
    "Local model name: {model_name}\n\n"
    "Candidate catalog keys:\n{candidates}\n\n"
    "Pick the ONE key that is the same base model as the local file, or reply "
    "null.\n\n"
    "Treat these as IRRELEVANT -- they must not block a match:\n"
    "- host / reseller / cloud / org prefixes: openrouter/, deepinfra/, "
    "cloudflare/, @cf/, bedrock/, vertex_ai/, azure/, together_ai/, "
    "fireworks_ai/, novita/, groq/, wandb/, and org segments like google/, "
    "Qwen/, meta-llama/, mistralai/.\n"
    "- suffixes and tags: -it, -instruct, -chat, -hf, quantization (Q4_K_M, "
    "IQ4_XS, UD-Q2_K_XL, ...), file format (.gguf), and MTP / draft / "
    "speculative / tensor-split / layer-split / reasoning / context-length / "
    "-uncensored / fine-tune tags.\n\n"
    "Match on model family, parameter size (e.g. 27B, 35B-A3B, 4B) and "
    "generation/version. If several candidates are the same base model, choose "
    "the shortest, plainest key. If nothing is the same base model, or you are "
    "genuinely unsure, reply null -- never invent.\n\n"
    "Reply with ONLY compact JSON, no prose: {\"match\": \"<key>\"} or "
    "{\"match\": null}\n"
)

# The two placeholders the prompt template must contain. Substituted literally
# (not via str.format) so the JSON braces in the template are harmless.
PROMPT_FIELDS = ("{model_name}", "{candidates}")


class PricingSyncError(Exception):
    """Raised when the pricing catalog cannot be fetched or used.

    Sibling to ``fx.FxError`` / ``pricing.PricingValidationError``: any instance
    means the caller keeps whatever it already had. ``kind == "not_modified"``
    is the one benign case (HTTP 304) -- the daily anchor still advances.
    """

    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _normalize_price(raw) -> str:
    """Validate a per-token cost and return USD-per-million as an 8-dp string.

    ``raw`` is whatever ``json.loads(..., parse_float=Decimal)`` produced -- a
    ``Decimal`` normally, sometimes an ``int``. Zero is valid (a genuinely free
    model). Raises :class:`PricingSyncError` for anything unusable.
    """
    try:
        d = Decimal(raw) * PER_MILLION
    except (InvalidOperation, ValueError, TypeError):
        raise PricingSyncError(f"unparseable price: {raw!r}")
    if d.is_nan() or d.is_infinite():
        raise PricingSyncError(f"non-finite price: {raw!r}")
    if d < 0:
        raise PricingSyncError(f"negative price: {raw!r}")
    if d > _PRICE_MAX:
        raise PricingSyncError(f"price out of range: {raw!r}")
    return format(d.quantize(_EIGHT_DP), "f")


def _extract_prices(entry: dict) -> dict[str, str]:
    """Return ``{model_column: 8-dp USD/M string}`` for a LiteLLM catalog entry.

    Only keys present in the entry that also normalise cleanly are returned. A
    key that raises in :func:`_normalize_price` is dropped, never zeroed. An
    empty dict means "nothing usable was published" -- the caller's
    empty-extraction guard then keeps whatever it already had.
    """
    out: dict[str, str] = {}
    if not isinstance(entry, dict):
        return out
    for src_key, col in FIELD_MAP.items():
        if src_key not in entry:
            continue
        try:
            out[col] = _normalize_price(entry[src_key])
        except PricingSyncError:
            # Malformed single field: skip it, keep the rest of the entry.
            continue
    return out


# --------------------------------------------------------------------------- #
# Catalog fetch (DB-free, mock-transport testable)
# --------------------------------------------------------------------------- #
def _fetch_commit_sha(client: httpx.Client) -> str | None:
    """Best-effort: the latest commit SHA touching the catalog file.

    Unauthenticated GitHub API, 60 req/hr -- ample for a once-daily call. Any
    failure returns ``None`` and the caller falls back to the blob ETag.
    """
    try:
        resp = client.get(LITELLM_COMMITS_API,
                          headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        data = json.loads(resp.text)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            sha = data[0].get("sha")
            return sha if isinstance(sha, str) and sha else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return None


def fetch_catalog(*, client: httpx.Client | None = None,
                  timeout: float | None = None,
                  etag: str | None = None) -> dict:
    """Fetch the LiteLLM pricing catalog.

    Returns ``{"entries", "etag", "commit", "fetched_at", "not_modified"}``.
    ``entries`` maps catalog key -> entry dict. On HTTP 304 ``not_modified`` is
    ``True`` and ``entries`` is empty. Raises :class:`PricingSyncError` on any
    transport, HTTP or parse failure.

    Pass ``client`` (e.g. built on ``httpx.MockTransport``) to test without
    network access.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout or CATALOG_TIMEOUT_S,
                              follow_redirects=True)
    try:
        headers = {"If-None-Match": etag} if etag else {}
        try:
            resp = client.get(LITELLM_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise PricingSyncError(f"transport error: {exc}") from exc

        now = int(time.time())
        if resp.status_code == 304:
            raise PricingSyncError("catalog not modified", kind="not_modified")
        if resp.status_code != 200:
            raise PricingSyncError(f"HTTP {resp.status_code} from {SOURCE}")

        # NOT resp.json(): stdlib json turns 6e-07 into a binary float, and
        # Decimal(6e-07) is then 5.99999...e-7. parse_float=Decimal keeps every
        # price exact from the first moment it exists. This is the single most
        # important line in the feature.
        try:
            body = json.loads(resp.text, parse_float=Decimal)
        except (ValueError, TypeError) as exc:
            raise PricingSyncError(f"unparseable body: {exc}") from exc
        if not isinstance(body, dict):
            raise PricingSyncError("catalog body is not an object")

        entries = {k: v for k, v in body.items()
                   if k != "sample_spec" and isinstance(v, dict)}
        if not entries:
            raise PricingSyncError("catalog has no usable entries")

        new_etag = resp.headers.get("etag")
        commit = _fetch_commit_sha(client) or _etag_sha(new_etag)
        return {
            "entries": entries,
            "etag": new_etag,
            "commit": commit,
            "fetched_at": now,
            "not_modified": False,
        }
    finally:
        if owns_client:
            client.close()


def _etag_sha(etag: str | None) -> str | None:
    """Pull the hex blob SHA out of a (possibly weak/quoted) ETag header."""
    if not isinstance(etag, str):
        return None
    m = re.search(r"[0-9a-f]{7,40}", etag)
    return m.group(0) if m else None


# --------------------------------------------------------------------------- #
# Name matching (Step A: local, no network)
# --------------------------------------------------------------------------- #
_EXT_RE = re.compile(r"\.(gguf|bin|safetensors|pt|pth|ggml|onnx)$", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:x\d+)?)\s*b(?![a-z0-9])", re.IGNORECASE)
_QUANT_RE = re.compile(
    "|".join(re.escape(q) for q in sorted(QUANT_TOKENS, key=len, reverse=True)),
    re.IGNORECASE,
)
_STOPWORDS = {
    "gguf", "ggml", "hf", "mlx", "gptq", "awq", "exl2", "fp16", "fp8",
    "int4", "int8", "gs", "main", "latest", "model", "models", "ai", "llm",
    "the", "of", "and", "pro", "preview",
}
_HIGH = 0.60
_MARGIN = 0.15
_MID = 0.42
_LOW = 0.30


def _norm_key(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    base = _EXT_RE.sub("", base)
    base = _QUANT_RE.sub(" ", base)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def _tokenize(name: str):
    """Return ``(token_frozenset, size_token_or_None)`` for a model name.

    Directory, file extension and quantization tokens are stripped; a
    ``<n>b`` / ``<a>x<b>b`` parameter-size token is extracted separately so it
    can gate matches. Single letters and known noise words are dropped.
    """
    base = re.split(r"[\\/]", name)[-1]
    base = _EXT_RE.sub("", base)
    base = _QUANT_RE.sub(" ", base).lower()

    size = None
    m = _SIZE_RE.search(base)
    if m:
        size = m.group(1).lower() + "b"

    toks: set[str] = set()
    for part in re.split(r"[^a-z0-9]+", base):
        if not part:
            continue
        for run in re.findall(r"[a-z]+|[0-9]+", part):
            if run in FAMILY_TAG_TOKENS or run in _STOPWORDS:
                continue
            if run.isalpha() and len(run) == 1:
                continue
            toks.add(run)
    if size:
        toks.add(size)
    return frozenset(toks), size


def _score(q_toks, q_size, key):
    k_toks, k_size = _tokenize(key)
    if not k_toks:
        return None
    if q_size and k_size and q_size != k_size:
        return None
    inter = len(q_toks & k_toks)
    if not inter:
        return None
    union = len(q_toks | k_toks)
    jaccard = inter / union
    coverage = inter / len(q_toks)
    # Blend: coverage of the local name's tokens matters more than symmetry.
    return 0.5 * jaccard + 0.5 * coverage


def prefilter_candidates(model_name: str, keys, *,
                         limit: int = MATCH_CANDIDATE_LIMIT) -> list[str]:
    """Top ``limit`` catalog keys by token overlap with ``model_name``."""
    q_toks, q_size = _tokenize(model_name)
    if not q_toks:
        return []
    scored = []
    for k in keys:
        s = _score(q_toks, q_size, k)
        if s is not None:
            scored.append((s, k))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [k for _, k in scored[:limit]]


def resolve_key(model_name: str, keys):
    """Return ``(key_or_None, decision)``.

    ``decision`` is one of ``exact`` / ``near`` / ``ambiguous`` / ``none``.
    ``exact`` and ``near`` are used directly with no LLM call; ``ambiguous`` is
    eligible for the optional inference-assisted match; ``none`` is left
    untouched.
    """
    q_toks, q_size = _tokenize(model_name)
    if not q_toks:
        return None, "none"

    norm_q = _norm_key(model_name)
    exact = [k for k in keys if _norm_key(k) == norm_q]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        # Same normalised name published under several catalog keys (e.g. one
        # per hosting vendor). No deterministic pick -- defer, don't guess.
        return None, "ambiguous"

    scored = []
    for k in keys:
        s = _score(q_toks, q_size, k)
        if s is not None:
            scored.append((s, k))
    if not scored:
        return None, "none"
    scored.sort(key=lambda t: (-t[0], t[1]))

    top_score, top_key = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if top_score >= _HIGH and (top_score - runner) >= _MARGIN:
        return top_key, "near"

    strong = [s for s, _ in scored if s >= _LOW]
    if len(strong) >= 2 or (strong and strong[0] >= _MID):
        return None, "ambiguous"
    return None, "none"


# --------------------------------------------------------------------------- #
# Applying prices to a Model row
# --------------------------------------------------------------------------- #
def _apply_prices(model, prices: dict, provenance: str, key: str,
                  now: float) -> bool:
    """Write extracted prices onto ``model``; return whether anything changed.

    Per field: a value missing from ``prices`` never overwrites a stored
    non-zero rate (the empty-extraction guard); it clears a stored zero/blank
    to ``None``. Always stamps provenance and clears stale/error state.
    """
    changed = False
    for col in FIELD_MAP.values():
        new = prices.get(col)
        cur = getattr(model, col, None)
        if new is None:
            if cur is not None and cur != "":
                try:
                    if Decimal(cur) != 0:
                        continue  # keep the good rate
                except (InvalidOperation, ValueError, TypeError):
                    continue
            if cur is not None:
                setattr(model, col, None)
                changed = True
            continue
        if cur != new:
            setattr(model, col, new)
            changed = True

    model.pricing_source = provenance
    model.pricing_litellm_key = key
    model.pricing_synced_at = int(now * 1000)
    model.pricing_stale = False
    model.pricing_last_error = None
    return changed


# --------------------------------------------------------------------------- #
# Persistence: the existing ``setting`` key/value table, JSON-encoded scalars.
# --------------------------------------------------------------------------- #
_ENABLED_KEY = "pricing_sync_enabled"
_RUN_TIME_KEY = "pricing_sync_run_time"
_USE_MATCH_KEY = "pricing_use_inference_match"
_MATCH_PROVIDER_KEY = "pricing_match_provider_id"
_MATCH_MODEL_KEY = "pricing_match_model"
_PROMPT_KEY = "pricing_match_prompt"
_LAST_RUN_KEY = "pricing_last_run_at"
_ETAG_KEY = "pricing_catalog_etag"
_COMMIT_KEY = "pricing_catalog_commit"
_CATALOG_FETCHED_KEY = "pricing_catalog_fetched_at"
_NOMATCH_KEY = "pricing_nomatch_seen"

_DEFAULT_RUN_TIME = "04:00"


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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(epoch: int | float | None) -> str:
    if not isinstance(epoch, (int, float)):
        return "?"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _parse_hhmm(run_time_str) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in str(run_time_str).split(":"))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except (ValueError, AttributeError):
        pass
    return 4, 0


def _run_due(last_run_at, run_time_str, now: float) -> bool:
    """The once-per-local-day gate.

    Opens when the current local time is at or past ``run_time`` and no run has
    completed yet on the current local calendar day. Restart-safe: the anchor
    is the persisted ``pricing_last_run_at``.
    """
    hh, mm = _parse_hhmm(run_time_str)
    lt = time.localtime(now)
    past_run_time = (lt.tm_hour, lt.tm_min) >= (hh, mm)
    if not isinstance(last_run_at, (int, float)):
        return past_run_time
    prev = time.localtime(last_run_at)
    if (prev.tm_year, prev.tm_yday) == (lt.tm_year, lt.tm_yday):
        return False
    return past_run_time


def _next_run_at(last_run_at, run_time_str, now: float) -> int:
    hh, mm = _parse_hhmm(run_time_str)
    lt = time.localtime(now)
    cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    ran_today = (isinstance(last_run_at, (int, float))
                 and time.localtime(last_run_at)[:3] == lt[:3])
    if cand <= now or ran_today:
        cand += 86_400
    return int(cand)


def stored_config(session, *, now: float | None = None) -> dict:
    """The canonical automatic-pricing config block for the API."""
    now = time.time() if now is None else now
    run_time = _get(session, _RUN_TIME_KEY) or _DEFAULT_RUN_TIME
    last_run = _get(session, _LAST_RUN_KEY)
    pid = _get(session, _MATCH_PROVIDER_KEY)
    return {
        "enabled": bool(_get(session, _ENABLED_KEY)),
        "run_time": run_time,
        "use_inference_match": bool(_get(session, _USE_MATCH_KEY)),
        "match_provider_id": pid if isinstance(pid, int) else None,
        "match_model": _get(session, _MATCH_MODEL_KEY) or None,
        "prompt": _get(session, _PROMPT_KEY) or DEFAULT_PROMPT,
        "default_prompt": DEFAULT_PROMPT,
        "last_run_at": last_run if isinstance(last_run, (int, float)) else None,
        "next_run_at": _next_run_at(last_run, run_time, now),
        "catalog_commit": _get(session, _COMMIT_KEY),
        "catalog_fetched_at": _get(session, _CATALOG_FETCHED_KEY),
    }


# --------------------------------------------------------------------------- #
# Idle detection + match-target selection
# --------------------------------------------------------------------------- #
def _loaded_model_id(models) -> str | None:
    """Pick an already-loaded model id from a ``/v1/models`` payload.

    A bare llama-server lists exactly one, always loaded. A router may list
    several with a state field; only ``loaded``/``ready``/``active`` count.
    """
    for entry in models or []:
        if not isinstance(entry, dict):
            continue
        state = entry.get("state") or entry.get("status")
        if state is not None and str(state).lower() not in (
                "loaded", "ready", "active", "running"):
            continue
        mid = entry.get("id") or entry.get("name")
        if mid:
            return str(mid)
    return None


def provider_idle(session, provider_id: int, *, min_idle_s: int = IDLE_MIN_S,
                  now_ms: int | None = None, client=None,
                  require_model: str | None = None,
                  relaxed: bool = False) -> tuple[bool, str]:
    """``(is_idle, reason)`` for a provider. Conservative: any doubt -> not idle.

    When ``client`` is given the reason string on success carries the model id
    to use for :func:`llm_match` -- the first currently-loaded model, or
    ``require_model`` when the operator configured a specific one (verified to
    exist on the provider; a router may load it lazily on the first request).

    ``relaxed`` (a manual "Run now") drops the DB history checks -- the
    30-min-idle window and the recent-session-row check -- and trusts only the
    live ``slots()`` "is a slot processing right now" probe. The scheduled
    path keeps all of them.
    """
    from sqlalchemy import func
    from sqlmodel import select

    from observatory.models import Model as ModelRow, SessionRow

    now_ms = _now_ms() if now_ms is None else now_ms

    if not relaxed:
        busy = session.exec(
            select(SessionRow.id).where(
                SessionRow.provider_id == provider_id,
                SessionRow.status.in_(("ACTIVE", "FINALIZING")),
                SessionRow.live_seen_at.is_not(None),
                SessionRow.live_seen_at >= now_ms - 10_000,
            ).limit(1)
        ).first()
        if busy is not None:
            return False, "active session"

        last_vals = [
            session.exec(select(func.max(ModelRow.last_used_at)).where(
                ModelRow.provider_id == provider_id)).one(),
            session.exec(select(func.max(SessionRow.live_seen_at)).where(
                SessionRow.provider_id == provider_id)).one(),
            session.exec(select(func.max(SessionRow.end_at)).where(
                SessionRow.provider_id == provider_id)).one(),
        ]
        last = max((v for v in last_vals if isinstance(v, (int, float))),
                   default=None)
        if last is not None and now_ms - last < min_idle_s * 1000:
            return False, "used recently"

    if client is None:
        return True, ""

    try:
        models = client.models()
    except Exception:
        return False, "models check failed"

    if require_model:
        ids = {str(e.get("id") or e.get("name")) for e in (models or [])
               if isinstance(e, dict)}
        if require_model not in ids:
            return False, f"match model {require_model!r} not on provider"
        # A specific model was chosen; the DB idle window already guards
        # against interrupting real work, so accept a lazy router load.
        return True, require_model

    loaded = _loaded_model_id(models)
    if not loaded:
        return False, "no model loaded"
    try:
        slots = client.slots(model=loaded)
    except Exception:
        return False, "slots check failed"
    for slot in slots or []:
        if isinstance(slot, dict) and slot.get("is_processing"):
            return False, "slot processing"
    return True, loaded


def _pick_match_target(session, *, client_factory, now_ms: int | None = None,
                       relaxed: bool = False):
    """Return ``(Provider, loaded_model_id)`` or ``(None, None)``."""
    from sqlmodel import select

    from observatory.models import Provider

    now_ms = _now_ms() if now_ms is None else now_ms
    pid = _get(session, _MATCH_PROVIDER_KEY)
    prov = None
    if isinstance(pid, int):
        prov = session.get(Provider, pid)
    if prov is None:
        prov = session.exec(
            select(Provider).where(Provider.is_default.is_(True))).first()
    if prov is None:
        prov = session.exec(
            select(Provider).where(Provider.enabled.is_(True))
            .order_by(Provider.id)).first()
    if prov is None or not prov.enabled:
        return None, None
    # No stored-status gate: the collector may not poll this provider at all
    # (so Provider.status is meaningless here). provider_idle's live
    # models()/slots() call is the real reachability test.

    require_model = _get(session, _MATCH_MODEL_KEY) or None
    client = client_factory(prov) if client_factory else None
    try:
        idle, reason = provider_idle(session, prov.id, now_ms=now_ms,
                                     client=client, require_model=require_model,
                                     relaxed=relaxed)
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass
    if not idle:
        return None, None
    return prov, reason


# --------------------------------------------------------------------------- #
# Inference-assisted match (Step B) -- the one documented prompt Observatory
# ever sends. Selects a catalog key only; never a price.
# --------------------------------------------------------------------------- #
def _parse_match(content) -> str | None:
    """Pull ``match`` out of a model reply.

    Tolerant of code fences and of reasoning models that restate the answer
    several times: the LAST ``{... "match" ...}`` object wins, so a final
    ``{"match": null}`` overrides an earlier tentative key.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    txt = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", content.strip()))
    for chunk in reversed(re.findall(r"\{[^{}]*\}", txt)):
        if "match" not in chunk:
            continue
        try:
            obj = json.loads(chunk.replace("'", '"'))
        except ValueError:
            continue
        if isinstance(obj, dict) and "match" in obj:
            v = obj.get("match")
            return v if isinstance(v, str) and v else None
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            v = obj.get("match")
            return v if isinstance(v, str) and v else None
    except ValueError:
        pass
    return None


def llm_match(model_name: str, candidates: list[str], *, provider_base_url: str,
              loaded_model: str, prompt_template: str,
              client: httpx.Client | None = None,
              timeout: float | None = None) -> str | None:
    """Ask an already-loaded local model which candidate key matches.

    Returns the chosen key only if it is one of ``candidates``; ``None``
    otherwise. Raises :class:`PricingSyncError` on any transport / HTTP /
    response-shape failure. The reply is never read as a price.
    """
    if not candidates:
        return None
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout or MATCH_TIMEOUT_S)
    try:
        prompt = (
            prompt_template
            .replace("{model_name}", model_name)
            .replace("{candidates}", "\n".join(f"- {c}" for c in candidates))
        )
        payload = {
            "model": loaded_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 512,
            "stream": False,
            "response_format": {"type": "json_object"},
            # Qwen-family hint: answer directly, don't open a <think> block.
            # Non-Qwen templates ignore it.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        url = provider_base_url.rstrip("/") + "/v1/chat/completions"
        try:
            resp = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise PricingSyncError(f"match transport error: {exc}") from exc
        if resp.status_code != 200:
            raise PricingSyncError(f"match HTTP {resp.status_code}")
        try:
            body = json.loads(resp.text)
            msg = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PricingSyncError(f"match response malformed: {exc}") from exc
        # A reasoning model that runs out of tokens mid-<think> leaves content
        # empty but has already written the answer into reasoning_content.
        content = (msg.get("content") or "").strip() or (
            msg.get("reasoning_content") or "")
        chosen = _parse_match(content)
        return chosen if chosen in candidates else None
    finally:
        if owns_client:
            client.close()


# --------------------------------------------------------------------------- #
# The run + the daily gate
# --------------------------------------------------------------------------- #
def _run_dict(row) -> dict:
    return {
        "id": row.id,
        "trigger": row.trigger,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "result": row.result,
        "attempted": row.attempted,
        "updated": row.updated,
        "skipped": row.skipped,
        "unresolved": row.unresolved,
        "failed": row.failed,
        "error_summary": row.error_summary,
        "litellm_commit": row.litellm_commit,
    }


def run_sync(session, *, trigger: str = "scheduled", now: float | None = None,
             force: bool = False,
             catalog_client: httpx.Client | None = None,
             match_client_factory=None,
             match_http_client: httpx.Client | None = None,
             catalog_timeout: float | None = None,
             match_timeout: float | None = None) -> dict:
    """Execute one pricing-sync run. Returns
    ``{"run": <dict>, "affected_provider_ids": [...]}``.

    Records a ``PricingSyncRun`` row before doing any network work so the API
    can see ``result='running'`` immediately. On a hard catalog failure the row
    is finalised as ``error`` and the exception propagates (the caller must not
    advance the daily anchor).

    ``force`` (a manual "Run now") fetches the catalog unconditionally -- no
    ``If-None-Match``, so an unchanged catalog is still re-evaluated -- and
    clears the per-model "no match" back-off so previously unresolved models
    are retried. The scheduled path (``force=False``) keeps the 304
    short-circuit.
    """
    from sqlmodel import select

    from observatory.models import Model as ModelRow, PricingSyncRun

    now = time.time() if now is None else now
    run = PricingSyncRun(trigger=trigger, started_at=_now_ms(), result="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    try:
        cat = fetch_catalog(
            client=catalog_client, timeout=catalog_timeout,
            etag=None if force else _get(session, _ETAG_KEY))
    except PricingSyncError as exc:
        if exc.kind == "not_modified":
            run.result = "not_modified"
            run.finished_at = _now_ms()
            session.add(run)
            session.commit()
            return {"run": _run_dict(run), "affected_provider_ids": []}
        run.result = "error"
        run.error_summary = str(exc)[:500]
        run.finished_at = _now_ms()
        session.add(run)
        session.commit()
        raise

    sha = cat["commit"] or "unknown"
    provenance = f"LiteLLM catalog @ {sha} fetched {_iso(cat['fetched_at'])}"
    keys = list(cat["entries"])

    nomatch = {} if force else _get(session, _NOMATCH_KEY)
    if not isinstance(nomatch, dict):
        nomatch = {}

    step_b_on = bool(_get(session, _USE_MATCH_KEY))
    prompt_template = _get(session, _PROMPT_KEY) or DEFAULT_PROMPT

    models = session.exec(
        select(ModelRow).where(ModelRow.pricing_mode == "auto")).all()

    updated = skipped = unresolved = failed = 0
    affected: set[int] = set()
    errors: list[str] = []

    for m in models:
        run.attempted += 1

        # Already matched: re-price straight from the stored catalog key, no
        # name resolution and no inference. llm_match only runs on a model's
        # first match, when its key disappears from the catalog, or after a
        # manual re-match (which clears pricing_litellm_key).
        if m.pricing_litellm_key and m.pricing_litellm_key in cat["entries"]:
            changed = _apply_prices(
                m, _extract_prices(cat["entries"][m.pricing_litellm_key]),
                provenance, m.pricing_litellm_key, now)
            session.add(m)
            if changed:
                updated += 1
                affected.add(m.provider_id)
            else:
                skipped += 1
            continue

        if nomatch.get(str(m.id)) == sha:
            unresolved += 1
            continue

        key, decision = resolve_key(m.name, keys)

        if key is None and decision == "ambiguous" and step_b_on:
            prov, loaded = _pick_match_target(
                session, client_factory=match_client_factory, relaxed=force)
            if prov is not None and loaded:
                cands = prefilter_candidates(m.name, keys)
                try:
                    # match_client_factory builds a LlamaClient for the idle
                    # check (models()/slots()); the chat POST needs a plain
                    # httpx client, which llm_match builds itself when none is
                    # injected.
                    key = llm_match(
                        m.name, cands,
                        provider_base_url=prov.base_url, loaded_model=loaded,
                        prompt_template=prompt_template,
                        client=match_http_client,
                        timeout=match_timeout)
                except PricingSyncError as exc:
                    failed += 1
                    errors.append(f"{m.name}: {exc}")
                    key = None

        if key is None:
            unresolved += 1
            if decision in ("none", "ambiguous"):
                nomatch[str(m.id)] = sha
            continue

        changed = _apply_prices(m, _extract_prices(cat["entries"][key]),
                                provenance, key, now)
        session.add(m)
        if changed:
            updated += 1
            affected.add(m.provider_id)
        else:
            skipped += 1

    _set(session, _NOMATCH_KEY, nomatch)
    _set(session, _ETAG_KEY, cat["etag"])
    _set(session, _COMMIT_KEY, cat["commit"])
    _set(session, _CATALOG_FETCHED_KEY, cat["fetched_at"])
    session.commit()

    run.result = "partial" if failed else "ok"
    run.updated = updated
    run.skipped = skipped
    run.unresolved = unresolved
    run.failed = failed
    run.litellm_commit = cat["commit"]
    run.finished_at = _now_ms()
    if errors:
        seen: list[str] = []
        for e in errors:
            if e not in seen:
                seen.append(e)
        run.error_summary = "; ".join(seen)[:500]
    session.add(run)
    session.commit()

    return {"run": _run_dict(run), "affected_provider_ids": sorted(affected)}


def refresh(session, *, force: bool = False, trigger: str = "scheduled",
            now: float | None = None,
            catalog_client: httpx.Client | None = None,
            match_client_factory=None,
            match_http_client: httpx.Client | None = None,
            catalog_timeout: float | None = None,
            match_timeout: float | None = None) -> dict | None:
    """Run the daily sync subject to the once-per-day gate.

    Returns the run dict on a completed run (including a benign ``304``), or
    ``None`` when the gate short-circuits. A hard :class:`PricingSyncError`
    propagates without advancing the daily anchor -- the next tick retries.
    """
    now = time.time() if now is None else now
    if not force:
        if not bool(_get(session, _ENABLED_KEY)):
            return None
        last_run = _get(session, _LAST_RUN_KEY)
        run_time = _get(session, _RUN_TIME_KEY) or _DEFAULT_RUN_TIME
        if not _run_due(last_run, run_time, now):
            return None

    result = run_sync(session, trigger=trigger, now=now, force=force,
                      catalog_client=catalog_client,
                      match_client_factory=match_client_factory,
                      match_http_client=match_http_client,
                      catalog_timeout=catalog_timeout,
                      match_timeout=match_timeout)
    _set(session, _LAST_RUN_KEY, int(now))
    session.commit()
    return result
