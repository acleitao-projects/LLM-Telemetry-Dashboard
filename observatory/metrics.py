"""Aggregation queries feeding the dashboard JSON APIs."""
from __future__ import annotations

import re
import time
import hashlib
import json
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from . import database as db
from .models import (BuildInfo, GpuTelemetrySample, HardwareInfo, Model,
                     ModelConfig, Provider, SessionRow, TelemetrySample, now_ms)
from .settings import (FAMILY_TAG_TOKENS, MODEL_COLORS, QUANT_TOKENS,
                       RANGE_BUCKETS, TELEMETRY_GROUPS)

DT_CAP_S = 300.0
SPARK_BUCKETS = 24
GPU_COLORS = ["#48a77c", "#4b8de8", "#d8733e", "#d29b25", "#8a7bc8", "#4fa3a5"]


def _latest_build(s: Session, provider_id: int) -> Optional[BuildInfo]:
    identity = (
        ((BuildInfo.version.is_not(None)) & (BuildInfo.version != "")) |
        ((BuildInfo.commit.is_not(None)) & (BuildInfo.commit != "")) |
        ((BuildInfo.docker_image.is_not(None)) & (BuildInfo.docker_image != "")) |
        ((BuildInfo.container_id.is_not(None)) & (BuildInfo.container_id != ""))
    )
    return s.exec(select(BuildInfo).where(
        BuildInfo.provider_id == provider_id, identity,
    ).order_by(BuildInfo.last_seen_at.desc())).first()


def _json_ids(raw: str) -> set[int]:
    try:
        return {int(value) for value in json.loads(raw or "[]")}
    except (TypeError, ValueError):
        return set()


def _gpu_color(key: str) -> str:
    digest = hashlib.sha1(key.encode()).digest()[0]
    return GPU_COLORS[digest % len(GPU_COLORS)]


def _gpu_rows(s: Session, provider_id: int, start: int, end: Optional[int] = None,
              model_id: Optional[int] = None,
              session_id: Optional[int] = None) -> list[GpuTelemetrySample]:
    q = select(GpuTelemetrySample).where(
        GpuTelemetrySample.provider_id == provider_id,
        GpuTelemetrySample.ts >= start,
    )
    if end is not None:
        q = q.where(GpuTelemetrySample.ts <= end)
    rows = list(s.exec(q.order_by(GpuTelemetrySample.ts)).all())
    if model_id is not None:
        rows = [row for row in rows if model_id in _json_ids(row.active_model_ids)]
    if session_id is not None:
        rows = [row for row in rows if session_id in _json_ids(row.active_session_ids)]
    return rows


def _gpu_series(rows: list[GpuTelemetrySample], start: int, end: int,
                bucket_s: int) -> list[dict]:
    nb = max(2, int(max(1, end - start) / 1000 / bucket_s))
    by_index: dict[int, list[GpuTelemetrySample]] = {}
    for row in rows:
        by_index.setdefault(row.gpu_index, []).append(row)
    out = []
    for index, gpu_rows in sorted(by_index.items()):
        latest = gpu_rows[-1]
        key = latest.gpu_uuid or f"index:{index}"
        sums = {name: [[0.0, 0] for _ in range(nb)] for name in
                ("util", "vram_mb", "temp_c", "power_w")}
        attrs = {"util": "util", "vram_mb": "vram_used_mb",
                 "temp_c": "temp_c", "power_w": "power_w"}
        for row in gpu_rows:
            pos = min(nb - 1, max(0, int((row.ts - start) / 1000 / bucket_s)))
            for name, attr in attrs.items():
                value = getattr(row, attr)
                if value is not None:
                    sums[name][pos][0] += value
                    sums[name][pos][1] += 1
        series = {name: [round(total / count, 1) if count else None
                         for total, count in values]
                  for name, values in sums.items()}
        labels = [datetime.fromtimestamp((start + i * bucket_s * 1000) / 1000.0).strftime(
            "%H:%M:%S" if bucket_s < 60 else "%m-%d %H:%M") for i in range(nb)]
        summary = {}
        for name, attr in attrs.items():
            vals = [getattr(row, attr) for row in gpu_rows if getattr(row, attr) is not None]
            summary[name] = round(sum(vals) / len(vals), 1) if vals else None
        out.append({
            "key": key, "index": index, "uuid": latest.gpu_uuid,
            "name": latest.name, "label": f"GPU {index} · {latest.name or key}",
            "color": _gpu_color(key), "pcie": latest.pcie,
            "vram_total_mb": latest.vram_total_mb,
            "current": {
                "util": latest.util, "vram_mb": latest.vram_used_mb,
                "temp_c": latest.temp_c, "power_w": latest.power_w,
            },
            "summary": summary, "labels": labels, "series": series,
        })
    return out


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------
def parse_model_name(name: str) -> tuple[str, Optional[str]]:
    """Return (family, quant) parsed from a model file/alias name."""
    base = re.sub(r"\.(gguf|bin)$", "", name or "", flags=re.I)
    tokens = [t for t in base.split("-") if t]
    quant = None
    qi = None
    for i, t in enumerate(tokens):
        if t.upper() in QUANT_TOKENS:
            quant = t.upper()
            qi = i
            break
    core = tokens[:qi] + tokens[qi + 1:] if qi is not None else tokens
    if not core:
        core = tokens
    size_idx = None
    for i, t in enumerate(core):
        if re.match(r"^\d+(\.\d+)?[Bb](pw)?$", t) or re.match(r"^A\d+[Bb]$", t):
            size_idx = i
            break
    if size_idx is not None:
        fam_tokens = core[:size_idx + 1]
    else:
        fam_tokens = list(core)
        while fam_tokens:
            t = fam_tokens[-1]
            if (t.lower() in FAMILY_TAG_TOKENS
                    or re.match(r"^\d+(\.\d+)?bpw$", t, re.I)
                    or re.match(r"^M\d+$", t, re.I)):
                fam_tokens = fam_tokens[:-1]
            else:
                break
        if not fam_tokens:
            fam_tokens = core
    cleaned = [t for t in fam_tokens
               if t.lower() not in FAMILY_TAG_TOKENS
               and not re.match(r"^\d+(\.\d+)?bpw$", t, re.I)
               and not re.match(r"^M\d+$", t, re.I)]
    family = "-".join(cleaned) if cleaned else "-".join(fam_tokens)
    return family, quant


def next_model_color(s: Session, provider_id: int) -> str:
    n = len(s.exec(select(Model.id).where(Model.provider_id == provider_id)).all())
    return MODEL_COLORS[n % len(MODEL_COLORS)]


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------
def local_day_start_ms(now: int) -> int:
    dt = datetime.fromtimestamp(now / 1000.0)
    mid = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(mid.timestamp() * 1000)


