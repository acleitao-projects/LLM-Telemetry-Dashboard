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

    def test_selected_model_uses_stable_wide_capture_layout(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('data-capture-width="600"', script)
        self.assertIn("capturePanelAtWidth", script)

    def test_capture_uuid_has_plain_http_fallback(self):
        script_path = os.path.join(app.BASE_DIR, "static", "js", "app.js")
        with open(script_path, encoding="utf-8") as source:
            script = source.read()

        self.assertIn('typeof crypto.randomUUID === "function"', script)
        self.assertIn("crypto.getRandomValues(new Uint8Array(16))", script)
        self.assertIn("const captureId = newCaptureId()", script)

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
        self.assertIn('id="themeToggle"', template)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertIn('@media (max-width: 800px)', css)

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


if __name__ == "__main__":
    unittest.main()
