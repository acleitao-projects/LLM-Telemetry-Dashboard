"""Background passive collection loop.

Polls each enabled provider's read-only endpoints, derives runtime state,
detects sessions, stores telemetry samples and handles counter resets.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Callable, Optional

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, delete, select

from . import database as db
from .llama_provider import (AgentClient, config_fingerprint,
                             extract_config, map_metrics)
from .metrics import next_model_color, parse_model_name
from .models import (BuildInfo, CollectorLease, GpuTelemetrySample, HardwareInfo, Model,
                     ModelConfig, Provider, SessionRow, TelemetrySample, now_ms)
from .settings import (AGENT_POLL_S, BUCKET_FULL_S, BUCKET_MID_S, LLAMA_POLL_S,
                        MODELS_POLL_S, PROPS_POLL_S, QUANT_TOKENS, RETENTION_FULL_S,
                        RETENTION_MID_S, RETENTION_RAW_S, RETENTION_SWEEP_S,
                        SESSION_END_DELAY_S, SAMPLE_DT_MAX_S)
from .session_tracker import (LiveTaskState, ProviderState, SessionStats, counter_reset,
                              detect_state, safe_delta)

log = logging.getLogger("observatory.collector")

COUNTER_KEYS = ("tokens_total", "prompt_total", "gen_total",
                "prompt_seconds_total", "gen_seconds_total",
                "mtp_proposed_total", "mtp_accepted_total")

MTP_WINDOW_S = 30.0
LIVE_FINALIZE_GRACE_S = 10.0
LIVE_SPEED_WINDOW_S = 8.0
LIVE_SHORT_WINDOW_S = 3.0
LEASE_STALE_MS = 10_000
LEASE_RETRY_S = 2.0


def _phase_duration(token_delta: float, seconds_delta: float,
                    throughput: Optional[float]) -> float:
    """Return observed work time without treating scrape time as inference time."""
    if token_delta <= 0:
        return 0.0
    if seconds_delta > 0:
        return seconds_delta
    if throughput is not None and throughput > 0:
        return token_delta / throughput
    return 0.0


def _slot_snapshots(payload: object) -> list[dict]:
    """Extract only the numeric live fields LLM-Telemetry is allowed to retain."""
    out: list[dict] = []
    if not isinstance(payload, list):
        return out
    for raw in payload:
        if not isinstance(raw, dict) or not raw.get("is_processing"):
            continue
        slot_id = _opt_int(raw.get("id"))
        task_id = _opt_int(raw.get("id_task"))
        if slot_id is None or task_id is None:
            continue
        nxt = raw.get("next_token")
        nxt0 = nxt[0] if isinstance(nxt, list) and nxt and isinstance(nxt[0], dict) else {}
        out.append({
            "slot_id": slot_id,
            "task_id": task_id,
            "prompt_tokens": float(_opt_int(raw.get("n_prompt_tokens_processed")) or 0),
            "gen_tokens": float(_opt_int(nxt0.get("n_decoded")) or 0),
            "context": _opt_int(raw.get("n_prompt_tokens")),
            "context_max": _opt_int(raw.get("n_ctx")),
        })
    return out


def parse_cmdline(argv: list[str]) -> dict:
    """Parse a llama.cpp server command line into a config dict (read-only)."""
    cfg: dict = {}
    flags: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):
            flags.append(a)
            nxt = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else None
            v = nxt
            if a in ("-m", "--model"):
                cfg["model_path"] = v
            elif a in ("-c", "--ctx-size", "--context-size"):
                cfg["context"] = _to_int(v)
            elif a in ("-ngl", "--n-gpu-layers", "--gpu-layers"):
                cfg["gpu_layers"] = _to_int(v)
            elif a in ("-t", "--threads"):
                cfg["threads"] = _to_int(v)
            elif a in ("-tb", "--threads-batch"):
                cfg["threads_batch"] = _to_int(v)
            elif a in ("-b", "--batch-size", "--batch"):
                cfg["batch"] = _to_int(v)
            elif a in ("-ub", "--ubatch-size", "--ubatch"):
                cfg["ubatch"] = _to_int(v)
            elif a in ("-np", "--parallel"):
                cfg["parallel"] = _to_int(v)
            elif a in ("-fa", "--flash-attn", "--flash"):
                cfg["flash_attn"] = True if v is None else v.lower() not in ("0", "false", "off", "no")
            elif a == "--split-mode":
                cfg["split_mode"] = v
            elif a == "--tensor-split":
                cfg["tensor_split"] = v
            elif a == "--cache-type-k":
                cfg["kv_cache_k"] = v
            elif a == "--cache-type-v":
                cfg["kv_cache_v"] = v
            elif a == "--cpu-moe":
                cfg["cpu_moe"] = _to_int(v)
            elif a in ("-r", "--reasoning-format", "--reasoning"):
                cfg["reasoning"] = v
            elif a == "--reasoning-effort":
                cfg["reasoning_effort"] = v
            elif a == "--reasoning-preserve":
                cfg["reasoning_preserve"] = True
            elif a in ("--mmproj", "--mmproj-path"):
                cfg["mmproj"] = v
            elif a == "--mtp":
                cfg["mtp_enabled"] = True if v is None else v.lower() not in ("0", "false", "off", "no")
            elif a in ("--mtp-model", "--draft-model"):
                cfg["mtp_model"] = v
            elif a in ("--draft-max", "--draft-max-tokens"):
                cfg["draft_max"] = _to_int(v)
            elif a == "--speculative":
                cfg["speculative"] = v
            elif a == "--port":
                cfg["port"] = v
        i += 1
    if flags:
        cfg["flags_raw"] = flags
    return cfg


def _to_int(v):
    if v is None:
        return None
    try:
        return int(str(v).split(".")[0])
    except (ValueError, TypeError):
        return v


def _model_entries(mlist: list) -> list[dict]:
    """Normalize a /v1/models response into per-model entries.

    Multi-model router: every entry carries status.value (loaded/unloaded),
    status.args (llama-server cmdline) and meta (vocab/params/ftype).
    Classic llama-server / demo: a single entry without status, in which
    case the first entry is the active (loaded) model.
    """
    entries: list[dict] = []
    has_status = False
    for raw in mlist or []:
        raw = raw or {}
        st = raw.get("status") or {}
        val = st.get("value")
        if val:
            has_status = True
        key = raw.get("id") or raw.get("name")
        if not key:
            continue
        entries.append({
            "key": key,
            "loaded": (val == "loaded"),
            "args": st.get("args") or [],
            "meta": raw.get("meta") or {},
        })
    if not has_status:
        for i, e in enumerate(entries):
            e["loaded"] = (i == 0)
    return entries


def model_entry_config(entry: dict) -> dict:
    """Build a config dict from a router /v1/models entry (args + meta)."""
    cfg: dict = {}
    args = entry.get("args") or []
    if args:
        cfg = parse_cmdline(args)
        cfg.pop("flags_raw", None)
        cfg["flags_raw"] = args
    meta = entry.get("meta") or {}
    if meta.get("n_params"):
        cfg.setdefault("params", meta.get("n_params"))
    if meta.get("n_ctx"):
        cfg.setdefault("context", meta.get("n_ctx"))
    if meta.get("ftype"):
        cfg["ftype"] = meta.get("ftype")
    return cfg


def quant_from_ftype(ftype: str) -> Optional[str]:
    """Map a ggml ftype string (e.g. 'Q4_K - Medium') to a quant token."""
    if not ftype:
        return None
    parts = [p.strip() for p in str(ftype).split(" - ")]
    base = parts[0] if parts else ""
    qual = parts[1] if len(parts) > 1 else ""
    candidates = []
    ql = qual.lower()
    if ql.startswith("m"):
        candidates.append(base + "_M")
    elif ql.startswith("s"):
        candidates.append(base + "_S")
    elif ql.startswith(("l", "h")):
        candidates.append(base + "_L")
    candidates.append(base)
    for cand in candidates:
        if cand.upper() in QUANT_TOKENS:
            return cand.upper()
    low = base.lower()
    for tok in QUANT_TOKENS:  # longest first
        if low and low in tok.lower():
            return tok
    return None


class Collector:
    def __init__(self, make_client: Callable[[Provider], object],
                 make_agent: Optional[Callable[[Provider], object]] = None):
        self.make_client = make_client
        self.make_agent = make_agent
        self.states: dict[int, ProviderState] = {}  # per-provider bookkeeping
        self.model_states: dict[tuple[int, str], ProviderState] = {}  # per (provider, model)
        self.clients: dict[int, object] = {}
        self.agents: dict[int, object] = {}
        self._model_cache: dict[int, list] = {}
        self._last_poll: dict[int, float] = {}
        self._last_props: dict[int, float] = {}
        self._last_models: dict[int, float] = {}
        self._last_agent: dict[int, float] = {}
        self._last_retention = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        self.owner_id = str(uuid.uuid4())
        self.role = "standby"
        self._last_lease_attempt = 0.0

    # ------------------------------------------------------------------ life
    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        for c in list(self.clients.values()):
            try:
                c.close()
            except Exception:
                pass
        self._release_lease()

    def lease_status(self) -> dict:
        return {"role": self.role, "owner_id": self.owner_id if self.role == "active" else None}

    def endpoint_status(self) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for (provider_id, _), state in self.model_states.items():
            item = out.setdefault(provider_id, {"slots": None})
            if state.slots_available is True:
                item["slots"] = True
            elif item["slots"] is None and state.slots_available is False:
                item["slots"] = False
        return out

    def _ensure_lease(self, now: float) -> bool:
        if self.role != "active" and now - self._last_lease_attempt < LEASE_RETRY_S:
            return False
        self._last_lease_attempt = now
        heartbeat = int(now * 1000)
        cutoff = heartbeat - LEASE_STALE_MS
        with db.new_session() as s:
            stmt = sqlite_insert(CollectorLease).values(
                key="collector", owner_id=self.owner_id, heartbeat_at=heartbeat,
            ).on_conflict_do_update(
                index_elements=[CollectorLease.key],
                set_={"owner_id": self.owner_id, "heartbeat_at": heartbeat},
                where=((CollectorLease.owner_id == self.owner_id) |
                       (CollectorLease.heartbeat_at < cutoff)),
            )
            s.exec(stmt)
            s.commit()
            row = s.get(CollectorLease, "collector")
            acquired = bool(row and row.owner_id == self.owner_id)
        self.role = "active" if acquired else "standby"
        return acquired

    def _release_lease(self):
        if self.role != "active":
            return
        try:
            with db.new_session() as s:
                row = s.get(CollectorLease, "collector")
                if row and row.owner_id == self.owner_id:
                    s.delete(row)
                    s.commit()
        except Exception:
            log.exception("failed releasing collector lease")
        self.role = "standby"

    # ----------------------------------------------------------------- loop
    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception:
                log.exception("collector tick failed")
            elapsed = time.time() - t0
            time.sleep(max(0.2, min(0.6, 0.6 - elapsed)))
            if t0 - self._last_retention > RETENTION_SWEEP_S:
                self._last_retention = t0
                try:
                    run_retention()
                except Exception:
                    log.exception("retention sweep failed")

    def _tick(self):
        now = time.time()
        if not self._ensure_lease(now):
            return
        with db.new_session() as s:
            providers = s.exec(select(Provider).order_by(Provider.id)).all()
            pids = {p.id for p in providers if p.enabled}
            for pid in list(self.states):
                if pid not in pids:
                    self._drop_provider(pid)
            for p in providers:
                if not p.enabled:
                    continue
                st = self.states.setdefault(p.id, ProviderState())
                interval = max(0.25, float(p.poll_interval_s or LLAMA_POLL_S))
                if now - self._last_poll.get(p.id, 0) < interval:
                    continue
                self._last_poll[p.id] = now
                try:
                    self._poll(p, st, now)
                except Exception as e:
                    self._poll_fail(p, st, e)

    def _drop_provider(self, pid: int):
        ts = int(time.time() * 1000)
        try:
            with db.new_session() as s:
                for (pid2, _mk), st_m in list(self.model_states.items()):
                    if pid2 != pid:
                        continue
                    if st_m.live_tasks:
                        self._end_live_tasks(s, st_m, ts, "INTERRUPTED")
                    elif st_m.active_session_id:
                        self._close_session(s, st_m, ts)
        except Exception:
            log.exception("failed closing sessions on provider drop")
        self.states.pop(pid, None)
        for k in [k for k in self.model_states if k[0] == pid]:
            del self.model_states[k]
        c = self.clients.pop(pid, None)
        if c:
            try:
                c.close()
            except Exception:
                pass
        a = self.agents.pop(pid, None)
        if a:
            try:
                a.close()
            except Exception:
                pass
        self._model_cache.pop(pid, None)

    # --------------------------------------------------------------- clients
    def _client_for(self, p: Provider):
        c = self.clients.get(p.id)
        if c is None or getattr(c, "base_url", None) != (p.base_url or "").rstrip("/"):
            if c:
                try:
                    c.close()
                except Exception:
                    pass
            c = self.make_client(p)
            self.clients[p.id] = c
        return c

    def _agent_for(self, p: Provider):
        if not p.agent_url:
            a = self.agents.pop(p.id, None)
            if a:
                try:
                    a.close()
                except Exception:
                    pass
            return None
        a = self.agents.get(p.id)
        if a is None or getattr(a, "base_url", None) != p.agent_url.rstrip("/"):
            if a:
                try:
                    a.close()
                except Exception:
                    pass
            a = self.make_agent(p) if self.make_agent else AgentClient(p.agent_url)
            self.agents[p.id] = a
        return a

    # ------------------------------------------------------------------ poll
    def _poll(self, p: Provider, st: ProviderState, now: float):
        client = self._client_for(p)
        t0 = time.time()
        health = client.health()

        props: dict = {}
        if now - self._last_props.get(p.id, 0) >= PROPS_POLL_S:
            try:
                props = client.props() or {}
            except Exception:
                props = {}
            self._last_props[p.id] = now
        if now - self._last_models.get(p.id, 0) >= MODELS_POLL_S:
            try:
                self._model_cache[p.id] = client.models() or []
            except Exception:
                pass
            self._last_models[p.id] = now
        entries = _model_entries(self._model_cache.get(p.id) or [])
        if not entries:
            mi = props.get("model_info") or {}
            name = mi.get("general_name") or mi.get("name")
            if name:
                entries = [{"key": name, "loaded": True, "args": [], "meta": {}}]

        # agent (hardware / build / launch flags)
        agent = self._agent_for(p)
        agent_data: dict = {}
        if agent and now - self._last_agent.get(p.id, 0) >= AGENT_POLL_S:
            self._last_agent[p.id] = now
            try:
                agent_data = {
                    "info": agent.info(),
                    "gpu": agent.gpu(),
                    "llama": agent.llama(),
                }
            except Exception:
                agent_data = {}
        fake_agent = getattr(client, "agent_snapshot", None)
        if callable(fake_agent):
            try:
                extra = fake_agent() or {}
                if extra:
                    agent_data = {**agent_data, **extra}
            except Exception:
                pass

        ts_ms = int(now * 1000)
        with db.new_session() as s:
            provider = s.get(Provider, p.id)
            if provider is None:
                return
            known_keys = {e["key"] for e in entries}
            loaded_count = 0
            metrics_ok = 0
            metrics_errors: list[str] = []
            for entry in entries:
                st_m = self.model_states.get((provider.id, entry["key"]))
                if entry["loaded"]:
                    loaded_count += 1
                    error = self._poll_model(s, provider, entry, props, st_m, client,
                                             health, agent_data, ts_ms, now)
                    if error:
                        metrics_errors.append(error)
                    else:
                        metrics_ok += 1
                elif st_m is not None and (st_m.was_loaded or st_m.active_session_id):
                    self._unload_model(s, provider, entry, st_m, ts_ms)
            for k in [k for k in self.model_states
                      if k[0] == provider.id and k[1] not in known_keys]:
                st_m = self.model_states.pop(k)
                if st_m.live_tasks:
                    self._end_live_tasks(s, st_m, ts_ms, "INCOMPLETE")
                elif st_m.active_session_id:
                    self._close_session(s, st_m, ts_ms)
            if loaded_count and metrics_ok == 0:
                raise RuntimeError("; ".join(metrics_errors) or
                                   "metrics unavailable for every loaded model")
            if props:
                self._store_router_props(s, provider, props)
            if agent_data.get("info"):
                self._store_agent(s, provider, agent_data, ts_ms)

            latency = (time.time() - t0) * 1000.0
            provider.last_success_at = ts_ms
            provider.latency_ms = round(latency, 1)
            provider.fail_streak = 0
            provider.status = "LIVE"
            provider.last_error = "; ".join(metrics_errors)[:300] if metrics_errors else None
            s.commit()

        st.last_ts = now

    def _poll_fail(self, p: Provider, st: ProviderState, err: Exception):
        st.fail_streak += 1
        st.last_ts = None
        st.prev = {}
        st.mtp_window = []
        now_ms_v = int(time.time() * 1000)
        with db.new_session() as s:
            provider = s.get(Provider, p.id)
            if provider is None:
                return
            last = provider.last_success_at
            if last and (now_ms_v - last) < 20_000:
                provider.status = "STALE"
            else:
                provider.status = "OFFLINE"
                for (pid2, _mk), st_m in list(self.model_states.items()):
                    if pid2 != p.id:
                        continue
                    if st_m.live_tasks:
                        self._end_live_tasks(s, st_m, now_ms_v, "INTERRUPTED")
                    elif st_m.active_session_id:
                        self._close_session(s, st_m, now_ms_v)
            provider.fail_streak = st.fail_streak
            provider.last_error = str(err)[:300]
            s.commit()
        log.warning("provider %s poll failed: %s", p.name, err)

    # ------------------------------------------------------------- per-model
    def _poll_model(self, s: Session, provider: Provider, entry: dict, props: dict,
                    st_m: ProviderState, client, health: dict, agent_data: dict,
                    ts_ms: int, now: float) -> Optional[str]:
        """One scrape for a loaded model: /metrics?model=<key> + samples."""
        mk = entry["key"]
        if st_m is None:
            st_m = self.model_states[(provider.id, mk)] = ProviderState()

        try:
            raw_metrics = client.metrics(mk)
        except Exception as e:
            st_m.metrics_fail += 1
            return f"metrics failed for {mk}: {str(e)[:160]}"
        st_m.metrics_fail = 0
        mapped = map_metrics(raw_metrics)

        model = self._upsert_model_entry(s, provider, entry, props)
        st_m.model_key = mk
        st_m.model_id = model.id
        st_m.was_loaded = True

        # configuration (history preserving)
        cfg = model_entry_config(entry)
        if not cfg:
            cfg = extract_config(props) if props else {}
        if agent_data.get("llama"):
            ll = agent_data["llama"] or {}
            argv = ll.get("argv") or []
            if argv:
                cmd_cfg = parse_cmdline(argv)
                cmd_cfg.pop("flags_raw", None)
                merged = {**cfg, **{k: v for k, v in cmd_cfg.items() if v is not None}}
                if ll.get("argv"):
                    merged["flags_raw"] = argv
                cfg = merged
        if cfg:
            self._upsert_model_config(s, st_m, model, cfg)

        slots: list[dict] = []
        try:
            slots = _slot_snapshots(client.slots(mk))
            st_m.slots_available = True
            self._sync_live_tasks(s, provider, st_m, cfg, slots, ts_ms, now)
        except Exception:
            st_m.slots_available = False

        # counters / resets
        cur = {k: mapped.get(k) for k in COUNTER_KEYS}
        reset = st_m.last_ts is not None and counter_reset(st_m.prev, cur, COUNTER_KEYS)
        d_prompt = safe_delta(cur, "prompt_total", st_m.prev)
        d_gen = safe_delta(cur, "gen_total", st_m.prev)
        d_prompt_s = safe_delta(cur, "prompt_seconds_total", st_m.prev)
        d_gen_s = safe_delta(cur, "gen_seconds_total", st_m.prev)
        d_prop = safe_delta(cur, "mtp_proposed_total", st_m.prev)
        d_acc = safe_delta(cur, "mtp_accepted_total", st_m.prev)

        prompt_tps = mapped.get("prompt_tps")
        gen_tps = mapped.get("gen_tps")
        prompt_duration = _phase_duration(d_prompt, d_prompt_s, prompt_tps)
        gen_duration = _phase_duration(d_gen, d_gen_s, gen_tps)
        if prompt_tps is None and prompt_duration > 0:
            prompt_tps = d_prompt / prompt_duration
        if gen_tps is None and gen_duration > 0:
            gen_tps = d_gen / gen_duration

        active_live = [task for task in st_m.live_tasks.values()
                       if task.finalizing_since is None]
        slots_busy = bool(active_live) or float(mapped.get("slots_processing") or 0) > 0
        if active_live:
            state = "GENERATING" if any(task.gen_tokens > 0 for task in active_live) else "PROMPTING"
        elif slots_busy:
            state = "GENERATING"
        else:
            state = detect_state((health or {}).get("status"), d_prompt, d_gen, True)
        activity = d_prompt > 0 or d_gen > 0
        if slots_busy and not active_live and not st_m.active_session_id and st_m.model_id:
            sess = SessionRow(
                provider_id=provider.id, model_id=st_m.model_id,
                config_id=st_m.config_id, start_at=ts_ms, status="ACTIVE",
                live_seen_at=ts_ms, mtp_enabled=bool(cfg.get("mtp_enabled")),
            )
            s.add(sess)
            s.commit()
            s.refresh(sess)
            st_m.active_session_id = sess.id
            st_m.stats = SessionStats()
        if slots_busy and st_m.active_session_id:
            busy_session = s.get(SessionRow, st_m.active_session_id)
            if busy_session:
                busy_session.live_seen_at = ts_ms
                s.add(busy_session)
                s.commit()
            st_m.last_activity_ts = now
        # sessions
        if reset:
            if st_m.active_session_id and not st_m.live_tasks:
                self._close_session(s, st_m, int((st_m.last_ts or now) * 1000))
            st_m.epoch += 1
            activity = False
        if st_m.active_session_id and not activity and not slots_busy and \
                not st_m.live_tasks and \
                st_m.last_activity_ts and \
                (now - st_m.last_activity_ts) > SESSION_END_DELAY_S:
            self._close_session(s, st_m, int(st_m.last_activity_ts * 1000))
        if st_m.active_session_id and not reset and st_m.last_ts and \
                (now - st_m.last_ts) > SAMPLE_DT_MAX_S:
            self._close_session(s, st_m, int(st_m.last_ts * 1000))
        st_m.prev = dict(cur)

        if activity and st_m.model_id:
            if not st_m.active_session_id:
                sess = SessionRow(
                    provider_id=provider.id, model_id=st_m.model_id, config_id=st_m.config_id,
                    start_at=ts_ms, status="ACTIVE",
                    live_seen_at=ts_ms,
                    mtp_enabled=bool(cfg.get("mtp_enabled")) or (d_prop > 0),
                )
                s.add(sess)
                s.commit()
                s.refresh(sess)
                st_m.active_session_id = sess.id
                st_m.stats = SessionStats()
                st_m.first_gen_ts = None
                st_m.prompt_phase_start_ts = None
            stats = st_m.stats or SessionStats()
            st_m.stats = stats
            live_row = s.get(SessionRow, st_m.active_session_id)
            if live_row:
                live_row.live_seen_at = ts_ms
                s.add(live_row)
            stats.prompt_tokens += d_prompt
            stats.gen_tokens += d_gen
            if d_prompt > 0:
                stats.prompt_time_s += prompt_duration
                if st_m.prompt_phase_start_ts is None:
                    st_m.prompt_phase_start_ts = now
                if prompt_tps is not None and prompt_tps > 0:
                    stats.peak_prompt_tps = max(stats.peak_prompt_tps, prompt_tps)
            if d_gen > 0:
                stats.gen_time_s += gen_duration
                if st_m.first_gen_ts is None:
                    st_m.first_gen_ts = now
                if gen_tps is not None and gen_tps > 0:
                    stats.peak_gen_tps = max(stats.peak_gen_tps, gen_tps)
            ctx = _opt_int(mapped.get("context_used"))
            if ctx is not None:
                stats.context_max = max(stats.context_max, int(ctx))
            stats.mtp_proposed += d_prop
            stats.mtp_accepted += d_acc
            if d_prop > 0:
                stats.mtp_enabled = True
            for attr, val in (("gpu_util", mapped.get("gpu_util")),
                              ("vram_used_mb", agent_gpu_val(agent_data, "vram_used_mb")),
                              ("ram_used_mb", agent_val(agent_data, "ram_used_mb")),
                              ("power_w", agent_val(agent_data, "power_w"))):
                if val is not None:
                    setattr(stats, attr + "_sum", getattr(stats, attr + "_sum") + val)
                    setattr(stats, attr + "_n", getattr(stats, attr + "_n") + 1)
            st_m.last_activity_ts = now

            ttft = None
            if st_m.first_gen_ts and st_m.prompt_phase_start_ts:
                ttft = max(0.0, st_m.first_gen_ts - st_m.prompt_phase_start_ts)
            macc = (stats.mtp_accepted / stats.mtp_proposed * 100.0) if stats.mtp_proposed > 0 else None
            self._update_session_row(s, st_m, ttft, macc)
            observed_session = s.get(SessionRow, st_m.active_session_id)
            if observed_session:
                observed_session.result_source = "metrics"
                s.add(observed_session)
                s.commit()
            finalizing = [key for key, task in st_m.live_tasks.items()
                          if task.finalizing_since is not None and
                          task.session_id == st_m.active_session_id]
            if len(finalizing) == 1 and not active_live:
                sess = s.get(SessionRow, st_m.active_session_id)
                if sess:
                    sess.result_source = "metrics"
                    s.add(sess)
                    s.commit()
                self._close_session(s, st_m, ts_ms)
                st_m.live_tasks.pop(finalizing[0], None)

        # rolling MTP acceptance window
        st_m.mtp_window.append((now, d_prop, d_acc))
        st_m.mtp_window = [(t, a, b) for (t, a, b) in st_m.mtp_window if now - t <= MTP_WINDOW_S]
        w_prop = sum(a for (_, a, _) in st_m.mtp_window)
        w_acc = sum(b for (_, _, b) in st_m.mtp_window)
        mtp_acc_sample = (w_acc / w_prop * 100.0) if w_prop > 0 else None

        sample = TelemetrySample(
            provider_id=provider.id, model_id=st_m.model_id, ts=ts_ms, state=state,
            tokens_total=mapped.get("tokens_total"),
            prompt_total=mapped.get("prompt_total"),
            gen_total=mapped.get("gen_total"),
            prompt_seconds_total=mapped.get("prompt_seconds_total"),
            gen_seconds_total=mapped.get("gen_seconds_total"),
            mtp_proposed_total=mapped.get("mtp_proposed_total"),
            mtp_accepted_total=mapped.get("mtp_accepted_total"),
            prompt_tps=prompt_tps, gen_tps=gen_tps,
            context_used=_opt_int(mapped.get("context_used")),
            context_max=_opt_int(mapped.get("context_max")),
            mtp_acc=mtp_acc_sample,
            gpu_util=mapped.get("gpu_util") if mapped.get("gpu_util") is not None
                      else agent_gpu_val(agent_data, "gpu_util"),
            vram_used_mb=mapped.get("vram_used_mb") if mapped.get("vram_used_mb") is not None
                         else agent_gpu_val(agent_data, "vram_used_mb"),
            vram_total_mb=mapped.get("vram_total_mb") if mapped.get("vram_total_mb") is not None
                          else agent_gpu_val(agent_data, "vram_total_mb"),
            gpu_temp=agent_gpu_val(agent_data, "gpu_temp"),
            gpu_power_w=agent_gpu_val(agent_data, "gpu_power_w"),
            cpu_pct=agent_val(agent_data, "cpu_pct"),
            ram_used_mb=agent_val(agent_data, "ram_used_mb"),
            power_w=agent_val(agent_data, "power_w"),
            session_id=st_m.active_session_id,
        )
        s.add(sample)

        if activity or state in ("IDLE", "PROMPTING", "GENERATING"):
            model.last_used_at = ts_ms
            model.first_seen_at = model.first_seen_at or ts_ms

        st_m.last_ts = now
        return None

    def _sync_live_tasks(self, s: Session, provider: Provider, st: ProviderState,
                         cfg: dict, slots: list[dict], ts_ms: int, now: float):
        """Create/update task sessions from sanitized /slots observations."""
        seen: set[str] = set()
        for snap in slots:
            key = f"{snap['slot_id']}:{snap['task_id']}"
            seen.add(key)
            task = st.live_tasks.get(key)
            if task is None:
                row = s.exec(select(SessionRow).where(
                    SessionRow.provider_id == provider.id,
                    SessionRow.model_id == st.model_id,
                    SessionRow.source_slot_id == snap["slot_id"],
                    SessionRow.source_task_id == snap["task_id"],
                    SessionRow.status.in_(["ACTIVE", "FINALIZING"]),
                ).order_by(SessionRow.start_at.desc())).first()
                if row is None:
                    row = SessionRow(
                        provider_id=provider.id, model_id=st.model_id,
                        config_id=st.config_id, start_at=ts_ms, status="ACTIVE",
                        source_slot_id=snap["slot_id"],
                        source_task_id=snap["task_id"],
                        mtp_enabled=bool(cfg.get("mtp_enabled")),
                    )
                    s.add(row)
                    s.commit()
                    s.refresh(row)
                task = LiveTaskState(
                    slot_id=snap["slot_id"], task_id=snap["task_id"],
                    session_id=row.id, first_seen=now, last_seen=now,
                )
                st.live_tasks[key] = task
            task.last_seen = now
            task.finalizing_since = None
            gen_tokens = float(snap.get("gen_tokens") or 0)
            if task.speed_points and gen_tokens < task.speed_points[-1][1]:
                task.speed_points.clear()
                task.generation_started_at = None
                task.generation_start_tokens = 0.0
            task.speed_points.append((now, gen_tokens))
            task.speed_points = [(t, value) for t, value in task.speed_points
                                 if now - t <= LIVE_SPEED_WINDOW_S]
            live_tps = None
            if len(task.speed_points) >= 2:
                t0, v0 = task.speed_points[0]
                dt = now - t0
                if dt > 0 and gen_tokens >= v0:
                    live_tps = (gen_tokens - v0) / dt
            if task.generation_started_at is None and gen_tokens > 0:
                task.generation_started_at = now
                task.generation_start_tokens = gen_tokens
            live_tps_avg = None
            if task.generation_started_at is not None:
                elapsed = now - task.generation_started_at
                generated = gen_tokens - task.generation_start_tokens
                if elapsed > 0 and generated >= 0:
                    live_tps_avg = generated / elapsed
            short_points = [(t, value) for t, value in task.speed_points
                            if now - t <= LIVE_SHORT_WINDOW_S]
            live_tps_3s = None
            if len(short_points) >= 2:
                short_t0, short_v0 = short_points[0]
                short_dt = now - short_t0
                if short_dt > 0 and gen_tokens >= short_v0:
                    live_tps_3s = (gen_tokens - short_v0) / short_dt
            task.prompt_tokens = float(snap.get("prompt_tokens") or 0)
            task.gen_tokens = gen_tokens
            task.context = snap.get("context")
            task.context_max = snap.get("context_max") or _opt_int(cfg.get("context"))
            row = s.get(SessionRow, task.session_id)
            if row:
                row.status = "ACTIVE"
                row.end_at = None
                row.live_prompt_tokens = task.prompt_tokens
                row.live_gen_tokens = task.gen_tokens
                row.live_context = task.context
                row.live_context_max = task.context_max
                row.live_gen_tps = live_tps
                row.live_gen_tps_avg = live_tps_avg
                row.live_gen_tps_3s = live_tps_3s
                row.live_seen_at = ts_ms
                s.add(row)
            st.last_activity_ts = now

        for key, task in list(st.live_tasks.items()):
            if key in seen:
                continue
            row = s.get(SessionRow, task.session_id)
            if task.finalizing_since is None:
                task.finalizing_since = now
                if row:
                    row.status = "FINALIZING"
                    s.add(row)
            elif now - task.finalizing_since >= LIVE_FINALIZE_GRACE_S:
                if row:
                    has_metrics = row.result_source == "metrics"
                    row.status = "CLOSED" if has_metrics else "INCOMPLETE"
                    row.result_source = "metrics" if has_metrics else "incomplete"
                    row.end_at = int(task.last_seen * 1000)
                    row.duration_s = max(0.0, (row.end_at - row.start_at) / 1000.0)
                    s.add(row)
                st.live_tasks.pop(key, None)

        candidates = list(st.live_tasks.values())
        if len(candidates) == 1:
            task = candidates[0]
            st.active_session_id = task.session_id
            if st.stats is None:
                row = s.get(SessionRow, task.session_id)
                st.stats = self._stats_from_session(row) if row else SessionStats()
        elif len(candidates) > 1:
            st.active_session_id = None
            st.stats = None
        elif st.active_session_id and st.slots_available:
            st.active_session_id = None
            st.stats = None
        s.commit()

    @staticmethod
    def _stats_from_session(row: SessionRow) -> SessionStats:
        stats = SessionStats()
        stats.prompt_tokens = float(row.prompt_tokens or 0)
        stats.gen_tokens = float(row.gen_tokens or 0)
        stats.prompt_time_s = float(row.prompt_time_s or 0)
        stats.gen_time_s = float(row.gen_time_s or 0)
        stats.peak_prompt_tps = float(row.peak_prompt_tps or 0)
        stats.peak_gen_tps = float(row.peak_gen_tps or 0)
        stats.context_max = int(row.context_max or 0)
        stats.mtp_proposed = float(row.mtp_proposed or 0)
        stats.mtp_accepted = float(row.mtp_accepted or 0)
        return stats

    def _unload_model(self, s: Session, provider: Provider, entry: dict,
                      st_m: ProviderState, ts_ms: int):
        """Model went from loaded to unloaded: close session, emit UNLOADED."""
        if st_m.live_tasks:
            self._end_live_tasks(s, st_m, ts_ms, "INCOMPLETE")
        elif st_m.active_session_id:
            self._close_session(s, st_m, ts_ms)
        model = s.exec(select(Model).where(
            Model.provider_id == provider.id, Model.key == entry["key"])).first()
        if model is not None:
            s.add(TelemetrySample(provider_id=provider.id, model_id=model.id,
                                  ts=ts_ms, state="UNLOADED"))
        st_m.prev = {}
        st_m.last_ts = None
        st_m.was_loaded = False
        st_m.metrics_fail = 0
        st_m.mtp_window = []
        st_m.live_tasks.clear()
        st_m.slots_available = None

    def _end_live_tasks(self, s: Session, st: ProviderState, end_ts_ms: int,
                        status: str):
        """End every slot-backed request without turning provisional values into totals."""
        for task in list(st.live_tasks.values()):
            row = s.get(SessionRow, task.session_id)
            if row is None or row.status in ("CLOSED", "INCOMPLETE", "INTERRUPTED"):
                continue
            row.status = status
            row.result_source = "incomplete" if status == "INCOMPLETE" else "interrupted"
            row.end_at = min(end_ts_ms, int(task.last_seen * 1000))
            row.duration_s = max(0.0, (row.end_at - row.start_at) / 1000.0)
            s.add(row)
        st.live_tasks.clear()
        st.active_session_id = None
        st.stats = None
        st.last_activity_ts = None
        st.prompt_phase_start_ts = None
        st.first_gen_ts = None
        s.commit()

    def _store_router_props(self, s: Session, provider: Provider, props: dict):
        """Router-level /props: persist build_info as a BuildInfo row."""
        bi = props.get("build_info")
        if not bi:
            return
        bstr = str(bi)
        if "-" in bstr:
            version, commit = bstr.split("-", 1)
        else:
            version, commit = bstr, None
        row = s.exec(select(BuildInfo).where(
            BuildInfo.provider_id == provider.id,
            BuildInfo.version == version,
        )).first()
        if row is None:
            row = BuildInfo(provider_id=provider.id, version=version, commit=commit,
                            build_json=json.dumps(props, default=str))
            s.add(row)
        else:
            row.commit = row.commit or commit
            if not row.build_json or row.build_json == "{}":
                row.build_json = json.dumps(props, default=str)
            row.last_seen_at = now_ms()
        s.add(row)

    # ---------------------------------------------------------------- models
    def _upsert_model_entry(self, s: Session, provider: Provider, entry: dict,
                            props: dict) -> Model:
        """Create/update the Model row for a /v1/models entry (any status)."""
        key = entry["key"]
        m = s.exec(select(Model).where(
            Model.provider_id == provider.id, Model.key == key)).first()
        name = _display_name(key)
        fam, quant = parse_model_name(name)
        cfg = model_entry_config(entry)
        if not cfg:
            cfg = extract_config(props) if props else {}
        mi = (props or {}).get("model_info") or {}
        arch = cfg.get("arch") or mi.get("model_arch")
        params = _params_str(cfg.get("params") or mi.get("n_params"))
        fam_meta = cfg.get("family_meta") or mi.get("general_name")
        fquant = quant_from_ftype(cfg.get("ftype"))
        if m is None:
            m = Model(
                provider_id=provider.id, key=key, name=name,
                quant=fquant or quant, family=fam, arch=arch, params=params,
                color=next_model_color(s, provider.id),
            )
            s.add(m)
            s.commit()
            s.refresh(m)
        else:
            changed = False
            if (fquant or quant) and m.quant is None:
                m.quant = fquant or quant
                changed = True
            if fam and (m.family is None or m.family == m.name):
                m.family = fam
                changed = True
            if arch and m.arch is None:
                m.arch = arch
                changed = True
            if params and m.params is None:
                m.params = params
                changed = True
            if fam_meta and (m.family is None or m.family == m.name):
                clean = re.sub(r"\s+(Instruct|Chat|Base|GGUF)$", "", str(fam_meta))
                if clean:
                    m.family = clean
                    changed = True
            if changed:
                s.commit()
                s.refresh(m)
        return m

    def _upsert_model_config(self, s: Session, st_m: ProviderState, model: Model,
                             cfg: dict):
        fp = config_fingerprint(cfg)
        if st_m.config_fp == fp and st_m.config_id:
            return
        row = s.exec(select(ModelConfig).where(
            ModelConfig.model_id == model.id,
            ModelConfig.fingerprint == fp,
        )).first()
        if row is None:
            row = ModelConfig(
                model_id=model.id, fingerprint=fp, payload=json.dumps(cfg, default=str),
                context=_opt_int(cfg.get("context")), kv_cache_k=cfg.get("kv_cache_k"),
                kv_cache_v=cfg.get("kv_cache_v"),
                flash_attn=cfg.get("flash_attn"), parallel=cfg.get("parallel"),
                split_mode=cfg.get("split_mode"), tensor_split=cfg.get("tensor_split"),
                gpu_layers=cfg.get("gpu_layers"), cpu_moe=cfg.get("cpu_moe"),
                threads=cfg.get("threads"), batch=cfg.get("batch"),
                ubatch=cfg.get("ubatch"), reasoning=cfg.get("reasoning"),
                reasoning_effort=cfg.get("reasoning_effort"),
                reasoning_preserve=cfg.get("reasoning_preserve"),
                mmproj=cfg.get("mmproj"), mtp_enabled=cfg.get("mtp_enabled"),
                mtp_model=cfg.get("mtp_model"), speculative=cfg.get("speculative"),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
        st_m.config_id = row.id
        st_m.config_fp = fp
        st_m.config_payload = cfg

    # --------------------------------------------------------------- sessions
    def _update_session_row(self, s: Session, st: ProviderState, ttft, macc):
        if not st.active_session_id:
            return
        sess = s.get(SessionRow, st.active_session_id)
        if sess is None or st.stats is None:
            return
        st = st
        stats: SessionStats = st.stats
        sess.prompt_tokens = stats.prompt_tokens
        sess.gen_tokens = stats.gen_tokens
        sess.total_tokens = stats.prompt_tokens + stats.gen_tokens
        sess.prompt_time_s = stats.prompt_time_s
        sess.gen_time_s = stats.gen_time_s
        if stats.prompt_time_s > 0:
            sess.prompt_tps = stats.prompt_tokens / stats.prompt_time_s
        if stats.gen_time_s > 0:
            sess.avg_gen_tps = stats.gen_tokens / stats.gen_time_s
        sess.peak_gen_tps = stats.peak_gen_tps or None
        sess.peak_prompt_tps = stats.peak_prompt_tps or None
        if ttft is not None:
            sess.ttft_s = ttft
        if stats.context_max:
            sess.context_max = stats.context_max
        if stats.mtp_proposed:
            sess.mtp_proposed = stats.mtp_proposed
            sess.mtp_accepted = stats.mtp_accepted
            if macc is not None:
                sess.mtp_acc = macc
            sess.mtp_enabled = True
        if stats.gpu_util_n:
            sess.gpu_util_avg = stats.gpu_util_sum / stats.gpu_util_n
        if stats.vram_used_mb_n:
            sess.vram_used_mb = stats.vram_used_mb_sum / stats.vram_used_mb_n
        if stats.ram_used_mb_n:
            sess.ram_used_mb = stats.ram_used_mb_sum / stats.ram_used_mb_n
        if stats.power_w_n:
            sess.power_w = stats.power_w_sum / stats.power_w_n
        s.commit()

    def _close_session(self, s: Session, st: ProviderState, end_ts_ms: int):
        sess_id = st.active_session_id
        if not sess_id:
            return
        sess = s.get(SessionRow, sess_id)
        if sess is None:
            st.active_session_id = None
            return
        if sess.status == "CLOSED":
            st.active_session_id = None
            st.stats = None
            return
        self._update_session_row(s, st, sess.ttft_s, sess.mtp_acc)
        sess = s.get(SessionRow, sess_id)
        if sess.end_at is None:
            sess.end_at = end_ts_ms
            sess.duration_s = max(0.0, (end_ts_ms - sess.start_at) / 1000.0)
        if sess.gen_time_s and sess.gen_tokens and sess.avg_gen_tps is None:
            sess.avg_gen_tps = sess.gen_tokens / sess.gen_time_s
        sess.status = "CLOSED"
        sess.result_source = sess.result_source or "metrics"
        st.active_session_id = None
        st.stats = None
        st.last_activity_ts = None
        st.prompt_phase_start_ts = None
        st.first_gen_ts = None
        s.commit()

    # ----------------------------------------------------------------- agent
    def _store_agent(self, s: Session, provider: Provider, data: dict, ts_ms: int):
        info = data.get("info") or {}
        gpu = data.get("gpu") or {}
        llama = data.get("llama") or {}

        hw_sig = json.dumps([info.get("hostname"), info.get("os"), info.get("cpu_model"),
                             info.get("cpu_threads"), info.get("ram_mb"),
                             [g.get("name") for g in (gpu.get("gpus") or [])],
                             gpu.get("nvidia_driver")], sort_keys=True, default=str)
        hw = s.exec(select(HardwareInfo).where(
            HardwareInfo.provider_id == provider.id).order_by(HardwareInfo.id.desc())).first()
        if hw is None or _sig_of(hw) != hw_sig:
            row = HardwareInfo(
                provider_id=provider.id,
                hostname=info.get("hostname"), os_name=info.get("os"), kernel=info.get("kernel"),
                cpu_model=info.get("cpu_model"), cpu_threads=info.get("cpu_threads"),
                ram_mb=info.get("ram_mb"), gpus=json.dumps(gpu.get("gpus") or []),
                nvidia_driver=gpu.get("nvidia_driver"), cuda=gpu.get("cuda"),
                pcie=gpu.get("pcie"), source="agent",
            )
            s.add(row)
            s.commit()
        elif hw is not None:
            hw.gpus = json.dumps(gpu.get("gpus") or [])
            hw.last_seen_at = now_ms()
            s.commit()

        active_models = sorted({st.model_id for (pid, _), st in self.model_states.items()
                                if pid == provider.id and st.was_loaded and st.model_id})
        active_sessions = sorted({st.active_session_id for (pid, _), st in self.model_states.items()
                                  if pid == provider.id and st.active_session_id})
        for fallback_index, raw in enumerate(gpu.get("gpus") or []):
            index = _opt_int(raw.get("index"))
            if index is None:
                index = fallback_index
            uuid = str(raw.get("uuid") or "").strip() or None
            key = uuid or f"index:{index}"
            s.exec(sqlite_insert(GpuTelemetrySample).values(
                provider_id=provider.id, ts=ts_ms, gpu_key=key, gpu_index=index,
                gpu_uuid=uuid, name=raw.get("name"), util=_opt_float(raw.get("util")),
                vram_used_mb=_opt_float(raw.get("vram_used_mb")),
                vram_total_mb=_opt_float(raw.get("vram_mb")),
                temp_c=_opt_float(raw.get("temp_c")), power_w=_opt_float(raw.get("power_w")),
                pcie=raw.get("pcie"), active_model_ids=json.dumps(active_models),
                active_session_ids=json.dumps(active_sessions),
            ).on_conflict_do_nothing(index_elements=["provider_id", "gpu_key", "ts"]))

        identity = {k: llama.get(k) for k in
                    ("version", "commit", "docker_image", "container_id")}
        if any(identity.values()):
            q = select(BuildInfo).where(
                BuildInfo.provider_id == provider.id,
                (BuildInfo.version.is_not(None) | BuildInfo.commit.is_not(None)),
            ).order_by(BuildInfo.last_seen_at.desc())
            b = s.exec(q).first()
            if b is None and (identity["version"] or identity["commit"]):
                b = BuildInfo(provider_id=provider.id, version=identity["version"],
                              commit=identity["commit"], build_json=json.dumps(llama))
                s.add(b)
            if b is not None:
                b.docker_image = identity["docker_image"] or b.docker_image
                b.container_id = identity["container_id"] or b.container_id
                if not b.version:
                    b.version = identity["version"]
                if not b.commit:
                    b.commit = identity["commit"]
        s.commit()


def _sig_of(hw: HardwareInfo) -> str:
    return json.dumps([hw.hostname, hw.os_name, hw.cpu_model, hw.cpu_threads, hw.ram_mb,
                       [g.get("name") for g in (json.loads(hw.gpus or "[]") or [])],
                       hw.nvidia_driver], sort_keys=True, default=str)


def agent_gpu_val(data: dict, key: str):
    gpu = data.get("gpu") or {}
    gpus = gpu.get("gpus") or []
    if gpus:
        g = gpus[0]
        mapping = {"gpu_util": "util", "vram_used_mb": "vram_used_mb",
                   "vram_total_mb": "vram_mb", "gpu_temp": "temp_c", "gpu_power_w": "power_w"}
        v = g.get(mapping.get(key, key))
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def agent_val(data: dict, key: str):
    info = data.get("info") or {}
    v = info.get(key)
    if v is None:
        v = (data.get("gpu") or {}).get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _params_str(v) -> Optional[str]:
    """Human-readable parameter count (Model.params is a string column)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f >= 1e9:
        return f"{f / 1e9:.1f}B".replace(".0B", "B")
    if f >= 1e6:
        return f"{f / 1e6:.0f}M"
    return str(v)


