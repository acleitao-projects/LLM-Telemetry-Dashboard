"""Verify G03 migration (v12 -> v13) does not lose production data.

Builds a genuine schema-v12 database (no G03 columns or unique indexes), runs the
real migration, and verifies: no data loss (content checksums per table), catalog
fields populated from existing observation data, unique constraints created,
idempotency, duplicate-conflict abort, and fresh/migrated schema equivalence.
"""
from __future__ import annotations

import hashlib
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy import insert as sa_insert
from sqlmodel import Session, SQLModel, create_engine

from observatory import database as odb
from observatory.models import (GpuTelemetrySample, Model, ModelConfig, ModelResidency,
                                 ModelUsageBucket, Provider, SessionRow, TelemetrySample, now_ms)

MINUTE_MS = 60_000
DAY_MS = 86_400_000

# Columns/indexes introduced by G03 (schema v13). A genuine v12 DB must not have them.
G03_MODEL_COLUMNS = ("catalog_available", "catalog_last_seen_at",
                     "input_price_per_million", "output_price_per_million")
G03_INDEXES = ("uq_model_provider_key", "uq_modelconfig_model_fingerprint",
               "ix_modelconfig_model_created")

# A genuine v12 model/modelconfig table has no G03 unique constraints.  A fresh
# create_all enforces them via SQLite autoindexes that cannot be dropped in place,
# so duplicate-conflict fixtures rebuild these two tables from v12 DDL.
V12_MODEL_DDL = """
CREATE TABLE model (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES provider(id),
    key VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    quant VARCHAR,
    family VARCHAR,
    arch VARCHAR,
    params VARCHAR,
    color VARCHAR NOT NULL DEFAULT '#4b8de8',
    first_seen_at INTEGER NOT NULL,
    last_used_at INTEGER
)
"""

V12_MODELCONFIG_DDL = """
CREATE TABLE modelconfig (
    id INTEGER PRIMARY KEY,
    model_id INTEGER NOT NULL REFERENCES model(id),
    fingerprint VARCHAR NOT NULL,
    payload VARCHAR NOT NULL DEFAULT '{}',
    context INTEGER,
    kv_cache_k VARCHAR,
    kv_cache_v VARCHAR,
    flash_attn BOOLEAN,
    parallel INTEGER,
    split_mode VARCHAR,
    tensor_split VARCHAR,
    gpu_layers INTEGER,
    cpu_moe INTEGER,
    threads INTEGER,
    batch INTEGER,
    ubatch INTEGER,
    reasoning VARCHAR,
    reasoning_effort VARCHAR,
    reasoning_preserve BOOLEAN,
    mmproj VARCHAR,
    mtp_enabled BOOLEAN,
    mtp_model VARCHAR,
    speculative VARCHAR,
    created_at INTEGER NOT NULL
)
"""


def _recreate_v12_tables(s) -> None:
    """Rebuild model/modelconfig from v12 DDL (no G03 unique constraints)."""
    s.exec(text("DROP TABLE IF EXISTS modelconfig"))
    s.exec(text("DROP TABLE IF EXISTS model"))
    s.exec(text(V12_MODEL_DDL))
    s.exec(text(V12_MODELCONFIG_DDL))
    s.commit()


def _strip_g03_schema(engine) -> None:
    """Drop G03 columns/indexes so the database is a genuine schema-v12 database."""
    with Session(engine) as s:
        for idx in G03_INDEXES:
            s.exec(text(f"DROP INDEX IF EXISTS {idx}"))
        cols = {row[1] for row in s.exec(text("PRAGMA table_info(model)")).all()}
        for col in G03_MODEL_COLUMNS:
            if col in cols:
                s.exec(text(f'ALTER TABLE model DROP COLUMN "{col}"'))
        s.commit()


