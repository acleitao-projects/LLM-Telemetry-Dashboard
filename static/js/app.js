/* Observatory - dashboard app logic (part A: core + overview + models) */
"use strict";

const RANGES = ["today", "2d", "3d", "5d", "7d", "30d", "all"];
const RANGE_LABELS = { today: "Today", "2d": "2d", "3d": "3d", "5d": "5d", "7d": "7d", "30d": "30d", all: "All" };
const GROUPS = ["family", "file", "quant"];
const GROUP_LABELS = { family: "Family", file: "Each file", quant: "Quant" };
const METRIC_KEYS = ["active", "inference"];
const METRIC_LABELS = { active: "Active", inference: "Inference" };
const DETAIL_RANGES = ["1m", "5m", "15m", "1h", "session", "24h", "7d", "30d"];

window.META = null;
window.__onLive = null;

function initShell() {
  const body = document.body;
  const sidebarToggle = el("sidebarToggle");
  const mobileMenu = el("mobileMenu");
  const mobileNavBackdrop = el("mobileNavBackdrop");
  const themeToggles = [el("themeToggle"), el("mobileThemeToggle")].filter(Boolean);
  const mobileQuery = matchMedia("(max-width: 800px)");
  const savedSidebar = localStorage.getItem("llm-telemetry-sidebar");
  const collapsed = savedSidebar === "collapsed";

  const setSidebar = (isCollapsed) => {
    body.classList.toggle("sidebar-collapsed", isCollapsed);
    if (sidebarToggle) {
      sidebarToggle.setAttribute("aria-expanded", String(!isCollapsed));
      sidebarToggle.title = isCollapsed ? "Expand sidebar" : "Collapse sidebar";
      const label = sidebarToggle.querySelector("span");
      if (label) label.textContent = isCollapsed ? "Expand" : "Collapse";
    }
    localStorage.setItem("llm-telemetry-sidebar", isCollapsed ? "collapsed" : "expanded");
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  };
  setSidebar(collapsed);
  const setMobileNav = (open) => {
    body.classList.toggle("mobile-nav-open", open && mobileQuery.matches);
    if (mobileMenu) {
      mobileMenu.setAttribute("aria-expanded", String(open && mobileQuery.matches));
      mobileMenu.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    }
    if (sidebarToggle && mobileQuery.matches) {
      sidebarToggle.setAttribute("aria-expanded", String(open));
      sidebarToggle.title = "Close navigation";
      const label = sidebarToggle.querySelector("span");
      if (label) label.textContent = "Close";
    }
  };
  if (sidebarToggle) sidebarToggle.onclick = () => {
    if (mobileQuery.matches) setMobileNav(false);
    else setSidebar(!body.classList.contains("sidebar-collapsed"));
  };
  if (mobileMenu) mobileMenu.onclick = () => setMobileNav(!body.classList.contains("mobile-nav-open"));
  if (mobileNavBackdrop) mobileNavBackdrop.onclick = () => setMobileNav(false);
  document.querySelectorAll(".sidebar nav a").forEach((link) => {
    link.addEventListener("click", () => setMobileNav(false));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMobileNav(false);
  });
  mobileQuery.addEventListener("change", () => {
    setMobileNav(false);
    if (!mobileQuery.matches) setSidebar(body.classList.contains("sidebar-collapsed"));
  });
  setMobileNav(false);

  const syncThemeToggle = () => {
    const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    const next = current === "light" ? "dark" : "light";
    themeToggles.forEach((toggle) => {
      toggle.setAttribute("aria-label", "Switch to " + next + " mode");
      toggle.title = "Switch to " + next + " mode";
    });
  };
  syncThemeToggle();
  themeToggles.forEach((toggle) => {
    toggle.onclick = () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      localStorage.setItem("llm-telemetry-theme", next);
      document.documentElement.dataset.theme = next;
      syncThemeToggle();
      location.reload();
    };
  });

  const captureDialog = el("captureDialog");
  const captureClose = el("captureClose");
  if (captureClose && captureDialog) captureClose.onclick = () => captureDialog.close();
  if (captureDialog) captureDialog.onclick = (event) => {
    if (event.target === captureDialog) captureDialog.close();
  };

  const unloadButton = el("unloadModels"), unloadDialog = el("unloadDialog");
  const unloadConfirm = el("unloadConfirm"), unloadCancel = el("unloadCancel");
  const unloadClose = el("unloadClose"), unloadMessage = el("unloadMessage");
  const unloadResult = el("unloadResult");
  const closeUnload = () => { if (unloadDialog) unloadDialog.close(); };
  if (unloadButton && unloadDialog) unloadButton.onclick = () => {
    unloadMessage.textContent = "This unloads every active model from every enabled provider. Active inference may be interrupted.";
    unloadResult.hidden = true;
    unloadConfirm.hidden = false;
    unloadCancel.textContent = "Cancel";
    unloadDialog.showModal();
  };
  if (unloadClose) unloadClose.onclick = closeUnload;
  if (unloadCancel) unloadCancel.onclick = closeUnload;
  if (unloadDialog) unloadDialog.onclick = (event) => { if (event.target === unloadDialog) closeUnload(); };
  if (unloadConfirm) unloadConfirm.onclick = async () => {
    unloadConfirm.disabled = true;
    if (unloadButton) { unloadButton.disabled = true; unloadButton.textContent = "Unloading…"; }
    unloadMessage.textContent = "Contacting enabled providers…";
    try {
      const response = await fetch("/api/models/unload-all", { method: "POST" });
      if (!response.ok) throw new Error("Unload request failed");
      const data = await response.json();
      const summary = data.summary || {};
      const failed = (data.providers || []).filter((provider) => provider.status === "error");
      unloadMessage.textContent = data.demo ? "Demo mode does not contact providers." :
        "Unloaded " + (summary.unloaded_models || 0) + " model" + (summary.unloaded_models === 1 ? "" : "s") + ".";
      unloadResult.textContent = failed.length ? "Could not reach: " + failed.map((provider) => provider.name).join(", ") + "." :
        (data.demo ? "No provider requests were made." : "All enabled providers completed.");
      unloadResult.hidden = false;
      unloadConfirm.hidden = true;
      unloadCancel.textContent = "Close";
    } catch (error) {
      unloadMessage.textContent = "Could not complete the unload request.";
      unloadResult.textContent = "Try again after checking provider connectivity.";
      unloadResult.hidden = false;
    } finally {
      unloadConfirm.disabled = false;
      if (unloadButton) { unloadButton.disabled = false; unloadButton.textContent = "Unload Models"; }
    }
  };
}

function esc(x) {
  return String(x == null ? "" : x).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtBytes(b) {
  if (b == null || isNaN(b)) return "-";
  b = Number(b);
  if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
  if (b >= 1e3) return Math.round(b / 1e3) + " KB";
  return Math.round(b) + " B";
}

function stateBadge(state) {
  const s = state || "UNLOADED";
  return '<span class="state ' + esc(s) + '"><span class="dot"></span>' + esc(s) + "</span>";
}

function pillCls(status) {
  if (status === "LIVE") return "pill live";
  if (status === "STALE") return "pill stale";
  return "pill offline";
}

function pillHtml(status) {
  return '<span class="' + pillCls(status) + '"><span class="dot"></span>' + esc(status || "OFFLINE") + "</span>";
}

function trendHtml(t) {
  if (t == null) return '<span class="muted">no prior data</span>';
  const up = t >= 0;
  return '<span class="' + (up ? "up" : "down") + '">' + (up ? "↑" : "↓") + " " +
    Math.abs(t).toFixed(1) + "% vs prior</span>";
}

function makeCharts() {
  const list = [];
  // The registry returns the same instance for a given node, so re-adding a
  // chart during a refresh hands back the object already tracked.  Recording
  // it once keeps this list the size of the surface rather than the size of
  // the surface times the number of refreshes.
  const track = (c) => { if (c && !list.includes(c)) list.push(c); return c; };
  return {
    list: list,
    add(box, opt) { return track(registerChart(box, opt)); },
    spark(box, data, color) { return track(sparkline(box, data, color)); },
    // Structural teardown for this surface: dispose each instance through the
    // shared registry so it leaves every registry/resize collection.
    clear() {
      list.forEach((c) => {
        try { if (c && !c.isDisposed()) ChartRegistry.dispose(c.getDom()); } catch (e) {}
      });
      list.length = 0;
    },
    // Structural teardown of one node, for a surface that is replacing a chart
    // with non-chart content.  Without this the instance would be orphaned by
    // the innerHTML that overwrites its canvas.
    drop(box) {
      if (!box) return;
      const c = ChartRegistry.get(box);
      if (!c) return;
      ChartRegistry.dispose(box);
      const i = list.indexOf(c);
      if (i >= 0) list.splice(i, 1);
    },
  };
}

function kvTable(pairs) {
  return '<table class="kv">' + pairs.map(([k, v]) =>
    '<tr><td class="k">' + esc(k) + '</td><td class="v">' + v + "</td></tr>").join("") + "</table>";
}

function metricCard(l, v, subHtml, sparkId) {
  return '<div class="metric-card"><div class="mc-label">' + esc(l) + "</div>" +
    '<div class="mc-value">' + v + '</div><div class="mc-sub">' + (subHtml || "") + "</div>" +
    (sparkId ? '<div class="mc-spark" id="' + sparkId + '"></div>' : "") + "</div>";
}

// The Models range cards, shared with Overview so the two pages cannot drift
// apart or disagree on a number. `leader` is optional: Models passes its
// grouped leading row, Overview falls back to the leader the API reports.
function topCardsHtml(top, opts) {
  top = top || {};
  opts = opts || {};
  const leader = opts.leader;
  // Absolute figure first, share of all tokens as a smaller suffix.
  const withPct = (value, pct) => fmtTokens(value) +
    (pct != null ? ' <small>' + pct + "%</small>" : "");
  const shareValue = leader ? leader.share : top.leader_share;
  const shareName = leader ? leader.label : top.leader_name;
  return metricCard("TOKENS", fmtTokens(top.tokens), trendHtml(top.tokens_trend),
                    opts.sparkId || null) +
    metricCard("AVG DAILY TOKENS", fmtTokens(top.avg_daily_tokens), "selected period", null) +
    metricCard("GENERATED", withPct(top.gen_tokens, top.generated_pct), "generated tokens", null) +
    metricCard("GENERATED COST", costCell(top.generated_cost), "generated tokens", null) +
    metricCard("INPUT", withPct(top.prompt_tokens, top.input_pct), "prompt processed tokens", null) +
    metricCard("INPUT COST", costCell(top.input_cost), "prompt tokens", null) +
    metricCard("TOTAL COST", costCell(top.total_cost), "input + generated", null) +
    metricCard("FAMILIES", top.families != null ? top.families : "—", top.families_leader || "", null) +
    metricCard("SESSIONS", top.sessions != null ? top.sessions : "—",
      top.avg_context_session ? "avg ctx " + fmtNum(top.avg_context_session) : "", null) +
    metricCard("LEADER SHARE", shareValue != null ? shareValue + "%" : "—", shareName || "", null) +
    metricCard("FASTEST GEN", top.fastest != null ? top.fastest + " t/s" : "—", top.fastest_name || "", null) +
    metricCard("SLOWEST GEN", top.slowest != null ? top.slowest + " t/s" : "—", top.slowest_name || "", null);
}

function selMetric(l, v, s, sec) {
  return '<div class="sel-metric"><div class="l">' + esc(l) + '</div><div class="v">' + esc(v) +
    (sec ? '<span class="sel-fx">' + esc(sec) + "</span>" : "") +
    '</div><div class="s">' + esc(s || "") + "</div></div>";
}

// The three IN/OUT/TOTAL cost cards of the selected-model panel. Rendered from
// both the initial row and the live-patch path -- shared here so they cannot
// drift on which currency line they carry.
function selCostMetrics(stats) {
  const inTok = stats.prompt_tokens || 0, outTok = stats.gen_tokens || 0;
  return selMetric("IN COST", fmtCost(stats.input_cost),
                   fmtTokens(inTok) + " in tokens", fmtSecondary(stats.input_cost)) +
    selMetric("OUT COST", fmtCost(stats.output_cost),
              fmtTokens(outTok) + " out tokens", fmtSecondary(stats.output_cost)) +
    selMetric("TOTAL COST", fmtCost(stats.total_cost), "estimated",
              fmtSecondary(stats.total_cost));
}

async function capturePanel(target, includeOverflow = true) {
  if (!target) throw new Error("Capture target is unavailable");
  const rootRect = target.getBoundingClientRect();
  const captureWidth = Math.ceil(includeOverflow ? Math.max(target.scrollWidth, rootRect.width) : rootRect.width);
  const captureHeight = Math.ceil(Math.max(target.scrollHeight, rootRect.height));
  const padding = 1;
  const width = captureWidth + padding * 2;
  const height = captureHeight + padding * 2;
  const scale = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
  const canvas = document.createElement("canvas");
  canvas.width = Math.ceil(width * scale);
  canvas.height = Math.ceil(height * scale);
  const context = canvas.getContext("2d");
  context.scale(scale, scale);

  const relativeRect = (rect) => ({
    x: rect.left - rootRect.left + target.scrollLeft + padding,
    y: rect.top - rootRect.top + target.scrollTop + padding,
    width: rect.width,
    height: rect.height,
  });

  const pathBox = (rect, radius) => {
    context.beginPath();
    if (context.roundRect) context.roundRect(rect.x, rect.y, rect.width, rect.height, radius);
    else context.rect(rect.x, rect.y, rect.width, rect.height);
  };

  const drawText = (node, opacity) => {
    const parent = node.parentElement;
    if (!parent || !node.nodeValue || !node.nodeValue.trim()) return;
    const style = getComputedStyle(parent);
    const transform = style.textTransform;
    context.save();
    context.globalAlpha = opacity;
    context.fillStyle = style.color;
    context.font = [style.fontStyle, style.fontWeight, style.fontSize, style.fontFamily].join(" ");
    context.textBaseline = "top";
    for (let index = 0; index < node.nodeValue.length; index += 1) {
      let character = node.nodeValue[index];
      if (/\s/.test(character)) continue;
      if (transform === "uppercase") character = character.toUpperCase();
      else if (transform === "lowercase") character = character.toLowerCase();
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + 1);
      const rect = range.getBoundingClientRect();
      if (!rect.width || !rect.height) continue;
      const point = relativeRect(rect);
      const fontSize = parseFloat(style.fontSize) || 12;
      context.fillText(character, point.x, point.y + Math.max(0, (point.height - fontSize) / 2));
    }
    context.restore();
  };

  const drawSvg = async (element, rect, opacity) => {
    const clone = element.cloneNode(true);
    // Blob-loaded SVGs otherwise fall back to a 300x150 intrinsic viewport.
    // Drawing that fallback bitmap into the CSS box distorts circles and text.
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", String(rect.width));
    clone.setAttribute("height", String(rect.height));
    clone.style.width = rect.width + "px";
    clone.style.height = rect.height + "px";
    const sourceNodes = [element, ...element.querySelectorAll("*")];
    const clonedNodes = [clone, ...clone.querySelectorAll("*")];
    const svgProperties = [
      "color", "fill", "fill-opacity", "stroke", "stroke-width", "stroke-opacity",
      "stroke-linecap", "stroke-linejoin", "opacity", "font-family", "font-size",
      "font-style", "font-weight", "letter-spacing", "text-anchor", "dominant-baseline",
    ];
    sourceNodes.forEach((sourceNode, index) => {
      const clonedNode = clonedNodes[index];
      if (!clonedNode || !clonedNode.style) return;
      const computed = getComputedStyle(sourceNode);
      svgProperties.forEach((property) => clonedNode.style.setProperty(property, computed.getPropertyValue(property)));
    });
    const source = new XMLSerializer().serializeToString(clone);
    const url = URL.createObjectURL(new Blob([source], { type: "image/svg+xml;charset=utf-8" }));
    try {
      const image = new Image();
      const loaded = new Promise((resolve) => {
        image.onload = () => resolve(true);
        image.onerror = () => resolve(false);
        setTimeout(() => resolve(false), 1000);
      });
      image.src = url;
      if (await loaded) {
        context.save();
        context.globalAlpha = opacity;
        context.drawImage(image, rect.x, rect.y, rect.width, rect.height);
        context.restore();
      }
    } finally {
      URL.revokeObjectURL(url);
    }
  };

  const drawElement = async (element, inheritedOpacity) => {
    if (element.hasAttribute("data-capture-ignore")) return;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden") return;
    const ownOpacity = Number.isFinite(Number(style.opacity)) ? Number(style.opacity) : 1;
    const opacity = inheritedOpacity * ownOpacity;
    if (opacity <= 0) return;
    const rect = relativeRect(element.getBoundingClientRect());
    if (rect.width <= 0 || rect.height <= 0) return;

    const radius = parseFloat(style.borderTopLeftRadius) || 0;
    if (style.backgroundColor && style.backgroundColor !== "rgba(0, 0, 0, 0)") {
      context.save();
      context.globalAlpha = opacity;
      context.fillStyle = style.backgroundColor;
      pathBox(rect, radius);
      context.fill();
      context.restore();
    }
    const borderWidth = parseFloat(style.borderTopWidth) || 0;
    if (borderWidth && style.borderTopStyle !== "none") {
      context.save();
      context.globalAlpha = opacity;
      context.strokeStyle = style.borderTopColor;
      context.lineWidth = borderWidth;
      pathBox({ x: rect.x + borderWidth / 2, y: rect.y + borderWidth / 2,
        width: Math.max(0, rect.width - borderWidth), height: Math.max(0, rect.height - borderWidth) }, radius);
      context.stroke();
      context.restore();
    }

    if (element instanceof HTMLCanvasElement) {
      context.save();
      context.globalAlpha = opacity;
      context.drawImage(element, rect.x, rect.y, rect.width, rect.height);
      context.restore();
      return;
    }
    if (element instanceof SVGElement && element.tagName.toLowerCase() === "svg") {
      await drawSvg(element, rect, opacity);
      return;
    }
    if (element instanceof HTMLImageElement && element.complete && element.naturalWidth) {
      context.save();
      context.globalAlpha = opacity;
      context.drawImage(element, rect.x, rect.y, rect.width, rect.height);
      context.restore();
      return;
    }
    for (const child of element.childNodes) {
      if (child.nodeType === Node.ELEMENT_NODE) await drawElement(child, opacity);
      else if (child.nodeType === Node.TEXT_NODE) drawText(child, opacity);
    }
  };

  const rootStyle = getComputedStyle(target);
  const rootRadius = Math.max(
    parseFloat(rootStyle.borderTopLeftRadius) || 0,
    parseFloat(rootStyle.borderTopRightRadius) || 0,
    parseFloat(rootStyle.borderBottomRightRadius) || 0,
    parseFloat(rootStyle.borderBottomLeftRadius) || 0,
  );
  const rootBox = { x: padding, y: padding, width: captureWidth, height: captureHeight };
  context.save();
  pathBox(rootBox, rootRadius);
  context.clip();
  context.fillStyle = rootStyle.backgroundColor && rootStyle.backgroundColor !== "rgba(0, 0, 0, 0)" ?
    rootStyle.backgroundColor : "#1b1b19";
  context.fillRect(rootBox.x, rootBox.y, rootBox.width, rootBox.height);
  for (const child of target.childNodes) {
    if (child.nodeType === Node.ELEMENT_NODE) await drawElement(child, 1);
    else if (child.nodeType === Node.TEXT_NODE) drawText(child, 1);
  }
  const rootBorderWidth = parseFloat(rootStyle.borderTopWidth) || 0;
  if (rootBorderWidth && rootStyle.borderTopStyle !== "none") {
    context.strokeStyle = rootStyle.borderTopColor;
    context.lineWidth = rootBorderWidth;
    pathBox({
      x: rootBox.x + rootBorderWidth / 2,
      y: rootBox.y + rootBorderWidth / 2,
      width: rootBox.width - rootBorderWidth,
      height: rootBox.height - rootBorderWidth,
    }, Math.max(0, rootRadius - rootBorderWidth / 2));
    context.stroke();
  }
  context.restore();
  return canvas.toDataURL("image/png");
}