def _display_name(key: str) -> str:
    name = (key or "").strip()
    name = name.split("/")[-1] if "/" in name else name
    name = re.sub(r"\.(gguf|bin)$", "", name, flags=re.I)
    return name or key


# ---------------------------------------------------------------------------
# Retention: downsample + purge old telemetry
# ---------------------------------------------------------------------------
def run_retention():
    now = int(time.time() * 1000)
    raw_cut = now - RETENTION_RAW_S * 1000
    mid_cut = now - RETENTION_MID_S * 1000
    full_cut = now - RETENTION_FULL_S * 1000

    with db.new_session() as s:
        s.exec(delete(TelemetrySample).where(TelemetrySample.ts < full_cut))
        s.exec(delete(GpuTelemetrySample).where(GpuTelemetrySample.ts < full_cut))
        s.commit()
        _bucketize(s, mid_cut, raw_cut, int(BUCKET_MID_S * 1000))
        _bucketize(s, full_cut, mid_cut, int(BUCKET_FULL_S * 1000))
        _bucketize_gpu(s, mid_cut, raw_cut, int(BUCKET_MID_S * 1000))
        _bucketize_gpu(s, full_cut, mid_cut, int(BUCKET_FULL_S * 1000))


def _bucketize(s: Session, old_cut: int, new_cut: int, bucket_ms: int):
    rows = s.exec(select(TelemetrySample).where(
        TelemetrySample.ts >= old_cut, TelemetrySample.ts < new_cut)).all()
    if not rows:
        return
    groups: dict[tuple, list[TelemetrySample]] = {}
    for r in rows:
        b = (r.ts // bucket_ms) * bucket_ms
        groups.setdefault((r.provider_id, r.model_id, b), []).append(r)

    def _mx(rs, attr):
        vals = [getattr(x, attr) for x in rs if getattr(x, attr) is not None]
        return max(vals) if vals else None

    def _av(rs, attr):
        vals = [getattr(x, attr) for x in rs if getattr(x, attr) is not None]
        return sum(vals) / len(vals) if vals else None

    def _av_positive(rs, attr):
        vals = [getattr(x, attr) for x in rs
                if getattr(x, attr) is not None and getattr(x, attr) > 0]
        return sum(vals) / len(vals) if vals else None

    ins = sqlite_insert(TelemetrySample)
    for (pid, mid, b), rs in groups.items():
        rs.sort(key=lambda r: r.ts)
        last = rs[-1]
        s.exec(ins.values(
            provider_id=pid, model_id=mid, ts=b, state=last.state,
            tokens_total=_mx(rs, "tokens_total"), prompt_total=_mx(rs, "prompt_total"),
            gen_total=_mx(rs, "gen_total"),
            prompt_seconds_total=_mx(rs, "prompt_seconds_total"),
            gen_seconds_total=_mx(rs, "gen_seconds_total"),
            mtp_proposed_total=_mx(rs, "mtp_proposed_total"),
            mtp_accepted_total=_mx(rs, "mtp_accepted_total"),
            prompt_tps=_av_positive(rs, "prompt_tps"),
            gen_tps=_av_positive(rs, "gen_tps"),
            context_used=_mx(rs, "context_used"), context_max=_mx(rs, "context_max"),
            mtp_acc=_av(rs, "mtp_acc"), gpu_util=_av(rs, "gpu_util"),
            vram_used_mb=_av(rs, "vram_used_mb"), vram_total_mb=_mx(rs, "vram_total_mb"),
            gpu_temp=_av(rs, "gpu_temp"), gpu_power_w=_av(rs, "gpu_power_w"),
            cpu_pct=_av(rs, "cpu_pct"), ram_used_mb=_av(rs, "ram_used_mb"),
            power_w=_av(rs, "power_w"), session_id=last.session_id, extra=None,
        ).on_conflict_do_nothing(index_elements=["provider_id", "model_id", "ts"]))
    s.commit()
    s.exec(delete(TelemetrySample).where(
        TelemetrySample.ts >= old_cut, TelemetrySample.ts < new_cut,
        (TelemetrySample.ts % bucket_ms) != 0))
    s.commit()


def _bucketize_gpu(s: Session, old_cut: int, new_cut: int, bucket_ms: int):
    rows = s.exec(select(GpuTelemetrySample).where(
        GpuTelemetrySample.ts >= old_cut, GpuTelemetrySample.ts < new_cut)).all()
    groups: dict[tuple, list[GpuTelemetrySample]] = {}
    for row in rows:
        bucket = (row.ts // bucket_ms) * bucket_ms
        groups.setdefault((row.provider_id, row.gpu_key, bucket), []).append(row)

    def avg(items, attr):
        vals = [getattr(item, attr) for item in items if getattr(item, attr) is not None]
        return sum(vals) / len(vals) if vals else None

    for (pid, key, bucket), items in groups.items():
        items.sort(key=lambda item: item.ts)
        last = items[-1]
        model_ids = sorted({value for item in items
                            for value in json.loads(item.active_model_ids or "[]")})
        session_ids = sorted({value for item in items
                              for value in json.loads(item.active_session_ids or "[]")})
        s.exec(sqlite_insert(GpuTelemetrySample).values(
            provider_id=pid, ts=bucket, gpu_key=key, gpu_index=last.gpu_index,
            gpu_uuid=last.gpu_uuid, name=last.name, util=avg(items, "util"),
            vram_used_mb=avg(items, "vram_used_mb"),
            vram_total_mb=avg(items, "vram_total_mb"), temp_c=avg(items, "temp_c"),
            power_w=avg(items, "power_w"), pcie=last.pcie,
            active_model_ids=json.dumps(model_ids),
            active_session_ids=json.dumps(session_ids),
        ).on_conflict_do_nothing(index_elements=["provider_id", "gpu_key", "ts"]))
    s.commit()
    s.exec(delete(GpuTelemetrySample).where(
        GpuTelemetrySample.ts >= old_cut, GpuTelemetrySample.ts < new_cut,
        (GpuTelemetrySample.ts % bucket_ms) != 0))
    s.commit()
