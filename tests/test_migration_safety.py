"""Verify G02 migration does not lose production data.

Builds a production-shaped database using the actual ORM, sets version to 11,
then runs the migration and verifies no data loss.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy import text

from observatory import database as odb
from observatory.models import (Model, ModelResidency, ModelUsageBucket,
                                Provider, TelemetrySample, now_ms)

MINUTE_MS = 60_000
DAY_MS = 86_400_000


def build_v11_db(path: str) -> dict:
    """Build a production-shaped DB at schema_version=11 using the ORM."""
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    import random
    rng = random.Random(42)
    now = now_ms()
    start = now - 2 * DAY_MS

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
            for j in range(4):
                m = Model(provider_id=p.id, key=f"m{p.id}-{j}", name=f"model-{p.id}-{j}",
                          family=f"fam{j%2}", arch="llama", params="8B",
                          quant="Q4_K_M", first_seen_at=start)
                s.add(m)
                s.commit()
                s.refresh(m)
                models.append(m)

        # Telemetry: every 15 min for 2 days
        from sqlalchemy import insert as sa_insert
        telemetry_rows = []
        for m in models:
            tokens = prompt = gen = prompt_s = gen_s = prop = acc = 0.0
            ts = start
            while ts <= now:
                if rng.random() < 0.7:
                    dp = rng.randint(50, 400)
                    dg = rng.randint(20, 200)
                    prompt += dp
                    gen += dg
                    tokens = prompt + gen
                    prompt_s += max(0.1, dp / 250.0)
                    gen_s += max(0.1, dg / 40.0)
                    prop += rng.randint(0, 40)
                    acc += rng.randint(0, 30)
                    telemetry_rows.append({
                        "provider_id": m.provider_id, "model_id": m.id, "ts": ts,
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
                ts += 900_000
        if telemetry_rows:
            s.execute(sa_insert(TelemetrySample).values(telemetry_rows))
            s.commit()

        # Sessions
        sess_rows = []
        for _ in range(50):
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
                "peak_prompt_tps": 350.0, "ttft_s": rng.uniform(0.2, 2.0),
                "context_max": rng.randint(4096, 32768), "mtp_enabled": True,
                "mtp_proposed": rng.randint(0, 500), "mtp_accepted": rng.randint(0, 400),
                "mtp_acc": rng.uniform(60, 90), "status": "CLOSED", "result_source": "metrics",
            })
        from observatory.models import SessionRow
        if sess_rows:
            s.execute(sa_insert(SessionRow).values(sess_rows))
            s.commit()

        # Create meta table and force version to 11 (simulating pre-G02 DB)
        s.exec(text("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"))
        s.exec(text("DELETE FROM meta WHERE key = 'schema_version'"))
        s.exec(text("INSERT INTO meta (key, value) VALUES ('schema_version', '11')"))
        s.commit()

        counts = {
            "telemetrysample": len(s.exec(text("SELECT 1 FROM telemetrysample")).all()),
            "session": len(s.exec(text("SELECT 1 FROM session")).all()),
            "model": len(s.exec(text("SELECT 1 FROM model")).all()),
            "provider": len(s.exec(text("SELECT 1 FROM provider")).all()),
        }
        checksums = {
            "telemetry": s.exec(text(
                "SELECT SUM(tokens_total), SUM(prompt_total), SUM(gen_total), COUNT(*) "
                "FROM telemetrysample")).one(),
            "session": s.exec(text(
                "SELECT SUM(total_tokens), SUM(prompt_tokens), SUM(gen_tokens), COUNT(*) "
                "FROM session")).one(),
        }
        # Drop the new G02 tables to simulate a true v11 DB
        s.exec(text("DROP TABLE IF EXISTS modelusagebucket"))
        s.exec(text("DROP TABLE IF EXISTS modelresidency"))
        s.commit()

    engine.dispose()
    return {"counts": counts, "checksums": checksums, "now": now}


def get_post_state(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    counts = {}
    for table in ["telemetrysample", "session", "model", "provider"]:
        counts[table] = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    checksums = {
        "telemetry": c.execute(
            "SELECT SUM(tokens_total), SUM(prompt_total), SUM(gen_total), COUNT(*) "
            "FROM telemetrysample").fetchone(),
        "session": c.execute(
            "SELECT SUM(total_tokens), SUM(prompt_tokens), SUM(gen_tokens), COUNT(*) "
            "FROM session").fetchone(),
    }
    new_tables = {}
    for table in ["modelusagebucket", "modelresidency"]:
        try:
            new_tables[table] = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            new_tables[table] = -1
    version = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    return {"counts": counts, "checksums": checksums, "new_tables": new_tables, "version": version}


def run_migration(db_path: str):
    """Run the actual migration code (create_all + _migrate)."""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 15})
    SQLModel.metadata.create_all(engine)
    odb._migrate(engine)
    engine.dispose()


class MigrationSafetyTests(unittest.TestCase):
    """Verify the G02 migration (v11 → v12) does not lose production data."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="g02_mig_safety_")
        cls.db_path = os.path.join(cls.tmpdir, "prod.db")
        cls.pre = build_v11_db(cls.db_path)
        run_migration(cls.db_path)
        cls.post = get_post_state(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_no_existing_data_lost(self):
        for table in self.pre["counts"]:
            self.assertEqual(
                self.pre["counts"][table], self.post["counts"][table],
                f"Row count changed for {table}")
        for key in self.pre["checksums"]:
            self.assertEqual(
                self.pre["checksums"][key], self.post["checksums"][key],
                f"Checksum changed for {key}")

    def test_version_bumped(self):
        # A v11 DB is migrated forward through the full chain to the current
        # schema version (G03 raised it to 13, P02's covering index to 14).
        self.assertGreaterEqual(int(self.post["version"]), 13)

    def test_buckets_and_residency_created(self):
        self.assertGreater(self.post["new_tables"]["modelusagebucket"], 0)
        self.assertGreater(self.post["new_tables"]["modelresidency"], 0)

    def test_idempotency(self):
        before = get_post_state(self.db_path)
        run_migration(self.db_path)
        after = get_post_state(self.db_path)
        for table in self.pre["counts"]:
            self.assertEqual(before["counts"][table], after["counts"][table])
        for key in self.pre["checksums"]:
            self.assertEqual(before["checksums"][key], after["checksums"][key])
        self.assertEqual(before["new_tables"]["modelusagebucket"],
                         after["new_tables"]["modelusagebucket"])
        self.assertEqual(before["new_tables"]["modelresidency"],
                         after["new_tables"]["modelresidency"])

    def test_collector_rows_survive_rebackfill(self):
        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        with Session(engine) as s:
            s.add(ModelUsageBucket(
                provider_id=1, model_id=1, bucket_start=now_ms() - DAY_MS,
                input_tokens=999.0, output_tokens=888.0, provenance="collector"))
            s.add(ModelResidency(
                provider_id=1, model_id=1, loaded_at=now_ms() - 2 * DAY_MS,
                unloaded_at=now_ms() - DAY_MS, source="collector"))
            s.commit()
        engine.dispose()

        run_migration(self.db_path)

        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        with Session(engine) as s:
            cb = s.exec(select(ModelUsageBucket).where(
                ModelUsageBucket.provenance == "collector")).first()
            self.assertIsNotNone(cb)
            self.assertAlmostEqual(cb.input_tokens, 999.0)
            cr = s.exec(select(ModelResidency).where(
                ModelResidency.source == "collector")).first()
            self.assertIsNotNone(cr)
        engine.dispose()

    def test_partial_unique_index_enforced(self):
        from sqlalchemy.exc import IntegrityError
        engine = create_engine(f"sqlite:///{self.db_path}",
                               connect_args={"check_same_thread": False})
        with Session(engine) as s:
            base = now_ms()
            s.add(ModelResidency(provider_id=2, model_id=2, loaded_at=base,
                                 source="test_idx"))
            s.commit()
            with self.assertRaises(IntegrityError):
                s.add(ModelResidency(provider_id=2, model_id=2,
                                     loaded_at=base + 1000, source="test_idx"))
                s.commit()
            s.rollback()
            s.exec(text("DELETE FROM modelresidency WHERE source = 'test_idx'"))
            s.commit()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()


class RequiredIndexTests(unittest.TestCase):
    """Every index the query plans depend on must exist on any database.

    These were declared only inside ``if version < N:`` migration blocks, which
    meant two different databases never received them: a freshly created one
    stamps meta.schema_version at SCHEMA_VERSION and runs no block at all, and
    an existing one that had already passed version N skipped the statement
    permanently.  Both cases were real -- five indexes were absent from a fresh
    database and from the development copy alike.
    """

    REQUIRED = [name for name, _ in odb._REQUIRED_INDEXES]

    @staticmethod
    def _indexes(path):
        con = sqlite3.connect(path)
        try:
            return {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
        finally:
            con.close()

    def _init(self, path):
        odb._engine = None
        try:
            odb.init_db(path)
        finally:
            odb._engine = None

    def test_a_fresh_database_has_every_required_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fresh.db")
            self._init(path)
            present = self._indexes(path)
            missing = [name for name in self.REQUIRED if name not in present]
            self.assertEqual([], missing,
                             "fresh database is missing: %s" % ", ".join(missing))

    def test_an_existing_database_that_skipped_them_is_repaired(self):
        """A database already stamped current, but without the indexes."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "existing.db")
            self._init(path)
            con = sqlite3.connect(path)
            for name in self.REQUIRED:
                con.execute("DROP INDEX IF EXISTS %s" % name)
            con.commit()
            con.close()
            self.assertTrue(
                set(self.REQUIRED).isdisjoint(self._indexes(path)),
                "precondition: the indexes should be gone")

            self._init(path)

            present = self._indexes(path)
            missing = [name for name in self.REQUIRED if name not in present]
            self.assertEqual([], missing,
                             "repair left these missing: %s" % ", ".join(missing))

    def test_repair_is_a_no_op_when_nothing_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "healthy.db")
            self._init(path)
            odb._engine = None
            engine = odb.init_db(path)
            try:
                with Session(engine) as session:
                    self.assertEqual([], odb._ensure_indexes(session))
            finally:
                odb._engine = None