function afterCaptureLayout() {
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

function newCaptureId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return hex.slice(0, 4).join("") + "-" + hex.slice(4, 6).join("") + "-" +
    hex.slice(6, 8).join("") + "-" + hex.slice(8, 10).join("") + "-" +
    hex.slice(10).join("");
}

async function capturePanelAtWidth(target, width) {
  if (!width) return capturePanel(target);
  const original = {
    width: target.style.width,
    minWidth: target.style.minWidth,
    maxWidth: target.style.maxWidth,
    overflow: target.style.overflow,
  };
  target.style.width = width + "px";
  target.style.minWidth = width + "px";
  target.style.maxWidth = "none";
  target.style.overflow = "hidden";
  try {
    await afterCaptureLayout();
    window.dispatchEvent(new Event("resize"));
    await afterCaptureLayout();
    return await capturePanel(target, false);
  } finally {
    target.style.width = original.width;
    target.style.minWidth = original.minWidth;
    target.style.maxWidth = original.maxWidth;
    target.style.overflow = original.overflow;
    await afterCaptureLayout();
    window.dispatchEvent(new Event("resize"));
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-capture-target]");
  if (!button) return;
  if (button.classList.contains("is-capturing")) return;
  event.stopPropagation();
  event.preventDefault();
  const captureId = newCaptureId();
  button.classList.add("is-capturing");
  button.setAttribute("aria-busy", "true");
  try {
    const requestedWidth = Math.max(0, Number(button.dataset.captureWidth) || 0);
    const pngUrl = await capturePanelAtWidth(
      document.getElementById(button.dataset.captureTarget), requestedWidth,
    );
    const png = await (await fetch(pngUrl)).blob();
    const response = await fetch("/api/screenshots/" + captureId, {
      method: "PUT", headers: { "Content-Type": "image/png" }, body: png,
    });
    if (!response.ok) throw new Error(await response.text() || "Screenshot upload failed");
    const result = await response.json();
    const imageUrl = result.url || ("/screenshots/" + captureId + ".png");
    const dialog = el("captureDialog"), preview = el("capturePreview");
    const open = el("captureOpen"), download = el("captureDownload"), title = el("captureTitle");
    if (title) title.textContent = "Screenshot ready";
    if (preview) { preview.hidden = false; preview.src = imageUrl; }
    if (open) { open.hidden = false; open.href = imageUrl; }
    if (download) { download.hidden = false; download.href = imageUrl; download.download = "llm-telemetry-" + captureId + ".png"; }
    if (dialog && typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); }
    else location.assign(imageUrl);
  } catch (error) {
    console.error("Screenshot capture failed", error);
    const dialog = el("captureDialog"), preview = el("capturePreview");
    const open = el("captureOpen"), download = el("captureDownload"), title = el("captureTitle");
    if (title) title.textContent = "Screenshot failed: " + (error.message || "unknown error");
    if (preview) preview.hidden = true;
    if (open) open.hidden = true;
    if (download) download.hidden = true;
    if (dialog && typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); }
  } finally {
    button.classList.remove("is-capturing");
    button.removeAttribute("aria-busy");
  }
});

function dualAxisOption(labels, series) {
  const o = baseOption();
  o.xAxis.data = labels;
  o.yAxis = [
    { type: "value", splitLine: { lineStyle: { color: OC.split } },
      axisLabel: { color: OC.label, fontSize: 9.5 }, axisLine: { show: false } },
    { type: "value", splitLine: { show: false },
      axisLabel: { color: OC.label, fontSize: 9.5 }, axisLine: { show: false } },
  ];
  o.series = series.map((s) => ({
    id: s.name, name: s.name, type: "line", data: s.data, yAxisIndex: s.y || 0,
    showSymbol: false, smooth: 0.15, connectNulls: false,
    lineStyle: { width: 1, color: s.color, type: s.dash ? "dashed" : "solid" },
    itemStyle: { color: s.color },
  }));
  o.legend = { top: 0, right: 0, itemWidth: 8, itemHeight: 6,
    textStyle: { color: OC.label, fontSize: 9.5 } };
  o.grid.top = 22;
  o.tooltip.valueFormatter = (v) => (v == null ? "-" : v);
  return o;
}

function gpuDualSeries(gpus) {
  const out = [];
  (gpus || []).forEach((gpu) => {
    out.push({ name: gpu.label + " util %", color: gpu.color, data: gpu.series.util, y: 0 });
    out.push({ name: gpu.label + " VRAM MB", color: gpu.color,
      data: gpu.series.vram_mb, y: 1, dash: true });
  });
  return out;
}

function clampPct(value) {
  if (value == null || !isFinite(Number(value))) return null;
  return Math.max(0, Math.min(100, Number(value)));
}

function resourceGauge(label, pct, center, detail, aria) {
  const value = clampPct(pct);
  const arc = value == null ? 0 : value * 0.75;
  return '<div class="resource-gauge" role="img" aria-label="' + esc(aria || label) + '">' +
    '<div class="gauge-label">' + esc(label) + '</div><svg viewBox="0 0 100 84" aria-hidden="true">' +
    '<circle class="gauge-track" cx="50" cy="47" r="35" pathLength="100"></circle>' +
    '<circle class="gauge-fill" cx="50" cy="47" r="35" pathLength="100" style="stroke-dasharray:' +
    arc.toFixed(2) + ' 100"></circle>' +
    '<text class="gauge-value" x="50" y="51" text-anchor="middle">' + esc(center) + '</text></svg>' +
    '<div class="gauge-detail">' + esc(detail || "—") + '</div></div>';
}

function gpuSummaryCards(gpus, mode) {
  if (!gpus || !gpus.length) return '<div class="empty">no per-GPU data in range</div>';
  const isLive = mode === "live";
  const modeLabel = isLive ? "LIVE" : mode === "session" ? "SESSION AVG" : "RANGE AVG";
  return '<div class="gpu-cards">' + gpus.map((gpu) => {
    const v = isLive ? (gpu.current || {}) : (gpu.summary || {});
    const total = gpu.vram_total_mb;
    const vramPct = v.vram_mb != null && total ? v.vram_mb / total * 100 : null;
    const shownVramPct = clampPct(vramPct);
    const shownUtilPct = clampPct(v.util);
    const usedText = v.vram_mb != null ? fmtBytes(v.vram_mb * 1024 * 1024) : "—";
    const totalText = total ? fmtBytes(total * 1024 * 1024) : "max unavailable";
    const ident = gpu.label || ("GPU " + (gpu.index != null ? gpu.index : "?"));
    return '<div class="gpu-card" style="border-top-color:' + esc(gpu.color) + '">' +
      '<div class="gpu-title"><b><span class="m-dot" style="background:' + esc(gpu.color) + '"></span> ' +
      esc(ident) + '</b><span>' + modeLabel + '</span></div>' +
      '<div class="gpu-gauges">' +
      resourceGauge("VRAM", vramPct, shownVramPct == null ? "—" : Math.round(shownVramPct) + "%",
        usedText + " / " + totalText, ident + " VRAM " + usedText + " of " + totalText) +
      resourceGauge("UTILIZATION", v.util, shownUtilPct == null ? "—" : Math.round(shownUtilPct) + "%",
        (v.temp_c != null ? v.temp_c + "°C" : "—") + " · " +
        (v.power_w != null ? v.power_w + " W" : "—"), ident + " utilization") + '</div>' +
      '<div class="gpu-meta"><span><i>PCIe</i>' + esc(gpu.pcie || "—") + '</span>' +
      '<span><i>TEMPERATURE</i>' + (v.temp_c != null ? esc(v.temp_c + "°C") : "—") + '</span>' +
      '<span><i>POWER</i>' + (v.power_w != null ? esc(v.power_w + " W") : "—") + '</span></div></div>';
  }).join("") + "</div>";
}

function renderHBar(ch, box, labels, series, emptyMsg) {
  if (!labels.length) { box.innerHTML = '<div class="empty">' + emptyMsg + "</div>"; return; }
  ch.add(box, barOption(labels, series, { horizontal: true }));
}

function fillSelect(box, allLabel, options, value) {
  if (!box) return;
  box.innerHTML = '<option value="">' + allLabel + "</option>" +
    options.map(([v, l]) => '<option value="' + esc(v) + '">' + esc(l) + "</option>").join("");
  box.value = value != null ? value : "";
}

function cfgPairs(cfg) {
  return [
    ["context", cfg.context != null ? fmtNum(cfg.context) : "—"],
    ["kv cache", cfg.kv_cache_k ? cfg.kv_cache_k + " / " + (cfg.kv_cache_v || "-") : "—"],
    ["flash attn", cfg.flash_attn == null ? "—" : cfg.flash_attn ? "yes" : "no"],
    ["split mode", cfg.split_mode || "—"],
    ["gpu layers", cfg.gpu_layers != null ? cfg.gpu_layers : "—"],
    ["threads", cfg.threads != null ? cfg.threads : "—"],
    ["batch / ubatch", cfg.batch != null ? cfg.batch + " / " + (cfg.ubatch || "-") : "—"],
    ["reasoning", cfg.reasoning ? cfg.reasoning + (cfg.reasoning_effort ? " (" + cfg.reasoning_effort + ")" : "") : "—"],
    ["mtp", cfg.mtp_enabled ? "on" + (cfg.mtp_model ? " · " + esc(cfg.mtp_model) : "") : cfg.mtp_enabled === false ? "off" : "—"],
    ["fingerprint", esc(cfg.fingerprint)],
  ];
}

function cfgPanelHtml(cfg, tblId, flagsId, subEl) {
  if (!cfg) {
    el(tblId).innerHTML = '<table class="kv"><tr><td class="k">config</td><td class="v">not observed yet</td></tr></table>';
    return;
  }
  el(tblId).innerHTML = kvTable(cfgPairs(cfg));
  el(flagsId).textContent = JSON.stringify(cfg.payload || {}, null, 2);
  if (subEl) {
    const a = document.createElement("a");
    a.className = "muted";
    a.textContent = "fingerprint " + cfg.fingerprint + " · click to view flags";
    a.href = "#";
    a.onclick = (e) => { e.preventDefault(); el(flagsId).classList.toggle("open"); };
    subEl.innerHTML = "";
    subEl.appendChild(a);
  }
}

/* ------------------------------------------------------------------ bootstrap */
// Only the process holding the single-writer lease polls providers. A standby
// process serves the same pages from the same database while collecting
// nothing, so provider status alone cannot tell the two apart.
function collectorNotCollecting(role) {
  return !!role && role !== "active";
}

function topbarPill(providers, collectorRole) {
  if (!providers || !providers.length) return;
  const def = providers.find((p) => p.is_default) || providers[0];
  const known = window.META && window.META.providers.find((p) => p.id === def.id);
  const name = (known && known.name) || def.name || "provider";
  const idle = collectorNotCollecting(collectorRole);
  const status = def.status || "OFFLINE";
  // Provider status still means something on a standby process -- the database
  // it reads is kept current by whichever process holds the lease -- so report
  // it, and append the fact that *this* process is not the one polling.
  const label = name + " · " + status + (idle ? " · not collecting" : "");
  const title = idle
    ? name + " · " + status + " — collector " + collectorRole +
      ": this process is not polling providers, another one holds the lease"
    : label;
  const pill = el("provPill"), txt = el("provPillText");
  if (pill && txt) {
    pill.className = idle ? "pill stale" : pillCls(status);
    txt.textContent = label;
    pill.title = title;
  }
  const mobilePill = el("mobileProvPill");
  if (mobilePill) {
    mobilePill.className = "mobile-provider " + (idle ? "stale" :
      status === "LIVE" ? "live" : status === "STALE" ? "stale" : "offline");
    mobilePill.setAttribute("aria-label", title);
    mobilePill.title = title;
  }
  const dot = el("sideDot"), sp = el("sideProv");
  if (dot && sp) {
    dot.style.background = idle ? "var(--amber)" :
      status === "LIVE" ? "var(--green)" :
      status === "STALE" ? "var(--amber)" : "var(--red)";
    sp.textContent = label;
    sp.title = title;
  }
}

function bootstrap() {
  const page = document.body.dataset.page || "";
  api("/api/meta").then((meta) => {
    window.META = meta;
    // Gate every FX-driven width change on one attribute so the disabled case
    // stays byte-identical to today.
    if (meta.currency && meta.currency.enabled) document.body.dataset.fx = "1";
    topbarPill(meta.providers, meta.collector_role);
    const init = {
      overview: initOverview, models: initModels, model: initModelDetail,
      sessions: initSessions, session: initSessionDetail, compare: initCompare,
      hardware: initHardware, settings: initSettings,
    }[page];
    if (init) {
      const result = init(meta);
      if (result && typeof result.catch === "function") {
        result.catch((e) => console.error(init.name, e));
      }
    }
  }).catch((e) => console.error("meta", e));

  api("/api/status").then((st) => {
    const sideUp = el("sideUp"), sideDb = el("sideDb");
    if (sideUp) {
      const t0 = Date.now(), u0 = st.uptime_s || 0;
      const tick = () => { sideUp.textContent = fmtDur(u0 + (Date.now() - t0) / 1000); };
      tick(); setInterval(tick, 1000);
    }
    if (sideDb) sideDb.textContent = fmtBytes(st.db_size_bytes || 0);
  }).catch(() => {});

  try {
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (!d || d.error) return;
      topbarPill(d.providers, d.collector_role);
      if (window.__onLive) window.__onLive(d);
    };
    es.onerror = () => {};
  } catch (e) {}
}

/* ------------------------------------------------------------------ overview */
function renderOvTop(d) {
  const c = d.current;
  const stEl = el("ovNowState"), moEl = el("ovNowModel"), suEl = el("ovNowSub");
  if (c && c.model) {
    stEl.innerHTML = stateBadge(c.state);
    moEl.innerHTML = '<a href="/model/' + (c.model_id || 0) + '"><b>' + esc(c.model) + "</b></a>";
    const bits = [];
    if (c.gen_tps != null) bits.push(fmtTps(c.gen_tps));
    if (c.session_elapsed_s != null) bits.push("session " + fmtDur(c.session_elapsed_s));
    suEl.textContent = bits.join(" · ") || (c.provider || "");
  } else {
    stEl.innerHTML = stateBadge("UNLOADED");
    moEl.textContent = "no model loaded";
    suEl.textContent = "";
  }
}

// In-place chart update for Overview: init-or-reuse the registered instance
// on the stable box node and setOption in place. Empty payloads hide the
// (still registered) canvas and show a managed message node, so canvas
// identity and any interaction state survive empty<->data transitions.
function ovSetChart(boxId, option, emptyMsg) {
  const box = el(boxId);
  if (!box) return null;
  const chart = ChartRegistry.init(box, option);
  if (!chart) return null;
  const canvas = ChartRegistry.canvas(box);
  let node = box.querySelector(":scope > .ov-empty");
  if (!option) {
    if (canvas) canvas.style.display = "none";
    if (emptyMsg) {
      if (!node) {
        node = document.createElement("div");
        node.className = "ov-empty empty";
        box.appendChild(node);
      }
      node.textContent = emptyMsg;
    }
  } else {
    if (canvas) canvas.style.display = "";
    if (node) node.remove();
  }
  return chart;
}

function renderOvRecent(rows) {
  const tb = el("ovRecentBody");
  if (!tb) return;
  if (!tb.__ovRecentBound) {
    tb.__ovRecentBound = true;
    tb.addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr[data-id]");
      if (tr) location.href = "/session/" + tr.dataset.id;
    });
  }
  rows = rows || [];
  const wanted = new Set(rows.map((x) => String(x.id)));
  Array.from(tb.querySelectorAll("tr[data-id]")).forEach((tr) => {
    if (!wanted.has(tr.dataset.id)) tr.remove();
  });
  // The empty-state row has no data-id, so the keyed cleanup above cannot
  // see it. Drop it before rendering either state, so a stale
  // "no sessions yet" row never survives an empty -> data refresh.
  Array.from(tb.querySelectorAll("tr:not([data-id])")).forEach((tr) => tr.remove());
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="6"><div class="empty">no sessions yet</div></td></tr>';
    return;
  }
  rows.forEach((x) => {
    let tr = tb.querySelector('tr[data-id="' + String(x.id) + '"]');
    if (!tr) {
      tr = document.createElement("tr");
      tr.className = "clickable";
      tr.dataset.id = x.id;
      tr.innerHTML =
        "<td></td>" +
        '<td><span class="m-dot"></span> <span class="ov-m"></span></td>' +
        '<td class="num"></td><td class="num"></td><td class="num"></td><td class="num"></td>';
    }
    tr.children[0].textContent = fmtDate(x.start);
    tr.querySelector(".m-dot").style.background = x.color || "#74736e";
    tr.querySelector(".ov-m").textContent = x.model || "—";
    tr.children[2].textContent = fmtDur(x.duration_s);
    tr.children[3].textContent = fmtTokens(x.gen_tokens);
    tr.children[4].textContent = x.avg_gen_tps != null ? x.avg_gen_tps + " t/s" : "—";
    tr.children[5].textContent = x.mtp_acc != null ? x.mtp_acc + "%" : "—";
    tb.appendChild(tr);
  });
}

function initOverview(meta) {
  let loading = false;
  const disp = (meta && meta.display) || {};
  // Shared with Models via display settings, so a period chosen on either page
  // applies to both.
  const ovSt = { range: RANGES.includes(disp.default_range) ? disp.default_range : "7d" };
  let cardsLoading = false;

  const renderCards = (d) => {
    // No sparkline here on purpose. Rendering one would mean disposing and
    // re-creating a chart instance on every refresh, which is the teardown
    // pattern G06 removed from this page and #55 removed from the Models
    // runtime card. The strip is replaced wholesale, so a chart inside it
    // could not survive anyway.
    el("ovCards").innerHTML = topCardsHtml((d && d.top) || {}, {});
    const note = el("ovRangeNote");
    if (note) note.textContent = (d && d.rows ? d.rows.length + " models · " : "") + ovSt.range;
  };

  const loadCards = () => {
    if (cardsLoading) return Promise.resolve();
    cardsLoading = true;
    // Reuses the Models endpoint so both pages report identical figures for a
    // given range rather than aggregating the same numbers twice.
    return api("/api/models?range=" + ovSt.range + "&group=model")
      .then(renderCards).catch(() => {}).finally(() => { cardsLoading = false; });
  };

  window.__onLive = (d) => {
    // Only the live cards follow SSE. The range cards are period figures and
    // are refreshed on their own cadence.
    if (d.current) renderOvTop(d);
  };
  const render = (d) => {
    renderOvTop(d);
    const u = d.usage_24h || { labels: [], series: [] };
    ovSetChart("ovUsage",
      u.series.length ? areaStackOption(u.labels,
        u.series.map((s) => ({ name: s.name, color: s.color, data: s.data }))) : null,
      "no token activity in the last 24h");
    const daily = d.daily_volume || {};
    const dailyTotal = [...(daily.inference_seconds || []), ...(daily.prompt_tokens || []),
      ...(daily.generated_tokens || []), ...(daily.unclassified_tokens || [])]
      .reduce((total, value) => total + (Number(value) || 0), 0);
    ovSetChart("ovDaily",
      dailyTotal ? dailyVolumeOption(daily.labels || [], daily) : null,
      "no activity in the last 30 days");
    renderOvRecent(d.recent_sessions);
    if (d.today) {
      el("ovInfVal").textContent = fmtDur(d.today.inference_s);
      el("ovInfSub").textContent = (d.today.utilization != null ? d.today.utilization + "% utilization" : "") +
        " · loaded " + fmtDur(d.today.loaded_s) + " · idle " + fmtDur(d.today.idle_s);
    }
  };
  const load = () => {
    if (loading) return Promise.resolve();
    loading = true;
    return api("/api/overview").then(render).finally(() => { loading = false; });
  };
  // The endpoint serves a recent cached snapshot while refreshing its data in
  // the background. Re-fetch so a page opened in that short window cannot
  // remain frozen before fresh telemetry is available.
  segControl("ovRngSeg", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[ovSt.range], (lbl) => {
    ovSt.range = RANGES.find((r) => RANGE_LABELS[r] === lbl) || "7d";
    fetch("/api/settings/display", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_range: ovSt.range }),
    }).catch(() => {});
    loadCards();
  });

  const initial = load();
  loadCards();
  setInterval(load, 7000);
  setInterval(loadCards, MODELS_REFRESH_MS);
  return initial;
}

