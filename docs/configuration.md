# Configuration

LLM-Telemetry uses a single source of configuration: `observatory/settings.py`. There are no `.env` files, CLI config flags (beyond `--host`, `--port`, `--db`, `--demo`), or YAML files. All user-facing configuration is managed through the **Settings** page UI.

## Runtime flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address for the web server |
| `--port` | `8090` | Listening port |
| `--db` | `data/observatory.db` (real) / `data/observatory_demo.db` (demo) | SQLite database path |
| `--demo` | off | Enable demo mode with synthetic data |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_TELEMETRY_SCREENSHOT_DIR` | `<db-dir>/screenshots/` | Override directory for screenshot captures |

## Provider configuration (Settings → Providers)

Providers are the core configuration unit. Each provider represents one inference server.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Name | Yes | — | Display label for this provider |
| Type | Yes | `llama.cpp` | Server type (current: `llama.cpp` only) |
| Base URL | Yes | — | HTTP address of the server (e.g., `http://127.0.0.1:8083`) |
| Agent URL | No | — | Host agent address for hardware telemetry (e.g., `http://127.0.0.1:8091`) |
| Enabled | Yes | `true` | Whether to actively poll this provider |
| Is default | No | `false` | Primary provider for default queries |
| Poll interval | Yes | `1.0` | Seconds between polls (minimum 0.25) |
| Notes | No | — | Free-text notes |

> [!NOTE]
> A default provider at `http://127.0.0.1:8083` (agent `http://127.0.0.1:8091`) is created automatically on first start in every mode. Replace or update it with your actual server.

## Display settings (Settings → Display)

Stored in the SQLite `setting` table under key `display`.

| Setting | Default | Description |
|---------|---------|-------------|
| `default_range` | `7d` | Default time range for Models/Sessions pages |
| `default_group` | `family` | Default grouping mode on Models page |
| `theme` | `dark` | UI theme (`dark` or `light`) |

## Collection cadence (internal)

These values are defined in `observatory/settings.py` and control the collector loop:

| Constant | Value | Description |
|----------|-------|-------------|
| `LLAMA_POLL_S` | `1.0` | Live metrics interval (health + /metrics) |
| `PROPS_POLL_S` | `30.0` | /props refresh (build info) |
| `MODELS_POLL_S` | `15.0` | /v1/models refresh (model load/unload detection) |
| `AGENT_POLL_S` | `10.0` | Host agent interval |
| `STALE_AFTER_S` | `20.0` | Provider marked STALE after no-success for this many seconds |
| `OFFLINE_AFTER_FAILS` | `3` | Consecutive failed polls before OFFLINE status |
| `SESSION_END_DELAY_S` | `8.0` | No token activity for this many seconds → session ends |
| `SAMPLE_DT_MAX_S` | `30.0` | Gaps larger than this close a session first |

## Storage and retention (internal)

| Constant | Value | Description |
|----------|-------|-------------|
| `DB_PATH_DEFAULT` | `data/observatory.db` | SQLite path in real mode |
| `DB_PATH_DEMO` | `data/observatory_demo.db` | SQLite path in demo mode |
| `RETENTION_RAW_S` | `2 * 3600` (2 h) | Fine-grained raw samples kept for |
| `RETENTION_MID_S` | `7 * 86400` (7 d) | 10-second buckets kept for |
| `RETENTION_FULL_S` | `30 * 86400` (30 d) | 60-second buckets kept for |
| `RETENTION_SWEEP_S` | `300` (5 min) | Retention sweep runs every |
| `BUCKET_MID_S` | `10.0` | Medium-resolution bucket size |
| `BUCKET_FULL_S` | `60.0` | Old-resolution bucket size |
| `SCREENSHOT_TTL_S` | `86400` (24 h) | Screenshot files auto-delete after |

Retention sweep runs every 5 minutes in the background. Raw samples, 10-second buckets, and 60-second buckets are each pruned to their respective retention windows. **Models, configs, builds, hardware info, and sessions are never pruned.**

## Model pricing (Settings → Providers → select a provider)

Per-model pricing is stored in the `model` table's `input_price_per_million` and `output_price_per_million` columns (as Decimal strings). Prices are validated against:

- Must be a non-negative number
- Maximum 8 decimal places
- Maximum value: 999999999.99999999

Pricing is displayed in the Settings page when viewing a provider's models.

## Range keys

Used on Models, Sessions, and Compare pages:

| Key | Meaning |
|-----|---------|
| `today` | Today only |
| `2d` | Last 2 days |
| `3d` | Last 3 days |
| `5d` | Last 5 days |
| `7d` | Last 7 days |
| `30d` | Last 30 days |
| `all` | All available data |

## Chart bucket sizes

Used for detail graphs on Model Detail and Session Detail pages:

| Range | Bucket size |
|-------|-------------|
| `1m` | 2 seconds |
| `5m` | 10 seconds |
| `15m` | 30 seconds |
| `1h` | 60 seconds |
| `session` | 2 seconds |
| `24h` | 300 seconds |
| `7d` | 3600 seconds |
| `30d` | 14400 seconds |

## Metric families (Settings → System Status)

The System Status section on the Settings page reports telemetry availability per group:

| Group | Metrics |
|-------|---------|
| Counters | `tokens_total`, `prompt_total`, `gen_total` |
| Speeds | `gen_tps`, `prompt_tps` |
| Context | `context_used`, `context_max` |
| MTP | `mtp_proposed_total`, `mtp_accepted_total`, `mtp_avg_acc` |
| GPU | `gpu_util`, `vram_used_mb` |
