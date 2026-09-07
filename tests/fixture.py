"""Production-shaped fixture for G02 (durable usage / residency / snapshots).

Builds a realistic dataset end-to-end the way the system does in production:
providers, 16+ models, cumulative telemetry counters over 30 days, GPU samples,
and 1000+ sessions -- then runs the G02 backfill (the same code the schema
migration uses) to derive durable usage buckets and residency intervals.

Because the buckets and residency rows are produced by :func:`observatory
.database._backfill_g02`, tests built on this fixture exercise the *strict*
aggregation path (minute buckets + residency overlap) exactly as it runs in
production, while remaining fast on an in-memory database.
"""
from __future__ import annotations

import random
from typing import Optional

from sqlalchemy import insert
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from observatory import database as odb
from observatory.models import (Model, Provider, GpuTelemetrySample,
                                SessionRow, TelemetrySample, now_ms)

DAY_MS = 86_400_000
MINUTE_MS = 60_000


def make_engine():
    """A shared single-connection in-memory SQLite engine (safe across threads)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def build_production_fixture(engine, now: Optional[int] = None, *,
                             providers: int = 2, models_per_provider: int = 8,
                             days: int = 30, sessions: int = 1000,
                             sample_every_ms: int = 5 * MINUTE_MS,
                             active_fraction: float = 0.7,
                             gpus_per_provider: int = 2,
                             seed: int = 1337) -> dict:
    """Populate ``engine`` with a production-shaped G02 dataset.

    Returns a summary dict with ``now``, ``days``, provider/model ids and counts.
    """
    now = int(now if now is not None else now_ms())
    rng = random.Random(seed)
    start = now - days * DAY_MS

    with Session(engine) as s:
        prov_ids: list[int] = []
        for i in range(providers):
            p = Provider(name=f"router-{i}", ptype="llama.cpp",
                         base_url=f"http://router-{i}:8080",
                         agent_url=f"http://router-{i}:50051",
                         status="LIVE", enabled=True, is_default=(i == 0),
                         last_success_at=now - 1000)
            s.add(p)
            s.commit()
            s.refresh(p)
            prov_ids.append(p.id)

        model_ids: list[int] = []
        model_provider: dict[int, int] = {}
        for p in prov_ids:
            for j in range(models_per_provider):
                m = Model(provider_id=p, key=f"m{p}-{j}", name=f"model-{p}-{j}",
                          quant="Q4_K_M", family=f"family-{j % 4}", arch="llama",
                          params="8B", first_seen_at=start)
                s.add(m)
                s.commit()
                s.refresh(m)
                model_ids.append(m.id)
                model_provider[m.id] = p

        _seed_telemetry(s, model_ids, model_provider, start, now,
                        sample_every_ms, active_fraction, rng)
        _seed_gpu(s, prov_ids, gpus_per_provider, start, now, rng)
        _seed_sessions(s, model_ids, model_provider, start, now, sessions, rng)
        s.commit()

    # Derive durable buckets + residency exactly as the migration backfill does.
    with Session(engine) as s:
        odb._backfill_g02(s)

    return {
        "now": now, "days": days, "start": start,
        "provider_ids": prov_ids, "model_ids": model_ids,
        "model_provider": model_provider,
        "provider_count": len(prov_ids), "model_count": len(model_ids),
        "session_target": sessions,
    }


def _seed_telemetry(s: Session, model_ids: list[int], model_provider: dict,
                    start: int, now: int, sample_every_ms: int,
                    active_fraction: float, rng: random.Random) -> None:
    """Cumulative counters per model over the window. Active samples advance the
    counters; gaps (no sample) produce no bucket and contribute zero usage."""
    samples: list[dict] = []
    for mid in model_ids:
        pid = model_provider[mid]
        tokens = prompt = gen = prompt_s = gen_s = prop = acc = 0.0
        ts = start
        while ts <= now:
            if rng.random() < active_fraction:
                prompt += rng.randint(50, 400)
                gen += rng.randint(20, 200)
                prompt_s += prompt_delta_s(rng, prompt)
                gen_s += gen_delta_s(rng, gen)
                tokens = prompt + gen
                prop += rng.randint(0, 40)
                acc += rng.randint(0, 30)
                samples.append({
                    "provider_id": pid, "model_id": mid, "ts": ts,
                    "state": "GENERATING", "tokens_total": tokens,
                    "prompt_total": prompt, "gen_total": gen,
                    "prompt_seconds_total": prompt_s, "gen_seconds_total": gen_s,
                    "mtp_proposed_total": prop, "mtp_accepted_total": acc,
                    "prompt_tps": 240.0, "gen_tps": rng.uniform(20, 60),
                    "context_used": rng.randint(2000, 16000), "context_max": 32768,
                    "mtp_acc": rng.uniform(60, 90), "gpu_util": rng.uniform(50, 95),
                    "vram_used_mb": rng.uniform(20000, 28000), "vram_total_mb": 32768,
                    "gpu_temp": rng.uniform(45, 75), "power_w": rng.uniform(150, 400),
                })
            ts += sample_every_ms
    if samples:
        s.execute(insert(TelemetrySample).values(samples))


def prompt_delta_s(rng: random.Random, n_tokens: float) -> float:
    return max(0.1, n_tokens / rng.uniform(150, 350))


def gen_delta_s(rng: random.Random, n_tokens: float) -> float:
    return max(0.1, n_tokens / rng.uniform(20, 60))


def _seed_gpu(s: Session, prov_ids: list[int], gpus_per_provider: int,
              start: int, now: int, rng: random.Random) -> None:
    samples: list[dict] = []
    step = 5 * MINUTE_MS
    for pid in prov_ids:
        for g in range(gpus_per_provider):
            ts = start
            while ts <= now:
                samples.append({
                    "provider_id": pid, "ts": ts, "gpu_key": f"gpu-{pid}-{g}",
                    "gpu_index": g, "gpu_uuid": f"UUID-{pid}-{g}",
                    "name": "NVIDIA A100 80GB", "util": rng.uniform(30, 95),
                    "vram_used_mb": rng.uniform(20000, 75000), "vram_total_mb": 81920,
                    "temp_c": rng.uniform(40, 70), "power_w": rng.uniform(100, 400),
                    "pcie": "gen4 x16",
                })
                ts += step
    if samples:
        s.execute(insert(GpuTelemetrySample).values(samples))


def _seed_sessions(s: Session, model_ids: list[int], model_provider: dict,
                   start: int, now: int, count: int, rng: random.Random) -> None:
    rows: list[dict] = []
    for _ in range(count):
        mid = rng.choice(model_ids)
        pid = model_provider[mid]
        start_at = start + rng.randint(0, max(1, now - start - 5 * MINUTE_MS))
        dur = rng.randint(15_000, 900_000)
        end_at = min(now, start_at + dur)
        prompt = rng.randint(100, 4000)
        gen = rng.randint(50, 8000)
        rows.append({
            "provider_id": pid, "model_id": mid, "start_at": start_at,
            "end_at": end_at, "duration_s": round((end_at - start_at) / 1000.0, 1),
            "prompt_time_s": round(prompt / 250.0, 2),
            "gen_time_s": round(gen / 40.0, 2), "prompt_tokens": prompt,
            "gen_tokens": gen, "total_tokens": prompt + gen, "prompt_tps": 250.0,
            "avg_gen_tps": 40.0, "peak_gen_tps": 60.0, "peak_prompt_tps": 350.0,
            "ttft_s": rng.uniform(0.2, 2.0), "context_max": rng.randint(4096, 32768),
            "mtp_enabled": True, "mtp_proposed": rng.randint(0, 500),
            "mtp_accepted": rng.randint(0, 400), "mtp_acc": rng.uniform(60, 90),
            "status": "CLOSED", "result_source": "metrics",
        })
    if rows:
        s.execute(insert(SessionRow).values(rows))
