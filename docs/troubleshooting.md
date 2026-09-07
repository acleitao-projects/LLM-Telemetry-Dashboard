# Troubleshooting

This document covers common problems and their solutions.

## Dashboard opens but no telemetry appears

### Symptoms

- Dashboard loads normally
- All pages show empty data or "no data"
- Provider status shows STALE or OFFLINE

### Checks

1. **Verify the provider is enabled**: Go to Settings → Providers and ensure the provider's toggle is ON.

2. **Test the provider connection**: Click the Test button on the provider row. Check which endpoints fail:
   - `/health` — server is reachable
   - `/v1/models` — model listing works
   - `/metrics` — metrics endpoint returns parseable data
   - `/props` — build info endpoint works
   - `/slots` — slots endpoint returns a list

3. **Check the Base URL**: Ensure the Base URL matches the llama.cpp server's actual address. Common mistakes:
   - Wrong port (default llama.cpp server port is 8080, not 8083)
   - Missing `http://` prefix
   - Trailing slash causing path issues

4. **Check the collector logs**:
   ```bash
   journalctl -u llm-telemetry -n 50 | grep -i error
   ```

5. **Verify network reachability**:
   ```bash
   curl http://<provider-base-url>/health
   curl http://<provider-base-url>/metrics
   ```

## Dashboard is up but nothing is being polled

### Symptoms

- The dashboard serves pages normally
- The provider pill is appended with `not collecting`
- The provider server's own logs show no incoming requests

### Cause

Only one process may collect per database. The collector takes a renewable
single-writer lease; any additional process pointed at the same database stays
read-only and issues no provider requests. This happens most often when a
second instance is started alongside the first — for example a branch server on
another port sharing the production database.

### Checks

1. **Confirm the role**:
   ```bash
   curl -s http://127.0.0.1:8090/api/status | grep -o '"role":"[a-z]*"'
   ```
   `active` is the collecting process; `standby` is not.

2. **Find the other instance**:
   ```bash
   ps -eo pid,args | grep [a]pp.py
   ```

3. Stop whichever instance should not own the database. The remaining process
   takes over the lease within about 10 seconds.

## Provider unreachable / always OFFLINE

### Symptoms

- Provider status stays OFFLINE
- Test endpoint returns errors

### Checks

1. **Is the llama.cpp server running?**
   ```bash
   curl http://<server>:<port>/health
   ```

2. **Is the server accessible from the dashboard host?**
   ```bash
   nc -zv <server-ip> <port>
   ```

3. **Firewall rules**: Check if a firewall is blocking access.
   ```bash
   sudo ufw status
   ```

4. **Cors**: llama.cpp server may need `--host 0.0.0.0` to accept connections from other hosts.

## Wrong provider URL or port

### Common mistakes

| Mistake | Fix |
|---------|-----|
| Using llama.cpp default port (8080) but provider says 8083 | Update Base URL to correct port |
| Using `localhost` when server is on another machine | Use server's actual IP or hostname |
| Missing `http://` prefix | Add `http://` to Base URL |

## No models detected

### Symptoms

- Models page shows empty table
- Provider test shows `/v1/models` returns false

### Checks

1. **Verify the server has loaded a model**:
   ```bash
   curl http://<server>/v1/models
   ```

2. **Check the response format**: The endpoint should return a JSON array with objects containing `id` or `name` fields.

3. **Metrics naming**: If using a non-standard server build, check that metric names match the aliases in `observatory/settings.py`.

## Host/GPU telemetry missing

### Symptoms

- Hardware page shows no GPU data
- GPU columns are empty in session detail

### Checks

1. **Is the host agent running?**
   ```bash
   curl http://<agent-host>:8091/health
   ```

2. **Is the Agent URL configured?** Check Settings → Providers → Agent URL field.

3. **Is nvidia-smi in PATH?**
   ```bash
   which nvidia-smi
   nvidia-smi
   ```

4. **Multiple GPUs**: The agent scans for GPUs using `nvidia-smi`. If multiple GPUs exist, ensure the server is using the expected GPU.

## SQLite / database permission problems

### Symptoms

- App fails to start
- Error messages about database access

### Fixes

```bash
# Check permissions
ls -la /var/lib/llm-telemetry/

# Fix ownership (systemd deployment)
sudo chown -R llm-telemetry:llm-telemetry /var/lib/llm-telemetry/

# Check disk space
df -h /var/lib/llm-telemetry/
```

## Service / port already in use

### Symptoms

- `Address already in use` error on startup

### Fixes

```bash
# Find what's using the port
lsof -i :8090

# Kill the process
kill <PID>

# Or use a different port
python app.py --port 9090
```

## Stale / no live-session data

### Symptoms

- Live updates stop refreshing
- Sessions page shows old data

### Checks

1. **Check collector lease**:
   ```bash
   curl http://127.0.0.1:8090/api/status
   ```
   Look at the `collector` field for lease status.

2. **Restart the service**:
   ```bash
   sudo systemctl restart llm-telemetry
   ```

3. **Check for multiple collector processes**:
   ```bash
   pgrep -a "app.py"
   ```

## Pricing unavailable

### Symptoms

- Model pricing columns show "—" in Settings

### Fixes

1. **Select a provider**: Go to Settings, select a provider from the dropdown.

2. **Enter pricing**: Click into the price fields and enter values (non-negative numbers).

3. **Save**: Changes are saved immediately when you leave the field.

## Browser / cache issues

### Symptoms

- Dashboard looks broken
- Old data persists after restart
- Charts don't render

### Fixes

1. **Hard refresh**: `Ctrl+Shift+R` (Chrome/Firefox) or `Cmd+Shift+R` (Mac)

2. **Clear site data**: Open DevTools (F12) → Application → Clear site data

3. **Try incognito mode**: Rules out extension interference

4. **Check console**: Open DevTools console (F12) for JavaScript errors

5. **Verify ECharts loaded**: Check that `echarts.min.js` is in `static/js/` and loads without 404 errors

## SSE / live updates not working

### Symptoms

- No real-time updates on dashboard
- Live snapshot shows "LAST LIVE SNAPSHOT" stale indicator

### Checks

1. **Verify SSE endpoint**:
   ```bash
   curl -N http://127.0.0.1:8090/api/stream
   ```
   Should show continuous `data: ...` lines.

2. **Check browser console**: Look for EventSource connection errors.

3. **Verify collector is running**: Check that the collector lease is active via `/api/status`.

## Slow API responses

### Symptoms

- API endpoints take > 500ms
- Dashboard feels sluggish

### Checks

1. **Check API timing headers**:
   ```bash
   curl -sI http://127.0.0.1:8090/api/models | grep Server-Timing
   ```

2. **Database size**:
   ```bash
   ls -la /var/lib/llm-telemetry/
   ```

3. **Check for large result sets**: The collector logs slow API warnings with row counts.

4. **Verify indexes**: Ensure schema version is current (migrations add performance indexes).

## Troubleshooting summary

| Error | Likely cause | Fix |
|-------|--------------|-----|
| `ModuleNotFoundError: No module named 'fastapi'` | venv not activated | `. .venv/bin/activate` |
| `Address already in use` | Port conflict | `python app.py --port 9090` |
| Provider OFFLINE | Server unreachable | `curl http://<server>/health` |
| No models shown | No model loaded | Check `/v1/models` returns data |
| GPU data missing | Agent not running | `curl http://<agent>:8091/health` |
| Charts not rendering | JS error | Open browser DevTools console |
| SSE not connecting | Collector lease lost | Restart service |
