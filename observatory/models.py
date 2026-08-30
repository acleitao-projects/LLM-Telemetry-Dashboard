"""SQLModel table definitions. All timestamps are integer milliseconds (epoch)."""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def now_ms() -> int:
    return int(time.time() * 1000)


class Provider(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ptype: str = "llama.cpp"
    base_url: str
    agent_url: Optional[str] = None
    enabled: bool = True
    is_default: bool = False
    poll_interval_s: float = 1.0
    notes: str = ""
    created_at: int = Field(default_factory=now_ms)
    last_success_at: Optional[int] = None
    last_error: Optional[str] = None
    status: str = "OFFLINE"  # LIVE / STALE / OFFLINE
    latency_ms: Optional[float] = None
    fail_streak: int = 0


class Model(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(index=True, foreign_key="provider.id")
    key: str = Field(index=True)          # model id as reported by the server
    name: str                             # display name (file/alias)
    quant: Optional[str] = None
    family: Optional[str] = None
    arch: Optional[str] = None
    params: Optional[str] = None
    color: str = "#4b8de8"
    first_seen_at: int = Field(default_factory=now_ms)
    last_used_at: Optional[int] = None

    __table_args__ = {"extend_existing": True}


class ModelConfig(SQLModel, table=True):
    """One row per distinct observed launch configuration (history preserved)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(index=True, foreign_key="model.id")
    fingerprint: str = Field(index=True)   # short hash, e.g. A3F91C
    payload: str = "{}"                    # full observed config json
    context: Optional[int] = None
    kv_cache_k: Optional[str] = None
    kv_cache_v: Optional[str] = None
    flash_attn: Optional[bool] = None
    parallel: Optional[int] = None
    split_mode: Optional[str] = None
    tensor_split: Optional[str] = None
    gpu_layers: Optional[int] = None
    cpu_moe: Optional[int] = None
    threads: Optional[int] = None
    batch: Optional[int] = None
    ubatch: Optional[int] = None
    reasoning: Optional[str] = None
    reasoning_effort: Optional[str] = None
    reasoning_preserve: Optional[bool] = None
    mmproj: Optional[str] = None
    mtp_enabled: Optional[bool] = None
    mtp_model: Optional[str] = None
    speculative: Optional[str] = None
    created_at: int = Field(default_factory=now_ms)


class BuildInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(index=True, foreign_key="provider.id")
    version: Optional[str] = None
    commit: Optional[str] = None
    build_json: str = "{}"                 # raw build info json
    docker_image: Optional[str] = None
    container_id: Optional[str] = None
    first_seen_at: int = Field(default_factory=now_ms)
    last_seen_at: int = Field(default_factory=now_ms)


class HardwareInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(index=True, foreign_key="provider.id")
    hostname: Optional[str] = None
    os_name: Optional[str] = None
    kernel: Optional[str] = None
    cpu_model: Optional[str] = None
    cpu_threads: Optional[int] = None
    ram_mb: Optional[int] = None
    gpus: str = "[]"                       # json: [{name, vram_mb}]
    nvidia_driver: Optional[str] = None
    cuda: Optional[str] = None
    pcie: Optional[str] = None
    source: str = "agent"                  # agent | llama
    first_seen_at: int = Field(default_factory=now_ms)
    last_seen_at: int = Field(default_factory=now_ms)


class TelemetrySample(SQLModel, table=True):
    """One passive observation. Counter columns are cumulative server values;
    gauge columns are instantaneous values at scrape time."""
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(index=True, foreign_key="provider.id")
    model_id: Optional[int] = Field(default=None, index=True)
    ts: int = Field(index=True)
    state: str = "IDLE"                    # UNLOADED/LOADING/IDLE/PROMPTING/GENERATING
    tokens_total: Optional[float] = None
    prompt_total: Optional[float] = None
    gen_total: Optional[float] = None
    prompt_seconds_total: Optional[float] = None
    gen_seconds_total: Optional[float] = None
    mtp_proposed_total: Optional[float] = None
    mtp_accepted_total: Optional[float] = None
    prompt_tps: Optional[float] = None
    gen_tps: Optional[float] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    mtp_acc: Optional[float] = None        # % acceptance (rolling or per-step)
    gpu_util: Optional[float] = None
    vram_used_mb: Optional[float] = None
    vram_total_mb: Optional[float] = None
    gpu_temp: Optional[float] = None
    gpu_power_w: Optional[float] = None
    cpu_pct: Optional[float] = None
    ram_used_mb: Optional[float] = None
    power_w: Optional[float] = None
    session_id: Optional[int] = Field(default=None, index=True)
    extra: Optional[str] = None            # json of unmapped metrics

    __table_args__ = (
        UniqueConstraint("provider_id", "model_id", "ts", name="uq_sample_prov_model_ts"),
        Index("ix_telemetrysample_provider_ts", "provider_id", "ts"),
    )


class GpuTelemetrySample(SQLModel, table=True):
    """One host-level observation for one physical GPU."""
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(index=True, foreign_key="provider.id")
    ts: int = Field(index=True)
    gpu_key: str = Field(index=True)       # UUID when available, otherwise index:<n>
    gpu_index: int
    gpu_uuid: Optional[str] = None
    name: Optional[str] = None
    util: Optional[float] = None
    vram_used_mb: Optional[float] = None
    vram_total_mb: Optional[float] = None
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    pcie: Optional[str] = None
    active_model_ids: str = "[]"          # JSON array; host activity, not attribution
    active_session_ids: str = "[]"        # JSON array; host activity, not attribution

    __table_args__ = (
        UniqueConstraint("provider_id", "gpu_key", "ts",
                         name="uq_gpu_sample_prov_key_ts"),
    )


class SessionRow(SQLModel, table=True):
    """__tablename__ Session clashes with sqlmodel.Session, so alias."""
    __tablename__ = "session"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(index=True, foreign_key="provider.id")
    model_id: Optional[int] = Field(default=None, index=True)
    config_id: Optional[int] = Field(default=None, foreign_key="modelconfig.id")
    start_at: int = Field(index=True)
    end_at: Optional[int] = None
    duration_s: Optional[float] = None
    prompt_time_s: float = 0.0
    gen_time_s: float = 0.0
    prompt_tokens: float = 0.0
    gen_tokens: float = 0.0
    total_tokens: float = 0.0
    prompt_tps: Optional[float] = None
    avg_gen_tps: Optional[float] = None
    peak_gen_tps: Optional[float] = None
    peak_prompt_tps: Optional[float] = None
    ttft_s: Optional[float] = None
    context_max: Optional[int] = None
    mtp_enabled: Optional[bool] = None
    mtp_proposed: Optional[float] = None
    mtp_accepted: Optional[float] = None
    mtp_acc: Optional[float] = None
    gpu_util_avg: Optional[float] = None
    vram_used_mb: Optional[float] = None
    ram_used_mb: Optional[float] = None
    power_w: Optional[float] = None
    status: str = "ACTIVE"                 # ACTIVE / FINALIZING / CLOSED / INTERRUPTED / INCOMPLETE
    source_slot_id: Optional[int] = Field(default=None, index=True)
    source_task_id: Optional[int] = Field(default=None, index=True)
    live_prompt_tokens: Optional[float] = None
    live_gen_tokens: Optional[float] = None
    live_context: Optional[int] = None
    live_context_max: Optional[int] = None
    live_gen_tps: Optional[float] = None
    live_gen_tps_avg: Optional[float] = None
    live_gen_tps_3s: Optional[float] = None
    live_seen_at: Optional[int] = Field(default=None, index=True)
    result_source: Optional[str] = None      # metrics / slots / incomplete
    created_at: int = Field(default_factory=now_ms)


class CollectorLease(SQLModel, table=True):
    """Single-writer lease for one SQLite database."""
    __tablename__ = "collectorlease"

    key: str = Field(primary_key=True)
    owner_id: str = Field(index=True)
    heartbeat_at: int = Field(index=True)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
