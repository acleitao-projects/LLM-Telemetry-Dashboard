"""Passive, read-only client for llama.cpp server endpoints.

Observatory NEVER sends prompts. The only endpoints ever touched are:
    GET /health
    GET /metrics
    GET /props
    GET /v1/models
    GET /slots
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Callable, Optional

import httpx

from .settings import METRIC_ALIASES


class LlamaError(Exception):
    pass


def parse_metrics_text(text: str) -> dict[str, float]:
    """Parse prometheus exposition text into {metric_name: value} (last label wins).

    Handles both labeled (``name{a=b} v``) and unlabeled (``name v``) lines.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or " " not in line:
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if not name:
            continue
        raw_val = line.rsplit(" ", 1)[1]
        try:
            out[name] = float(raw_val)
        except ValueError:
            continue
    return out


def map_metrics(raw: dict[str, float]) -> dict[str, Any]:
    """Map known llama.cpp metric names to canonical keys."""
    out: dict[str, Any] = {}
    for key, names in METRIC_ALIASES.items():
        for n in names:
            if n in raw:
                out[key] = raw[n]
                break
    if "tokens_total" not in out and "prompt_total" in out and "gen_total" in out:
        out["tokens_total"] = out["prompt_total"] + out["gen_total"]
    return out


class LlamaClient:
    """Synchronous read-only llama.cpp client. One instance per provider."""

    def __init__(self, base_url: str, timeout: float = 4.0):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._c = httpx.Client(timeout=timeout)
        self.available: dict[str, bool] = {}

    # -- read-only endpoints ------------------------------------------------
    def health(self) -> dict:
        r = self._c.get(self.base_url + "/health")
        r.raise_for_status()
        self.available["health"] = True
        try:
            return r.json()
        except ValueError:
            return {"status": "ok" if r.text.strip() == "ok" else "unknown"}

    def metrics(self, model: Optional[str] = None) -> dict[str, float]:
        """Per-model metrics. Nautilus routes /metrics?model=<name> to the
        llama-server spawned for that model; classic servers ignore the param."""
        url = self.base_url + "/metrics"
        if model:
            url += "?model=" + urllib.parse.quote(model)
        r = self._c.get(url)
        r.raise_for_status()
        self.available["metrics"] = True
        return parse_metrics_text(r.text)

    def props(self) -> dict:
        r = self._c.get(self.base_url + "/props")
        r.raise_for_status()
        self.available["props"] = True
        try:
            return r.json()
        except ValueError:
            return {}

    def models(self) -> list[dict]:
        r = self._c.get(self.base_url + "/v1/models")
        r.raise_for_status()
        self.available["models"] = True
        data = r.json()
        if isinstance(data, dict):
            return data.get("data", []) or []
        return data or []

    def slots(self, model: Optional[str] = None) -> list[dict]:
        """Live per-slot state; Nautilus requires the model query parameter."""
        url = self.base_url + "/slots"
        if model:
            url += "?model=" + urllib.parse.quote(model)
        r = self._c.get(url)
        r.raise_for_status()
        self.available["slots"] = True
        data = r.json()
        return data if isinstance(data, list) else []

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


class AgentClient:
    """Optional passive host agent (nautilus_agent.py). Read-only."""

    def __init__(self, base_url: str, timeout: float = 4.0):
        self.base_url = (base_url or "").rstrip("/")
        self._c = httpx.Client(timeout=timeout)

    def info(self) -> dict:
        r = self._c.get(self.base_url + "/info")
        r.raise_for_status()
        return r.json()

    def gpu(self) -> dict:
        r = self._c.get(self.base_url + "/gpu")
        r.raise_for_status()
        return r.json()

    def llama(self) -> dict:
        r = self._c.get(self.base_url + "/llama")
        r.raise_for_status()
        return r.json()

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


class FakeClient:
    """Interface-compatible stand-in for LlamaClient (used by demo mode)."""

    def __init__(self, snapshot_fn: Callable[[], dict]):
        self._fn = snapshot_fn
        self._last: dict = {}
        self.base_url = "demo://nautilus"
        self.available = {"health": True, "metrics": True, "props": True,
                          "models": True, "slots": True}

    def _snap(self) -> dict:
        self._last = self._fn()
        return self._last

    def health(self) -> dict:
        return self._snap().get("health", {"status": "ok"})

    def metrics(self, model: Optional[str] = None) -> dict[str, float]:
        return self._snap().get("metrics", {})

    def props(self) -> dict:
        return self._snap().get("props", {})

    def models(self) -> list[dict]:
        return self._snap().get("models", [])

    def slots(self, model: Optional[str] = None) -> list[dict]:
        return self._snap().get("slots", [])

    def agent_snapshot(self) -> dict:
        self._snap()
        return self._last.get("agent", {}) or {}

    def close(self):
        pass


def extract_config(props: dict) -> dict:
    """Extract an observed model/server configuration dict from /props.

    Defensive: keys vary by llama.cpp build, so we pull whatever exists.
    """
    si = props.get("system_info") or {}
    mi = props.get("model_info") or {}
    gs = props.get("generation_settings") or {}
    cfg: dict[str, Any] = {}

    def put(key: str, val: Any):
        if val is not None and val != "":
            cfg[key] = val

    put("context", si.get("n_ctx") or mi.get("n_ctx_train") or props.get("context"))
    put("kv_cache_k", si.get("cache_type_k"))
    put("kv_cache_v", si.get("cache_type_v"))
    fa = si.get("flash_attn")
    if fa is not None:
        cfg["flash_attn"] = bool(fa)
    put("parallel", si.get("parallel"))
    put("split_mode", si.get("split_mode"))
    put("tensor_split", si.get("tensor_split"))
    put("gpu_layers", si.get("n_gpu_layers") or si.get("gpu_layers"))
    put("cpu_moe", si.get("cpu_moe"))
    put("threads", si.get("n_threads"))
    put("threads_batch", si.get("n_threads_batch"))
    put("batch", si.get("n_batch"))
    put("ubatch", si.get("n_ubatch"))
    put("backend", si.get("backend"))
    put("n_gpu", si.get("n_gpu"))
    put("reasoning", gs.get("reasoning_format") or si.get("reasoning_format"))
    put("reasoning_effort", gs.get("reasoning_effort"))
    put("reasoning_preserve", gs.get("reasoning_preserve"))
    put("mmproj", si.get("mmproj") or props.get("mmproj"))
    mtp = si.get("mtp_enabled", gs.get("mtp_enabled"))
    if mtp is not None:
        cfg["mtp_enabled"] = bool(mtp)
    put("mtp_model", si.get("mtp_model") or gs.get("mtp_model") or si.get("draft_model"))
    put("speculative", si.get("speculative") or gs.get("speculative"))
    put("draft_max", si.get("draft_max") or gs.get("draft_max"))
    put("arch", mi.get("model_arch"))
    put("params", mi.get("n_params"))
    put("family_meta", mi.get("general_name"))
    put("vocab", mi.get("n_vocab"))
    return cfg


def config_fingerprint(cfg: dict) -> str:
    import hashlib
    raw = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:6].upper()