/* ------------------------------------------------------------------ models page */
function svgSpark(data, color, width, height) {
  width = width || 72; height = height || 20;
  if (!data || !data.length) return "";
  const max = Math.max(1, ...data);
  const step = width / (data.length - 1 || 1);
  const points = data.map((v, i) => (i * step).toFixed(1) + "," + (height - (v / max) * (height - 2) - 1).toFixed(1)).join(" ");
  return '<svg class="row-spark-svg" width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">' +
    '<polyline points="' + points + '" fill="none" stroke="' + (color || OC.blue) + '" stroke-width="1.2" opacity="0.85"/></svg>';
}

function fmtCost(val) {
  if (val == null || val === "0" || val === "0.0") return "—";
  const n = Number(val);
  if (!isFinite(n)) return "—";
  if (n < 0.01) return "$" + n.toFixed(4);
  if (n < 1) return "$" + n.toFixed(3);
  return "$" + fmtNum(n);
}

const _DEC_BIGN = 100000000n;
function _decParse(s) {
  s = String(s || "0");
  const neg = s.startsWith("-");
  if (neg) s = s.slice(1);
  const parts = s.split(".");
  const int = BigInt(parts[0] || "0");
  const frac = BigInt((parts[1] || "0").slice(0, 8).padEnd(8, "0") || "0");
  const val = int * _DEC_BIGN + frac;
  return neg ? -val : val;
}
function decStrAdd(a, b) {
  const sum = _decParse(a) + _decParse(b);
  const neg = sum < 0n;
  const abs = neg ? -sum : sum;
  const intPart = abs / _DEC_BIGN;
  const fracPart = (abs % _DEC_BIGN).toString().padStart(8, "0");
  return (neg ? "-" : "") + intPart + "." + fracPart;
}
// Exact 8-dp fixed-point multiply, sibling to decStrAdd. Never Number(a) * rate:
// compounding two floats is precisely what the codebase's decimal discipline
// exists to prevent. _decParse already truncates each operand to 8 dp, which
// matches the server's format(x, ".8f") output, so nothing is lost at parse.
function decStrMul(a, b) {
  const prod = _decParse(a) * _decParse(b);      // 1e16-scale product
  const scaled = prod / _DEC_BIGN;               // back to 1e8-scale (truncates)
  const neg = scaled < 0n;
  const abs = neg ? -scaled : scaled;
  const intPart = abs / _DEC_BIGN;
  const fracPart = (abs % _DEC_BIGN).toString().padStart(8, "0");
  return (neg ? "-" : "") + intPart + "." + fracPart;
}

// The secondary-currency amount for a USD decimal string, or "" when there is
// nothing to show. Every call site treats "" as "render nothing", which is what
// makes "hide it everywhere when no rate exists" a one-line guarantee.
function fmtSecondary(usdStr) {
  const c = window.META && window.META.currency;
  if (!c || !c.enabled) return "";
  if (usdStr == null || usdStr === "0" || usdStr === "0.0") return "";
  const n = Number(decStrMul(usdStr, c.rate));
  if (!isFinite(n) || n <= 0) return "";
  const dp = c.decimals == null ? 2 : c.decimals;
  let shown = n.toFixed(dp);
  // Zero-decimal collision: $0.0001-scale costs are ¥0 at JPY precision. If the
  // converted value rounds to zero but is non-zero, show two significant figures.
  if (Number(shown) === 0) shown = n.toPrecision(2);
  return c.symbol + shown;
}

// Card form: primary USD with the secondary amount on a smaller block line.
function costCell(v) {
  const primary = fmtCost(v);
  if (primary === "—") return primary;
  const sec = fmtSecondary(v);
  return sec ? primary + '<span class="mc-fx">' + esc(sec) + "</span>" : primary;
}

// Table-row form: `$0.1234 / R$0.6304`, the second symbol whichever is selected.
function costPair(v) {
  const primary = fmtCost(v);
  if (primary === "—") return primary;
  const sec = fmtSecondary(v);
  return sec ? primary + ' <span class="fx-alt">/ ' + esc(sec) + "</span>" : primary;
}

// An observation older than this is no longer a current reading of the slot.
const STALE_OBSERVATION_S = 5;
// Models page background refresh, matching the Overview cadence.
const MODELS_REFRESH_MS = 7000;

function fmtObservedAge(ageS) {
  if (ageS == null) return "\u2014";
  if (ageS < 1) return "just now";
  if (ageS < 90) return Math.round(ageS) + "s ago";
  return fmtDur(ageS) + " ago";
}