def _table_checksum(path: str, table: str, exclude_cols: frozenset = frozenset()) -> str:
    """Compute a stable SHA-256 checksum of a table's content (ordered by id).

    Excludes G03 columns for the model table since those are expected to change.
    """
    conn = sqlite3.connect(path)
    c = conn.cursor()
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    cols = [col for col in cols if col not in exclude_cols]
    if not cols:
        conn.close()
        return "empty"
    col_list = ", ".join(f'"{col}"' for col in cols)
    rows = c.execute(f'SELECT {col_list} FROM "{table}" ORDER BY id').fetchall()
    conn.close()
    h = hashlib.sha256()
    for row in rows:
        h.update(repr(row).encode())
    return h.hexdigest()


def _all_table_checksums(path: str) -> dict:
    """Compute content checksums for all authoritative/history tables."""
    return {
        "provider": _table_checksum(path, "provider"),
        "model": _table_checksum(path, "model", exclude_cols=frozenset(G03_MODEL_COLUMNS)),
        "modelconfig": _table_checksum(path, "modelconfig"),
        "session": _table_checksum(path, "session"),
        "telemetrysample": _table_checksum(path, "telemetrysample"),
        "gputelemetrysample": _table_checksum(path, "gputelemetrysample"),
        "modelusagebucket": _table_checksum(path, "modelusagebucket"),
        "modelresidency": _table_checksum(path, "modelresidency"),
    }


