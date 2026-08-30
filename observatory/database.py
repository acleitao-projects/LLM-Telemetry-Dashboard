"""SQLite database setup, engine, and light migration support."""
from __future__ import annotations

import os
import time

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine

from . import models  # noqa: F401  (register tables)

SCHEMA_VERSION = 5

_engine: Engine | None = None
_db_path: str | None = None


def init_db(path: str) -> Engine:
    """Create the database (and data dir) if needed and return the engine."""
    global _engine, _db_path
    if _engine is not None:
        return _engine
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    SQLModel.metadata.create_all(engine)
    _migrate(engine)
    _engine = engine
    _db_path = path
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("database not initialized; call init_db() first")
    return _engine


def get_db_path() -> str | None:
    return _db_path


def new_session() -> Session:
    return Session(get_engine())


def db_size_bytes() -> int:
    if _db_path and os.path.exists(_db_path):
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = _db_path + suffix
            if os.path.exists(p):
                total += os.path.getsize(p)
        return total
    return 0


def _migrate(engine: Engine) -> None:
    """Minimal forward migrations. v1 is created via create_all."""
    from sqlalchemy import text
    with Session(engine) as s:
        s.exec(text("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"))
        s.commit()
        res = s.exec(text("SELECT value FROM meta WHERE key='schema_version'")).first()
        if res is None:
            s.exec(text(
                "INSERT INTO meta (key, value) VALUES ('schema_version', :version)"
            ), params={"version": str(SCHEMA_VERSION)})
            s.commit()
        else:
            version = int(res[0])
            if version < 2:
                s.exec(text("""
                    DELETE FROM buildinfo
                    WHERE COALESCE(TRIM(version), '') = ''
                      AND COALESCE(TRIM("commit"), '') = ''
                      AND COALESCE(TRIM(docker_image), '') = ''
                      AND COALESCE(TRIM(container_id), '') = ''
                """))
                s.exec(text(
                    "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
                ))
                s.commit()
            if version < 3:
                columns = {row[1] for row in s.exec(text(
                    "PRAGMA table_info(telemetrysample)"
                )).all()}
                if "prompt_seconds_total" not in columns:
                    s.exec(text(
                        "ALTER TABLE telemetrysample ADD COLUMN prompt_seconds_total FLOAT"
                    ))
                if "gen_seconds_total" not in columns:
                    s.exec(text(
                        "ALTER TABLE telemetrysample ADD COLUMN gen_seconds_total FLOAT"
                    ))
                _repair_retained_session_speeds(s, text)
                s.exec(text(
                    "UPDATE meta SET value = '3' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 3
            if version < 4:
                columns = {row[1] for row in s.exec(text(
                    "PRAGMA table_info(session)"
                )).all()}
                additions = {
                    "source_slot_id": "INTEGER",
                    "source_task_id": "INTEGER",
                    "live_prompt_tokens": "FLOAT",
                    "live_gen_tokens": "FLOAT",
                    "live_context": "INTEGER",
                    "live_gen_tps": "FLOAT",
                    "live_seen_at": "INTEGER",
                    "result_source": "VARCHAR",
                }
                for name, sql_type in additions.items():
                    if name not in columns:
                        s.exec(text(
                            f'ALTER TABLE session ADD COLUMN "{name}" {sql_type}'
                        ))
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_source_slot_id "
                    "ON session (source_slot_id)"
                ))
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_source_task_id "
                    "ON session (source_task_id)"
                ))
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_live_seen_at "
                    "ON session (live_seen_at)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '4' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 4
            if version < 5:
                columns = {row[1] for row in s.exec(text(
                    "PRAGMA table_info(session)"
                )).all()}
                additions = {
                    "live_gen_tps_avg": "FLOAT",
                    "live_gen_tps_3s": "FLOAT",
                }
                for name, sql_type in additions.items():
                    if name not in columns:
                        s.exec(text(
                            f'ALTER TABLE session ADD COLUMN "{name}" {sql_type}'
                        ))
                s.exec(text(
                    "UPDATE meta SET value = '5' WHERE key = 'schema_version'"
                ))
                s.commit()
            if version > SCHEMA_VERSION:
                pass


def _repair_retained_session_speeds(s: Session, text) -> None:
    """Repair sessions backed by recent raw samples with positive llama gauges."""
    cutoff = int((time.time() - 2 * 3600) * 1000)
    sessions = s.exec(text("""
        SELECT id, provider_id, model_id, prompt_tokens, gen_tokens
        FROM session
        WHERE start_at >= :cutoff AND model_id IS NOT NULL
    """), params={"cutoff": cutoff}).all()
    for sid, provider_id, model_id, prompt_tokens, gen_tokens in sessions:
        samples = s.exec(text("""
            SELECT ts, prompt_total, gen_total, prompt_tps, gen_tps
            FROM telemetrysample
            WHERE session_id = :sid
            ORDER BY ts
        """), params={"sid": sid}).all()
        prompt_work = []
        gen_work = []
        for ts, prompt_total, gen_total, prompt_tps, gen_tps in samples:
            prev = s.exec(text("""
                SELECT prompt_total, gen_total
                FROM telemetrysample
                WHERE provider_id = :provider_id AND model_id = :model_id AND ts < :ts
                ORDER BY ts DESC LIMIT 1
            """), params={
                "provider_id": provider_id, "model_id": model_id, "ts": ts,
            }).first()
            if prev is None:
                continue
            prev_prompt, prev_gen = prev
            d_prompt = max(0.0, (prompt_total or 0.0) - (prev_prompt or 0.0))
            d_gen = max(0.0, (gen_total or 0.0) - (prev_gen or 0.0))
            if d_prompt > 0 and prompt_tps is not None and prompt_tps > 0:
                prompt_work.append((d_prompt, prompt_tps))
            if d_gen > 0 and gen_tps is not None and gen_tps > 0:
                gen_work.append((d_gen, gen_tps))

        values = {"sid": sid}
        assignments = []
        if prompt_work:
            observed_tokens = sum(tokens for tokens, _ in prompt_work)
            observed_time = sum(tokens / tps for tokens, tps in prompt_work)
            avg_prompt = observed_tokens / observed_time
            values.update({
                "prompt_time": (prompt_tokens or observed_tokens) / avg_prompt,
                "prompt_tps": avg_prompt,
                "peak_prompt": max(tps for _, tps in prompt_work),
            })
            assignments.extend([
                "prompt_time_s = :prompt_time", "prompt_tps = :prompt_tps",
                "peak_prompt_tps = :peak_prompt",
            ])
        if gen_work:
            observed_tokens = sum(tokens for tokens, _ in gen_work)
            observed_time = sum(tokens / tps for tokens, tps in gen_work)
            avg_gen = observed_tokens / observed_time
            values.update({
                "gen_time": (gen_tokens or observed_tokens) / avg_gen,
                "avg_gen": avg_gen,
                "peak_gen": max(tps for _, tps in gen_work),
            })
            assignments.extend([
                "gen_time_s = :gen_time", "avg_gen_tps = :avg_gen",
                "peak_gen_tps = :peak_gen",
            ])
        if assignments:
            s.exec(text(
                "UPDATE session SET " + ", ".join(assignments) + " WHERE id = :sid"
            ), params=values)