function initModels(meta) {
  const ch = makeCharts();
  let selChart = null;
  let runtimeChart = null;
  let runtimeSeries = { sessionId: null, timestamps: [], genTps: [], context: [] };
  let runtimeShape = null;
  let selectedWasActive = false;
  let selectedRealtime = null;
  let activeSignature = null;
  let _reconcileTimer = 0;
  let _reconciling = false;
  let _selAbort = null;
  let _refreshAbort = null;
  const abortSelected = () => {
    if (_selAbort) { try { _selAbort.abort(); } catch (e) {} _selAbort = null; }
    if (_refreshAbort) { try { _refreshAbort.abort(); } catch (e) {} _refreshAbort = null; }
  };
  const disp = (meta && meta.display) || {};
  const st = {
    range: RANGES.includes(disp.default_range) ? disp.default_range : "7d",
    group: GROUPS.includes(disp.default_group) ? disp.default_group : "family",
    sort: "active",
    sourceRows: [],
    rows: [],
    top: {},
    selectedKey: null,
  };

  const load = () => api("/api/models?range=" + st.range + "&group=model");

  const groupedRows = (sourceRows) => {
    const groups = new Map();
    (sourceRows || []).forEach((source) => {
      const rawVal = st.group === "family" ? (source.family || source.label) :
        st.group === "quant" ? (source.quant || "unknown") : source.key;
      const key = st.group === "file" ? "file:" + rawVal : st.group + ":" + rawVal;
      let row = groups.get(key);
      if (!row) {
        row = { key, label: st.group === "file" ? source.label : rawVal,
          model_ids: [], tokens: 0, gen_tokens: 0, prompt_tokens: 0,
          gen_time: 0, prompt_time: 0, loaded_time: 0, idle_time: 0,
          sessions: 0, peak_gen: 0, peak_prompt: 0, active_rank: 0,
          active_tasks: 0, active_seen_at: 0, spark: new Array(24).fill(0),
          input_cost: "0", output_cost: "0", total_cost: "0",
          input_price_per_million: null, output_price_per_million: null,
          _providers: new Set(), _colors: [] };
        groups.set(key, row);
      }
      row.model_ids.push(...(source.model_ids || []));
      ["tokens", "gen_tokens", "prompt_tokens", "gen_time", "prompt_time", "loaded_time", "idle_time", "sessions", "active_tasks"].forEach((field) => {
        row[field] += Number(source[field]) || 0;
      });
      row.input_cost = decStrAdd(row.input_cost, source.input_cost);
      row.output_cost = decStrAdd(row.output_cost, source.output_cost);
      row.total_cost = decStrAdd(row.total_cost, source.total_cost);
      if (source.input_price_per_million != null) {
        if (row.input_price_per_million == null) row.input_price_per_million = source.input_price_per_million;
        else if (String(row.input_price_per_million) !== String(source.input_price_per_million)) row.input_price_per_million = "mixed";
      }
      if (source.output_price_per_million != null) {
        if (row.output_price_per_million == null) row.output_price_per_million = source.output_price_per_million;
        else if (String(row.output_price_per_million) !== String(source.output_price_per_million)) row.output_price_per_million = "mixed";
      }
      row.peak_gen = Math.max(row.peak_gen, Number(source.peak_gen) || 0);
      row.peak_prompt = Math.max(row.peak_prompt, Number(source.peak_prompt) || 0);
      row.active_rank = Math.max(row.active_rank, Number(source.active_rank) || 0);
      row.active_seen_at = Math.max(row.active_seen_at, Number(source.active_seen_at) || 0);
      (source.spark || []).forEach((value, index) => { row.spark[index] += Number(value) || 0; });
      if (source.provider) row._providers.add(source.provider);
      if (source.color) row._colors.push(source.color);
    });
    const rows = Array.from(groups.values());
    const totalTokens = rows.reduce((total, row) => total + row.tokens, 0);
    rows.forEach((row) => {
      row.share = totalTokens ? Math.round(row.tokens / totalTokens * 1000) / 10 : 0;
      row.gen_tps = row.gen_time > 0 ? Math.round(row.gen_tokens / row.gen_time * 10) / 10 : null;
      row.inference_s = Math.round(row.prompt_time + row.gen_time);
      row.loaded_s = Math.round(row.loaded_time);
      row.idle_s = Math.round(row.idle_time);
      row.active = row.active_rank > 0;
      row.active_status = row.active_rank === 2 ? "LIVE" : (row.active_rank === 1 ? "FINALIZING" : null);
      row.spark = row.spark.map((value) => Math.round(value));
      row.color = row._colors[0] || OC.blue;
      row.provider = row._providers.size === 1 ? Array.from(row._providers)[0] :
        row._providers.size > 1 ? row._providers.size + " providers" : null;
      row._providers = null; row._colors = null;
    });
    return rows;
  };

  const loadAndRender = () => load().then(render);

  const sorted = () => {
    const rows = st.rows.slice();
    const stableId = (r) => r.key;
    if (st.sort === "active") {
      rows.sort((a, b) =>
        (b.active_rank || 0) - (a.active_rank || 0) ||
        (b.active_tasks || 0) - (a.active_tasks || 0) ||
        (b.active_seen_at || 0) - (a.active_seen_at || 0) ||
        String(a.label).localeCompare(String(b.label)) ||
        stableId(a).localeCompare(stableId(b)));
    } else {
      // Inference means "did the most generation". The previous key was
      // st.sort + "_s", which read inference_s/loaded_s/idle_s -- fields that
      // /api/models returns but groupedRows() does not copy onto the grouped
      // rows, so every comparison saw undefined and the sort did nothing.
      rows.sort((a, b) =>
        (b.gen_tokens || 0) - (a.gen_tokens || 0) ||
        (b.tokens || 0) - (a.tokens || 0) ||
        (b.active_seen_at || 0) - (a.active_seen_at || 0) ||
        String(a.label).localeCompare(String(b.label)) ||
        stableId(a).localeCompare(stableId(b)));
    }
    return rows;
  };

  const renderTopCards = (top) => {
    ch.clear();
    st.top = top || {};
    const spark = new Array(24).fill(0);
    st.rows.forEach((r) => (r.spark || []).forEach((v, i) => { spark[i] += v || 0; }));
    const leader = st.rows.slice().sort((a, b) => b.tokens - a.tokens)[0];
    el("topCards").innerHTML = topCardsHtml(top, { leader: leader, sparkId: "topSpark" });
    ch.spark(el("topSpark"), spark, OC.blue);
  };

  const disposeRuntimeChart = () => {
    if (runtimeChart) {
      const dom = runtimeChart.getDom();
      try { if (!runtimeChart.isDisposed()) ChartRegistry.dispose(dom); } catch (e) {}
      runtimeChart = null;
    }
  };

  const compactRuntimeSeries = () => {
    if (runtimeSeries.timestamps.length <= 240) return;
    const latest = {
      ts: runtimeSeries.timestamps.pop(),
      gen: runtimeSeries.genTps.pop(),
      context: runtimeSeries.context.pop(),
    };
    const limit = 119;
    const start = runtimeSeries.timestamps[0];
    const end = runtimeSeries.timestamps[runtimeSeries.timestamps.length - 1];
    const bucketMs = Math.max(1, Math.ceil((end - start + 1) / limit));
    const buckets = Array.from({ length: limit }, () => ({
      ts: null, gen: 0, genN: 0, context: 0, contextN: 0,
    }));
    runtimeSeries.timestamps.forEach((ts, index) => {
      const bucket = buckets[Math.min(limit - 1, Math.max(0, Math.floor((ts - start) / bucketMs)))];
      bucket.ts = ts;
      const gen = runtimeSeries.genTps[index], context = runtimeSeries.context[index];
      if (gen != null) { bucket.gen += Number(gen); bucket.genN += 1; }
      if (context != null) { bucket.context += Number(context); bucket.contextN += 1; }
    });
    let last = buckets.length - 1;
    while (last > 0 && buckets[last].ts == null) last -= 1;
    const kept = buckets.slice(0, last + 1);
    runtimeSeries.timestamps = kept.map((bucket, index) => bucket.ts == null ? start + index * bucketMs : bucket.ts);
    runtimeSeries.genTps = kept.map((bucket) => bucket.genN ? Number((bucket.gen / bucket.genN).toFixed(1)) : null);
    runtimeSeries.context = kept.map((bucket) => bucket.contextN ? Number((bucket.context / bucket.contextN).toFixed(1)) : null);
    runtimeSeries.timestamps.push(latest.ts);
    runtimeSeries.genTps.push(latest.gen);
    runtimeSeries.context.push(latest.context);
  };

  const syncRuntimeSeries = (live, seed) => {
    const sessionId = live && live.session_id != null ? String(live.session_id) : null;
    if (!sessionId) {
      runtimeSeries = { sessionId: null, timestamps: [], genTps: [], context: [] };
      return;
    }
    if (runtimeSeries.sessionId !== sessionId) {
      runtimeSeries = { sessionId: sessionId, timestamps: [], genTps: [], context: [] };
    }
    const history = live.history;
    if (history && (seed || !runtimeSeries.timestamps.length)) {
      const length = Math.min(
        (history.timestamps || []).length,
        (history.gen_tps || []).length,
        (history.context || []).length,
      );
      runtimeSeries.timestamps = (history.timestamps || []).slice(0, length).map(Number);
      runtimeSeries.genTps = (history.gen_tps || []).slice(0, length);
      runtimeSeries.context = (history.context || []).slice(0, length);
    }
    const observedAt = Number(live.observed_at);
    if (isFinite(observedAt) && observedAt > 0) {
      const last = runtimeSeries.timestamps.length - 1;
      if (last >= 0 && runtimeSeries.timestamps[last] === observedAt) {
        if (live.gen_tps != null) runtimeSeries.genTps[last] = live.gen_tps;
        if (live.context != null) runtimeSeries.context[last] = live.context;
      } else if (last < 0 || observedAt > runtimeSeries.timestamps[last]) {
        runtimeSeries.timestamps.push(observedAt);
        runtimeSeries.genTps.push(live.gen_tps == null ? null : live.gen_tps);
        runtimeSeries.context.push(live.context == null ? null : live.context);
      }
    }
    compactRuntimeSeries();
  };

  // Single source of truth for the derived cell values, so the initial render
  // and an in-place patch can never drift apart.
  const runtimeValues = (live) => {
    live = live || {};
    const ctx = live.context;
    const ctxMax = live.context_max;
    const ctxDetail = ctx != null
      ? fmtNum(ctx) + " / " + (ctxMax ? fmtNum(ctxMax) + " tokens" : "max unavailable")
      : "no context data";
    return {
      slot: live.slot_id,
      task: live.task_id,
      n_gen: live.gen_tokens == null ? "—" : fmtNum(live.gen_tokens),
      tg_avg: live.gen_tps_avg != null ? live.gen_tps_avg.toFixed(2) + " t/s" :
        live.processing ? "warming up" : "—",
      tg_3s: live.gen_tps_3s != null ? live.gen_tps_3s.toFixed(2) + " t/s" :
        live.processing ? "warming up" : "—",
      observed: fmtObservedAge(live.age_s),
      ctxDetail: ctxDetail,
      ctxCenter: live.context_pct != null ? Math.round(live.context_pct) + "%" :
        (ctx != null ? fmtTokens(ctx) : "—"),
    };
  };

  // Only a structural change (runtime observation appearing or disappearing, a
  // chart appearing or disappearing) needs the markup rebuilt.
  const runtimeShapeOf = (live) => {
    live = live || {};
    return (live.source ? "full" : "empty") + ":" +
      (live.session_id != null ? "chart" : "nochart");
  };

  const patchRuntime = (target, live) => {
    const values = runtimeValues(live);
    ["slot", "task", "n_gen", "tg_avg", "tg_3s", "observed"].forEach((key) => {
      const node = target.querySelector('[data-rt="' + key + '"]');
      if (!node) return;
      const next = values[key] == null ? "—" : String(values[key]);
      if (node.textContent !== next) node.textContent = next;
    });
    const badge = target.querySelector(".runtime-badge");
    if (badge) {
      const status = (live && live.status) || "NO DATA";
      const stale = live && !live.processing && live.age_s != null &&
        live.age_s > STALE_OBSERVATION_S;
      const cls = "runtime-badge status-" + status.toLowerCase().replace(/\s+/g, "-") +
        (stale ? " is-stale" : "");
      if (badge.className !== cls) badge.className = cls;
      const text = "● " + status + (stale ? " · " + fmtObservedAge(live.age_s) : "");
      if (badge.textContent !== text) badge.textContent = text;
    }
    // The gauge is plain SVG with no chart instance behind it, so re-rendering
    // this small subtree is safe; the chart container is untouched.
    const gauge = target.querySelector('[data-rt="gauge"]');
    if (gauge) {
      gauge.innerHTML = resourceGauge("CONTEXT", live && live.context_pct,
                                      values.ctxCenter, values.ctxDetail,
                                      "Context " + values.ctxDetail);
    }
  };

  const runtimeHtml = (live) => {
    live = live || { status: "NO DATA", snapshot: "NO DATA" };
    const status = live.status || "NO DATA";
    // A retained "last live" observation keeps reporting the tokens and speed
    // of the request that produced it. Once it is no longer current, say so on
    // the badge instead of rendering it identically to a live one.
    const stale = !live.processing && live.age_s != null && live.age_s > STALE_OBSERVATION_S;
    const statusHtml = '<span class="runtime-badge status-' + esc(status.toLowerCase().replace(/\s+/g, "-")) +
      (stale ? " is-stale" : "") + '">● ' + esc(status) +
      (stale ? esc(" · " + fmtObservedAge(live.age_s)) : "") + '</span>';
    if (!live.source) {
      return '<div class="live-timing"><div class="live-timing-head"><span>RUNTIME TELEMETRY</span>' +
        statusHtml + '</div><div class="live-timing-note">No reliable runtime observation exists for this selection.</div></div>';
    }
    // data-rt hooks let an ordinary live update patch these values without
    // replacing the DOM (see patchRuntime).
    const item = (key, label, value) => '<span class="live-timing-item"><i>' + esc(label) +
      '</i><b data-rt="' + key + '">' + esc(value == null ? "—" : value) + "</b></span>";
    const vals = runtimeValues(live);
    const age = vals.observed;
    const ctxDetail = vals.ctxDetail;
    const ctxCenter = vals.ctxCenter;
    return '<div class="live-timing"><div class="live-timing-head"><span>RUNTIME TELEMETRY</span>' +
      statusHtml + '</div><div class="runtime-layout"><div class="runtime-main"><div class="live-timing-grid">' +
      item("slot", "slot", live.slot_id) + item("task", "task", live.task_id) +
      item("n_gen", "n_gen", runtimeValues(live).n_gen) +
      item("tg_avg", "tg avg", runtimeValues(live).tg_avg) +
      item("tg_3s", "tg 3s", runtimeValues(live).tg_3s) +
      item("observed", "observed", age) + '</div></div><div class="runtime-context" data-rt="gauge">' +
      resourceGauge("CONTEXT", live.context_pct, ctxCenter, ctxDetail, "Context " + ctxDetail) +
      '</div></div>' + (live.session_id != null ?
        '<div class="runtime-history"><div class="runtime-chart-key"><span><i class="runtime-key-tps"></i>TK/S</span>' +
        '<span><i class="runtime-key-context"></i>CONTEXT</span></div>' +
        '<div class="runtime-chart" id="runtimeChart" role="img" aria-label="Generated tokens per second and context size across the current session"></div></div>' : "") +
      '</div>';
  };

  const renderRuntimeChart = () => {
    const box = el("runtimeChart");
    if (!box || typeof echarts === "undefined" || !runtimeSeries.timestamps.length) return;
    runtimeChart = ChartRegistry.init(box, {
      animation: false,
      backgroundColor: "transparent",
      grid: { left: 1, right: 1, top: 2, bottom: 0 },
      tooltip: {
        trigger: "axis", backgroundColor: "#1e1e1b", borderColor: OC.border,
        borderWidth: 1, padding: [5, 8], textStyle: { color: OC.text, fontSize: 10 },
      },
      xAxis: {
        type: "category", show: false,
        data: runtimeSeries.timestamps.map((ts) => fmtClock(ts, true)),
      },
      yAxis: [
        { type: "value", scale: true, show: false },
        { type: "value", scale: true, show: false },
      ],
      series: [
        { name: "tk/s", type: "line", yAxisIndex: 0, data: runtimeSeries.genTps,
          showSymbol: false, smooth: 0.2, connectNulls: false,
          lineStyle: { width: 1.25, color: OC.orange }, itemStyle: { color: OC.orange },
          areaStyle: { color: OC.orange + "26" },
          tooltip: { valueFormatter: (v) => v == null ? "—" : Number(v).toFixed(1) + " t/s" } },
        { name: "context", type: "line", yAxisIndex: 1, data: runtimeSeries.context,
          showSymbol: false, smooth: 0.2, connectNulls: false,
          lineStyle: { width: 1.15, color: OC.green }, itemStyle: { color: OC.green },
          areaStyle: { color: OC.green + "20" },
          tooltip: { valueFormatter: (v) => v == null ? "—" : fmtNum(v) + " tokens" } },
      ],
    });
  };

  const updateRuntime = (live, seed) => {
    const target = el("selRuntime");
    if (!target) return;
    syncRuntimeSeries(live || {}, !!seed);
    const shape = runtimeShapeOf(live);
    if (shape !== runtimeShape || !target.querySelector("[data-rt]")) {
      // Structure actually changed. Replacing the markup destroys the node the
      // chart registry is keyed on, so the instance must be disposed with it.
      disposeRuntimeChart();
      target.innerHTML = runtimeHtml(live);
      runtimeShape = shape;
      renderRuntimeChart();
      return;
    }
    // Ordinary live update: patch values and let ChartRegistry.init reuse the
    // existing instance in place. Rebuilding the DOM here on every SSE tick
    // orphaned the registry entry and re-created the canvas once per tick.
    patchRuntime(target, live);
    renderRuntimeChart();
  };

  const renderSelected = (row) => {
    const box = el("selPanel");
    if (selChart) { try { selChart.dispose(); } catch (e) {} selChart = null; }
    disposeRuntimeChart();
    runtimeShape = null;
    runtimeSeries = { sessionId: null, timestamps: [], genTps: [], context: [] };
    selectedRealtime = null;
    selectedWasActive = false;
    if (!row) { box.innerHTML = '<div class="empty">select a model</div>'; return; }
    const requestedKey = row.key;
    // Immediately render known shared-summary data from the row
    const pt = row.prompt_tokens || 0, gt = row.gen_tokens || 0, tot = (pt + gt) || 1;
    box.innerHTML =
      '<div class="sel-head"><span class="m-dot" style="background:' + esc(row.color) + '"></span>' +
      '<span class="tag">SELECTED</span><span class="sel-name">' + esc(row.label) + '</span>' +
      '<a class="capture-btn" href="about:blank" target="_blank" data-capture-target="selPanel" data-capture-name="selected-model" ' +
      'data-capture-width="600" ' +
      'data-capture-ignore aria-label="Capture selected model card as an image">CAPTURE</a></div>' +
      '<div class="mc-sub" style="margin-top:2px">' + esc(row.provider || "") + " · range " + st.range + "</div>" +
      '<div id="selRuntime">' + runtimeHtml(null) + "</div>" +
      '<div class="sel-grid" id="selTokens">' +
      selMetric("INPUT", fmtTokens(pt), "prompt tokens") +
      selMetric("OUTPUT", fmtTokens(gt), "generated tokens") +
      selMetric("TOTAL", fmtTokens(row.tokens), row.share != null ? row.share + "% of all" : "") +
      "</div>" +
      '<div class="sel-grid" id="selCosts">' +
      selCostMetrics(row) +
      selMetric("SESSIONS", row.sessions || "—",
        row.gen_tps != null ? "avg " + row.gen_tps + " t/s" : "") +
      "</div>" +
      '<div class="sel-extra" id="selExtra"></div>';
    // Fetch detailed data with abort support
    abortSelected();
    _selAbort = new AbortController();
    const _selSignal = _selAbort.signal;
    fetch("/api/models/selected?ids=" + row.model_ids.join(",") + "&range=" + st.range, { signal: _selSignal }).then((r) => {
      if (_selSignal.aborted) return;
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then((s) => {
      if (_selSignal.aborted || requestedKey !== st.selectedKey) return;
      patchSelectedStats(s, true);
    }).catch((e) => {
      if (e && e.name === "AbortError") return;
    });
  };

  // Patch the numeric cards of an already-rendered selected panel in place.
  // Never rebuilds the panel, so the runtime chart and the current selection
  // survive a background refresh.
  const patchSelectedStats = (s, seedRuntime) => {
    if (!s || !s.label) return;
    const sPt = s.prompt_tokens || 0, sGt = s.gen_tokens || 0;
    const hasMtp = s.mtp_proposed != null && s.mtp_proposed > 0;
    const live = s.live && !s.live.tasks ? s.live : (s.live && s.live.tasks ? s.live.tasks[0] : null);
    // Patch token cards with precise API values
    const tokEl = el("selTokens");
    if (tokEl) tokEl.innerHTML =
      selMetric("INPUT", fmtTokens(sPt), "prompt tokens") +
      selMetric("OUTPUT", fmtTokens(sGt), "generated tokens") +
      selMetric("TOTAL", fmtTokens(s.tokens), s.share != null ? s.share + "% of all models" : "") +
      selMetric("GENERATED", s.generated_pct != null ? s.generated_pct + "%" : "—", fmtTokens(sGt) + " generated");
    // Patch cost cards
    const costEl = el("selCosts");
    if (costEl) costEl.innerHTML =
      selCostMetrics(s) +
      selMetric("SESSIONS", s.sessions != null ? s.sessions : "—",
        s.per_session != null ? fmtTokens(s.per_session) + " tokens each" : "");
    // Extra details: MTP, peak, stack bar
    const extraEl = el("selExtra");
    if (extraEl) {
      const sTot = (sPt + sGt) || 1;
      extraEl.innerHTML =
        (s.peak_gen != null || s.peak_prompt != null ?
          '<div class="sel-grid">' +
          selMetric("PEAK GEN", s.peak_gen != null ? s.peak_gen + " t/s" : "—",
            s.gen_tps != null ? "avg " + s.gen_tps + " t/s" : "") +
          selMetric("PEAK PROMPT", s.peak_prompt != null ? s.peak_prompt + " t/s" : "—", fmtTokens(sPt) + " prompt") +
          "</div>" : "") +
        (hasMtp ?
          '<div class="sel-grid">' +
          selMetric("MTP ACCEPT", s.mtp_acc != null ? s.mtp_acc + "%" : "—",
            fmtTokens(s.mtp_accepted) + " / " + fmtTokens(s.mtp_proposed)) +
          selMetric("MTP DRAFTS", fmtTokens(s.mtp_accepted) + " acc",
            fmtTokens(s.mtp_rejected) + " rej") +
          "</div>" : "") +
        '<div class="stackbar"><i style="width:' + (sPt / sTot * 100) + "%;background:" + OC.amber + '"></i>' +
        '<i style="width:' + (sGt / sTot * 100) + "%;background:" + OC.green + '"></i></div>' +
        '<div class="legend"><span><i style="background:' + OC.amber + '"></i>prompt ' + fmtTokens(sPt) +
        '</span><span><i style="background:' + OC.green + '"></i>generated ' + fmtTokens(sGt) + "</span></div>";
    }
    const runtime = selectedRealtime || s.realtime;
    selectedWasActive = !!(runtime && ["LIVE", "FINALIZING"].includes(runtime.status));
    // While SSE is driving a live model it owns the runtime strip; re-seeding
    // it here would dispose and rebuild the chart on every refresh.
    if (seedRuntime || !selectedRealtime) updateRuntime(runtime, seedRuntime);
  };

  // The sparkline column is already served by /api/models, which returns
  // per-model spark buckets that groupedRows() sums into each displayed row.
  // A second /api/models/sparks request was not just redundant, it destroyed
  // that data: it is keyed by *group identity* (family/quant name in group
  // modes) while the lookup here was by model id, so on the default family
  // grouping nothing matched and every sparkline was overwritten with an empty
  // one right after it rendered correctly.

  const renderTable = () => {
    const rows = sorted();
    const unit = st.group === "file" ? "files" : st.group === "quant" ? "quants" : "families";
    el("mModelsCount").textContent = rows.length ? rows.length + " " + unit + " · " + st.range : "";
    const tb = el("modelTableBody");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="12"><div class="empty">no model activity in this range</div></td></tr>';
      return;
    }
    tb.innerHTML = rows.map((r, i) =>
      '<tr class="clickable' + (r.key === st.selectedKey ? " selected" : "") + '" data-key="' + esc(r.key) + '">' +
      '<td class="m-rank">' + (i + 1) + "</td>" +
      '<td class="m-name col-model"><span class="m-dot" style="background:' + esc(r.color) + '"></span> ' + esc(r.label) +
      (r.provider ? '<span class="m-prov">' + esc(r.provider) + "</span>" : "") +
      (r.active_rank ? ' <span class="row-live status-' + (r.active_status || "LIVE").toLowerCase() + '">● ' +
        esc(r.active_status || "LIVE") + '</span>' : "") + "</td>" +
      '<td class="col-activity">' + (r.active_rank ?
        '<span class="task-count">' + (r.active_tasks || 0) + 't</span>' : '<span class="muted">—</span>') + "</td>" +
      '<td class="num">' + fmtTokens(r.prompt_tokens) + "</td>" +
      '<td class="num">' + fmtTokens(r.gen_tokens) + "</td>" +
      '<td class="num"><b>' + fmtTokens(r.tokens) + "</b></td>" +
      '<td class="num">' + costPair(r.input_cost) + "</td>" +
      '<td class="num">' + costPair(r.output_cost) + "</td>" +
      '<td class="num">' + costPair(r.total_cost) + "</td>" +
      '<td class="num">' + r.sessions + "</td>" +
      '<td class="num">' + (r.gen_tps != null ? r.gen_tps : "—") + "</td>" +
      '<td class="spark-cell">' + svgSpark(r.spark, r.color, 64, 18) + "</td>" +
      "</tr>").join("");
    tb.querySelectorAll("tr.clickable").forEach((tr) => {
      tr.onclick = () => {
        st.selectedKey = tr.dataset.key;
        tb.querySelectorAll("tr").forEach((x) => x.classList.remove("selected"));
        tr.classList.add("selected");
        const row = st.rows.find((x) => x.key === st.selectedKey);
        if (row) renderSelected(row);
      };
    });
  };

  const renderGroups = () => {
    st.rows = groupedRows(st.sourceRows);
    if (st.selectedKey == null) st.selectedKey = st.rows.length ? sorted()[0].key : null;
    if (st.selectedKey && !st.rows.find((r) => r.key === st.selectedKey)) {
      st.selectedKey = st.rows.length ? sorted()[0].key : null;
    }
    renderTopCards(st.top || {});
    renderTable();
    const row = st.rows.find((r) => r.key === st.selectedKey);
    renderSelected(row || null);
  };

  const render = (d) => {
    st.sourceRows = d.rows || [];
    st.top = d.top || {};
    renderGroups();
  };

  segControl("grpSeg", GROUPS.map((g) => GROUP_LABELS[g]), GROUP_LABELS[st.group], (lbl) => {
    st.group = GROUPS.find((g) => GROUP_LABELS[g] === lbl) || "family";
    fetch("/api/settings/display", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_group: st.group }),
    }).catch(() => {});
    renderGroups();
  });
  segControl("rngSeg", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[st.range], (lbl) => {
    abortSelected();
    st.range = RANGES.find((r) => RANGE_LABELS[r] === lbl) || "7d";
    fetch("/api/settings/display", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_range: st.range }),
    }).catch(() => {});
    loadAndRender();
  });
  segControl("sortSeg", METRIC_KEYS.map((k) => METRIC_LABELS[k]), METRIC_LABELS[st.sort], (lbl) => {
    st.sort = METRIC_KEYS.find((k) => METRIC_LABELS[k] === lbl) || "active";
    renderTable();
  });

  window.__onLive = (d) => {
    const activeModels = (d && d.active_models) || [];
    const unrepresented = activeModels.some((a) => !st.rows.some((r) => (r.model_ids || []).includes(a.model_id)));
    const nowMs = Date.now();
    if (unrepresented && nowMs - _reconcileTimer >= 30000 && !_reconciling) {
      _reconcileTimer = nowMs;
      _reconciling = true;
      load().then((fresh) => {
        render(fresh);
      }).catch(() => {}).finally(() => { _reconciling = false; });
    }
    const signature = activeModels.map((x) => [x.model_id, x.rank, x.task_count].join(":"))
      .sort().join("|");
    if (signature !== activeSignature) {
      activeSignature = signature;
      st.sourceRows.forEach((r) => {
        r.active = false; r.active_rank = 0; r.active_tasks = 0;
        r.active_seen_at = null; r.active_status = null;
      });
      activeModels.forEach((a) => {
        st.sourceRows.filter((r) => (r.model_ids || []).includes(a.model_id)).forEach((r) => {
          r.active = true;
          r.active_rank = Math.max(r.active_rank || 0, a.rank || 0);
          r.active_tasks = (r.active_tasks || 0) + (a.task_count || 0);
          r.active_seen_at = Math.max(r.active_seen_at || 0, a.latest_seen || 0);
          r.active_status = r.active_rank === 2 ? "LIVE" : "FINALIZING";
        });
      });
      st.rows = groupedRows(st.sourceRows);
      renderTable();
    }
    const row = st.rows.find((r) => r.key === st.selectedKey);
    const selectedActive = row ? activeModels.filter((a) => (row.model_ids || []).includes(a.model_id)) : [];
    selectedActive.sort((a, b) => (b.rank || 0) - (a.rank || 0) ||
      (b.task_count || 0) - (a.task_count || 0) || (b.latest_seen || 0) - (a.latest_seen || 0));
    if (selectedActive.length) {
      selectedRealtime = selectedActive[0].realtime;
      updateRuntime(selectedRealtime, false);
      selectedWasActive = true;
    } else if (selectedWasActive && row) {
      selectedWasActive = false;
      selectedRealtime = null;
      api("/api/models/selected?ids=" + row.model_ids.join(",") + "&range=" + st.range)
        .then((s) => { if (row.key === st.selectedKey) updateRuntime(s.realtime, true); })
        .catch(() => {});
    }
  };

  // The Models page previously loaded once and then only ever changed via SSE,
  // which patches the runtime strip alone. Everything else on the selected card
  // (tokens, costs, sessions, MTP) stayed frozen for as long as the page was
  // open. Refresh the underlying rows on an interval, mirroring Overview.
  const refreshSelectedStats = (row) => {
    if (_refreshAbort) { try { _refreshAbort.abort(); } catch (e) {} }
    _refreshAbort = new AbortController();
    const signal = _refreshAbort.signal;
    const requestedKey = row.key;
    fetch("/api/models/selected?ids=" + row.model_ids.join(",") + "&range=" + st.range, { signal })
      .then((r) => {
        if (signal.aborted) return;
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((s) => {
        if (signal.aborted || requestedKey !== st.selectedKey) return;
        patchSelectedStats(s, false);
      })
      .catch(() => {});
  };

  const softRefresh = () => {
    if (_reconciling || document.hidden) return;
    _reconciling = true;
    load().then((d) => {
      const previousKey = st.selectedKey;
      st.sourceRows = d.rows || [];
      st.top = d.top || {};
      st.rows = groupedRows(st.sourceRows);
      if (st.selectedKey && !st.rows.find((r) => r.key === st.selectedKey)) {
        st.selectedKey = st.rows.length ? sorted()[0].key : null;
      }
      renderTopCards(st.top || {});
      renderTable();
      const row = st.rows.find((r) => r.key === st.selectedKey);
      if (!row) renderSelected(null);
      else if (row.key !== previousKey) renderSelected(row);
      else refreshSelectedStats(row);
    }).catch(() => {}).finally(() => { _reconciling = false; });
  };
  setInterval(softRefresh, MODELS_REFRESH_MS);

  loadAndRender();
}
/* Observatory - dashboard app logic (part B: detail, sessions, compare, hw, settings) */

/* ------------------------------------------------------------- model detail */
function initModelDetail(meta) {
  const mid = (window.QUERY && window.QUERY.mid) || 0;
  const ch = makeCharts();
  const st = { range: "24h" };
  const load = () => api("/api/model/" + mid + "?range=" + st.range);

  // Every SSE tick re-renders this page.  It used to dispose all five ECharts
  // instances and rebuild them, and to reassign innerHTML on every section
  // whether or not anything in it had changed -- so a live model destroyed and
  // recreated its charts once a second, losing canvas identity, tooltips and
  // any interaction in progress.  Nothing structural actually changes between
  // ticks: the same nodes hold the same surfaces, only the values move.
  //
  // So charts are now updated in place through the registry (which reuses the
  // instance bound to a node), and markup is assigned only when it differs
  // from what the node already shows.  Sections driven by live values still
  // update on every tick; the rest are left untouched.
  const lastHtml = new Map();
  const setHtml = (node, html) => {
    if (!node || lastHtml.get(node) === html) return;
    lastHtml.set(node, html);
    node.innerHTML = html;
  };
  // The MTP panel is the one surface that swaps between a chart and an empty
  // state, which is a structural change: the instance must be disposed before
  // its canvas is overwritten, and the node cleared before a chart is built
  // over the empty state.
  let mtpMode = null;
  let cfgKey = null;

  const render = (d) => {
    if (!d || !d.model) return;
    const m = d.model;
    const live = m.live && !m.live.tasks ? m.live : (m.live && m.live.tasks ? m.live.tasks[0] : null);
    setHtml(el("mdlHead"),
      '<span class="m-dot" style="background:' + esc(m.color) + ';width:10px;height:10px"></span>' +
      '<span style="font-size:14px;font-weight:600">' + esc(m.name) + "</span>" +
      (m.quant ? '<span class="badge">' + esc(m.quant) + "</span>" : "") +
      (m.family && m.family !== m.name ? '<span class="muted">· ' + esc(m.family) + "</span>" : "") +
      (m.params ? '<span class="muted">· ' + esc(m.params) + "</span>" : "") +
      stateBadge(m.live_state) +
      '<span class="muted" style="font-size:11px">' + esc(m.provider || "") + "</span>" +
      '<a href="/models" class="muted" style="font-size:11px;margin-left:12px">← models</a>');

    setHtml(el("mdlCards"),
      metricCard("TOKENS", fmtTokens(d.tokens.total),
        fmtTokens(d.tokens.prompt) + " prompt · " + fmtTokens(d.tokens.generated) + " gen", null) +
      metricCard("INFERENCE", fmtDur(d.accounting.inference_s),
        d.accounting.utilization != null ? d.accounting.utilization + "% of loaded time" : "", null) +
      metricCard(live ? "LIVE GEN" : "AVG GEN", live ? fmtTokens(live.gen_tokens) :
        (d.speeds.avg_gen_tps != null ? d.speeds.avg_gen_tps + " t/s" : "—"),
        live ? ((live.gen_tps != null ? live.gen_tps + " t/s · " : "") + "provisional") :
        (d.speeds.peak_gen_tps ? "peak " + d.speeds.peak_gen_tps + " t/s" : ""), null) +
      metricCard("AVG PROMPT", d.speeds.avg_prompt_tps != null ? d.speeds.avg_prompt_tps + " t/s" : "—",
        d.speeds.peak_prompt_tps ? "peak " + d.speeds.peak_prompt_tps + " t/s" : "", null) +
      metricCard("MTP ACCEPT", d.mtp_acc != null ? d.mtp_acc + "%" : "—",
        d.mtp_proposed ? d.mtp_proposed + " proposed · " + d.mtp_accepted + " accepted" : "no MTP counters", null));

    const acc = d.accounting;
    const total = Math.max(1, acc.loaded_s || 1);
    setHtml(el("mdlAccBar"),
      '<i style="width:' + ((acc.prompt_s || 0) / total * 100) + "%;background:" + OC.amber + '"></i>' +
      '<i style="width:' + ((acc.gen_s || 0) / total * 100) + "%;background:" + OC.green + '"></i>' +
      '<i style="width:' + ((acc.idle_s || 0) / total * 100) + "%;background:#3a3a36" + '"></i>');
    setHtml(el("mdlAccLegend"),
      '<span><i style="background:' + OC.amber + '"></i>prompt ' + fmtDur(acc.prompt_s) + "</span>" +
      '<span><i style="background:' + OC.green + '"></i>generation ' + fmtDur(acc.gen_s) + "</span>" +
      '<span><i style="background:#3a3a36"></i>idle ' + fmtDur(acc.idle_s) + "</span>" +
      '<span class="muted">loaded ' + fmtDur(acc.loaded_s) + " · " + d.sessions + " sessions</span>");
    el("mdlAccSub").textContent = "in " + st.range;

    const g = d.graphs;
    ch.add(el("mdlTokChart"), barOption(g.labels,
      [{ name: "tokens", color: OC.blue, data: g.series.tokens }]));
    ch.add(el("mdlSpeed"), lineOption(g.labels, [
      { name: "gen t/s", color: OC.green, data: g.series.gen_tps },
      { name: "prompt t/s", color: OC.amber, data: g.series.prompt_tps },
    ]));
    ch.add(el("mdlCtx"), lineOption(g.labels,
      [{ name: "context used", color: OC.blue, data: g.series.context, area: true }]));
    const mtpNode = el("mdlMtp");
    if ((g.series.mtp_acc || []).filter((v) => v != null).length) {
      // Clear the empty state before building over it, so ECharts does not
      // initialise on top of leftover markup.
      if (mtpMode !== "chart") { mtpNode.innerHTML = ""; mtpMode = "chart"; }
      ch.add(mtpNode, lineOption(g.labels,
        [{ name: "mtp acc %", color: OC.orange, data: g.series.mtp_acc }]));
    } else if (mtpMode !== "empty") {
      ch.drop(mtpNode);
      mtpNode.innerHTML = '<div class="empty">no MTP data in range</div>';
      mtpMode = "empty";
    }
    const mdlGpuSeries = gpuDualSeries(g.gpus || []);
    ch.add(el("mdlHw"), dualAxisOption(g.labels, mdlGpuSeries.length ? mdlGpuSeries : [
      { name: "gpu 0 util %", color: OC.green, data: g.series.gpu_util, y: 0 },
      { name: "gpu 0 VRAM MB", color: OC.blue, data: g.series.vram_mb, y: 1 },
    ]));
    setHtml(el("mdlGpuCards"), gpuSummaryCards(g.gpus || [], "range"));

    // The configuration panel is not live data: it changes when the model is
    // reloaded with different flags, not on every tick.  cfgPanelHtml writes
    // several nodes itself, so it is gated on the config actually differing.
    const nextCfgKey = JSON.stringify([d.config || null, (d.configs || []).length]);
    if (nextCfgKey !== cfgKey) {
      cfgKey = nextCfgKey;
      cfgPanelHtml(d.config, "mdlCfg", "mdlFlags", el("mdlCfgSub"));
      const many = !!(d.configs && d.configs.length > 1);
      // Set both ways: a model that gains a second configuration while the
      // page is open has to reveal the table, not stay hidden from an earlier
      // render.
      el("mdlCfgHistWrap").style.display = many ? "" : "none";
      if (many) {
        setHtml(el("mdlCfgHistBody"), d.configs.map((c) =>
          "<tr><td>" + esc(c.fingerprint) + "</td><td>" + fmtDate(c.created_at) +
          '</td><td class="num">' + (c.context != null ? fmtNum(c.context) : "—") +
          '</td><td class="num">' + (c.kv_cache_k || "—") +
          '</td><td class="num">' + (c.mtp_enabled ? "on" : c.mtp_enabled === false ? "off" : "—") +
          '</td><td class="num">' + (c.threads != null ? c.threads : "—") + "</td></tr>").join(""));
      }
    }
  };

  segControl("mdlRange", DETAIL_RANGES, "24h", (r) => { st.range = r; load().then(render); });
  load().then(render);
  window.__onLive = () => load().then(render).catch(() => {});
}

/* ------------------------------------------------------------------ sessions */
function initSessions(meta) {
  const st = { range: "7d", provider: "", model: "", quant: "", mtp: "any", reasoning: "" };
  const providers = (meta && meta.providers) || [];

  const params = () => {
    const q = [];
    if (st.provider) q.push("provider=" + st.provider);
    if (st.model) q.push("model=" + st.model);
    if (st.quant) q.push("quant=" + encodeURIComponent(st.quant));
    if (st.mtp !== "any") q.push("mtp=" + st.mtp);
    if (st.reasoning) q.push("reasoning=" + encodeURIComponent(st.reasoning));
    q.push("range=" + st.range);
    return "/api/sessions?" + q.join("&");
  };

  const uniqVals = (arr) => {
    const seen = new Set(), out = [];
    for (const v of arr) {
      if (v == null || v === "" || seen.has(String(v))) continue;
      seen.add(String(v)); out.push(v);
    }
    return out;
  };

  const render = (d) => {
    const modelPairs = uniqVals(d.sessions.map((x) => [x.model_id, x.model]))
      .map((id) => d.sessions.find((x) => x.model_id === id))
      .filter((x) => x && x.model_id)
      .map((x) => [x.model_id, x.model]);
    const quantOpts = uniqVals(d.sessions.map((x) => x.quant)).map((q) => [q, q]);
    if (st.model && !modelPairs.some(([id]) => String(id) === String(st.model))) st.model = "";
    if (st.quant && !quantOpts.some(([q]) => q === st.quant)) st.quant = "";
    fillSelect(el("fProvider"), "All providers", providers.map((p) => [p.id, p.name]), st.provider);
    fillSelect(el("fModel"), "All models", modelPairs, st.model);
    fillSelect(el("fQuant"), "All quants", quantOpts, st.quant);
    el("sessCount").textContent = d.sessions.length + " sessions";

    const tb = el("sessBody");
    if (!d.sessions.length) {
      tb.innerHTML = '<tr><td colspan="12"><div class="empty">no sessions match the filters</div></td></tr>';
      return;
    }
    tb.innerHTML = d.sessions.map((x) =>
      (() => {
      const live = x.live || null;
      const promptTokens = live ? live.prompt_tokens : x.prompt_tokens;
      const genTokens = live ? live.gen_tokens : x.gen_tokens;
      const genTps = live && live.gen_tps != null ? live.gen_tps : x.avg_gen_tps;
      const duration = live ? Math.max(0, (d.now - x.start) / 1000) : x.duration_s;
      const status = x.status === "ACTIVE" ? '<span class="badge badge-demo">LIVE</span>' :
        x.status === "FINALIZING" ? '<span class="badge">FINALIZING</span>' :
        x.status === "INTERRUPTED" ? '<span class="muted">interrupted</span>' :
        x.status === "INCOMPLETE" ? '<span class="muted">incomplete</span>' : '<span class="muted">closed</span>';
      return '<tr class="clickable" data-id="' + x.id + '">' +
      "<td>" + fmtDate(x.start) + '</td><td class="muted">' + fmtAgo(x.start, d.now) + "</td>" +
      '<td><span class="m-dot" style="background:' + esc(x.color || "#74736e") + '"></span> ' + esc(x.model || "—") + "</td>" +
      '<td class="muted">' + esc(x.provider || "") + "</td>" +
      "<td>" + (x.quant ? '<span class="badge">' + esc(x.quant) + "</span>" : "—") + "</td>" +
      '<td class="num">' + fmtDur(duration) + "</td>" +
      '<td class="num">' + fmtTokens(promptTokens) + (live ? ' <span class="muted">live</span>' : '') + "</td>" +
      '<td class="num">' + fmtTokens(genTokens) + (live ? ' <span class="muted">live</span>' : '') + "</td>" +
      '<td class="num">' + (x.prompt_tps != null ? x.prompt_tps : "—") + "</td>" +
      '<td class="num">' + (genTps != null ? genTps : "—") + (live ? ' <span class="muted">live</span>' : '') + "</td>" +
      '<td class="num">' + (x.mtp_acc != null ? x.mtp_acc + "%" :
        (x.mtp_enabled == null ? "—" : x.mtp_enabled ? "on" : "off")) + "</td>" +
      '<td>' + status + "</td>" +
      "</tr>";
      })()).join("");
    tb.querySelectorAll("tr.clickable").forEach((tr) => {
      tr.onclick = () => { location.href = "/session/" + tr.dataset.id; };
    });
  };

  let pending = false;
  let refreshTimer = null;
  const reload = () => {
    if (pending) return;
    pending = true;
    api(params()).then(render).catch((error) => console.error("sessions", error)).finally(() => {
      pending = false;
      if (!document.hidden) refreshTimer = window.setTimeout(reload, 2000);
    });
  };
  const refreshNow = () => { if (refreshTimer) window.clearTimeout(refreshTimer); reload(); };
  el("fProvider").onchange = (e) => { st.provider = e.target.value; st.model = ""; refreshNow(); };
  el("fModel").onchange = (e) => { st.model = e.target.value; refreshNow(); };
  el("fQuant").onchange = (e) => { st.quant = e.target.value; refreshNow(); };
  el("fReasoning").onchange = (e) => { st.reasoning = e.target.value; refreshNow(); };
  segControl("sessRange", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[st.range], (lbl) => {
    st.range = RANGES.find((r) => RANGE_LABELS[r] === lbl) || "7d"; refreshNow();
  });
  segControl("sessMtp", ["any", "on", "off"], "any", (v) => { st.mtp = v; refreshNow(); });
  reload();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshNow(); });
  return Promise.resolve();
}

