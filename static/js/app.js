/* Observatory - dashboard app logic (part A: core + overview + models) */
"use strict";

const RANGES = ["today", "2d", "3d", "5d", "7d", "30d", "all"];
const RANGE_LABELS = { today: "Today", "2d": "2d", "3d": "3d", "5d": "5d", "7d": "7d", "30d": "30d", all: "All" };
const GROUPS = ["family", "file", "quant"];
const GROUP_LABELS = { family: "Family", file: "Each file", quant: "Quant" };
const METRIC_KEYS = ["active", "inference", "loaded", "idle"];
const METRIC_LABELS = { active: "Active", inference: "Inference", loaded: "Loaded", idle: "Idle" };
const DETAIL_RANGES = ["1m", "5m", "15m", "1h", "session", "24h", "7d", "30d"];

window.META = null;
window.__onLive = null;

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
  return {
    list: list,
    add(box, opt) { const c = registerChart(box, opt); if (c) list.push(c); return c; },
    spark(box, data, color) { const c = sparkline(box, data, color); if (c) list.push(c); return c; },
    clear() { list.forEach((c) => { try { c.dispose(); } catch (e) {} }); list.length = 0; },
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

function selMetric(l, v, s) {
  return '<div class="sel-metric"><div class="l">' + esc(l) + '</div><div class="v">' + esc(v) +
    '</div><div class="s">' + esc(s || "") + "</div></div>";
}

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
    name: s.name, type: "line", data: s.data, yAxisIndex: s.y || 0,
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
function topbarPill(providers) {
  if (!providers || !providers.length) return;
  const def = providers.find((p) => p.is_default) || providers[0];
  const known = window.META && window.META.providers.find((p) => p.id === def.id);
  const name = (known && known.name) || def.name || "provider";
  const pill = el("provPill"), txt = el("provPillText");
  if (pill && txt) {
    pill.className = pillCls(def.status);
    txt.textContent = name + " · " + (def.status || "OFFLINE");
  }
  const dot = el("sideDot"), sp = el("sideProv");
  if (dot && sp) {
    dot.style.background = def.status === "LIVE" ? "var(--green)" :
      def.status === "STALE" ? "var(--amber)" : "var(--red)";
    sp.textContent = name + " · " + (def.status || "OFFLINE");
  }
}

function bootstrap() {
  const page = document.body.dataset.page || "";
  api("/api/meta").then((meta) => {
    window.META = meta;
    topbarPill(meta.providers);
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
      topbarPill(d.providers);
      if (window.__onLive) window.__onLive(d);
    };
    es.onerror = () => {};
  } catch (e) {}

  window.addEventListener("resize", () => {
    (window.__charts || []).forEach((c) => { try { c.resize(); } catch (e) {} });
  });
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
  const cxE = el("ovCtxVal"), bar = el("ovCtxBar"), cxS = el("ovCtxSub");
  if (c && c.context_used) {
    cxE.textContent = fmtNum(c.context_used);
    if (bar) bar.firstElementChild.style.width = (c.context_pct != null ? c.context_pct : 0) + "%";
    cxS.textContent = "of " + fmtNum(c.context_max) + (c.mtp_acc != null ? " · MTP " + c.mtp_acc + "%" : "");
  } else {
    cxE.textContent = "—";
    if (bar) bar.firstElementChild.style.width = "0%";
    cxS.textContent = "—";
  }
}

function renderOvRecent(rows) {
  const tb = el("ovRecentBody");
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="6"><div class="empty">no sessions yet</div></td></tr>';
    return;
  }
  tb.innerHTML = rows.map((x) =>
    '<tr class="clickable" data-id="' + x.id + '">' +
    "<td>" + fmtDate(x.start) + "</td>" +
    '<td><span class="m-dot" style="background:' + esc(x.color || "#74736e") + '"></span> ' + esc(x.model || "—") + "</td>" +
    '<td class="num">' + fmtDur(x.duration_s) + "</td>" +
    '<td class="num">' + fmtTokens(x.gen_tokens) + "</td>" +
    '<td class="num">' + (x.avg_gen_tps != null ? x.avg_gen_tps + " t/s" : "—") + "</td>" +
    '<td class="num">' + (x.mtp_acc != null ? x.mtp_acc + "%" : "—") + "</td>" +
    "</tr>").join("");
  tb.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.onclick = () => { location.href = "/session/" + tr.dataset.id; };
  });
}