def range_start_ms(key: str, now: int) -> int:
    if key == "today":
        return local_day_start_ms(now)
    if key == "all":
        return 0
    m = re.match(r"^(\d+)d$", key or "")
    if m:
        return now - int(m.group(1)) * 86400_000
    return now - 7 * 86400_000


# ---------------------------------------------------------------------------
# sample helpers
# ---------------------------------------------------------------------------
def fetch_samples(s: Session, prov_ids: list[int], start_ms: int,
                  end_ms: Optional[int] = None,
                  model_ids: Optional[list[int]] = None) -> list[TelemetrySample]:
    q = select(TelemetrySample).where(TelemetrySample.provider_id.in_(prov_ids))
    if start_ms is not None:
        q = q.where(TelemetrySample.ts >= start_ms)
    if end_ms is not None:
        q = q.where(TelemetrySample.ts <= end_ms)
    if model_ids:
        q = q.where(TelemetrySample.model_id.in_(model_ids))
    return list(s.exec(q.order_by(TelemetrySample.ts)).all())


def _delta(r, prev, attr) -> float:
    if r is None or prev is None:
        return 0.0
    a, b = getattr(prev, attr), getattr(r, attr)
    if a is None or b is None:
        return 0.0
    d = b - a
    return d if d > 0 else 0.0


def _phase_delta(r: TelemetrySample, prev: TelemetrySample,
                 tokens_attr: str, seconds_attr: str, tps_attr: str) -> float:
    """Prefer llama.cpp work-time counters, then a positive observed gauge."""
    token_delta = _delta(r, prev, tokens_attr)
    if token_delta <= 0:
        return 0.0
    seconds_delta = _delta(r, prev, seconds_attr)
    if seconds_delta > 0:
        return seconds_delta
    throughput = getattr(r, tps_attr)
    if throughput is not None and throughput > 0:
        return token_delta / throughput
    return 0.0


class ModelAcc:
    __slots__ = ("tokens", "prompt_tokens", "gen_tokens", "d_proposed", "d_accepted",
                 "gen_time", "prompt_time", "idle_time", "loaded_time",
                 "peak_gen", "peak_prompt", "context_max", "spark")

    def __init__(self):
        self.tokens = 0.0
        self.prompt_tokens = 0.0
        self.gen_tokens = 0.0
        self.d_proposed = 0.0
        self.d_accepted = 0.0
        self.gen_time = 0.0
        self.prompt_time = 0.0
        self.idle_time = 0.0
        self.loaded_time = 0.0
        self.peak_gen = 0.0
        self.peak_prompt = 0.0
        self.context_max = 0
        self.spark = [0.0] * SPARK_BUCKETS


def accumulate(samples: list[TelemetrySample], acc: dict[int, ModelAcc],
               start_ms: int, end_ms: int, now: int):
    """Fill acc (model_id -> ModelAcc) from ordered samples."""
    span = max(1, end_ms - start_ms)
    prev_by_model: dict[int, TelemetrySample] = {}
    for r in samples:
        mid = r.model_id
        a = acc.get(mid)
        if a is None:
            a = acc[mid] = ModelAcc()
        prev = prev_by_model.get(mid)
        if prev is not None:
            dt_s = min(DT_CAP_S, max(0.0, (r.ts - prev.ts) / 1000.0))
            a.tokens += _delta(r, prev, "tokens_total")
            a.prompt_tokens += _delta(r, prev, "prompt_total")
            a.gen_tokens += _delta(r, prev, "gen_total")
            a.prompt_time += _phase_delta(
                r, prev, "prompt_total", "prompt_seconds_total", "prompt_tps")
            a.gen_time += _phase_delta(
                r, prev, "gen_total", "gen_seconds_total", "gen_tps")
            a.d_proposed += _delta(r, prev, "mtp_proposed_total")
            a.d_accepted += _delta(r, prev, "mtp_accepted_total")
            st = prev.state
            if st in ("IDLE", "PROMPTING", "GENERATING"):
                a.loaded_time += dt_s
            b = min(SPARK_BUCKETS - 1, int((prev.ts - start_ms) * SPARK_BUCKETS / span))
            d_tok = _delta(r, prev, "tokens_total")
            if d_tok == 0.0:
                d_tok = _delta(r, prev, "prompt_total") + _delta(r, prev, "gen_total")
            a.spark[max(0, b)] += d_tok
        if r.gen_tps is not None:
            a.peak_gen = max(a.peak_gen, r.gen_tps)
        if r.prompt_tps is not None:
            a.peak_prompt = max(a.peak_prompt, r.prompt_tps)
        if r.context_used:
            a.context_max = max(a.context_max, r.context_used)
        prev_by_model[mid] = r
    # if a model is loaded now, count the tail up to `now`
    for mid, a in acc.items():
        rows = [r for r in samples if r.model_id == mid]
        if rows:
            last = rows[-1]
            if last.state in ("IDLE", "PROMPTING", "GENERATING") and now - last.ts < 60_000:
                a.loaded_time += min(60.0, (now - last.ts) / 1000.0)
        a.idle_time = max(0.0, a.loaded_time - a.prompt_time - a.gen_time)


def sessions_in_range(s: Session, prov_ids: list[int], start_ms: int,
                      model_ids: Optional[list[int]] = None) -> list[SessionRow]:
    q = select(SessionRow).where(
        SessionRow.provider_id.in_(prov_ids),
        SessionRow.start_at <= now_ms(),
        (SessionRow.end_at.is_(None) | (SessionRow.end_at >= start_ms)),
    )
    if model_ids:
        q = q.where(SessionRow.model_id.in_(model_ids))
    return list(s.exec(q).all())


def _sessions_by_model(sess: list[SessionRow]) -> dict[int, list[SessionRow]]:
    out: dict[int, list[SessionRow]] = {}
    for x in sess:
        out.setdefault(x.model_id or 0, []).append(x)
    return out


def _fmt_ago(ts_ms: Optional[int], now: int) -> str:
    if not ts_ms:
        return "never"
    d = max(0, (now - ts_ms) / 1000.0)
    if d < 5:
        return "just now"
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d / 60)}m ago"
    if d < 86400:
        return f"{int(d / 3600)}h ago"
    return f"{int(d / 86400)}d ago"


def _build_str(b) -> Optional[str]:
    """Display form of a build tag (keeps versions that already start with 'b')."""
    if b is None or not b.version:
        return None
    v = str(b.version)
    return v if v.lower().startswith("b") else f"b{v}"