/* ------------------------------------------------------------- session detail */
function initSessionDetail(meta) {
  const sid = (window.QUERY && window.QUERY.sid) || 0;
  const ch = makeCharts();
  return api("/api/session/" + sid).then((d) => {
    if (!d || !d.session) return;
    const s = d.session;
    const live = s.live || null;
    const displayDuration = live ? Math.max(0, (d.now - s.start) / 1000) : s.duration_s;
    el("sdHead").innerHTML =
      '<div class="kv-inline"><span class="l">MODEL</span><span class="v"><span class="m-dot" style="background:' +
      esc(s.color || "#74736e") + '"></span> ' +
      (s.model_id ? '<a href="/model/' + s.model_id + '">' + esc(s.model || "—") + "</a>" : esc(s.model || "—")) +
      "</span></div>" +
      '<div class="kv-inline"><span class="l">QUANT</span><span class="v">' + (s.quant ? esc(s.quant) : "—") + "</span></div>" +
      '<div class="kv-inline"><span class="l">PROVIDER</span><span class="v">' + esc(s.provider || "—") + "</span></div>" +
      '<div class="kv-inline"><span class="l">STARTED</span><span class="v">' + fmtDate(s.start) + "</span></div>" +
      '<div class="kv-inline"><span class="l">DURATION</span><span class="v">' + fmtDur(displayDuration) + "</span></div>" +
      '<div class="kv-inline"><span class="l">STATUS</span><span class="v">' +
      (s.status === "ACTIVE" ? '<span class="badge badge-demo">LIVE</span>' :
        s.status === "FINALIZING" ? '<span class="badge">FINALIZING</span>' : esc((s.status || "closed").toLowerCase())) + "</span></div>";

    el("sdCards").innerHTML =
      metricCard("PROMPT TOK", fmtTokens(live ? live.prompt_tokens : s.prompt_tokens),
        s.prompt_tps != null ? s.prompt_tps + " t/s · " + fmtDur(s.prompt_time_s) : fmtDur(s.prompt_time_s), null) +
      metricCard("GEN TOK", fmtTokens(live ? live.gen_tokens : s.gen_tokens),
        (live ? "live " + (live.gen_tps != null ? live.gen_tps + " t/s" : "in progress") :
          (s.avg_gen_tps != null ? "avg " + s.avg_gen_tps + " t/s · " : "") + fmtDur(s.gen_time_s)), null) +
      metricCard("TTFT", s.ttft_s != null ? s.ttft_s + "s" : "—",
        s.peak_gen_tps ? "peak gen " + s.peak_gen_tps + " t/s" : "", null) +
      metricCard("MTP", s.mtp_acc != null ? s.mtp_acc + "%" :
        (s.mtp_enabled == null ? "—" : s.mtp_enabled ? "on" : "off"),
        s.mtp_proposed ? s.mtp_proposed + " proposed · " + s.mtp_accepted + " accepted" : "", null) +
      metricCard("CONTEXT", live && live.context ? fmtNum(live.context) : (s.context_max ? fmtNum(s.context_max) : "—"),
        live ? "live · provisional" : "peak used in session", null) +
      metricCard("HARDWARE", (s.gpus || []).length ? (s.gpus.length + " GPUs") :
        (s.gpu_util_avg != null ? s.gpu_util_avg + "%" : "—"),
        (s.gpus || []).length ? "per-GPU host activity" :
          (s.vram_used_mb ? fmtBytes(s.vram_used_mb) + " VRAM" : "no agent data"), null);

    const g = d.graphs;
    ch.add(el("sdSpeed"), lineOption(g.labels, [
      { name: "gen t/s", color: OC.green, data: g.series.gen_tps },
      { name: "prompt t/s", color: OC.amber, data: g.series.prompt_tps },
    ]));
    ch.add(el("sdCtx"), lineOption(g.labels,
      [{ name: "context used", color: OC.blue, data: g.series.context, area: true }]));
    if ((g.series.mtp_acc || []).filter((v) => v != null).length) {
      ch.add(el("sdMtp"), lineOption(g.labels,
        [{ name: "mtp acc %", color: OC.orange, data: g.series.mtp_acc }]));
    } else el("sdMtp").innerHTML = '<div class="empty">no MTP data in session</div>';
    const sdGpuSeries = gpuDualSeries(g.gpus || []);
    ch.add(el("sdHw"), dualAxisOption(g.labels, sdGpuSeries.length ? sdGpuSeries : [
      { name: "gpu 0 util %", color: OC.green, data: g.series.gpu_util, y: 0 },
      { name: "gpu 0 VRAM MB", color: OC.blue, data: g.series.vram_mb, y: 1 },
    ]));
    el("sdGpuCards").innerHTML = gpuSummaryCards(s.gpus || [], "session");

    cfgPanelHtml(d.config, "sdCfg", "sdFlags", el("sdCfgSub"));
  });
}

