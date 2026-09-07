"""G09 issue #23: automatic model-pricing sync (observatory/pricing_sync.py).

Step 1 of the build order -- proves the fetch/parse/match core is correct and
float-free before schema or app wiring depend on it. Every HTTP interaction is
mocked; this suite never calls GitHub or an inference server.
"""
from __future__ import annotations

import json
import time
import types
import unittest
from decimal import Decimal

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import pricing_sync as ps
from observatory.models import (Model, PricingSyncRun, Provider, SessionRow,
                                now_ms)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def catalog_payload(**overrides) -> dict:
    body = {
        "sample_spec": {"input_cost_per_token": 0.0, "note": "not a model"},
        "gpt-4o": {
            "input_cost_per_token": 2.5e-06,
            "output_cost_per_token": 1e-05,
            "cache_read_input_token_cost": 1.25e-06,
            "litellm_provider": "openai",
            "mode": "chat",
        },
        "claude-sonnet-4-5": {
            "input_cost_per_token": 3e-06,
            "output_cost_per_token": 1.5e-05,
            "cache_creation_input_token_cost": 3.75e-06,
            "cache_read_input_token_cost": 3e-07,
        },
        "qwen2.5-7b-instruct": {
            "input_cost_per_token": 6e-07,
            "output_cost_per_token": 6e-07,
        },
    }
    body.update(overrides)
    return body


_FAKE_ETAG = '"d3adb33fcafe0123456789abcdef0123456789ab"'


def catalog_client(payload=None, *, status=200, etag=_FAKE_ETAG,
                   commits_status=404, commits_body="[]"):
    payload = catalog_payload() if payload is None else payload

    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(commits_status, text=commits_body)
        if status == 304:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(status, text=json.dumps(payload),
                              headers={"etag": etag})

    return client_for(handler)


# --------------------------------------------------------------------------- #
# _normalize_price
# --------------------------------------------------------------------------- #
class NormalizePrice(unittest.TestCase):
    def test_per_token_becomes_per_million_8dp(self):
        self.assertEqual(ps._normalize_price(Decimal("0.0000006")), "0.60000000")
        self.assertEqual(ps._normalize_price(Decimal("3e-06")), "3.00000000")

    def test_zero_is_valid(self):
        self.assertEqual(ps._normalize_price(Decimal("0")), "0.00000000")

    def test_rejects_negative_nan_inf_and_over_ceiling(self):
        for bad in (Decimal("-1e-9"), Decimal("NaN"), Decimal("Infinity"),
                    Decimal("2")):  # 2 * 1e6 = 2e6 > _PRICE_MAX
            with self.assertRaises(ps.PricingSyncError):
                ps._normalize_price(bad)


# --------------------------------------------------------------------------- #
# _extract_prices
# --------------------------------------------------------------------------- #
class ExtractPrices(unittest.TestCase):
    def test_all_four_fields(self):
        out = ps._extract_prices({
            "input_cost_per_token": Decimal("3e-06"),
            "output_cost_per_token": Decimal("1.5e-05"),
            "cache_creation_input_token_cost": Decimal("3.75e-06"),
            "cache_read_input_token_cost": Decimal("3e-07"),
        })
        self.assertEqual(out, {
            "input_price_per_million": "3.00000000",
            "output_price_per_million": "15.00000000",
            "cache_write_price_per_million": "3.75000000",
            "cache_read_price_per_million": "0.30000000",
        })

    def test_missing_field_is_absent_not_zero(self):
        out = ps._extract_prices({"input_cost_per_token": Decimal("6e-07")})
        self.assertEqual(list(out), ["input_price_per_million"])
        self.assertNotIn("cache_write_price_per_million", out)

    def test_one_bad_field_dropped_others_kept(self):
        out = ps._extract_prices({
            "input_cost_per_token": Decimal("-1"),
            "output_cost_per_token": Decimal("1e-05"),
        })
        self.assertEqual(out, {"output_price_per_million": "10.00000000"})

    def test_price_reaches_normalize_as_decimal_not_float(self):
        seen = {}
        real = ps._normalize_price

        def spy(raw):
            seen["type"] = type(raw)
            return real(raw)

        ps._normalize_price = spy
        try:
            body = json.loads('{"input_cost_per_token": 0.0000006}',
                              parse_float=Decimal)
            ps._extract_prices(body)
        finally:
            ps._normalize_price = real
        self.assertIs(seen["type"], Decimal)


