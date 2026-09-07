"""Aggregation queries feeding the dashboard JSON APIs."""
from __future__ import annotations

import re
import time
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple, Optional

from sqlalchemy import and_, bindparam, case, func, or_, text
from sqlmodel import Session, select

from . import database as db
from .models import (BuildInfo, GpuTelemetrySample, HardwareInfo, Model,
                      ModelConfig, ModelResidency, ModelUsageBucket, Provider,
                      SessionRow, TelemetrySample, now_ms)
from .pricing import compute_costs

from .settings import LLAMA_POLL_S, STALE_AFTER_S

LIVE_FRESH_MS = 10_000
FINALIZING_FRESH_MS = 15_000

# Session Detail chart resolution.  The chart used to be built at a fixed 2 s
# bucket regardless of how long the session ran, so the number of points grew
# without bound with *duration* -- not with the number of samples.  An
# unterminated session spanning 30 h produced 54,286 points per series (and the
# same again per GPU in _gpu_series), taking ~16 s of CPU to render.  Cap the
# point count and widen the bucket instead.  Sessions up to
# MAX_POINTS * MIN_BUCKET_S (10 min) are bucketed exactly as before.
SESSION_DETAIL_MAX_POINTS = 300
SESSION_DETAIL_MIN_BUCKET_S = 2


