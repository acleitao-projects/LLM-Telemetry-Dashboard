# Security

> [!WARNING]
> **LLM-Telemetry has no authentication.** Do not expose it directly to the public internet; keep it on a trusted closed network or place it behind an authenticated reverse proxy.

## Operating assumption

LLM-Telemetry is designed for a **trusted closed network**. The dashboard, API, event stream, and screenshot upload endpoints are all accessible without authentication or authorization.

## Exposure risks

### No authentication

Any client that can reach the dashboard URL can:

- View all telemetry data
- Filter and query by model, provider, range
- Unload models on enabled providers (via the **Unload Models** action in the topbar)
- Upload screenshots via `/api/screenshots/{id}`
- Manage providers (add, edit, delete, test)
- Update display settings and model pricing

### Sensitive data exposure

The dashboard may expose:

- Model file names and paths
- Launch configurations (including GPU layers, batch size, cache settings)
- Server build versions and commit hashes
- Docker images and container IDs
- GPU models, VRAM, temperature, power
- CPU model, RAM, thread count
- Session data with token counts and speeds
- Provider base URLs and agent URLs
- Model pricing data

### Provider credentials

Provider base URLs may include authentication tokens or API keys in the URL path or query string:

```
http://user:pass@server:8083
http://server:8083?token=abc123
```

These appear in the Settings page and may be visible in browser history or logs.

## Public internet exposure

Running LLM-Telemetry directly on the public internet is safe for **reading** telemetry, but consider:

- **Model unload action**: The topbar includes an "Unload Models" button. Clicking it sends `/models/unload` to all enabled providers.
- **Provider management**: Anyone can add, edit, or delete providers.
- **Screenshot upload**: The `/api/screenshots/{id}` endpoint accepts any PNG (up to 12 MB).
- **Database path**: The SQLite database path may be visible in server response headers or error messages.

### Recommendations for public exposure

1. **Use a reverse proxy** with authentication:
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8090;
       auth_basic "Protected";
       auth_basic_user_file /etc/nginx/.htpasswd;
   }
   ```

2. **Use a firewall** to restrict access:
   ```bash
   sudo ufw allow from 10.0.0.0/8 to any port 8090
   ```

3. **Use a VPN** for remote access.

4. **Hide the dashboard behind** a subdomain with TLS:
   ```
   https://telemetry.example.com
   ```

## Reverse proxy configuration

### Nginx example

```nginx
server {
    listen 443 ssl;
    server_name telemetry.example.com;

    ssl_certificate /etc/ssl/certs/telemetry.crt;
    ssl_certificate_key /etc/ssl/private/telemetry.key;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Allow SSE connections
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }
}
```

### Caddy example

```caddy
telemetry.example.com {
    reverse_proxy localhost:8090
}
```

## Provider secrets

Provider base URLs may contain secrets:

- **API keys** in query parameters: `http://server:8083?api_key=xxx`
- **Basic auth** in URL: `http://user:pass@server:8083`
- **Bearer tokens** in headers (stored by the collector, not in the database)

The database stores `base_url` and `agent_url` for each provider. These are plain text in SQLite.

## Database security

The SQLite database (`data/observatory.db` or `/var/lib/llm-telemetry/observatory.db`) contains:

- Provider URLs (may include credentials)
- Model names and configurations
- Telemetry samples (counters, gauges, timestamps)
- Session data
- Model pricing

The database file permissions depend on how it was created:

```bash
ls -la /var/lib/llm-telemetry/observatory.db
# Typical: -rw-r--r-- (readable by all users)
```

## Host agent security

The host agent (`host_agent.py`) runs as a separate HTTP server:

| Endpoint | Auth | Sensitive data |
|----------|------|----------------|
| `/info` | None | CPU model, RAM, hostname |
| `/gpu` | None | GPU models, VRAM, temperature, power |
| `/llama` | None | Build version, commit, Docker image, container ID |
| `/health` | None | Status string |

The agent has no authentication. Ensure it's on a trusted network or restrict with a firewall.

## Screenshot security

Screenshots are stored in `<db-dir>/screenshots/` and served via `/screenshots/{id}.png`. Each screenshot has a 24-hour TTL and is auto-deleted. Screenshots are named by UUID and stored as PNG files.

## Network binding

By default, the app binds to `127.0.0.1` (localhost only). To expose to other hosts:

```bash
python app.py --host 0.0.0.0 --port 8090
```

When running behind a reverse proxy, bind to `0.0.0.0` so the proxy can reach it.

## Security checklist

- [ ] Dashboard is on a trusted closed network, or behind a reverse proxy
- [ ] No public-facing ports without authentication
- [ ] Provider base URLs don't expose sensitive query parameters
- [ ] Host agent is reachable only from the dashboard host (or filtered)
- [ ] Database file is readable by the service user
- [ ] Old screenshots are auto-deleted (24h TTL)
- [ ] systemd service has `ProtectSystem=strict` and `ReadWritePaths` set