# --------------------------------------------------------------------------- #
# fetch_catalog
# --------------------------------------------------------------------------- #
class FetchCatalog(unittest.TestCase):
    def test_happy_path(self):
        out = ps.fetch_catalog(client=catalog_client())
        self.assertFalse(out["not_modified"])
        self.assertIn("gpt-4o", out["entries"])
        self.assertNotIn("sample_spec", out["entries"])
        self.assertEqual(out["etag"], _FAKE_ETAG)
        # commits API 404 -> commit falls back to the blob SHA in the ETag
        self.assertEqual(out["commit"], "d3adb33fcafe0123456789abcdef0123456789ab")
        self.assertIsInstance(out["fetched_at"], int)

    def test_commit_sha_from_github_api_when_available(self):
        client = catalog_client(commits_status=200,
                                commits_body='[{"sha": "abc123def456abc123"}]')
        out = ps.fetch_catalog(client=client)
        self.assertEqual(out["commit"], "abc123def456abc123")

    def test_parse_is_float_free(self):
        seen = {}
        real = ps._normalize_price

        def spy(raw):
            seen.setdefault("types", set()).add(type(raw))
            return real(raw)

        ps._normalize_price = spy
        try:
            out = ps.fetch_catalog(client=catalog_client())
            ps._extract_prices(out["entries"]["gpt-4o"])
        finally:
            ps._normalize_price = real
        self.assertEqual(seen["types"], {Decimal})

    def test_304_raises_not_modified(self):
        with self.assertRaises(ps.PricingSyncError) as cm:
            ps.fetch_catalog(client=catalog_client(status=304), etag='"x"')
        self.assertEqual(cm.exception.kind, "not_modified")

    def test_http_error_raises(self):
        with self.assertRaises(ps.PricingSyncError) as cm:
            ps.fetch_catalog(client=catalog_client(status=500))
        self.assertNotEqual(cm.exception.kind, "not_modified")

    def test_transport_error_raises(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        with self.assertRaises(ps.PricingSyncError):
            ps.fetch_catalog(client=client_for(handler))

    def test_unparseable_body_raises(self):
        def handler(request):
            return httpx.Response(200, text="not json at all")

        with self.assertRaises(ps.PricingSyncError):
            ps.fetch_catalog(client=client_for(handler))


# --------------------------------------------------------------------------- #
# _apply_prices -- empty-extraction guard
# --------------------------------------------------------------------------- #
def _fake_model(**cols):
    base = dict(input_price_per_million=None, output_price_per_million=None,
                cache_write_price_per_million=None,
                cache_read_price_per_million=None, pricing_source=None,
                pricing_litellm_key=None, pricing_synced_at=None,
                pricing_stale=True, pricing_last_error="old")
    base.update(cols)
    return types.SimpleNamespace(**base)


class ApplyPrices(unittest.TestCase):
    def test_missing_field_never_overwrites_a_nonzero_rate(self):
        m = _fake_model(input_price_per_million="5.00000000")
        changed = ps._apply_prices(m, {}, "prov", "k", now=1000.0)
        self.assertEqual(m.input_price_per_million, "5.00000000")
        self.assertFalse(changed)
        self.assertEqual(m.pricing_source, "prov")
        self.assertFalse(m.pricing_stale)
        self.assertIsNone(m.pricing_last_error)

    def test_missing_field_clears_a_stored_zero(self):
        m = _fake_model(input_price_per_million="0.00000000")
        changed = ps._apply_prices(m, {}, "prov", "k", now=1000.0)
        self.assertIsNone(m.input_price_per_million)
        self.assertTrue(changed)

    def test_present_field_overwrites(self):
        m = _fake_model(input_price_per_million="5.00000000")
        changed = ps._apply_prices(m, {"input_price_per_million": "1.00000000"},
                                   "prov", "k", now=1000.0)
        self.assertEqual(m.input_price_per_million, "1.00000000")
        self.assertTrue(changed)

    def test_stamps_synced_at_in_ms(self):
        m = _fake_model()
        ps._apply_prices(m, {"input_price_per_million": "1.00000000"},
                         "prov", "key-x", now=1000.0)
        self.assertEqual(m.pricing_synced_at, 1_000_000)
        self.assertEqual(m.pricing_litellm_key, "key-x")


# --------------------------------------------------------------------------- #
# resolve_key / prefilter_candidates  (Step A -- no network)
# --------------------------------------------------------------------------- #
class ResolveKey(unittest.TestCase):
    KEYS = ["gpt-4o", "claude-sonnet-4-5", "qwen2.5-7b-instruct",
            "meta-llama/llama-3.1-8b-instruct",
            "meta-llama/meta-llama-3.1-8b-instruct",
            "groq/llama-3.1-8b-instant"]

    def test_exact_match_ignores_quant_and_extension(self):
        key, decision = ps.resolve_key(
            "Qwen2.5-7B-Instruct-Q4_K_M.gguf", self.KEYS)
        self.assertEqual(key, "qwen2.5-7b-instruct")
        self.assertEqual(decision, "exact")

    def test_clean_versioned_name_is_a_near_match(self):
        key, decision = ps.resolve_key(
            "Llama-3.1-8B-Instruct-Q5_K_M.gguf",
            ["gpt-4o", "meta-llama/llama-3.1-8b-instruct", "qwen2.5-7b-instruct"])
        self.assertEqual(key, "meta-llama/llama-3.1-8b-instruct")
        self.assertIn(decision, ("exact", "near"))

    def test_same_model_from_two_vendors_is_ambiguous(self):
        key, decision = ps.resolve_key(
            "gpt-4o.gguf", ["openai/gpt-4o", "azure/gpt-4o", "claude-sonnet-4-5"])
        self.assertIsNone(key)
        self.assertEqual(decision, "ambiguous")

    def test_no_shared_tokens_is_none(self):
        key, decision = ps.resolve_key("totally-made-up-thing.gguf", self.KEYS)
        self.assertIsNone(key)
        self.assertEqual(decision, "none")

    def test_prefilter_ranks_related_keys_first(self):
        cands = ps.prefilter_candidates("Llama-3.1-8B-Instruct.gguf",
                                        self.KEYS, limit=3)
        self.assertEqual(len(cands), 3)
        self.assertTrue(all("llama" in c for c in cands))


# --------------------------------------------------------------------------- #
# run_sync / refresh / provider_idle / llm_match  (Step 3)
# --------------------------------------------------------------------------- #
def memory_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class FakeResp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class FakeLlama:
    """Stands in for LlamaClient for the idle check + the match POST."""

    def __init__(self, *, loaded="local-model", processing=False,
                 match_reply='{"match": null}', post_status=200,
                 models_raise=False, slots_raise=False):
        self.loaded = loaded
        self.processing = processing
        self.match_reply = match_reply
        self.post_status = post_status
        self.models_raise = models_raise
        self.slots_raise = slots_raise
        self.posts = []

    def models(self):
        if self.models_raise:
            raise httpx.ConnectError("boom")
        return [] if self.loaded is None else [{"id": self.loaded}]

    def slots(self, model=None):
        if self.slots_raise:
            raise httpx.ConnectError("boom")
        return [{"is_processing": True}] if self.processing else []

    def post(self, url, json=None):
        self.posts.append((url, json))
        body = json_dumps_choice(self.match_reply)
        return FakeResp(self.post_status, body)

    def close(self):
        pass


def json_dumps_choice(content):
    return json.dumps({"choices": [{"message": {"content": content}}]})


def match_http(reply, *, status=200, calls=None):
    """httpx.Client for llm_match's POST /v1/chat/completions."""
    def handler(request):
        assert request.url.path.endswith("/v1/chat/completions")
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(status, text=json_dumps_choice(reply))
    return client_for(handler)


def seed_provider(s, **kw):
    p = Provider(name=kw.get("name", "router-0"),
                 base_url=kw.get("base_url", "http://r0:8080"),
                 ptype="llama.cpp", status=kw.get("status", "LIVE"),
                 enabled=True, is_default=True,
                 last_success_at=kw.get("last_success_at", now_ms() - 1000))
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def seed_model(s, provider_id, key, name, mode="auto", **prices):
    m = Model(provider_id=provider_id, key=key, name=name, pricing_mode=mode,
              **prices)
    s.add(m)
    s.commit()
    s.refresh(m)
    return m


class RunSync(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()

    def _s(self):
        return Session(self.engine)

    def test_exact_match_writes_all_available_fields_with_provenance(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "Claude-Sonnet-4-5-Q4_K_M.gguf")
            res = ps.run_sync(s, trigger="manual", now=1_000_000.0,
                              catalog_client=catalog_client())
            self.assertEqual(res["run"]["result"], "ok")
            self.assertEqual(res["run"]["updated"], 1)
        with self._s() as s:
            m = s.get(Model, 1)
            self.assertEqual(m.input_price_per_million, "3.00000000")
            self.assertEqual(m.output_price_per_million, "15.00000000")
            self.assertEqual(m.cache_write_price_per_million, "3.75000000")
            self.assertEqual(m.cache_read_price_per_million, "0.30000000")
            self.assertTrue(m.pricing_source.startswith("LiteLLM catalog @ "))
            self.assertEqual(m.pricing_litellm_key, "claude-sonnet-4-5")
            self.assertFalse(m.pricing_stale)
            self.assertIsNotNone(m.pricing_synced_at)

    def test_manual_rows_are_never_touched(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "Claude-Sonnet-4-5.gguf", mode="manual",
                       input_price_per_million="9.99999999")
            res = ps.run_sync(s, now=1_000_000.0, catalog_client=catalog_client())
            self.assertEqual(res["run"]["attempted"], 0)
        with self._s() as s:
            self.assertEqual(s.get(Model, 1).input_price_per_million,
                             "9.99999999")

    def test_missing_cache_field_never_clobbers_a_manual_nonzero(self):
        # gpt-4o publishes no cache-creation cost; a stored non-zero survives.
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf",
                       cache_write_price_per_million="1.23000000")
            ps.run_sync(s, now=1_000_000.0, catalog_client=catalog_client())
        with self._s() as s:
            m = s.get(Model, 1)
            self.assertEqual(m.cache_write_price_per_million, "1.23000000")
            self.assertEqual(m.input_price_per_million, "2.50000000")

    def test_unresolved_row_is_left_untouched(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "some-private-frankenmerge-v7.gguf")
            res = ps.run_sync(s, now=1_000_000.0, catalog_client=catalog_client())
            self.assertEqual(res["run"]["unresolved"], 1)
            self.assertEqual(res["run"]["updated"], 0)
        with self._s() as s:
            self.assertIsNone(s.get(Model, 1).input_price_per_million)

    def test_not_modified_run_writes_nothing_and_is_logged(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf")
            res = ps.run_sync(s, now=1_000_000.0,
                              catalog_client=catalog_client(status=304))
            self.assertEqual(res["run"]["result"], "not_modified")
            self.assertEqual(res["affected_provider_ids"], [])
        with self._s() as s:
            from sqlmodel import select
            self.assertIsNone(s.get(Model, 1).input_price_per_million)
            runs = list(s.exec(select(PricingSyncRun)))
            self.assertEqual(len(runs), 1)

    def test_hard_fetch_failure_records_error_run_and_raises(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf")
            with self.assertRaises(ps.PricingSyncError):
                ps.run_sync(s, now=1_000_000.0,
                            catalog_client=catalog_client(status=500))
        with self._s() as s:
            from sqlmodel import select
            run = s.exec(select(PricingSyncRun)).one()
            self.assertEqual(run.result, "error")
            self.assertIsNotNone(run.finished_at)
            self.assertIsNone(s.get(Model, 1).input_price_per_million)

    def test_stored_key_reprices_without_any_inference(self):
        def boom_factory(prov):
            raise AssertionError("inference must not be consulted")

        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "some-obscure-local-name.gguf",
                       pricing_litellm_key="claude-sonnet-4-5")
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(s, now=1_000_000.0,
                              catalog_client=catalog_client(),
                              match_client_factory=boom_factory)
            self.assertEqual(res["run"]["updated"], 1)
        with self._s() as s:
            m = s.get(Model, 1)
            self.assertEqual(m.input_price_per_million, "3.00000000")
            self.assertEqual(m.pricing_litellm_key, "claude-sonnet-4-5")

    def test_force_ignores_stored_etag_and_nomatch_backoff(self):
        sent_etags = []

        def cc():
            def handler(request):
                if request.url.host == "api.github.com":
                    return httpx.Response(404, text="[]")
                sent_etags.append(request.headers.get("if-none-match"))
                return httpx.Response(200, text=json.dumps(catalog_payload()),
                                      headers={"etag": _FAKE_ETAG})
            return client_for(handler)

        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "Claude-Sonnet-4-5.gguf")
            ps._set(s, "pricing_catalog_etag", _FAKE_ETAG)
            ps._set(s, "pricing_nomatch_seen", {"1": "whatever"})
            s.commit()
            res = ps.run_sync(s, now=1_000_000.0, force=True, catalog_client=cc())
            self.assertEqual(res["run"]["result"], "ok")
            self.assertEqual(res["run"]["updated"], 1)
        self.assertEqual(sent_etags, [None])  # no If-None-Match on a forced run
        with self._s() as s:
            self.assertEqual(ps._get(s, "pricing_nomatch_seen"), {})

    def test_scheduled_run_still_sends_stored_etag(self):
        sent = []

        def cc():
            def handler(request):
                if request.url.host == "api.github.com":
                    return httpx.Response(404, text="[]")
                sent.append(request.headers.get("if-none-match"))
                return httpx.Response(304, headers={"etag": _FAKE_ETAG})
            return client_for(handler)

        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf")
            ps._set(s, "pricing_catalog_etag", _FAKE_ETAG)
            s.commit()
            res = ps.run_sync(s, now=1_000_000.0, force=False, catalog_client=cc())
            self.assertEqual(res["run"]["result"], "not_modified")
        self.assertEqual(sent, [_FAKE_ETAG])

    def test_stored_key_gone_from_catalog_falls_through_to_resolve(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "Claude-Sonnet-4-5-Q4_K_M.gguf",
                       pricing_litellm_key="vendor/deleted-key")
            res = ps.run_sync(s, now=1_000_000.0,
                              catalog_client=catalog_client())
            self.assertEqual(res["run"]["updated"], 1)
        with self._s() as s:
            self.assertEqual(s.get(Model, 1).pricing_litellm_key,
                             "claude-sonnet-4-5")

    def test_snapshot_affected_providers_only_for_changed(self):
        with self._s() as s:
            p1 = seed_provider(s, name="p1", base_url="http://p1")
            p2 = Provider(name="p2", base_url="http://p2", ptype="llama.cpp",
                          status="LIVE", enabled=True)
            s.add(p2)
            s.commit()
            s.refresh(p2)
            seed_model(s, p1.id, "a", "gpt-4o.gguf")
            seed_model(s, p2.id, "b", "nothing-matches-this.gguf")
            res = ps.run_sync(s, now=1_000_000.0, catalog_client=catalog_client())
            self.assertEqual(res["affected_provider_ids"], [p1.id])


class StepBInferenceMatch(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()

    def _s(self):
        return Session(self.engine)

    def _seed_ambiguous(self, s, **prov):
        p = seed_provider(s, **prov)
        seed_model(s, p.id, "m1", "gpt-4o.gguf")
        return p

    def _catalog_with_two_vendors(self):
        payload = catalog_payload(**{
            "openai/gpt-4o": {"input_cost_per_token": 2.5e-06,
                              "output_cost_per_token": 1e-05},
            "azure/gpt-4o": {"input_cost_per_token": 2.5e-06,
                             "output_cost_per_token": 1e-05},
        })
        del payload["gpt-4o"]
        return payload

    def test_happy_path_selects_key_and_prices_come_from_catalog(self):
        calls = []
        with self._s() as s:
            self._seed_ambiguous(s)
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=lambda p: FakeLlama(),
                match_http_client=match_http('{"match": "openai/gpt-4o"}',
                                             calls=calls))
            self.assertEqual(res["run"]["updated"], 1)
        with self._s() as s:
            m = s.get(Model, 1)
            self.assertEqual(m.pricing_litellm_key, "openai/gpt-4o")
            self.assertEqual(m.input_price_per_million, "2.50000000")
        # the chat call carried the model name and the candidate keys
        self.assertEqual(len(calls), 1)
        content = calls[0]["messages"][0]["content"]
        self.assertIn("gpt-4o", content)
        self.assertIn("openai/gpt-4o", content)

    def test_out_of_set_reply_is_discarded(self):
        with self._s() as s:
            self._seed_ambiguous(s)
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=lambda p: FakeLlama(),
                match_http_client=match_http('{"match": "vendor/not-a-real-key"}'))
            self.assertEqual(res["run"]["unresolved"], 1)
            self.assertEqual(res["run"]["updated"], 0)

    def test_match_http_error_counts_as_failed(self):
        with self._s() as s:
            self._seed_ambiguous(s)
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=lambda p: FakeLlama(),
                match_http_client=match_http("{}", status=500))
            self.assertEqual(res["run"]["failed"], 1)
            self.assertEqual(res["run"]["result"], "partial")

    def test_toggle_off_means_no_llm_call(self):
        called = []

        def factory(prov):
            called.append(prov)
            return FakeLlama()

        with self._s() as s:
            self._seed_ambiguous(s)
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=factory)
            self.assertEqual(res["run"]["unresolved"], 1)
        self.assertEqual(called, [])

    def test_busy_provider_skips_step_b(self):
        def factory(prov):
            return FakeLlama(match_reply='{"match": "openai/gpt-4o"}')

        with self._s() as s:
            p = self._seed_ambiguous(s)
            s.add(SessionRow(provider_id=p.id, model_id=1,
                             start_at=now_ms() - 5_000, status="ACTIVE",
                             live_seen_at=now_ms() - 2_000))
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=factory)
            self.assertEqual(res["run"]["updated"], 0)
            self.assertEqual(res["run"]["unresolved"], 1)

    def test_recently_used_provider_skips_step_b(self):
        def factory(prov):
            return FakeLlama(match_reply='{"match": "openai/gpt-4o"}')

        with self._s() as s:
            p = self._seed_ambiguous(s)
            m = s.get(Model, 1)
            m.last_used_at = now_ms() - 10 * 60 * 1000  # 10 min ago
            s.add(m)
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=factory)
            self.assertEqual(res["run"]["updated"], 0)

    def test_no_model_loaded_skips_step_b(self):
        def factory(prov):
            return FakeLlama(loaded=None)

        with self._s() as s:
            self._seed_ambiguous(s)
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=factory)
            self.assertEqual(res["run"]["updated"], 0)

    def test_slots_processing_skips_step_b(self):
        def factory(prov):
            return FakeLlama(processing=True,
                             match_reply='{"match": "openai/gpt-4o"}')

        with self._s() as s:
            self._seed_ambiguous(s)
            ps._set(s, "pricing_use_inference_match", True)
            s.commit()
            res = ps.run_sync(
                s, now=1_000_000.0,
                catalog_client=catalog_client(self._catalog_with_two_vendors()),
                match_client_factory=factory)
            self.assertEqual(res["run"]["updated"], 0)


