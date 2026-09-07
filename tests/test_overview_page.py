"""Overview keeps its live cards outside the range filter.

The page carries two groups: NOW and INFERENCE TIME describe the present and
must ignore the period selector, while the twelve range cards mirror the Models
page. Those cards are fed from /api/models so the two pages cannot report
different numbers for the same range.
"""
from __future__ import annotations

import pathlib
import re
import unittest


class OverviewTemplateTests(unittest.TestCase):
    @staticmethod
    def _template() -> str:
        return (pathlib.Path(__file__).parents[1] / "templates" /
                "overview.html").read_text(encoding="utf-8")

    def test_live_cards_kept(self):
        html = self._template()
        self.assertIn("NOW", html)
        self.assertIn("INFERENCE TIME", html)

    def test_superseded_cards_removed(self):
        html = self._template()
        self.assertNotIn("TODAY TOKENS", html)
        self.assertNotIn("CONTEXT", html)
        # and their element ids must not linger for the JS to target
        for stale in ("ovTokVal", "ovSessSub", "ovCtxVal", "ovCtxBar", "ovCtxSub"):
            self.assertNotIn(stale, html, f"{stale} still in the template")

    def test_range_control_and_card_strip_exist(self):
        html = self._template()
        self.assertIn('id="ovRngSeg"', html)
        self.assertIn('id="ovCards"', html)

    def test_live_group_is_marked_separately(self):
        # The live pair must be distinguishable from the filtered strip, both
        # for styling and so a reader can tell which cards follow the filter.
        self.assertIn("ov-live", self._template())


class OverviewScriptTests(unittest.TestCase):
    @staticmethod
    def _script() -> str:
        return (pathlib.Path(__file__).parents[1] / "static" / "js" /
                "app.js").read_text(encoding="utf-8")

    def test_no_references_to_removed_elements(self):
        script = self._script()
        for stale in ("ovTokVal", "ovSessSub", "ovCtxVal", "ovCtxBar", "ovCtxSub"):
            self.assertNotIn(stale, script, f"{stale} still referenced in app.js")

    def test_cards_are_shared_with_models(self):
        # One renderer for both pages: duplicating it invites the two pages
        # drifting apart on the same numbers.
        script = self._script()
        self.assertIn("function topCardsHtml(", script)
        self.assertGreaterEqual(len(re.findall(r"topCardsHtml\(", script)), 3,
                                "expected the shared renderer plus both callers")

    def test_overview_cards_read_the_models_endpoint(self):
        self.assertIn('api("/api/models?range=" + ovSt.range', self._script())


if __name__ == "__main__":
    unittest.main()
