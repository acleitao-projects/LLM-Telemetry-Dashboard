"""G06: Shared chart registry/lifecycle + Overview adoption (issues #12, #13).

Source contracts for the bounded shared chart lifecycle (chart-runtime.js
registry, app.js adoption, removal of the unbounded window.__charts array,
in-place Overview updates with stable-key recent rows) plus server-side
conditional asset loading: ECharts and the chart runtime only ship on pages
that draw charts.
"""
from __future__ import annotations

import os
import re
import unittest

from fastapi.testclient import TestClient

import app

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(BASE_DIR, *parts), encoding="utf-8") as source:
        return source.read()


CHART_PAGES = ["/overview", "/models", "/model/1", "/session/1", "/hardware"]
PLAIN_PAGES = ["/sessions", "/compare", "/settings"]


class ChartRegistrySourceContractTests(unittest.TestCase):
    """chart-runtime.js owns a bounded, Map-backed chart lifecycle."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = _read("static", "js", "chart-runtime.js")

    def test_registry_is_map_backed_and_bounded(self):
        self.assertIn("const instances = new Map()", self.runtime)
        self.assertIn("instances.set(el, entry)", self.runtime)
        self.assertIn("instances.delete(el)", self.runtime)
        self.assertIn("size() { return instances.size; }", self.runtime)

    def test_init_reuses_instance_and_updates_in_place(self):
        self.assertIn("init(el, option)", self.runtime)
        self.assertIn("let entry = instances.get(el)", self.runtime)
        self.assertIn(
            'entry.chart.setOption(option, { replaceMerge: ["series"] })',
            self.runtime)

    def test_single_lazy_resize_listener(self):
        self.assertIn("let resizeBound = false", self.runtime)
        self.assertIn("if (resizeBound) return", self.runtime)
        self.assertIn('window.addEventListener("resize", onResize)', self.runtime)
        self.assertIn("resizeCount() { return resizeBound ? 1 : 0; }", self.runtime)

    def test_dispose_leaves_every_collection(self):
        self.assertIn("entry.chart.dispose()", self.runtime)
        self.assertIn("instances.delete(el)", self.runtime)
        self.assertIn("disposeAll()", self.runtime)

    def test_stable_series_ids_and_22px_daily_bars(self):
        self.assertIn("barMaxWidth: 22", self.runtime)
        self.assertNotIn("barMaxWidth: 18", self.runtime)
        self.assertIn("id: s.id || s.name", self.runtime)
        self.assertIn('id: "Inference time"', self.runtime)
        self.assertIn('id: "Prompt tokens"', self.runtime)
        self.assertIn('id: "Generated tokens"', self.runtime)
        self.assertIn('id: "Total tokens (unsplit)"', self.runtime)

    def test_registry_is_exposed_for_evidence(self):
        self.assertIn("window.__chartRegistry = ChartRegistry", self.runtime)


class ChartHelperRelocationTests(unittest.TestCase):
    """Builders moved to chart-runtime.js; shared helpers stay in charts.js."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = _read("static", "js", "chart-runtime.js")
        cls.charts = _read("static", "js", "charts.js")

    def test_builders_left_charts_js(self):
        for name in ("baseOption", "lineOption", "areaStackOption", "barOption",
                     "dailyVolumeOption", "sparkline", "registerChart"):
            self.assertNotIn("function " + name, self.charts)

    def test_builders_live_in_chart_runtime(self):
        for name in ("baseOption", "lineOption", "areaStackOption", "barOption",
                     "dailyVolumeOption", "sparkline", "registerChart"):
            self.assertIn("function " + name, self.runtime)

    def test_shared_helpers_stay_in_charts_js(self):
        for helper in ("const OC", "const LIGHT_THEME", "function api",
                       "const _apiInflight", "function el(", "function segControl"):
            self.assertIn(helper, self.charts)


class AppAdoptionContractTests(unittest.TestCase):
    """app.js routes every chart through the shared registry."""

    @classmethod
    def setUpClass(cls):
        cls.script = _read("static", "js", "app.js")

    def test_no_unbounded_global_chart_array(self):
        self.assertNotIn("window.__charts", self.script)

    def test_no_direct_echarts_init_and_no_private_resize_listeners(self):
        self.assertNotIn("echarts.init(", self.script)
        # only remaining resize bindings may be capture-layout dispatches
        for line in self.script.splitlines():
            if "addEventListener(\"resize\"" in line:
                self.fail("private resize listener remains: " + line.strip())

    def test_makecharts_clear_disposes_through_registry(self):
        self.assertIn("ChartRegistry.dispose(c.getDom())", self.script)

    def test_runtime_chart_uses_registry(self):
        self.assertIn("runtimeChart = ChartRegistry.init(box, {", self.script)
        self.assertIn("ChartRegistry.dispose(dom)", self.script)

    def test_hardware_toggle_resizes_through_registry(self):
        self.assertIn("if (detail.open) ChartRegistry.resizeAll();", self.script)
        self.assertNotIn("ch.list.forEach((chart) => { try { chart.resize()", self.script)

    def test_dual_axis_series_have_stable_ids(self):
        self.assertIn("id: s.name, name: s.name, type: \"line\", data: s.data, yAxisIndex: s.y || 0",
                      self.script)