function initOverview() {
  const ch = makeCharts();
  window.__onLive = (d) => {
    if (d.current) renderOvTop(d);
    if (d.today) {
      const t = el("ovTokVal");
      if (t) t.textContent = fmtTokens(d.today.tokens);
    }
  };
  return api("/api/overview").then((d) => {
    renderOvTop(d);
    const u = d.usage_24h;
    if (u.series.length) {
      ch.add(el("ovUsage"), areaStackOption(u.labels,
        u.series.map((s) => ({ name: s.name, color: s.color, data: s.data }))));
    } else el("ovUsage").innerHTML = '<div class="empty">no token activity in the last 24h</div>';
    renderHBar(ch, el("ovInf"), d.inference_by_model.map((x) => x.name),
      [{ name: "seconds", color: OC.blue, data: d.inference_by_model.map((x) => x.seconds) }],
      "no inference time in the last 7 days");
    renderHBar(ch, el("ovTok"), d.tokens_by_model.map((x) => x.name),
      [{ name: "tokens", color: OC.orange, data: d.tokens_by_model.map((x) => x.tokens) }],
      "no tokens in the last 7 days");
    renderOvRecent(d.recent_sessions);
    if (d.today) {
      el("ovTokVal").textContent = fmtTokens(d.today.tokens);
      el("ovSessSub").textContent = d.today.sessions + " sessions today";
      el("ovInfVal").textContent = fmtDur(d.today.inference_s);
      el("ovInfSub").textContent = (d.today.utilization != null ? d.today.utilization + "% utilization" : "") +
        " · loaded " + fmtDur(d.today.loaded_s) + " · idle " + fmtDur(d.today.idle_s);
    }
  });
}