# ---------------------------------------------------------------------------
# Models page
# ---------------------------------------------------------------------------
def models_page(s: Session, provider_id: Optional[int], range_key: str, group: str) -> dict:
    now = now_ms()
    start = range_start_ms(range_key, now)
    provs = list(s.exec(select(Provider)).all())
    if provider_id:
        provs = [p for p in provs if p.id == provider_id]
    prov_ids = [p.id for p in provs]
    if not prov_ids:
        return {"rows": [], "top": {}}
    models = list(s.exec(select(Model).where(Model.provider_id.in_(prov_ids))).all())
    m_by_id = {m.id: m for m in models}
    samples = fetch_samples(s, prov_ids, start)
    acc: dict[int, ModelAcc] = {}
    accumulate(samples, acc, start, now, now)
    sess = sessions_in_range(s, prov_ids, start)
    sess_by_model = _sessions_by_model(sess)

    def gkey(m: Model) -> str:
        if group == "family":
            return m.family or m.name
        if group == "quant":
            return m.quant or "unknown"
        return m.name

    total_tokens = sum(a.tokens for a in acc.values())
    total_gen = sum(a.gen_tokens for a in acc.values())
    groups: dict[str, dict] = {}
    for m in models:
        a = acc.get(m.id)
        tokens = a.tokens if a else 0.0
        if tokens <= 0 and m.id not in sess_by_model:
            continue
        g = gkey(m)
        row = groups.setdefault(g, {
            "key": g, "label": g, "model_ids": [], "tokens": 0.0, "gen_tokens": 0.0,
            "prompt_tokens": 0.0, "gen_time": 0.0, "prompt_time": 0.0,
            "loaded_time": 0.0, "idle_time": 0.0,
            "peak_gen": 0.0, "peak_prompt": 0.0,
            "sessions": 0, "spark": [0.0] * SPARK_BUCKETS, "color": m.color,
        })
        row["model_ids"].append(m.id)
        row["tokens"] += tokens
        if a:
            row["gen_tokens"] += a.gen_tokens
            row["prompt_tokens"] += a.prompt_tokens
            row["gen_time"] += a.gen_time
            row["prompt_time"] += a.prompt_time
            row["loaded_time"] += a.loaded_time
            row["idle_time"] += a.idle_time
            row["peak_gen"] = max(row["peak_gen"], a.peak_gen)
            row["peak_prompt"] = max(row["peak_prompt"], a.peak_prompt)
            for i in range(SPARK_BUCKETS):
                row["spark"][i] += a.spark[i]
        row["sessions"] += len(sess_by_model.get(m.id, []))

    rows = []
    for row in groups.values():
        share = (row["tokens"] / total_tokens * 100.0) if total_tokens else 0.0
        gen_tps = (row["gen_tokens"] / row["gen_time"]) if row["gen_time"] > 0 else None
        rows.append({
            "key": row["key"], "label": row["label"], "model_ids": row["model_ids"],
            "color": row["color"], "tokens": round(row["tokens"]),
            "share": round(share, 1), "sessions": row["sessions"],
            "gen_tps": round(gen_tps, 1) if gen_tps else None,
            "peak_gen": round(row["peak_gen"], 1) if row["peak_gen"] else None,
            "inference_s": round(row["prompt_time"] + row["gen_time"]),
            "loaded_s": round(row["loaded_time"]),
            "idle_s": round(row["idle_time"]),
            "spark": [round(x) for x in row["spark"]],
        })
    rows.sort(key=lambda r: r["tokens"], reverse=True)

    # top metrics
    fam_tokens: dict[str, float] = {}
    for m in models:
        a = acc.get(m.id)
        if not a or a.tokens <= 0:
            continue
        f = m.family or m.name
        fam_tokens[f] = fam_tokens.get(f, 0.0) + a.tokens
    leader_row = rows[0] if rows else None
    fastest = None
    for r in rows:
        if r["peak_gen"] and (fastest is None or r["peak_gen"] > fastest[1]):
            fastest = (r["label"], r["peak_gen"])

    prev_start = start - max(1, now - start)
    prev_samples = fetch_samples(s, prov_ids, prev_start, start) if start > 1 else []
    prev_acc: dict[int, ModelAcc] = {}
    if prev_samples:
        accumulate(prev_samples, prev_acc, prev_start, start, start)
    prev_tokens = sum(a.tokens for a in prev_acc.values())
    trend = None
    if prev_tokens > 0:
        trend = round((total_tokens - prev_tokens) / prev_tokens * 100.0, 1)

    sess_ctx = [x.context_max for x in sess if x.context_max]
    top = {
        "tokens": round(total_tokens),
        "tokens_trend": trend,
        "families": len(fam_tokens),
        "families_leader": max(fam_tokens, key=fam_tokens.get) if fam_tokens else None,
        "sessions": len(sess),
        "avg_context_session": round(sum(sess_ctx) / len(sess_ctx)) if sess_ctx else None,
        "generated_pct": round(total_gen / total_tokens * 100.0, 1) if total_tokens else None,
        "leader_share": leader_row["share"] if leader_row else None,
        "leader_name": leader_row["label"] if leader_row else None,
        "fastest": fastest[1] if fastest else None,
        "fastest_name": fastest[0] if fastest else None,
    }
    return {"rows": rows, "top": top, "now": now, "range": range_key, "group": group}


