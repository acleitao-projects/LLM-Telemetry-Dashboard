# LLM-Telemetry

> This was built using Qwen 3.8 27B and Codex. The code was not reviewed and was designed to run on a closed network.
>
> Use at your own risk. If something breaks, explodes, leaks memory, summons a daemon, or generally behaves in ways that disappoint you, I’ll be expecting your PR.
>
> As a very wise developer once said:
>
> “It runs on my machine.”

> [!WARNING]
> LLM-Telemetry has no authentication. Do not expose it directly to the public internet; keep it on a trusted closed network or place it behind an authenticated reverse proxy.

Passive observability dashboard for llama.cpp servers. It watches one or more
llama.cpp endpoints (plus an optional host agent) **read-only** and turns the
stream into a dense, dark, technical dashboard: which models did the work,
how they were launched, how MTP behaved, and what the hardware was doing.

**Hard rule:** LLM-Telemetry never sends prompts, never triggers inference,
never loads/unloads models, never restarts anything. It only reads the
read-only endpoints `/health`, `/metrics`, `/props`, `/v1/models`, and `/slots` of a
llama.cpp server, and (optionally) `/info`, `/gpu`, `/llama` of the
passive host agent.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt    # Linux/macOS

.venv\Scripts\python app.py            # real mode (default provider: local llama.cpp)
.venv\Scripts\python app.py --demo     # demo mode: 30 days of synthetic data, no network
```

Open http://127.0.0.1:8090

By default, a provider named **Local llama.cpp** pointing at
`http://127.0.0.1:8080` (agent `http://127.0.0.1:8091`) is created on
first start, in every mode. If the server is unreachable the app still starts;
the provider shows as `STALE` (no data for 20 s) and then `OFFLINE`. Add more
providers under **Settings → Providers**.

### Demo mode

`python app.py --demo` uses a separate database (`data/observatory_demo.db`),
seeds ~30 days of synthetic history (6 models across 4 families, MTP on two of
them, two launch configs per model, hardware/build info) and then keeps
producing *live* synthetic activity through the exact same collector path the
real mode uses. It never touches the network. Use it to explore the UI or to
verify the whole pipeline end to end.

## Pages

- **Overview** — current state of the busiest slot (state badge, t/s, context,
  MTP), today's tokens/inference/utilization, 24 h token activity by model,
  7-day inference-time and token leaders, recent sessions.
- **Models** — the workhorse page. Group by **Family** (rolls quants of one
  model together), **Each file**, or **Quant**; range Today … All; sort by
  **Active / Inference / Loaded / Idle** time. Active is the default and supports
  multiple simultaneous slots. Six summary cards, a ranking table with activity
  state and sparklines, and a selected-model panel whose top section keeps the
  latest sanitized runtime snapshot (`n_gen`, observed `tg`, rolling `tg 3s`,
  and an orange context gauge) separate from authoritative historical totals.
- **Model detail** — live state, time accounting (prompt / generation / idle
  stacked over loaded time), token buckets, prompt vs generation speed,
  context, MTP acceptance, hardware; the full observed launch configuration
  with a **Full launch flags** dump and **config history** (every distinct
  launch config observed, kept forever).
- **Sessions** — externally driven inference sessions detected passively from
  llama.cpp slot/task identity, with explicitly provisional live progress and
  authoritative completed totals from `/metrics`. Filter by
  provider, model, quant, MTP on/off, reasoning effort, range.
- **Session detail** — TTFT, prompt/gen tokens and speeds, peaks, context
  peak, MTP acceptance, hardware averages, per-second series, the launch
  config that session ran with.
- **Compare** — line up to five observed model families side by side across a
  shared range. Files and quants are aggregated; nothing is executed.
- **Hardware** — host (CPU/RAM/GPU/driver/PCIe), authoritative llama.cpp build
  version/commit from `/props`, and 1 h per-GPU utilization, VRAM, temperature
  and power series alongside CPU/RAM. Live GPU cards use separate orange VRAM
  and utilization gauges for every device. Requires the host agent for host metrics.
- **Settings** — system status (telemetry availability per metric group),
  provider CRUD with a passive connection test (health/metrics/props/models —
  still no prompts), and display defaults.

## Semantics

- All timestamps are integer epoch milliseconds.
- Counters are cumulative server values; LLM-Telemetry stores them and derives
  deltas. A counter reset (restart) starts a new epoch: the session is closed,
  baselines re-anchor, and no negative deltas are ever produced.
- Runtime states: `UNLOADED / LOADING / IDLE / PROMPTING / GENERATING`.
  **Inference time = prompting + generation**; IDLE time is loaded-but-idle
  and is never counted as inference. **Model utilization = inference / loaded**.
- Sessions start when `/slots` reports a processing task, carry the launch config that was
  active at the time, and record TTFT (first generated delta after a prompt
  phase), per-session MTP proposed/accepted, context peak, and hardware
  averages. Live slot counts are never added to completed metric totals.
- A renewable SQLite lease permits only one collector per database. Additional
  dashboard processes stay read-only and automatically take over if the active
  collector lease becomes stale.
- MTP acceptance = accepted / proposed × 100 over the range, from the
  server's cumulative MTP counters.

## Storage & retention

SQLite (WAL) at `data/observatory.db` (demo: `data/observatory_demo.db`).
A background sweep every 5 minutes downsamples: raw samples are kept for 2 h,
10-s buckets for 7 days, 60-s buckets for 30 days. Models, configs, builds,
hardware and sessions are never pruned. Per-GPU samples follow the same
2 h raw / 7 d 10-second / 30 d 60-second retention tiers.

## Optional host agent

`host_agent.py` is a stdlib-only passive agent for the inference host
(Linux). It reports host facts (OS, CPU, RAM, GPUs via `nvidia-smi`, PCIe),
the llama.cpp build (version/commit/docker image/container from
`/proc/<pid>/cmdline` and env), and live per-GPU/CPU gauges with stable GPU
identity — reading only,
never controlling Docker or the server.

```bash
python3 host_agent.py --port 8091 --host 0.0.0.0
```

Then set the provider's **Agent URL** to `http://<host>:8091`.

## Stack

FastAPI + Jinja2 + SQLite (SQLModel) + ECharts (vendored, no CDN) + vanilla JS.
No frontend build step.

## Continuous integration

Pushes and pull requests run the complete Python regression suite on a
GitHub-hosted runner. This public repository contains no automated deployment
job and has no connection to a production environment or self-hosted runner.
