from __future__ import annotations

import base64
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

import app


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ScreenshotTests(unittest.TestCase):
    def test_capture_assets_are_cache_busted(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "base.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()

        self.assertIn('/static/css/app.css?v=', template)
        self.assertIn('/static/js/app.js?v=', template)

    def test_topbar_unload_action_has_confirmation_controls(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "base.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('id="unloadModels"', template)
        self.assertIn('id="unloadDialog"', template)
        self.assertIn('id="unloadConfirm"', template)
        self.assertIn('fetch("/api/models/unload-all", { method: "POST" })', script)

    def test_compare_header_uses_the_data_table_columns(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('class="data-table cmp-data-table"', script)
        self.assertIn('class="cmp-label-head"', script)
        self.assertIn('<thead><tr><th class="cmp-label-head"></th>', script)
        self.assertNotIn('class="cmp-model-header"', script)

    def test_selected_model_uses_stable_wide_capture_layout(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('data-capture-width="600"', script)
        self.assertIn("capturePanelAtWidth", script)
        self.assertIn("capturePanel(target, false)", script)
        self.assertIn('target.style.overflow = "hidden"', script)

    def test_models_runtime_prefers_fresh_sse_snapshot(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()
        self.assertIn("let selectedRealtime = null", script)
        self.assertIn("const runtime = selectedRealtime || s.realtime", script)

    def test_capture_uuid_has_plain_http_fallback(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('typeof crypto.randomUUID === "function"', script)
        self.assertIn("crypto.getRandomValues(new Uint8Array(16))", script)
        self.assertIn("const captureId = newCaptureId()", script)

    def test_compare_table_has_a_stable_capture_target(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "compare.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()

        self.assertIn('id="cmpCapture"', template)
        self.assertIn('data-capture-target="cmpCapture"', template)
        self.assertIn('data-capture-width="1200"', template)
        self.assertIn('data-capture-ignore', template)

    def test_models_screen_capture_targets_main_area_without_sidebar(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "models.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()
        base_path = os.path.join(app.BASE_DIR, "templates", "base.html")
        with open(base_path, encoding="utf-8") as source:
            base = source.read()

        self.assertIn('data-capture-target="pageContent"', template)
        self.assertIn('data-capture-width="1800"', template)
        self.assertIn('data-capture-ignore', template)
        self.assertIn('class="content" id="pageContent"', base)

    def test_overview_has_a_content_only_capture_button(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "overview.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()

        self.assertIn('data-capture-target="pageContent"', template)
        self.assertIn('CAPTURE SCREEN', template)
        self.assertIn('data-capture-ignore', template)

    def test_models_grouping_uses_the_loaded_canonical_payload(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('const groupedRows = (sourceRows)', script)
        self.assertIn('st.sourceRows = d.rows || []', script)
        self.assertIn('renderGroups();', script)
        self.assertIn('const load = () => api("/api/models?range=" + st.range + "&group=model")', script)

    def test_overview_uses_full_width_daily_volume_chart(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "overview.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()
        chart_path = os.path.join(app.BASE_DIR, "static", "js", "chart-runtime.js")
        with open(chart_path, encoding="utf-8") as source:
            charts = source.read()

        self.assertIn('id="ovDaily"', template)
        self.assertNotIn('id="ovInf"', template)
        self.assertNotIn('id="ovTok"', template)
        self.assertIn('dailyVolumeOption', script)
        self.assertIn('function dailyVolumeOption', charts)
        self.assertIn('name: "Inference time"', charts)
        self.assertIn('name: "Prompt tokens"', charts)
        self.assertIn('name: "Generated tokens"', charts)
        self.assertIn('name: "Total tokens (unsplit)"', charts)
        self.assertIn('setInterval(load, 7000)', script)

    def test_capture_uses_visible_preview_instead_of_popup(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()
        template_path = os.path.join(app.BASE_DIR, "templates", "base.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()

        self.assertIn('el("captureDialog")', script)
        self.assertIn('dialog.showModal()', script)
        self.assertNotIn("window.open(waitUrl", script)
        self.assertIn('id="captureDownload"', template)

    def test_responsive_theme_and_sidebar_controls_are_present(self):
        template_path = os.path.join(app.BASE_DIR, "templates", "base.html")
        with open(template_path, encoding="utf-8") as source:
            template = source.read()
        css_path = os.path.join(app.BASE_DIR, "static", "css", "app.css")
        with open(css_path, encoding="utf-8") as source:
            css = source.read()

        self.assertIn('id="sidebarToggle"', template)
        self.assertIn('id="mobileMenu"', template)
        self.assertIn('id="mobileNavBackdrop"', template)
        self.assertIn('id="themeToggle"', template)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertIn('@media (max-width: 800px)', css)
        self.assertIn('.mobile-nav-open .sidebar', css)

    def test_selected_gauge_capture_preserves_ratio_and_detail_clearance(self):
        css_path = os.path.join(app.BASE_DIR, "static", "css", "app.css")
        with open(css_path, encoding="utf-8") as source:
            css = source.read()
        selector = ".runtime-context .resource-gauge svg"
        start = css.index(selector)
        rule = css[start:css.index("}", start)]

        self.assertIn("aspect-ratio: 100 / 84", rule)
        self.assertIn("margin: -6px auto 4px", rule)
        self.assertNotIn(".runtime-layout { grid-template-columns: 1fr; }", css)

    def test_default_capture_directory_follows_database_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "observatory.db")
            with patch.object(app, "SCREENSHOT_DIR", None), patch.object(
                app.odb, "get_db_path", return_value=db_path
            ):
                self.assertEqual(
                    app._screenshot_dir(), os.path.join(directory, "screenshots")
                )

    def test_png_upload_wait_redirect_and_image_response(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "SCREENSHOT_DIR", directory):
            capture_id = str(uuid4())
            with TestClient(app.create_app()) as client:
                saved = client.put(f"/api/screenshots/{capture_id}", content=PNG_1X1)
                self.assertEqual(saved.status_code, 200)
                shown = client.get(f"/screenshots/{capture_id}/wait")
                self.assertEqual(shown.status_code, 200)
                self.assertEqual(shown.headers["content-type"], "image/png")
                self.assertEqual(shown.content, PNG_1X1)

    def test_rejects_non_png_capture(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "SCREENSHOT_DIR", directory):
            with TestClient(app.create_app()) as client:
                response = client.put(f"/api/screenshots/{uuid4()}", content=b"not an image")
                self.assertEqual(response.status_code, 400)

    def test_daily_cleanup_removes_only_expired_screenshots(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "SCREENSHOT_DIR", directory):
            old_path = os.path.join(directory, f"{uuid4()}.png")
            current_path = os.path.join(directory, f"{uuid4()}.png")
            unrelated_path = os.path.join(directory, "keep.txt")
            for path in (old_path, current_path, unrelated_path):
                with open(path, "wb") as output:
                    output.write(b"x")
            now = time.time()
            os.utime(old_path, (now - app.SCREENSHOT_TTL_S - 1, now - app.SCREENSHOT_TTL_S - 1))

            app._cleanup_screenshots(now=now)

            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(current_path))
            self.assertTrue(os.path.exists(unrelated_path))


class BrowserCorrectnessTests(unittest.TestCase):
    def test_api_dedup_prevents_concurrent_duplicate_requests(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "charts.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()
        self.assertIn("_apiInflight", script)
        self.assertIn("const existing = _apiInflight.get(path)", script)
        self.assertIn("_apiInflight.set(path, p)", script)
        self.assertIn("_apiInflight.delete(path)", script)

    def test_sse_unrepresented_has_cooldown_and_inflight_guard(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()
        self.assertIn("_reconcileTimer", script)
        self.assertIn("_reconciling", script)
        self.assertIn("nowMs - _reconcileTimer >= 30000", script)
        self.assertIn("!_reconciling", script)

    def test_render_selected_clears_prior_model_state(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()
        self.assertIn("selChart = null", script)
        self.assertIn("disposeRuntimeChart()", script)
        self.assertIn("runtimeSeries = { sessionId: null", script)
        self.assertIn("selectedRealtime = null", script)
        self.assertIn("selectedWasActive = false", script)


if __name__ == "__main__":
    unittest.main()
