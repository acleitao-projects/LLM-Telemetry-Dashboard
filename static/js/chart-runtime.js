/* Observatory - chart runtime: ECharts option builders + bounded shared chart lifecycle.
   Loaded only on pages that create charts, after echarts.min.js. */
"use strict";

/* ------------------------------------------------------------ chart registry
   One registry owns every ECharts instance on the page: initialization,
   lookup/update, resize, and disposal. Instances are keyed by their stable
   DOM node and reused across refreshes; updates call setOption. Only
   structural teardown disposes an instance, and disposal removes it from
   every registry/resize collection. A single window resize listener is
   bound lazily on the first chart and never duplicated. */
const ChartRegistry = (function () {
  const instances = new Map(); // DOM node -> { chart }
  let resizeBound = false;
  const onResize = () => {
    instances.forEach((entry) => { try { entry.chart.resize(); } catch (e) {} });
  };
  const bindResize = () => {
    if (resizeBound) return;
    resizeBound = true;
    window.addEventListener("resize", onResize);
  };
  const safe = (fn) => { try { fn(); } catch (e) {} };
  return {
    // Init or reuse the instance bound to el, applying option in place.
    init(el, option) {
      if (!el || typeof echarts === "undefined") return null;
      let entry = instances.get(el);
      if (!entry) {
        entry = { chart: echarts.init(el, null, { renderer: "canvas" }) };
        instances.set(el, entry);
        bindResize();
      }
      if (option) {
        safe(() => entry.chart.setOption(option, { replaceMerge: ["series"] }));
      }
      return entry.chart;
    },
    // Update an existing instance; null when el has no live chart.
    update(el, option) {
      const entry = instances.get(el);
      if (!entry || typeof entry.chart.isDisposed === "function" && entry.chart.isDisposed()) return null;
      safe(() => entry.chart.setOption(option, { replaceMerge: ["series"] }));
      return entry.chart;
    },
    get(el) {
      const entry = instances.get(el);
      return entry ? entry.chart : null;
    },
    canvas(el) {
      const entry = instances.get(el);
      if (!entry) return null;
      return entry.chart.getDom().querySelector("canvas") || null;
    },
    has(el) { return instances.has(el); },
    size() { return instances.size; },
    // 1 while the shared resize listener is bound, else 0.
    resizeCount() { return resizeBound ? 1 : 0; },
    resizeAll() { onResize(); },
    // Structural teardown only: dispose the instance and drop it from every
    // registry/resize collection.
    dispose(el) {
      const entry = instances.get(el);
      if (!entry) return;
      safe(() => { if (entry.chart.isDisposed()) return; entry.chart.dispose(); });
      instances.delete(el);
    },
    disposeAll() {
      Array.from(instances.keys()).forEach((node) => this.dispose(node));
    },
  };
})();
window.__chartRegistry = ChartRegistry;

/* ------------------------------------------------------- option builders */
function baseOption() {
  return {
    backgroundColor: OC.bg,
    grid: { left: 40, right: 10, top: 12, bottom: 22 },
    tooltip: {
      trigger: "axis",
      backgroundColor: LIGHT_THEME ? "#ffffff" : "#1e1e1b",
      borderColor: OC.border,
      borderWidth: 1,
      padding: [6, 10],
      textStyle: { color: OC.text, fontSize: 11 },
      axisPointer: { lineStyle: { color: OC.border } },
    },
    xAxis: {
      type: "category",
      axisLine: { lineStyle: { color: OC.axis } },
      axisTick: { show: false },
      axisLabel: { color: OC.label, fontSize: 9.5, hideOverlap: true },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: OC.split } },
      axisLabel: { color: OC.label, fontSize: 9.5 },
      axisLine: { show: false },
    },
  };
}

function lineOption(labels, series) {
  // series: [{name, color, data, area?}]
  const o = baseOption();
  o.xAxis.data = labels;
  o.series = series.map((s, i) => ({
    id: s.id || s.name,
    name: s.name,
    type: "line",
    data: s.data,
    showSymbol: false,
    smooth: 0.15,
    connectNulls: false,
    lineStyle: { width: 1, color: s.color },
    itemStyle: { color: s.color },
    areaStyle: s.area ? { color: s.color + "22" } : undefined,
    emphasis: { focus: "series" },
    z: 10 - i,
  }));
  o.legend = series.length > 1 ? {
    top: 0, right: 0, itemWidth: 8, itemHeight: 6,
    textStyle: { color: OC.label, fontSize: 9.5 },
  } : undefined;
  if (o.legend) o.grid.top = 22;
  o.tooltip.valueFormatter = (v) => (v == null ? "-" : v);
  return o;
}