/* ------------------------------------------------------------------- compare */
function initCompare(meta) {
  const providers = (meta && meta.providers) || [];
  const st = { range: "7d", provider: "", selected: [], candidates: [] };
  const rowGroups = [
    ["Model file", [
      ["Family", (x) => x.family || "—"],
      ["Quant", (x) => x.quant || "—"],
      ["Provider", (x) => (x.providers || []).join(" · ") || "—"],
    ]],
    ["Tokens", [
      ["Total tokens", (x) => fmtTokens(x.tokens)],
      ["Prompt tokens", (x) => fmtTokens(x.prompt_tokens)],
      ["Generated tokens", (x) => fmtTokens(x.gen_tokens)],
    ]],
    ["Throughput", [
      ["Avg prompt t/s", (x) => x.prompt_tps, "max"],
      ["Peak prompt t/s", (x) => x.peak_prompt_tps, "max"],
      ["Avg gen t/s", (x) => x.avg_gen_tps, "max"],
      ["Peak gen t/s", (x) => x.peak_gen_tps, "max"],
    ]],
    ["Runtime", [
      ["Inference time", (x) => fmtDur(x.inference_s)],
      ["Loaded / idle", (x) => fmtDur(x.loaded_s) + " / " + fmtDur(x.idle_s)],
      ["Utilization", (x) => x.utilization != null ? x.utilization + "%" : "—", "max"],
      ["Context max", (x) => x.context_max ? fmtNum(x.context_max) : "—"],
    ]],
    ["MTP", [
      ["MTP acceptance", (x) => x.mtp_acc != null ? x.mtp_acc + "%" : "—", "max"],
      ["MTP drafts", (x) => x.mtp_proposed ? fmtTokens(x.mtp_accepted) + " accepted / " + fmtTokens(x.mtp_rejected) + " rejected" : "—"],
    ]],
    ["Configuration", [
      ["Configuration", (x) => x.configuration || "—"],
      ["KV cache", (x) => x.kv_cache || "—"],
      ["Split / reasoning", (x) => (x.split_mode || "—") + " / " + (x.reasoning_effort || "—")],
      ["Per-GPU averages", (x) => (x.gpus || []).map((g) => "GPU " + g.index + ": " +
        (g.summary.util != null ? g.summary.util + "%" : "—") + " / " +
        (g.summary.vram_mb != null ? fmtBytes(g.summary.vram_mb * 1024 * 1024) : "—")).join(" · ") || "—"],
      ["Build", (x) => x.build || "—"],
    ]],
  ];

  const query = (path, includeKeys) => {
    const q = new URLSearchParams({ range: st.range });
    if (st.provider) q.set("provider", st.provider);
    if (includeKeys) q.set("keys", st.selected.join("|"));
    return path + "?" + q.toString();
  };
  /* Bounded page-session candidate cache keyed by (provider, range).  It is
     the last-good store per context: reused for the lifetime of the page
     session, LRU-bounded, and dropped when a refresh for that key fails.
     Live state is an overlay and never part of this identity. */
  const CAND_CACHE_MAX = 32;
  const candCache = new Map();
  const candKey = () => (st.provider || "all") + "|" + st.range;
  const candCacheGet = (key) => {
    const hit = candCache.get(key);
    if (hit) { candCache.delete(key); candCache.set(key, hit); }
    return hit || null;
  };
  const candCacheSet = (key, value) => {
    if (candCache.has(key)) candCache.delete(key);
    candCache.set(key, value);
    while (candCache.size > CAND_CACHE_MAX) candCache.delete(candCache.keys().next().value);
  };

  /* Independent request identities for the candidate, comparison, and GPU
     surfaces.  Cancelling one surface must not corrupt another, and an
     intentional abort is never treated as a failure. */
  let candCtl = null, candSeq = 0;
  let cmpCtl = null, cmpSeq = 0;
  let gpuCtl = null, gpuSeq = 0;
  const abortCand = () => { if (candCtl) { try { candCtl.abort(); } catch (e) {} candCtl = null; } };
  const abortCmp = () => { if (cmpCtl) { try { cmpCtl.abort(); } catch (e) {} cmpCtl = null; } };
  const abortGpu = () => { if (gpuCtl) { try { gpuCtl.abort(); } catch (e) {} gpuCtl = null; } };
  const isAbort = (error) => !!(error && error.name === "AbortError");
  const fetchJson = (path, signal) =>
    fetch(path, { signal }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });

  /* Context identity: the provider/range a rendered surface belongs to.
     Content from a previous context is never displayed as the current one. */
  const ctxNow = () => (st.provider || "all") + "|" + st.range;
  let cmpCtx = null;   // context of the current table content
  let cmpKeys = null;  // selection keys the current table was requested with
  let lastCmp = null;  // { ctx, models } last successful comparison payload
  const sameKeys = (a, b) => a.length === b.length && a.every((k, i) => k === b[i]);

  const setNotice = (id, message, onRetry) => {
    const box = el(id);
    if (!box) return;
    box.innerHTML = message
      ? '<span class="cmp-notice-text">' + esc(message) + '</span>' +
        '<button class="btn small" type="button">Retry</button>'
      : "";
    if (message) box.querySelector("button").onclick = onRetry;
    box.hidden = !message;
  };
  const hideNotice = (id) => setNotice(id, null, null);
  const setCols = (n) => {
    const panel = el("cmpTablePanel");
    if (panel) panel.className = "compare-result" + (n >= 2 ? " cmp-cols-" + n : "");
  };
  const showCmpLoading = () => {
    el("cmpDeltaPanel").hidden = true;
    el("cmpTable").innerHTML = '<div class="compare-loading"><span class="spin"></span>loading comparison…</div>';
  };
  const showCmpIdle = () => {
    el("cmpDeltaPanel").hidden = true;
    el("cmpTable").innerHTML = '<div class="empty">select at least two model files to compare</div>';
  };
  const showCmpError = (message) => {
    el("cmpDeltaPanel").hidden = true;
    el("cmpTable").innerHTML = '<div class="compare-error">' + esc(message) +
      '<button class="btn small" type="button">Retry</button></div>';
    el("cmpTable").querySelector("button").onclick = () => loadCompare();
  };

  const renderPick = () => {
    el("cmpCount").textContent = st.selected.length + " / 5 selected";
    el("cmpPick").innerHTML = st.candidates.length ? st.candidates.map((x, i) =>
      '<label class="cmp-row"><input type="checkbox" data-idx="' + i + '"' +
      (st.selected.includes(x.key) ? " checked" : "") + ">" +
      '<span class="m-dot" style="background:' + esc(x.color || "#74736e") + '"></span>' +
      '<span class="cmp-pick-name">' + esc(x.label) + '<small class="cmp-pick-meta">' +
      esc([x.quant, x.provider].filter(Boolean).join(" · ") || x.family || "model file") + '</small></span>' +
      '<span class="cmp-pick-stat">' + (x.gen_tps != null ? x.gen_tps + " t/s" : fmtTokens(x.tokens)) +
      '<small>' + fmtTokens(x.tokens) + " · " + (x.share || 0) + "%</small></span></label>").join("") :
      '<div class="empty">no model-file activity in this range</div>';
    el("cmpPick").querySelectorAll("input").forEach((input) => {
      input.onchange = () => {
        const key = st.candidates[Number(input.dataset.idx)].key;
        if (input.checked) {
          if (st.selected.length >= 5) { input.checked = false; return; }
          st.selected.push(key);
        } else st.selected = st.selected.filter((value) => value !== key);
        renderPick(); scheduleCompare();
      };
    });
  };
  /* 100-150 ms debounce for selection changes only.  Provider and range
     changes start their new context immediately (see loadContext). */
  const SELECTION_DEBOUNCE_MS = 120;
  let cmpDebounce = null;
  const scheduleCompare = () => {
    if (cmpDebounce) clearTimeout(cmpDebounce);
    cmpDebounce = setTimeout(() => { cmpDebounce = null; loadCompare(); }, SELECTION_DEBOUNCE_MS);
  };
  const cancelScheduledCompare = () => {
    if (cmpDebounce) { clearTimeout(cmpDebounce); cmpDebounce = null; }
  };
  const renderDeltas = (models) => {
    const panel = el("cmpDeltaPanel"), box = el("cmpDeltas");
    if (!panel || !box || models.length < 2) { if (panel) panel.hidden = true; return; }
    const metrics = [
      ["Generated tokens", "gen_tokens", (value) => fmtTokens(value)],
      ["Avg gen t/s", "avg_gen_tps", (value) => value == null ? "—" : value + " t/s"],
      ["Inference time", "inference_s", (value) => fmtDur(value)],
      ["Utilization", "utilization", (value) => value == null ? "—" : value + "%"],
    ];
    let html = '<div class="table-scroll"><table class="data-table"><tr><th>Change</th>' +
      metrics.map(([label]) => "<th>" + esc(label) + "</th>").join("") + "</tr>";
    for (let index = 1; index < models.length; index += 1) {
      const before = models[index - 1], after = models[index];
      html += "<tr><td>" + esc(before.model) + " → " + esc(after.model) + "</td>";
      metrics.forEach(([, key, format]) => {
        const a = Number(before[key]), b = Number(after[key]);
        const pct = Number.isFinite(a) && Number.isFinite(b) && a !== 0 ? (b - a) / Math.abs(a) * 100 : null;
        const cls = pct == null || pct === 0 ? "" : pct > 0 ? " cmp-delta-up" : " cmp-delta-down";
        html += '<td class="num' + cls + '">' + esc(format(after[key])) +
          (pct == null ? "" : " (" + (pct > 0 ? "+" : "") + pct.toFixed(1) + "%)") + "</td>";
      });
      html += "</tr>";
    }
    box.innerHTML = html + "</table></div>";
    panel.hidden = false;
  };
  const renderCompareTable = (models) => {
    let html = '<table class="data-table cmp-data-table"><colgroup><col class="cmp-label-col">' +
      models.map(() => '<col class="cmp-model-col">').join("") + '</colgroup><thead><tr><th class="cmp-label-head"></th>' +
      models.map((x, i) => '<th class="cmp-model-head"><div class="cmp-model-top"><span class="cmp-model-index">0' + (i + 1) +
      '</span><span class="m-dot" style="background:' + esc(x.color || "#74736e") + '"></span>' +
      '<span class="cmp-model-name">' + esc(x.model) + '</span></div><div class="cmp-model-meta">' +
      esc([x.quant, (x.providers || []).join(" · ")].filter(Boolean).join(" · ") || "observed model file") +
      "</div></th>").join("") + "</tr></thead><tbody>";
    rowGroups.forEach(([group, rows]) => {
      html += '<tr class="cmp-section-row"><td colspan="' + (models.length + 1) + '">' + esc(group) + "</td></tr>";
      rows.forEach(([label, get, mode]) => {
        const values = models.map(get); let best = -1, max = null;
        if (mode === "max") values.forEach((value, i) => {
          const n = parseFloat(value); if (Number.isFinite(n) && (max == null || n > max)) { max = n; best = i; }
        });
        const gpuRow = label === "Per-GPU averages";
        html += '<tr><td class="muted">' + label + "</td>" + values.map((value, i) =>
          gpuRow ? '<td class="num muted" id="cmpGpu-' + i + '">loading…</td>' :
            '<td class="num' + (i === best ? " diff-hl" : "") + '">' + esc(value == null ? "—" : value) + "</td>").join("") + "</tr>";
      });
    });
    el("cmpTable").innerHTML = html + "</tbody></table>";
  };
  const applyGpu = (models, gpuData) => {
    models.forEach((model, index) => {
      const cell = el("cmpGpu-" + index);
      if (!cell) return;
      const values = (gpuData.gpus || {})[model.key] || [];
      cell.textContent = values.map((gpu) => "GPU " + gpu.index + ": " +
        (gpu.summary.util != null ? gpu.summary.util + "%" : "—") + " / " +
        (gpu.summary.vram_mb != null ? fmtBytes(gpu.summary.vram_mb * 1024 * 1024) : "—")).join(" · ") || "—";
      cell.classList.remove("muted");
    });
  };
  /* GPU summaries stay lazy: they start only after the comparison has
     rendered, on their own identity, and never block the initial table. */
  const loadGpu = (models, ctx, cmpSeqRef) => {
    abortGpu();
    const seq = ++gpuSeq;
    const ctl = new AbortController(); gpuCtl = ctl;
    fetchJson(query("/api/compare/models/gpus", true), ctl.signal).then((gpuData) => {
      if (seq !== gpuSeq || cmpSeqRef !== cmpSeq || ctxNow() !== ctx) return;
      applyGpu(models, gpuData);
    }).catch((error) => {
      if (seq !== gpuSeq || cmpSeqRef !== cmpSeq || ctxNow() !== ctx) return;
      if (isAbort(error)) return;
      console.error("compare gpus", error);
      models.forEach((_, index) => {
        const cell = el("cmpGpu-" + index);
        if (cell) {
          cell.classList.remove("muted");
          cell.innerHTML = "unavailable" +
            '<span class="cmp-gpu-retry" data-gpu-retry role="button">Retry</span>';
        }
      });
    });
  };
  const loadCompare = () => {
    const ctx = ctxNow();
    if (st.selected.length < 2) {
      abortCmp(); abortGpu();
      hideNotice("cmpNotice");
      cmpCtx = ctx; cmpKeys = null;
      setCols(0);
      showCmpIdle();
      return;
    }
    const keys = st.selected.slice();
    /* Deduplicate only while the identical (context, selection) request is
       still live: cmpCtl is set when it starts and nulled only by
       abortCmp(), and a failed load clears cmpKeys, so a dead or failed
       request is never mistaken for coverage.  The check must run before the
       abort, or a restart would kill the one in-flight request delivering
       this state and then skip re-issuing it, leaving the surface dead. */
    if (ctx === cmpCtx && cmpKeys && sameKeys(cmpKeys, keys) && cmpCtl) return;
    /* Compare restarts abort the comparison and its lazy GPU decoration,
       never the candidate surface. */
    abortCmp(); abortGpu();
    hideNotice("cmpNotice");
    const contextChanged = ctx !== cmpCtx;
    cmpCtx = ctx; cmpKeys = keys;
    if (contextChanged) {
      /* Previous-context content is incompatible: never display it as the
         new context, and never let a failed load fall back to it. */
      setCols(0);
      showCmpLoading();
    }
    const seq = ++cmpSeq;
    const ctl = new AbortController(); cmpCtl = ctl;
    fetchJson(query("/api/compare/models", true), ctl.signal).then((data) => {
      if (seq !== cmpSeq || ctxNow() !== ctx) return;
      const models = (data && data.models) || [];
      if (models.length < 2) {
        lastCmp = null; cmpKeys = null;
        setCols(0);
        showCmpError("The selected model files could not be compared in this range.");
        return;
      }
      lastCmp = { ctx: ctx, models: models };
      setCols(models.length);
      renderCompareTable(models);
      renderDeltas(models);
      loadGpu(models, ctx, seq);
    }).catch((error) => {
      if (seq !== cmpSeq || ctxNow() !== ctx) return;
      if (isAbort(error)) return;
      console.error("compare", error);
      cmpKeys = null;
      if (lastCmp && lastCmp.ctx === ctx) {
        /* Same-context last-good content stays visible; only the failed
           refresh is recoverable. */
        setNotice("cmpNotice", "Comparison refresh failed — showing last available results.", () => loadCompare());
      } else {
        setCols(0);
        showCmpError("Comparison failed to load. Please try again.");
      }
    });
  };
  const reconcileCompare = () => {
    /* After the current context's candidate list is known, align the
       comparison: drop selections absent from this context and restart the
       comparison only when the effective keys actually changed. */
    const ctx = ctxNow();
    if (st.selected.length < 2) {
      if (cmpCtx === ctx) { abortCmp(); abortGpu(); cmpKeys = null; setCols(0); showCmpIdle(); }
      return;
    }
    if (cmpCtx === ctx && cmpKeys && sameKeys(cmpKeys, st.selected)) return;
    loadCompare();
  };
  const onCandidates = (models) => {
    st.candidates = models;
    const available = new Set(models.map((x) => x.key));
    st.selected = st.selected.filter((key) => available.has(key));
    hideNotice("cmpCandNotice");
    renderPick();
    reconcileCompare();
  };
  const loadCandidates = (opts) => {
    /* Candidate restart aborts only the candidate surface.  A page-session
       cache hit reuses the last-good list for this (provider, range) without
       network.  A failure never erases the selection. */
    const force = !!(opts && opts.force);
    const key = candKey();
    abortCand();
    if (!force) {
      const hit = candCacheGet(key);
      if (hit) { onCandidates(hit.models); return Promise.resolve(); }
    }
    const seq = ++candSeq;
    const ctl = new AbortController(); candCtl = ctl;
    el("cmpPick").innerHTML = '<div class="compare-loading"><span class="spin"></span>loading models…</div>';
    el("cmpCount").textContent = "…";
    return fetchJson(query("/api/compare/models/candidates", false), ctl.signal).then((data) => {
      if (seq !== candSeq) return;
      const models = (data && data.models) || [];
      candCacheSet(key, { models: models, fetchedAt: Date.now() });
      onCandidates(models);
    }).catch((error) => {
      if (seq !== candSeq) return;
      if (isAbort(error)) return;
      console.error("compare candidates", error);
      const fallback = candCacheGet(key);
      if (fallback) {
        onCandidates(fallback.models);
        setNotice("cmpCandNotice", "Candidate refresh failed — showing last available list.",
          () => loadCandidates({ force: true }));
      } else {
        candCache.delete(key);
        el("cmpPick").innerHTML = '<div class="compare-error">Models failed to load.' +
          '<button class="btn small" type="button">Retry</button></div>';
        el("cmpPick").querySelector("button").onclick = () => loadCandidates({ force: true });
      }
    });
  };
  const gpuRetryTarget = el("cmpTablePanel");
  gpuRetryTarget.addEventListener("click", (event) => {
    if (event.target.closest("[data-gpu-retry]") && lastCmp) loadGpu(lastCmp.models, lastCmp.ctx, cmpSeq);
  });
  const loadContext = () => {
    /* Provider/range change: start the new context immediately — restart
       candidates, comparison, and GPU (no selection debounce). */
    cancelScheduledCompare();
    hideNotice("cmpNotice");
    hideNotice("cmpCandNotice");
    loadCandidates();
    loadCompare();
  };
  fillSelect(el("cmpProvider"), "All providers", providers.map((p) => [p.id, p.name]), st.provider);
  el("cmpProvider").onchange = (event) => { st.provider = event.target.value; loadContext(); };
  segControl("cmpRange", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[st.range], (label) => {
    st.range = RANGES.find((r) => RANGE_LABELS[r] === label) || "7d"; loadContext();
  });
  loadCandidates();
  window.__g07 = {
    state: () => ({
      range: st.range, provider: st.provider || null,
      selected: st.selected.slice(),
      candidateCount: st.candidates.length,
      candCacheSize: candCache.size,
      lastCmpCtx: lastCmp ? lastCmp.ctx : null,
      lastCmpModels: lastCmp ? lastCmp.models.length : 0,
    }),
  };
  return Promise.resolve();
}

/* ------------------------------------------------------------------ hardware */
function hwKv(h) {
  if (!h) return '<div class="empty">no agent data — run the passive agent on the host</div>';
  return kvTable([
    ["hostname", esc(h.hostname) || "—"],
    ["os", esc(h.os) || "—"],
    ["kernel", esc(h.kernel) || "—"],
    ["cpu", (esc(h.cpu) || "—") + (h.cpu_threads ? " · " + h.cpu_threads + " threads" : "")],
    ["ram", h.ram_mb ? fmtBytes(h.ram_mb * 1024 * 1024) : "—"],
    ["gpus", (h.gpus || []).length + " detected"],
    ["driver", (esc(h.nvidia_driver) || "—") + (h.cuda ? " · CUDA " + esc(h.cuda) : "")],
    ["pcie", esc(h.pcie) || "—"],
  ]);
}

function buildKv(b) {
  if (!b) return '<div class="empty">not observed yet</div>';
  return kvTable([
    ["version", esc(b.version) || "—"],
    ["commit", esc(b.commit) || "—"],
    ["docker image", esc(b.docker_image) || "—"],
    ["container", b.container_id ? String(b.container_id).slice(0, 12) : "—"],
    ["source", esc(b.source) || "—"],
    ["updated", b.updated || "—"],
  ]);
}

