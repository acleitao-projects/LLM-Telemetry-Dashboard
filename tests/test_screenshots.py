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
