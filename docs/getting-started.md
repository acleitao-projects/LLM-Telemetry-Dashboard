# Getting Started

This guide takes you from a fresh clone to a working dashboard with visible telemetry — in under five minutes using demo mode.

## Prerequisites

- **Python 3.12 or higher** — check your version with:
  ```bash
  python3 --version
  # Expected: Python 3.12.x
  ```
- A browser (Chrome, Firefox, Edge, Safari)
- An inference server (optional for demo mode)

## Step 1 — Clone and install

```bash
git clone https://github.com/acleitao-projects/LLM-Telemetry-Dashboard.git
cd LLM-Telemetry-Dashboard
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

This installs: FastAPI, uvicorn, sqlmodel, httpx, jinja2.

## Step 2 — Run in demo mode

Demo mode seeds 30 days of synthetic history and produces live synthetic activity through the real collector path. It never contacts the network.

```bash
python app.py --demo
```

Expected output:

```
observatory listening on http://127.0.0.1:8090 (demo)
```

Open **http://127.0.0.1:8090** in your browser.

> [!TIP]
> The demo creates its own database at `data/observatory_demo.db`, separate from real mode's `data/observatory.db`. You can switch between modes without data interference.

## Step 3 — Explore the dashboard

The landing page redirects to **Overview**. Navigate using the sidebar:

- **Overview** — current slot state, today's metrics, 24-hour activity by model, 7-day leaderboards
- **Models** — group-by-family ranking table with activity states and sparklines
- **Sessions** — externally driven inference sessions with filters
- **Compare** — side-by-side model comparison across time ranges
- **Hardware** — CPU/RAM/GPU facts and per-GPU utilization charts
- **Settings** — providers, display defaults, model pricing

## Step 4 — Connect a real provider (optional)

When running in **real mode** (`python app.py` without `--demo`), LLM-Telemetry expects one or more llama.cpp-compatible inference servers.

### Minimal setup

1. Start a llama.cpp server:

   ```bash
   llama-server -m /path/to/model.gguf -c 4096 --port 8083
   ```

2. Start LLM-Telemetry in real mode:

   ```bash
   python app.py
   ```

3. Go to **Settings → Providers** and verify the default provider points at your server's base URL and port.

4. Click **Test** on the provider row to run a passive connection test (checks `/health`, `/metrics`, `/props`, `/v1/models`).

5. Wait ~30 seconds for the collector to begin polling. Telemetry data will appear on the dashboard.

### Adding additional providers

In **Settings → Providers**, click **Add Provider** and enter:

| Field | Example |
|-------|---------|
| **Name** | `My GPU Server` |
| **Type** | `llama.cpp` |
| **Base URL** | `http://127.0.0.1:8083` |
| **Agent URL** (optional) | `http://127.0.0.1:8091` |
| **Poll interval** | `1.0` |

## Step 5 — Verify telemetry is arriving

On the **Settings** page, the **System Status** section shows telemetry availability per metric group:

- **Counters** — `tokens_total`, `prompt_total`, `gen_total`
- **Speeds** — `gen_tps`, `prompt_tps`
- **Context** — `context_used`, `context_max`
- **MTP** — `mtp_proposed_total`, `mtp_accepted_total`, `mtp_avg_acc`
- **GPU** — `gpu_util`, `vram_used_mb`

Each group displays a status indicator (green = live data, gray = no data).

## Troubleshooting

**`ModuleNotFoundError: No module named 'fastapi'`**
Your virtual environment is not activated. Run `. .venv/bin/activate` and verify with `which python` (should show `.venv/bin/python`).

**Port 8090 already in use**
Use a different port: `python app.py --port 9090`.

**Provider shows OFFLINE after adding**
Verify your server is reachable: `curl http://<your-server>/health` should return `{"status": "ok"}`.

**Demo mode shows no data after 30 seconds**
Check the collector log: `journalctl -u llm-telemetry -n 50` or look at the terminal output for errors.

## Next steps

- Read the [Installation guide](installation.md) for production deployment
- See [Providers](providers.md) for detailed llama.cpp integration
- Browse the [Dashboard Guide](dashboard-guide.md) for page-by-page explanations
