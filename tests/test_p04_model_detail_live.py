"""P04: Model Detail patches in place on live updates (issue #32, frontend half).

Source contracts for the Model Detail half of the P04 batch. The Models
runtime-card and duplicate-spark halves shipped in PR #55; this covers what was
deliberately left out of it.

The behaviour these pin down is proved end to end by
``tests/p04_model_detail_evidence.mjs``, which drives a real browser and
asserts canvas identity across live updates. These tests are the cheap
regression guard that runs in CI without a browser.
"""
from __future__ import annotations

import os
import re
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as source:
        return source.read()


def _model_detail_body(app_js: str) -> str:
    """The source of initModelDetail, up to the next top-level function."""
    start = app_js.index("function initModelDetail(")
    rest = app_js[start:]
    match = re.search(r"\n/\* -+ [a-z]", rest)
    return rest[:match.start()] if match else rest


class ModelDetailLiveUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read("static", "js", "app.js")
        cls.body = _model_detail_body(cls.app)

    def test_live_update_does_not_tear_down_the_charts(self):
        """The whole point: an SSE tick must not dispose this page's charts."""
        self.assertNotIn("ch.clear()", self.body,
                         "Model Detail must not dispose its charts on a refresh")

    def test_charts_are_re_added_so_the_registry_reuses_them(self):
        # ChartRegistry.init returns the instance already bound to the node, so
        # re-adding is an in-place setOption rather than a rebuild.
        for node in ("mdlTokChart", "mdlSpeed", "mdlCtx", "mdlHw"):
            self.assertIn('ch.add(el("%s")' % node, self.body)

    def test_markup_is_assigned_only_when_it_changed(self):
        self.assertIn("const lastHtml = new Map();", self.body)
        self.assertIn("if (!node || lastHtml.get(node) === html) return;", self.body)
        for node in ("mdlHead", "mdlCards", "mdlAccBar", "mdlAccLegend", "mdlGpuCards"):
            self.assertIn('setHtml(el("%s")' % node, self.body,
                          "%s should be patched, not reassigned blindly" % node)

    def test_no_raw_innerhtml_assignment_on_patched_nodes(self):
        for node in ("mdlHead", "mdlCards", "mdlAccBar", "mdlAccLegend"):
            self.assertNotIn('el("%s").innerHTML =' % node, self.body)

    def test_mtp_panel_swap_is_treated_as_structural(self):
        """Swapping a chart for an empty state must dispose the instance."""
        self.assertIn("let mtpMode = null;", self.body)
        self.assertIn("ch.drop(mtpNode);", self.body)
        self.assertIn('if (mtpMode !== "chart") { mtpNode.innerHTML = ""', self.body)

    def test_config_panel_is_gated_on_the_config_changing(self):
        self.assertIn("if (nextCfgKey !== cfgKey)", self.body)

    def test_config_history_visibility_is_set_both_ways(self):
        # It used to only ever hide, so a model that gained a second config
        # while the page was open kept the table hidden.
        self.assertIn('style.display = many ? "" : "none"', self.body)


class ChartCollectionTests(unittest.TestCase):
    """makeCharts tracks a surface once, however many times it refreshes."""

    @classmethod
    def setUpClass(cls):
        cls.app = _read("static", "js", "app.js")

    def test_add_does_not_record_the_same_instance_twice(self):
        self.assertIn("const track = (c) => { if (c && !list.includes(c)) list.push(c); return c; };",
                      self.app)
        self.assertIn("add(box, opt) { return track(registerChart(box, opt)); },", self.app)
        self.assertIn("spark(box, data, color) { return track(sparkline(box, data, color)); },",
                      self.app)

    def test_drop_disposes_one_node_and_forgets_it(self):
        self.assertIn("drop(box) {", self.app)
        self.assertIn("ChartRegistry.dispose(box);", self.app)
        self.assertIn("if (i >= 0) list.splice(i, 1);", self.app)


if __name__ == "__main__":
    unittest.main()