class OverviewInPlaceContractTests(unittest.TestCase):
    """Overview refreshes in place; no chart clearing, stable row keys."""

    @classmethod
    def setUpClass(cls):
        cls.script = _read("static", "js", "app.js")

    def _overview_body(self):
        start = self.script.index("function initOverview(")
        end = self.script.index("/* ---", start)
        return self.script[start:end]

    def test_overview_does_not_clear_charts_each_refresh(self):
        body = self._overview_body()
        self.assertNotIn("ch.clear()", body)
        self.assertNotIn("makeCharts()", body)
        self.assertIn('ovSetChart("ovUsage"', body)
        self.assertIn('ovSetChart("ovDaily"', body)
        self.assertIn("setInterval(load, 7000)", body)

    def test_empty_state_keeps_canvas_registered_and_hidden(self):
        self.assertIn('canvas.style.display = "none"', self.script)
        self.assertIn('node.className = "ov-empty empty"', self.script)
        self.assertIn("box.appendChild(node)", self.script)
        # empty state never wipes the chart box contents
        self.assertNotIn('el("ovUsage").innerHTML', self.script)
        self.assertNotIn('el("ovDaily").innerHTML', self.script)

    def test_recent_rows_use_stable_keys_and_one_delegated_handler(self):
        start = self.script.index("function renderOvRecent(")
        end = self.script.index("function initOverview(", start)
        body = self.script[start:end]
        self.assertIn("tr[data-id]", body)
        self.assertIn("tb.__ovRecentBound", body)
        self.assertIn('ev.target.closest("tr[data-id]")', body)
        # no per-row handler rebinding in the recent-sessions patcher
        self.assertNotIn("tr.onclick", body)

    def test_stale_empty_row_cannot_survive_empty_to_data_refresh(self):
        start = self.script.index("function renderOvRecent(")
        end = self.script.index("function initOverview(", start)
        body = self.script[start:end]
        # The empty-state <tr> carries no data-id, so the keyed-row cleanup
        # (tr[data-id]) cannot see it. It must be removed before both the
        # empty branch and the keyed-row append; otherwise an empty -> data
        # refresh leaves "no sessions yet" above the real rows.
        marker = "tr:not([data-id])"
        self.assertIn(marker, body)
        self.assertLess(body.index(marker), body.index("tb.innerHTML"))
        self.assertLess(body.index(marker), body.index("rows.forEach"))

    def test_live_handler_only_touches_the_live_cards(self):
        # The TODAY TOKENS card this used to assert on was removed in #62: the
        # range-filtered Tokens card supersedes it. The contract that still
        # matters is that the SSE handler patches the live cards and does not
        # re-render the range strip, which is period data on its own cadence.
        body = self._overview_body()
        start = body.index("window.__onLive")
        handler = body[start:body.index("};", start)]
        self.assertIn("renderOvTop(d)", handler)
        self.assertNotIn("renderCards", handler)
        self.assertNotIn("ovCards", handler)


class ConditionalChartAssetTests(unittest.TestCase):
    """ECharts + chart runtime ship only on chart-drawing pages."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app.create_app())

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def _script_srcs(self, path):
        html = self.client.get(path)
        self.assertEqual(html.status_code, 200, path)
        return re.findall(r'<script src="([^"]+)"', html.text)

    def test_every_page_keeps_shared_helpers_and_app(self):
        for path in CHART_PAGES + PLAIN_PAGES:
            srcs = self._script_srcs(path)
            self.assertTrue(any(s.startswith("/static/js/charts.js?v=") for s in srcs), path)
            self.assertTrue(any(s.startswith("/static/js/app.js?v=") for s in srcs), path)

    def test_chart_pages_load_echarts_and_runtime_in_order(self):
        for path in CHART_PAGES:
            html = self.client.get(path).text
            self.assertIn("/static/js/echarts.min.js", html, path)
            self.assertIn("/static/js/chart-runtime.js", html, path)
            self.assertLess(html.index("/static/js/echarts.min.js"),
                            html.index("/static/js/chart-runtime.js"), path)
            self.assertLess(html.index("/static/js/chart-runtime.js"),
                            html.index("/static/js/charts.js?v="), path)
            self.assertLess(html.index("/static/js/charts.js?v="),
                            html.index("/static/js/app.js?v="), path)

    def test_non_chart_pages_ship_no_chart_assets(self):
        for path in PLAIN_PAGES:
            html = self.client.get(path).text
            self.assertNotIn("echarts.min.js", html, path)
            self.assertNotIn("chart-runtime.js", html, path)

    def test_chart_runtime_static_file_is_served(self):
        response = self.client.get("/static/js/chart-runtime.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()
