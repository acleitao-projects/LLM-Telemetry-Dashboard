"""Demo mode: a synthetic llama.cpp provider plus 30 days of history.

Demo mode NEVER contacts an external server. It runs the real collector against a
fake client that serves the same read-only interface (health/metrics/
props/models) with realistic values.
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from typing import Optional

from sqlmodel import Session, select

from . import database as db
from .llama_provider import FakeClient
from .models import (BuildInfo, HardwareInfo, Model, ModelConfig, Provider,
                     SessionRow, TelemetrySample, now_ms)
from .settings import (DEFAULT_PROVIDER_AGENT_URL, DEFAULT_PROVIDER_NAME,
                       DEFAULT_PROVIDER_TYPE, DEFAULT_PROVIDER_URL)
from .metrics import parse_model_name

DAY = 86400.0


class Spec:
    def __init__(self, key: str, gen: float, prompt: float, mtp: bool,
                 mtp_acc: float, ctx: int, params: str, arch: str,
                 vram: int, pop: float, general: str):
        self.key = key
        self.gen = gen
        self.prompt = prompt
        self.mtp = mtp
        self.mtp_acc = mtp_acc
        self.ctx = ctx
        self.params = params
        self.arch = arch
        self.vram = vram
        self.pop = pop
        self.general = general

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1].removesuffix(".gguf")


DEMO_MODELS = [
    Spec("Qwen3.8-27B-Q4_K_M.gguf", 24.2, 470, False, 0.0, 32768, "27.1B",
         "qwen3", 16100, 3.0, "Qwen3.8 27B"),
    Spec("Qwen3.8-27B-IQ4_XS.gguf", 26.8, 505, False, 0.0, 32768, "27.1B",
         "qwen3", 14300, 1.1, "Qwen3.8 27B"),
    Spec("Qwen3.8-Flash-Next-AD-3.84bpw-IQ4_XS-M64.gguf", 37.0, 610, True, 0.42,
         65536, "30.6B", "qwen3next", 15400, 4.2, "Qwen3.8 Flash-Next"),
    Spec("Ornith-35B-A3B-Q5_K_M.gguf", 18.4, 340, True, 0.27, 32768, "34.8B",
         "ornith", 21200, 1.5, "Ornith 35B A3B"),
    Spec("Gemma-4-12B-Q8_0.gguf", 41.3, 690, False, 0.0, 16384, "12.3B",
         "gemma4", 13800, 0.9, "Gemma 4 12B"),
    Spec("Gemma-4-27B-IQ4_XS.gguf", 20.9, 415, False, 0.0, 32768, "27.0B",
         "gemma4", 14600, 0.6, "Gemma 4 27B"),
]


class SimState:
    """Drives the fake llama.cpp server for demo mode."""

    def __init__(self):
        self.rng = random.Random(20260829)
        self.models = DEMO_MODELS
        self.current = DEMO_MODELS[2]
        self.phase = "idle"
        self.phase_until = 0.0
        self.gen_target = 0.0
        self.gen_done = 0.0
        self.prompt_total = 0.0
        self.gen_total = 0.0
        self.mtp_proposed = 0.0
        self.mtp_accepted = 0.0
        self.context_used = 0
        self.last = time.time()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _pick_model(self) -> Spec:
        total = sum(m.pop for m in self.models)
        x = self.rng.random() * total
        for m in self.models:
            x -= m.pop
            if x <= 0:
                return m
        return self.models[-1]

    def advance(self, now: Optional[float] = None) -> dict:
        with self._lock:
            now = now or time.time()
            dt = max(0.0, min(5.0, now - self.last))
            self.last = now
            r = self.rng

            # rare server restart: counters reset
            if r.random() < 0.0002:
                self.prompt_total = 0.0
                self.gen_total = 0.0
                self.mtp_proposed = 0.0
                self.mtp_accepted = 0.0
                self.phase = "loading"
                self.phase_until = now + r.uniform(2, 5)
                self.current = self._pick_model()
                self.context_used = 0

            if now > self.phase_until:
                if self.phase == "idle":
                    self.phase = "loading"
                    self.current = self._pick_model()
                    self.phase_until = now + r.uniform(1.5, 4.0)
                    self.context_used = 0
                elif self.phase == "loading":
                    self.phase = "prompting"
                    self.phase_until = now + r.uniform(1.5, 9.0)
                    self.context_used = int(r.uniform(200, 1500))
                elif self.phase == "prompting":
                    self.phase = "generating"
                    self.gen_target = r.uniform(150, 3800)
                    self.gen_done = 0.0
                    self.phase_until = now + self.gen_target / self.current.gen + r.uniform(0, 8)
                elif self.phase == "generating":
                    self.phase = "idle"
                    self.phase_until = now + r.uniform(4, 45)
                    if r.random() < 0.25:
                        self.current = self._pick_model()
                        self.context_used = 0

            gen_tps = 0.0
            prompt_tps = 0.0
            if self.phase == "prompting":
                prompt_tps = self.current.prompt * r.uniform(0.9, 1.08)
                self.prompt_total += prompt_tps * dt
                self.context_used += int(prompt_tps * dt * 0.4)
            elif self.phase == "generating":
                gen_tps = self.current.gen * r.uniform(0.88, 1.1)
                self.gen_total += gen_tps * dt
                self.gen_done += gen_tps * dt
                self.context_used += int(gen_tps * dt)
                if self.context_used > self.current.ctx * 0.85:
                    self.context_used = int(self.current.ctx * 0.85)
                if self.gen_done >= self.gen_target:
                    self.phase = "idle"
                    self.phase_until = now + r.uniform(4, 45)
                    if r.random() < 0.25:
                        self.current = self._pick_model()
                        self.context_used = 0
                if self.current.mtp:
                    prop = 1.9 * dt
                    acc = prop * min(0.95, self.current.mtp_acc * r.uniform(0.7, 1.25))
                    self.mtp_proposed += prop
                    self.mtp_accepted += acc

            busy = self.phase in ("prompting", "generating")
            if self.phase == "generating":
                gpu_util = r.uniform(58, 96)
                gpu_temp = r.uniform(58, 79)
                gpu_power = r.uniform(190, 330)
            elif self.phase == "prompting":
                gpu_util = r.uniform(78, 100)
                gpu_temp = r.uniform(62, 82)
                gpu_power = r.uniform(260, 430)
            else:
                gpu_util = r.uniform(2, 18)
                gpu_temp = r.uniform(44, 56)
                gpu_power = r.uniform(35, 85)
            vram_used = self.current.vram + int(self.context_used * 0.09) if self.context_used else int(self.current.vram * 0.4)

            tokens_total = self.prompt_total + self.gen_total
            metrics = {
                "llama_server_tokens_total": tokens_total,
                "llama_server_prompt_tokens_total": self.prompt_total,
                "llama_server_generation_tokens_total": self.gen_total,
                "llama_server_context_length": float(self.current.ctx),
                "llama_server_context_used": float(self.context_used),
                "llama_server_n_slots": 1.0,
                "llama_server_n_batch": 2048.0,
                "llama_server_n_ubatch": 512.0,
                "llama_server_token_generation_tps": round(gen_tps, 2),
                "llama_server_prompt_processing_tps": round(prompt_tps, 2),
                "llama_server_gpu_utilization": round(gpu_util, 1),
                "llama_server_vram_used_mb": float(vram_used),
                "llama_server_vram_total_mb": 24564.0,
            }
            if self.current.mtp or self.mtp_proposed > 0:
                metrics["llama_server_mtp_proposed_total"] = self.mtp_proposed
                metrics["llama_server_mtp_accepted_total"] = self.mtp_accepted
            health = {"status": "loading" if self.phase == "loading" else "ok"}
            props = self._props()
            models = [{"id": self.current.key, "object": "model",
                       "owned_by": "demo-router"}]
            argv = ["llama-server", "-m", self.current.key,
                    "-c", str(self.current.ctx), "-ngl", "35",
                    "--flash-attn", "--cache-type-k", "q8_0",
                    "--cache-type-v", "q8_0", "-t", "16", "-b", "4096",
                    "-ub", "512", "--port", "8083"]
            if self.current.mtp:
                argv.append("--mtp")
            return {
                "health": health,
                "metrics": metrics,
                "props": props,
                "models": models,
                "agent": {
                    "info": {"hostname": "demo-host",
                             "os": "Debian GNU/Linux 12 (bookworm)",
                             "kernel": "6.1.0-28-amd64",
                             "cpu_model": "AMD Ryzen 9 7950X",
                             "cpu_threads": 32, "ram_mb": 262144,
                             "cpu_pct": r.uniform(18, 70) if busy else r.uniform(4, 18),
                             "ram_used_mb": r.uniform(90000, 118000),
                             "power_w": r.uniform(180, 420) if busy else r.uniform(60, 140)},
                    "gpu": {"gpus": [{"name": "NVIDIA GeForce RTX 4090",
                                      "vram_mb": 24564, "vram_used_mb": vram_used,
                                      "util": round(gpu_util, 1),
                                      "temp_c": round(gpu_temp, 1),
                                      "power_w": round(gpu_power, 1)}],
                            "nvidia_driver": "570.86.15", "cuda": "12.8",
                            "pcie": "Gen4 x16"},
                    "llama": {"version": "b6318", "commit": "c7d2e91",
                              "docker_image": "ghcr.io/ggerganov/llama.cpp:server",
                              "container_id": "77aa02", "argv": argv},
                },
            }

    def _props(self) -> dict:
        m = self.current
        cfg = {
            "system_info": {
                "n_ctx": m.ctx, "n_gpu_layers": 35, "flash_attn": True,
                "split_mode": "layer", "parallel": 1, "n_batch": 2048,
                "n_ubatch": 512, "n_threads": 16, "n_threads_batch": 16,
                "cache_type_k": "q8_0", "cache_type_v": "q8_0",
                "cpu_moe": 0 if m.arch != "ornith" else 6,
                "backend": "CUDA", "n_gpu": 1,
                "mtp_enabled": m.mtp,
            },
            "model_info": {
                "model_arch": m.arch, "n_params": m.params,
                "general_name": m.general, "n_ctx_train": m.ctx,
                "n_vocab": 151936,
            },
            "generation_settings": {
                "reasoning_format": "deepseek" if m.arch in ("qwen3", "qwen3next") else "none",
                "reasoning_effort": "medium" if m.arch in ("qwen3", "qwen3next") else None,
                "reasoning_preserve": True if m.arch in ("qwen3", "qwen3next") else False,
            },
        }
        if m.mtp:
            cfg["system_info"]["mtp_model"] = f"{m.key.split('-')[0]}-mtp-head"
        return cfg

    # -- seeding support ------------------------------------------------
    def finalize_seed(self, last_model: Spec, prompt_total: float, gen_total: float,
                      mtp_proposed: float, mtp_accepted: float):
        self.current = last_model
        self.prompt_total = prompt_total
        self.gen_total = gen_total
        self.mtp_proposed = mtp_proposed
        self.mtp_accepted = mtp_accepted
        self.phase = "idle"
        self.phase_until = time.time() + self.rng.uniform(3, 12)
        self.last = time.time()
        self.context_used = 0


class DemoWorld:
    def __init__(self):
        self.sim = SimState()
        self.seeded = False

    def client(self, provider) -> FakeClient:
        return FakeClient(self.sim.advance)

    # ------------------------------------------------------------------
    def seed_if_empty(self, engine) -> bool:
        with Session(engine) as s:
            if s.exec(select(Provider)).first() is not None:
                return False
        self._seed(engine)
        self.seeded = True
        return True

    def _seed(self, engine):
        rng = random.Random(777)
        now = time.time()
        start_day0 = now - 30 * DAY
        palette = ["#4b8de8", "#d8733e", "#48a77c", "#d29b25", "#8a7bc8", "#4fa3a5"]
        with Session(engine) as s:
            prov = Provider(
                name=DEFAULT_PROVIDER_NAME, ptype=DEFAULT_PROVIDER_TYPE,
                base_url=DEFAULT_PROVIDER_URL,
                agent_url=DEFAULT_PROVIDER_AGENT_URL, enabled=True, is_default=True,
                poll_interval_s=1.0, status="LIVE",
                last_success_at=int(now * 1000), latency_ms=11.0,
            )
            s.add(prov)
            s.commit()
            s.refresh(prov)

            models: dict[str, Model] = {}
            for i, spec in enumerate(DEMO_MODELS):
                name = spec.name
                fam, quant = parse_model_name(name)
                m = Model(provider_id=prov.id, key=spec.key, name=name, quant=quant,
                          family=fam, arch=spec.arch, params=spec.params,
                          color=palette[i % len(palette)])
                s.add(m)
                models[spec.key] = m
            s.commit()
            for spec in DEMO_MODELS:
                models[spec.key] = s.get(Model, models[spec.key].id)

            # two config generations per model (history)
            cfgs: dict[str, tuple] = {}
            for spec in DEMO_MODELS:
                m = models[spec.key]
                base = spec.mtp
                c1 = ModelConfig(
                    model_id=m.id, fingerprint="A1" + f"{m.id:04X}"[:4],
                    payload=json.dumps({"context": spec.ctx, "flash_attn": True,
                                       "gpu_layers": 33, "n_batch": 2048,
                                       "mtp_enabled": base,
                                       "kv_cache_k": "f16", "kv_cache_v": "f16",
                                       "flags_raw": ["llama-server", "-m", spec.key,
                                                     "-c", str(spec.ctx), "-ngl", "33"]}),
                    context=spec.ctx, flash_attn=True, gpu_layers=33, batch=2048,
                    ubatch=512, threads=16, split_mode="layer", parallel=1,
                    kv_cache_k="f16", kv_cache_v="f16", mtp_enabled=base,
                    mtp_model=f"{spec.key.split('-')[0]}-mtp-head" if base else None,
                    reasoning="deepseek" if spec.arch.startswith("qwen") else "none",
                    reasoning_effort="medium" if spec.arch.startswith("qwen") else None,
                    created_at=int((start_day0 + DAY) * 1000),
                )
                c2 = ModelConfig(
                    model_id=m.id, fingerprint="B2" + f"{m.id:04X}"[:4],
                    payload=json.dumps({"context": spec.ctx, "flash_attn": True,
                                       "gpu_layers": 35, "n_batch": 4096,
                                       "mtp_enabled": base,
                                       "kv_cache_k": "q8_0", "kv_cache_v": "q8_0",
                                       "flags_raw": ["llama-server", "-m", spec.key,
                                                     "-c", str(spec.ctx), "-ngl", "35"]}),
                    context=spec.ctx, flash_attn=True, gpu_layers=35, batch=4096,
                    ubatch=512, threads=16, split_mode="layer", parallel=1,
                    kv_cache_k="q8_0", kv_cache_v="q8_0", mtp_enabled=base,
                    mtp_model=f"{spec.key.split('-')[0]}-mtp-head" if base else None,
                    reasoning="deepseek" if spec.arch.startswith("qwen") else "none",
                    reasoning_effort="medium" if spec.arch.startswith("qwen") else None,
                    created_at=int((start_day0 + 18 * DAY) * 1000),
                )
                s.add(c1)
                s.add(c2)
                cfgs[spec.key] = (c1.id, c2.id)
            s.commit()
            for spec in DEMO_MODELS:
                cfgs[spec.key] = (s.get(ModelConfig, cfgs[spec.key][0]),
                                  s.get(ModelConfig, cfgs[spec.key][1]))

            # builds
            b1 = BuildInfo(provider_id=prov.id, version="b6123", commit="a3f91c4",
                           build_json=json.dumps({"image": "ghcr.io/ggerganov/llama.cpp:server"}),
                           docker_image="ghcr.io/ggerganov/llama.cpp:server",
                           container_id="9f2c1e", first_seen_at=int(start_day0 * 1000),
                           last_seen_at=int((start_day0 + 18 * DAY) * 1000))
            b2 = BuildInfo(provider_id=prov.id, version="b6318", commit="c7d2e91",
                           build_json=json.dumps({"image": "ghcr.io/ggerganov/llama.cpp:server"}),
                           docker_image="ghcr.io/ggerganov/llama.cpp:server",
                           container_id="77aa02", first_seen_at=int((start_day0 + 18 * DAY) * 1000),
                           last_seen_at=int(now * 1000))
            s.add(b1)
            s.add(b2)
            s.commit()

            hw = HardwareInfo(
                provider_id=prov.id, hostname="demo-host",
                os_name="Debian GNU/Linux 12 (bookworm)", kernel="6.1.0-28-amd64",
                cpu_model="AMD Ryzen 9 7950X", cpu_threads=32, ram_mb=262144,
                gpus=json.dumps([{"name": "NVIDIA GeForce RTX 4090", "vram_mb": 24564}]),
                nvidia_driver="570.86.15", cuda="12.8", pcie="Gen4 x16", source="agent",
                last_seen_at=int(now * 1000),
            )
            s.add(hw)
            s.commit()

            # ---- generate 30 days of sessions (+idle time)
            prompt_total = 0.0
            gen_total = 0.0
            mtp_prop = 0.0
            mtp_acc = 0.0
            t = start_day0 + 7 * 3600  # start weekday 07:00
            last_spec = DEMO_MODELS[2]
            samples_buf: list[TelemetrySample] = []
            sessions_buf: list[SessionRow] = []
            RECENT = now - 24 * 3600

            def _flush():
                nonlocal samples_buf
                if samples_buf:
                    s.add_all(samples_buf)
                    s.commit()
                    samples_buf = []

            while t < now - 120:
                spec = self._weighted(rng)
                last_spec = spec
                m = models[spec.key]
                c1, c2 = cfgs[spec.key]
                cfg = c2 if t > start_day0 + 18 * DAY else c1
                step = 5.0 if t > RECENT else 10.0
                prompt_s = rng.uniform(2.0, 9.0)
                prompt_tokens = rng.uniform(300, 4200)
                gen_tokens = rng.uniform(150, 3600)
                gen_tps = spec.gen * rng.uniform(0.88, 1.12)
                prompt_tps = spec.prompt * rng.uniform(0.9, 1.1)
                gen_s = gen_tokens / gen_tps
                duration = prompt_s + gen_s
                t0 = t
                t_end = t + duration
                ctx0 = int(rng.uniform(150, 900))
                peak_gen = gen_tps * 1.18
                ttft = rng.uniform(0.4, 2.4)
                mtp_enabled = spec.mtp
                sess_prop = sess_acc = 0.0
                if mtp_enabled:
                    sess_prop = gen_s * 1.9
                    acc_rate = min(0.95, spec.mtp_acc * rng.uniform(0.7, 1.25))
                    sess_acc = sess_prop * acc_rate
                sess_start_ms = int(t0 * 1000)
                ctx = ctx0
                ctx_final = min(spec.ctx, int(ctx0 + prompt_tokens * 0.6 + gen_tokens))
                gpu_avg = 0.0
                n_g = 0
                tt = t0
                while tt <= t_end:
                    if tt - t0 < prompt_s:
                        state = "PROMPTING"
                        cur_prompt = prompt_tokens * min(1.0, (tt - t0) / prompt_s)
                        cur_gen = 0.0
                        tps_g = 0.0
                        tps_p = prompt_tps
                    else:
                        state = "GENERATING"
                        cur_prompt = prompt_tokens
                        cur_gen = gen_tokens * min(1.0, (tt - t0 - prompt_s) / gen_s)
                        tps_g = gen_tps * rng.uniform(0.9, 1.05)
                        tps_p = 0.0
                    ctx = min(int(spec.ctx * 0.85),
                              ctx0 + int(prompt_tokens * min(1.0, (tt - t0) / prompt_s) * 0.6)
                              + int(cur_gen))
                    gu = (rng.uniform(58, 96) if state == "GENERATING"
                          else rng.uniform(78, 100))
                    gpu_avg += gu
                    n_g += 1
                    vram = spec.vram + int(ctx * 0.09)
                    frac = cur_gen / max(1.0, gen_tokens)
                    samples_buf.append(TelemetrySample(
                        provider_id=prov.id, model_id=m.id, ts=int(tt * 1000),
                        state=state,
                        tokens_total=prompt_total + gen_total + cur_prompt + cur_gen,
                        prompt_total=prompt_total + cur_prompt,
                        gen_total=gen_total + cur_gen,
                        mtp_proposed_total=(mtp_prop + sess_prop * frac) if mtp_enabled else mtp_prop,
                        mtp_accepted_total=(mtp_acc + sess_acc * frac) if mtp_enabled else mtp_acc,
                        prompt_tps=round(tps_p, 1) if tps_p else None,
                        gen_tps=round(tps_g, 1) if tps_g else None,
                        context_used=ctx, context_max=spec.ctx,
                        mtp_acc=round(sess_acc / sess_prop * 100.0, 1) if (mtp_enabled and sess_prop > 0) else None,
                        gpu_util=round(gu, 1),
                        vram_used_mb=float(vram), vram_total_mb=24564.0,
                        gpu_temp=round(rng.uniform(58, 79) if state == "GENERATING"
                                      else rng.uniform(44, 58), 1),
                        gpu_power_w=round(rng.uniform(190, 330) if state == "GENERATING"
                                         else rng.uniform(40, 90), 1),
                        cpu_pct=round(rng.uniform(20, 75), 1),
                        ram_used_mb=rng.uniform(90000, 118000),
                        power_w=rng.uniform(180, 420),
                        session_id=None,  # patched after insert
                    ))
                    tt += step
                sessions_buf.append(SessionRow(
                    provider_id=prov.id, model_id=m.id, config_id=cfg.id,
                    start_at=sess_start_ms, end_at=int(t_end * 1000),
                    duration_s=round(duration, 1),
                    prompt_time_s=round(prompt_s, 2), gen_time_s=round(gen_s, 2),
                    prompt_tokens=round(prompt_tokens), gen_tokens=round(gen_tokens),
                    total_tokens=round(prompt_tokens + gen_tokens),
                    prompt_tps=round(prompt_tokens / prompt_s, 1),
                    avg_gen_tps=round(gen_tps, 1),
                    peak_gen_tps=round(peak_gen, 1),
                    peak_prompt_tps=round(prompt_tps * 1.05, 1),
                    ttft_s=round(ttft, 2), context_max=ctx_final,
                    mtp_enabled=mtp_enabled,
                    mtp_proposed=round(sess_prop) if mtp_enabled else None,
                    mtp_accepted=round(sess_acc) if mtp_enabled else None,
                    mtp_acc=round(sess_acc / sess_prop * 100.0, 1) if mtp_enabled else None,
                    gpu_util_avg=round(gpu_avg / max(1, n_g), 1),
                    vram_used_mb=round(spec.vram + int(ctx_final * 0.09)),
                    ram_used_mb=round(rng.uniform(95000, 115000)),
                    power_w=round(rng.uniform(220, 380)),
                    status="CLOSED",
                ))
                prompt_total += prompt_tokens
                gen_total += gen_tokens
                mtp_prop += sess_prop
                mtp_acc += sess_acc
                gap = rng.uniform(60, 2400)
                idle_step = 5.0 if t_end > RECENT else 60.0
                tt = t_end
                while tt < t_end + gap:
                    idle_gu = rng.uniform(2, 15)
                    samples_buf.append(TelemetrySample(
                        provider_id=prov.id, model_id=m.id, ts=int(tt * 1000),
                        state="IDLE",
                        tokens_total=prompt_total + gen_total,
                        prompt_total=prompt_total,
                        gen_total=gen_total,
                        mtp_proposed_total=mtp_prop, mtp_accepted_total=mtp_acc,
                        prompt_tps=None, gen_tps=None,
                        context_used=ctx, context_max=spec.ctx,
                        mtp_acc=None,
                        gpu_util=round(idle_gu, 1),
                        vram_used_mb=float(spec.vram + int(ctx * 0.09)),
                        vram_total_mb=24564.0,
                        gpu_temp=round(rng.uniform(44, 56), 1),
                        gpu_power_w=round(rng.uniform(35, 85), 1),
                        cpu_pct=round(rng.uniform(4, 18), 1),
                        ram_used_mb=rng.uniform(90000, 110000),
                        power_w=rng.uniform(60, 140),
                        session_id=None,
                    ))
                    tt += idle_step
                t = t_end + gap
                _flush()

            # link session ids to samples
            s.add_all(sessions_buf)
            s.commit()
            for sb in sessions_buf:
                s.refresh(sb)
            for sb in sessions_buf:
                from sqlalchemy import update
                s.exec(update(TelemetrySample).where(
                    TelemetrySample.provider_id == prov.id,
                    TelemetrySample.ts >= sb.start_at,
                    TelemetrySample.ts <= (sb.end_at or sb.start_at),
                    TelemetrySample.model_id == sb.model_id,
                ).values(session_id=sb.id))
            s.commit()

            # final provider state
            prov.last_success_at = int(now * 1000)
            prov.status = "LIVE"
            s.commit()

        self.sim.finalize_seed(last_spec, prompt_total, gen_total, mtp_prop, mtp_acc)

    def _weighted(self, rng: random.Random) -> Spec:
        total = sum(m.pop for m in self.models)
        x = rng.random() * total
        for m in self.models:
            x -= m.pop
            if x <= 0:
                return m
        return self.models[-1]


_world: DemoWorld | None = None


def get_world() -> DemoWorld:
    global _world
    if _world is None:
        _world = DemoWorld()
    return _world