def selected_stats(s: Session, model_ids: list[int], provider_id: Optional[int],
                   range_key: str) -> dict:
    if not model_ids:
        return {}
    now = now_ms()
    start = range_start_ms(range_key, now)
    provs = list(s.exec(select(Provider)).all())
    prov_ids = [p.id for p in provs if p.id == provider_id] if provider_id else [p.id for p in provs]
    models = {m.id: m for m in s.exec(select(Model).where(Model.id.in_(model_ids))).all()}
    samples = fetch_samples(s, prov_ids, start, model_ids=model_ids)
    acc: dict[int, ModelAcc] = {}
    accumulate(samples, acc, start, now, now)
    sess = sessions_in_range(s, prov_ids, start, model_ids=model_ids)
    tokens = sum(a.tokens for a in acc.values())
    gen_tokens = sum(a.gen_tokens for a in acc.values())
    prompt_tokens = sum(a.prompt_tokens for a in acc.values())
    gen_time = sum(a.gen_time for a in acc.values())
    mtp_proposed = sum(a.d_proposed for a in acc.values())
    mtp_accepted = sum(a.d_accepted for a in acc.values())
    mtp_rejected = max(0.0, mtp_proposed - mtp_accepted)
    mtp_acc = (mtp_accepted / mtp_proposed * 100.0) if mtp_proposed > 0 else None
    peak_gen = max((a.peak_gen for a in acc.values()), default=0.0)
    peak_prompt = max((a.peak_prompt for a in acc.values()), default=0.0)
    total_all = 0.0
    all_acc: dict[int, ModelAcc] = {}
    all_samples = fetch_samples(s, prov_ids, start)
    accumulate(all_samples, all_acc, start, now, now)
    total_all = sum(a.tokens for a in all_acc.values())
    span = max(1, now - start)
    spark = [0.0] * SPARK_BUCKETS
    prev = None
    for r in samples:
        if prev is not None and r.model_id == prev.model_id:
            d = _delta(r, prev, "tokens_total")
            if d == 0:
                d = _delta(r, prev, "prompt_total") + _delta(r, prev, "gen_total")
            b = min(SPARK_BUCKETS - 1, int((prev.ts - start) * SPARK_BUCKETS / span))
            spark[max(0, b)] += d
        prev = r
    m = models.get(model_ids[0])
    return {
        "label": m.name if m else None,
        "color": m.color if m else "#4b8de8",
        "tokens": round(tokens),
        "share": round(tokens / total_all * 100.0, 1) if total_all else None,
        "generated_pct": round(gen_tokens / tokens * 100.0, 1) if tokens else None,
        "gen_tokens": round(gen_tokens),
        "prompt_tokens": round(prompt_tokens),
        "sessions": len(sess),
        "per_session": round(tokens / len(sess)) if sess else None,
        "peak_gen": round(peak_gen, 1) if peak_gen else None,
        "peak_prompt": round(peak_prompt, 1) if peak_prompt else None,
        "gen_tps": round(gen_tokens / gen_time, 1) if gen_time > 0 else None,
        "mtp_proposed": round(mtp_proposed),
        "mtp_accepted": round(mtp_accepted),
        "mtp_rejected": round(mtp_rejected),
        "mtp_acc": round(mtp_acc, 1) if mtp_acc is not None else None,
        "spark": [round(x) for x in spark],
        "provider": (s.get(Provider, m.provider_id).name if m else None),
    }


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
def overview(s: Session, provider_id: Optional[int] = None) -> dict:
    now = now_ms()
    provs = list(s.exec(select(Provider).order_by(Provider.id)).all())
    provs_out = []
    live = None
    for p in provs:
        last = s.exec(select(TelemetrySample).where(
            TelemetrySample.provider_id == p.id).order_by(TelemetrySample.ts.desc())
            .limit(1)).first()
        entry = {
            "id": p.id, "name": p.name, "status": p.status, "enabled": p.enabled,
            "url": p.base_url, "latency_ms": p.latency_ms,
            "last_success_ago": _fmt_ago(p.last_success_at, now),
            "last_error": p.last_error,
        }
        provs_out.append(entry)
        if last is not None and (live is None or (last.ts or 0) > (live["sample"].ts or 0)):
            live = {"sample": last, "provider": p}

    current = None
    if live:
        r = live["sample"]
        model = s.get(Model, r.model_id) if r.model_id else None
        sess = None
        if r.session_id:
            sess = s.get(SessionRow, r.session_id)
        elapsed = None
        if sess:
            elapsed = max(0.0, (now - sess.start_at) / 1000.0)
        ctx_pct = None
        if r.context_used and r.context_max:
            ctx_pct = round(r.context_used / r.context_max * 100.0, 1)
        current = {
            "provider": live["provider"].name,
            "model": model.name if model else None,
            "model_id": model.id if model else None,
            "color": model.color if model else "#4b8de8",
            "state": r.state,
            "gen_tps": r.gen_tps,
            "prompt_tps": r.prompt_tps,
            "context_used": r.context_used,
            "context_max": r.context_max,
            "context_pct": ctx_pct,
            "mtp_acc": r.mtp_acc,
            "session_elapsed_s": round(elapsed) if elapsed is not None else None,
            "tokens_total": r.tokens_total,
            "sample_age_s": round((now - r.ts) / 1000.0, 1) if r.ts else None,
        }

    day_start = local_day_start_ms(now)
    prov_ids = [p.id for p in provs]
    samples = fetch_samples(s, prov_ids, day_start)
    acc: dict[int, ModelAcc] = {}
    accumulate(samples, acc, day_start, now, now)
    today = {
        "tokens": round(sum(a.tokens for a in acc.values())),
        "inference_s": round(sum(a.prompt_time + a.gen_time for a in acc.values())),
        "loaded_s": round(sum(a.loaded_time for a in acc.values())),
        "idle_s": round(sum(a.idle_time for a in acc.values())),
        "sessions": len(sessions_in_range(s, prov_ids, day_start)),
    }
    today["utilization"] = (round(today["inference_s"] / today["loaded_s"] * 100.0, 1)
                            if today["loaded_s"] > 0 else None)

    # model usage over last 24h (hourly buckets)
    h0 = now - 24 * 3600_000
    samples24 = fetch_samples(s, prov_ids, h0)
    per_model: dict[int, list[int]] = {}
    for r in samples24:
        if r.model_id is None:
            continue
        per_model.setdefault(r.model_id, [0] * 24)
    prev24 = None
    for r in samples24:
        if r.model_id is None:
            prev24 = r
            continue
        if prev24 is not None and r.model_id == prev24.model_id:
            d = _delta(r, prev24, "tokens_total")
            if d == 0:
                d = _delta(r, prev24, "prompt_total") + _delta(r, prev24, "gen_total")
            h = min(23, int((prev24.ts - h0) / 3600_000))
            per_model.setdefault(r.model_id, [0] * 24)[max(0, h)] += int(d)
        prev24 = r
    models = {m.id: m for m in s.exec(select(Model).where(Model.provider_id.in_(prov_ids))).all()}
    usage_series = []
    for mid, buckets in sorted(per_model.items(), key=lambda kv: sum(kv[1]), reverse=True):
        m = models.get(mid)
        if not m:
            continue
        if sum(buckets) <= 0:
            continue
        usage_series.append({"name": m.name, "color": m.color,
                             "model_id": mid, "data": [int(x) for x in buckets]})
    hour_labels = [datetime.fromtimestamp((h0 + i * 3600_000) / 1000.0).strftime("%H:%M")
                   for i in range(24)]

    # inference time by model (7d) + tokens by model (7d)
    w7 = now - 7 * 86400_000
    s7 = fetch_samples(s, prov_ids, w7)
    acc7: dict[int, ModelAcc] = {}
    accumulate(s7, acc7, w7, now, now)
    inference_by_model = []
    tokens_by_model = []
    for mid, a in acc7.items():
        m = models.get(mid)
        if not m:
            continue
        inf = a.prompt_time + a.gen_time
        if inf > 0:
            inference_by_model.append({"name": m.name, "color": m.color,
                                       "model_id": mid, "seconds": round(inf)})
        if a.tokens > 0:
            tokens_by_model.append({"name": m.name, "color": m.color,
                                    "model_id": mid, "tokens": round(a.tokens)})
    inference_by_model.sort(key=lambda x: x["seconds"], reverse=True)
    tokens_by_model.sort(key=lambda x: x["tokens"], reverse=True)

    recent = list(s.exec(select(SessionRow).order_by(SessionRow.start_at.desc()).limit(10)).all())
    recent_out = []
    for x in recent:
        m = models.get(x.model_id) if x.model_id else None
        recent_out.append({
            "id": x.id, "start": x.start_at, "model": m.name if m else None,
            "model_id": x.model_id, "color": m.color if m else None,
            "duration_s": x.duration_s, "gen_tokens": round(x.gen_tokens or 0),
            "prompt_tokens": round(x.prompt_tokens or 0),
            "avg_gen_tps": round(x.avg_gen_tps, 1) if x.avg_gen_tps else None,
            "mtp_acc": round(x.mtp_acc, 1) if x.mtp_acc is not None else None,
            "status": x.status, "context_max": x.context_max,
        })

    return {
        "providers": provs_out,
        "current": current,
        "today": today,
        "usage_24h": {"labels": hour_labels, "series": usage_series},
        "inference_by_model": inference_by_model,
        "tokens_by_model": tokens_by_model,
        "recent_sessions": recent_out,
        "now": now,
    }