def build_v12_db(path: str) -> dict:
    """Build a production-shaped, genuine schema-v12 database using the real ORM."""
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    rng = random.Random(42)
    now = now_ms()
    start = now - 3 * DAY_MS

    with Session(engine) as s:
        provs = []
        for i in range(2):
            p = Provider(name=f"router-{i}", base_url=f"http://r{i}:8080",
                         ptype="llama.cpp", status="LIVE", enabled=True,
                         is_default=(i == 0), last_success_at=now - 1000)
            s.add(p)
            s.commit()
            s.refresh(p)
            provs.append(p)

        models = []
        for p in provs:
            for j in range(3):
                m = Model(provider_id=p.id, key=f"m{p.id}-{j}", name=f"model-{p.id}-{j}",
                          family=f"fam{j % 2}", arch="llama", params="8B",
                          quant="Q4_K_M", first_seen_at=start,
                          last_used_at=now - rng.randint(0, DAY_MS))
                s.add(m)
                s.commit()
                s.refresh(m)
                models.append(m)

        for m in models:
            for k in range(2):
                s.add(ModelConfig(model_id=m.id, fingerprint=f"FP{m.id}{k}",
                                  payload='{"context": 4096}', context=4096))
        s.commit()

        sess_rows = []
        for _ in range(20):
            m = rng.choice(models)
            s_at = start + rng.randint(0, max(1, now - start - 300_000))
            dur = rng.randint(15_000, 900_000)
            e_at = min(now, s_at + dur)
            p_tok = rng.randint(100, 4000)
            g_tok = rng.randint(50, 8000)
            sess_rows.append({
                "provider_id": m.provider_id, "model_id": m.id,
                "start_at": s_at, "end_at": e_at, "duration_s": (e_at - s_at) / 1000.0,
                "prompt_time_s": p_tok / 250.0, "gen_time_s": g_tok / 40.0,
                "prompt_tokens": p_tok, "gen_tokens": g_tok, "total_tokens": p_tok + g_tok,
                "prompt_tps": 250.0, "avg_gen_tps": 40.0, "peak_gen_tps": 60.0,
                "peak_prompt_tps": 350.0, "status": "CLOSED", "result_source": "metrics",
            })
        if sess_rows:
            s.execute(sa_insert(SessionRow).values(sess_rows))
            s.commit()

        telemetry_rows = []
        for m in models:
            tokens = prompt = gen = 0.0
            ts = start
            while ts <= now:
                if rng.random() < 0.7:
                    dp = rng.randint(50, 400)
                    dg = rng.randint(20, 200)
                    prompt += dp
                    gen += dg
                    tokens = prompt + gen
                    telemetry_rows.append({
                        "provider_id": m.provider_id, "model_id": m.id, "ts": ts,
                        "state": "GENERATING", "tokens_total": tokens,
                        "prompt_total": prompt, "gen_total": gen,
                        "prompt_tps": 240.0, "gen_tps": rng.uniform(20, 60),
                    })
                ts += 900_000
        if telemetry_rows:
            s.execute(sa_insert(TelemetrySample).values(telemetry_rows))
            s.commit()

        gpu_rows = []
        for gi, m in enumerate(models[:2]):
            for i in range(5):
                gpu_rows.append({
                    "provider_id": m.provider_id,
                    "ts": start + i * 60_000, "gpu_key": f"gpu-{m.id}-{gi}",
                    "gpu_index": gi,
                    "util": float(rng.randint(40, 95)),
                    "vram_used_mb": float(rng.randint(2000, 12000)),
                    "temp_c": float(rng.randint(50, 85)),
                    "power_w": float(rng.randint(100, 350)),
                })
        if gpu_rows:
            s.execute(sa_insert(GpuTelemetrySample).values(gpu_rows))
            s.commit()

        bucket_rows = []
        for m in models:
            for minute_offset in range(0, 20):
                bs = start + minute_offset * MINUTE_MS
                bucket_rows.append({
                    "provider_id": m.provider_id, "model_id": m.id,
                    "bucket_start": bs, "input_tokens": rng.randint(100, 1000),
                    "output_tokens": rng.randint(50, 500),
                    "first_observed_at": bs, "last_observed_at": bs + MINUTE_MS,
                    "provenance": "collector",
                })
        if bucket_rows:
            s.execute(sa_insert(ModelUsageBucket).values(bucket_rows))
            s.commit()

        res_rows = []
        for m in models[:4]:
            res_rows.append({
                "provider_id": m.provider_id, "model_id": m.id,
                "loaded_at": start, "unloaded_at": now - DAY_MS, "source": "collector",
            })
        if res_rows:
            s.execute(sa_insert(ModelResidency).values(res_rows))
            s.commit()

        sample = (models[0].id, models[0].last_used_at, models[0].first_seen_at)

    # Strip G03 schema so this is a genuine v12 database (columns re-added by migration).
    _strip_g03_schema(engine)

    with Session(engine) as s:
        s.exec(text("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"))
        s.exec(text("DELETE FROM meta WHERE key = 'schema_version'"))
        s.exec(text("INSERT INTO meta (key, value) VALUES ('schema_version', '12')"))
        s.commit()
        counts = {
            "model": len(s.exec(text("SELECT 1 FROM model")).all()),
            "provider": len(s.exec(text("SELECT 1 FROM provider")).all()),
            "session": len(s.exec(text("SELECT 1 FROM session")).all()),
            "modelconfig": len(s.exec(text("SELECT 1 FROM modelconfig")).all()),
            "telemetrysample": len(s.exec(text("SELECT 1 FROM telemetrysample")).all()),
            "gputelemetrysample": len(s.exec(text("SELECT 1 FROM gputelemetrysample")).all()),
            "modelusagebucket": len(s.exec(text("SELECT 1 FROM modelusagebucket")).all()),
            "modelresidency": len(s.exec(text("SELECT 1 FROM modelresidency")).all()),
        }

    engine.dispose()
    checksums = _all_table_checksums(path)
    return {"counts": counts, "checksums": checksums, "now": now,
            "models": len(models), "sample": sample}


def get_post_state(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    counts = {}
    for table in ["model", "provider", "session", "modelconfig",
                  "telemetrysample", "gputelemetrysample", "modelusagebucket", "modelresidency"]:
        counts[table] = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    version = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    model_cols = {row[1] for row in c.execute("PRAGMA table_info(model)").fetchall()}
    has_new_cols = all(col in model_cols for col in G03_MODEL_COLUMNS)
    catalog_rows = c.execute(
        "SELECT COUNT(*) FROM model WHERE catalog_available = 1").fetchone()[0]
    last_seen_rows = c.execute(
        "SELECT COUNT(*) FROM model WHERE catalog_last_seen_at IS NOT NULL").fetchone()[0]
    indices = {row[0] for row in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    return {"counts": counts, "version": version, "has_new_cols": has_new_cols,
            "catalog_available_count": catalog_rows, "last_seen_count": last_seen_rows,
            "has_model_uq": "uq_model_provider_key" in indices,
            "has_config_uq": "uq_modelconfig_model_fingerprint" in indices}


def run_migration(db_path: str):
    """Run the real migration code (create_all is a no-op on an existing DB)."""
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False, "timeout": 15})
    SQLModel.metadata.create_all(engine)
    odb._migrate(engine)
    engine.dispose()


