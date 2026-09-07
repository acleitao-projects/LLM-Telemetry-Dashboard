"""SQLite database setup, engine, and light migration support."""
from __future__ import annotations

import logging
import os
import time

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine

from . import models  # noqa: F401  (register tables)

log = logging.getLogger("observatory.database")

SCHEMA_VERSION = 15

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
        # The production database is small enough to fit in memory.  Keeping a
        # useful per-connection page cache avoids repeatedly reading the same
        # telemetry pages while the collector is writing in WAL mode.
        cur.execute("PRAGMA cache_size=-32768")
        cur.execute("PRAGMA temp_store=MEMORY")
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
                version = 5
            if version < 6:
                columns = {row[1] for row in s.exec(text(
                    "PRAGMA table_info(session)"
                )).all()}
                if "live_context_max" not in columns:
                    s.exec(text(
                        "ALTER TABLE session ADD COLUMN live_context_max INTEGER"
                    ))
                s.exec(text(
                    "UPDATE meta SET value = '6' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 6
            if version < 7:
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_telemetrysample_provider_ts "
                    "ON telemetrysample (provider_id, ts)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '7' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 7
            if version < 8:
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_gputelemetrysample_provider_ts "
                    "ON gputelemetrysample (provider_id, ts)"
                ))
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_provider_start "
                    "ON session (provider_id, start_at DESC)"
                ))
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_model_start "
                    "ON session (model_id, start_at DESC)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '8' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 8
            if version < 9:
                # Selected-model runtime lookups filter by a model then need
                # its latest telemetry sample.  The single-column model index
                # forced a production-sized timestamp sort on every card load.
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_telemetrysample_model_ts "
                    "ON telemetrysample (model_id, ts DESC)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '9' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 9
            if version < 10:
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_status_live_seen "
                    "ON session (status, live_seen_at DESC)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '10' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 10
            if version < 11:
                # Range aggregates include sessions that overlap the start of
                # a period, so the end-time side of that lookup needs an index
                # as well as the existing provider/start-time index.
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_session_provider_end "
                    "ON session (provider_id, end_at)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '11' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 11
            if version < 12:
                # G02: minute usage buckets + residency intervals. create_all
                # already created the tables and the partial open-interval
                # unique index; ensure the index on any DB that predates it and
                # backfill historical usage/residency from retained telemetry.
                s.exec(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_residency_open "
                    "ON modelresidency (provider_id, model_id) "
                    "WHERE unloaded_at IS NULL"
                ))
                _backfill_g02(s)
                s.exec(text(
                    "UPDATE meta SET value = '12' WHERE key = 'schema_version'"
                ))
                s.commit()
                version = 12
            if version < 13:
                _migrate_v12_to_v13(s)
                s.exec(text(
                    "UPDATE meta SET value = '13' WHERE key = 'schema_version'"
                ))
                s.commit()
            if version < 14:
                # P02: strict-range bucket aggregates SUM the seven value
                # columns for a (provider, model) minute window. The key-only
                # indexes force a table-row fetch per bucket row; this covering
                # index lets the grouped SUMs read only index pages.
                s.exec(text(
                    "CREATE INDEX IF NOT EXISTS ix_usagebucket_cov_values "
                    "ON modelusagebucket (provider_id, model_id, bucket_start, "
                    "input_tokens, output_tokens, unclassified_tokens, "
                    "prompt_time_s, gen_time_s, mtp_proposed, mtp_accepted)"
                ))
                s.exec(text(
                    "UPDATE meta SET value = '14' WHERE key = 'schema_version'"
                ))
                s.commit()
            if version < 15:
                # G09 (#23): automatic model-pricing sync. Additive columns on
                # `model`, a run-log table, and a `pricing_mode` backfill that
                # locks every already-priced row to 'manual'.
                _migrate_v14_to_v15(s)
                s.exec(text(
                    "UPDATE meta SET value = '15' WHERE key = 'schema_version'"
                ))
                s.commit()
            if version > SCHEMA_VERSION:
                pass
        _ensure_indexes(s)


