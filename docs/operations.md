# Operations

This document covers operational procedures for running LLM-Telemetry in production.

## Startup and shutdown

### Manual start

```bash
# Real mode
python app.py

# Demo mode
python app.py --demo

# Custom port and database
python app.py --host 0.0.0.0 --port 9090 --db /path/to/data.db
```

### Systemd service

```bash
sudo systemctl start llm-telemetry
sudo systemctl stop llm-telemetry
sudo systemctl restart llm-telemetry
sudo systemctl status llm-telemetry
```

See [Run as a service](installation.md#run-as-a-service-systemd).

### What a clean stop looks like

A stop should complete in well under a second. On SIGTERM the app ends its
open SSE streams, the collector loop stops, and the single-writer lease is
**released** so a replacement process can start polling immediately rather than
waiting out the stale-lease window.

If the journal shows this instead:

```
llm-telemetry.service: State 'stop-sigterm' timed out. Killing.
llm-telemetry.service: Main process exited, code=killed, status=9/KILL
```

then something held an in-flight request open past `TimeoutStopSec`, the process
was SIGKILLed, and the lease was left behind — which shows up as a gap of
several seconds in provider polling after the restart. `uvicorn` is configured
with `timeout_graceful_shutdown` below `TimeoutStopSec` so this should not
happen; a recurrence means a new long-lived endpoint needs the same
shutdown-aware treatment as `/api/stream`.

## Database backups

Take a database backup before every update. SQLite's online backup API is
consistent against a live writer in WAL mode:

```bash
sqlite3 /var/lib/llm-telemetry/observatory.db \
  ".backup '/var/lib/llm-telemetry/backups/observatory-$(date -u +%Y%m%d-%H%M%S).db'"
```

- Path: `/var/lib/llm-telemetry/backups/observatory-YYYYMMDD-HHMMSS.db`
- Verify it: the file must be non-empty and `PRAGMA integrity_check` must
  return `ok`.
- Prune old copies on whatever schedule suits you (`find ... -mtime +N -delete`).

### Restoring a backup

```bash
sudo systemctl stop llm-telemetry
sudo cp /var/lib/llm-telemetry/backups/observatory-<stamp>.db \
        /var/lib/llm-telemetry/observatory.db
sudo chown llm-telemetry:llm-telemetry /var/lib/llm-telemetry/observatory.db
sudo systemctl start llm-telemetry
```

Restoring data is **not** part of any automatic rollback you wire up. Restore data deliberately and by hand.

## Database

### Location

| Mode | Default path |
|------|-------------|
| Real | `data/observatory.db` |
| Demo | `data/observatory_demo.db` |

Override with `--db /path/to/file.db`.

### Database file

The SQLite database file grows as telemetry accumulates. The WAL mode files (`*-wal`, `*-shm`) are transient and can be removed while the app is running without data loss.

### Manual backup

Every deploy already takes one automatically — see
[Database backups](#database-backups). To take one by hand at any time, with the
service running:

```bash
sudo sqlite3 /var/lib/llm-telemetry/observatory.db \
  ".backup '/backup/observatory-$(date -u +%Y%m%d-%H%M%S).db'"
sudo sqlite3 /backup/observatory-<stamp>.db 'PRAGMA integrity_check;'   # expect: ok
```

> [!WARNING]
> Do not back up a running database with `cp`. The database runs in WAL mode, so
> recently committed data lives in the `-wal` sidecar file: copying only the
> `.db` silently loses it, and copying while a write is in flight can produce a
> torn file. `.backup` uses SQLite's online backup API and is consistent against
> a live writer. If you must copy files directly, stop the service first and copy
> the `.db`, `-wal` and `-shm` files together.

### Restore

See [Restoring a backup](#restoring-a-backup).

### Database size

The database typically contains:

- **Small** installations (< 1 model): a few MB
- **Medium** installations (2-5 models, 30 days): ~20-50 MB
- **Large** installations (multiple providers, 30+ days): ~100+ MB

SQLite page cache is set to -32768 pages (negative = KiB, so ~32 MiB) for production databases that fit in memory.

## Logging

The service logs to the journal at INFO. Per-request HTTP client logging
(`httpx`, `httpcore`) is deliberately raised to WARNING at startup: the
collector polls every provider once a second and issues several requests per
poll, so at INFO those lines were 99.5% of all output — measured at 10,224 of
10,272 lines in one hour, roughly 245k lines a day.

```bash
journalctl -u llm-telemetry --since -1h | wc -l     # expect tens, not thousands
journalctl -u llm-telemetry --since -7d | grep 'slow API'
```

A healthy instance produces very little log output, so **sudden growth is itself
a signal**. Provider health is not lost by this: it is surfaced on the dashboard
and in `Provider.last_error`, and request failures still reach the log as
collector warnings.

If the journal has already grown large from before this change:

```bash
journalctl --disk-usage
sudo journalctl --vacuum-time=7d
```

## Health check

```bash
curl -s http://127.0.0.1:8090/api/meta
```

Expected response:

```json
{
  "demo": false,
  "providers": [...],
  "display": {"default_range": "7d", "default_group": "family", "theme": "dark"},
  "snapshots": {"size": 0, "hits": 0, "misses": 0, ...}
}
```

### Provider-level health

Check individual provider status:

```bash
curl -s http://127.0.0.1:8090/api/status
```

This returns per-provider status including endpoint availability and latency.

## Logging

The collector writes log messages at INFO level:

```
2026-09-06 12:00:00,000 INFO observatory.collector: startup reconciliation: 3 stale sessions terminalized
2026-09-06 12:00:01,000 INFO httpx: HTTP Request: GET http://127.0.0.1:8083/health "HTTP/1.1 200 OK"
2026-09-06 12:00:01,500 WARNING observatory: slow API models: 450.2 ms (12 rows)
```

Log format: `%(asctime)s %(levelname)s %(name)s: %(message)s`

To capture logs:

```bash
# Systemd journal
journalctl -u llm-telemetry -f

# Or redirect app output
python app.py 2>&1 | tee app.log
```

## Service hardening

A hardened systemd unit (see [Installation](installation.md#run-as-a-service-systemd)) should set:

| Setting | Value | Purpose |
|---------|-------|---------|
| `User` | `llm-telemetry` | Non-root user |
| `ProtectSystem` | `strict` | Read-only /usr, /boot, /etc |
| `ReadWritePaths` | `/var/lib/llm-telemetry` | Only DB directory is writable |
| `ProtectHome` | `true` | Home directories not accessible |
| `NoNewPrivileges` | `true` | Cannot gain additional privileges |
| `PrivateTmp` | `true` | Isolated /tmp |
| `Restart` | `on-failure` | Auto-restart on crash |
| `RestartSec` | `3` | 3-second restart delay |
| `TimeoutStopSec` | `20` | Graceful shutdown timeout |

## Retention management

Retention runs automatically every 5 minutes. Manual intervention is rarely needed:

| Metric | Value |
|--------|-------|
| Sweep interval | 5 minutes |
| Raw sample retention | 2 hours |
| 10-second bucket retention | 7 days |
| 60-second bucket retention | 30 days |
| Models/configs/hardware/sessions | Never pruned |

To manually trigger a retention sweep, the collector's `_retention_sweep()` method can be called via the API or by restarting the service.

## Monitor disk usage

```bash
# Database size
ls -la /var/lib/llm-telemetry/

# Screenshot directory
ls -la /var/lib/llm-telemetry/screenshots/

```