class LlmMatchParsing(unittest.TestCase):
    def _client(self, content, status=200):
        def handler(request):
            return httpx.Response(status, text=json_dumps_choice(content))
        return client_for(handler)

    def test_plain_json(self):
        out = ps.llm_match("x", ["a", "b"], provider_base_url="http://p",
                           loaded_model="m", prompt_template=ps.DEFAULT_PROMPT,
                           client=self._client('{"match": "a"}'))
        self.assertEqual(out, "a")

    def test_fenced_json(self):
        out = ps.llm_match("x", ["a", "b"], provider_base_url="http://p",
                           loaded_model="m", prompt_template=ps.DEFAULT_PROMPT,
                           client=self._client('```json\n{"match": "b"}\n```'))
        self.assertEqual(out, "b")

    def test_null_and_out_of_set(self):
        for content in ('{"match": null}', '{"match": "zzz"}', "garbage"):
            out = ps.llm_match("x", ["a", "b"], provider_base_url="http://p",
                               loaded_model="m",
                               prompt_template=ps.DEFAULT_PROMPT,
                               client=self._client(content))
            self.assertIsNone(out)

    def test_http_error_raises(self):
        with self.assertRaises(ps.PricingSyncError):
            ps.llm_match("x", ["a"], provider_base_url="http://p",
                         loaded_model="m", prompt_template=ps.DEFAULT_PROMPT,
                         client=self._client("{}", status=503))

    def test_reads_reasoning_content_when_content_is_empty(self):
        # A reasoning model that runs out of tokens mid-<think>: content is
        # empty, the answer (restated several times) is in reasoning_content.
        def handler(request):
            return httpx.Response(200, text=json.dumps({"choices": [{"message": {
                "content": "",
                "reasoning_content": (
                    'Maybe {"match": "b"}. Wait, reconsider.\n'
                    'Actually the size differs. Let me check again.\n'
                    'Final: {"match": "a"}\nVerify: "a" is in the list. Ready.'),
            }}]}))
        out = ps.llm_match("x", ["a", "b"], provider_base_url="http://p",
                           loaded_model="m", prompt_template=ps.DEFAULT_PROMPT,
                           client=client_for(handler))
        self.assertEqual(out, "a")

    def test_last_object_wins_when_model_restates(self):
        for content, expect in [
            ('{"match": "a"} ... on reflection {"match": null}', None),
            ('{"match": null} ... wait, {"match": "b"}', "b"),
        ]:
            out = ps.llm_match("x", ["a", "b"], provider_base_url="http://p",
                               loaded_model="m",
                               prompt_template=ps.DEFAULT_PROMPT,
                               client=self._client(content))
            self.assertEqual(out, expect)

    def test_prompt_carries_name_and_candidates(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, text=json_dumps_choice('{"match": null}'))

        ps.llm_match("MyModel-7B", ["alpha", "beta"],
                     provider_base_url="http://p", loaded_model="m",
                     prompt_template=ps.DEFAULT_PROMPT,
                     client=client_for(handler))
        content = seen["body"]["messages"][0]["content"]
        self.assertIn("MyModel-7B", content)
        self.assertIn("alpha", content)
        self.assertIn("beta", content)