class MigrationV12ToV13Tests(unittest.TestCase):
    """Verify the G03 migration (v12 -> v13) does not lose production data."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="g03_mig_safety_")
        cls.db_path = os.path.join(cls.tmpdir, "prod.db")
        cls.pre = build_v12_db(cls.db_path)
        run_migration(cls.db_path)
        cls.post = get_post_state(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_no_existing_data_lost(self):
        for table in self.pre["counts"]:
            self.assertEqual(self.pre["counts"][table], self.post["counts"][table],
                             f"Row count changed for {table}")

    def test_content_checksums_unchanged(self):
        """Per-table content (excluding G03 columns) must be byte-identical after migration."""
        post_checksums = _all_table_checksums(self.db_path)
        for table in self.pre["checksums"]:
            self.assertEqual(
                self.pre["checksums"][table], post_checksums[table],
                f"Content checksum changed for table '{table}'")

    def test_version_bumped(self):
        # G03 raised the schema to 13; the later P02 covering-index migration
        # bumped it to 14. A v12 DB is migrated forward through the full chain.
        self.assertGreaterEqual(int(self.post["version"]), 13)

    def test_new_columns_added(self):
        self.assertTrue(self.post["has_new_cols"])

    def test_catalog_available_populated(self):
        self.assertEqual(self.post["catalog_available_count"], self.pre["models"])

    def test_catalog_last_seen_derived(self):
        self.assertGreater(self.post["last_seen_count"], 0)
        model_id, last_used_at, first_seen_at = self.pre["sample"]
        conn = sqlite3.connect(self.db_path)
        seen = conn.execute(
            "SELECT catalog_last_seen_at FROM model WHERE id = ?", (model_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(seen, last_used_at if last_used_at is not None else first_seen_at)

    def test_unique_indexes_created(self):
        self.assertTrue(self.post["has_model_uq"])
        self.assertTrue(self.post["has_config_uq"])

    def test_idempotency(self):
        before = get_post_state(self.db_path)
        run_migration(self.db_path)
        after = get_post_state(self.db_path)
        for table in before["counts"]:
            self.assertEqual(before["counts"][table], after["counts"][table])
        self.assertEqual(before["version"], after["version"])


class MigrationDuplicateAbortTests(unittest.TestCase):
    """Verify the migration refuses safely when duplicates exist (no data loss)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="g03_mig_dup_")
        self.db_path = os.path.join(self.tmpdir, "dup.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _assert_version_unchanged(self):
        conn = sqlite3.connect(self.db_path)
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        conn.close()
        self.assertEqual(version, "12")

    def test_duplicate_model_aborts(self):
        """Duplicate (provider_id, key) causes a clean abort before any change."""
        from observatory.database import MigrationAbortedError
        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        now = now_ms()
        with Session(engine) as s:
            _recreate_v12_tables(s)
            p = Provider(name="test", base_url="http://t:8080", ptype="llama.cpp")
            s.add(p)
            s.commit()
            s.refresh(p)
            for name in ("A", "B"):
                s.exec(text(
                    "INSERT INTO model (provider_id, key, name, first_seen_at) "
                    "VALUES (:pid, :key, :name, :now)"),
                    params={"pid": p.id, "key": "same-key", "name": name, "now": now})
            s.exec(text("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"))
            s.exec(text("INSERT INTO meta (key, value) VALUES ('schema_version', '12')"))
            s.commit()
            model_count = len(s.exec(text("SELECT 1 FROM model")).all())
        engine.dispose()

        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        with self.assertRaises(MigrationAbortedError) as ctx:
            odb._migrate(engine)
        engine.dispose()

        self.assertIn("uq_model_provider_key", str(ctx.exception))
        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertEqual(ctx.exception.conflicts[0]["key"], "same-key")

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM model").fetchone()[0]
        conn.close()
        self.assertEqual(count, model_count)
        self._assert_version_unchanged()

    def test_duplicate_config_aborts(self):
        """Duplicate (model_id, fingerprint) causes a clean abort."""
        from observatory.database import MigrationAbortedError
        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        now = now_ms()
        with Session(engine) as s:
            _recreate_v12_tables(s)
            p = Provider(name="test", base_url="http://t:8080", ptype="llama.cpp")
            s.add(p)
            s.commit()
            s.refresh(p)
            s.exec(text(
                "INSERT INTO model (provider_id, key, name, first_seen_at) "
                "VALUES (:pid, :key, :name, :now)"),
                params={"pid": p.id, "key": "k1", "name": "M1", "now": now})
            s.commit()
            mid = s.exec(text("SELECT id FROM model WHERE key = 'k1'")).one()[0]
            for _ in range(2):
                s.exec(text(
                    "INSERT INTO modelconfig (model_id, fingerprint, payload, created_at) "
                    "VALUES (:mid, 'DUP', '{}', :now)"), params={"mid": mid, "now": now})
            s.exec(text("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"))
            s.exec(text("INSERT INTO meta (key, value) VALUES ('schema_version', '12')"))
            s.commit()
        engine.dispose()

        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        with self.assertRaises(MigrationAbortedError) as ctx:
            odb._migrate(engine)
        engine.dispose()

        self.assertIn("uq_modelconfig_model_fingerprint", str(ctx.exception))
        self.assertEqual(ctx.exception.conflicts[0]["fingerprint"], "DUP")
        self._assert_version_unchanged()


def _assert_unique_enforced(path: str) -> None:
    """Both fresh and migrated DBs must reject duplicate (provider_id, key)."""
    from sqlalchemy.exc import IntegrityError
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with Session(engine) as s:
        p = Provider(name="dupchk", ptype="llama.cpp", base_url="http://x")
        s.add(p)
        s.commit()
        s.refresh(p)
        now = now_ms()
        try:
            for name in ("X", "Y"):
                s.exec(text(
                    "INSERT INTO model (provider_id, key, name, color, catalog_available, "
                    "first_seen_at) VALUES (:pid, 'dupkey', :name, :color, 1, :now)"),
                    params={"pid": p.id, "name": name, "color": "#4b8de8", "now": now})
            s.commit()
        except IntegrityError:
            s.rollback()
        else:
            s.rollback()
            raise AssertionError("duplicate (provider_id, key) was not rejected")
    engine.dispose()


class FreshDbEquivalenceTests(unittest.TestCase):
    """Fresh create_all DB and migrated v12 -> v13 DB have equivalent G03 schemas."""

    def test_schema_equivalence(self):
        tmpdir = tempfile.mkdtemp(prefix="g03_equiv_")
        try:
            fresh_path = os.path.join(tmpdir, "fresh.db")
            engine = create_engine(f"sqlite:///{fresh_path}",
                                   connect_args={"check_same_thread": False})
            SQLModel.metadata.create_all(engine)
            fresh_cols = {r[1] for r in
                          engine.raw_connection().execute("PRAGMA table_info(model)").fetchall()}
            engine.dispose()

            mig_path = os.path.join(tmpdir, "mig.db")
            build_v12_db(mig_path)
            run_migration(mig_path)
            conn = sqlite3.connect(mig_path)
            mig_cols = {r[1] for r in conn.execute("PRAGMA table_info(model)").fetchall()}
            conn.close()

            for col in G03_MODEL_COLUMNS:
                self.assertIn(col, fresh_cols, f"fresh DB missing {col}")
                self.assertIn(col, mig_cols, f"migrated DB missing {col}")
            # Uniqueness on (provider_id, key) must be enforced in both, whether
            # via a create_all autoindex (fresh) or a named unique index (migrated).
            _assert_unique_enforced(fresh_path)
            _assert_unique_enforced(mig_path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
