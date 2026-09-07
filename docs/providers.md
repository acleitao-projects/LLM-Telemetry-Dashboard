# Providers

LLM-Telemetry monitors one or more inference providers. Each provider represents an inference server whose telemetry the dashboard collects.

## Supported server types

| Type | Description |
|------|-------------|
| `llama.cpp` | llama.cpp `llama-server` or router (including a multi-model router) |

## llama.cpp integration

LLM-Telemetry is designed for llama.cpp-compatible servers that expose the standard read-only endpoints.

### Required endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Server health status |
| `/metrics` | GET | Prometheus-style counters and gauges |
| `/props` | GET | Build info, model info, generation settings |
| `/v1/models` | GET | List of loaded models |
| `/slots` | GET | Current slot states |

### Optional endpoint

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/models/unload` | POST | Unload all currently loaded models |

The collector polls `/health` and `/metrics` every `LLAMA_POLL_S` seconds (default: 1.0 s). `/props` is polled every `PROPS_POLL_S` seconds (default: 30 s). `/v1/models` is polled every `MODELS_POLL_S` seconds (default: 15 s).

### Metric mapping

LLM-Telemetry maps standard llama.cpp Prometheus metrics through an alias system in `observatory/settings.py`. It supports both the classic `llama_server_*` naming and the router-style `llamacpp:*` naming:

| Internal name | Classic (`llama_server_*`) | Router (`llamacpp:*`) |
|---------------|---------------------------|------------------------|
| `tokens_total` | `llama_server_tokens_total` | `llamacpp:tokens_total` |
| `prompt_total` | `llama_server_prompt_tokens_total` | `llamacpp:prompt_tokens_total` |
| `gen_total` | `llama_server_generation_tokens_total` | `llamacpp:tokens_predicted_total` |
| `prompt_tps` | `llama_server_prompt_processing_tps` | `llamacpp:prompt_tokens_seconds` |
| `gen_tps` | `llama_server_token_generation_tps` | `llamacpp:predicted_tokens_seconds` |
| `context_used` | `llama_server_context_used` | `llamacpp:n_tokens_max` |
| `context_max` | `llama_server_context_length` | — |
| `mtp_proposed_total` | `llama_server_mtp_proposed_total` | `llamacpp:spec_decode_num_draft_tokens_total` |
| `mtp_accepted_total` | `llama_server_mtp_accepted_total` | `llamacpp:spec_decode_num_accepted_tokens_total` |

### Counter reset handling

When a llama.cpp server restarts, its cumulative counters reset to zero. LLM-Telemetry detects this by observing a negative delta between consecutive samples. A counter reset:

1. Closes the active session for that model
2. Re-anchors baselines from the new zero point
3. Never produces negative deltas

### Model detection

Models are identified by their `id` or `name` field from the `/v1/models` response. Model names are parsed to extract family and quantization level using the algorithm in `observatory/metrics.py`. Known quantization tokens (IQ2, IQ3, IQ4, IQ5, IQ6, IQ7, Q2–Q8, F16, BF16, F32, MXP4, MXP8) are recognized.

## Host agent

The host agent (`host_agent.py` or `host_agent.py`) provides GPU and host-level telemetry that the llama.cpp metrics endpoint does not include.

### Agent endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/info` | GET | Host facts (OS, CPU, RAM, hostname) |
| `/gpu` | GET | GPU details (VRAM, utilization, temperature, power) |
| `/llama` | GET | llama.cpp process info (version, commit, Docker image, container ID) |
| `/health` | GET | Agent health check |

### Requirements

- **Linux only** — reads from `/proc/meminfo`, `/proc/cpuinfo`, `/proc/stat`
- **`nvidia-smi`** — required for GPU data (NVIDIA GPUs)
- **`/proc/<pid>/cmdline`** scanning — detects llama.cpp process, extracts version/commit from argv

### Configuration

Set the provider's **Agent URL** to the host agent's address:

```
http://<host>:8091
```

The agent runs on port 8091 by default:

```bash
python3 host_agent.py --port 8091 --host 0.0.0.0
```

> [!NOTE]
> `host_agent.py` and `host_agent.py` are functionally identical. The only differences are the default hostname ("local-router" vs "inference-host") and the health response message. Use whichever filename fits your setup.

## Adding a provider

1. Navigate to **Settings → Providers**
2. Click **Add Provider**
3. Enter:
   - **Name**: A descriptive label (e.g., "Main GPU server")
   - **Type**: `llama.cpp`
   - **Base URL**: The server's HTTP address
   - **Agent URL** (optional): Host agent address for hardware telemetry
   - **Poll interval**: Seconds between polls (minimum 0.25 s)
4. Click **Save**
5. Click **Test** to run a passive connection test

### Connection test

The provider test checks:

1. `/health` — returns 200 OK
2. `/v1/models` — returns a list of models
3. `/metrics` — returns parseable metrics
4. `/slots` — returns a list
5. `/props` — returns build info

All checks are read-only. No prompts are sent.

### Provider statuses

| Status | Meaning |
|--------|---------|
| `LIVE` | Successfully polled within `STALE_AFTER_S` seconds |
| `STALE` | No successful poll in `STALE_AFTER_S` seconds (default: 20 s) |
| `OFFLINE` | `OFFLINE_AFTER_FAILS` consecutive failed polls (default: 3) |
| `not collecting` | Appended when this process is not the lease holder and polls nothing |

Status is aged against the provider's last successful poll every time it is
read, not just when the collector writes it. The stored value is only ever
updated by the collector, so a collector that stops entirely — killed, crashed,
or demoted to standby — would otherwise leave the last value on screen forever
and a long-dead provider would keep reporting `LIVE`. The grace window follows
the provider's own `poll_interval_s` (with `STALE_AFTER_S` as the floor) so a
deliberately slow provider is not reported stale between two healthy polls.

`not collecting` is about the dashboard process rather than the provider: only
the holder of the single-writer lease polls, and any other process sharing the
database serves the same pages read-only while issuing no provider requests.
See [Architecture](architecture.md) for the lease itself.

## Common connectivity issues

| Problem | Check |
|---------|-------|
| Provider shows OFFLINE after adding | Verify Base URL is reachable from the dashboard host (`curl http://<base-url>/health`) |
| Metrics show but models don't | Ensure `/v1/models` returns a JSON array with `id` or `name` fields |
| No GPU data | Verify Agent URL is correct and the agent is running; check that `nvidia-smi` is in PATH |
| MTP metrics missing | Verify the server was started with `--mtp` or equivalent flag |
| Counter resets causing session gaps | Normal behavior on llama.cpp restart; check that the server is stable |

## Multiple providers

LLM-Telemetry supports multiple concurrent providers. The collector loop iterates over all enabled providers and polls each at its configured interval. On the dashboard:

- **Providers page** (Settings): manage all providers in one place
- **Models/Sessions pages**: filter by specific provider using the provider dropdown
- **Compare page**: compare models across providers
- **Hardware page**: shows hardware for the selected provider
