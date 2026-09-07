"""G07: Compare screen request/cache lifecycle + layout/recovery (issues #14, #15).

Primary acceptance evidence is the Playwright script (tests/g07_browser_evidence.mjs)
and the performance gate (tests/benchmark_g07.py).  This module carries the
functional snapshot-reuse checks (Compare endpoints consume the shared G02
snapshot registry) plus the secondary source-contract guards for the client
request lifecycle, the 120 ms selection-only debounce, the bounded candidate
cache, stale suppression, context-scoped last-good recovery, and the layout
CSS contract.
"""
from __future__ import annotations

import os
import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient

import app
from observatory import database as odb
from observatory.models import Model, Provider, SessionRow, now_ms

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as source:
        return source.read()


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(session, providers=2, models_per_provider=3):
    """Seed providers + models with 7d session activity.  Returns
    {provider_id: [model_ids]}."""
    now = now_ms()
    out = {}
    for i in range(providers):
        p = Provider(name=f"router-{i}", ptype="llama.cpp",
                     base_url=f"http://router-{i}:8080", status="LIVE",
                     enabled=True, is_default=(i == 0))
        session.add(p)
        session.commit()
        session.refresh(p)
        ids = []
        for j in range(models_per_provider):
            m = Model(provider_id=p.id, key=f"m{i}-{j}", name=f"model-{i}-{j}",
                      quant="Q4_K_M", family=f"family-{j % 2}", arch="llama",
                      params="8B")
            session.add(m)
            session.commit()
            session.refresh(m)
            ids.append(m.id)
            session.add(SessionRow(
                provider_id=p.id, model_id=m.id,
                start_at=now - 3_600_000, end_at=now - 1_800_000,
                duration_s=1800.0,
                prompt_tokens=1000.0 + i * 100 + j * 10,
                gen_tokens=500.0 + i * 50 + j * 5,
                total_tokens=1500.0 + i * 150 + j * 15,
                prompt_time_s=10.0, gen_time_s=20.0,
                peak_gen_tps=50.0 + j, peak_prompt_tps=100.0,
                context_max=4096, status="CLOSED",
            ))
        out[p.id] = ids
        session.commit()
    return out


class CompareSnapshotReuseTests(unittest.TestCase):
    """Functional: Compare endpoints consume the shared G02 snapshot."""

    def setUp(self):
        self.engine = memory_engine()
        with Session(self.engine) as s:
            self.ids = _seed(s)
        self._orig_engine = odb._engine
        self._orig_db_path = odb._db_path
        odb._engine = self.engine
        odb._db_path = "test.db"
        self.client = TestClient(app.create_app(demo=True))

    def tearDown(self):
        self.client.close()
        odb._engine = self._orig_engine
        odb._db_path = self._orig_db_path

    def _stats(self):
        return self.client.app.state.snapshots.stats()

    def test_candidates_endpoint_serves_model_files(self):
        response = self.client.get("/api/compare/models/candidates",
                                   params={"range": "7d"})
        self.assertEqual(response.status_code, 200)
        models = response.json()["models"]
        self.assertGreaterEqual(len(models), 3)
        for row in models:
            for field in ("key", "label", "model_ids", "color", "tokens",
                          "share", "sessions", "gen_tps", "active"):
                self.assertIn(field, row)

    def test_compare_endpoints_reuse_one_shared_snapshot(self):
        before = self._stats()
        p = next(iter(self.ids))
        keys = "|".join(str(m) for m in self.ids[p][:2])
        self.assertEqual(self.client.get(
            "/api/compare/models/candidates", params={"range": "7d"}).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/compare/models", params={"range": "7d", "keys": keys}).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/models", params={"range": "7d"}).status_code, 200)
        after = self._stats()
        self.assertEqual(after["builds"] - before["builds"], 1,
                         "one shared snapshot build for the (None, 7d) context")
        self.assertGreaterEqual(after["hits"] - before["hits"], 2,
                                "compare + models screens hit the same snapshot")

    def test_provider_scoped_context_is_a_distinct_snapshot_key(self):
        p = next(iter(self.ids))
        before = self._stats()
        self.assertEqual(self.client.get(
            "/api/compare/models/candidates",
            params={"range": "7d", "provider": p}).status_code, 200)
        after = self._stats()
        self.assertEqual(after["builds"] - before["builds"], 1)
        keys = "|".join(str(m) for m in self.ids[p][:2])
        self.assertEqual(self.client.get(
            "/api/compare/models",
            params={"range": "7d", "provider": p, "keys": keys}).status_code, 200)
        after2 = self._stats()
        self.assertEqual(after2["builds"], after["builds"],
                         "scoped compare reuses the scoped snapshot")
        self.assertGreaterEqual(after2["hits"], after["hits"] + 1)

    def test_compare_preserves_request_order_and_caps_at_five(self):
        p = next(iter(self.ids))
        ids = self.ids[p]
        reverse = "|".join(str(m) for m in reversed(ids[:2]))
        data = self.client.get("/api/compare/models",
                               params={"range": "7d", "keys": reverse}).json()
        self.assertEqual([m["key"] for m in data["models"]],
                         [str(m) for m in reversed(ids[:2])])
        all_ids = [m for prov in self.ids.values() for m in prov]
        overflow = "|".join(str(m) for m in all_ids[:7])
        data = self.client.get("/api/compare/models",
                               params={"range": "7d", "keys": overflow}).json()
        self.assertEqual(len(data["models"]), 5)

    def test_gpus_endpoint_is_the_separate_lazy_surface(self):
        p = next(iter(self.ids))
        keys = "|".join(str(m) for m in self.ids[p][:2])
        before = self._stats()
        response = self.client.get("/api/compare/models/gpus",
                                   params={"range": "7d", "keys": keys})
        self.assertEqual(response.status_code, 200)
        self.assertIn("gpus", response.json())
        after = self._stats()
        self.assertEqual(after["builds"], before["builds"],
                         "the GPU surface never touches the shared snapshot")

    def test_compare_page_renders_the_notice_hooks(self):
        html = self.client.get("/compare")
        self.assertEqual(html.status_code, 200)
        for hook in ('id="cmpCandNotice"', 'id="cmpNotice"'):
            self.assertIn(hook, html.text)