/* ------------------------------------------------------------------ models page */
function initModels(meta) {
  const ch = makeCharts();
  const rowCh = makeCharts();
  let selChart = null;
  let selectedWasActive = false;
  let activeSignature = null;
  const disp = (meta && meta.display) || {};
  const st = {
    range: RANGES.includes(disp.default_range) ? disp.default_range : "7d",
    group: GROUPS.includes(disp.default_group) ? disp.default_group : "family",
    sort: "active",
    rows: [],
    top: {},
    selectedKey: null,
  };

  const load = () => api("/api/models?range=" + st.range + "&group=" + st.group);

  const sorted = () => {
    const rows = st.rows.slice();
    rows.sort((a, b) => {
      if (st.sort === "active") {
        return (b.active_rank || 0) - (a.active_rank || 0) ||
          (b.active_tasks || 0) - (a.active_tasks || 0) ||
          (b.active_seen_at || 0) - (a.active_seen_at || 0) ||
          (b.inference_s || 0) - (a.inference_s || 0) ||
          String(a.label).localeCompare(String(b.label));
      }
      return (b[st.sort + "_s"] || 0) - (a[st.sort + "_s"] || 0) ||
        String(a.label).localeCompare(String(b.label));
    });
    return rows;
  };

  const renderTopCards = (top) => {
    ch.clear();
    st.top = top || {};
    const spark = new Array(24).fill(0);
    st.rows.forEach((r) => (r.spark || []).forEach((v, i) => { spark[i] += v || 0; }));
    el("topCards").innerHTML =
      metricCard("TOKENS", fmtTokens(top.tokens), trendHtml(top.tokens_trend), "topSpark") +
      metricCard("FAMILIES", top.families != null ? top.families : "—", top.families_leader || "", null) +
      metricCard("SESSIONS", top.sessions != null ? top.sessions : "—",
        top.avg_context_session ? "avg ctx " + fmtNum(top.avg_context_session) : "", null) +
      metricCard("GENERATED", top.generated_pct != null ? top.generated_pct + "%" : "—", "of all tokens", null) +
      metricCard("LEADER SHARE", top.leader_share != null ? top.leader_share + "%" : "—", top.leader_name || "", null) +
      metricCard("FASTEST GEN", top.fastest != null ? top.fastest + " t/s" : "—", top.fastest_name || "", null);
    ch.spark(el("topSpark"), spark, OC.blue);
  };

  const runtimeHtml = (live) => {
    live = live || { status: "NO DATA", snapshot: "NO DATA" };
    const status = live.status || "NO DATA";
    const statusHtml = '<span class="runtime-badge status-' + esc(status.toLowerCase().replace(/\s+/g, "-")) +
      '">● ' + esc(status) + '</span>';
    if (!live.source) {
      return '<div class="live-timing"><div class="live-timing-head"><span>RUNTIME TELEMETRY</span>' +
        statusHtml + '</div><div class="live-timing-note">No reliable runtime observation exists for this selection.</div></div>';
    }
    const item = (label, value) => '<span class="live-timing-item"><i>' + esc(label) + '</i><b>' +
      esc(value == null ? "—" : value) + "</b></span>";
    const age = live.age_s == null ? "—" : live.age_s < 1 ? "just now" : live.age_s + "s ago";
    const ctx = live.context;
    const ctxMax = live.context_max;
    const ctxDetail = ctx != null ? fmtNum(ctx) + " / " + (ctxMax ? fmtNum(ctxMax) + " tokens" : "max unavailable") : "no context data";
    const ctxCenter = live.context_pct != null ? Math.round(live.context_pct) + "%" : (ctx != null ? fmtTokens(ctx) : "—");
    const note = live.source === "slots" ?
      "Slot-observed and provisional; completed totals still come from /metrics." :
      live.source === "metrics" ? "Latest completed /metrics observation; not a live slot." :
        "No reliable runtime observation exists for this selection.";
    return '<div class="live-timing"><div class="live-timing-head"><span>RUNTIME TELEMETRY</span>' +
      statusHtml + '</div>' + (live.model ? '<div class="runtime-variant">' + esc(live.model) +
        (live.snapshot ? ' · ' + esc(live.snapshot) : "") + '</div>' : "") +
      '<div class="runtime-layout"><div class="live-timing-grid">' +
      item("slot", live.slot_id) + item("task", live.task_id) +
      item("n_gen", live.gen_tokens == null ? "—" : fmtNum(live.gen_tokens)) +
      item("tg avg", live.gen_tps_avg != null ? live.gen_tps_avg.toFixed(2) + " t/s" :
        live.processing ? "warming up" : "—") +
      item("tg 3s", live.gen_tps_3s != null ? live.gen_tps_3s.toFixed(2) + " t/s" :
        live.processing ? "warming up" : "—") +
      item("observed", age) + '</div><div class="runtime-context">' +
      resourceGauge("CONTEXT", live.context_pct, ctxCenter, ctxDetail, "Context " + ctxDetail) +
      '</div></div><div class="live-timing-note">' + esc(note) + '</div></div>';
  };

  const updateRuntime = (live) => {
    const target = el("selRuntime");
    if (target) target.innerHTML = runtimeHtml(live);
  };

  const renderSelected = (row) => {
    const box = el("selPanel");
    if (!row) { box.innerHTML = '<div class="empty">select a model</div>'; return; }
    const requestedKey = row.key;
    box.innerHTML = '<div class="empty"><span class="spin"></span> loading…</div>';
    api("/api/models/selected?ids=" + row.model_ids.join(",") + "&range=" + st.range).then((s) => {
      if (requestedKey !== st.selectedKey) return;
      if (!s || !s.label) { box.innerHTML = '<div class="empty">no data for selection</div>'; return; }
      const pt = s.prompt_tokens || 0, gt = s.gen_tokens || 0, tot = (pt + gt) || 1;
      const hasMtp = s.mtp_proposed != null && s.mtp_proposed > 0;
      const live = s.live && !s.live.tasks ? s.live : (s.live && s.live.tasks ? s.live.tasks[0] : null);
      box.innerHTML =
        '<div class="sel-head"><span class="m-dot" style="background:' + esc(s.color) + '"></span>' +
        '<span class="tag">SELECTED</span><span class="sel-name">' + esc(s.label) + "</span></div>" +
        '<div class="mc-sub" style="margin-top:2px">' + esc(s.provider || "") + " · range " + st.range + "</div>" +
        '<div id="selRuntime">' + runtimeHtml(s.realtime) + "</div>" +
        '<div class="sel-grid">' +
        selMetric("TOKENS", fmtTokens(s.tokens), s.share != null ? s.share + "% of all models" : "") +
        selMetric("GENERATED", s.generated_pct != null ? s.generated_pct + "%" : "—", fmtTokens(gt) + " generated") +
        selMetric("SESSIONS", s.sessions != null ? s.sessions : "—",
          s.per_session != null ? fmtTokens(s.per_session) + " tokens each" : "") +
        selMetric(live ? "LIVE GEN" : "PEAK GEN", live ? fmtTokens(live.gen_tokens) :
          (s.peak_gen != null ? s.peak_gen + " t/s" : "—"), live ?
          ((live.gen_tps != null ? live.gen_tps + " t/s · " : "") + "provisional") :
          (s.gen_tps != null ? "avg " + s.gen_tps + " t/s" : "")) +
        selMetric("PEAK PROMPT", s.peak_prompt != null ? s.peak_prompt + " t/s" : "—", fmtTokens(pt) + " prompt") +
        selMetric("PROMPT SHARE", pt ? Math.round(pt / (pt + gt) * 100) + "%" : "—", "of group tokens") +
        selMetric("MTP ACCEPTANCE", hasMtp && s.mtp_acc != null ? s.mtp_acc + "%" : "No activity",
          hasMtp ? fmtTokens(s.mtp_accepted) + " accepted / " + fmtTokens(s.mtp_proposed) + " proposed" :
            "no proposed draft tokens") +
        selMetric("MTP DRAFTS", hasMtp ? fmtTokens(s.mtp_accepted) + " accepted" : "—",
          hasMtp ? fmtTokens(s.mtp_rejected) + " rejected" : "not observed") +
        "</div>" +
        '<div class="stackbar"><i style="width:' + (pt / tot * 100) + "%;background:" + OC.amber + '"></i>' +
        '<i style="width:' + (gt / tot * 100) + "%;background:" + OC.green + '"></i></div>' +
        '<div class="legend"><span><i style="background:' + OC.amber + '"></i>prompt ' + fmtTokens(pt) +
        '</span><span><i style="background:' + OC.green + '"></i>generated ' + fmtTokens(gt) + "</span></div>" +
        '<div id="selSpark" style="width:100%;height:34px;margin-top:10px"></div>';
      selectedWasActive = !!(s.realtime && ["LIVE", "FINALIZING"].includes(s.realtime.status));
      if (selChart) { try { selChart.dispose(); } catch (e) {} selChart = null; }
      selChart = sparkline(el("selSpark"), s.spark || [0], s.color);
    });
  };

  const renderTable = () => {
    rowCh.clear();
    const rows = sorted();
    const metricHead = el("sortMetricHead");
    if (metricHead) metricHead.textContent = METRIC_LABELS[st.sort];
    const unit = st.group === "file" ? "files" : st.group === "quant" ? "quants" : "families";
    el("mModelsCount").textContent = rows.length ? rows.length + " " + unit + " · " + st.range : "";
    const tb = el("modelTableBody");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="8"><div class="empty">no model activity in this range</div></td></tr>';
      return;
    }
    const maxMetric = st.sort === "active" ? 0 : Math.max(1, ...rows.map((r) => r[st.sort + "_s"] || 0));
    const sortCell = (r) => {
      if (st.sort === "active") {
        if (!r.active_rank) return '<span class="muted">—</span>';
        return '<span class="row-live status-' + (r.active_status || "LIVE").toLowerCase() + '">● ' +
          esc(r.active_status || "LIVE") + '</span><span class="task-count">' +
          (r.active_tasks || 0) + ' task' + ((r.active_tasks || 0) === 1 ? "" : "s") + '</span>';
      }
      const seconds = r[st.sort + "_s"] || 0;
      return '<div class="sort-duration">' + esc(fmtDur(seconds)) + '</div><div class="share-bar"><i style="width:' +
        Math.min(100, seconds / maxMetric * 100) + '%"></i></div>';
    };
    tb.innerHTML = rows.map((r, i) =>
      '<tr class="clickable' + (r.key === st.selectedKey ? " selected" : "") + '" data-key="' + esc(r.key) + '">' +
      '<td class="m-rank">' + (i + 1) + "</td>" +
      '<td class="m-name"><span class="m-dot" style="background:' + esc(r.color) + '"></span> ' + esc(r.label) +
      (r.active_rank ? ' <span class="row-live status-' + (r.active_status || "LIVE").toLowerCase() + '">● ' +
        esc(r.active_status || "LIVE") + '</span>' : "") + "</td>" +
      '<td class="share-cell">' + sortCell(r) + '</td>' +
      '<td><div class="row-spark" id="rsp' + i + '"></div></td>' +
      '<td class="num"><b>' + fmtTokens(r.tokens) + "</b></td>" +
      '<td class="num">' + (r.share != null ? r.share + "%" : "—") + "</td>" +
      '<td class="num">' + r.sessions + "</td>" +
      '<td class="num">' + (r.gen_tps != null ? r.gen_tps : "—") + "</td>" +
      "</tr>").join("");
    rows.forEach((r, i) => { rowCh.spark(el("rsp" + i), r.spark || [0], r.color); });
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

  const render = (d) => {
    st.rows = d.rows || [];
    if (st.selectedKey == null) st.selectedKey = st.rows.length ? sorted()[0].key : null;
    if (st.selectedKey && !st.rows.find((r) => r.key === st.selectedKey)) {
      st.selectedKey = st.rows.length ? sorted()[0].key : null;
    }
    renderTopCards(d.top || {});
    renderTable();
    const row = st.rows.find((r) => r.key === st.selectedKey);
    renderSelected(row || null);
  };

  segControl("grpSeg", GROUPS.map((g) => GROUP_LABELS[g]), GROUP_LABELS[st.group], (lbl) => {
    st.group = GROUPS.find((g) => GROUP_LABELS[g] === lbl) || "family";
    fetch("/api/settings/display", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_group: st.group }),
    }).catch(() => {});
    load().then(render);
  });
  segControl("rngSeg", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[st.range], (lbl) => {
    st.range = RANGES.find((r) => RANGE_LABELS[r] === lbl) || "7d";
    fetch("/api/settings/display", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_range: st.range }),
    }).catch(() => {});
    load().then(render);
  });
  segControl("sortSeg", METRIC_KEYS.map((k) => METRIC_LABELS[k]), METRIC_LABELS[st.sort], (lbl) => {
    st.sort = METRIC_KEYS.find((k) => METRIC_LABELS[k] === lbl) || "active";
    renderTable();
  });

  window.__onLive = (d) => {
    const activeModels = (d && d.active_models) || [];
    const unrepresented = activeModels.some((a) => !st.rows.some((r) => (r.model_ids || []).includes(a.model_id)));
    if (unrepresented) {
      load().then((fresh) => {
        st.rows = fresh.rows || [];
        renderTopCards(fresh.top || {});
        renderTable();
      }).catch(() => {});
    }
    const signature = activeModels.map((x) => [x.model_id, x.rank, x.task_count].join(":"))
      .sort().join("|");
    if (signature !== activeSignature) {
      activeSignature = signature;
      st.rows.forEach((r) => {
        r.active = false; r.active_rank = 0; r.active_tasks = 0;
        r.active_seen_at = null; r.active_status = null;
      });
      activeModels.forEach((a) => {
        st.rows.filter((r) => (r.model_ids || []).includes(a.model_id)).forEach((r) => {
          r.active = true;
          r.active_rank = Math.max(r.active_rank || 0, a.rank || 0);
          r.active_tasks = (r.active_tasks || 0) + (a.task_count || 0);
          r.active_seen_at = Math.max(r.active_seen_at || 0, a.latest_seen || 0);
          r.active_status = r.active_rank === 2 ? "LIVE" : "FINALIZING";
        });
      });
      renderTable();
    }
    const row = st.rows.find((r) => r.key === st.selectedKey);
    const selectedActive = row ? activeModels.filter((a) => (row.model_ids || []).includes(a.model_id)) : [];
    selectedActive.sort((a, b) => (b.rank || 0) - (a.rank || 0) ||
      (b.task_count || 0) - (a.task_count || 0) || (b.latest_seen || 0) - (a.latest_seen || 0));
    if (selectedActive.length) {
      updateRuntime(selectedActive[0].realtime);
      selectedWasActive = true;
    } else if (selectedWasActive && row) {
      selectedWasActive = false;
      api("/api/models/selected?ids=" + row.model_ids.join(",") + "&range=" + st.range)
        .then((s) => { if (row.key === st.selectedKey) updateRuntime(s.realtime); })
        .catch(() => {});
    }
  };

  load().then(render);
}
/* Observatory - dashboard app logic (part B: detail, sessions, compare, hw, settings) */