def session_detail_bucket_s(start_ms: int, end_ms: int) -> int:
    """Bucket width that keeps a session's series within the point cap."""
    span_s = max(1, int((end_ms - start_ms) / 1000))
    # ceil division without importing math
    return max(SESSION_DETAIL_MIN_BUCKET_S,
               -(-span_s // SESSION_DETAIL_MAX_POINTS))


def _bounded_pct(used: Optional[float], total: Optional[float]) -> Optional[float]:
    if used is None or total is None or total <= 0:
        return None
    return round(max(0.0, min(100.0, used / total * 100.0)), 1)


def _effective_session_status(row: SessionRow, now: int) -> str:
    if row.status == "ACTIVE":
        if row.live_seen_at is None:
            return "INTERRUPTED"
        return "ACTIVE" if now - row.live_seen_at <= LIVE_FRESH_MS else "INTERRUPTED"
    if row.status == "FINALIZING":
        if row.live_seen_at is not None and now - row.live_seen_at <= FINALIZING_FRESH_MS:
            return "FINALIZING"
        return "INCOMPLETE"
    return row.status


def _session_live(row: Optional[SessionRow], now: int) -> Optional[dict]:
    if row is None or _effective_session_status(row, now) not in ("ACTIVE", "FINALIZING"):
        return None
    context_pct = _bounded_pct(row.live_context, row.live_context_max)
    return {
        "processing": row.status == "ACTIVE",
        "slot_id": row.source_slot_id,
        "task_id": row.source_task_id,
        "prompt_tokens": round(row.live_prompt_tokens or 0),
        "gen_tokens": round(row.live_gen_tokens or 0),
        "context": row.live_context,
        "context_max": row.live_context_max,
        "context_pct": context_pct,
        "gen_tps": round(row.live_gen_tps, 1) if row.live_gen_tps is not None else None,
        "gen_tps_avg": (round(row.live_gen_tps_avg, 2)
                        if row.live_gen_tps_avg is not None else None),
        "gen_tps_3s": (round(row.live_gen_tps_3s, 2)
                       if row.live_gen_tps_3s is not None else None),
        "observed_at": row.live_seen_at,
        "provisional": True,
    }


def _model_live(s: Session, model_id: int, now: int) -> Optional[dict]:
    rows = list(s.exec(select(SessionRow).where(
        SessionRow.model_id == model_id,
        SessionRow.status.in_(["ACTIVE", "FINALIZING"]),
    ).order_by(SessionRow.live_seen_at.desc()).limit(8)).all())
    live = [_session_live(row, now) for row in rows]
    live = [item for item in live if item]
    if not live:
        return None
    if len(live) == 1:
        return live[0]
    return {"processing": any(item["processing"] for item in live),
            "tasks": live, "provisional": True}


def _runtime_from_session(s: Session, row: SessionRow, now: int,
                          status: str) -> dict:
    """Expose a sanitized slot snapshot without treating it as completed work."""
    model = s.get(Model, row.model_id) if row.model_id else None
    provider = s.get(Provider, row.provider_id)
    cfg = s.get(ModelConfig, row.config_id) if row.config_id else None
    context_max = row.live_context_max or (cfg.context if cfg else None)
    context_pct = _bounded_pct(row.live_context, context_max)
    costs = compute_costs(model, row.live_prompt_tokens or 0,
                          row.live_gen_tokens or 0) if model else {
        "input_price_per_million": None, "output_price_per_million": None,
        "input_cost": "0.00000000", "output_cost": "0.00000000",
        "total_cost": "0.00000000"}
    return {
        "status": status,
        "processing": status == "LIVE",
        "source": "slots",
        "snapshot": "LIVE SLOT" if status in ("LIVE", "FINALIZING") else "LAST SLOT",
        "session_id": row.id,
        "session_started_at": row.start_at,
        "model_id": row.model_id,
        "model": model.name if model else None,
        "provider": provider.name if provider else None,
        "slot_id": row.source_slot_id,
        "task_id": row.source_task_id,
        "prompt_tokens": round(row.live_prompt_tokens or 0),
        "gen_tokens": round(row.live_gen_tokens or 0),
        "gen_tps": round(row.live_gen_tps, 1) if row.live_gen_tps is not None else None,
        "gen_tps_avg": (round(row.live_gen_tps_avg, 2)
                        if row.live_gen_tps_avg is not None else None),
        "gen_tps_3s": (round(row.live_gen_tps_3s, 2)
                       if row.live_gen_tps_3s is not None else None),
        "context": row.live_context,
        "context_max": context_max,
        "context_pct": context_pct,
        "observed_at": row.live_seen_at,
        "age_s": (round(max(0, now - row.live_seen_at) / 1000.0, 1)
                  if row.live_seen_at else None),
        "provisional": True,
        "input_price_per_million": costs["input_price_per_million"],
        "output_price_per_million": costs["output_price_per_million"],
        "input_cost": costs["input_cost"],
        "output_cost": costs["output_cost"],
        "total_cost": costs["total_cost"],
    }


def _active_models(s: Session, now: int,
                   model_ids: Optional[list[int]] = None) -> dict[int, dict]:
    q = select(SessionRow).where(
        SessionRow.status.in_(["ACTIVE", "FINALIZING"]),
        or_(
            and_(SessionRow.status == "ACTIVE",
                 SessionRow.live_seen_at >= now - LIVE_FRESH_MS),
            and_(SessionRow.status == "FINALIZING",
                 SessionRow.live_seen_at >= now - FINALIZING_FRESH_MS),
        ),
    )
    if model_ids:
        q = q.where(SessionRow.model_id.in_(model_ids))
    rows = list(s.exec(q.order_by(SessionRow.live_seen_at.desc())).all())
    out: dict[int, dict] = {}
    for row in rows:
        if not row.model_id:
            continue
        status = "LIVE" if row.status == "ACTIVE" else "FINALIZING"
        item = out.setdefault(row.model_id, {
            "model_id": row.model_id,
            "status": status,
            "rank": 2 if status == "LIVE" else 1,
            "task_count": 0,
            "latest_seen": row.live_seen_at or 0,
            "realtime": None,
            "_runtime_rank": 0,
            "_runtime_seen": 0,
        })
        item["task_count"] += 1
        rank = 2 if status == "LIVE" else 1
        if rank > item["rank"]:
            item["rank"] = rank
            item["status"] = status
        seen = row.live_seen_at or 0
        item["latest_seen"] = max(item["latest_seen"], seen)
        if (rank, seen) >= (item["_runtime_rank"], item["_runtime_seen"]):
            item["_runtime_rank"] = rank
            item["_runtime_seen"] = seen
            item["realtime"] = _runtime_from_session(s, row, now, status)
    for item in out.values():
        item.pop("_runtime_rank", None)
        item.pop("_runtime_seen", None)
    return out


def realtime_snapshot(s: Session) -> dict:
    """Compact collector-backed state for the shared SSE refresh loop.

    This deliberately touches only providers and current session live fields.
    Historical telemetry and provider endpoints are not part of the realtime
    path, so one snapshot can safely serve every connected browser.
    """
    now = now_ms()
    providers = list(s.exec(select(Provider).order_by(Provider.id)).all())
    active_models = list(_active_models(s, now).values())
    by_model = {item["model_id"]: item for item in active_models}
    models = ({model.id: model for model in s.exec(select(Model).where(
        Model.id.in_(list(by_model))
    )).all()} if by_model else {})
    for item in active_models:
        model = models.get(item["model_id"])
        if model and item.get("realtime"):
            item["realtime"]["color"] = model.color
            item["realtime"].update(compute_costs(
                model, item["realtime"].get("prompt_tokens", 0.0),
                item["realtime"].get("gen_tokens", 0.0)))

    current = None
    if active_models:
        chosen = max(active_models, key=lambda item: (
            item["rank"], item["task_count"], item["latest_seen"], item["model_id"]
        ))
        live = chosen["realtime"]
        model = models.get(chosen["model_id"])
        current = {
            "provider": live.get("provider"), "model": live.get("model"),
            "model_id": chosen["model_id"], "color": model.color if model else "#4b8de8",
            "state": "GENERATING" if live.get("gen_tokens", 0) else "PROMPTING",
            "gen_tps": live.get("gen_tps"), "prompt_tps": None,
            "context_used": live.get("context"), "context_max": live.get("context_max"),
            "context_pct": live.get("context_pct"), "mtp_acc": None,
            "session_elapsed_s": round(max(0, now - live.get("session_started_at", now)) / 1000),
            "live": live,
        }
        if model:
            current.update(compute_costs(model, live.get("prompt_tokens", 0.0),
                                         live.get("gen_tokens", 0.0)))
    return {
        "providers": [{"id": provider.id, "name": provider.name,
                       "status": effective_provider_status(provider, now),
                       "latency_ms": provider.latency_ms,
                       "enabled": provider.enabled} for provider in providers],
        "current": current, "active_models": active_models, "now": now,
    }


def effective_provider_status(provider: Optional[Provider], now: int) -> str:
    """Provider status aged against its last successful poll.

    ``Provider.status`` is only ever written by the collector.  When the
    collector stops writing at all -- process killed, crashed, or demoted to
    standby by the single-writer lease -- the column keeps whatever it last
    said.  A provider last polled a day ago therefore still reports LIVE, which
    is exactly how a dead collector stays invisible on the dashboard.  Derive
    staleness from ``last_success_at`` at read time instead of trusting the
    stored value unconditionally.

    The grace window follows the provider's own poll interval (a deliberately
    slow provider must not be reported stale between two healthy polls) with
    ``STALE_AFTER_S`` as the floor, mirroring the collector's own rule in
    ``Collector._poll_fail``.
    """
    stored = (provider.status if provider else None) or "OFFLINE"
    if provider is None or not provider.enabled:
        return stored
    if stored in ("STALE", "OFFLINE"):
        # The collector already knows this provider is unhealthy.
        return stored
    if not provider.last_success_at:
        return stored
    interval = float(provider.poll_interval_s or LLAMA_POLL_S)
    grace_ms = max(STALE_AFTER_S, interval * 3.0) * 1000.0
    age_ms = now - provider.last_success_at
    if age_ms <= grace_ms:
        return stored
    return "STALE" if age_ms <= grace_ms * 3.0 else "OFFLINE"


def _provider_runtime_status(provider: Optional[Provider], sample: Optional[TelemetrySample],
                             now: int) -> str:
    status = effective_provider_status(provider, now) if provider else None
    if status in ("STALE", "OFFLINE"):
        return status
    if sample and now - sample.ts <= LIVE_FRESH_MS * 2 and sample.state != "UNLOADED":
        return "IDLE"
    return "LAST SEEN"


def _selected_realtime(s: Session, models: dict[int, Model], now: int) -> dict:
    model_ids = list(models)
    if not model_ids:
        return {"status": "NO DATA", "processing": False, "source": None,
                "snapshot": "NO DATA", "observed_at": None, "provisional": False}
    active = _active_models(s, now, model_ids)
    if active:
        chosen = max(active.values(), key=lambda item: (
            item["rank"], item["task_count"], item["latest_seen"], item["model_id"]))
        return chosen["realtime"]

    slot_row = s.exec(select(SessionRow).where(
        SessionRow.model_id.in_(model_ids),
        SessionRow.live_seen_at.is_not(None),
    ).order_by(SessionRow.live_seen_at.desc()).limit(1)).first()
    if slot_row is not None:
        model = models.get(slot_row.model_id)
        provider = s.get(Provider, model.provider_id) if model else None
        sample = s.exec(select(TelemetrySample).where(
            TelemetrySample.model_id == slot_row.model_id,
        ).order_by(TelemetrySample.ts.desc()).limit(1)).first()
        status = _provider_runtime_status(provider, sample, now)
        return _runtime_from_session(s, slot_row, now, status)

    latest_sample = s.exec(select(TelemetrySample).where(
        TelemetrySample.model_id.in_(model_ids),
    ).order_by(TelemetrySample.ts.desc()).limit(1)).first()
    latest_session = s.exec(select(SessionRow).where(
        SessionRow.model_id.in_(model_ids),
    ).order_by(SessionRow.start_at.desc()).limit(1)).first()
    model_id = (latest_sample.model_id if latest_sample and latest_sample.model_id else
                (latest_session.model_id if latest_session else None))
    model = models.get(model_id) if model_id else None
    provider = s.get(Provider, model.provider_id) if model else None
    history_session = latest_session if latest_session and latest_session.model_id == model_id else None
    if history_session is None and model_id:
        history_session = s.exec(select(SessionRow).where(
            SessionRow.model_id == model_id,
        ).order_by(SessionRow.start_at.desc()).limit(1)).first()
    cfg = None
    if history_session and history_session.config_id:
        cfg = s.get(ModelConfig, history_session.config_id)
    context = latest_sample.context_used if latest_sample else None
    context_max = ((latest_sample.context_max if latest_sample else None) or
                   (cfg.context if cfg else None))
    context_pct = _bounded_pct(context, context_max)
    observed_at = (latest_sample.ts if latest_sample else
                   ((history_session.end_at or history_session.start_at)
                    if history_session else None))
    if model is None:
        return {"status": "NO DATA", "processing": False, "source": None,
                "snapshot": "NO DATA", "observed_at": None, "provisional": False}
    _sess_costs = compute_costs(
        model,
        (history_session.prompt_tokens or 0) if history_session else 0,
        (history_session.gen_tokens or 0) if history_session else 0,
    )
    return {
        "status": _provider_runtime_status(provider, latest_sample, now),
        "processing": False,
        "source": "metrics",
        "snapshot": "LAST METRICS",
        "session_id": history_session.id if history_session else None,
        "session_started_at": history_session.start_at if history_session else None,
        "model_id": model.id,
        "model": model.name,
        "provider": provider.name if provider else None,
        "slot_id": None,
        "task_id": None,
        "prompt_tokens": round(history_session.prompt_tokens or 0) if history_session else None,
        "gen_tokens": round(history_session.gen_tokens or 0) if history_session else None,
        "gen_tps": (round(history_session.avg_gen_tps, 1)
                    if history_session and history_session.avg_gen_tps is not None else None),
        "gen_tps_avg": (round(history_session.avg_gen_tps, 2)
                        if history_session and history_session.avg_gen_tps is not None else None),
        "gen_tps_3s": None,
        "context": context,
        "context_max": context_max,
        "context_pct": context_pct,
        "observed_at": observed_at,
        "age_s": (round(max(0, now - observed_at) / 1000.0, 1)
                  if observed_at else None),
        "provisional": False,
        "input_price_per_million": _sess_costs["input_price_per_million"],
        "output_price_per_million": _sess_costs["output_price_per_million"],
        "input_cost": _sess_costs["input_cost"],
        "output_cost": _sess_costs["output_cost"],
        "total_cost": _sess_costs["total_cost"],
    }


def _runtime_history(s: Session, realtime: dict,
                     max_points: int = 120) -> Optional[dict]:
    """Return a bounded, gap-preserving history for one observed session."""
    session_id = realtime.get("session_id")
    if not session_id or max_points < 2:
        return None
    rows = list(s.exec(select(
        TelemetrySample.ts,
        TelemetrySample.gen_tps,
        TelemetrySample.context_used,
    ).where(
        TelemetrySample.session_id == session_id,
    ).order_by(TelemetrySample.ts)).all())
    points = [(row.ts, row.gen_tps, row.context_used) for row in rows]
    observed_at = realtime.get("observed_at")
    if observed_at:
        live_point = (observed_at, realtime.get("gen_tps"), realtime.get("context"))
        if points and points[-1][0] == observed_at:
            previous = points[-1]
            points[-1] = (
                observed_at,
                live_point[1] if live_point[1] is not None else previous[1],
                live_point[2] if live_point[2] is not None else previous[2],
            )
        elif not points or observed_at > points[-1][0]:
            points.append(live_point)
    if not points:
        return None
    if len(points) <= max_points:
        return {
            "timestamps": [point[0] for point in points],
            "gen_tps": [point[1] for point in points],
            "context": [point[2] for point in points],
        }

    latest = points[-1]
    historical = points[:-1]
    history_limit = max_points - 1
    start = realtime.get("session_started_at") or historical[0][0]
    end = historical[-1][0]
    span = max(1, end - start + 1)
    bucket_ms = max(1, (span + history_limit - 1) // history_limit)
    buckets = [{"ts": None, "gen": 0.0, "gen_n": 0,
                "context": 0.0, "context_n": 0}
               for _ in range(history_limit)]
    for ts, gen_tps, context in historical:
        index = min(history_limit - 1, max(0, (ts - start) // bucket_ms))
        bucket = buckets[index]
        bucket["ts"] = ts
        if gen_tps is not None:
            bucket["gen"] += gen_tps
            bucket["gen_n"] += 1
        if context is not None:
            bucket["context"] += context
            bucket["context_n"] += 1
    last_index = max(index for index, bucket in enumerate(buckets) if bucket["ts"] is not None)
    buckets = buckets[:last_index + 1]
    timestamps = [bucket["ts"] if bucket["ts"] is not None
                  else start + index * bucket_ms
                  for index, bucket in enumerate(buckets)]
    gen_tps = [round(bucket["gen"] / bucket["gen_n"], 1)
               if bucket["gen_n"] else None for bucket in buckets]
    context = [round(bucket["context"] / bucket["context_n"], 1)
               if bucket["context_n"] else None for bucket in buckets]
    timestamps.append(latest[0])
    gen_tps.append(latest[1])
    context.append(latest[2])
    return {"timestamps": timestamps, "gen_tps": gen_tps, "context": context}
from .settings import (FAMILY_TAG_TOKENS, MODEL_COLORS, QUANT_TOKENS,
                       RANGE_BUCKETS, TELEMETRY_GROUPS)

DT_CAP_S = 300.0
SPARK_BUCKETS = 24

# Upper time bound replicating the historical unbounded sample fetch on the
# Model Detail path: future-stamped rows participate in chains and tails.
_UNBOUNDED_MS = 2**63 - 1
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


class _GpuSeriesRow(NamedTuple):
    """Attribute-shaped projection matching _gpu_series field access."""

    gpu_key: str
    gpu_index: int
    gpu_uuid: Optional[str]
    name: Optional[str]
    util: Optional[float]
    vram_used_mb: Optional[float]
    vram_total_mb: Optional[float]
    temp_c: Optional[float]
    power_w: Optional[float]
    pcie: Optional[str]
    ts: int


def _sqlite_has_json1(s: Session) -> bool:
    """True when the underlying SQLite library supports the JSON1 functions."""
    try:
        s.exec(text("SELECT json_valid('[]')")).first()
        return True
    except Exception:
        return False


def _gpu_rows_for_model(s: Session, provider_id: int, start: int, end: int,
                        model_id: int) -> list:
    """Bounded GPU-row projection for Model Detail.

    Same result as ``_gpu_rows(..., model_id=...)`` (exact integer
    membership in the JSON id array) but the membership test runs in SQL
    via SQLite json_each and only the columns ``_gpu_series`` consumes are
    materialized.  Falls back to the plain Python-filtered loader when the
    SQLite build lacks JSON1.
    """
    if not _sqlite_has_json1(s):
        return _gpu_rows(s, provider_id, start, end, model_id=model_id)
    # Guard the SQL filter against malformed/legacy JSON stored before
    # validation existed: rows that fail json parsing could crash the
    # query.  Each candidate row must still satisfy the exact integer
    # membership test, so the guard never widens the result set.
    q = (select(GpuTelemetrySample.gpu_key, GpuTelemetrySample.gpu_index,
                GpuTelemetrySample.gpu_uuid, GpuTelemetrySample.name,
                GpuTelemetrySample.util, GpuTelemetrySample.vram_used_mb,
                GpuTelemetrySample.vram_total_mb, GpuTelemetrySample.temp_c,
                GpuTelemetrySample.power_w, GpuTelemetrySample.pcie,
                GpuTelemetrySample.ts)
         .where(GpuTelemetrySample.provider_id == provider_id,
                GpuTelemetrySample.ts >= start,
                GpuTelemetrySample.ts <= end,
                text("EXISTS (SELECT 1 FROM json_each("
                     "CASE WHEN json_valid(gputelemetrysample"
                     ".active_model_ids) THEN gputelemetrysample"
                     ".active_model_ids ELSE '[]' END, '$') "
                     "WHERE json_each.value = :target)")
                .bindparams(bindparam("target", model_id)))
         .with_hint(GpuTelemetrySample, "INDEXED BY ix_gputelemetrysample_ts"))
    rows = s.exec(q.order_by(GpuTelemetrySample.ts)).all()
    return [_GpuSeriesRow(*row) for row in rows]


def _gpu_rows_for_session(s: Session, provider_id: int, start: int, end: int,
                          session_id: int) -> list:
    """Bounded GPU-row projection for Session Detail.

    Same result as ``_gpu_rows(..., session_id=...)`` -- exact integer
    membership in the JSON id array -- but the membership test runs in SQL and
    only the columns ``_gpu_series`` consumes are materialized.  The Python
    path hydrated a full ORM entity per row and json-decoded each one, which on
    a session left open for days meant tens of thousands of entities: a
    production session open for 195 h loaded 70,394 of them and spent 32 s
    doing it.  Mirrors :func:`_gpu_rows_for_model`, including its JSON1
    fallback and its guard against malformed legacy JSON.
    """
    if not _sqlite_has_json1(s):
        return _gpu_rows(s, provider_id, start, end, session_id=session_id)
    q = (select(GpuTelemetrySample.gpu_key, GpuTelemetrySample.gpu_index,
                GpuTelemetrySample.gpu_uuid, GpuTelemetrySample.name,
                GpuTelemetrySample.util, GpuTelemetrySample.vram_used_mb,
                GpuTelemetrySample.vram_total_mb, GpuTelemetrySample.temp_c,
                GpuTelemetrySample.power_w, GpuTelemetrySample.pcie,
                GpuTelemetrySample.ts)
         .where(GpuTelemetrySample.provider_id == provider_id,
                GpuTelemetrySample.ts >= start,
                GpuTelemetrySample.ts <= end,
                text("EXISTS (SELECT 1 FROM json_each("
                     "CASE WHEN json_valid(gputelemetrysample"
                     ".active_session_ids) THEN gputelemetrysample"
                     ".active_session_ids ELSE '[]' END, '$') "
                     "WHERE json_each.value = :target)")
                .bindparams(bindparam("target", session_id)))
         .with_hint(GpuTelemetrySample, "INDEXED BY ix_gputelemetrysample_ts"))
    rows = s.exec(q.order_by(GpuTelemetrySample.ts)).all()
    return [_GpuSeriesRow(*row) for row in rows]


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


# Model Detail's GPU chart is per-bucket averages, but it was built by pulling
# every GPU sample in the window into Python and bucketing them there: a 7 day
# range meant 34,078 rows fetched and looped over to produce 168 points per
# GPU, about 4.4 s of the endpoint.  SQLite can do the bucketing, so only the
# points that are actually drawn cross the boundary.
#
# Deliberately a separate function rather than a change to _gpu_series, which
# four other surfaces share.  It returns the identical structure; the
# equivalence is asserted against the Python path in test_gpu_series_sql.py.
# gpu_index, bucket_index, then four metrics as (sum, count): latest_ts lands
# at offset 10, and the bare columns of its row follow it.
_LATEST_TS = 10


def _gpu_series_sql_for_model(s: Session, provider_id: int, model_id: int,
                              start: int, end: int, bucket_s: int) -> list[dict]:
    if not _sqlite_has_json1(s):
        # Same fallback contract as _gpu_rows_for_model.
        return _gpu_series(_gpu_rows(s, provider_id, start, end,
                                     model_id=model_id), start, end, bucket_s)
    nb = max(2, int(max(1, end - start) / 1000 / bucket_s))
    member = """
        EXISTS (SELECT 1 FROM json_each(
            CASE WHEN json_valid(active_model_ids)
                 THEN active_model_ids ELSE '[]' END, '$')
        WHERE json_each.value = :model_id)
    """
    where = f"""
        WHERE provider_id = :provider_id
          AND ts >= :start AND ts <= :end
          AND {member}
    """
    params = {"provider_id": provider_id, "model_id": model_id,
              "start": start, "end": end, "nb": nb, "bucket_s": bucket_s}

    metrics_cols = (("util", "util"), ("vram_mb", "vram_used_mb"),
                    ("temp_c", "temp_c"), ("power_w", "power_w"))
    sums = ", ".join(
        f"SUM({col}) AS {name}_sum, "
        f"SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) AS {name}_n"
        for name, col in metrics_cols)
    # One pass. Alongside the bucket aggregates each group carries its own
    # latest sample: SQLite takes the remaining bare columns from the row that
    # produced the MAX, so the per-GPU latest is the bucket-latest with the
    # greatest ts -- which is the "last row wins" the Python path had. Asking
    # for it in a second query cost another full scan of the window (1.0 s of
    # a 7 day request) to re-derive what this scan already touched.
    bucketed = s.exec(text(f"""
        SELECT gpu_index,
               MIN(:nb - 1, MAX(0, CAST((ts - :start) / 1000.0
                   / :bucket_s AS INTEGER))) AS bucket_index,
               {sums},
               MAX(ts) AS latest_ts, gpu_key, gpu_uuid, name, pcie,
               vram_total_mb, util, vram_used_mb, temp_c, power_w
        FROM gputelemetrysample
        {where}
        GROUP BY gpu_index, bucket_index
    """), params=params).all()
    if not bucketed:
        return []
    latest: dict[int, tuple] = {}
    for row in bucketed:
        index = row[0]
        held = latest.get(index)
        if held is None or row[_LATEST_TS] > held[_LATEST_TS]:
            latest[index] = row

    per_gpu: dict[int, dict] = {}
    for row in bucketed:
        index, bucket_index = row[0], int(row[1])
        acc = per_gpu.setdefault(index, {
            name: [[0.0, 0] for _ in range(nb)] for name, _ in metrics_cols})
        for position, (name, _) in enumerate(metrics_cols):
            total, count = row[2 + position * 2], row[3 + position * 2]
            if count:
                acc[name][bucket_index][0] += total or 0.0
                acc[name][bucket_index][1] += count

    labels = [datetime.fromtimestamp((start + i * bucket_s * 1000) / 1000.0).strftime(
        "%H:%M:%S" if bucket_s < 60 else "%m-%d %H:%M") for i in range(nb)]
    out = []
    for index in sorted(per_gpu):
        acc = per_gpu[index]
        meta = latest[index]
        gpu_uuid, name_, pcie = meta[_LATEST_TS + 2], meta[_LATEST_TS + 3], meta[_LATEST_TS + 4]
        vram_total = meta[_LATEST_TS + 5]
        c_util, c_vram = meta[_LATEST_TS + 6], meta[_LATEST_TS + 7]
        c_temp, c_power = meta[_LATEST_TS + 8], meta[_LATEST_TS + 9]
        key = gpu_uuid or f"index:{index}"
        series = {metric: [round(total / count, 1) if count else None
                           for total, count in values]
                  for metric, values in acc.items()}
        # The overall average is the same sum and count the buckets hold, so
        # it needs no second pass over the rows.
        summary = {}
        for metric, values in acc.items():
            total = sum(v[0] for v in values)
            count = sum(v[1] for v in values)
            summary[metric] = round(total / count, 1) if count else None
        out.append({
            "key": key, "index": index, "uuid": gpu_uuid,
            "name": name_, "label": f"GPU {index} · {name_ or key}",
            "color": _gpu_color(key), "pcie": pcie,
            "vram_total_mb": vram_total,
            "current": {"util": c_util, "vram_mb": c_vram,
                        "temp_c": c_temp, "power_w": c_power},
            "summary": summary, "labels": labels, "series": series,
        })
    return out


def _gpu_series_rows(s: Session, provider_id: int, start: int,
                     end: int) -> list:
    """Bounded GPU-row projection for Hardware.

    Same rows as ``_gpu_rows(s, provider_id, start, end)`` (no JSON id
    filter) but only the columns ``_gpu_series`` consumes are
    materialized, so no ORM entities are created for the 1h window.  The
    ts-range index hint matches the P03-reviewed loader.
    """
    q = (select(GpuTelemetrySample.gpu_key, GpuTelemetrySample.gpu_index,
                GpuTelemetrySample.gpu_uuid, GpuTelemetrySample.name,
                GpuTelemetrySample.util, GpuTelemetrySample.vram_used_mb,
                GpuTelemetrySample.vram_total_mb, GpuTelemetrySample.temp_c,
                GpuTelemetrySample.power_w, GpuTelemetrySample.pcie,
                GpuTelemetrySample.ts)
         .where(GpuTelemetrySample.provider_id == provider_id,
                GpuTelemetrySample.ts >= start,
                GpuTelemetrySample.ts <= end)
         .with_hint(GpuTelemetrySample, "INDEXED BY ix_gputelemetrysample_ts"))
    rows = s.exec(q.order_by(GpuTelemetrySample.ts)).all()
    return [_GpuSeriesRow(*row) for row in rows]


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


# Sub-day windows used by the detail views.  RANGE_BUCKETS names these keys
# and the Model Detail range control offers them, but only the "Nd" form was
# ever parsed here -- so "1m", "5m", "15m", "1h", "24h" and "session" all fell
# through to the seven-day default.  Every one of those buttons returned a
# week of data while picking a bucket size for the window the user asked for,
# which is both wrong and slow: "1h" spanned 604,800 s at a 60 s bucket and
# produced 10,080 chart points for one hour.
_UNIT_MS = {"m": 60_000, "h": 3600_000, "d": 86400_000}
_SESSION_WINDOW_MS = 60_000


def range_start_ms(key: str, now: int) -> int:
    if key == "today":
        return local_day_start_ms(now)
    if key == "all":
        return 0
    if key == "session":
        # Session Detail's live window; range_detail applies the same bound.
        return now - _SESSION_WINDOW_MS
    m = re.match(r"^(\d+)([mhd])$", key or "")
    if m:
        return now - int(m.group(1)) * _UNIT_MS[m.group(2)]
    return now - 7 * 86400_000


def _observed_day_count(s: Session, prov_ids: list[int], now: int) -> Optional[int]:
    """Calendar days of history actually held, or None when there is none."""
    if not prov_ids:
        return None
    first = s.exec(select(SessionRow.start_at).where(
        SessionRow.provider_id.in_(prov_ids),
    ).order_by(SessionRow.start_at).limit(1)).first()
    if not first:
        return None
    first_day = datetime.fromtimestamp(first / 1000.0).date()
    current_day = datetime.fromtimestamp(now / 1000.0).date()
    return max(1, (current_day - first_day).days + 1)


def range_day_count(s: Session, prov_ids: list[int], range_key: str,
                    start: int, now: int) -> int:
    """Calendar days represented by a Models range, always at least one.

    A fixed range is capped by the history actually held.  Dividing a total by
    30 when only 8 days of data exist understates every per-day figure, and
    silently so: selecting 30d and all-time returned identical token totals --
    the same number to the digit, because both covered the same 8 days -- while
    reporting average daily figures that differed by more than 3x.  A range
    longer than the data now behaves like all-time, which was already correct.
    """
    if range_key == "today":
        return 1
    observed = _observed_day_count(s, prov_ids, now)
    match = re.match(r"^(\d+)d$", range_key or "")
    if match:
        requested = max(1, int(match.group(1)))
        return min(requested, observed) if observed else requested
    if range_key != "all":
        return 1
    return observed or 1


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


class OverviewSample(NamedTuple):
    model_id: Optional[int]
    ts: int
    state: str
    tokens_total: Optional[float]
    prompt_total: Optional[float]
    gen_total: Optional[float]
    prompt_seconds_total: Optional[float]
    gen_seconds_total: Optional[float]
    prompt_tps: Optional[float]
    gen_tps: Optional[float]
    mtp_proposed_total: Optional[float]
    mtp_accepted_total: Optional[float]
    context_used: Optional[int]


def fetch_overview_samples(s: Session, prov_ids: list[int],
                           start_ms: int, end_ms: Optional[int] = None,
                           model_ids: Optional[list[int]] = None) -> list[OverviewSample]:
    """Load aggregation fields without materializing full ORM objects."""
    q = select(
        TelemetrySample.model_id,
        TelemetrySample.ts,
        TelemetrySample.state,
        TelemetrySample.tokens_total,
        TelemetrySample.prompt_total,
        TelemetrySample.gen_total,
        TelemetrySample.prompt_seconds_total,
        TelemetrySample.gen_seconds_total,
        TelemetrySample.prompt_tps,
        TelemetrySample.gen_tps,
        TelemetrySample.mtp_proposed_total,
        TelemetrySample.mtp_accepted_total,
        TelemetrySample.context_used,
    ).where(
        TelemetrySample.provider_id.in_(prov_ids),
        TelemetrySample.ts >= start_ms,
    )
    if end_ms is not None:
        q = q.where(TelemetrySample.ts <= end_ms)
    if model_ids:
        q = q.where(TelemetrySample.model_id.in_(model_ids))
    q = q.order_by(TelemetrySample.ts)
    return [OverviewSample(*row) for row in s.exec(q).all()]


def _delta(r, prev, attr) -> float:
    if r is None or prev is None:
        return 0.0
    a, b = getattr(prev, attr), getattr(r, attr)
    if a is None or b is None:
        return 0.0
    d = b - a
    return d if d > 0 else 0.0


def _value_delta(current, previous) -> float:
    """Positive counter delta for values already read from a sample."""
    if current is None or previous is None:
        return 0.0
    delta = current - previous
    return delta if delta > 0 else 0.0


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
    __slots__ = ("tokens", "prompt_tokens", "gen_tokens", "unclassified_tokens",
                 "d_proposed", "d_accepted", "gen_time", "prompt_time", "idle_time",
                 "loaded_time", "peak_gen", "peak_prompt", "context_max", "spark")

    def __init__(self):
        self.tokens = 0.0
        self.prompt_tokens = 0.0
        self.gen_tokens = 0.0
        self.unclassified_tokens = 0.0
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
            token_delta = _value_delta(r.tokens_total, prev.tokens_total)
            prompt_delta = _value_delta(r.prompt_total, prev.prompt_total)
            gen_delta = _value_delta(r.gen_total, prev.gen_total)
            a.tokens += token_delta
            a.prompt_tokens += prompt_delta
            a.gen_tokens += gen_delta
            if prompt_delta > 0:
                prompt_seconds = _value_delta(
                    r.prompt_seconds_total, prev.prompt_seconds_total)
                if prompt_seconds > 0:
                    a.prompt_time += prompt_seconds
                elif r.prompt_tps is not None and r.prompt_tps > 0:
                    a.prompt_time += prompt_delta / r.prompt_tps
            if gen_delta > 0:
                gen_seconds = _value_delta(
                    r.gen_seconds_total, prev.gen_seconds_total)
                if gen_seconds > 0:
                    a.gen_time += gen_seconds
                elif r.gen_tps is not None and r.gen_tps > 0:
                    a.gen_time += gen_delta / r.gen_tps
            a.d_proposed += _value_delta(
                r.mtp_proposed_total, prev.mtp_proposed_total)
            a.d_accepted += _value_delta(
                r.mtp_accepted_total, prev.mtp_accepted_total)
            st = prev.state
            if st in ("IDLE", "PROMPTING", "GENERATING"):
                a.loaded_time += dt_s
            b = min(SPARK_BUCKETS - 1, int((prev.ts - start_ms) * SPARK_BUCKETS / span))
            d_tok = token_delta if token_delta > 0 else prompt_delta + gen_delta
            a.spark[max(0, b)] += d_tok
        if r.gen_tps is not None:
            a.peak_gen = max(a.peak_gen, r.gen_tps)
        if r.prompt_tps is not None:
            a.peak_prompt = max(a.peak_prompt, r.prompt_tps)
        if r.context_used:
            a.context_max = max(a.context_max, r.context_used)
        prev_by_model[mid] = r
    # If a model is loaded now, count the tail up to `now`. The first pass
    # already retained each model's last row, so this remains O(samples).
    for mid, a in acc.items():
        last = prev_by_model.get(mid)
        if last is not None:
            if last.state in ("IDLE", "PROMPTING", "GENERATING") and now - last.ts < 60_000:
                a.loaded_time += min(60.0, (now - last.ts) / 1000.0)
        a.idle_time = max(0.0, a.loaded_time - a.prompt_time - a.gen_time)


def aggregate_samples(s: Session, prov_ids: list[int], start_ms: int,
                      end_ms: int, now: int,
                      model_ids: Optional[list[int]] = None) -> dict[int, ModelAcc]:
    """Aggregate one telemetry window in SQLite without ORM materialization.

    With end_ms = now the loaded-tail term is non-negative, so SUM over the
    single is_last row equals the previous MAX form exactly.  SUM also
    matches Python accumulate() when a future-stamped last row makes the
    tail negative (Model Detail's unbounded fetch contract).
    """
    if not prov_ids:
        return {}
    model_filter = " AND current.model_id IN :model_ids" if model_ids else ""
    stmt = text("""
        WITH ordered AS (
            SELECT current.model_id, current.ts, current.state,
                   current.tokens_total, current.prompt_total, current.gen_total,
                   current.prompt_seconds_total, current.gen_seconds_total,
                   current.prompt_tps, current.gen_tps,
                   current.mtp_proposed_total, current.mtp_accepted_total,
                   current.context_used,
                   LAG(current.ts) OVER sample_window AS prev_ts,
                   LAG(current.state) OVER sample_window AS prev_state,
                   LAG(current.tokens_total) OVER sample_window AS prev_tokens_total,
                   LAG(current.prompt_total) OVER sample_window AS prev_prompt_total,
                   LAG(current.gen_total) OVER sample_window AS prev_gen_total,
                   LAG(current.prompt_seconds_total) OVER sample_window AS prev_prompt_seconds,
                   LAG(current.gen_seconds_total) OVER sample_window AS prev_gen_seconds,
                   LAG(current.mtp_proposed_total) OVER sample_window AS prev_proposed,
                   LAG(current.mtp_accepted_total) OVER sample_window AS prev_accepted,
                   ROW_NUMBER() OVER (
                       PARTITION BY current.provider_id, current.model_id
                       ORDER BY current.ts DESC
                   ) = 1 AS is_last
            FROM telemetrysample AS current
            WHERE current.provider_id IN :provider_ids
              AND current.ts >= :start_ms AND current.ts <= :end_ms
              {model_filter}
            WINDOW sample_window AS (
                PARTITION BY current.provider_id, current.model_id
                ORDER BY current.ts
            )
        ), deltas AS (
            SELECT *,
                   CASE WHEN tokens_total > prev_tokens_total
                        THEN tokens_total - prev_tokens_total ELSE 0 END AS token_delta,
                   CASE WHEN prompt_total > prev_prompt_total
                        THEN prompt_total - prev_prompt_total ELSE 0 END AS prompt_delta,
                   CASE WHEN gen_total > prev_gen_total
                        THEN gen_total - prev_gen_total ELSE 0 END AS gen_delta,
                   CASE WHEN mtp_proposed_total > prev_proposed
                        THEN mtp_proposed_total - prev_proposed ELSE 0 END AS proposed_delta,
                   CASE WHEN mtp_accepted_total > prev_accepted
                        THEN mtp_accepted_total - prev_accepted ELSE 0 END AS accepted_delta,
                   CASE WHEN prev_state IN ('IDLE', 'PROMPTING', 'GENERATING')
                        THEN MIN(:dt_cap, MAX(0.0, (ts - prev_ts) / 1000.0))
                        ELSE 0 END AS loaded_delta
            FROM ordered
        )
        SELECT model_id,
               SUM(token_delta), SUM(prompt_delta), SUM(gen_delta),
               SUM(proposed_delta), SUM(accepted_delta),
               SUM(CASE WHEN prompt_delta > 0 THEN
                       CASE WHEN prompt_seconds_total > prev_prompt_seconds
                            THEN prompt_seconds_total - prev_prompt_seconds
                            WHEN prompt_tps > 0 THEN prompt_delta / prompt_tps
                            ELSE 0 END
                   ELSE 0 END) AS prompt_time,
               SUM(CASE WHEN gen_delta > 0 THEN
                       CASE WHEN gen_seconds_total > prev_gen_seconds
                            THEN gen_seconds_total - prev_gen_seconds
                            WHEN gen_tps > 0 THEN gen_delta / gen_tps
                            ELSE 0 END
                   ELSE 0 END) AS gen_time,
               SUM(loaded_delta) + SUM(CASE
                   WHEN is_last
                    AND state IN ('IDLE', 'PROMPTING', 'GENERATING')
                    AND :now_ms - ts < 60000
                   THEN MIN(60.0, (:now_ms - ts) / 1000.0)
                   ELSE 0 END) AS loaded_time,
               MAX(gen_tps), MAX(prompt_tps), MAX(context_used)
        FROM deltas
        GROUP BY model_id
    """.format(model_filter=model_filter)).bindparams(
        bindparam("provider_ids", expanding=True),
        *([bindparam("model_ids", expanding=True)] if model_ids else []),
    )
    params = {
        "provider_ids": prov_ids, "start_ms": start_ms, "end_ms": end_ms,
        "now_ms": now, "dt_cap": DT_CAP_S,
    }
    if model_ids:
        params["model_ids"] = model_ids
    rows = s.execute(stmt, params).all()
    out: dict[int, ModelAcc] = {}
    for row in rows:
        a = ModelAcc()
        (mid, a.tokens, a.prompt_tokens, a.gen_tokens, a.d_proposed,
         a.d_accepted, a.prompt_time, a.gen_time, a.loaded_time,
         a.peak_gen, a.peak_prompt, a.context_max) = row
        a.tokens = a.tokens or 0.0
        a.prompt_tokens = a.prompt_tokens or 0.0
        a.gen_tokens = a.gen_tokens or 0.0
        a.d_proposed = a.d_proposed or 0.0
        a.d_accepted = a.d_accepted or 0.0
        a.prompt_time = a.prompt_time or 0.0
        a.gen_time = a.gen_time or 0.0
        a.loaded_time = a.loaded_time or 0.0
        a.peak_gen = a.peak_gen or 0.0
        a.peak_prompt = a.peak_prompt or 0.0
        a.context_max = a.context_max or 0
        a.idle_time = max(0.0, a.loaded_time - a.prompt_time - a.gen_time)
        out[mid] = a
    return out


def _day_index_lookup(day_bounds: list[int]) -> str:
    """SQL CASE expression mapping a prev_ts to its local-day bucket index."""
    cases = []
    for index, start in enumerate(day_bounds):
        end = day_bounds[index + 1] if index + 1 < len(day_bounds) else None
        if end is not None:
            cases.append(
                f"WHEN prev_ts >= {int(start)} AND prev_ts < {int(end)} THEN {index}")
        else:
            cases.append(f"WHEN prev_ts >= {int(start)} THEN {index}")
    return "CASE " + " ".join(cases) + " ELSE NULL END"


def _py_round_sql(expr: str) -> str:
    """SQL expression replicating Python round() for non-negative values.

    Python rounds .5 ties to even; SQLite ROUND() rounds them away from
    zero.  Deltas are positive-clamped before this expression runs, so only
    the non-negative branch is needed: floor via truncating CAST, then bump
    only when the fraction exceeds 0.5, resolving exact ties to the even
    neighbour via the floor's parity.
    """
    floor_expr = f"CAST({expr} AS INTEGER)"
    frac_expr = f"{expr} - {floor_expr}"
    return (f"({floor_expr} + CASE"
            f" WHEN {frac_expr} > 0.5 THEN 1"
            f" WHEN {frac_expr} < 0.5 THEN 0"
            f" ELSE {floor_expr} % 2 END)")


def _aggregate_overview_daily(s: Session, prov_ids: list[int],
                              start_ms: int, end_ms: int,
                              day_bounds: list[int]) -> dict[int, list[float]]:
    """SQL-side 30d daily delta aggregation for the Overview volume chart.

    Mirrors the Python per-model chain it replaces: rows are windowed per
    model (NULL model_id rows form their own partitions and never advance a
    chain), deltas are positive-clamped, attributed to the local calendar day
    of the *previous* sample, and rounded per row with Python round()
    semantics (ties-to-even) before summing.  Every non-NULL model_id
    participates, including historical/orphan ids with no current Model row,
    exactly as the previous Python loop did.  Returns day index ->
    [prompt, generated, unclassified, inference_seconds].
    """
    if not prov_ids:
        return {}
    day_case = _day_index_lookup(day_bounds)
    r = _py_round_sql
    stmt_text = f"""
        WITH ordered AS (
            SELECT model_id, ts,
                   tokens_total, prompt_total, gen_total,
                   prompt_seconds_total, gen_seconds_total,
                   prompt_tps, gen_tps,
                   LAG(ts) OVER w AS prev_ts,
                   LAG(tokens_total) OVER w AS prev_tokens_total,
                   LAG(prompt_total) OVER w AS prev_prompt_total,
                   LAG(gen_total) OVER w AS prev_gen_total,
                   LAG(prompt_seconds_total) OVER w AS prev_prompt_seconds,
                   LAG(gen_seconds_total) OVER w AS prev_gen_seconds
            FROM telemetrysample
            WHERE provider_id IN :provider_ids
              AND ts >= :start_ms AND ts <= :end_ms
            WINDOW w AS (PARTITION BY model_id ORDER BY ts)
        ), deltas AS MATERIALIZED (
            SELECT prev_ts,
                   CASE WHEN tokens_total > prev_tokens_total
                        THEN tokens_total - prev_tokens_total ELSE 0 END AS token_delta,
                   CASE WHEN prompt_total > prev_prompt_total
                        THEN prompt_total - prev_prompt_total ELSE 0 END AS prompt_delta,
                   CASE WHEN gen_total > prev_gen_total
                        THEN gen_total - prev_gen_total ELSE 0 END AS gen_delta,
                   CASE WHEN prompt_seconds_total > prev_prompt_seconds
                        THEN prompt_seconds_total - prev_prompt_seconds
                        ELSE 0 END AS prompt_seconds_delta,
                   CASE WHEN gen_seconds_total > prev_gen_seconds
                        THEN gen_seconds_total - prev_gen_seconds
                        ELSE 0 END AS gen_seconds_delta,
                   prompt_tps, gen_tps
            FROM ordered
            WHERE model_id IS NOT NULL
        ), bucketed AS (
            SELECT {day_case} AS day_index,
                  {r("prompt_delta")} AS prompt_delta,
                  {r("gen_delta")} AS gen_delta,
                  {r("CASE WHEN prompt_delta + gen_delta = 0"
                          " THEN token_delta ELSE 0 END")} AS unclassified_delta,
                  {r("CASE WHEN prompt_delta > 0 THEN"
                        " CASE WHEN prompt_seconds_delta > 0 THEN prompt_seconds_delta"
                        "      WHEN prompt_tps IS NOT NULL AND prompt_tps > 0"
                        "      THEN prompt_delta / prompt_tps"
                        "      ELSE 0 END"
                        " ELSE 0 END"
                       " + CASE WHEN gen_delta > 0 THEN"
                        " CASE WHEN gen_seconds_delta > 0 THEN gen_seconds_delta"
                        "      WHEN gen_tps IS NOT NULL AND gen_tps > 0"
                        "      THEN gen_delta / gen_tps"
                        "      ELSE 0 END"
                        " ELSE 0 END")} AS inference_delta
             FROM deltas
             WHERE prev_ts IS NOT NULL
        )
         SELECT day_index, SUM(prompt_delta), SUM(gen_delta),
               SUM(unclassified_delta), SUM(inference_delta)
        FROM bucketed
        WHERE day_index IS NOT NULL
        GROUP BY day_index
    """
    stmt = (text(stmt_text)
            .bindparams(bindparam("provider_ids", expanding=True)))
    rows = s.execute(stmt, {"provider_ids": list(prov_ids),
                            "start_ms": start_ms, "end_ms": end_ms}).all()
    out: dict[int, list[float]] = {}
    for day_index, prompt, generated, unclassified, inference in rows:
        out[int(day_index)] = [prompt or 0.0, generated or 0.0,
                               unclassified or 0.0, inference or 0.0]
    return out


def _aggregate_overview_hourly(s: Session, prov_ids: list[int],
                              start_ms: int, hour_ms: int) -> dict[int, list[int]]:
    """SQL-side 24h per-model delta aggregation for the Overview usage chart.

    Mirrors the Python chain it replaces: rows are windowed over the whole
    ordered sample set (regardless of model), a delta only counts when the
    previous *consecutive* row shares the same model, and the delta is
    attributed to the hour bucket of the previous row, clamped to [0, 23].
    Tokens fall back to the prompt+gen delta when the total delta is 0.
    Returns model_id -> 24 hourly buckets.
    """
    if not prov_ids:
        return {}
    stmt = (text("""
        WITH ordered AS (
            SELECT model_id, ts, tokens_total, prompt_total, gen_total,
                   LAG(model_id) OVER g AS prev_model_id,
                   LAG(ts) OVER g AS prev_ts,
                   LAG(tokens_total) OVER g AS prev_tokens_total,
                   LAG(prompt_total) OVER g AS prev_prompt_total,
                   LAG(gen_total) OVER g AS prev_gen_total
            FROM telemetrysample
            WHERE provider_id IN :provider_ids
              AND ts >= :start_ms
            WINDOW g AS (ORDER BY ts)
        ), deltas AS (
            SELECT model_id, prev_ts,
                   CASE WHEN tokens_total > prev_tokens_total
                        THEN tokens_total - prev_tokens_total ELSE 0 END AS token_delta,
                   CASE WHEN prompt_total > prev_prompt_total
                        THEN prompt_total - prev_prompt_total ELSE 0 END AS prompt_delta,
                   CASE WHEN gen_total > prev_gen_total
                        THEN gen_total - prev_gen_total ELSE 0 END AS gen_delta
            FROM ordered
            WHERE model_id IS NOT NULL
              AND model_id = prev_model_id
        ), bucketed AS (
            SELECT model_id,
                   MIN(23, MAX(0, CAST((prev_ts - :hour_ms) / 3600000 AS INTEGER)))
                        AS hour_index,
                   CAST(CASE WHEN token_delta > 0 THEN token_delta
                             ELSE prompt_delta + gen_delta END AS INTEGER) AS token_delta
            FROM deltas
            WHERE prev_ts IS NOT NULL
        )
        SELECT model_id, hour_index, SUM(token_delta)
        FROM bucketed
        GROUP BY model_id, hour_index
    """).bindparams(bindparam("provider_ids", expanding=True)))
    rows = s.execute(stmt, {"provider_ids": list(prov_ids),
                            "start_ms": start_ms, "hour_ms": hour_ms}).all()
    out: dict[int, list[int]] = {}
    for model_id, hour_index, delta in rows:
        if hour_index is None or delta is None:
            continue
        buckets = out.setdefault(int(model_id), [0] * 24)
        buckets[int(hour_index)] += int(delta)
    return out


def _session_count_aggregate(s: Session, prov_ids: list[int], start_ms: int,
                             model_ids: Optional[list[int]] = None
                             ) -> tuple[dict[int, int], int, int, int]:
    """Grouped COUNT(*) form of sessions_in_range (identical predicates).

    Returns ``(per_model_counts, total, ctx_n, ctx_sum)`` where ``ctx_n`` /
    ``ctx_sum`` feed avg_context_session: count and sum of truthy (non-NULL,
    non-zero) ``context_max`` over the same rows, matching the legacy Python
    ``[x.context_max for x in sess if x.context_max]`` filter.  One scan, no
    ORM materialization.
    """
    if not prov_ids:
        return {}, 0, 0, 0
    q = select(
        SessionRow.model_id,
        func.count().label("n"),
        func.sum(case((SessionRow.context_max.isnot(None)
                       & (SessionRow.context_max != 0), 1), else_=0)).label("ctx_n"),
        func.sum(case((SessionRow.context_max.isnot(None)
                       & (SessionRow.context_max != 0),
                       SessionRow.context_max), else_=0)).label("ctx_sum"),
    ).where(
        SessionRow.provider_id.in_(prov_ids),
        SessionRow.start_at <= now_ms(),
        (SessionRow.end_at.is_(None) | (SessionRow.end_at >= start_ms)),
    ).group_by(SessionRow.model_id)
    if model_ids:
        q = q.where(SessionRow.model_id.in_(model_ids))
    counts: dict[int, int] = {}
    total = 0
    ctx_n = 0
    ctx_sum = 0
    for model_id, n, c_n, c_sum in s.exec(q).all():
        key = model_id or 0
        counts[key] = counts.get(key, 0) + int(n)
        total += int(n)
        ctx_n += int(c_n or 0)
        ctx_sum += int(c_sum or 0)
    return counts, total, ctx_n, ctx_sum


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


def sessions_started_in_range(s: Session, prov_ids: list[int], start_ms: int,
                              end_ms: int) -> list[SessionRow]:
    """Sessions that began inside a calendar/reporting window."""
    if not prov_ids:
        return []
    return list(s.exec(select(SessionRow).where(
        SessionRow.provider_id.in_(prov_ids),
        SessionRow.start_at >= start_ms,
        SessionRow.start_at <= end_ms,
    )).all())


def _count_sessions_started(s: Session, prov_ids: list[int], start_ms: int,
                            end_ms: int) -> int:
    """COUNT(*) form of sessions_started_in_range, identical predicates."""
    if not prov_ids:
        return 0
    q = select(func.count()).select_from(SessionRow).where(
        SessionRow.provider_id.in_(prov_ids),
        SessionRow.start_at >= start_ms,
        SessionRow.start_at <= end_ms,
    )
    return int(s.exec(q).one())


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
# Strict G02 range aggregation (minute buckets + residency overlap)
# ---------------------------------------------------------------------------
_MINUTE_MS = 60_000


def _range_has_buckets(s: Session, prov_ids: list[int], start_ms: int,
                       end_ms: int) -> bool:
    """True when the range is covered by durable minute buckets (strict path)."""
    if not prov_ids:
        return False
    row = s.exec(text("""
        SELECT 1 FROM modelusagebucket
        WHERE provider_id IN :provider_ids
          AND bucket_start >= :start_ms AND bucket_start <= :end_ms
        LIMIT 1
    """).bindparams(bindparam("provider_ids", expanding=True)),
    params={"provider_ids": prov_ids, "start_ms": start_ms, "end_ms": end_ms}).first()
    return row is not None


def _telemetry_window(s: Session, pid: int, mid: int, a: int, b: int) -> dict:
    """Positive counter deltas over [a, b] from retained telemetry.

    A counter that decreases across the window is a reset: the post-reset value
    is used as the new baseline and the window is flagged ``reset``.
    """
    rows = s.exec(text("""
        SELECT ts, tokens_total, prompt_total, gen_total,
               prompt_seconds_total, gen_seconds_total,
               mtp_proposed_total, mtp_accepted_total
        FROM telemetrysample
        WHERE provider_id = :p AND model_id = :m
          AND ts >= :lo AND ts <= :hi
        ORDER BY ts
    """), params={"p": pid, "m": mid, "lo": a - _MINUTE_MS, "hi": b}).all()
    base = None
    head = None
    for r in rows:
        if r[0] <= a:
            base = r
        head = r
    if base is None:
        base = head  # no sample before the window start: best available baseline
        baseline_estimated = True
    else:
        baseline_estimated = False
    if head is None:
        return {"tokens": 0.0, "prompt": 0.0, "gen": 0.0, "uncl": 0.0,
                "prop": 0.0, "acc": 0.0, "prompt_s": 0.0, "gen_s": 0.0,
                "reset": False, "estimated": baseline_estimated}

    def pdelta(cur, prev):
        if cur is None or prev is None:
            return 0.0
        d = float(cur) - float(prev)
        return d if d > 0 else 0.0

    prompt = pdelta(head[2], base[2])
    gen = pdelta(head[3], base[3])
    tokens = pdelta(head[1], base[1])
    reset = (head[1] is not None and base[1] is not None
             and head[1] < base[1]) or \
            (head[2] is not None and base[2] is not None and head[2] < base[2])
    return {
        "tokens": tokens, "prompt": prompt, "gen": gen,
        "uncl": max(0.0, tokens - prompt - gen),
        "prop": pdelta(head[6], base[6]), "acc": pdelta(head[7], base[7]),
        "prompt_s": pdelta(head[4], base[4]), "gen_s": pdelta(head[5], base[5]),
        "reset": reset, "estimated": baseline_estimated or reset,
    }


def _residency_overlap_s(s: Session, pid: int, mid: int, a: int, b: int) -> float:
    """Seconds the model was resident within [a, b], from residency intervals."""
    rows = s.exec(text("""
        SELECT loaded_at, unloaded_at FROM modelresidency
        WHERE provider_id = :p AND model_id = :m
          AND loaded_at <= :b AND COALESCE(unloaded_at, :b) >= :a
    """), params={"p": pid, "m": mid, "a": a, "b": b}).all()
    total = 0.0
    for loaded_at, unloaded_at in rows:
        upper = unloaded_at if unloaded_at is not None else b
        total += max(0, min(b, upper) - max(a, loaded_at))
    return total / 1000.0


def _spark_index(minute: int, start_ms: int, span: int) -> int:
    if span <= 0:
        return 0
    idx = int((minute - start_ms) * SPARK_BUCKETS / span)
    return max(0, min(SPARK_BUCKETS - 1, idx))


def _strict_model(s: Session, pid: int, mid: int, start_ms: int, end_ms: int,
                  compute_spark: bool = False,
                  first_full: Optional[int] = None,
                  last_full: Optional[int] = None) -> tuple[ModelAcc, bool]:
    """Strict per-model aggregate: full interior minute buckets via SQL SUM,
    retained telemetry for partial boundary minutes, residency overlap."""
    acc = ModelAcc()
    estimated = False
    span = max(1, end_ms - start_ms)

    # Full interior minutes: minute >= start_ms AND minute+60s <= end_ms
    if first_full is None:
        first_full = ((start_ms + _MINUTE_MS - 1) // _MINUTE_MS) * _MINUTE_MS
    if last_full is None:
        last_full = ((end_ms - _MINUTE_MS) // _MINUTE_MS) * _MINUTE_MS

    # 1. SQL SUM for all full interior buckets (single aggregate, no row transfer).
    #    INDEXED BY forces the covering value index: equality seeks on
    #    (provider_id, model_id, bucket_start) read only index pages.
    if first_full <= last_full:
        row = s.exec(text("""
            SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(unclassified_tokens),0),
                   COALESCE(SUM(mtp_proposed),0), COALESCE(SUM(mtp_accepted),0),
                   COALESCE(SUM(prompt_time_s),0), COALESCE(SUM(gen_time_s),0)
            FROM modelusagebucket INDEXED BY ix_usagebucket_cov_values
            WHERE provider_id = :p AND model_id = :m
              AND bucket_start >= :a AND bucket_start <= :b
        """), params={"p": pid, "m": mid, "a": first_full, "b": last_full}).first()
        if row:
            acc.prompt_tokens += row[0] or 0.0
            acc.gen_tokens += row[1] or 0.0
            acc.unclassified_tokens += row[2] or 0.0
            acc.d_proposed += row[3] or 0.0
            acc.d_accepted += row[4] or 0.0
            acc.prompt_time += row[5] or 0.0
            acc.gen_time += row[6] or 0.0

        # Spark: only computed when explicitly requested (display feature)
        if compute_spark:
            bucket_ms = span / SPARK_BUCKETS
            spark_rows = s.exec(text("""
                SELECT MIN(:sbm1, MAX(0, CAST((bucket_start - :start) / :bms AS INTEGER))) as idx,
                       SUM(input_tokens + output_tokens + unclassified_tokens)
                FROM modelusagebucket INDEXED BY ix_usagebucket_cov_values
                WHERE provider_id = :p AND model_id = :m
                  AND bucket_start >= :a AND bucket_start <= :b
                GROUP BY idx
            """), params={"p": pid, "m": mid, "a": first_full, "b": last_full,
                          "start": float(start_ms), "bms": bucket_ms,
                          "sbm1": SPARK_BUCKETS - 1}).all()
            for idx, tokens in spark_rows:
                if idx is not None and tokens:
                    acc.spark[idx] += tokens

    # 2. Partial boundary minutes (at most 2): use retained telemetry
    first_pmin = (start_ms // _MINUTE_MS) * _MINUTE_MS if start_ms % _MINUTE_MS != 0 else None
    last_pmin = (end_ms // _MINUTE_MS) * _MINUTE_MS if end_ms % _MINUTE_MS != 0 else None
    if first_pmin is not None and last_pmin is not None and first_pmin == last_pmin:
        partial_ranges = [(start_ms, end_ms)]
    elif first_pmin is not None:
        partial_ranges = [(start_ms, min(end_ms, first_pmin + _MINUTE_MS))]
    elif last_pmin is not None:
        partial_ranges = [(max(start_ms, last_pmin), end_ms)]
    else:
        partial_ranges = []
    if first_pmin is not None and last_pmin is not None and first_pmin != last_pmin:
        partial_ranges = [(start_ms, first_pmin + _MINUTE_MS),
                          (last_pmin, end_ms)]

    for pa, pb in partial_ranges:
        d = _telemetry_window(s, pid, mid, pa, pb)
        tokens = d["prompt"] + d["gen"] + d["uncl"]
        acc.prompt_tokens += d["prompt"]
        acc.gen_tokens += d["gen"]
        acc.unclassified_tokens += d["uncl"]
        acc.d_proposed += d["prop"]
        acc.d_accepted += d["acc"]
        acc.prompt_time += d["prompt_s"]
        acc.gen_time += d["gen_s"]
        if compute_spark:
            pmin = (pa // _MINUTE_MS) * _MINUTE_MS
            acc.spark[_spark_index(pmin, start_ms, span)] += tokens
        if d["estimated"]:
            estimated = True

    acc.tokens = acc.prompt_tokens + acc.gen_tokens + acc.unclassified_tokens
    acc.loaded_time = _residency_overlap_s(s, pid, mid, start_ms, end_ms)
    acc.idle_time = max(0.0, acc.loaded_time - acc.prompt_time - acc.gen_time)
    peak = s.exec(text("""
        SELECT MAX(gen_tps), MAX(prompt_tps), MAX(context_used)
        FROM telemetrysample
        WHERE provider_id = :p AND model_id = :m
          AND ts >= :a AND ts <= :b
    """), params={"p": pid, "m": mid, "a": start_ms, "b": end_ms}).first()
    if peak:
        acc.peak_gen = peak[0] or 0.0
        acc.peak_prompt = peak[1] or 0.0
        acc.context_max = peak[2] or 0
    return acc, estimated


def _strict_range(s: Session, prov_ids: list[int], models: list[Model],
                  start_ms: int, end_ms: int) -> tuple[dict[int, ModelAcc], bool]:
    """Strict per-model aggregates over a range.

    Per-model equality seeks on ix_usagebucket_cov_values keep the scan fully
    covered (no table-row fetch per bucket) and, measured on the production
    replica, are ~10x faster than IN-list/GROUP BY variants of the same query.
    Semantics are identical to _strict_model (boundary telemetry, residency,
    peaks); only the bucket aggregation is lifted to a covered index scan.
    """
    acc: dict[int, ModelAcc] = {}
    estimated = False
    first_full = ((start_ms + _MINUTE_MS - 1) // _MINUTE_MS) * _MINUTE_MS
    last_full = ((end_ms - _MINUTE_MS) // _MINUTE_MS) * _MINUTE_MS
    for model in models:
        m_acc, m_est = _strict_model(s, model.provider_id, model.id, start_ms,
                                     end_ms, first_full=first_full,
                                     last_full=last_full)
        acc[model.id] = m_acc
        estimated = estimated or m_est
    return acc, estimated


# ---------------------------------------------------------------------------
# Models page
# ---------------------------------------------------------------------------
class RangeSummary(NamedTuple):
    now: int
    start: int
    providers: list[Provider]
    models: list[Model]
    acc: dict[int, ModelAcc]
    session_counts: dict[int, int]
    session_total: int
    active: dict[int, dict]
    session_ctx_n: int = 0
    session_ctx_sum: int = 0
    sparks: Optional[dict] = None
    total_tokens: float = 0.0
    prev_tokens: float = 0.0
    day_count: int = 1
    estimated: bool = False
    completed_at: float = 0.0


def _window_acc(s: Session, prov_ids: list[int], models: list[Model],
                start_ms: int, end_ms: int, now: int) -> dict[int, ModelAcc]:
    """Per-model aggregate for a window: strict buckets when covered, else the
    legacy session/telemetry aggregate."""
    if _range_has_buckets(s, prov_ids, start_ms, end_ms):
        return _strict_range(s, prov_ids, models, start_ms, end_ms)[0]
    return aggregate_range_summary(s, prov_ids, start_ms, end_ms, now)


def _session_sparks(s: Session, prov_ids: list[int], models: list[Model],
                    start_ms: int, now: int) -> dict[int, list[float]]:
    """Legacy per-model sparkline (session-start based), keyed by model id."""
    if not prov_ids:
        return {}
    bucket_ms = max(1, (now - start_ms) // SPARK_BUCKETS)
    stmt = text("""
        SELECT model_id,
               MIN(:bucket_count - 1,
                   CAST((MAX(start_at, :start_ms) - :start_ms) / :bucket_ms AS INTEGER)) AS bucket,
               SUM(COALESCE(total_tokens, 0)) AS tokens
        FROM session
        WHERE provider_id IN :provider_ids
          AND start_at <= :end_ms
          AND (end_at IS NULL OR end_at >= :start_ms)
        GROUP BY model_id, bucket
    """).bindparams(bindparam("provider_ids", expanding=True))
    out: dict[int, list[float]] = {}
    for model_id, bucket, tokens in s.execute(stmt, {
        "provider_ids": prov_ids, "start_ms": start_ms, "end_ms": now,
        "bucket_ms": bucket_ms, "bucket_count": SPARK_BUCKETS,
    }).all():
        if model_id is not None:
            out.setdefault(model_id, [0.0] * SPARK_BUCKETS)[bucket] += max(0.0, tokens or 0.0)
    return out


def _prev_window_tokens(s: Session, pm_pairs: list[tuple[int, int]],
                        start_ms: int, end_ms: int) -> float:
    """Lightweight token total for the previous window.

    Per-model equality seeks on the covering bucket index avoid both the
    per-row table fetches of the key-only indexes and the pathological
    IN-list range-loop plans SQLite picks for the grouped variant.
    """
    if not pm_pairs:
        return 0.0
    first_full = ((start_ms + _MINUTE_MS - 1) // _MINUTE_MS) * _MINUTE_MS
    last_full = ((end_ms - _MINUTE_MS) // _MINUTE_MS) * _MINUTE_MS
    if first_full > last_full:
        return 0.0
    total = 0.0
    for pid, mid in pm_pairs:
        row = s.exec(text("""
            SELECT COALESCE(SUM(input_tokens + output_tokens + unclassified_tokens), 0)
            FROM modelusagebucket INDEXED BY ix_usagebucket_cov_values
            WHERE provider_id = :p AND model_id = :m
              AND bucket_start >= :a AND bucket_start <= :b
        """), params={"p": pid, "m": mid, "a": first_full, "b": last_full}).first()
        if row and row[0]:
            total += float(row[0])
    return total


def range_summary(s: Session, provider_id: Optional[int], range_key: str) -> RangeSummary:
    """Build the immutable provider/range snapshot reused by Models, selected,
    sparks, and Compare. Uses durable minute buckets + residency overlap when the
    range is covered by buckets (strict); otherwise the session/telemetry legacy
    aggregate (compat fallback)."""
    now = now_ms()
    start = range_start_ms(range_key, now)
    provs = list(s.exec(select(Provider)).all())
    if provider_id:
        provs = [p for p in provs if p.id == provider_id]
    prov_ids = [p.id for p in provs]
    if not prov_ids:
        return RangeSummary(now, start, [], [], {}, {}, 0, {})
    models = list(s.exec(select(Model).where(Model.provider_id.in_(prov_ids))).all())
    estimated = False
    if _range_has_buckets(s, prov_ids, start, now):
        acc, estimated = _strict_range(s, prov_ids, models, start, now)
        sparks = {mid: list(a.spark) for mid, a in acc.items() if any(a.spark)}
    else:
        acc = aggregate_range_summary(s, prov_ids, start, now, now)
        sparks = _session_sparks(s, prov_ids, models, start, now)
    sess_counts, sess_total, sess_ctx_n, sess_ctx_sum = \
        _session_count_aggregate(s, prov_ids, start)
    active = _active_models(s, now, [m.id for m in models])
    total_tokens = sum(a.tokens for a in acc.values())
    prev_tokens = 0.0
    if start > 1:
        prev_start = start - max(1, now - start)
        prev_tokens = _prev_window_tokens(
            s, [(m.provider_id, m.id) for m in models], prev_start, start)
    day_count = range_day_count(s, prov_ids, range_key, start, now)
    return RangeSummary(now, start, provs, models, acc, sess_counts, sess_total,
                        active, sess_ctx_n, sess_ctx_sum, sparks, total_tokens,
                        prev_tokens, day_count, estimated, time.monotonic())


def models_page(s: Session, provider_id: Optional[int], range_key: str, group: str,
                summary: Optional[RangeSummary] = None,
                include_sparks: bool = True) -> dict:
    summary = summary or range_summary(s, provider_id, range_key)
    now = summary.now
    start = summary.start
    provs = summary.providers
    models = summary.models
    acc = summary.acc
    sess_by_model = summary.session_counts
    active_by_model = summary.active
    prov_ids = [p.id for p in provs]
    provider_names = {p.id: p.name for p in provs}
    if not prov_ids:
        return {"rows": [], "top": {}}
    m_by_id = {m.id: m for m in models}
    # Sparklines come from the shared snapshot (no new SQL per request).
    spark_values = model_sparks(s, summary, "model") if include_sparks else {}

    def gkey(m: Model) -> str:
        if group == "model":
            return str(m.id)
        if group == "family":
            return m.family or m.name
        if group == "quant":
            return m.quant or "unknown"
        return m.name

    total_tokens = sum(a.tokens for a in acc.values())
    total_gen = sum(a.gen_tokens for a in acc.values())
    total_prompt = sum(a.prompt_tokens for a in acc.values())
    groups: dict[str, dict] = {}
    for m in models:
        a = acc.get(m.id)
        tokens = a.tokens if a else 0.0
        active = active_by_model.get(m.id)
        if tokens <= 0 and m.id not in sess_by_model and active is None:
            continue
        g = gkey(m)
        row = groups.setdefault(g, {
            "key": g, "label": m.name if group == "model" else g,
            "model_ids": [], "quant": m.quant if group == "model" else None,
            "family": m.family if group == "model" else None,
            "provider": provider_names.get(m.provider_id) if group == "model" else None,
            "tokens": 0.0, "gen_tokens": 0.0,
            "prompt_tokens": 0.0, "gen_time": 0.0, "prompt_time": 0.0,
            "loaded_time": 0.0, "idle_time": 0.0,
            "peak_gen": 0.0, "peak_prompt": 0.0,
            "sessions": 0, "spark": [0.0] * SPARK_BUCKETS, "color": m.color,
            "active_rank": 0, "active_tasks": 0, "active_seen_at": 0,
            "input_cost": "0", "output_cost": "0", "total_cost": "0",
            "input_price_per_million": None, "output_price_per_million": None,
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
        costs = compute_costs(m, a.prompt_tokens if a else 0.0,
                              a.gen_tokens if a else 0.0)
        row["input_cost"] = str(Decimal(row["input_cost"]) + Decimal(costs["input_cost"]))
        row["output_cost"] = str(Decimal(row["output_cost"]) + Decimal(costs["output_cost"]))
        row["total_cost"] = str(Decimal(row["total_cost"]) + Decimal(costs["total_cost"]))
        if m.input_price_per_million and not row["input_price_per_million"]:
            row["input_price_per_million"] = m.input_price_per_million
        elif m.input_price_per_million and row["input_price_per_million"] and \
                m.input_price_per_million != row["input_price_per_million"]:
            row["input_price_per_million"] = "mixed"
        if m.output_price_per_million and not row["output_price_per_million"]:
            row["output_price_per_million"] = m.output_price_per_million
        elif m.output_price_per_million and row["output_price_per_million"] and \
                m.output_price_per_million != row["output_price_per_million"]:
            row["output_price_per_million"] = "mixed"
        if include_sparks:
            spark = spark_values.get(str(m.id))
            if spark:
                for i in range(SPARK_BUCKETS):
                    row["spark"][i] += spark[i]
        row["sessions"] += sess_by_model.get(m.id, 0)
        if active:
            row["active_rank"] = max(row["active_rank"], active["rank"])
            row["active_tasks"] += active["task_count"]
            row["active_seen_at"] = max(row["active_seen_at"], active["latest_seen"])

    rows = []
    for row in groups.values():
        share = (row["tokens"] / total_tokens * 100.0) if total_tokens else 0.0
        gen_tps = (row["gen_tokens"] / row["gen_time"]) if row["gen_time"] > 0 else None
        rows.append({
            "key": row["key"], "label": row["label"], "model_ids": row["model_ids"],
            "quant": row["quant"], "family": row["family"], "provider": row["provider"],
            "color": row["color"], "tokens": round(row["tokens"]),
            # Keep canonical model-level values in the API response.  The
            # browser groups these rows without making another range request.
            "gen_tokens": round(row["gen_tokens"]),
            "prompt_tokens": round(row["prompt_tokens"]),
            "gen_time": row["gen_time"], "prompt_time": row["prompt_time"],
            "loaded_time": row["loaded_time"], "idle_time": row["idle_time"],
            "share": round(share, 1), "sessions": row["sessions"],
            "gen_tps": round(gen_tps, 1) if gen_tps else None,
            "peak_gen": round(row["peak_gen"], 1) if row["peak_gen"] else None,
            "inference_s": round(row["prompt_time"] + row["gen_time"]),
            "loaded_s": round(row["loaded_time"]),
            "idle_s": round(row["idle_time"]),
            "active": row["active_rank"] > 0,
            "active_status": ("LIVE" if row["active_rank"] == 2 else
                              ("FINALIZING" if row["active_rank"] == 1 else None)),
            "active_rank": row["active_rank"],
            "active_tasks": row["active_tasks"],
            "active_seen_at": row["active_seen_at"] or None,
            "spark": [round(x) for x in row["spark"]],
            "input_price_per_million": row["input_price_per_million"],
            "output_price_per_million": row["output_price_per_million"],
            "input_cost": format(Decimal(row["input_cost"]), ".8f"),
            "output_cost": format(Decimal(row["output_cost"]), ".8f"),
            "total_cost": format(Decimal(row["total_cost"]), ".8f"),
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
    slowest = None
    for r in rows:
        if not r["peak_gen"]:
            continue
        if fastest is None or r["peak_gen"] > fastest[1]:
            fastest = (r["label"], r["peak_gen"])
        # Deliberately no token-count floor: the slowest decode counts however
        # little it generated.
        if slowest is None or r["peak_gen"] < slowest[1]:
            slowest = (r["label"], r["peak_gen"])

    # Costs for the selected range, summed from the same per-model figures the
    # table and the selected-model card already show, so the three always agree.
    cost_in = sum((Decimal(r["input_cost"]) for r in rows), Decimal("0"))
    cost_out = sum((Decimal(r["output_cost"]) for r in rows), Decimal("0"))

    prev_tokens = summary.prev_tokens
    trend = None
    if prev_tokens > 0:
        trend = round((total_tokens - prev_tokens) / prev_tokens * 100.0, 1)

    sess_ctx = None
    if summary.session_ctx_n > 0:
        sess_ctx = round(summary.session_ctx_sum / summary.session_ctx_n)
    top = {
        "tokens": round(total_tokens),
        "avg_daily_tokens": round(total_tokens / summary.day_count) if summary.day_count else 0,
        "tokens_trend": trend,
        "families": len(fam_tokens),
        "families_leader": max(fam_tokens, key=fam_tokens.get) if fam_tokens else None,
        "sessions": summary.session_total,
        "avg_context_session": sess_ctx,
        "generated_pct": round(total_gen / total_tokens * 100.0, 1) if total_tokens else None,
        "gen_tokens": round(total_gen),
        "prompt_tokens": round(total_prompt),
        "input_pct": round(total_prompt / total_tokens * 100.0, 1) if total_tokens else None,
        "generated_cost": format(cost_out, ".8f"),
        "input_cost": format(cost_in, ".8f"),
        "total_cost": format(cost_in + cost_out, ".8f"),
        "leader_share": leader_row["share"] if leader_row else None,
        "leader_name": leader_row["label"] if leader_row else None,
        "fastest": fastest[1] if fastest else None,
        "fastest_name": fastest[0] if fastest else None,
        "slowest": slowest[1] if slowest else None,
        "slowest_name": slowest[0] if slowest else None,
    }
    return {"rows": rows, "top": top, "now": now, "range": range_key, "group": group}


def model_sparks(s: Session, summary: RangeSummary, group: str) -> dict[str, list[int]]:
    """Sparkline-only payload. Reads the shared snapshot's precomputed sparks, so
    this performs no additional range SQL (compat fallback only if absent)."""
    now, start = summary.now, summary.start
    models = summary.models
    if not models:
        return {}
    raw = summary.sparks
    if not raw:
        raw = _session_sparks(s, [p.id for p in summary.providers], models, start, now)

    def key(model: Model) -> str:
        if group == "model":
            return str(model.id)
        if group == "family":
            return model.family or model.name
        if group == "quant":
            return model.quant or "unknown"
        return model.name

    out: dict[str, list[int]] = {}
    for model in models:
        spark = raw.get(model.id)
        if not spark:
            continue
        values = out.setdefault(key(model), [0] * SPARK_BUCKETS)
        for index, value in enumerate(spark):
            values[index] += round(value)
    return out


def aggregate_range_summary(s: Session, prov_ids: list[int], start_ms: int,
                            end_ms: int, now: int,
                            model_ids: Optional[list[int]] = None) -> dict[int, ModelAcc]:
    """Fast, correct per-model range aggregate for landing views.

    Session rows persist authoritative totals even after a router counter is
    reset by unloading or reloading a model.  They are also updated while a
    session is active, so this stays current without scanning telemetry history.
    Telemetry is consulted only for loaded duration, which sessions do not
    represent when a model is resident without an active request.
    """
    if not prov_ids:
        return {}
    model_filter = " AND model_id IN :model_ids" if model_ids else ""
    session_stmt = text("""
        SELECT model_id,
               SUM(COALESCE(total_tokens, 0)) AS tokens,
               SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
               SUM(COALESCE(gen_tokens, 0)) AS gen_tokens,
               SUM(COALESCE(mtp_proposed, 0)) AS proposed,
               SUM(COALESCE(mtp_accepted, 0)) AS accepted,
               SUM(COALESCE(prompt_time_s, 0)) AS prompt_time,
               SUM(COALESCE(gen_time_s, 0)) AS gen_time,
               MAX(COALESCE(peak_gen_tps, 0)) AS peak_gen,
               MAX(COALESCE(peak_prompt_tps, 0)) AS peak_prompt,
               MAX(COALESCE(context_max, 0)) AS context_max
        FROM session
        WHERE provider_id IN :provider_ids
          AND start_at <= :end_ms
          AND (end_at IS NULL OR end_at >= :start_ms)
        {model_filter}
        GROUP BY model_id
    """.format(model_filter=model_filter)).bindparams(
        bindparam("provider_ids", expanding=True),
        *([bindparam("model_ids", expanding=True)] if model_ids else []),
    )
    params = {"provider_ids": prov_ids, "start_ms": start_ms, "end_ms": end_ms}
    if model_ids:
        params["model_ids"] = model_ids
    out: dict[int, ModelAcc] = {}
    for row in s.execute(session_stmt, params).all():
        (model_id, tokens, prompt_tokens, gen_tokens, proposed, accepted,
         prompt_time, gen_time, peak_gen, peak_prompt, context_max) = row
        if model_id is None:
            continue
        acc = out.setdefault(model_id, ModelAcc())
        acc.tokens += max(0.0, tokens or 0.0)
        acc.prompt_tokens += max(0.0, prompt_tokens or 0.0)
        acc.gen_tokens += max(0.0, gen_tokens or 0.0)
        acc.d_proposed += max(0.0, proposed or 0.0)
        acc.d_accepted += max(0.0, accepted or 0.0)
        acc.prompt_time += max(0.0, prompt_time or 0.0)
        acc.gen_time += max(0.0, gen_time or 0.0)
        acc.peak_gen = max(acc.peak_gen, peak_gen or 0.0)
        acc.peak_prompt = max(acc.peak_prompt, peak_prompt or 0.0)
        acc.context_max = max(acc.context_max, context_max or 0)

    loaded_stmt = text("""
        SELECT model_id, SUM(loaded_ms) AS loaded_ms
        FROM (
            SELECT provider_id, model_id, MAX(ts) - MIN(ts) AS loaded_ms
            FROM telemetrysample
            WHERE provider_id IN :provider_ids AND ts >= :start_ms AND ts <= :end_ms
            {model_filter}
            GROUP BY provider_id, model_id
        )
        GROUP BY model_id
    """.format(model_filter=model_filter)).bindparams(
        bindparam("provider_ids", expanding=True),
        *([bindparam("model_ids", expanding=True)] if model_ids else []),
    )
    for model_id, loaded_ms in s.execute(loaded_stmt, params).all():
        if model_id is not None:
            acc = out.setdefault(model_id, ModelAcc())
            acc.loaded_time += max(0.0, (loaded_ms or 0.0) / 1000.0)
    for acc in out.values():
        acc.idle_time = max(0.0, acc.loaded_time - acc.prompt_time - acc.gen_time)
    return out


def selected_stats(s: Session, model_ids: list[int], provider_id: Optional[int],
                   range_key: str, summary: Optional[RangeSummary] = None,
                   realtime_override: Optional[dict] = None) -> dict:
    if not model_ids:
        return {}
    now = now_ms()
    start = range_start_ms(range_key, now)
    provs = list(s.exec(select(Provider)).all())
    prov_ids = [p.id for p in provs if p.id == provider_id] if provider_id else [p.id for p in provs]
    models = {m.id: m for m in s.exec(select(Model).where(Model.id.in_(model_ids))).all()}
    # The surrounding Models page already built this exact snapshot. Reusing its
    # selected rows/sparks/sessions makes this endpoint a pure reader: it runs no
    # new all-model, spark, or session SQL when a summary is supplied.
    idset = set(model_ids)
    if summary is not None:
        acc = {model_id: summary.acc.get(model_id, ModelAcc())
               for model_id in model_ids}
        if summary.sparks:
            spark_rows = {str(model_id): list(summary.sparks.get(model_id, []))
                          for model_id in model_ids}
        else:
            spark_rows = model_sparks(s, summary, "model")
        sess_count = sum(summary.session_counts.get(m, 0) for m in idset)
        total_all = summary.total_tokens
    else:
        acc = aggregate_range_summary(s, prov_ids, start, now, now, model_ids=model_ids)
        local_summary = range_summary(s, provider_id, range_key)
        spark_rows = model_sparks(s, local_summary, "model")
        counts, sess_total, _, _ = _session_count_aggregate(
            s, prov_ids, start, model_ids=model_ids)
        sess_count = sess_total
        total_all = sum(a.tokens for a in
                        aggregate_samples(s, prov_ids, start, now, now).values())
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
    spark = [0.0] * SPARK_BUCKETS
    for model_id in model_ids:
        for index, value in enumerate(spark_rows.get(str(model_id), [])):
            spark[index] += value
    m = models.get(model_ids[0])
    prov = s.get(Provider, m.provider_id) if m else None
    if realtime_override is None:
        live_items = [_model_live(s, model_id, now) for model_id in model_ids]
        live_items = [item for item in live_items if item]
        realtime = _selected_realtime(s, models, now)
        history = _runtime_history(s, realtime)
        if history:
            realtime["history"] = history
    else:
        live_items = []
        realtime = realtime_override
    _in_cost_total = Decimal("0")
    _out_cost_total = Decimal("0")
    _in_rates: set[str] = set()
    _out_rates: set[str] = set()
    for _mid in model_ids:
        _m = models.get(_mid)
        if _m is None:
            continue
        _a = acc.get(_mid)
        if _a is None:
            continue
        _in_r = Decimal(_m.input_price_per_million) if _m.input_price_per_million else Decimal("0")
        _out_r = Decimal(_m.output_price_per_million) if _m.output_price_per_million else Decimal("0")
        _in_cost_total += (Decimal(str(_a.prompt_tokens)) / Decimal("1000000")) * _in_r
        _out_cost_total += (Decimal(str(_a.gen_tokens)) / Decimal("1000000")) * _out_r
        _in_rates.add(_m.input_price_per_million or "0")
        _out_rates.add(_m.output_price_per_million or "0")
    sel_costs = {
        "input_price_per_million": _in_rates.pop() if len(_in_rates) == 1 else None,
        "output_price_per_million": _out_rates.pop() if len(_out_rates) == 1 else None,
        "input_cost": format(_in_cost_total, ".8f"),
        "output_cost": format(_out_cost_total, ".8f"),
        "total_cost": format(_in_cost_total + _out_cost_total, ".8f"),
    }
    return {
        "label": m.name if m else None,
        "color": m.color if m else "#4b8de8",
        "tokens": round(tokens),
        "share": round(tokens / total_all * 100.0, 1) if total_all else None,
        "generated_pct": round(gen_tokens / tokens * 100.0, 1) if tokens else None,
        "gen_tokens": round(gen_tokens),
        "prompt_tokens": round(prompt_tokens),
        "sessions": sess_count,
        "per_session": round(tokens / sess_count) if sess_count else None,
        "peak_gen": round(peak_gen, 1) if peak_gen else None,
        "peak_prompt": round(peak_prompt, 1) if peak_prompt else None,
        "gen_tps": round(gen_tokens / gen_time, 1) if gen_time > 0 else None,
        "mtp_proposed": round(mtp_proposed),
        "mtp_accepted": round(mtp_accepted),
        "mtp_rejected": round(mtp_rejected),
        "mtp_acc": round(mtp_acc, 1) if mtp_acc is not None else None,
        "spark": [round(x) for x in spark],
        "provider": prov.name if prov else None,
        "live": live_items[0] if len(live_items) == 1 else
                ({"tasks": live_items, "provisional": True} if live_items else None),
        "realtime": realtime,
        "input_price_per_million": sel_costs["input_price_per_million"],
        "output_price_per_million": sel_costs["output_price_per_million"],
        "input_cost": sel_costs["input_cost"],
        "output_cost": sel_costs["output_cost"],
        "total_cost": sel_costs["total_cost"],
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
            "id": p.id, "name": p.name,
            "status": effective_provider_status(p, now), "enabled": p.enabled,
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
            "live": _session_live(sess, now),
        }
        if current["live"]:
            current["state"] = "GENERATING" if current["live"].get("gen_tokens", 0) > 0 else "PROMPTING"
            current["gen_tps"] = current["live"].get("gen_tps")
            current["context_used"] = current["live"].get("context")
            current["context_max"] = current["live"].get("context_max") or current["context_max"]
            current["context_pct"] = current["live"].get("context_pct")

    day_start = local_day_start_ms(now)
    h0 = now - 24 * 3600_000
    prov_ids = [p.id for p in provs]
    current_day = datetime.fromtimestamp(now / 1000.0).replace(
        hour=0, minute=0, second=0, microsecond=0)
    daily_starts = [current_day - timedelta(days=offset) for offset in range(29, -1, -1)]
    daily_ms = [int(day.timestamp() * 1000) for day in daily_starts]
    daily = {
        "labels": [day.strftime("%b %d").replace(" 0", " ") for day in daily_starts],
        "inference_seconds": [0] * 30,
        "prompt_tokens": [0] * 30,
        "generated_tokens": [0] * 30,
        "unclassified_tokens": [0] * 30,
    }
    # Historical chart deltas are aggregated in SQL (window functions) so the
    # 30d sample history is never materialized in Python.  Both charts derive
    # from the same positive-clamped counter-delta semantics the Python chain
    # used: per-row rounding, reset clamping, phase-time fallbacks, and
    # prev-sample bucket attribution are all preserved.
    daily_agg = _aggregate_overview_daily(s, prov_ids, daily_ms[0], now,
                                          day_bounds=daily_ms + [now + 1])
    for index, values in daily_agg.items():
        prompt, generated, unclassified, inference = values
        daily["prompt_tokens"][index] += int(round(prompt))
        daily["generated_tokens"][index] += int(round(generated))
        daily["unclassified_tokens"][index] += int(round(unclassified))
        daily["inference_seconds"][index] += int(round(inference))
    acc = aggregate_samples(s, prov_ids, day_start, now, now)
    today = {
        "tokens": round(sum(a.tokens for a in acc.values())),
        "inference_s": round(sum(a.prompt_time + a.gen_time for a in acc.values())),
        "loaded_s": round(sum(a.loaded_time for a in acc.values())),
        "idle_s": round(sum(a.idle_time for a in acc.values())),
        "sessions": _count_sessions_started(s, prov_ids, day_start, now),
    }
    today["utilization"] = (round(today["inference_s"] / today["loaded_s"] * 100.0, 1)
                            if today["loaded_s"] > 0 else None)

    # model usage over last 24h (hourly buckets); deltas aggregated in SQL
    per_model = _aggregate_overview_hourly(s, prov_ids, h0, h0)
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
            "status": _effective_session_status(x, now), "context_max": x.context_max,
            "live": _session_live(x, now),
        })

    return {
        "providers": provs_out,
        "current": current,
        "today": today,
        "usage_24h": {"labels": hour_labels, "series": usage_series},
        "daily_volume": daily,
        "recent_sessions": recent_out,
        "now": now,
    }


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
    # The historical Python path fetched samples with no upper time bound;
    # future-stamped rows (clock skew) participate in the chain and in the
    # loaded-tail term.  Keep the window unbounded above for exactness.
    acc: dict[int, ModelAcc] = aggregate_samples(
        s, [m.provider_id], start, _UNBOUNDED_MS, now, model_ids=[model_id])
    a = acc.get(model_id) or ModelAcc()
    sess_count = _count_sessions_in_range(
        s, [m.provider_id], start, model_ids=[model_id])
    inference = a.prompt_time + a.gen_time
    dts = range_detail(s, m.provider_id, model_id, range_key, [], start, now)
    gpu_start = now - 60_000 if range_key == "session" else start
    gpu_bucket = RANGE_BUCKETS["1m"] if range_key == "session" else \
        RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
    dts["gpus"] = _gpu_series_sql_for_model(
        s, m.provider_id, model_id, gpu_start, now, gpu_bucket)
    cfgs = _model_config_projection(s, model_id)
    mtp_acc = (a.d_accepted / a.d_proposed * 100.0) if a.d_proposed > 0 else None
    avg_gen = (a.gen_tokens / a.gen_time) if a.gen_time > 0 else None
    avg_prompt = (a.prompt_tokens / a.prompt_time) if a.prompt_time > 0 else None
    detail_costs = compute_costs(m, a.prompt_tokens, a.gen_tokens)
    return {
        "model": {
            "id": m.id, "name": m.name, "key": m.key, "quant": m.quant,
            "family": m.family, "arch": m.arch, "params": m.params, "color": m.color,
            "first_seen": m.first_seen_at, "last_used": m.last_used_at,
            "provider": provider.name if provider else None,
            "live_state": _live_state(s, m.id, now),
            "live": _model_live(s, m.id, now),
            "catalog_available": m.catalog_available,
            "catalog_last_seen_at": m.catalog_last_seen_at,
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
        "sessions": sess_count,
        "tokens": {
            "prompt": round(a.prompt_tokens),
            "generated": round(a.gen_tokens),
            "total": round(a.tokens),
        },
        "costs": detail_costs,
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
        "config": cfgs[0] if cfgs else None,
        "configs": cfgs[:12],
        "graphs": dts,
        "now": now,
    }


def _count_sessions_in_range(s: Session, prov_ids: list[int], start_ms: int,
                             model_ids: Optional[list[int]] = None) -> int:
    """COUNT(*) form of sessions_in_range, identical predicates."""
    if not prov_ids:
        return 0
    q = select(func.count()).select_from(SessionRow).where(
        SessionRow.provider_id.in_(prov_ids),
        SessionRow.start_at <= now_ms(),
        (SessionRow.end_at.is_(None) | (SessionRow.end_at >= start_ms)),
    )
    if model_ids:
        q = q.where(SessionRow.model_id.in_(model_ids))
    return int(s.exec(q).one())


def _model_config_projection(s: Session, model_id: int) -> list[dict]:
    """Projected ModelConfig columns for the detail page (no payload text)."""
    rows = s.exec(
        select(ModelConfig.id, ModelConfig.fingerprint, ModelConfig.created_at,
               ModelConfig.context, ModelConfig.kv_cache_k, ModelConfig.kv_cache_v,
               ModelConfig.flash_attn, ModelConfig.parallel, ModelConfig.split_mode,
               ModelConfig.tensor_split, ModelConfig.gpu_layers, ModelConfig.cpu_moe,
               ModelConfig.threads, ModelConfig.batch, ModelConfig.ubatch,
               ModelConfig.reasoning, ModelConfig.reasoning_effort,
               ModelConfig.reasoning_preserve, ModelConfig.mmproj,
               ModelConfig.mtp_enabled, ModelConfig.mtp_model,
               ModelConfig.speculative)
        .where(ModelConfig.model_id == model_id)
        .order_by(ModelConfig.created_at.desc())).all()
    out = []
    for row in rows:
        out.append({
            "id": row[0], "fingerprint": row[1], "created_at": row[2],
            "context": row[3], "kv_cache_k": row[4], "kv_cache_v": row[5],
            "flash_attn": row[6], "parallel": row[7], "split_mode": row[8],
            "tensor_split": row[9], "gpu_layers": row[10], "cpu_moe": row[11],
            "threads": row[12], "batch": row[13], "ubatch": row[14],
            "reasoning": row[15], "reasoning_effort": row[16],
            "reasoning_preserve": row[17], "mmproj": row[18],
            "mtp_enabled": row[19], "mtp_model": row[20],
            "speculative": row[21], "payload": {},
        })
    return out


def _live_state(s: Session, model_id: int, now: int) -> str:
    last = s.exec(select(TelemetrySample).where(
        TelemetrySample.model_id == model_id).order_by(TelemetrySample.ts.desc())
        .limit(1)).first()
    if last and now - last.ts < 60_000:
        return last.state
    return "UNLOADED"


def range_detail(s: Session, provider_id: int, model_id: int, range_key: str,
                 samples: list[TelemetrySample], start: int, now: int) -> dict:
    """Adaptive-bucketed time series for model detail graphs.

    The ``samples`` argument is retained for callers/tests that pass ORM
    rows (e.g. session_detail); the Model Detail path calls the bounded SQL
    implementation via :func:`range_detail_sql`.
    """
    if range_key == "session":
        bucket = RANGE_BUCKETS["1m"]
        start = now - 60_000
        samples = [r for r in samples if r.ts >= start]
    else:
        bucket = RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
    return range_detail_sql(s, provider_id, model_id, bucket, start, now)


def range_detail_sql(s: Session, provider_id: int, model_id: int,
                     bucket_s: int, start: int, now: int) -> dict:
    """SQL-side adaptive bucketing for model detail graphs.

    Mirrors the Python chain it replaces: rows for one model ordered by ts
    form a consecutive chain; token deltas are positive-clamped with the
    prompt+gen fallback when the total delta is 0; phase seconds prefer
    counter deltas then divide the token delta by the observed tps; gauge
    averages are per-bucket sums with Python round(tot/n, 1) semantics and
    None for empty buckets; token/inference contributions are int()/round()
    per interval before accumulating.  Returns labels + the eight series
    exactly as before.
    """
    span = max(1, now - start)
    nb = max(2, int(span / 1000 / bucket_s))
    labels = [datetime.fromtimestamp((start + i * bucket_s * 1000) / 1000.0).strftime(
        "%H:%M" if bucket_s < 3600 else "%m-%d %H:%M") for i in range(nb)]
    series = {
        "gen_tps": [None] * nb, "prompt_tps": [None] * nb,
        "tokens": [0] * nb, "context": [None] * nb,
        "mtp_acc": [None] * nb, "inference_s": [0] * nb,
        "gpu_util": [None] * nb, "vram_mb": [None] * nb,
    }
    r = _py_round_sql
    # Gauges are summed per bucket over EVERY sample in the window (the
    # Python loop included the first sample); only token/inference deltas
    # need a predecessor row.  The two grouped results are combined with
    # UNION ALL because buckets can hold gauge-only or delta-only rows.
    gauge_cols = [("prompt_tps", "prompt_tps"), ("gen_tps", "gen_tps"),
                  ("mtp_acc", "mtp_acc"), ("context", "context_used"),
                  ("gpu_util", "gpu_util"), ("vram_mb", "vram_used_mb")]
    gauge_proj = ", ".join(f"{attr} AS {name}" for name, attr in gauge_cols)
    sum_exprs = ", ".join(f"SUM({name}) AS {name}_sum" for name, _ in gauge_cols)
    count_exprs = ", ".join(
        f"SUM(CASE WHEN {name} IS NOT NULL THEN 1 ELSE 0 END) AS {name}_n"
        for name, _ in gauge_cols)
    stmt_text = f"""
        WITH ordered AS (
            SELECT ts,
                   LAG(ts) OVER w AS prev_ts,
                   tokens_total, prompt_total, gen_total,
                   prompt_seconds_total, gen_seconds_total,
                   prompt_tps, gen_tps, mtp_acc,
                   context_used, gpu_util, vram_used_mb,
                   LAG(tokens_total) OVER w AS prev_tokens_total,
                   LAG(prompt_total) OVER w AS prev_prompt_total,
                   LAG(gen_total) OVER w AS prev_gen_total,
                   LAG(prompt_seconds_total) OVER w AS prev_prompt_seconds,
                   LAG(gen_seconds_total) OVER w AS prev_gen_seconds
            FROM telemetrysample INDEXED BY ix_telemetrysample_provider_ts
            WHERE provider_id = :provider_id
              AND model_id = :model_id
              AND ts >= :start_ms
            WINDOW w AS (ORDER BY ts)
        ), gauge_bucketed AS (
            SELECT MIN(:nb - 1, MAX(0, CAST((ts - :start_ms) / 1000.0
                       / :bucket_s AS INTEGER))) AS bucket_index,
                   {gauge_proj}
            FROM ordered
        ), gauge_agg AS (
            SELECT bucket_index,
                   {sum_exprs},
                   {count_exprs}
            FROM gauge_bucketed
            GROUP BY bucket_index
        ), deltas AS MATERIALIZED (
            SELECT ts,
                   CASE WHEN tokens_total > prev_tokens_total
                        THEN tokens_total - prev_tokens_total ELSE 0 END
                        AS token_delta,
                   CASE WHEN prompt_total > prev_prompt_total
                        THEN prompt_total - prev_prompt_total ELSE 0 END
                        AS prompt_delta,
                   CASE WHEN gen_total > prev_gen_total
                        THEN gen_total - prev_gen_total ELSE 0 END
                        AS gen_delta,
                   CASE WHEN prompt_total > prev_prompt_total THEN
                        CASE WHEN prompt_seconds_total > prev_prompt_seconds
                             THEN prompt_seconds_total - prev_prompt_seconds
                             WHEN prompt_tps IS NOT NULL AND prompt_tps > 0
                             THEN (prompt_total - prev_prompt_total) / prompt_tps
                             ELSE 0 END
                   ELSE 0 END AS prompt_phase_s,
                   CASE WHEN gen_total > prev_gen_total THEN
                        CASE WHEN gen_seconds_total > prev_gen_seconds
                             THEN gen_seconds_total - prev_gen_seconds
                             WHEN gen_tps IS NOT NULL AND gen_tps > 0
                             THEN (gen_total - prev_gen_total) / gen_tps
                             ELSE 0 END
                   ELSE 0 END AS gen_phase_s
            FROM ordered
            WHERE prev_ts IS NOT NULL
        ), delta_agg AS (
            SELECT MIN(:nb - 1, MAX(0, CAST((ts - :start_ms) / 1000.0
                       / :bucket_s AS INTEGER))) AS bucket_index,
                   SUM(CAST(CASE WHEN token_delta > 0 THEN token_delta
                                 ELSE prompt_delta + gen_delta END
                            AS INTEGER)) AS tokens_sum,
                   SUM({r("prompt_phase_s + gen_phase_s")}) AS inference_sum
            FROM deltas
            GROUP BY bucket_index
        )
        SELECT g.bucket_index,
               {", ".join(f"g.{name}_sum" for name, _ in gauge_cols)},
               {", ".join(f"g.{name}_n" for name, _ in gauge_cols)},
               d.tokens_sum, d.inference_sum
        FROM gauge_agg g LEFT JOIN delta_agg d ON d.bucket_index = g.bucket_index
        ORDER BY g.bucket_index
    """
    stmt = text(stmt_text)
    rows = s.execute(stmt, {"provider_id": provider_id, "model_id": model_id,
                            "start_ms": start, "nb": nb,
                            "bucket_s": bucket_s}).all()
    for row in rows:
        (idx, pt_sum, gt_sum, mtp_sum, ctx_sum, gu_sum, vm_sum,
         pt_n, gt_n, mtp_n, ctx_n, gu_n, vm_n, tokens_sum, inference_sum) = row
        i = int(idx)
        series["prompt_tps"][i] = round(pt_sum / pt_n, 1) if pt_n else None
        series["gen_tps"][i] = round(gt_sum / gt_n, 1) if gt_n else None
        series["mtp_acc"][i] = round(mtp_sum / mtp_n, 1) if mtp_n else None
        series["context"][i] = round(ctx_sum / ctx_n, 1) if ctx_n else None
        series["gpu_util"][i] = round(gu_sum / gu_n, 1) if gu_n else None
        series["vram_mb"][i] = round(vm_sum / vm_n, 1) if vm_n else None
        series["tokens"][i] += int(tokens_sum or 0)
        series["inference_s"][i] += int(inference_sum or 0)
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
    # Project the columns actually consumed instead of four mapped entities.
    # SessionRow alone has ~39 columns, and ModelConfig was hydrated in full for
    # every row purely so an optional filter could reference one of its fields.
    # Building 2,000 ORM objects to read a couple of dozen attributes was the
    # bulk of this endpoint's cost.
    q = (select(SessionRow.id, SessionRow.start_at, SessionRow.end_at,
                SessionRow.model_id, SessionRow.duration_s,
                SessionRow.prompt_tokens, SessionRow.gen_tokens,
                SessionRow.avg_gen_tps, SessionRow.prompt_tps,
                SessionRow.mtp_acc, SessionRow.mtp_enabled,
                SessionRow.context_max, SessionRow.status,
                SessionRow.live_seen_at, SessionRow.source_slot_id,
                SessionRow.source_task_id, SessionRow.live_prompt_tokens,
                SessionRow.live_gen_tokens, SessionRow.live_context,
                SessionRow.live_context_max, SessionRow.live_gen_tps,
                SessionRow.live_gen_tps_avg, SessionRow.live_gen_tps_3s,
                Model.name.label("model_name"), Model.color.label("model_color"),
                Model.quant.label("model_quant"),
                Provider.name.label("provider_name"))
         .join(Model, SessionRow.model_id == Model.id, isouter=True)
         .join(Provider, SessionRow.provider_id == Provider.id, isouter=True)
         .join(ModelConfig, SessionRow.config_id == ModelConfig.id, isouter=True)
         .where(SessionRow.start_at <= now))
    q = q.where((SessionRow.end_at.is_(None) | (SessionRow.end_at >= start)))
    if provider_id:
        q = q.where(SessionRow.provider_id == provider_id)
    if model_id:
        q = q.where(SessionRow.model_id == model_id)
    if quant:
        q = q.where(Model.quant == quant)
    if reasoning:
        q = q.where(ModelConfig.reasoning_effort == reasoning)
    if mtp in ("on", "off"):
        # SQL NULL fails this equality exactly like the historical Python
        # filter (rows with mtp_enabled IS NULL are skipped for on/off).
        q = q.where(SessionRow.mtp_enabled == (mtp == "on"))
    # Live sessions must reach the page even when 500 newer ones exist, which
    # is why ordering used to lead with a CASE over status and freshness. No
    # index can satisfy an ORDER BY on a parameterised expression, so SQLite
    # sorted every matching session in a temp B-tree before applying the limit
    # -- work that grew with the whole table rather than with the 500 rows
    # returned.
    #
    # Fetch the two groups separately instead. Live sessions are bounded by a
    # ten to fifteen second freshness window and are therefore few, and the
    # remainder orders by start_at alone, which an index can serve. The final
    # ordering is unchanged: the Python sort below already applies it.
    live_filter = or_(
        and_(SessionRow.status == "ACTIVE",
             SessionRow.live_seen_at.isnot(None),
             SessionRow.live_seen_at >= now - LIVE_FRESH_MS),
        and_(SessionRow.status == "FINALIZING",
             SessionRow.live_seen_at.isnot(None),
             SessionRow.live_seen_at >= now - FINALIZING_FRESH_MS),
    )
    # The isnot(None) guards keep the negation NULL-safe: a row with no
    # live_seen_at fails the live test cleanly and lands in the remainder.
    # id breaks ties on start_at. Sessions can share a start timestamp, and
    # the previous single-query ordering left those to whatever the temp
    # B-tree produced -- arbitrary, and free to differ between refreshes.
    live_rows = list(s.exec(q.where(live_filter)
                            .order_by(SessionRow.start_at.desc(),
                                      SessionRow.id.desc())
                            .limit(500)).all())
    rest_rows = list(s.exec(q.where(~live_filter)
                            .order_by(SessionRow.start_at.desc(),
                                      SessionRow.id.desc())
                            .limit(500)).all())
    rows = (live_rows + rest_rows)[:500]
    out = []
    for x in rows:
        out.append({
            "id": x.id, "start": x.start_at, "end": x.end_at,
            "provider": x.provider_name,
            "model": x.model_name, "model_id": x.model_id,
            "color": x.model_color, "quant": x.model_quant,
            "duration_s": x.duration_s,
            "prompt_tokens": round(x.prompt_tokens or 0),
            "gen_tokens": round(x.gen_tokens or 0),
            "avg_gen_tps": round(x.avg_gen_tps, 1) if x.avg_gen_tps else None,
            "prompt_tps": round(x.prompt_tps, 1) if x.prompt_tps else None,
            "mtp_acc": round(x.mtp_acc, 1) if x.mtp_acc is not None else None,
            "mtp_enabled": x.mtp_enabled,
            "context_max": x.context_max,
            "status": _effective_session_status(x, now),
            "live": _session_live(x, now),
        })
    out.sort(key=lambda item: (item["status"] not in ("ACTIVE", "FINALIZING"),
                               -item["start"], -item["id"]))
    return {"sessions": out, "now": now}


def session_detail(s: Session, session_id: int) -> dict:
    now = now_ms()
    x = s.get(SessionRow, session_id)
    if x is None:
        return {}
    m = s.get(Model, x.model_id) if x.model_id else None
    p = s.get(Provider, x.provider_id)
    cfg = s.get(ModelConfig, x.config_id) if x.config_id else None
    # Project only the columns the chart consumes.  Materializing full ORM
    # entities here builds a mapped object per sample purely to read ten
    # attributes off it.
    samples = list(s.exec(select(
        TelemetrySample.ts,
        TelemetrySample.gen_tps, TelemetrySample.prompt_tps,
        TelemetrySample.context_used, TelemetrySample.mtp_acc,
        TelemetrySample.gpu_util, TelemetrySample.vram_used_mb,
        TelemetrySample.tokens_total, TelemetrySample.prompt_total,
        TelemetrySample.gen_total,
    ).where(TelemetrySample.session_id == session_id)
     .order_by(TelemetrySample.ts)).all())
    start = x.start_at
    # A session with no end_at is not necessarily still running: stopping a
    # request from the dashboard leaves an INTERRUPTED session with no end
    # recorded, which is normal rather than damage. Treating those as running
    # to "now" stretched the window across every day since, charting hours of
    # nothing after the work actually finished. Only a session that is still
    # effectively live extends to now; anything terminal ends at the last
    # moment it was actually observed.
    end = x.end_at
    if end is None:
        if _effective_session_status(x, now) in ("ACTIVE", "FINALIZING"):
            end = now
        else:
            end = x.live_seen_at or (samples[-1].ts if samples else None) or start
    # An ACTIVE session has no end_at, so `end` is now: a session left open by a
    # crash or an interrupted provider spans hours or days, and a fixed bucket
    # would generate a point for every 2 s of it.
    bucket = session_detail_bucket_s(start, end)
    nb = max(2, min(SESSION_DETAIL_MAX_POINTS,
                    int((end - start) / 1000 / bucket)))
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
    label_fmt = "%H:%M:%S" if bucket < 60 else "%m-%d %H:%M"
    labels = [datetime.fromtimestamp((start + i * bucket * 1000) / 1000.0).strftime(label_fmt)
              for i in range(nb)]
    gpu_data = _gpu_series(
        _gpu_rows_for_session(s, x.provider_id, start, end, session_id),
        start, end, bucket,
    )
    live_info = _session_live(x, now)
    if live_info and series["gen_tps"]:
        series["gen_tps"][-1] = live_info.get("gen_tps")
        if live_info.get("context") is not None:
            series["context"][-1] = live_info["context"]
    sess_costs = compute_costs(m, x.prompt_tokens or 0.0, x.gen_tokens or 0.0) if m else {
        "input_price_per_million": None, "output_price_per_million": None,
        "input_cost": "0.00000000", "output_cost": "0.00000000",
        "total_cost": "0.00000000"}
    return {
        "session": {
            "id": x.id, "start": x.start_at, "end": x.end_at,
            "duration_s": x.duration_s, "status": _effective_session_status(x, now),
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
            "input_price_per_million": sess_costs["input_price_per_million"],
            "output_price_per_million": sess_costs["output_price_per_million"],
            "input_cost": sess_costs["input_cost"],
            "output_cost": sess_costs["output_cost"],
            "total_cost": sess_costs["total_cost"],
            "live": live_info,
            "gpus": [{k: gpu[k] for k in
                      ("key", "index", "uuid", "name", "label", "color",
                       "pcie", "vram_total_mb", "summary")}
                     for gpu in gpu_data],
        },
        "config": _config_out(cfg) if cfg else None,
        "graphs": {"labels": labels, "series": series, "gpus": gpu_data,
                   "span_s": round((end - start) / 1000.0)},
        "now": now,
    }


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
def compare_model_candidates(s: Session, provider_id: Optional[int],
                             range_key: str,
                             summary: Optional[RangeSummary] = None) -> list[dict]:
    summary = summary or range_summary(s, provider_id, range_key)
    providers = summary.providers
    models = summary.models
    acc = summary.acc
    session_counts = summary.session_counts
    active = summary.active
    provider_names = {provider.id: provider.name for provider in providers}
    prov_ids = list(provider_names)
    if not prov_ids:
        return []
    model_ids = [model.id for model in models]
    total_tokens = sum(item.tokens for item in acc.values())
    out = []
    for model in models:
        item = acc.get(model.id) or ModelAcc()
        live = active.get(model.id)
        if item.tokens <= 0 and session_counts.get(model.id, 0) <= 0 and live is None:
            continue
        inference = item.prompt_time + item.gen_time
        cand_costs = compute_costs(model, item.prompt_tokens, item.gen_tokens)
        out.append({
            "key": str(model.id), "label": model.name, "model_ids": [model.id],
            "color": model.color, "quant": model.quant, "family": model.family,
            "provider": provider_names.get(model.provider_id),
            "tokens": round(item.tokens),
            "share": round(item.tokens / total_tokens * 100.0, 1) if total_tokens else 0.0,
            "sessions": session_counts.get(model.id, 0),
            "gen_tps": round(item.gen_tokens / item.gen_time, 1) if item.gen_time > 0 else None,
            "peak_gen": round(item.peak_gen, 1) if item.peak_gen else None,
            "inference_s": round(inference), "loaded_s": round(item.loaded_time),
            "active": live is not None,
            "active_status": live.get("status") if live else None,
            "active_tasks": live.get("task_count", 0) if live else 0,
            "input_price_per_million": cand_costs["input_price_per_million"],
            "output_price_per_million": cand_costs["output_price_per_million"],
            "input_cost": cand_costs["input_cost"],
            "output_cost": cand_costs["output_cost"],
            "total_cost": cand_costs["total_cost"],
        })
    out.sort(key=lambda row: row["tokens"], reverse=True)
    return out


def compare_models(s: Session, keys: list[str], provider_id: Optional[int],
                   range_key: str, include_gpus: bool = False,
                   summary: Optional[RangeSummary] = None) -> dict:
    now = now_ms()
    start = range_start_ms(range_key, now)
    providers = list(s.exec(select(Provider)).all())
    if provider_id:
        providers = [p for p in providers if p.id == provider_id]
    prov_ids = [p.id for p in providers]
    models = list(s.exec(select(Model).where(Model.provider_id.in_(prov_ids))).all()) if prov_ids else []
    by_id = {str(model.id): model for model in models}
    selected_models = [by_id[key] for key in keys[:5] if key in by_id]
    selected_ids = [model.id for model in selected_models]
    all_acc = (summary.acc if summary else
               aggregate_range_summary(s, prov_ids, start, now, now,
                                       model_ids=selected_ids))
    all_configs = (list(s.exec(select(ModelConfig).where(
        ModelConfig.model_id.in_(selected_ids))).all()) if selected_ids else [])
    configs_by_model: dict[int, list[ModelConfig]] = {}
    for config in all_configs:
        configs_by_model.setdefault(config.model_id, []).append(config)
    provider_names = {provider.id: provider.name for provider in providers}
    builds = {provider.id: _build_str(_latest_build(s, provider.id)) for provider in providers}
    selected_provider_ids = {model.provider_id for model in selected_models}
    gpu_rows_by_provider = ({
        provider.id: _gpu_rows(s, provider.id, start, now) for provider in providers
        if provider.id in selected_provider_ids
    } if include_gpus else {})
    out = []
    for model in selected_models:
        key = str(model.id)
        family_models = [model]
        mids = [model.id]
        values = [all_acc.get(model.id) or ModelAcc()]
        prompt_tokens = sum(value.prompt_tokens for value in values)
        gen_tokens = sum(value.gen_tokens for value in values)
        uncl_tokens = sum(value.unclassified_tokens for value in values)
        prompt_time = sum(value.prompt_time for value in values)
        gen_time = sum(value.gen_time for value in values)
        loaded = sum(value.loaded_time for value in values)
        idle = sum(value.idle_time for value in values)
        proposed = sum(value.d_proposed for value in values)
        accepted = sum(value.d_accepted for value in values)
        inference = prompt_time + gen_time
        configs = configs_by_model.get(model.id, [])

        def mixed(attr: str):
            vals = {getattr(cfg, attr) for cfg in configs if getattr(cfg, attr) not in (None, "")}
            if not vals:
                return None
            return next(iter(vals)) if len(vals) == 1 else "mixed"

        gpu_rows = []
        for pid in sorted({model.provider_id for model in family_models}):
            rows = gpu_rows_by_provider.get(pid, [])
            gpu_rows.extend(row for row in rows
                            if set(_json_ids(row.active_model_ids)).intersection(mids))
        gpu_start = min((row.ts for row in gpu_rows), default=now) if start == 0 else start
        gpu_data = _gpu_series(gpu_rows, gpu_start, now,
                               max(2, RANGE_BUCKETS.get(range_key, 3600)))
        model_builds = {builds.get(pid) for pid in
                        {item.provider_id for item in family_models}}
        model_builds.discard(None)
        build = next(iter(model_builds)) if len(model_builds) == 1 else \
            ("mixed" if model_builds else None)
        variants = sorted({model.name for model in family_models})
        quants = sorted({model.quant for model in family_models if model.quant})
        _cmp_in_cost = Decimal("0")
        _cmp_out_cost = Decimal("0")
        _cmp_in_rates: set[str] = set()
        _cmp_out_rates: set[str] = set()
        for _fam_m in family_models:
            _fam_acc = all_acc.get(_fam_m.id) or ModelAcc()
            _c_in_r = Decimal(_fam_m.input_price_per_million) if _fam_m.input_price_per_million else Decimal("0")
            _c_out_r = Decimal(_fam_m.output_price_per_million) if _fam_m.output_price_per_million else Decimal("0")
            _cmp_in_cost += (Decimal(str(_fam_acc.prompt_tokens)) / Decimal("1000000")) * _c_in_r
            _cmp_out_cost += (Decimal(str(_fam_acc.gen_tokens)) / Decimal("1000000")) * _c_out_r
            _cmp_in_rates.add(_fam_m.input_price_per_million or "0")
            _cmp_out_rates.add(_fam_m.output_price_per_million or "0")
        cmp_costs = {
            "input_price_per_million": _cmp_in_rates.pop() if len(_cmp_in_rates) == 1 else None,
            "output_price_per_million": _cmp_out_rates.pop() if len(_cmp_out_rates) == 1 else None,
            "input_cost": format(_cmp_in_cost, ".8f"),
            "output_cost": format(_cmp_out_cost, ".8f"),
            "total_cost": format(_cmp_in_cost + _cmp_out_cost, ".8f"),
        }
        out.append({
            "key": key, "model": model.name, "color": model.color,
            "family": model.family, "quant": model.quant,
            "variants": variants, "quants": quants,
            "providers": sorted({provider_names[item.provider_id]
                                  for item in family_models if item.provider_id in provider_names}),
            "tokens": round(prompt_tokens + gen_tokens + uncl_tokens),
            "prompt_tokens": round(prompt_tokens), "gen_tokens": round(gen_tokens),
            "prompt_tps": round(prompt_tokens / prompt_time, 1) if prompt_time > 0 else None,
            "avg_gen_tps": round(gen_tokens / gen_time, 1) if gen_time > 0 else None,
            "peak_prompt_tps": round(max((value.peak_prompt for value in values), default=0), 1) or None,
            "peak_gen_tps": round(max((value.peak_gen for value in values), default=0), 1) or None,
            "inference_s": round(inference), "loaded_s": round(loaded), "idle_s": round(idle),
            "utilization": round(inference / loaded * 100.0, 1) if loaded > 0 else None,
            "context_max": max((value.context_max for value in values), default=0) or None,
            "mtp_proposed": round(proposed), "mtp_accepted": round(accepted),
            "mtp_rejected": round(max(0.0, proposed - accepted)),
            "mtp_acc": round(accepted / proposed * 100.0, 1) if proposed > 0 else None,
            "configuration": "mixed" if len({cfg.fingerprint for cfg in configs}) > 1 else
                              ("single" if configs else None),
            "kv_cache": mixed("kv_cache_k") if mixed("kv_cache_k") == "mixed" else
                        (f"{mixed('kv_cache_k') or '-'}/{mixed('kv_cache_v') or '-'}" if configs else None),
            "split_mode": mixed("split_mode"), "reasoning_effort": mixed("reasoning_effort"),
            "gpus": [{"index": gpu["index"], "label": gpu["label"],
                      "color": gpu["color"], "summary": gpu["summary"]}
                     for gpu in gpu_data],
            "build": build,
            "input_price_per_million": cmp_costs["input_price_per_million"],
            "output_price_per_million": cmp_costs["output_price_per_million"],
            "input_cost": cmp_costs["input_cost"],
            "output_cost": cmp_costs["output_cost"],
            "total_cost": cmp_costs["total_cost"],
        })
    return {"models": out, "range": range_key, "now": now}


def compare_model_gpus(s: Session, keys: list[str], provider_id: Optional[int],
                       range_key: str) -> dict:
    """Return only the per-GPU compare summaries, aggregated inside SQLite.

    The Compare table never consumes the GPU time series.  Building it used to
    deserialize every selected provider row and allocate thousands of chart
    buckets, which made this small UI decoration contend with the collector.
    """
    now = now_ms()
    start = range_start_ms(range_key, now)
    providers = list(s.exec(select(Provider)).all())
    if provider_id:
        providers = [provider for provider in providers if provider.id == provider_id]
    provider_ids = [provider.id for provider in providers]
    if not provider_ids:
        return {"gpus": {}, "range": range_key, "now": now}
    models = list(s.exec(select(Model).where(Model.provider_id.in_(provider_ids))).all())
    selected = [model for model in models if str(model.id) in keys[:5]]
    if not selected:
        return {"gpus": {}, "range": range_key, "now": now}

    selected_ids = [model.id for model in selected]
    statement = text("""
        SELECT CAST(active.value AS INTEGER) AS model_id,
               gpu.gpu_index AS gpu_index,
               MAX(gpu.gpu_uuid) AS gpu_uuid,
               MAX(gpu.name) AS name,
               MAX(gpu.pcie) AS pcie,
               MAX(gpu.vram_total_mb) AS vram_total_mb,
               AVG(gpu.util) AS util,
               AVG(gpu.vram_used_mb) AS vram_mb,
               AVG(gpu.temp_c) AS temp_c,
               AVG(gpu.power_w) AS power_w
        FROM gputelemetrysample AS gpu
        JOIN json_each(gpu.active_model_ids) AS active
        WHERE gpu.provider_id IN :provider_ids
          AND gpu.ts >= :start AND gpu.ts <= :end
          AND CAST(active.value AS INTEGER) IN :model_ids
        GROUP BY active.value, gpu.provider_id, gpu.gpu_index
        ORDER BY active.value, gpu.gpu_index
    """).bindparams(
        bindparam("provider_ids", expanding=True),
        bindparam("model_ids", expanding=True),
    )
    rows = s.exec(statement, params={
        "provider_ids": provider_ids, "model_ids": selected_ids,
        "start": start, "end": now,
    }).all()
    out = {str(model.id): [] for model in selected}
    for row in rows:
        value = row._mapping
        gpu_index = value["gpu_index"]
        gpu_uuid = value["gpu_uuid"]
        gpu_key = gpu_uuid or f"index:{gpu_index}"
        summary = {
            name: round(value[name], 1) if value[name] is not None else None
            for name in ("util", "vram_mb", "temp_c", "power_w")
        }
        out.setdefault(str(value["model_id"]), []).append({
            "index": gpu_index,
            "label": f"GPU {gpu_index} · {value['name'] or gpu_key}",
            "color": _gpu_color(gpu_key),
            "summary": summary,
        })
    return {"gpus": out, "range": range_key, "now": now}


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
            _gpu_rows_for_session(s, x.provider_id, x.start_at, end, x.id),
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
def _hardware_host_series_sql(s: Session, provider_id: int, start: int,
                              nb: int, bucket_s: int) -> dict[str, list]:
    """SQL-side 1h gauge bucketing for the Hardware host charts.

    Mirrors the Python loop it replaces: every sample in the window
    contributes its non-NULL gauges to the bucket computed as
    min(nb-1, max(0, int((ts - start) / 1000 / bucket_s))) — including
    future-stamped rows, which clamp into the last bucket exactly like the
    historical unbounded fetch.  Sums and per-metric non-NULL counts cross
    the SQL boundary (<= nb rows); the mean/round/None semantics stay in
    Python so float behavior is bit-identical to the previous chain.
    Returns the six series keyed as the API contract expects.
    """
    gauges = (("gpu_util", "gpu_util"), ("vram_mb", "vram_used_mb"),
              ("gpu_temp", "gpu_temp"), ("gpu_power", "gpu_power_w"),
              ("cpu_pct", "cpu_pct"), ("ram_mb", "ram_used_mb"))
    sum_exprs = ", ".join(f"SUM({attr}) AS {name}_sum" for name, attr in gauges)
    count_exprs = ", ".join(
        f"SUM(CASE WHEN {attr} IS NOT NULL THEN 1 ELSE 0 END) AS {name}_n"
        for name, attr in gauges)
    stmt_text = f"""
        SELECT MIN(:nb - 1, MAX(0, CAST((ts - :start_ms) / 1000.0
                   / :bucket_s AS INTEGER))) AS bucket_index,
               {sum_exprs},
               {count_exprs}
        FROM telemetrysample INDEXED BY ix_telemetrysample_provider_ts
        WHERE provider_id = :provider_id AND ts >= :start_ms
        GROUP BY bucket_index
    """
    rows = s.execute(text(stmt_text),
                     {"provider_id": provider_id, "start_ms": start,
                      "nb": nb, "bucket_s": bucket_s}).all()
    series = {name: [None] * nb for name, _ in gauges}
    for row in rows:
        i = int(row[0])
        for j, (name, _attr) in enumerate(gauges):
            tot = row[1 + j]
            n = row[1 + len(gauges) + j]
            series[name][i] = round(tot / n, 1) if n else None
    return series


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
        bucket = 30
        nb = max(2, 3600 // bucket)
        series = _hardware_host_series_sql(s, p.id, h1, nb, bucket)
        labels = [datetime.fromtimestamp((h1 + i * bucket * 1000) / 1000.0).strftime("%H:%M")
                  for i in range(nb)]
        import json as _json
        gpus = []
        if hw:
            try:
                gpus = _json.loads(hw.gpus or "[]")
            except ValueError:
                gpus = []
        gpu_data = _gpu_series(_gpu_series_rows(s, p.id, h1, now), h1, now, bucket)
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
            "status": effective_provider_status(p, now),
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
        p_status = effective_provider_status(p, now)
        agent_status = None
        if p.agent_url:
            agent_status = "LIVE" if p_status == "LIVE" else "OFFLINE"
        b = _latest_build(s, p.id)
        provs.append({
            "id": p.id, "name": p.name, "url": p.base_url, "status": p_status,
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