class CompareLifecycleSourceContractTests(unittest.TestCase):
    """Secondary guard: the client request lifecycle in app.js."""

    @classmethod
    def setUpClass(cls):
        cls.script = _read("static", "js", "app.js")

    def _body(self):
        start = self.script.index("function initCompare(meta)")
        end = self.script.index("/* ---", start)
        return self.script[start:end]

    def test_three_independent_request_identities(self):
        body = self._body()
        for pair in ("candCtl", "cmpCtl", "gpuCtl"):
            self.assertIn(pair, body)
        for pair in ("candSeq", "cmpSeq", "gpuSeq"):
            self.assertIn(pair, body)
        self.assertIn("const abortCand", body)
        self.assertIn("const abortCmp", body)
        self.assertIn("const abortGpu", body)

    def test_selection_debounce_is_120ms_and_selection_only(self):
        body = self._body()
        self.assertIn("const SELECTION_DEBOUNCE_MS = 120", body)
        self.assertIn("renderPick(); scheduleCompare();", body)
        # provider/range changes start immediately, through loadContext
        self.assertIn("st.provider = event.target.value; loadContext();", body)
        self.assertIn("loadContext();", body.split("segControl(\"cmpRange\"")[-1])
        # and the context restart cancels any pending selection debounce
        self.assertIn("cancelScheduledCompare()", body)
        self.assertNotIn("scheduleCompare()", body.split("const loadContext")[-1])

    def test_candidate_cache_is_bounded_lru(self):
        body = self._body()
        self.assertIn("const CAND_CACHE_MAX = 32", body)
        self.assertIn("const candCache = new Map()", body)
        self.assertIn("candCache.delete(key); candCache.set(key, hit)", body)
        self.assertIn("while (candCache.size > CAND_CACHE_MAX)", body)
        self.assertIn('candKey = () => (st.provider || "all") + "|" + st.range', body)

    def test_cache_hit_reuses_without_new_network_identity(self):
        body = self._body()
        hit = body.split("const hit = candCacheGet(key);")
        self.assertIn("onCandidates(hit.models); return Promise.resolve();", hit[1])

    def test_stale_response_suppressed_by_sequence_and_context(self):
        body = self._body()
        self.assertIn("if (seq !== candSeq) return;", body)
        self.assertIn("if (seq !== cmpSeq || ctxNow() !== ctx) return;", body)
        self.assertIn("if (seq !== gpuSeq || cmpSeqRef !== cmpSeq || ctxNow() !== ctx) return;", body)
        self.assertNotIn("if (requestId !== compareRequest)", body)

    def test_last_good_content_is_context_scoped(self):
        body = self._body()
        self.assertIn("lastCmp = { ctx: ctx, models: models };", body)
        self.assertIn("if (lastCmp && lastCmp.ctx === ctx) {", body)
        self.assertIn("showCmpError(\"Comparison failed to load. Please try again.\");", body)
        self.assertIn("showCmpError(\"The selected model files could not be compared in this range.\");", body)

    def test_failure_never_clears_the_selection(self):
        body = self._body()
        self.assertNotIn("st.selected = []", body)

    def test_gpu_is_lazy_on_its_own_identity(self):
        body = self._body()
        # the gpu row renders a loading placeholder, never data at table render
        self.assertIn("id=\"cmpGpu-' + i + '\">loading…</td>'", body)
        self.assertIn("loadGpu(models, ctx, seq);", body)
        # the gpu surface aborts/starts itself, never the compare surface
        self.assertIn("const loadGpu = (models, ctx, cmpSeqRef) => {\n    abortGpu();", body)
        self.assertNotIn("loadGpu()", body)

    def test_retry_is_scoped_to_its_own_surface(self):
        body = self._body()
        # GPU retry: delegated, re-fires only the gpu surface
        self.assertIn('event.target.closest("[data-gpu-retry]")', body)
        self.assertIn("loadGpu(lastCmp.models, lastCmp.ctx, cmpSeq)", body)
        # compare retry: only the compare surface
        self.assertIn('el("cmpTable").querySelector("button").onclick = () => loadCompare();', body)
        # candidate retry: only the candidate surface, forced
        self.assertIn("() => loadCandidates({ force: true })", body)

    def test_context_restart_cancels_all_three_surfaces(self):
        body = self._body()
        self.assertIn("const loadContext = () => {", body)
        ctx = body.split("const loadContext = () => {")[-1]
        ctx = ctx.split("fillSelect(")[0]
        self.assertIn("loadCandidates();", ctx)
        self.assertIn("loadCompare();", ctx)
        # loadCompare aborts compare + gpu; loadCandidates aborts candidates
        self.assertIn("abortCmp(); abortGpu();", body)
        self.assertIn("abortCand();", body)

    def test_evidence_probe_exposes_lifecycle_state(self):
        self.assertIn("window.__g07", self.script)
        self.assertIn("candCacheSize: candCache.size", self.script)
        self.assertIn("lastCmpCtx: lastCmp ? lastCmp.ctx : null", self.script)