/* ------------------------------------------------------------- model detail */
function initModelDetail(meta) {
  const mid = (window.QUERY && window.QUERY.mid) || 0;
  const ch = makeCharts();
  const st = { range: "24h" };
  const load = () => api("/api/model/" + mid + "?range=" + st.range);

  const render = (d) => {
    if (!d || !d.model) return;
    ch.clear();
    const m = d.model;
    const live = m.live && !m.live.tasks ? m.live : (m.live && m.live.tasks ? m.live.tasks[0] : null);
    el("mdlHead").innerHTML =
      '<span class="m-dot" style="background:' + esc(m.color) + ';width:10px;height:10px"></span>' +
      '<span style="font-size:14px;font-weight:600">' + esc(m.name) + "</span>" +
      (m.quant ? '<span class="badge">' + esc(m.quant) + "</span>" : "") +
      (m.family && m.family !== m.name ? '<span class="muted">· ' + esc(m.family) + "</span>" : "") +
      (m.params ? '<span class="muted">· ' + esc(m.params) + "</span>" : "") +
      stateBadge(m.live_state) +
      '<span class="muted" style="font-size:11px">' + esc(m.provider || "") + "</span>" +
      '<a href="/models" class="muted" style="font-size:11px;margin-left:12px">← models</a>';

    el("mdlCards").innerHTML =
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
        d.mtp_proposed ? d.mtp_proposed + " proposed · " + d.mtp_accepted + " accepted" : "no MTP counters", null);

    const acc = d.accounting;
    const total = Math.max(1, acc.loaded_s || 1);
    el("mdlAccBar").innerHTML =
      '<i style="width:' + ((acc.prompt_s || 0) / total * 100) + "%;background:" + OC.amber + '"></i>' +
      '<i style="width:' + ((acc.gen_s || 0) / total * 100) + "%;background:" + OC.green + '"></i>' +
      '<i style="width:' + ((acc.idle_s || 0) / total * 100) + "%;background:#3a3a36" + '"></i>';
    el("mdlAccLegend").innerHTML =
      '<span><i style="background:' + OC.amber + '"></i>prompt ' + fmtDur(acc.prompt_s) + "</span>" +
      '<span><i style="background:' + OC.green + '"></i>generation ' + fmtDur(acc.gen_s) + "</span>" +
      '<span><i style="background:#3a3a36"></i>idle ' + fmtDur(acc.idle_s) + "</span>" +
      '<span class="muted">loaded ' + fmtDur(acc.loaded_s) + " · " + d.sessions + " sessions</span>";
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
    if ((g.series.mtp_acc || []).filter((v) => v != null).length) {
      ch.add(el("mdlMtp"), lineOption(g.labels,
        [{ name: "mtp acc %", color: OC.orange, data: g.series.mtp_acc }]));
    } else el("mdlMtp").innerHTML = '<div class="empty">no MTP data in range</div>';
    const mdlGpuSeries = gpuDualSeries(g.gpus || []);
    ch.add(el("mdlHw"), dualAxisOption(g.labels, mdlGpuSeries.length ? mdlGpuSeries : [
      { name: "gpu 0 util %", color: OC.green, data: g.series.gpu_util, y: 0 },
      { name: "gpu 0 VRAM MB", color: OC.blue, data: g.series.vram_mb, y: 1 },
    ]));
    el("mdlGpuCards").innerHTML = gpuSummaryCards(g.gpus || [], "range");

    cfgPanelHtml(d.config, "mdlCfg", "mdlFlags", el("mdlCfgSub"));

    const hb = el("mdlCfgHistBody");
    if (d.configs && d.configs.length > 1) {
      hb.innerHTML = d.configs.map((c) =>
        "<tr><td>" + esc(c.fingerprint) + "</td><td>" + fmtDate(c.created_at) +
        '</td><td class="num">' + (c.context != null ? fmtNum(c.context) : "—") +
        '</td><td class="num">' + (c.kv_cache_k || "—") +
        '</td><td class="num">' + (c.mtp_enabled ? "on" : c.mtp_enabled === false ? "off" : "—") +
        '</td><td class="num">' + (c.threads != null ? c.threads : "—") + "</td></tr>").join("");
    } else {
      el("mdlCfgHistWrap").style.display = "none";
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

  const reload = () => api(params()).then(render);
  el("fProvider").onchange = (e) => { st.provider = e.target.value; st.model = ""; reload(); };
  el("fModel").onchange = (e) => { st.model = e.target.value; reload(); };
  el("fQuant").onchange = (e) => { st.quant = e.target.value; reload(); };
  el("fReasoning").onchange = (e) => { st.reasoning = e.target.value; reload(); };
  segControl("sessRange", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[st.range], (lbl) => {
    st.range = RANGES.find((r) => RANGE_LABELS[r] === lbl) || "7d"; reload();
  });
  segControl("sessMtp", ["any", "on", "off"], "any", (v) => { st.mtp = v; reload(); });
  reload();
  window.setInterval(reload, 2000);
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
  const rows = [
    ["Variants", (x) => (x.variants || []).join(" · ") || "—"],
    ["Quants", (x) => (x.quants || []).join(" · ") || "—"],
    ["Providers", (x) => (x.providers || []).join(" · ") || "—"],
    ["Total tokens", (x) => fmtTokens(x.tokens)],
    ["Prompt tokens", (x) => fmtTokens(x.prompt_tokens)],
    ["Generated tokens", (x) => fmtTokens(x.gen_tokens)],
    ["Avg prompt t/s", (x) => x.prompt_tps, "max"],
    ["Peak prompt t/s", (x) => x.peak_prompt_tps, "max"],
    ["Avg gen t/s", (x) => x.avg_gen_tps, "max"],
    ["Peak gen t/s", (x) => x.peak_gen_tps, "max"],
    ["Inference time", (x) => fmtDur(x.inference_s)],
    ["Loaded / idle", (x) => fmtDur(x.loaded_s) + " / " + fmtDur(x.idle_s)],
    ["Utilization", (x) => x.utilization != null ? x.utilization + "%" : "—", "max"],
    ["Context max", (x) => x.context_max ? fmtNum(x.context_max) : "—"],
    ["MTP acceptance", (x) => x.mtp_acc != null ? x.mtp_acc + "%" : "—", "max"],
    ["MTP drafts", (x) => x.mtp_proposed ? fmtTokens(x.mtp_accepted) + " accepted / " + fmtTokens(x.mtp_rejected) + " rejected" : "—"],
    ["Configuration", (x) => x.configuration || "—"],
    ["KV cache", (x) => x.kv_cache || "—"],
    ["Split / reasoning", (x) => (x.split_mode || "—") + " / " + (x.reasoning_effort || "—")],
    ["Per-GPU averages", (x) => (x.gpus || []).map((g) => "GPU " + g.index + ": " +
      (g.summary.util != null ? g.summary.util + "%" : "—") + " / " +
      (g.summary.vram_mb != null ? fmtBytes(g.summary.vram_mb * 1024 * 1024) : "—")).join(" · ") || "—"],
    ["Build", (x) => x.build || "—"],
  ];

  const query = (path, includeKeys) => {
    const q = new URLSearchParams({ range: st.range });
    if (st.provider) q.set("provider", st.provider);
    if (includeKeys) q.set("keys", st.selected.join("|"));
    return path + "?" + q.toString();
  };
  const renderPick = () => {
    el("cmpCount").textContent = st.selected.length + " / 5 selected";
    el("cmpPick").innerHTML = st.candidates.length ? st.candidates.map((x, i) =>
      '<label class="cmp-row"><input type="checkbox" data-idx="' + i + '"' +
      (st.selected.includes(x.key) ? " checked" : "") + ">" +
      '<span class="m-dot" style="background:' + esc(x.color || "#74736e") + '"></span>' +
      '<span>' + esc(x.label) + '</span><span class="muted" style="margin-left:auto">' +
      (x.gen_tps != null ? x.gen_tps + " t/s" : fmtTokens(x.tokens)) + "</span></label>").join("") :
      '<div class="empty">no model-family activity in this range</div>';
    el("cmpPick").querySelectorAll("input").forEach((input) => {
      input.onchange = () => {
        const key = st.candidates[Number(input.dataset.idx)].key;
        if (input.checked) {
          if (st.selected.length >= 5) { input.checked = false; return; }
          st.selected.push(key);
        } else st.selected = st.selected.filter((value) => value !== key);
        renderPick(); loadCompare();
      };
    });
  };
  const loadCompare = () => {
    el("cmpDeltaPanel").hidden = true;
    if (st.selected.length < 2) {
      el("cmpTable").innerHTML = '<div class="empty">select at least two model families to compare</div>';
      return;
    }
    api(query("/api/compare/models", true)).then((data) => {
      const models = data.models || [];
      let html = '<table class="data-table"><tr><th style="width:150px"></th>' + models.map((x) =>
        '<th><span class="m-dot" style="background:' + esc(x.color || "#74736e") + '"></span> ' + esc(x.model) + "</th>").join("") + "</tr>";
      rows.forEach(([label, get, mode]) => {
        const values = models.map(get); let best = -1, max = null;
        if (mode === "max") values.forEach((value, i) => {
          const n = parseFloat(value); if (Number.isFinite(n) && (max == null || n > max)) { max = n; best = i; }
        });
        html += '<tr><td class="muted">' + label + "</td>" + values.map((value, i) =>
          '<td class="num' + (i === best ? " diff-hl" : "") + '">' + esc(value == null ? "—" : value) + "</td>").join("") + "</tr>";
      });
      el("cmpTable").innerHTML = html + "</table>";
    });
  };
  const loadCandidates = () => api(query("/api/compare/models/candidates", false)).then((data) => {
    st.candidates = data.models || [];
    const available = new Set(st.candidates.map((x) => x.key));
    st.selected = st.selected.filter((key) => available.has(key));
    renderPick(); loadCompare();
  });
  fillSelect(el("cmpProvider"), "All providers", providers.map((p) => [p.id, p.name]), st.provider);
  el("cmpProvider").onchange = (event) => { st.provider = event.target.value; loadCandidates(); };
  segControl("cmpRange", RANGES.map((r) => RANGE_LABELS[r]), RANGE_LABELS[st.range], (label) => {
    st.range = RANGES.find((r) => RANGE_LABELS[r] === label) || "7d"; loadCandidates();
  });
  loadCandidates();
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
        if (detail.open) ch.list.forEach((chart) => { try { chart.resize(); } catch (e) {} });
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
    }).then((x) => x.json()).then(() => { location.reload(); });
  };

  loadAll().then(([st, provs, disp]) => {
    renderSystem(st);
    renderProviders(provs.providers);
    renderDisplay(disp.display);
  });
  return Promise.resolve();
}

document.addEventListener("DOMContentLoaded", bootstrap);