class DailyGate(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()

    def _s(self):
        return Session(self.engine)

    @staticmethod
    def _local(y, mo, d, h, mi):
        return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))

    def _enable(self, s, run_time="04:00"):
        ps._set(s, "pricing_sync_enabled", True)
        ps._set(s, "pricing_sync_run_time", run_time)
        s.commit()

    def test_before_run_time_on_fresh_db_does_nothing(self):
        calls = []

        def cc():
            calls.append(1)
            return catalog_client()

        with self._s() as s:
            seed_provider(s)
            self._enable(s)
            out = ps.refresh(s, now=self._local(2026, 6, 1, 2, 0),
                             catalog_client=None)
            self.assertIsNone(out)

    def test_after_run_time_runs_once_then_gates(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf")
            self._enable(s)
            t1 = self._local(2026, 6, 1, 5, 0)
            first = ps.refresh(s, now=t1, catalog_client=catalog_client())
            self.assertIsNotNone(first)
            self.assertEqual(first["run"]["result"], "ok")
            second = ps.refresh(s, now=t1 + 300,
                                catalog_client=catalog_client())
            self.assertIsNone(second)

    def test_force_bypasses_gate_and_disabled(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf")
            # not enabled, well before run time
            out = ps.refresh(s, force=True, trigger="manual",
                             now=self._local(2026, 6, 1, 2, 0),
                             catalog_client=catalog_client())
            self.assertIsNotNone(out)
            self.assertEqual(out["run"]["trigger"], "manual")

    def test_next_local_day_runs_again(self):
        with self._s() as s:
            p = seed_provider(s)
            seed_model(s, p.id, "m1", "gpt-4o.gguf")
            self._enable(s)
            ps.refresh(s, now=self._local(2026, 6, 1, 5, 0),
                       catalog_client=catalog_client())
            out = ps.refresh(s, now=self._local(2026, 6, 2, 5, 0),
                             catalog_client=catalog_client())
            self.assertIsNotNone(out)


class StoredConfig(unittest.TestCase):
    def test_defaults(self):
        engine = memory_engine()
        with Session(engine) as s:
            cfg = ps.stored_config(s, now=1_000_000.0)
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["run_time"], "04:00")
            self.assertFalse(cfg["use_inference_match"])
            self.assertIsNone(cfg["match_provider_id"])
            self.assertIsNone(cfg["match_model"])
            self.assertEqual(cfg["prompt"], ps.DEFAULT_PROMPT)
            self.assertEqual(cfg["default_prompt"], ps.DEFAULT_PROMPT)
            self.assertIsNone(cfg["last_run_at"])
            self.assertIsInstance(cfg["next_run_at"], int)

    def test_match_model_round_trips(self):
        engine = memory_engine()
        with Session(engine) as s:
            ps._set(s, "pricing_match_model", "Qwen3.6-35B-A3B-Uncensored")
            s.commit()
            self.assertEqual(ps.stored_config(s)["match_model"],
                             "Qwen3.6-35B-A3B-Uncensored")


class MatchModelSelection(unittest.TestCase):
    def test_configured_model_is_used_and_verified_present(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = seed_provider(s)
            ps._set(s, "pricing_match_model", "big-model")
            s.commit()
            idle, reason = ps.provider_idle(
                s, p.id, client=FakeLlama(loaded="big-model"),
                require_model="big-model")
            self.assertTrue(idle)
            self.assertEqual(reason, "big-model")

    def test_configured_model_absent_blocks_step_b(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = seed_provider(s)
            idle, reason = ps.provider_idle(
                s, p.id, client=FakeLlama(loaded="something-else"),
                require_model="big-model")
            self.assertFalse(idle)
            self.assertIn("big-model", reason)

    def test_relaxed_ignores_active_session_and_recent_use(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = seed_provider(s)
            m = seed_model(s, p.id, "m1", "x")
            m.last_used_at = now_ms()  # used right now
            s.add(m)
            s.add(SessionRow(provider_id=p.id, model_id=m.id,
                             start_at=now_ms() - 2_000, status="ACTIVE",
                             live_seen_at=now_ms() - 500))
            s.commit()
            strict, r1 = ps.provider_idle(s, p.id, client=FakeLlama())
            self.assertFalse(strict)
            relaxed, model = ps.provider_idle(
                s, p.id, client=FakeLlama(loaded="m0"), relaxed=True)
            self.assertTrue(relaxed)
            self.assertEqual(model, "m0")

    def test_relaxed_still_blocks_on_a_processing_slot(self):
        engine = memory_engine()
        with Session(engine) as s:
            p = seed_provider(s)
            idle, reason = ps.provider_idle(
                s, p.id, client=FakeLlama(processing=True), relaxed=True)
            self.assertFalse(idle)


if __name__ == "__main__":
    unittest.main()
