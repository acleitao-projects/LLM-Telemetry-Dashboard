from __future__ import annotations

import pathlib
import unittest


class DeployScriptTests(unittest.TestCase):
    def test_health_check_uses_lightweight_meta_endpoint(self):
        script = (pathlib.Path(__file__).parents[1] / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("HEALTH_URL=http://127.0.0.1:8090/api/meta", script)
        self.assertNotIn("HEALTH_URL=http://127.0.0.1:8090/api/overview", script)


if __name__ == "__main__":
    unittest.main()
