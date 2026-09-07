# Upgrading

This document covers safe upgrade procedures for LLM-Telemetry.

## Prerequisites

- Current installation is running
- SQLite database is backed up (see [Operations](operations.md))
- Git is available

## Upgrade procedure

### Step 1 — Back up the database

```bash
# Stop the service
sudo systemctl stop llm-telemetry

# Create a backup copy
cp /var/lib/llm-telemetry/observatory.db /var/lib/llm-telemetry/observatory.db.bak.$(date +%Y%m%d%H%M)

# Restart the service
sudo systemctl start llm-telemetry
```

### Step 2 — Pull new code

```bash
cd /opt/llm-telemetry/current
git pull origin main
```

### Step 3 — Install updated dependencies

```bash
.venv/bin/pip install -r requirements.txt
```

### Step 4 — Run tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

### Step 5 — Restart the service

```bash
sudo systemctl restart llm-telemetry
```

The database migrations run automatically on startup.

## Schema migrations

Schema changes are automatic and forward-only. The current schema version is **v14**.

### Migration history

| From → To | Change | Data impact |
|-----------|--------|-------------|
| v1 → v2 | Clean empty build info rows | Deletes empty rows |
| v1 → v3 | Add `prompt_seconds_total`, `gen_seconds_total` | Additive, no data loss |
| v1 → v4 | Add session metadata columns | Additive, no data loss |
| v1 → v5 | Add live TPS fields | Additive, no data loss |
| v1 → v6 | Add `live_context_max` | Additive, no data loss |
| v1 → v7 | Add indexes | Performance only |
| v1 → v8 | Add session indexes | Performance only |
| v1 → v9 | Add model+ts index | Performance only |
| v1 → v10 | Add session status index | Performance only |
| v1 → v11 | Add session provider+end index | Performance only |
| v1 → v12 | Add usage buckets + residency tables | Additive tables, backfill from retained data |
| v1 → v13 | Add catalog/pricing columns | Additive, no data loss |
| v1 → v14 | Add covering index on usage buckets | Performance only |
| v1 → v15 | Add cache-price columns, pricing mode/state, run-log table | Additive, no data loss |

### Migration safety

- All migrations are additive (new columns, new tables, new indexes)
- No columns are dropped or renamed
- No data types are changed
- v12 includes an idempotent backfill from retained telemetry samples
- v13 includes preflight duplicate checks with `MigrationAbortedError` on conflict

### If a migration fails

1. Check the service logs: `journalctl -u llm-telemetry -n 50`
2. If `MigrationAbortedError` is raised (v13 preflight), check for duplicate `(provider_id, key)` on models or `(model_id, fingerprint)` on modelconfigs
3. Fix duplicates and restart the service
4. If the database is corrupted, restore from backup and retry

## A safe manual upgrade

1. Back up the database (see [Operations](operations.md#database-backups)).
2. Pull the new code into a fresh directory, build a venv, install deps.
3. Run the test suite against it: `python -m unittest discover -s tests`.
4. Point your service at the new directory and restart it.
5. `curl http://127.0.0.1:8090/api/meta` — confirm it responds and the
   schema version is what you expect.
6. Keep the previous directory until the new one is confirmed good, so a
   rollback is just a symlink swap plus a database restore.

## Demo mode

When upgrading demo mode installations:

```bash
# Demo mode uses a separate database — no migration concerns
# Just reinstall dependencies and restart
.venv/bin/pip install -r requirements.txt
python app.py --demo
```

## Rolling upgrades (multi-process)

Because LLM-Telemetry uses a renewable SQLite lease, multiple dashboard processes can run concurrently:

1. Start the new process version alongside the old one
2. The new process will acquire the collector lease (or wait for the old one to release)
3. Old processes continue serving read-only queries
4. Stop old processes once the new one is confirmed working

## Rollback

If the new version has issues:

### Manual rollback

```bash
# Stop service
sudo systemctl stop llm-telemetry

# Restore database from backup
cp /var/lib/llm-telemetry/observatory.db.bak /var/lib/llm-telemetry/observatory.db

# Restart
sudo systemctl start llm-telemetry
```

### Automated rollback (GitHub Actions)

The deploy script automatically rolls back if the health check fails after deploying a new release. The previous release is restored and the service is restarted.