# Every index the query plans depend on, created unconditionally on each
# startup.
#
# These used to live only inside `if version < N:` blocks, which turned out to
# mean two different databases never got them.  A freshly created database
# stamps meta.schema_version at SCHEMA_VERSION and runs no migration block at
# all, so an index declared only in a block was never created -- create_all
# only makes what the models declare in __table_args__.  And an existing
# database that had already passed version N before a CREATE INDEX was added
# to that block skipped it permanently, because the block never runs again.
#
# Both cases were real: ix_telemetrysample_model_ts, ix_session_provider_start,
# ix_session_model_start, ix_session_status_live_seen and
# ix_gputelemetrysample_provider_ts were absent from a fresh database and from
# the development copy alike, while the endpoints that need them were being
# tuned against plans that assumed they existed.
#
# They are all declared in __table_args__ now, which covers new databases.
# This runs for the ones that already exist.  CREATE INDEX IF NOT EXISTS is a
# no-op when the index is present, so the cost on a healthy database is one
# catalogue lookup each.
_REQUIRED_INDEXES = (
    ("ix_telemetrysample_provider_ts", "telemetrysample (provider_id, ts)"),
    ("ix_telemetrysample_model_ts", "telemetrysample (model_id, ts DESC)"),
    ("ix_gputelemetrysample_provider_ts", "gputelemetrysample (provider_id, ts)"),
    ("ix_session_provider_start", "session (provider_id, start_at DESC)"),
    ("ix_session_provider_end", "session (provider_id, end_at)"),
    ("ix_session_model_start", "session (model_id, start_at DESC)"),
    ("ix_session_status_live_seen", "session (status, live_seen_at DESC)"),
    ("ix_modelconfig_model_created", "modelconfig (model_id, created_at DESC)"),
    ("ix_pricingsyncrun_started", "pricingsyncrun (started_at DESC)"),
)


