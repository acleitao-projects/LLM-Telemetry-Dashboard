# Installation

This document covers the supported installation paths for LLM-Telemetry.

## Supported environments

- **Linux** — primary target (verified on Ubuntu 24.04 / Debian 12)
- **Python 3.12+** — required
- **SQLite 3** — built into Python, no separate install needed

No Dockerfile or Docker Compose is provided. Run it from a virtual environment, directly or under systemd.

## Local development / evaluation

The fastest path to a working dashboard:

```bash
git clone https://github.com/acleitao-projects/LLM-Telemetry-Dashboard.git
cd LLM-Telemetry-Dashboard
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py --demo
```

Open http://127.0.0.1:8090

The `--demo` flag seeds 30 days of synthetic history and produces live synthetic activity through the real collector path. No network calls.

### Custom port or database path

```bash
python app.py --demo --host 0.0.0.0 --port 9090 --db /tmp/my_demo.db
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8090` | Listening port |
| `--db` | `data/observatory.db` (real) or `data/observatory_demo.db` (demo) | SQLite database path |
| `--demo` | off | Use synthetic data mode |

## Run as a service (systemd)

For an always-on instance, run it under systemd. Adjust the user and paths to
your host.

```ini
# /etc/systemd/system/llm-telemetry.service
[Unit]
Description=LLM-Telemetry dashboard
After=network-online.target

[Service]
User=llm-telemetry
WorkingDirectory=/opt/llm-telemetry
ExecStart=/opt/llm-telemetry/.venv/bin/python /opt/llm-telemetry/app.py \
    --host 0.0.0.0 --port 8090 --db /var/lib/llm-telemetry/observatory.db
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/llm-telemetry
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

Keep the database outside the code directory (e.g. `/var/lib/llm-telemetry/`)
so it survives updates.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now llm-telemetry.service
curl http://127.0.0.1:8090/api/meta        # expect "demo":false
```

Update by pulling the new code and `systemctl restart llm-telemetry`. The
schema migrates itself forward on start; take a database backup first (see
[Operations → Database backups](operations.md#database-backups)).
