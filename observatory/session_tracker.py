"""Passive session detection.

A session starts when external inference activity appears (token counters
increase) and ends when activity stops. No prompts are ever sent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .settings import SESSION_END_DELAY_S, SAMPLE_DT_MAX_S

DT_MIN_S = 0.05


@dataclass
class SessionStats:
    """Running totals for an active session."""
    prompt_tokens: float = 0.0
    gen_tokens: float = 0.0
    prompt_time_s: float = 0.0
    gen_time_s: float = 0.0
    peak_gen_tps: float = 0.0
    peak_prompt_tps: float = 0.0
    context_max: int = 0
    mtp_proposed: float = 0.0
    mtp_accepted: float = 0.0
    mtp_enabled: Optional[bool] = None
    gpu_util_sum: float = 0.0
    gpu_util_n: int = 0
    vram_used_mb_sum: float = 0.0
    vram_used_mb_n: int = 0
    ram_used_mb_sum: float = 0.0
    ram_used_mb_n: int = 0
    power_w_sum: float = 0.0
    power_w_n: int = 0


@dataclass
class LiveTaskState:
    """Ephemeral state for one llama.cpp slot task."""
    slot_id: int
    task_id: int
    session_id: int
    first_seen: float
    last_seen: float
    prompt_tokens: float = 0.0
    gen_tokens: float = 0.0
    context: Optional[int] = None
    speed_points: list[tuple[float, float]] = field(default_factory=list)
    finalizing_since: Optional[float] = None


@dataclass
class ProviderState:
    """Collector state for one (provider, model) pair across polls."""
    last_ts: Optional[float] = None
    prev: dict = field(default_factory=dict)      # last cumulative counters
    active_session_id: Optional[int] = None
    last_activity_ts: Optional[float] = None
    prompt_phase_start_ts: Optional[float] = None
    first_gen_ts: Optional[float] = None
    stats: Optional[SessionStats] = None
    model_key: Optional[str] = None
    model_id: Optional[int] = None
    config_id: Optional[int] = None
    config_fp: Optional[str] = None
    config_payload: dict = field(default_factory=dict)
    mtp_window: list = field(default_factory=list)
    was_loaded: bool = False
    metrics_fail: int = 0
    fail_streak: int = 0
    epoch: int = 0
    live_tasks: dict[str, LiveTaskState] = field(default_factory=dict)
    slots_available: Optional[bool] = None


def detect_state(health_status: Optional[str], d_prompt: float, d_gen: float,
                 has_model: bool) -> str:
    """Derive runtime state from health + counter deltas."""
    if not has_model:
        return "UNLOADED"
    hs = (health_status or "").lower()
    if hs in ("loading", "busy-loading"):
        return "LOADING"
    if hs in ("error",):
        return "UNLOADED"
    if d_gen > 1e-6:
        return "GENERATING"
    if d_prompt > 1e-6:
        return "PROMPTING"
    return "IDLE"


def clamp_dt(dt: float) -> float:
    if dt < DT_MIN_S:
        return DT_MIN_S
    return min(dt, SAMPLE_DT_MAX_S)


def counter_reset(prev: dict, cur: dict, keys: tuple) -> bool:
    """True if any cumulative counter decreased (server/model restart)."""
    for k in keys:
        a, b = prev.get(k), cur.get(k)
        if a is not None and b is not None and b < a - 1e-9:
            return True
    return False


def safe_delta(cur: dict, key: str, prev: dict) -> float:
    a, b = prev.get(key), cur.get(key)
    if a is None or b is None:
        return 0.0
    d = b - a
    return d if d > 0 else 0.0