def live_snapshot(s: Session) -> dict:
    """Lightweight snapshot for the SSE stream."""
    now = now_ms()
    provs = list(s.exec(select(Provider).order_by(Provider.id)).all())
    provs_out = []
    live = None
    for p in provs:
        last = s.exec(select(TelemetrySample).where(
            TelemetrySample.provider_id == p.id).order_by(TelemetrySample.ts.desc())
            .limit(1)).first()
        provs_out.append({"id": p.id, "name": p.name, "status": p.status,
                          "latency_ms": p.latency_ms, "enabled": p.enabled})
        if last is not None and (live is None or (last.ts or 0) > (live[0].ts or 0)):
            live = (last, p)
    current = None
    if live:
        r, p = live
        model = s.get(Model, r.model_id) if r.model_id else None
        sess = s.get(SessionRow, r.session_id) if r.session_id else None
        ctx_pct = round(r.context_used / r.context_max * 100.0, 1) if (r.context_used and r.context_max) else None
        current = {
            "provider": p.name, "model": model.name if model else None,
            "color": model.color if model else "#4b8de8", "state": r.state,
            "gen_tps": r.gen_tps, "prompt_tps": r.prompt_tps,
            "context_used": r.context_used, "context_max": r.context_max,
            "context_pct": ctx_pct, "mtp_acc": r.mtp_acc,
            "session_elapsed_s": round(max(0.0, (now - sess.start_at) / 1000.0)) if sess else None,
        }
    day_start = local_day_start_ms(now)
    samples = fetch_samples(s, [p.id for p in provs], day_start)
    acc: dict[int, ModelAcc] = {}
    accumulate(samples, acc, day_start, now, now)
    today = {
        "tokens": round(sum(a.tokens for a in acc.values())),
        "inference_s": round(sum(a.prompt_time + a.gen_time for a in acc.values())),
        "sessions": len(sessions_in_range(s, [p.id for p in provs], day_start)),
    }
    return {"providers": provs_out, "current": current, "today": today, "now": now}


# ---------------------------------------------------------------------------
# Model detail
# ---------------------------------------------------------------------------
def model_detail(s: Session, model_id: int, range_key: str) -> dict:
    now = now_ms()
    m = s.get(Model, model_id)
    if m is None:
        return {}
    provider = s.get(Provider, m.provider_id)
    start = range_start_ms(range_key, now)
    samples = fetch_samples(s, [m.provider_id], start, model_ids=[model_id])
    acc: dict[int, ModelAcc] = {}
    accumulate(samples, acc, start, now, now)
    a = acc.get(model_id) or ModelAcc()
    sess = sessions_in_range(s, [m.provider_id], start, model_ids=[model_id])
    inference = a.prompt_time + a.gen_time
    dts = range_detail(s, model_id, range_key, samples, start, now)
    gpu_start = now - 60_000 if range_key == "session" else start
    gpu_bucket = RANGE_BUCKETS["1m"] if range_key == "session" else \
        RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
    dts["gpus"] = _gpu_series(
        _gpu_rows(s, m.provider_id, gpu_start, now, model_id=model_id),
        gpu_start, now, gpu_bucket,
    )
    cfgs = list(s.exec(select(ModelConfig).where(ModelConfig.model_id == model_id)
                       .order_by(ModelConfig.created_at.desc())).all())
    mtp_acc = (a.d_accepted / a.d_proposed * 100.0) if a.d_proposed > 0 else None
    avg_gen = (a.gen_tokens / a.gen_time) if a.gen_time > 0 else None
    avg_prompt = (a.prompt_tokens / a.prompt_time) if a.prompt_time > 0 else None
    return {
        "model": {
            "id": m.id, "name": m.name, "key": m.key, "quant": m.quant,
            "family": m.family, "arch": m.arch, "params": m.params, "color": m.color,
            "first_seen": m.first_seen_at, "last_used": m.last_used_at,
            "provider": provider.name if provider else None,
            "live_state": _live_state(s, m.id, now),
        },
        "range": range_key,
        "accounting": {
            "loaded_s": round(a.loaded_time),
            "idle_s": round(a.idle_time),
            "prompt_s": round(a.prompt_time),
            "gen_s": round(a.gen_time),
            "inference_s": round(inference),
            "utilization": round(inference / a.loaded_time * 100.0, 1) if a.loaded_time > 0 else None,
        },
        "sessions": len(sess),
        "tokens": {
            "prompt": round(a.prompt_tokens),
            "generated": round(a.gen_tokens),
            "total": round(a.tokens),
        },
        "speeds": {
            "avg_gen_tps": round(avg_gen, 1) if avg_gen else None,
            "peak_gen_tps": round(a.peak_gen, 1) if a.peak_gen else None,
            "avg_prompt_tps": round(avg_prompt, 1) if avg_prompt else None,
            "peak_prompt_tps": round(a.peak_prompt, 1) if a.peak_prompt else None,
        },
        "mtp_acc": round(mtp_acc, 1) if mtp_acc is not None else None,
        "mtp_proposed": round(a.d_proposed),
        "mtp_accepted": round(a.d_accepted),
        "context": {"max_used": a.context_max},
        "config": _config_out(cfgs[0]) if cfgs else None,
        "configs": [_config_out(c) for c in cfgs[:12]],
        "graphs": dts,
        "now": now,
    }


