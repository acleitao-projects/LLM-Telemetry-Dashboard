/* Observatory - ECharts theme + helpers */
"use strict";

const OC = {
  bg: "transparent",
  border: "#2a2a27",
  split: "#21211e",
  axis: "#343431",
  label: "#74736e",
  text: "#a6a49d",
  blue: "#4b8de8",
  orange: "#d8733e",
  green: "#48a77c",
  amber: "#d29b25",
};

function baseOption() {
  return {
    backgroundColor: OC.bg,
    grid: { left: 40, right: 10, top: 12, bottom: 22 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#1e1e1b",
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

function areaStackOption(labels, series, unit) {
  const o = baseOption();
  o.xAxis.data = labels;
  o.series = series.map((s) => ({
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
      name: s.name, type: "bar", data: s.data, barMaxWidth: 14,
      itemStyle: { color: s.color || OC.blue, borderRadius: [0, 2, 2, 0] },
    }));
  }
  return o;
}

function sparkline(el, data, color, area = true) {
  if (!el || typeof echarts === "undefined") return null;
  const ch = echarts.init(el, null, { renderer: "canvas" });
  ch.setOption({
    backgroundColor: OC.bg,
    grid: { left: 0, right: 0, top: 2, bottom: 0 },
    xAxis: { type: "category", show: false, data: data.map((_, i) => i) },
    yAxis: { type: "value", show: false },
    series: [{
      type: "line", data, showSymbol: false, smooth: 0.2,
      lineStyle: { width: 1, color },
      itemStyle: { color },
      areaStyle: area ? { color: color + "26" } : undefined,
    }],
    tooltip: { show: false },
  });
  return ch;
}

function registerChart(el, option) {
  if (!el || typeof echarts === "undefined") return null;
  const ch = echarts.init(el, null, { renderer: "canvas" });
  ch.setOption(option);
  window.__charts = window.__charts || [];
  window.__charts.push(ch);
  return ch;
}

function fmtTokens(n) {
  if (n == null || isNaN(n)) return "-";
  n = Number(n);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}

function fmtDur(s) {
  if (s == null || isNaN(s)) return "-";
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h < 48) return h + "h " + m + "m";
  return Math.floor(h / 24) + "d " + (h % 24) + "h";
}

function fmtTps(v) {
  if (v == null || isNaN(v)) return "-";
  return Number(v).toFixed(1) + " t/s";
}

function fmtPct(v, digits = 1) {
  if (v == null || isNaN(v)) return "-";
  return Number(v).toFixed(digits) + "%";
}

function fmtNum(v) {
  if (v == null || isNaN(v)) return "-";
  return Number(v).toLocaleString("en-US");
}

function fmtClock(ms, withSec) {
  if (!ms) return "-";
  const d = new Date(ms);
  const p = (x) => String(x).padStart(2, "0");
  let s = p(d.getHours()) + ":" + p(d.getMinutes());
  if (withSec) s += ":" + p(d.getSeconds());
  return s;
}

function fmtDate(ms) {
  if (!ms) return "-";
  const d = new Date(ms);
  const p = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function fmtAgo(ms, now) {
  if (!ms) return "never";
  const s = Math.max(0, (now - ms) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function el(id) { return document.getElementById(id); }

function api(path) {
  return fetch(path).then((r) => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}

function segControl(containerId, options, active, onPick) {
  const box = el(containerId);
  if (!box) return;
  box.innerHTML = "";
  options.forEach((opt) => {
    const d = document.createElement("span");
    d.className = "seg-item" + (opt === active ? " active" : "");
    d.textContent = opt;
    d.onclick = () => {
      box.querySelectorAll(".seg-item").forEach((x) => x.classList.remove("active"));
      d.classList.add("active");
      onPick(opt);
    };
    box.appendChild(d);
  });
}