function areaStackOption(labels, series) {
  const o = baseOption();
  o.xAxis.data = labels;
  o.series = series.map((s) => ({
    id: s.id || s.name,
    name: s.name, type: "line", stack: "tok", data: s.data,
    showSymbol: false, smooth: 0.1, lineStyle: { width: 1, color: s.color },
    itemStyle: { color: s.color },
    areaStyle: { color: s.color + "30" },
    emphasis: { focus: "series" },
  }));
  o.legend = { top: 0, right: 0, itemWidth: 8, itemHeight: 6,
    textStyle: { color: OC.label, fontSize: 9.5 } };
  o.grid.top = 22;
  return o;
}

function barOption(labels, series, opts = {}) {
  const o = baseOption();
  o.xAxis.data = labels;
  o.series = series.map((s) => ({
    id: s.id || s.name,
    name: s.name, type: "bar", data: s.data,
    barMaxWidth: 26,
    itemStyle: { color: s.color || OC.blue, borderRadius: [2, 2, 0, 0] },
  }));
  if (opts.horizontal) {
    o.xAxis = { type: "value", splitLine: { lineStyle: { color: OC.split } },
      axisLabel: { color: OC.label, fontSize: 9.5 }, axisLine: { show: false } };
    o.yAxis = { type: "category", data: labels,
      axisLabel: { color: OC.text, fontSize: 10 }, axisLine: { show: false },
      axisTick: { show: false } };
    o.grid = { left: 110, right: 30, top: 6, bottom: 18 };
    o.series = series.map((s) => ({
      id: s.id || s.name,
      name: s.name, type: "bar", data: s.data, barMaxWidth: 14,
      itemStyle: { color: s.color || OC.blue, borderRadius: [0, 2, 2, 0] },
    }));
  }
  return o;
}

function dailyVolumeOption(labels, volume) {
  const o = baseOption();
  const inference = volume.inference_seconds || [];
  const prompt = volume.prompt_tokens || [];
  const generated = volume.generated_tokens || [];
  const unclassified = volume.unclassified_tokens || [];
  o.grid = { left: 54, right: 62, top: 30, bottom: 28 };
  o.xAxis.data = labels;
  o.xAxis.axisLabel = { color: OC.label, fontSize: 9.5, hideOverlap: true };
  o.yAxis = [
    { type: "value", name: "time", splitLine: { lineStyle: { color: OC.split } },
      axisLabel: { color: OC.label, fontSize: 9.5, formatter: (value) => fmtDur(value) },
      axisLine: { show: false }, nameTextStyle: { color: OC.label, fontSize: 9.5 } },
    { type: "value", name: "tokens", splitLine: { show: false },
      axisLabel: { color: OC.label, fontSize: 9.5, formatter: (value) => fmtTokens(value) },
      axisLine: { show: false }, nameTextStyle: { color: OC.label, fontSize: 9.5 } },
  ];
  o.legend = { top: 0, right: 0, itemWidth: 8, itemHeight: 7,
    textStyle: { color: OC.label, fontSize: 9.5 } };
  o.tooltip.formatter = (items) => {
    const date = items[0] ? items[0].axisValueLabel : "";
    return date + items.map((item) => "<br/>" + item.marker + item.seriesName + ": " +
      (item.seriesName === "Inference time" ? fmtDur(item.value) : fmtTokens(item.value))).join("");
  };
  o.series = [
    { id: "Inference time", name: "Inference time", type: "bar", yAxisIndex: 0, data: inference,
      barMaxWidth: 22, itemStyle: { color: OC.green, borderRadius: [2, 2, 0, 0] } },
    { id: "Prompt tokens", name: "Prompt tokens", type: "bar", yAxisIndex: 1, stack: "tokens", data: prompt,
      barMaxWidth: 22, itemStyle: { color: OC.blue, borderRadius: [0, 0, 0, 0] } },
    { id: "Generated tokens", name: "Generated tokens", type: "bar", yAxisIndex: 1, stack: "tokens", data: generated,
      barMaxWidth: 22, itemStyle: { color: OC.orange, borderRadius: [2, 2, 0, 0] } },
    { id: "Total tokens (unsplit)", name: "Total tokens (unsplit)", type: "bar", yAxisIndex: 1, stack: "tokens", data: unclassified,
      barMaxWidth: 22, itemStyle: { color: OC.label, borderRadius: [2, 2, 0, 0] } },
  ];
  return o;
}

/* ---------------------------------------------------- registry adapters */
function sparkline(el, data, color, area = true) {
  if (!el || typeof echarts === "undefined") return null;
  return ChartRegistry.init(el, {
    backgroundColor: OC.bg,
    grid: { left: 0, right: 0, top: 2, bottom: 0 },
    xAxis: { type: "category", show: false, data: data.map((_, i) => i) },
    yAxis: { type: "value", show: false },
    series: [{
      id: "spark",
      type: "line", data, showSymbol: false, smooth: 0.2,
      lineStyle: { width: 1, color },
      itemStyle: { color },
      areaStyle: area ? { color: color + "26" } : undefined,
    }],
    tooltip: { show: false },
  });
}

function registerChart(el, option) {
  return ChartRegistry.init(el, option);
}