function initHardware(meta) {
  const ch = makeCharts();
  const GPU_CHARTS = [
    ["gpu", "GPU utilization %", "gpu_util", OC.green],
    ["vram", "VRAM used (MB)", "vram_mb", OC.blue],
    ["temp", "GPU temperature °C", "gpu_temp", OC.amber],
    ["power", "GPU power (W)", "gpu_power", OC.orange],
  ];
  const HOST_CHARTS = [
    ["cpu", "CPU usage %", "cpu_pct", OC.green],
    ["ram", "RAM used (MB)", "ram_mb", OC.blue],
  ];
  return api("/api/hardware").then((d) => {
    const root = el("hwRoot");
    if (!d.providers.length) { root.innerHTML = '<div class="empty">no providers</div>'; return; }
    root.innerHTML = d.providers.map((p, i) => {
      const graphGpus = (p.graphs && p.graphs.gpus) || [];
      const inventory = ((p.hardware && p.hardware.gpus) || []).map((gpu, fallback) => ({
        label: "GPU " + (gpu.index != null ? gpu.index : fallback) + " · " + (gpu.name || "unknown"),
        color: gpu.color || OC.blue, pcie: gpu.pcie,
        index: gpu.index != null ? gpu.index : fallback,
        vram_total_mb: gpu.vram_mb,
        current: { util: gpu.util, vram_mb: gpu.vram_used_mb,
          temp_c: gpu.temp_c, power_w: gpu.power_w },
      }));
      let charts = "";
      if (p.graphs && p.graphs.series) {
        charts = '<div class="ov-charts-2" style="margin-top:12px;margin-bottom:0">' +
          GPU_CHARTS.concat(HOST_CHARTS).map(([key, label]) =>
          '<div class="panel" style="padding:10px 12px"><div class="panel-head" style="margin-bottom:6px"><b>' +
          label + "</b></div><div class=\"chart-box short\" id=\"hw" + i + key + "\"></div></div>").join("") + "</div>";
      }
      const details = graphGpus.map((gpu, gi) =>
        '<details class="gpu-detail"><summary><span class="m-dot" style="background:' + esc(gpu.color) + '"></span> ' +
        esc(gpu.label) + ' · per-GPU detail</summary><div class="gpu-detail-grid">' +
        GPU_CHARTS.map(([key, label]) => '<div class="panel"><div class="panel-head"><b>' + label +
          '</b></div><div class="chart-box" id="hw' + i + 'g' + gi + key + '"></div></div>').join("") +
        '</div></details>').join("");
      return '<div class="panel" style="margin-bottom:12px">' +
        '<div class="panel-head"><b>' + esc(p.provider) + " &nbsp;" + pillHtml(p.status) + "</b>" +
        '<span class="muted">' + (p.hardware && p.hardware.updated ? "hardware updated " + p.hardware.updated :
          "no host agent data") + "</span></div>" +
        ((p.hardware || p.build)
          ? '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">' +
            '<div><div class="panel-head"><b>Host</b><span class="muted">' + esc((p.hardware && p.hardware.source) || "") + "</span></div>" +
            hwKv(p.hardware) + gpuSummaryCards(inventory, "live") + "</div>" +
            '<div><div class="panel-head"><b>llama.cpp build</b></div>' + buildKv(p.build) + "</div>" +
            "</div>"
          : '<div class="empty">no hardware or build data observed yet</div>') +
        charts + details + "</div>";
    }).join("");
    d.providers.forEach((p, i) => {
      if (!p.graphs || !p.graphs.series) return;
      const g = p.graphs;
      const graphGpus = g.gpus || [];
      GPU_CHARTS.forEach(([key, label, sk]) => {
        const box = el("hw" + i + key);
        if (!box) return;
        const metricKey = sk === "gpu_util" ? "util" : sk === "gpu_temp" ? "temp_c" :
          sk === "gpu_power" ? "power_w" : "vram_mb";
        const lines = graphGpus.map((gpu) => ({ name: gpu.label, color: gpu.color,
          data: gpu.series[metricKey] }));
        if (lines.some((line) => line.data.some((v) => v != null))) {
          ch.add(box, lineOption(graphGpus[0].labels, lines));
        } else {
          box.innerHTML = '<div class="empty">no data in last hour</div>';
        }
      });
      HOST_CHARTS.forEach(([key, label, sk, color]) => {
        const box = el("hw" + i + key), vals = g.series[sk] || [];
        if (!box) return;
        if (vals.some((v) => v != null)) ch.add(box, lineOption(g.labels,
          [{ name: label, color: color, data: vals, area: true }]));
        else box.innerHTML = '<div class="empty">no data in last hour</div>';
      });
      graphGpus.forEach((gpu, gi) => {
        GPU_CHARTS.forEach(([key, label, sk, color]) => {
          const box = el("hw" + i + "g" + gi + key);
          if (!box) return;
          const metricKey = sk === "gpu_util" ? "util" : sk === "gpu_temp" ? "temp_c" :
            sk === "gpu_power" ? "power_w" : "vram_mb";
          const vals = gpu.series[metricKey] || [];
          if (vals.some((v) => v != null)) ch.add(box, lineOption(gpu.labels,
            [{ name: gpu.label, color: gpu.color || color, data: vals, area: true }]));
          else box.innerHTML = '<div class="empty">no data in last hour</div>';
        });
      });
    });
    root.querySelectorAll("details.gpu-detail").forEach((detail) => {
      detail.addEventListener("toggle", () => {
        if (detail.open) ChartRegistry.resizeAll();
      });
    });
  });
}

/* ------------------------------------------------------------------ settings */
function fieldBox(label, type, id, val, extra) {
  return '<div class="field"><label>' + esc(label) + '</label><input type="' + type + '" id="' + id +
    '" value="' + esc(val == null ? "" : val) + '"' + (extra || "") + "></div>";
}