def _ensure_indexes(s: Session) -> list[str]:
    """Create any missing performance index; return the names actually created."""
    from sqlalchemy import text
    present = {row[0] for row in s.exec(text(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    )).all()}
    created = []
    for name, definition in _REQUIRED_INDEXES:
        if name in present:
            continue
        s.exec(text(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}"))
        created.append(name)
    if created:
        s.commit()
        log.warning("created %d missing index(es): %s", len(created), ", ".join(created))
    return created


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


def _backfill_g02(s: Session) -> None:
    """Idempotently backfill G02 usage buckets and residency intervals from
    retained telemetry.

    Retained telemetry is authoritative and preferred. Deltas between
    consecutive samples per (provider, model) are positive-only: a counter
    decrease is treated as a new baseline and never subtracts prior usage.
    Only ``provenance='backfill'`` / ``source='backfill'`` rows are removed and
    recomputed, so re-running never duplicates data or touches collector-sourced
    rows, and it never rewrites session/token history.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy import text

    from .models import ModelResidency, ModelUsageBucket

    minute_ms = 60_000
    s.exec(text("DELETE FROM modelusagebucket WHERE provenance = 'backfill'"))
    s.exec(text("DELETE FROM modelresidency WHERE source = 'backfill'"))

    rows = s.exec(text("""
        SELECT provider_id, model_id, ts, state,
               tokens_total, prompt_total, gen_total,
               prompt_seconds_total, gen_seconds_total,
               mtp_proposed_total, mtp_accepted_total
        FROM telemetrysample
        WHERE model_id IS NOT NULL
        ORDER BY provider_id, model_id, ts
    """)).all()

    def positive_delta(cur, prev):
        if cur is None or prev is None:
            return 0.0
        d = float(cur) - float(prev)
        return d if d > 0 else 0.0

    buckets: dict[tuple, dict] = {}
    residencies: list[dict] = []
    prev_sample: dict[tuple, tuple] = {}
    loaded_start: dict[tuple, Optional[int]] = {}
    loaded_last: dict[tuple, int] = {}

    for (pid, mid, ts, state, tokens_total, prompt_total, gen_total,
         prompt_seconds_total, gen_seconds_total,
         mtp_proposed_total, mtp_accepted_total) in rows:
        key = (pid, mid)
        p = prev_sample.get(key)
        if p is not None:
            d_prompt = positive_delta(prompt_total, p[4])
            d_gen = positive_delta(gen_total, p[5])
            d_tokens = positive_delta(tokens_total, p[3])
            d_prompt_s = positive_delta(prompt_seconds_total, p[6])
            d_gen_s = positive_delta(gen_seconds_total, p[7])
            d_prop = positive_delta(mtp_proposed_total, p[8])
            d_acc = positive_delta(mtp_accepted_total, p[9])
            if d_prompt or d_gen or d_tokens or d_prompt_s or d_gen_s or d_prop or d_acc:
                minute = (ts // minute_ms) * minute_ms
                b = buckets.setdefault((pid, mid, minute), {
                    "input_tokens": 0.0, "output_tokens": 0.0,
                    "unclassified_tokens": 0.0, "prompt_time_s": 0.0,
                    "gen_time_s": 0.0, "mtp_proposed": 0.0, "mtp_accepted": 0.0,
                    "first_observed_at": ts, "last_observed_at": ts,
                })
                b["input_tokens"] += d_prompt
                b["output_tokens"] += d_gen
                b["unclassified_tokens"] += max(0.0, d_tokens - d_prompt - d_gen)
                b["prompt_time_s"] += d_prompt_s
                b["gen_time_s"] += d_gen_s
                b["mtp_proposed"] += d_prop
                b["mtp_accepted"] += d_acc
                b["first_observed_at"] = min(b["first_observed_at"], ts)
                b["last_observed_at"] = max(b["last_observed_at"], ts)
        prev_sample[key] = (pid, mid, ts, tokens_total, prompt_total, gen_total,
                            prompt_seconds_total, gen_seconds_total,
                            mtp_proposed_total, mtp_accepted_total)

        is_loaded = state is not None and state != "UNLOADED"
        if is_loaded:
            loaded_last[key] = ts
            if loaded_start.get(key) is None:
                loaded_start[key] = ts
        elif loaded_start.get(key) is not None:
            residencies.append({
                "provider_id": pid, "model_id": mid, "loaded_at": loaded_start[key],
                "last_seen_at": loaded_last.get(key, ts), "unloaded_at": ts,
                "source": "backfill", "estimated": False,
            })
            loaded_start[key] = None

    for key, start in list(loaded_start.items()):
        if start is not None:
            pid, mid = key
            last = loaded_last.get(key, start)
            residencies.append({
                "provider_id": pid, "model_id": mid, "loaded_at": start,
                "last_seen_at": last, "unloaded_at": last,
                "source": "backfill", "estimated": True,
            })

    if buckets:
        s.execute(sqlite_insert(ModelUsageBucket).values([
            {**b, "provider_id": pid, "model_id": mid, "bucket_start": minute,
             "estimated": False, "provenance": "backfill"}
            for (pid, mid, minute), b in buckets.items()
        ]).on_conflict_do_nothing(
            index_elements=["provider_id", "model_id", "bucket_start"]))
    if residencies:
        s.execute(sqlite_insert(ModelResidency).values(residencies))
    s.commit()


class MigrationAbortedError(Exception):
    """Raised when a migration cannot proceed safely due to data conflicts."""
    def __init__(self, message: str, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(message)


def _migrate_v12_to_v13(s: Session) -> None:
    """G03: Add catalog availability, pricing columns, and unique constraints.

    Preflight checks for duplicate (provider_id, key) on model and
    (model_id, fingerprint) on modelconfig. If duplicates exist, raises
    MigrationAbortedError without modifying the database.
    """
    from sqlalchemy import text

    # --- Preflight: check for duplicates before applying constraints ---
    dup_models = s.exec(text("""
        SELECT provider_id, key, GROUP_CONCAT(id) as ids
        FROM model GROUP BY provider_id, key HAVING COUNT(*) > 1
    """)).all()
    if dup_models:
        conflicts = [
            {"table": "model", "provider_id": r[0], "key": r[1], "ids": r[2]}
            for r in dup_models
        ]
        raise MigrationAbortedError(
            f"Cannot create uq_model_provider_key: {len(dup_models)} duplicate "
            f"(provider_id, key) group(s) exist", conflicts)

    dup_configs = s.exec(text("""
        SELECT model_id, fingerprint, GROUP_CONCAT(id) as ids
        FROM modelconfig GROUP BY model_id, fingerprint HAVING COUNT(*) > 1
    """)).all()
    if dup_configs:
        conflicts = [
            {"table": "modelconfig", "model_id": r[0],
             "fingerprint": r[1], "ids": r[2]}
            for r in dup_configs
        ]
        raise MigrationAbortedError(
            f"Cannot create uq_modelconfig_model_fingerprint: "
            f"{len(dup_configs)} duplicate (model_id, fingerprint) group(s) exist",
            conflicts)

    # --- Additive column additions (idempotent) ---
    model_cols = {row[1] for row in s.exec(text(
        "PRAGMA table_info(model)")).all()}
    if "catalog_available" not in model_cols:
        s.exec(text(
            "ALTER TABLE model ADD COLUMN catalog_available BOOLEAN NOT NULL DEFAULT 1"))
    if "catalog_last_seen_at" not in model_cols:
        s.exec(text(
            "ALTER TABLE model ADD COLUMN catalog_last_seen_at INTEGER"))
    if "input_price_per_million" not in model_cols:
        s.exec(text(
            "ALTER TABLE model ADD COLUMN input_price_per_million TEXT"))
    if "output_price_per_million" not in model_cols:
        s.exec(text(
            "ALTER TABLE model ADD COLUMN output_price_per_million TEXT"))

    # --- Populate catalog fields for existing v12 rows ---
    # catalog_available: existing rows are already observed catalog records
    # (the NOT NULL DEFAULT 1 backfill already set them; this is a no-op guard).
    s.exec(text(
        "UPDATE model SET catalog_available = 1 WHERE catalog_available IS NULL"))
    # catalog_last_seen_at: derive from existing observation data. Rows with
    # neither last_used_at nor first_seen_at keep NULL (no invented timestamp).
    s.exec(text("""
        UPDATE model SET catalog_last_seen_at = COALESCE(last_used_at, first_seen_at)
        WHERE catalog_last_seen_at IS NULL
          AND (last_used_at IS NOT NULL OR first_seen_at IS NOT NULL)
    """))

    # --- Create unique indexes (safe: preflight confirmed no duplicates) ---
    s.exec(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_provider_key "
        "ON model (provider_id, key)"))
    s.exec(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_modelconfig_model_fingerprint "
        "ON modelconfig (model_id, fingerprint)"))

    # --- Add supporting index ---
    s.exec(text(
        "CREATE INDEX IF NOT EXISTS ix_modelconfig_model_created "
        "ON modelconfig (model_id, created_at DESC)"))


def _migrate_v14_to_v15(s: Session) -> None:
    """G09 (#23): automatic model-pricing sync schema.

    Additive and idempotent. Adds cache-write/read price columns plus
    pricing-mode/provenance/sync-state columns to `model`, creates the
    `pricingsyncrun` run-log table, and backfills `pricing_mode='manual'` for
    every row that already carries a hand-entered price (the G05 Settings UI is
    the only thing that has ever written those columns). Rows with no price
    keep the ORM default `'auto'` and become eligible for the daily sync.

    Authoritative history tables are untouched.
    """
    from sqlalchemy import text

    model_cols = {row[1] for row in s.exec(text(
        "PRAGMA table_info(model)")).all()}
    additions = {
        "cache_write_price_per_million": "TEXT",
        "cache_read_price_per_million": "TEXT",
        "pricing_mode": "TEXT NOT NULL DEFAULT 'auto'",
        "pricing_source": "TEXT",
        "pricing_litellm_key": "TEXT",
        "pricing_synced_at": "INTEGER",
        "pricing_stale": "BOOLEAN NOT NULL DEFAULT 0",
        "pricing_last_error": "TEXT",
    }
    for name, decl in additions.items():
        if name not in model_cols:
            s.exec(text(f"ALTER TABLE model ADD COLUMN {name} {decl}"))

    s.exec(text("""
        CREATE TABLE IF NOT EXISTS pricingsyncrun (
            id INTEGER PRIMARY KEY,
            trigger TEXT,
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            result TEXT DEFAULT 'running',
            attempted INTEGER DEFAULT 0,
            updated INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            unresolved INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            error_summary TEXT,
            litellm_commit TEXT,
            created_at INTEGER
        )
    """))
    s.exec(text(
        "CREATE INDEX IF NOT EXISTS ix_pricingsyncrun_started "
        "ON pricingsyncrun (started_at DESC)"))

    # Lock every already-priced row to manual ownership. Runs once; on a second
    # pass the rows are already 'manual' so it is a no-op.
    s.exec(text("""
        UPDATE model SET pricing_mode = 'manual'
        WHERE pricing_mode = 'auto'
          AND (input_price_per_million IS NOT NULL
               OR output_price_per_million IS NOT NULL)
    """))