def _live_state(s: Session, model_id: int, now: int) -> str:
    last = s.exec(select(TelemetrySample).where(
        TelemetrySample.model_id == model_id).order_by(TelemetrySample.ts.desc())
        .limit(1)).first()
    if last and now - last.ts < 60_000:
        return last.state
    return "UNLOADED"


def range_detail(s: Session, model_id: int, range_key: str,
                 samples: list[TelemetrySample], start: int, now: int) -> dict:
    """Adaptive-bucketed time series for model detail graphs."""
    if range_key == "session":
        bucket = RANGE_BUCKETS["1m"]
        start = now - 60_000
        samples = [r for r in samples if r.ts >= start]
    else:
        bucket = RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
    span = max(1, now - start)
    nb = max(2, int(span / 1000 / bucket))
    series = {
        "gen_tps": [None] * nb, "prompt_tps": [None] * nb,
        "tokens": [0] * nb, "context": [None] * nb,
        "mtp_acc": [None] * nb, "inference_s": [0] * nb,
        "gpu_util": [None] * nb, "vram_mb": [None] * nb,
    }
    sums: dict[str, list] = {k: [[0.0, 0] for _ in range(nb)] for k in
                             ("gen_tps", "prompt_tps", "context", "mtp_acc", "gpu_util", "vram_mb")}
    for r in samples:
        i = min(nb - 1, max(0, int((r.ts - start) / 1000 / bucket)))
        for key, attr in (("gen_tps", "gen_tps"), ("prompt_tps", "prompt_tps"),
                          ("context", "context_used"), ("mtp_acc", "mtp_acc"),
                          ("gpu_util", "gpu_util"), ("vram_mb", "vram_used_mb")):
            v = getattr(r, attr)
            if v is not None:
                sums[key][i][0] += v
                sums[key][i][1] += 1
    for key, lst in sums.items():
        for i, (tot, n) in enumerate(lst):
            series[key][i] = round(tot / n, 1) if n else None
    prev = None
    for r in samples:
        i = min(nb - 1, max(0, int((r.ts - start) / 1000 / bucket)))
        if prev is not None:
            d = _delta(r, prev, "tokens_total")
            if d == 0:
                d = _delta(r, prev, "prompt_total") + _delta(r, prev, "gen_total")
            series["tokens"][i] += int(d)
            phase_s = _phase_delta(
                r, prev, "prompt_total", "prompt_seconds_total", "prompt_tps")
            phase_s += _phase_delta(
                r, prev, "gen_total", "gen_seconds_total", "gen_tps")
            series["inference_s"][i] += round(phase_s)
        prev = r
    labels = [datetime.fromtimestamp((start + i * bucket * 1000) / 1000.0).strftime(
        "%H:%M" if bucket < 3600 else "%m-%d %H:%M") for i in range(nb)]
    return {"labels": labels, "series": {k: v for k, v in series.items()}}


