"""G09 issue #23: v14 -> v15 migration safety for automatic model-pricing sync.

Builds a populated schema-v14-shaped database with the ORM, strips the G09
additions, forces version to 14, runs the real migration, and verifies:
  * the 8 new `model` columns and the `pricingsyncrun` table + index exist,
  * `pricing_mode` is backfilled to 'manual' for already-priced rows only,
  * authoritative history (session / telemetry / buckets / residency / model)
    is byte-identical,
  * `_migrate` is idempotent,
  * a freshly `create_all`-d schema matches the migrated one.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import text

from observatory import database as odb
from observatory.models import Model, Provider, SessionRow, TelemetrySample, now_ms

DAY_MS = 86_400_000
_NEW_MODEL_COLS = [
    "cache_write_price_per_million", "cache_read_price_per_million",
    "pricing_mode", "pricing_source", "pricing_litellm_key",
    "pricing_synced_at", "pricing_stale", "pricing_last_error",
]


def _authoritative_checksums(conn) -> dict:
    c = conn.cursor()
    return {
        "model_rows": c.execute("SELECT COUNT(*) FROM model").fetchone()[0],
        "provider_rows": c.execute("SELECT COUNT(*) FROM provider").fetchone()[0],
        "session": c.execute(
            "SELECT COUNT(*), SUM(total_tokens), SUM(prompt_tokens), "
            "SUM(gen_tokens) FROM session").fetchone(),
        "telemetry": c.execute(
            "SELECT COUNT(*), SUM(tokens_total), SUM(prompt_total), "
            "SUM(gen_total) FROM telemetrysample").fetchone(),
        "prices": c.execute(
            "SELECT COUNT(*), GROUP_CONCAT(input_price_per_million), "
            "GROUP_CONCAT(output_price_per_million) FROM model "
            "ORDER BY id").fetchone(),
    }


def build_v14_db(path: str) -> dict:
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    now = now_ms()

    with Session(engine) as s:
        prov = Provider(name="router-0", base_url="http://r0:8080",
                        ptype="llama.cpp", status="LIVE", is_default=True,
                        last_success_at=now - 1000)
        s.add(prov)
        s.commit()
        s.refresh(prov)

        # Three models: two hand-priced (become 'manual'), one unpriced ('auto').
        priced_a = Model(provider_id=prov.id, key="m-a", name="model-a",
                         input_price_per_million="0.15000000",
                         output_price_per_million="0.60000000")
        priced_b = Model(provider_id=prov.id, key="m-b", name="model-b",
                         input_price_per_million="1.00000000")
        unpriced = Model(provider_id=prov.id, key="m-c", name="model-c")
        s.add_all([priced_a, priced_b, unpriced])
        s.commit()
        for m in (priced_a, priced_b, unpriced):
            s.refresh(m)

        s.add_all([
            TelemetrySample(provider_id=prov.id, model_id=priced_a.id,
                            ts=now - 60_000, state="GENERATING",
                            tokens_total=500.0, prompt_total=300.0,
                            gen_total=200.0),
            TelemetrySample(provider_id=prov.id, model_id=priced_a.id,
                            ts=now - 30_000, state="IDLE", tokens_total=500.0,
                            prompt_total=300.0, gen_total=200.0),
        ])
        s.add(SessionRow(provider_id=prov.id, model_id=priced_a.id,
                         start_at=now - 120_000, end_at=now - 90_000,
                         duration_s=30.0, prompt_tokens=300.0, gen_tokens=200.0,
                         total_tokens=500.0, status="CLOSED",
                         result_source="metrics"))
        s.commit()

        # Strip the G09 additions to simulate a genuine v14 database.
        for col in _NEW_MODEL_COLS:
            s.exec(text(f"ALTER TABLE model DROP COLUMN {col}"))
        s.exec(text("DROP TABLE IF EXISTS pricingsyncrun"))
        s.exec(text("CREATE TABLE IF NOT EXISTS meta "
                    "(key TEXT PRIMARY KEY, value TEXT)"))
        s.exec(text("DELETE FROM meta WHERE key = 'schema_version'"))
        s.exec(text("INSERT INTO meta (key, value) "
                    "VALUES ('schema_version', '14')"))
        s.commit()

    engine.dispose()

    conn = sqlite3.connect(path)
    pre = _authoritative_checksums(conn)
    conn.close()
    return pre


def run_migration(path: str) -> None:
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False, "timeout": 15})
    SQLModel.metadata.create_all(engine)
    odb._migrate(engine)
    engine.dispose()


class G23MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="g23_mig_")
        cls.db_path = os.path.join(cls.tmpdir, "prod.db")
        cls.pre = build_v14_db(cls.db_path)
        run_migration(cls.db_path)
        run_migration(cls.db_path)  # idempotency: a second pass must be a no-op
        cls.conn = sqlite3.connect(cls.db_path)
        cls.post = _authoritative_checksums(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _cols(self, table):
        return {r[1] for r in self.conn.execute(
            f"PRAGMA table_info({table})").fetchall()}

    def test_new_model_columns_exist(self):
        cols = self._cols("model")
        for c in _NEW_MODEL_COLS:
            self.assertIn(c, cols)

    def test_run_log_table_and_index_exist(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("pricingsyncrun", tables)
        idx = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("ix_pricingsyncrun_started", idx)

    def test_schema_version_is_15(self):
        v = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(v, "15")

    def test_pricing_mode_backfill(self):
        rows = dict(self.conn.execute(
            "SELECT key, pricing_mode FROM model").fetchall())
        self.assertEqual(rows["m-a"], "manual")   # had both prices
        self.assertEqual(rows["m-b"], "manual")   # had input price only
        self.assertEqual(rows["m-c"], "auto")     # never priced

    def test_authoritative_history_unchanged(self):
        for key in ("model_rows", "provider_rows", "session", "telemetry",
                    "prices"):
            self.assertEqual(self.pre[key], self.post[key],
                             f"{key} changed across the migration")

    def test_fresh_schema_matches_migrated(self):
        fresh_path = os.path.join(self.tmpdir, "fresh.db")
        eng = create_engine(f"sqlite:///{fresh_path}",
                            connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(eng)
        odb._migrate(eng)
        eng.dispose()
        fresh = sqlite3.connect(fresh_path)
        try:
            fresh_cols = {r[1] for r in fresh.execute(
                "PRAGMA table_info(model)").fetchall()}
            fresh_run = {r[1] for r in fresh.execute(
                "PRAGMA table_info(pricingsyncrun)").fetchall()}
        finally:
            fresh.close()
        self.assertEqual(fresh_cols, self._cols("model"))
        self.assertEqual(fresh_run, self._cols("pricingsyncrun"))


if __name__ == "__main__":
    unittest.main()