class CompareLayoutCssContractTests(unittest.TestCase):
    """Secondary guard: the equal-width compare layout contract."""

    @classmethod
    def setUpClass(cls):
        cls.css = _read("static", "css", "app.css")

    def test_rail_is_clamped_280_to_320(self):
        self.assertIn(
            ".compare-layout { display: grid; grid-template-columns: "
            "clamp(280px, 22vw, 320px) minmax(0, 1fr);", self.css)

    def test_label_column_is_136px(self):
        self.assertIn(".cmp-label-col { width: 136px; }", self.css)
        self.assertIn("min-width: 136px", self.css)

    def test_equal_width_columns_for_each_selection_size(self):
        for n in (2, 3, 4, 5):
            self.assertIn(
                f".cmp-cols-{n} .cmp-model-col {{ width: calc((100% - 136px) / {n}); }}",
                self.css)

    def test_shared_minimum_width_scales_with_n(self):
        for n in (2, 3, 4, 5):
            self.assertIn(
                f".cmp-cols-{n} .cmp-data-table {{ min-width: calc(136px + {n} * 160px); }}",
                self.css)

    def test_legacy_fixed_minimums_are_gone(self):
        self.assertNotIn("min-width: 760px", self.css)
        self.assertNotIn("min-width: 170px", self.css)

    def test_error_and_retry_styles_present(self):
        for selector in (".compare-error", ".cmp-notice {",
                         ".cmp-notice[hidden]", ".cmp-gpu-retry"):
            self.assertIn(selector, self.css)


class CompareTemplateContractTests(unittest.TestCase):
    """Secondary guard: notice hooks and cache-bust bump."""

    def test_notice_hooks_present_with_capture_ignore(self):
        template = _read("templates", "compare.html")
        for hook in ('<div class="cmp-notice" id="cmpCandNotice" hidden '
                     'data-capture-ignore></div>',
                     '<div class="cmp-notice" id="cmpNotice" hidden '
                     'data-capture-ignore></div>'):
            self.assertIn(hook, template)

    def test_cache_bust_bumped_for_g07(self):
        # The asset query string moves forward with each shipped UI change; the
        # G09 automatic-pricing section is the latest to touch app.css / app.js.
        base = _read("templates", "base.html")
        self.assertIn("/static/css/app.css?v=20260907-sync-1", base)
        self.assertIn("/static/js/app.js?v=20260907-sync-1", base)
        self.assertIn("/static/js/charts.js?v=20260907-sync-1", base)


if __name__ == "__main__":
    unittest.main()