def _config_out(c: ModelConfig) -> dict:
    import json as _json
    try:
        payload = _json.loads(c.payload or "{}")
    except ValueError:
        payload = {}
    return {
        "id": c.id, "fingerprint": c.fingerprint, "created_at": c.created_at,
        "context": c.context, "kv_cache_k": c.kv_cache_k, "kv_cache_v": c.kv_cache_v,
        "flash_attn": c.flash_attn, "parallel": c.parallel, "split_mode": c.split_mode,
        "tensor_split": c.tensor_split, "gpu_layers": c.gpu_layers,
        "cpu_moe": c.cpu_moe, "threads": c.threads, "batch": c.batch,
        "ubatch": c.ubatch, "reasoning": c.reasoning,
        "reasoning_effort": c.reasoning_effort,
        "reasoning_preserve": c.reasoning_preserve, "mmproj": c.mmproj,
        "mtp_enabled": c.mtp_enabled, "mtp_model": c.mtp_model,
        "speculative": c.speculative, "payload": payload,
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def sessions_page(s: Session, provider_id: Optional[int], model_id: Optional[int],
                  quant: Optional[str], mtp: Optional[str], reasoning: Optional[str],
                  range_key: str) -> dict:
    now = now_ms()
    start = range_start_ms(range_key, now)
    q = select(SessionRow).where(SessionRow.start_at <= now)
    q = q.where((SessionRow.end_at.is_(None) | (SessionRow.end_at >= start)))
    if provider_id:
        q = q.where(SessionRow.provider_id == provider_id)
    if model_id:
        q = q.where(SessionRow.model_id == model_id)
    rows = list(s.exec(q.order_by(SessionRow.start_at.desc()).limit(500)).all())
    models = {m.id: m for m in s.exec(select(Model)).all()}
    provs = {p.id: p.name for p in s.exec(select(Provider)).all()}
    cfgs = {c.id: c for c in s.exec(select(ModelConfig)).all()}
    out = []
    for x in rows:
        m = models.get(x.model_id)
        if quant and (not m or m.quant != quant):
            continue
        cfg = cfgs.get(x.config_id) if x.config_id else None
        if mtp in ("on", "off"):
            want = mtp == "on"
            if x.mtp_enabled is None:
                continue
            if bool(x.mtp_enabled) != want:
                continue
        if reasoning and (not cfg or cfg.reasoning_effort != reasoning):
            continue
        out.append({
            "id": x.id, "start": x.start_at, "end": x.end_at,
            "provider": provs.get(x.provider_id),
            "model": m.name if m else None, "model_id": x.model_id,
            "color": m.color if m else None, "quant": m.quant if m else None,
            "duration_s": x.duration_s,
            "prompt_tokens": round(x.prompt_tokens or 0),
            "gen_tokens": round(x.gen_tokens or 0),
            "avg_gen_tps": round(x.avg_gen_tps, 1) if x.avg_gen_tps else None,
            "prompt_tps": round(x.prompt_tps, 1) if x.prompt_tps else None,
            "mtp_acc": round(x.mtp_acc, 1) if x.mtp_acc is not None else None,
            "mtp_enabled": x.mtp_enabled,
            "context_max": x.context_max,
            "status": x.status,
        })
    return {"sessions": out, "now": now}


def session_detail(s: Session, session_id: int) -> dict:
    x = s.get(SessionRow, session_id)
    if x is None:
        return {}
    m = s.get(Model, x.model_id) if x.model_id else None
    p = s.get(Provider, x.provider_id)
    cfg = s.get(ModelConfig, x.config_id) if x.config_id else None
    samples = list(s.exec(select(TelemetrySample).where(
        TelemetrySample.session_id == session_id).order_by(TelemetrySample.ts)).all())
    bucket = 2
    start = x.start_at
    end = x.end_at or now_ms()
    nb = max(2, int((end - start) / 1000 / bucket))
    series = {k: ([0] * nb if k == "tokens" else [None] * nb) for k in
              ("gen_tps", "prompt_tps", "context", "mtp_acc", "gpu_util", "vram_mb", "tokens")}
    sums = {k: [[0.0, 0] for _ in range(nb)] for k in
            ("gen_tps", "prompt_tps", "context", "mtp_acc", "gpu_util", "vram_mb")}
    for r in samples:
        i = min(nb - 1, max(0, int((r.ts - start) / 1000 / bucket)))
        for key, attr in (("gen_tps", "gen_tps"), ("prompt_tps", "prompt_tps"),
                          ("context", "context_used"), ("mtp_acc", "mtp_acc"),
                          ("gpu_util", "gpu_util"), ("vram_mb", "vram_used_mb")):
            v = getattr(r, attr)
            if v is not None:
                sums[key][i][0] += v
                sums[key][i][1] += 1
    for key, lst in sums.items():
        for i, (tot, n) in enumerate(lst):
            series[key][i] = round(tot / n, 1) if n else None
    prev = None
    for r in samples:
        i = min(nb - 1, max(0, int((r.ts - start) / 1000 / bucket)))
        if prev is not None:
            d = _delta(r, prev, "tokens_total")
            if d == 0:
                d = _delta(r, prev, "prompt_total") + _delta(r, prev, "gen_total")
            series["tokens"][i] += int(d)
        prev = r
    labels = [datetime.fromtimestamp((start + i * bucket * 1000) / 1000.0).strftime("%H:%M:%S")
              for i in range(nb)]
    gpu_data = _gpu_series(
        _gpu_rows(s, x.provider_id, start, end, session_id=session_id),
        start, end, bucket,
    )
    return {
        "session": {
            "id": x.id, "start": x.start_at, "end": x.end_at,
            "duration_s": x.duration_s, "status": x.status,
            "provider": p.name if p else None,
            "model": m.name if m else None, "color": m.color if m else None,
            "model_id": x.model_id, "quant": m.quant if m else None,
            "prompt_tokens": round(x.prompt_tokens or 0),
            "gen_tokens": round(x.gen_tokens or 0),
            "total_tokens": round(x.total_tokens or 0),
            "prompt_time_s": round(x.prompt_time_s, 1),
            "gen_time_s": round(x.gen_time_s, 1),
            "prompt_tps": round(x.prompt_tps, 1) if x.prompt_tps else None,
            "avg_gen_tps": round(x.avg_gen_tps, 1) if x.avg_gen_tps else None,
            "peak_gen_tps": round(x.peak_gen_tps, 1) if x.peak_gen_tps else None,
            "peak_prompt_tps": round(x.peak_prompt_tps, 1) if x.peak_prompt_tps else None,
            "ttft_s": round(x.ttft_s, 2) if x.ttft_s is not None else None,
            "context_max": x.context_max,
            "mtp_enabled": x.mtp_enabled, "mtp_acc": round(x.mtp_acc, 1) if x.mtp_acc is not None else None,
            "mtp_proposed": round(x.mtp_proposed or 0) if x.mtp_proposed else None,
            "mtp_accepted": round(x.mtp_accepted or 0) if x.mtp_accepted else None,
            "gpu_util_avg": round(x.gpu_util_avg, 1) if x.gpu_util_avg else None,
            "vram_used_mb": round(x.vram_used_mb) if x.vram_used_mb else None,
            "ram_used_mb": round(x.ram_used_mb) if x.ram_used_mb else None,
            "power_w": round(x.power_w, 1) if x.power_w else None,
            "gpus": [{k: gpu[k] for k in
                      ("key", "index", "uuid", "name", "label", "color",
                       "pcie", "vram_total_mb", "summary")}
                     for gpu in gpu_data],
        },
        "config": _config_out(cfg) if cfg else None,
        "graphs": {"labels": labels, "series": series, "gpus": gpu_data,
                   "span_s": round((end - start) / 1000.0)},
        "now": now_ms(),
    }


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
def compare(s: Session, ids: list[int]) -> dict:
    out = []
    models = {m.id: m for m in s.exec(select(Model)).all()}
    provs = {p.id: p for p in s.exec(select(Provider)).all()}
    cfgs = {c.id: c for c in s.exec(select(ModelConfig)).all()}
    for sid in ids[:5]:
        x = s.get(SessionRow, sid)
        if x is None:
            continue
        m = models.get(x.model_id) if x.model_id else None
        p = provs.get(x.provider_id)
        cfg = cfgs.get(x.config_id) if x.config_id else None
        b = _latest_build(s, x.provider_id)
        end = x.end_at or now_ms()
        gpu_data = _gpu_series(
            _gpu_rows(s, x.provider_id, x.start_at, end, session_id=x.id),
            x.start_at, end, 2,
        )
        out.append({
            "id": x.id, "start": x.start_at,
            "model": m.name if m else None, "color": m.color if m else None,
            "quant": m.quant if m else None,
            "provider": p.name if p else None,
            "duration_s": x.duration_s,
            "prompt_tokens": round(x.prompt_tokens or 0),
            "gen_tokens": round(x.gen_tokens or 0),
            "avg_gen_tps": round(x.avg_gen_tps, 1) if x.avg_gen_tps else None,
            "peak_gen_tps": round(x.peak_gen_tps, 1) if x.peak_gen_tps else None,
            "prompt_tps": round(x.prompt_tps, 1) if x.prompt_tps else None,
            "context_max": x.context_max,
            "mtp_enabled": x.mtp_enabled,
            "mtp_model": cfg.mtp_model if cfg else None,
            "mtp_acc": round(x.mtp_acc, 1) if x.mtp_acc is not None else None,
            "kv_cache": f"{cfg.kv_cache_k or '-'}/{cfg.kv_cache_v or '-'}" if cfg else None,
            "reasoning_effort": cfg.reasoning_effort if cfg else None,
            "split_mode": cfg.split_mode if cfg else None,
            "vram_used_mb": round(x.vram_used_mb) if x.vram_used_mb else None,
            "gpu_util_avg": round(x.gpu_util_avg, 1) if x.gpu_util_avg else None,
            "gpus": [{"index": gpu["index"], "label": gpu["label"],
                      "color": gpu["color"], "summary": gpu["summary"]}
                     for gpu in gpu_data],
            "build": _build_str(b),
        })
    return {"sessions": out}


def compare_candidates(s: Session, limit: int = 30) -> list[dict]:
    rows = list(s.exec(select(SessionRow).order_by(SessionRow.start_at.desc())
                       .limit(limit)).all())
    models = {m.id: m for m in s.exec(select(Model)).all()}
    out = []
    for x in rows:
        m = models.get(x.model_id) if x.model_id else None
        out.append({
            "id": x.id, "start": x.start_at,
            "model": m.name if m else None, "color": m.color if m else None,
            "quant": m.quant if m else None,
            "gen_tokens": round(x.gen_tokens or 0),
            "avg_gen_tps": round(x.avg_gen_tps, 1) if x.avg_gen_tps else None,
            "duration_s": x.duration_s,
            "mtp_enabled": x.mtp_enabled,
            "status": x.status,
        })
    return out


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------
def hardware(s: Session, provider_id: Optional[int] = None) -> dict:
    provs = list(s.exec(select(Provider)).all())
    if provider_id:
        provs = [p for p in provs if p.id == provider_id]
    out = []
    now = now_ms()
    for p in provs:
        hw = s.exec(select(HardwareInfo).where(
            HardwareInfo.provider_id == p.id).order_by(HardwareInfo.id.desc())).first()
        b = _latest_build(s, p.id)
        h1 = now - 3600_000
        samples = fetch_samples(s, [p.id], h1)
        bucket = 30
        nb = max(2, 3600 // bucket)
        series = {k: [None] * nb for k in ("gpu_util", "vram_mb", "gpu_temp", "gpu_power",
                                           "cpu_pct", "ram_mb")}
        sums = {k: [[0.0, 0] for _ in range(nb)] for k in series}
        for r in samples:
            i = min(nb - 1, max(0, int((r.ts - h1) / 1000 / bucket)))
            for key, attr in (("gpu_util", "gpu_util"), ("vram_mb", "vram_used_mb"),
                              ("gpu_temp", "gpu_temp"), ("gpu_power", "gpu_power_w"),
                              ("cpu_pct", "cpu_pct"), ("ram_mb", "ram_used_mb")):
                v = getattr(r, attr)
                if v is not None:
                    sums[key][i][0] += v
                    sums[key][i][1] += 1
        for key, lst in sums.items():
            for i, (tot, n) in enumerate(lst):
                series[key][i] = round(tot / n, 1) if n else None
        labels = [datetime.fromtimestamp((h1 + i * bucket * 1000) / 1000.0).strftime("%H:%M")
                  for i in range(nb)]
        import json as _json
        gpus = []
        if hw:
            try:
                gpus = _json.loads(hw.gpus or "[]")
            except ValueError:
                gpus = []
        gpu_data = _gpu_series(_gpu_rows(s, p.id, h1, now), h1, now, bucket)
        latest_by_index = {gpu["index"]: gpu for gpu in gpu_data}
        for fallback_index, gpu in enumerate(gpus):
            index = int(gpu.get("index", fallback_index))
            live = latest_by_index.get(index)
            gpu["index"] = index
            gpu["uuid"] = gpu.get("uuid") or (live.get("uuid") if live else None)
            gpu["color"] = live.get("color") if live else _gpu_color(
                gpu.get("uuid") or f"index:{index}")
            if live:
                current = live["current"]
                gpu.update({"util": current["util"], "vram_used_mb": current["vram_mb"],
                            "temp_c": current["temp_c"], "power_w": current["power_w"]})
        out.append({
            "provider": p.name,
            "status": p.status,
            "hardware": {
                "hostname": hw.hostname, "os": hw.os_name, "kernel": hw.kernel,
                "cpu": hw.cpu_model, "cpu_threads": hw.cpu_threads,
                "ram_mb": hw.ram_mb, "gpus": gpus,
                "nvidia_driver": hw.nvidia_driver, "cuda": hw.cuda, "pcie": hw.pcie,
                "source": hw.source,
                "updated": _fmt_ago(hw.last_seen_at, now) if hw else None,
            } if hw else None,
            "build": {
                "version": b.version, "commit": b.commit,
                "docker_image": b.docker_image, "container_id": b.container_id,
                "source": "llama.cpp /props",
                "updated": _fmt_ago(b.last_seen_at, now) if b else None,
            } if b else None,
            "graphs": {"labels": labels, "series": series, "gpus": gpu_data},
        })
    return {"providers": out, "now": now}


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------
def status(s: Session) -> dict:
    now = now_ms()
    provs = []
    for p in s.exec(select(Provider).order_by(Provider.id)).all():
        avail = _telemetry_availability(s, p.id, now)
        agent_status = None
        if p.agent_url:
            try:
                import httpx
                httpx.get(p.agent_url.rstrip("/") + "/info", timeout=1.0)
                agent_status = "LIVE"
            except Exception:
                agent_status = "OFFLINE"
        b = _latest_build(s, p.id)
        provs.append({
            "name": p.name, "url": p.base_url, "status": p.status,
            "last_success_ago": _fmt_ago(p.last_success_at, now),
            "latency_ms": p.latency_ms, "last_error": p.last_error,
            "agent_status": agent_status, "agent_url": p.agent_url,
            "telemetry": avail,
            "build": _build_str(b),
        })
    from . import database as _db
    from . import app_state
    return {
        "providers": provs,
        "db_size_bytes": _db.db_size_bytes(),
        "db_path": _db.get_db_path(),
        "uptime_s": round(time.time() - getattr(app_state, "STARTED", time.time())),
        "now": now,
    }


def _telemetry_availability(s: Session, provider_id: int, now: int) -> dict:
    rows = list(s.exec(select(TelemetrySample).where(
        TelemetrySample.provider_id == provider_id,
        TelemetrySample.ts >= now - 300_000,
    ).order_by(TelemetrySample.ts.desc()).limit(60)).all())
    groups = {
        "counters": ("tokens_total", "prompt_total", "gen_total"),
        "speeds": ("gen_tps", "prompt_tps"),
        "context": ("context_used", "context_max"),
        "mtp": ("mtp_proposed_total", "mtp_accepted_total", "mtp_acc"),
        "gpu": ("gpu_util", "vram_used_mb"),
    }
    out = {}
    for g, attrs in groups.items():
        found = False
        for r in rows:
            if any(getattr(r, a) is not None for a in attrs):
                found = True
                break
        out[g] = found
    return out
