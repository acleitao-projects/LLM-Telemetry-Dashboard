/* Observatory - shared helpers: theme colors, formatters, deduplicated api,
   segment controls. Loaded on every page. Chart-specific option builders and
   the shared chart lifecycle registry live in chart-runtime.js and load only
   on pages that create charts. */
"use strict";

const LIGHT_THEME = document.documentElement.dataset.theme === "light";
const OC = {
  bg: "transparent",
  border: LIGHT_THEME ? "#d5d8dc" : "#2a2a27",
  split: LIGHT_THEME ? "#e3e5e8" : "#21211e",
  axis: LIGHT_THEME ? "#b8bdc4" : "#343431",
  label: LIGHT_THEME ? "#747b84" : "#74736e",
  text: LIGHT_THEME ? "#4e545b" : "#a6a49d",
  blue: LIGHT_THEME ? "#245fb5" : "#4b8de8",
  orange: LIGHT_THEME ? "#b74f1f" : "#d8733e",
  green: LIGHT_THEME ? "#287a54" : "#48a77c",
  amber: LIGHT_THEME ? "#956814" : "#d29b25",
};

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

const _apiInflight = new Map();
function api(path) {
  const existing = _apiInflight.get(path);
  if (existing) return existing;
  const p = fetch(path).then((r) => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }).finally(() => { _apiInflight.delete(path); });
  _apiInflight.set(path, p);
  return p;
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