function initSettings(meta) {
  const loadAll = () => Promise.all([
    api("/api/status"),
    api("/api/settings/providers"),
    api("/api/settings/display"),
    api("/api/settings/currency"),
    api("/api/settings/pricing-sync"),
  ]);

  const renderSystem = (st) => {
    const tb = el("sysBody");
    tb.innerHTML = st.providers.map((p) => {
      const t = p.telemetry || {};
      const groups = ["counters", "speeds", "context", "mtp", "gpu"].map((g) =>
        '<span class="badge" style="margin-right:4px">' + g + (t[g] ? " ✓" : " ✗") + "</span>").join("");
      const slots = p.endpoints && p.endpoints.slots;
      const endpointBadge = '<span class="badge" style="margin-right:4px">slots ' +
        (slots === true ? "✓" : slots === false ? "✗" : "?") + "</span>";
      return "<tr>" +
        "<td>" + pillHtml(p.status) + " <b>" + esc(p.name) + "</b></td>" +
        '<td class="muted">' + esc(p.url) + "</td>" +
        '<td class="num">' + (p.latency_ms != null ? p.latency_ms + " ms" : "—") + "</td>" +
        "<td>" + esc(p.last_success_ago) + "</td>" +
        '<td class="muted">' + (p.agent_status || "—") + "</td>" +
        "<td>" + endpointBadge + groups + "</td>" +
        '<td class="muted">' + esc(p.build || "—") + "</td>" +
        "</tr>";
    }).join("");
    el("sysMeta").textContent = "collector " + ((st.collector && st.collector.role) || "unknown") +
      " · db " + fmtBytes(st.db_size_bytes) + " · " + st.db_path + " · uptime " + fmtDur(st.uptime_s);
    const errP = st.providers.find((p) => p.last_error);
    el("sysErr").textContent = errP ? "last error (" + errP.name + "): " + errP.last_error : "";
  };

  const renderProviders = (provs) => {
    const box = el("provList");
    box.innerHTML = provs.map((p) =>
      '<div class="panel" style="margin-bottom:10px">' +
      '<div class="panel-head"><b>' + esc(p.name) + " &nbsp;" + pillHtml(p.status) +
      (p.is_default ? ' <span class="badge">DEFAULT</span>' : "") + "</b>" +
      '<span class="muted">' + (p.latency_ms != null ? p.latency_ms + " ms · " : "") +
      esc(p.last_success_ago || "") + "</span></div>" +
      '<div class="form-grid">' +
      fieldBox("Name", "text", "pf_name_" + p.id, p.name) +
      fieldBox("Type", "text", "pf_type_" + p.id, p.ptype) +
      fieldBox("Base URL (llama.cpp)", "text", "pf_url_" + p.id, p.base_url) +
      fieldBox("Agent URL (optional)", "text", "pf_agent_" + p.id, p.agent_url) +
      fieldBox("Poll interval (s)", "number", "pf_poll_" + p.id, p.poll_interval_s, ' step="0.25" min="0.25"') +
      fieldBox("Notes", "text", "pf_notes_" + p.id, p.notes) +
      "</div>" +
      '<div style="display:flex;gap:16px;align-items:center;margin:2px 0 8px">' +
      '<label style="font-size:11.5px;display:flex;gap:6px;align-items:center"><input type="checkbox" id="pf_en_' + p.id + '"' +
      (p.enabled ? " checked" : "") + "> enabled</label>" +
      '<label style="font-size:11.5px;display:flex;gap:6px;align-items:center"><input type="checkbox" id="pf_def_' + p.id + '"' +
      (p.is_default ? " checked" : "") + "> default</label>" +
      "</div>" +
      '<div style="display:flex;gap:8px;align-items:center" id="pfbtns_' + p.id + '">' +
      '<button class="btn small" data-act="test">Test</button>' +
      '<button class="btn small primary" data-act="save">Save</button>' +
      '<button class="btn small danger" data-act="del">Delete</button>' +
      '<span class="muted" style="font-size:11px" id="pfmsg_' + p.id + '"></span>' +
      "</div></div>").join("");

    provs.forEach((p) => {
      const card = el("pfbtns_" + p.id);
      if (!card) return;
      card.querySelectorAll("button").forEach((btn) => {
        btn.onclick = () => {
          const act = btn.dataset.act;
          const msg = el("pfmsg_" + p.id);
          const gv = (id) => (el(id) ? el(id).value : "");
          if (act === "test") {
            msg.textContent = "testing…";
            fetch("/api/settings/providers/" + p.id + "/test", { method: "POST" })
              .then((r) => r.json()).then((t) => {
                if (t.ok) {
                  const n = Object.values(t.endpoints).filter(Boolean).length;
                  msg.textContent = "OK · " + n + "/" + Object.keys(t.endpoints).length + " endpoints · model " + (t.model || "?") +
                    " · " + t.latency_ms + " ms";
                } else msg.textContent = "FAIL · " + (t.error || "no /health endpoint");
              }).catch((e) => { msg.textContent = "error: " + e; });
          } else if (act === "save") {
            msg.textContent = "saving…";
            fetch("/api/settings/providers/" + p.id, {
              method: "PUT", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                name: gv("pf_name_" + p.id), ptype: gv("pf_type_" + p.id),
                base_url: gv("pf_url_" + p.id), agent_url: gv("pf_agent_" + p.id),
                poll_interval_s: Number(gv("pf_poll_" + p.id)) || 1,
                notes: gv("pf_notes_" + p.id),
                enabled: el("pf_en_" + p.id).checked, is_default: el("pf_def_" + p.id).checked,
              }),
            }).then((r) => r.json()).then(() => {
              msg.textContent = "saved";
              loadAll().then(([a, b, c]) => { renderSystem(a); renderProviders(b.providers); renderDisplay(c.display); });
            }).catch((e) => { msg.textContent = "error: " + e; });
          } else if (act === "del") {
            if (!confirm("Delete provider " + p.name + "? Its telemetry rows are kept but orphaned.")) return;
            fetch("/api/settings/providers/" + p.id, { method: "DELETE" }).then((r) => r.json()).then(() => {
              loadAll().then(([a, b, c]) => { renderSystem(a); renderProviders(b.providers); renderDisplay(c.display); });
            });
          }
        };
      });
    });
  };

  const renderDisplay = (disp) => {
    const r = el("d_range"), g = el("d_group"), t = el("d_theme");
    if (r) r.value = disp.default_range || "7d";
    if (g) g.value = disp.default_group || "family";
    if (t) t.value = disp.theme || "dark";
  };

  const curStatusText = (b) => {
    if (!b || !b.code) return "Off — costs are shown in USD only.";
    if (!b.enabled || !b.rate) return b.code + " · no rate yet";
    const bits = [b.code + " · " + b.symbol + Number(b.rate) + " per US$1",
                  b.source || "?"];
    if (b.quote_date) bits.push("quoted " + b.quote_date);
    if (b.fetched_at) bits.push("fetched " + fmtDur(Date.now() / 1000 - b.fetched_at) + " ago");
    return bits.join(" · ");
  };

  const renderCurrency = (block, options) => {
    const sel = el("cur_code");
    if (sel && options) {
      sel.innerHTML = '<option value="">Off</option>' + options.map((o) =>
        '<option value="' + esc(o.code) + '">' + esc(o.code + " — " + o.name) +
        "</option>").join("");
    }
    if (sel) sel.value = block && block.code ? block.code : "";
    const st = el("cur_status");
    if (st) st.textContent = curStatusText(block);
  };

  const curApply = (res) => {
    renderCurrency(res.currency, null);
    if (res.error) {
      const st = el("cur_status");
      if (st) st.textContent += " · " + res.error;
    }
  };

  /* --------------------------------------------------------- automatic pricing */
  const apStatusText = (b) => {
    if (!b) return "";
    if (!b.enabled) return "Off — model prices are only what you set by hand.";
    const bits = ["daily at " + (b.run_time || "04:00") + " (server local)"];
    const lr = b.last_result;
    if (lr) {
      bits.push("last " + lr.result + " · +" + lr.updated + " updated / " +
                lr.unresolved + " unresolved" +
                (lr.failed ? " / " + lr.failed + " failed" : ""));
    }
    if (b.last_run_at) bits.push("ran " + fmtAgo(b.last_run_at * 1000, Date.now()));
    if (b.catalog_commit) bits.push("catalog " + String(b.catalog_commit).slice(0, 7));
    if (b.running) bits.push("running now");
    return bits.join(" · ");
  };

  const renderPricingRuns = (runs) => {
    const tb = el("ap_runs");
    if (!tb) return;
    tb.innerHTML = (runs || []).map((r) =>
      "<tr><td>" + esc(fmtDate(r.started_at)) + "</td>" +
      "<td>" + esc(r.trigger || "") + "</td>" +
      "<td>" + esc(r.result || "") + "</td>" +
      '<td class="num">' + (r.updated || 0) + "</td>" +
      '<td class="num">' + (r.skipped || 0) + "</td>" +
      '<td class="num">' + (r.unresolved || 0) + "</td>" +
      '<td class="num">' + (r.failed || 0) + "</td>" +
      '<td class="muted">' + esc(r.error_summary || "") + "</td></tr>").join("");
  };

  let apDefaultPrompt = "";
  let apPromptTouched = false;

  const apMpSyncLine = (b) => {
    const d = el("mpSyncLine");
    if (!d) return;
    if (!b || !b.enabled) { d.textContent = ""; return; }
    const lr = b.last_result || (b.recent_runs && b.recent_runs[0]);
    const bits = [];
    if (b.last_run_at) bits.push("last auto-price run " + fmtAgo(b.last_run_at * 1000, Date.now()));
    else bits.push("auto-pricing on, no run yet");
    if (lr) bits.push(lr.result + " · " + lr.updated + " updated / " +
                      lr.skipped + " unchanged / " + lr.unresolved + " unresolved" +
                      (lr.failed ? " / " + lr.failed + " failed" : ""));
    if (b.running) bits.push("running now");
    d.textContent = bits.join(" · ");
  };

  const apLoadModels = (providerId) => {
    const sel = el("ap_model");
    if (!sel) return;
    const want = sel.value;
    const q = providerId ? "?provider=" + encodeURIComponent(providerId) : "";
    // No provider id -> the server uses the default provider; still ask so the
    // list reflects that provider.
    fetch("/api/settings/pricing-sync/provider-models" + q)
      .then((r) => r.json()).then((d) => {
        const models = (d && d.models) || [];
        sel.innerHTML = '<option value="">First loaded model</option>' +
          models.map((m) => '<option value="' + esc(m) + '">' + esc(m) + "</option>").join("");
        sel.value = want;
        if (want && sel.value !== want) {
          // configured model not in the live list: keep it selectable
          sel.insertAdjacentHTML("beforeend",
            '<option value="' + esc(want) + '">' + esc(want) + " (not currently listed)</option>");
          sel.value = want;
        }
      }).catch(() => {});
  };

  const renderPricingSync = (b) => {
    if (!b) return;
    apDefaultPrompt = b.default_prompt || apDefaultPrompt;
    if (el("ap_enabled")) el("ap_enabled").value = b.enabled ? "1" : "0";
    if (el("ap_time") && document.activeElement !== el("ap_time")) {
      el("ap_time").value = b.run_time || "04:00";
    }
    if (el("ap_match")) el("ap_match").value = b.use_inference_match ? "1" : "0";
    const sel = el("ap_provider");
    if (sel) {
      sel.innerHTML = '<option value="">Default provider</option>' +
        (b.provider_options || []).map((p) =>
          '<option value="' + p.id + '">' + esc(p.name) +
          (p.is_default ? " (default)" : "") +
          (p.status === "LIVE" ? "" : " · offline") + "</option>").join("");
      sel.value = b.match_provider_id == null ? "" : String(b.match_provider_id);
    }
    const ms = el("ap_model");
    if (ms) {
      // seed the current value so apLoadModels can preserve it
      if (b.match_model && ms.value !== b.match_model) {
        ms.innerHTML = '<option value="">First loaded model</option>' +
          '<option value="' + esc(b.match_model) + '">' + esc(b.match_model) + "</option>";
        ms.value = b.match_model;
      }
      apLoadModels(b.match_provider_id || "");
    }
    const ta = el("ap_prompt");
    // Fill on the first render regardless of focus; afterwards only when the
    // user has not started editing.
    if (ta && (!apPromptTouched || document.activeElement !== ta)) {
      ta.value = b.prompt || b.default_prompt || "";
    }
    const st = el("ap_status");
    if (st) st.textContent = apStatusText(b);
    renderPricingRuns(b.recent_runs);
    apMpSyncLine(b);
  };

  /* ------------------------------------------------------------ model pricing */
  // Plain finite non-negative decimal notation; blank means null. Exponent,
  // negative, NaN and Infinity forms are rejected inline.
  const MP_DEC_RE = /^(?:\d+(?:\.\d+)?|\.\d+)$/;
  // Users may still type a currency marker even though the field shows US$.
  const MP_CUR_RE = /^\s*(?:US)?\$/i;
  const MP_HINT = "Price per 1M tokens (USD). Use a decimal point or comma, e.g. 0.5 or 0,5. Blank clears.";
  // [which, api field, input-id prefix, column label] -- one row of the pricing
  // table. Editing any of them flips the model to manual (server-side).
  const MP_FIELDS = [
    ["in", "input_price_per_million", "mpin_", "Input"],
    ["out", "output_price_per_million", "mpout_", "Output"],
    ["cw", "cache_write_price_per_million", "mpcw_", "Cache write"],
    ["cr", "cache_read_price_per_million", "mpcr_", "Cache read"],
  ];
  const MP_PREFIX = Object.fromEntries(MP_FIELDS.map(([w, , p]) => [w, p]));
  const mp = {
    providerId: null, seq: 0, abort: null, models: [],
    original: new Map(), rowErr: new Map(), saving: false, errInvalid: false,
  };

  function mpClean(raw) {
    let s = String(raw == null ? "" : raw).trim().replace(MP_CUR_RE, "").trim();
    // Accept a comma as the decimal separator (pt-BR): "0,10" -> "0.10".
    // A single comma with no dot is unambiguous. Both separators or several
    // commas is ambiguous, so it is left as-is and the decimal regex rejects it.
    if (s.indexOf(",") >= 0 && s.indexOf(".") < 0 && (s.match(/,/g) || []).length === 1) {
      s = s.replace(",", ".");
    }
    return s;
  }

  function mpNorm(s) {
    if (s == null) return "";
    s = String(s).trim();
    if (s === "") return "";
    const dot = s.indexOf(".");
    let i = dot < 0 ? s : s.slice(0, dot);
    let f = dot < 0 ? "" : s.slice(dot + 1);
    i = i.replace(/^0+(?=\d)/, "");
    f = f.replace(/0+$/, "");
    return (i === "" ? "0" : i) + (f !== "" ? "." + f : "");
  }

  function mpValid(raw) {
    const s = mpClean(raw);
    return s === "" || MP_DEC_RE.test(s);
  }

  function mpSetStatus(msg, cls) {
    const d = el("mpStatus");
    if (!d) return;
    d.textContent = msg || "";
    d.classList.toggle("err", cls === "err");
  }

  function mpStatusNeutral() {
    const off = mp.models.filter((m) => !m.catalog_available).length;
    return mp.models.length + " model file" + (mp.models.length === 1 ? "" : "s") +
      (off ? " · " + off + " offline/historical, still editable" : "");
  }

  function mpFieldState(id, which) {
    const inp = el(MP_PREFIX[which] + id);
    const raw = inp ? mpClean(inp.value) : "";
    const o = mp.original.get(id);
    const committed = o ? (o[which] || null) : null;
    return { raw, committed, changed: mpNorm(raw) !== mpNorm(committed) };
  }

  function mpRowChanged(id) {
    return MP_FIELDS.some(([w]) => mpFieldState(id, w).changed);
  }

  function mpRowInvalid(id) {
    return MP_FIELDS.some(([w]) => !mpValid(mpFieldState(id, w).raw));
  }

  function mpHasUnsaved() {
    if (mp.saving) return true;
    return mp.models.some((m) => mpRowChanged(m.id));
  }

  function mpRowMsg(id) {
    if (mp.rowErr.has(id)) return mp.rowErr.get(id);
    const bits = [];
    if (!mpValid(mpFieldState(id, "in").raw)) bits.push("input must be 0 or a non-negative number");
    if (!mpValid(mpFieldState(id, "out").raw)) bits.push("output must be 0 or a non-negative number");
    if (!mpValid(mpFieldState(id, "cw").raw)) bits.push("cache write must be 0 or a non-negative number");
    if (!mpValid(mpFieldState(id, "cr").raw)) bits.push("cache read must be 0 or a non-negative number");
    return bits.join(" · ");
  }

  function mpShowRowErr(id) {
    const d = el("mprerr_" + id);
    if (!d) return;
    const msg = mpRowMsg(id);
    d.textContent = msg || "";
    if (msg) d.setAttribute("aria-live", "polite");
  }

  function mpUpdateButtons() {
    let dirtyN = 0, invalidN = 0;
    mp.models.forEach((m) => {
      const changed = mpRowChanged(m.id);
      if (changed) dirtyN++;
      if (changed && mpRowInvalid(m.id)) invalidN++;
      const row = el("mpr_" + m.id);
      if (row) row.classList.toggle("mp-dirty", changed);
      MP_FIELDS.forEach(([w, , prefix]) => {
        const st = mpFieldState(m.id, w);
        const inp = el(prefix + m.id);
        if (!inp) return;
        // Lock the price fields while a bulk save is in flight. The success
        // handler re-renders every row from the just-saved originals, so an
        // edit typed after the click would otherwise be silently clobbered by
        // the older in-flight response.
        inp.disabled = mp.saving;
        const bad = !mpValid(st.raw);
        inp.classList.toggle("mp-invalid", bad);
        inp.setAttribute("aria-invalid", bad ? "true" : "false");
      });
      mpShowRowErr(m.id);
    });
    const btn = el("mpSave");
    if (btn) {
      btn.disabled = !dirtyN || invalidN > 0 || mp.saving;
      btn.textContent = mp.saving ? "Saving…" : "Bulk Save";
      btn.classList.toggle("is-saving", mp.saving);
      btn.title = mp.saving ? "Saving…" :
        (invalidN > 0 ? "Fix invalid price" : (!dirtyN ? "No changes to save" : ""));
    }
    if (mp.saving) {
      // "saving N rows…" is owned by mpBulkSave.
    } else if (invalidN > 0) {
      mp.errInvalid = true;
      mpSetStatus(invalidN + " row" + (invalidN > 1 ? "s" : "") +
        " with an invalid price — enter a non-negative decimal (0.5, 10.50; a comma like 0,5 also works) or blank to clear", "err");
    } else if (mp.errInvalid) {
      // Clear a stale invalid-price message once every row is valid again,
      // without clobbering a separate "save failed" notice.
      mp.errInvalid = false;
      mpSetStatus(mpStatusNeutral());
    }
    const sel = el("mp_provider");
    if (sel) sel.disabled = mp.saving;
    const dc = el("mpDirtyCount");
    if (dc) dc.textContent = dirtyN ? dirtyN + " unsaved row" + (dirtyN > 1 ? "s" : "") : "";
  }

  function mpLastUpdated(m) {
    if (m.pricing_mode === "manual") {
      return '<span class="muted" style="font-size:10.5px" title="set by hand">✎ manual</span>';
    }
    if (!m.pricing_synced_at) {
      return '<span class="muted" style="font-size:10.5px">—</span>';
    }
    const when = fmtDate(m.pricing_synced_at);
    const src = m.pricing_litellm_key
      ? " · " + esc(m.pricing_litellm_key) : "";
    if (m.pricing_stale) {
      return '<span style="font-size:10.5px;color:var(--amber)" title="' +
        esc(m.pricing_last_error || "last refresh failed") + '">⚠ ' + esc(when) +
        " (stale)</span>";
    }
    return '<span class="muted" style="font-size:10.5px" title="auto from LiteLLM' +
      esc(src) + '">↻ ' + esc(when) + "</span>";
  }

  function mpModeCell(m) {
    const mode = m.pricing_mode || "auto";
    const stale = m.pricing_stale
      ? ' <span class="badge off">stale</span>' : "";
    const err = m.pricing_last_error
      ? ' <span class="muted" style="font-size:10px">' + esc(m.pricing_last_error) + "</span>" : "";
    return '<button type="button" class="btn small mp-mode" data-mid="' + m.id +
      '" data-mode="' + mode + '" title="' +
      (mode === "auto"
        ? "Auto — updated by the daily sync. Click to lock as manual."
        : "Manual — the daily sync leaves this model alone. Click to hand it back to auto.") +
      '">' + mode.toUpperCase() + "</button>" + stale + err;
  }

  function mpRenderRows() {
    const tb = el("mpBody");
    if (!tb) return;
    const now = Date.now();
    tb.innerHTML = mp.models.map((m) => {
      const o = mp.original.get(m.id) || {};
      const avail = m.catalog_available
        ? '<span class="badge">available</span>'
        : '<span class="badge off">offline</span> <span class="muted" style="font-size:10.5px">last seen ' +
          esc(fmtAgo(m.catalog_last_seen_at, now)) + "</span>";
      const lastCell = "<td>" + mpLastUpdated(m) + "</td>";
      const inCell = '<td class="num"><span class="mp-cur">US$</span>' +
        '<input type="text" inputmode="decimal" autocomplete="off" class="mp-price" id="mpin_' + m.id +
        '" value="' + esc(o.in == null ? "" : o.in) +
        '" title="' + MP_HINT + '"' +
        '" aria-label="Input price per million USD for ' + esc(m.name) + " (" + esc(m.key) + ')"></td>';
      const outCell = '<td class="num"><span class="mp-cur">US$</span>' +
        '<input type="text" inputmode="decimal" autocomplete="off" class="mp-price" id="mpout_' + m.id +
        '" value="' + esc(o.out == null ? "" : o.out) +
        '" title="' + MP_HINT + '"' +
        '" aria-label="Output price per million USD for ' + esc(m.name) + " (" + esc(m.key) + ')"></td>';
      const cacheCells = [["cw", "Cache write"], ["cr", "Cache read"]].map(([w, label]) =>
        '<td class="num"><span class="mp-cur">US$</span>' +
        '<input type="text" inputmode="decimal" autocomplete="off" class="mp-price" id="' +
        MP_PREFIX[w] + m.id + '" value="' + esc(o[w] == null ? "" : o[w]) +
        '" title="' + MP_HINT + '"' +
        '" aria-label="' + label + " price per million USD for " + esc(m.name) +
        " (" + esc(m.key) + ')"></td>').join("");
      return '<tr class="mp-row" id="mpr_' + m.id + '">' +
        '<td><b>' + esc(m.name) + '</b> <span class="muted" style="font-size:10.5px">' + esc(m.key) + "</span>" +
        '<div class="mp-row-err" id="mprerr_' + m.id + '"></div></td>' +
        "<td>" + avail + "</td>" +
        "<td>" + mpModeCell(m) + "</td>" +
        lastCell +
        inCell + outCell + cacheCells +
        "</tr>";
    }).join("");
    mp.models.forEach((m) => {
      MP_FIELDS.forEach(([, , prefix]) => {
        const inp = el(prefix + m.id);
        if (inp) inp.addEventListener("input", () => mpUpdateButtons());
      });
      mpShowRowErr(m.id);
    });
    tb.querySelectorAll(".mp-mode").forEach((btn) => {
      btn.addEventListener("click", () => mpToggleMode(Number(btn.dataset.mid)));
    });
  }

  function mpToggleMode(mid) {
    const m = mp.models.find((x) => x.id === mid);
    if (!m) return;
    const next = (m.pricing_mode || "auto") === "auto" ? "manual" : "auto";
    fetch("/api/settings/model-pricing/mode", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: mid, mode: next }),
    }).then((r) => r.json()).then((res) => {
      if (res && res.ok) {
        m.pricing_mode = res.mode;
        if (res.mode === "auto") m.pricing_last_error = null;
        mpRenderRows();
        mpUpdateButtons();
      }
    }).catch(() => {});
  }

  function mpLoad(providerId) {
    if (mp.abort) mp.abort.abort();
    mp.abort = new AbortController();
    const signal = mp.abort.signal;
    const seq = ++mp.seq;
    mp.providerId = providerId;
    mp.rowErr = new Map();
    mpSetStatus("loading model files…");
    fetch("/api/settings/models?provider=" + providerId, { signal: signal }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then((d) => {
      if (signal.aborted || seq !== mp.seq || mp.providerId !== providerId) return;
      mp.models = d.models || [];
      mp.original = new Map(mp.models.map((m) => [m.id, Object.fromEntries(
        MP_FIELDS.map(([w, field]) => [w,
          m[field] == null ? null : String(m[field])]),
      )]));
      mpRenderRows();
      mpUpdateButtons();
      const off = mp.models.filter((m) => !m.catalog_available).length;
      mpSetStatus(mp.models.length + " model file" + (mp.models.length === 1 ? "" : "s") +
        (off ? " · " + off + " offline/historical, still editable" : ""));
    }).catch((e) => {
      if (signal.aborted || seq !== mp.seq || mp.providerId !== providerId) return;
      mpSetStatus("failed to load model files: " + e, "err");
    });
  }

  function mpBulkSave() {
    if (mp.saving || mp.providerId == null) return;
    const rows = [];
    mp.models.forEach((m) => {
      const states = MP_FIELDS.map(([w, field]) => [field, mpFieldState(m.id, w)]);
      if (!states.some(([, st]) => st.changed)) return;
      if (states.some(([, st]) => !mpValid(st.raw))) return;
      const row = { model_id: m.id };
      states.forEach(([field, st]) => {
        row[field] = st.changed ? st.raw : (st.committed || null);
      });
      rows.push(row);
    });
    if (!rows.length) return;
    mp.saving = true;
    mpUpdateButtons();
    mpSetStatus("saving " + rows.length + " row" + (rows.length > 1 ? "s" : "") + "…");
    let req;
    try {
      req = fetch("/api/settings/model-pricing", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: mp.providerId, models: rows }),
      });
    } catch (e) {
      mp.saving = false;
      mpSetStatus("save failed — " + e + " · all edits preserved, fix and retry", "err");
      mpUpdateButtons();
      return;
    }
    req.then((r) => r.json().catch(() => ({})).then((data) => ({ status: r.status, ok: r.ok, data })))
      .then((res) => {
        if (!res.ok) {
          mp.saving = false;
          const msg = (res.data && res.data.detail) || ("HTTP " + res.status);
          mpSetStatus("save failed — " + msg + " · all edits preserved, fix and retry", "err");
          // Mark individual rows only when the response actually identifies them.
          const errs = res.data && res.data.errors;
          if (Array.isArray(errs)) {
            errs.forEach((e) => {
              if (e && e.model_id != null && mp.models.some((m) => m.id === e.model_id)) {
                mp.rowErr.set(e.model_id, (e.error || msg) + (e.field ? " (" + e.field + ")" : ""));
              }
            });
            mp.models.forEach((m) => mpShowRowErr(m.id));
          }
          mpUpdateButtons();
          return;
        }
        (res.data.models || []).forEach((row) => {
          mp.original.set(row.model_id, Object.fromEntries(
            MP_FIELDS.map(([w, field]) => [w,
              row[field] == null ? null : String(row[field])])));
          const mm = mp.models.find((x) => x.id === row.model_id);
          if (mm && row.pricing_mode) mm.pricing_mode = row.pricing_mode;
        });
        rows.forEach((row) => mp.rowErr.delete(row.model_id));
        mp.saving = false;
        mpRenderRows();
        mpUpdateButtons();
        mpSetStatus("saved " + (res.data.updated != null ? res.data.updated : rows.length) +
          " row" + ((res.data.updated != null ? res.data.updated : rows.length) === 1 ? "" : "s") +
          " · normalized values applied");
      }).catch((e) => {
        mp.saving = false;
        mpSetStatus("save failed — " + e + " · all edits preserved, fix and retry", "err");
        mpUpdateButtons();
      });
  }

  function mpInit(provs) {
    const sel = el("mp_provider");
    if (!sel) return;
    sel.innerHTML = provs.map((p) =>
      '<option value="' + p.id + '">' + esc(p.name) + (p.status === "LIVE" ? "" : " (offline)") + "</option>").join("");
    if (!provs.length) {
      mpSetStatus("no providers configured — add one above to edit prices");
      return;
    }
    const first = provs.find((p) => p.is_default) || provs[0];
    sel.value = String(first.id);
    mpLoad(first.id);
  }

  el("mpSave").onclick = () => mpBulkSave();
  el("mp_provider").onchange = () => {
    const sel = el("mp_provider");
    const id = Number(sel.value);
    if (!id) return;
    if (id !== mp.providerId && mpHasUnsaved()) {
      if (!confirm("You have unsaved price changes. Switch provider and discard them?")) {
        sel.value = mp.providerId == null ? "" : String(mp.providerId);
        return;
      }
    }
    mpLoad(id);
  };
  window.addEventListener("beforeunload", (e) => {
    if (mpHasUnsaved()) { e.preventDefault(); e.returnValue = ""; }
  });

  el("btnAddProv").onclick = () => { el("addProvForm").hidden = !el("addProvForm").hidden; };
  el("btnDoAddProv").onclick = () => {
    const gv = (id) => el(id).value;
    fetch("/api/settings/providers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: gv("np_name"), ptype: gv("np_type") || "llama.cpp",
        base_url: gv("np_url"), agent_url: gv("np_agent"),
        poll_interval_s: Number(gv("np_poll")) || 1, notes: "",
        enabled: true, is_default: el("np_def").checked,
      }),
    }).then((r) => r.json()).then(() => {
      el("addProvForm").hidden = true;
      loadAll().then(([a, b, c]) => { renderSystem(a); renderProviders(b.providers); renderDisplay(c.display); });
    });
  };
  el("btnSaveDisp").onclick = () => {
    const r = el("d_range"), g = el("d_group"), t = el("d_theme");
    fetch("/api/settings/display", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_range: r.value, default_group: g.value, theme: t.value }),
    }).then((x) => x.json()).then(() => {
      localStorage.setItem("llm-telemetry-theme", t.value);
      location.reload();
    });
  };

  if (el("cur_code")) el("cur_code").onchange = () => {
    const code = el("cur_code").value || null;
    el("cur_status").textContent = "saving…";
    fetch("/api/settings/currency", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code }),
    }).then((r) => r.json()).then(curApply)
      .catch((e) => { el("cur_status").textContent = "error: " + e; });
  };
  if (el("cur_refresh")) el("cur_refresh").onclick = () => {
    el("cur_status").textContent = "refreshing…";
    fetch("/api/settings/currency/refresh", { method: "POST" })
      .then((r) => r.json()).then(curApply)
      .catch((e) => { el("cur_status").textContent = "error: " + e; });
  };

  if (el("ap_prompt")) el("ap_prompt").addEventListener("input", () => { apPromptTouched = true; });
  if (el("ap_provider")) el("ap_provider").addEventListener("change", () => {
    apLoadModels(el("ap_provider").value);
  });
  if (el("ap_restore_prompt")) el("ap_restore_prompt").onclick = () => {
    if (el("ap_prompt") && apDefaultPrompt) {
      el("ap_prompt").value = apDefaultPrompt;
      apPromptTouched = true;
      el("ap_prompt_err").textContent = "";
      el("ap_status").textContent = "default prompt restored — Save to keep it";
    }
  };
  if (el("ap_save")) el("ap_save").onclick = () => {
    const provVal = el("ap_provider").value;
    const modelVal = el("ap_model") ? el("ap_model").value : "";
    const body = {
      enabled: el("ap_enabled").value === "1",
      run_time: el("ap_time").value.trim(),
      use_inference_match: el("ap_match").value === "1",
      match_provider_id: provVal ? Number(provVal) : null,
      match_model: modelVal || null,
      prompt: el("ap_prompt").value,
    };
    el("ap_prompt_err").textContent = "";
    el("ap_status").textContent = "saving…";
    fetch("/api/settings/pricing-sync", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json().then((d) => ({ ok: r.ok, d }))).then(({ ok, d }) => {
      if (!ok) { el("ap_prompt_err").textContent = d.detail || "save failed"; return; }
      apPromptTouched = false;
      renderPricingSync(d);
    }).catch((e) => { el("ap_status").textContent = "error: " + e; });
  };
  let apPollTimer = null;
  const apStopPoll = () => { if (apPollTimer) { clearInterval(apPollTimer); apPollTimer = null; } };

  const apPollRun = (startedMs) => {
    apStopPoll();
    let ticks = 0;
    let sawRunning = false;
    const tick = () => {
      ticks++;
      fetch("/api/settings/pricing-sync").then((r) => r.json()).then((b) => {
        renderPricingSync(b);
        const secs = Math.round((Date.now() - startedMs) / 1000);
        const fresh = b.recent_runs && b.recent_runs[0]
          && b.recent_runs[0].started_at >= startedMs - 3000;
        if (b.running || (fresh && !b.recent_runs[0].finished_at)) {
          sawRunning = true;
          el("ap_status").textContent = "running… " + secs + "s (catalog fetch"
            + (b.use_inference_match ? " + inference matching" : "") + ")";
        } else if (!sawRunning && ticks < 4) {
          el("ap_status").textContent = "starting… " + secs + "s";
        } else {
          apStopPoll();
          el("ap_run").disabled = false;
          const lr = b.last_result || (b.recent_runs && b.recent_runs[0]);
          el("ap_status").textContent = lr
            ? "done in " + secs + "s — " + lr.result + " · " + lr.updated
              + " updated / " + lr.skipped + " unchanged / " + lr.unresolved
              + " unresolved" + (lr.failed ? " / " + lr.failed + " failed" : "")
            : "done in " + secs + "s";
          // refresh the Model Pricing table so Last updated reflects new prices
          if (typeof mp === "object" && mp.providerId != null) mpLoad(mp.providerId);
        }
        if (ticks > 150) { apStopPoll(); el("ap_run").disabled = false;
          el("ap_status").textContent = "still running — reload to check"; }
      }).catch(() => {});
    };
    apPollTimer = setInterval(tick, 2500);
    tick();
  };

  if (el("ap_run")) el("ap_run").onclick = () => {
    const btn = el("ap_run");
    btn.disabled = true;
    el("ap_status").textContent = "starting…";
    const started = Date.now();
    fetch("/api/settings/pricing-sync/run", { method: "POST" })
      .then((r) => r.json().then((d) => ({ status: r.status, d })))
      .then(({ status, d }) => {
        if (status === 409 || d.error) {
          el("ap_status").textContent = d.error || "already running";
          btn.disabled = false;
          renderPricingSync(d);
          return;
        }
        apPollRun(started);
      })
      .catch((e) => { el("ap_status").textContent = "error: " + e; btn.disabled = false; });
  };

  loadAll().then(([st, provs, disp, cur, psync]) => {
    renderSystem(st);
    renderProviders(provs.providers);
    renderDisplay(disp.display);
    renderCurrency(cur.currency, cur.options);
    renderPricingSync(psync);
    mpInit(provs.providers);
  });
  return Promise.resolve();
}

document.addEventListener("DOMContentLoaded", () => { initShell(); bootstrap(); });
