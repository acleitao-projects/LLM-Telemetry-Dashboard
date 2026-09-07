"""Observatory - passive llama.cpp observability dashboard.

Run:
    python app.py            # real mode (reads from the configured provider by default)
    python app.py --demo     # demo mode (synthetic data, no network)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select

from observatory import app_state, database as odb, fx, metrics as m
from observatory import pricing_sync
from observatory.snapshot import SnapshotRegistry, build_snapshot
from observatory.collector import Collector
from observatory.llama_provider import LlamaClient
from observatory.models import Provider, Setting, now_ms
from observatory.settings import (DB_PATH_DEFAULT, DB_PATH_DEMO, DEFAULT_PROVIDER_AGENT_URL,
                                  DEFAULT_PROVIDER_NAME, DEFAULT_PROVIDER_TYPE, DEFAULT_PROVIDER_URL, RANGE_KEYS,
                                  SNAPSHOT_REVALIDATE_S)

log = logging.getLogger("observatory")


def _norm_range(range: str) -> str:
    return range if range in RANGE_KEYS else "7d"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOT_DIR = os.environ.get("LLM_TELEMETRY_SCREENSHOT_DIR")
SCREENSHOT_TTL_S = 24 * 60 * 60


def _screenshot_dir() -> str:
    """Keep captures beside the configured database unless explicitly overridden."""
    if SCREENSHOT_DIR:
        return SCREENSHOT_DIR
    db_path = odb.get_db_path()
    if db_path:
        return os.path.join(os.path.dirname(os.path.abspath(db_path)), "screenshots")
    return os.path.join(BASE_DIR, "data", "screenshots")


def _screenshot_path(capture_id: str) -> str:
    try:
        normalized = str(UUID(capture_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if normalized != capture_id.lower():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return os.path.join(_screenshot_dir(), normalized + ".png")


def _cleanup_screenshots(now: float | None = None) -> None:
    screenshot_dir = _screenshot_dir()
    os.makedirs(screenshot_dir, exist_ok=True)
    cutoff = (time.time() if now is None else now) - SCREENSHOT_TTL_S
    for entry in os.scandir(screenshot_dir):
        if not entry.is_file() or not entry.name.endswith((".png", ".tmp")):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except FileNotFoundError:
            pass


async def _screenshot_cleanup_loop() -> None:
    while True:
        _cleanup_screenshots()
        await asyncio.sleep(SCREENSHOT_TTL_S)


def ensure_default_provider():
    """The default provider exists from first start, in every mode."""
    with odb.new_session() as s:
        p = s.exec(select(Provider).where(Provider.name == DEFAULT_PROVIDER_NAME)).first()
        if p is None:
            p = Provider(name=DEFAULT_PROVIDER_NAME, ptype=DEFAULT_PROVIDER_TYPE, base_url=DEFAULT_PROVIDER_URL,
                         agent_url=DEFAULT_PROVIDER_AGENT_URL, enabled=True, is_default=True,
                         poll_interval_s=1.0, notes="Default llama.cpp server")
            s.add(p)
            s.commit()


# Per-request HTTP client logging is pure noise at this poll rate.
HTTPX_LOG_LEVEL = logging.WARNING
# Loggers quietened at startup. Only the HTTP client libraries: the app's own
# loggers keep their level so warnings and errors are unaffected.
QUIET_LOGGERS = ("httpx", "httpcore")


def configure_logging() -> None:
    """Application log configuration.

    The collector polls every provider once a second and issues several
    requests per poll, so ``httpx`` at INFO logs a line for each one.  Measured
    on production that was 99.5% of all log output -- 10,224 of 10,272 lines in
    an hour, roughly 245k lines a day and a 663 MB journal -- which buried the
    app's own warnings and made the journal impractical to search.

    Provider health is already surfaced on the dashboard and in
    ``Provider.last_error``, and request failures still reach the log as
    collector warnings, so nothing diagnostic is lost.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(HTTPX_LOG_LEVEL)

# SSE cadence, and how often the stream re-checks for shutdown while waiting.
# An open streaming response blocks uvicorn's graceful shutdown until it ends,
# so the wait is chopped into short ticks rather than one long sleep.
SSE_INTERVAL_S = 2.0
SSE_SHUTDOWN_TICK_S = 0.1


def _aged_observation(live: dict, now: int) -> dict:
    """Re-date a retained live observation against the current clock.

    ``age_s`` is computed when a slot observation is captured.  Retained
    "last live" entries are re-served for up to 120 s, so replaying the
    captured value makes a two-minute-old snapshot claim it was observed a
    moment ago.  ``observed_at`` stays authoritative; ``age_s`` is derived
    from it every time the entry is published.
    """
    observed_at = live.get("observed_at")
    if not observed_at:
        return live
    aged = dict(live)
    aged["age_s"] = round(max(0, now - observed_at) / 1000.0, 1)
    return aged


def create_app(demo: bool = False) -> FastAPI:
    app = FastAPI(title="LLM-Telemetry", docs_url=None, redoc_url=None)
    app.state.demo = demo
    app.state.started = time.time()
    app.state.collector = None
    # Set by main() so long-lived responses can notice uvicorn is shutting down.
    # Left None under TestClient and in tests, which never run a real server.
    app.state.server = None
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    overview_cache = {"data": None, "refreshed": 0.0, "refreshing": False}
    overview_lock = threading.Lock()
    live_cache = {"data": None, "refreshed": 0.0, "last_known": {},
                  "encoded": None, "generation": 0}
    live_lock = threading.Lock()
    live_stop = threading.Event()
    live_thread: Optional[threading.Thread] = None
    # Automatic model-pricing sync (#23): a dedicated worker thread, because the
    # optional inference-assisted name match can take 30-60 s. The once-per-day
    # gate lives in pricing_sync.refresh (persisted anchor); the lock is the
    # single-flight guard shared with the "Run now" endpoint.
    pricing_stop = threading.Event()
    pricing_thread: Optional[threading.Thread] = None
    app.state.pricing_run_lock = threading.Lock()
    app.state.pricing_last_result = None
    def _snapshot_data_version() -> int:
        collector = app.state.collector
        return getattr(collector, "data_generation", 0) if collector else 0

    app.state.snapshots = SnapshotRegistry(
        build_snapshot, odb.new_session, revalidate_s=SNAPSHOT_REVALIDATE_S,
        data_version_fn=_snapshot_data_version)
    snapshots = app.state.snapshots

    def refresh_overview():
        try:
            with odb.new_session() as s:
                data = m.overview(s)
            with overview_lock:
                overview_cache["data"] = data
                overview_cache["refreshed"] = time.monotonic()
        finally:
            with overview_lock:
                overview_cache["refreshing"] = False

    def cached_overview():
        with overview_lock:
            data = overview_cache["data"]
            stale = time.monotonic() - overview_cache["refreshed"] >= 5.0
            if data is not None and stale and not overview_cache["refreshing"]:
                overview_cache["refreshing"] = True
                threading.Thread(target=refresh_overview, name="overview-cache",
                                 daemon=True).start()
        if data is not None:
            return data
        refresh_overview()
        with overview_lock:
            return overview_cache["data"]

    def collector_role() -> str:
        """Whether *this* process is the one actually polling providers.

        Only the holder of the single-writer lease collects; any other process
        sharing the database stays read-only and issues no provider requests.
        Its pages would otherwise be indistinguishable from a collecting one.
        """
        collector = app.state.collector
        if collector is None:
            return "disabled"
        return (collector.lease_status() or {}).get("role") or "unknown"

    def refresh_live_snapshot():
        with odb.new_session() as s:
            data = m.realtime_snapshot(s)
        data["collector_role"] = collector_role()
        now = time.monotonic()
        with live_lock:
            last_known = live_cache["last_known"]
            for item in data["active_models"]:
                live = dict(item["realtime"])
                live["status"] = "LAST LIVE"
                live["snapshot"] = "LAST LIVE SNAPSHOT"
                live["processing"] = False
                live["provisional"] = True
                last_known[item["model_id"]] = (now, live)
            # A stale indicator is useful immediately after a request ends,
            # but discard old observations rather than presenting them as live.
            cutoff = now - 120.0
            live_cache["last_known"] = {
                model_id: value for model_id, value in last_known.items()
                if value[0] >= cutoff
            }
            data["last_known"] = {
                model_id: _aged_observation(value[1], data["now"])
                for model_id, value in live_cache["last_known"].items()
            }
            # Serialize once per refresh and publish the encoded string under
            # the same lock hold as the raw dict, so every subscriber sees a
            # coherent (encoded, raw) pair for this snapshot version.
            encoded = json.dumps(data)
            live_cache["data"] = data
            live_cache["encoded"] = encoded
            live_cache["generation"] = live_cache.get("generation", 0) + 1
            live_cache["refreshed"] = now

    def cached_live_snapshot():
        """Return the one-per-second snapshot shared by all SSE subscribers."""
        with live_lock:
            data = live_cache["data"]
        if data is not None:
            return data
        refresh_live_snapshot()
        with live_lock:
            return live_cache["data"]

    def cached_live_snapshot_encoded():
        """Pre-serialized SSE payload, refreshed once per snapshot version."""
        with live_lock:
            encoded = live_cache["encoded"]
        if encoded is not None:
            return encoded
        refresh_live_snapshot()
        with live_lock:
            return live_cache["encoded"]

    def selected_realtime(model_ids: list[int]) -> Optional[dict]:
        snapshot = cached_live_snapshot()
        active = [item for item in snapshot.get("active_models", [])
                  if item["model_id"] in model_ids]
        if active:
            active.sort(key=lambda item: (item["rank"], item["task_count"], item["latest_seen"]),
                        reverse=True)
            return active[0]["realtime"]
        for model_id in model_ids:
            stale = snapshot.get("last_known", {}).get(model_id)
            if stale:
                return stale
        return None

    def live_snapshot_loop():
        while not live_stop.is_set():
            try:
                refresh_live_snapshot()
            except Exception:
                log.exception("realtime snapshot refresh failed")
            live_stop.wait(1.0)

    def _pricing_sync_blocking(*, force: bool = False,
                               trigger: str = "scheduled"):
        """One pricing-sync attempt. Blocking; runs on the pricing-sync thread
        or in a worker thread off a request. Single-flight: a second caller
        while a run is in progress gets ``{"error": "in progress"}``."""
        lock = app.state.pricing_run_lock
        if not lock.acquire(blocking=False):
            return {"error": "in progress"}
        try:
            with odb.new_session() as s:
                res = pricing_sync.refresh(
                    s, force=force, trigger=trigger,
                    # short timeout: this client only does the idle
                    # models()/slots() probe; llm_match builds its own.
                    match_client_factory=lambda p: LlamaClient(
                        p.base_url, timeout=8.0))
            if res:
                app.state.pricing_last_result = res["run"]
                for pid in res.get("affected_provider_ids", []):
                    for rk in RANGE_KEYS:
                        app.state.snapshots.invalidate((pid, rk))
            return res
        finally:
            lock.release()

    def pricing_sync_loop():
        while not pricing_stop.is_set():
            try:
                _pricing_sync_blocking(trigger="scheduled")
            except pricing_sync.PricingSyncError:
                pass  # keep last-known-good rates; the next tick retries
            except Exception:  # pragma: no cover - defensive
                log.exception("pricing sync loop iteration failed")
            pricing_stop.wait(PRICING_TICK_S)

    def timed(response: Response, endpoint: str, started: float, data: dict) -> dict:
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["Server-Timing"] = f'app;dur={duration_ms:.1f}'
        if duration_ms >= 500:
            row_value = (data.get("rows") or data.get("models") or
                         data.get("sessions") or data.get("gpus") or [])
            rows = len(row_value) if hasattr(row_value, "__len__") else 1
            log.warning("slow API %s: %.1f ms (%s rows)", endpoint, duration_ms, rows)
        return data

    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
              name="static")

    NAV = [
        ("overview", "Overview", "/overview"),
        ("models", "Models", "/models"),
        ("sessions", "Sessions", "/sessions"),
        ("compare", "Compare", "/compare"),
        ("hardware", "Hardware", "/hardware"),
        ("settings", "Settings", "/settings"),
    ]

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=RedirectResponse)
    def index():
        return RedirectResponse("/overview")

    @app.get("/overview", response_class=HTMLResponse)
    def overview_page(request: Request):
        return templates.TemplateResponse(request, "base.html", {
            "page": "overview", "title": "Overview",
            "subtitle": "Live operation state. Passive read-only telemetry.",
            "nav": NAV, "demo": demo, "query": {}, "template": "overview.html",
        })

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request):
        return templates.TemplateResponse(request, "base.html", {
            "page": "models", "title": "Models",
            "subtitle": "Which models did the work. Group by family to roll quants together.",
            "nav": NAV, "demo": demo, "query": {}, "template": "models.html",
        })

    @app.put("/api/screenshots/{capture_id}")
    async def save_screenshot(capture_id: str, request: Request):
        path = _screenshot_path(capture_id)
        image = await request.body()
        if len(image) > 12 * 1024 * 1024:
            return Response("Screenshot is too large", status_code=413)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return Response("Invalid PNG screenshot", status_code=400)
        os.makedirs(_screenshot_dir(), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "wb") as output:
            output.write(image)
        os.replace(temporary, path)
        _cleanup_screenshots()
        return {"url": f"/screenshots/{capture_id}.png"}

    @app.get("/screenshots/{capture_id}/wait")
    async def wait_for_screenshot(capture_id: str):
        path = _screenshot_path(capture_id)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if os.path.isfile(path):
                return RedirectResponse(f"/screenshots/{capture_id}.png", status_code=302)
            await asyncio.sleep(0.05)
        return HTMLResponse("Screenshot generation failed or timed out.", status_code=408)

    @app.get("/screenshots/{capture_id}.png")
    def screenshot_image(capture_id: str):
        path = _screenshot_path(capture_id)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="Screenshot not found")
        return FileResponse(path, media_type="image/png", headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="llm-telemetry-{capture_id}.png"',
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/model/{mid}", response_class=HTMLResponse)
    def model_page(request: Request, mid: int):
        return templates.TemplateResponse(request, "base.html", {
            "page": "model", "title": "Model",
            "subtitle": "", "nav": NAV, "demo": demo,
            "query": {"mid": mid}, "template": "model_detail.html", "mid": mid,
        })

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions_page(request: Request):
        return templates.TemplateResponse(request, "base.html", {
            "page": "sessions", "title": "Sessions",
            "subtitle": "Externally driven inference sessions, observed passively.",
            "nav": NAV, "demo": demo, "query": {}, "template": "sessions.html",
        })

    @app.get("/session/{sid}", response_class=HTMLResponse)
    def session_page(request: Request, sid: int):
        return templates.TemplateResponse(request, "base.html", {
            "page": "session", "title": "Session",
            "subtitle": "", "nav": NAV, "demo": demo,
            "query": {"sid": sid}, "template": "session_detail.html", "sid": sid,
        })

    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(request: Request):
        return templates.TemplateResponse(request, "base.html", {
            "page": "compare", "title": "Compare",
            "subtitle": "Compare specific model files across the same time range.",
            "nav": NAV, "demo": demo, "query": {}, "template": "compare.html",
        })

    @app.get("/hardware", response_class=HTMLResponse)
    def hardware_page(request: Request):
        return templates.TemplateResponse(request, "base.html", {
            "page": "hardware", "title": "Hardware",
            "subtitle": "Inference hardware where observable.",
            "nav": NAV, "demo": demo, "query": {}, "template": "hardware.html",
        })

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return templates.TemplateResponse(request, "base.html", {
            "page": "settings", "title": "Settings",
            "subtitle": "Providers, collection, storage and display.",
            "nav": NAV, "demo": demo, "query": {}, "template": "settings.html",
        })

    # -------------------------------------------------------------------- api
    @app.get("/api/meta")
    def api_meta():
        with odb.new_session() as s:
            provs = s.exec(select(Provider).order_by(Provider.id)).all()
            disp = _display_settings(s)
            cur = _currency_settings(s)
            return {
                "demo": demo,
                "providers": [{"id": p.id, "name": p.name,
                               "status": m.effective_provider_status(p, now_ms()),
                               "enabled": p.enabled, "is_default": p.is_default}
                              for p in provs],
                "display": disp,
                # null unless a rate is live for the picked currency, so the
                # page's disabled state is byte-identical to today.
                "currency": cur if cur["enabled"] else None,
                "collector_role": collector_role(),
                "snapshots": snapshots.stats(),
            }

    @app.get("/api/overview")
    def api_overview():
        return cached_overview()

    @app.get("/api/models")
    def api_models(response: Response, range: str = "7d", group: str = "family",
                   provider: Optional[int] = None):
        started = time.perf_counter()
        range = _norm_range(range)
        with odb.new_session() as s:
            summary = snapshots.get(provider, range)
            return timed(response, "models", started,
                         m.models_page(s, provider, range, "model", summary,
                                       include_sparks=True))

    @app.get("/api/models/sparks")
    def api_model_sparks(response: Response, range: str = "7d", group: str = "family",
                         provider: Optional[int] = None):
        started = time.perf_counter()
        range = _norm_range(range)
        with odb.new_session() as s:
            summary = snapshots.get(provider, range)
            return timed(response, "models-sparks", started,
                         {"sparks": m.model_sparks(s, summary, group)})

    @app.post("/api/models/unload-all")
    def api_unload_all_models():
        """Unload all router models after an explicit dashboard confirmation."""
        if demo:
            return {"ok": True, "demo": True, "providers": [],
                    "summary": {"unloaded_models": 0, "failed_providers": 0}}
        with odb.new_session() as s:
            providers = s.exec(
                select(Provider).where(Provider.enabled == True).order_by(Provider.id)  # noqa: E712
            ).all()
        results = [_unload_provider_models(provider) for provider in providers]
        return {
            "ok": not any(result["status"] == "error" for result in results),
            "demo": False,
            "providers": results,
            "summary": {
                "unloaded_models": sum(len(result["models"]) for result in results),
                "failed_providers": sum(result["status"] == "error" for result in results),
            },
        }

    @app.get("/api/models/selected")
    def api_selected(response: Response, ids: str = "", range: str = "7d", provider: Optional[int] = None):
        started = time.perf_counter()
        range = _norm_range(range)
        ids_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        with odb.new_session() as s:
            summary = snapshots.get(provider, range)
            return timed(response, "models-selected", started,
                         m.selected_stats(s, ids_list, provider, range, summary,
                                          realtime_override=selected_realtime(ids_list)))

    @app.get("/api/model/{mid}")
    def api_model(mid: int, range: str = "24h"):
        with odb.new_session() as s:
            return m.model_detail(s, mid, range)

    @app.get("/api/sessions")
    def api_sessions(response: Response, provider: Optional[int] = None, model: Optional[int] = None,
                     quant: Optional[str] = None, mtp: Optional[str] = None,
                     reasoning: Optional[str] = None, range: str = "7d"):
        started = time.perf_counter()
        with odb.new_session() as s:
            return timed(response, "sessions", started,
                         m.sessions_page(s, provider, model, quant, mtp, reasoning, range))

    @app.get("/api/session/{sid}")
    def api_session(sid: int):
        with odb.new_session() as s:
            return m.session_detail(s, sid)

    @app.get("/api/compare")
    def api_compare(ids: str = ""):
        ids_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        with odb.new_session() as s:
            return m.compare(s, ids_list)

    @app.get("/api/compare/candidates")
    def api_compare_candidates():
        with odb.new_session() as s:
            return {"sessions": m.compare_candidates(s)}

    @app.get("/api/compare/models/candidates")
    def api_compare_model_candidates(response: Response, provider: Optional[int] = None,
                                     range: str = "7d"):
        started = time.perf_counter()
        range = _norm_range(range)
        with odb.new_session() as s:
            summary = snapshots.get(provider, range)
            return timed(response, "compare-candidates", started,
                         {"models": m.compare_model_candidates(s, provider, range, summary),
                          "range": range})

    @app.get("/api/compare/models")
    def api_compare_models(response: Response, keys: str = "", provider: Optional[int] = None,
                           range: str = "7d"):
        started = time.perf_counter()
        range = _norm_range(range)
        model_keys = [x for x in keys.split("|") if x]
        with odb.new_session() as s:
            summary = snapshots.get(provider, range)
            return timed(response, "compare-models", started,
                         m.compare_models(s, model_keys, provider, range, summary=summary))

    @app.get("/api/compare/models/gpus")
    def api_compare_model_gpus(response: Response, keys: str = "", provider: Optional[int] = None,
                               range: str = "7d"):
        started = time.perf_counter()
        model_keys = [x for x in keys.split("|") if x]
        with odb.new_session() as s:
            return timed(response, "compare-model-gpus", started,
                         m.compare_model_gpus(s, model_keys, provider, range))

    @app.get("/api/hardware")
    def api_hardware(provider: Optional[int] = None):
        with odb.new_session() as s:
            return m.hardware(s, provider)

    @app.get("/api/status")
    def api_status():
        with odb.new_session() as s:
            data = m.status(s)
        collector = app.state.collector
        data["collector"] = (collector.lease_status() if collector else
                             {"role": "disabled", "owner_id": None})
        endpoints = collector.endpoint_status() if collector else {}
        for provider in data.get("providers", []):
            provider["endpoints"] = endpoints.get(provider.get("id"), {})
        return data

    def shutting_down() -> bool:
        """True once the process has begun shutting down.

        uvicorn sets ``should_exit`` the moment SIGTERM arrives, before it waits
        for in-flight responses to finish, so this flips early enough for a
        streaming response to end itself instead of holding shutdown open.
        """
        server = getattr(app.state, "server", None)
        return bool((server is not None and server.should_exit) or live_stop.is_set())

    @app.get("/api/stream")
    async def api_stream():
        async def gen():
            # An SSE response never completes on its own, and uvicorn's graceful
            # shutdown waits for in-flight requests. Looping forever meant a
            # single open dashboard tab kept the process alive until systemd's
            # TimeoutStopSec elapsed and SIGKILLed it -- which skipped the
            # collector's lease release and left a gap in provider polling
            # after every restart. End the stream when shutdown starts instead.
            while not shutting_down():
                try:
                    # One shared serialized payload per snapshot version; the
                    # refresh loop re-encodes under the same lock that swaps
                    # the raw dict, so encoding work never runs per subscriber.
                    yield f"data: {await asyncio.to_thread(cached_live_snapshot_encoded)}\n\n"
                except Exception:
                    yield f"data: {json.dumps({'error': 'unavailable', 'now': now_ms()})}\n\n"
                waited = 0.0
                while waited < SSE_INTERVAL_S and not shutting_down():
                    await asyncio.sleep(SSE_SHUTDOWN_TICK_S)
                    waited += SSE_SHUTDOWN_TICK_S
        return StreamingResponse(gen(), media_type="text/event-stream",
                                  headers={"Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})

    # ------------------------------------------------------- provider CRUD
    @app.get("/api/settings/providers")
    def api_providers():
        with odb.new_session() as s:
            provs = s.exec(select(Provider).order_by(Provider.id)).all()
            return {"providers": [_provider_out(p) for p in provs]}

    @app.post("/api/settings/providers")
    async def api_provider_create(request: Request):
        data = await request.json()
        with odb.new_session() as s:
            _set_defaults(s, data)
            p = Provider(
                name=str(data.get("name") or "provider").strip(),
                ptype=str(data.get("ptype") or "llama.cpp"),
                base_url=str(data.get("base_url") or "").strip().rstrip("/"),
                agent_url=(str(data.get("agent_url")).strip().rstrip("/")
                           if data.get("agent_url") else None),
                enabled=bool(data.get("enabled", True)),
                is_default=bool(data.get("is_default", False)),
                poll_interval_s=float(data.get("poll_interval_s") or 1.0),
                notes=str(data.get("notes") or ""),
            )
            s.add(p)
            s.commit()
            s.refresh(p)
            return _provider_out(p)

    @app.put("/api/settings/providers/{pid}")
    async def api_provider_update(pid: int, request: Request):
        data = await request.json()
        with odb.new_session() as s:
            p = s.get(Provider, pid)
            if p is None:
                return {"error": "not found"}
            _apply_provider(s, p, data)
            s.commit()
            s.refresh(p)
            return _provider_out(p)

    @app.delete("/api/settings/providers/{pid}")
    def api_provider_delete(pid: int):
        with odb.new_session() as s:
            p = s.get(Provider, pid)
            if p is None:
                return {"error": "not found"}
            if p.is_default:
                p.is_default = False
                s.commit()
            s.delete(p)
            s.commit()
            return {"ok": True}

    @app.post("/api/settings/providers/{pid}/test")
    def api_provider_test(pid: int):
        with odb.new_session() as s:
            p = s.get(Provider, pid)
            if p is None:
                return {"error": "not found"}
            return _test_provider(p)

    # ----------------------------------------------------------- display cfg
    @app.get("/api/settings/display")
    def api_display():
        with odb.new_session() as s:
            return {"display": _display_settings(s)}

    @app.put("/api/settings/display")
    async def api_display_put(request: Request):
        data = await request.json()
        with odb.new_session() as s:
            for k in ("default_range", "default_group", "theme"):
                if k in data:
                    _write_setting(s, k, data[k])
            s.commit()
            return {"display": _display_settings(s)}

    # ------------------------------------------------------- secondary currency
    # A separate group from display: its write has a side effect (an outbound
    # fetch) and its read returns derived fields. Folding it into the display
    # PUT would make saving the theme trigger a network call.
    @app.get("/api/settings/currency")
    def api_currency():
        with odb.new_session() as s:
            return {
                "currency": _currency_settings(s),
                "options": [{"code": c, "symbol": v["symbol"],
                             "decimals": v["decimals"], "name": v["name"]}
                            for c, v in fx.SUPPORTED.items()],
            }

    @app.put("/api/settings/currency")
    async def api_currency_put(request: Request):
        data = await request.json()
        code = data.get("code")
        if code == "":
            code = None
        if code is not None and code not in fx.SUPPORTED:
            raise HTTPException(status_code=400, detail="unsupported currency")
        with odb.new_session() as s:
            _write_setting(s, "secondary_currency", code)
            s.commit()
        err = None
        if code is not None:
            # Synchronous, short timeout: Settings shows the live rate with no
            # second round-trip (requirement 7). One call per rare user click.
            try:
                await asyncio.to_thread(_fx_refresh_blocking, force=True,
                                        timeout=5.0)
            except fx.FxError as exc:
                err = str(exc)
        with odb.new_session() as s:
            return {"currency": _currency_settings(s), "error": err}

    @app.post("/api/settings/currency/refresh")
    async def api_currency_refresh():
        with odb.new_session() as s:
            block = _currency_settings(s)
        if block["code"] is None:
            return {"currency": block, "error": "no currency selected"}
        # Holding the button down cannot hammer Frankfurter.
        if block["fetched_at"] is not None and time.time() - block["fetched_at"] < 60:
            return {"currency": block, "error": "too soon"}
        err = None
        try:
            await asyncio.to_thread(_fx_refresh_blocking, force=True, timeout=10.0)
        except fx.FxError as exc:
            err = str(exc)
        with odb.new_session() as s:
            return {"currency": _currency_settings(s), "error": err}

    # ---------------------------------------------------- automatic pricing
    # Separate from model pricing: its write configures a schedule, its read
    # returns derived run state, and "Run now" kicks a background job.
    _RUN_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    def _pricing_sync_block(s: Session) -> dict:
        from observatory.models import PricingSyncRun
        block = pricing_sync.stored_config(s)
        block["running"] = app.state.pricing_run_lock.locked()
        block["last_result"] = app.state.pricing_last_result
        runs = s.exec(select(PricingSyncRun)
                      .order_by(PricingSyncRun.started_at.desc()).limit(10)).all()
        block["recent_runs"] = [pricing_sync._run_dict(r) for r in runs]
        provs = s.exec(select(Provider).order_by(Provider.id)).all()
        block["provider_options"] = [
            {"id": p.id, "name": p.name, "is_default": p.is_default,
             "status": m.effective_provider_status(p, now_ms())}
            for p in provs
        ]
        return block

    @app.get("/api/settings/pricing-sync")
    def api_pricing_sync():
        with odb.new_session() as s:
            return _pricing_sync_block(s)

    @app.put("/api/settings/pricing-sync")
    async def api_pricing_sync_put(request: Request):
        data = await request.json()
        if "run_time" in data:
            rt = data.get("run_time")
            if not isinstance(rt, str) or not _RUN_TIME_RE.match(rt):
                raise HTTPException(status_code=422,
                                    detail="run_time must be HH:MM (24h)")
        if "match_provider_id" in data:
            pid = data.get("match_provider_id")
            if pid is not None:
                if not isinstance(pid, int):
                    raise HTTPException(status_code=422,
                                        detail="match_provider_id must be an int or null")
                with odb.new_session() as s:
                    if s.get(Provider, pid) is None:
                        raise HTTPException(status_code=422,
                                            detail="match_provider_id is not a known provider")
        if "match_model" in data:
            mm = data.get("match_model")
            if mm is not None and (not isinstance(mm, str) or len(mm) > 200):
                raise HTTPException(status_code=422,
                                    detail="match_model must be a string or null")
        if "prompt" in data:
            prompt = data.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise HTTPException(status_code=422, detail="prompt must be non-empty")
            if len(prompt) > 8000:
                raise HTTPException(status_code=422, detail="prompt is too long")
            missing = [f for f in pricing_sync.PROMPT_FIELDS if f not in prompt]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"prompt must contain {' and '.join(pricing_sync.PROMPT_FIELDS)}")
        keymap = {
            "enabled": ("pricing_sync_enabled", bool),
            "run_time": ("pricing_sync_run_time", str),
            "use_inference_match": ("pricing_use_inference_match", bool),
            "match_provider_id": ("pricing_match_provider_id", lambda v: v),
            "match_model": ("pricing_match_model",
                            lambda v: (v.strip() or None) if isinstance(v, str) else None),
            "prompt": ("pricing_match_prompt", str),
        }
        with odb.new_session() as s:
            for field, (skey, cast) in keymap.items():
                if field in data:
                    _write_setting(s, skey, cast(data[field]))
            s.commit()
            return _pricing_sync_block(s)

    @app.post("/api/settings/pricing-sync/run")
    async def api_pricing_sync_run(request: Request):
        force_gate = request.query_params.get("force") in ("1", "true", "yes")
        last = app.state.pricing_last_result
        if not force_gate and last and last.get("started_at"):
            if now_ms() - last["started_at"] < 60_000:
                with odb.new_session() as s:
                    block = _pricing_sync_block(s)
                return {"error": "too soon", **block}
        if app.state.pricing_run_lock.locked():
            return Response(
                content=json.dumps({"error": "a pricing run is already in progress"}),
                status_code=409, media_type="application/json")
        # Fire and forget: a full run (catalog fetch + optional inference) can
        # take a minute or more. The client polls GET /api/settings/pricing-sync
        # for `running` and the growing run row. The single-flight lock inside
        # _pricing_sync_blocking still prevents overlap.
        app.state.pricing_run_task = asyncio.create_task(
            asyncio.to_thread(_pricing_sync_blocking, force=True,
                              trigger="manual"))
        await asyncio.sleep(0.1)  # let the worker acquire the lock + log the row
        with odb.new_session() as s:
            block = _pricing_sync_block(s)
        return {"ok": True, "started": True, **block}

    @app.get("/api/settings/pricing-sync/runs")
    def api_pricing_sync_runs(limit: int = 50):
        from observatory.models import PricingSyncRun
        limit = max(1, min(200, limit))
        with odb.new_session() as s:
            runs = s.exec(select(PricingSyncRun)
                          .order_by(PricingSyncRun.started_at.desc())
                          .limit(limit)).all()
            return {"runs": [pricing_sync._run_dict(r) for r in runs]}

    @app.get("/api/settings/pricing-sync/provider-models")
    def api_pricing_sync_provider_models(provider: int):
        """Live model ids a provider serves, for the match-model picker."""
        with odb.new_session() as s:
            prov = s.get(Provider, provider)
        if prov is None:
            raise HTTPException(status_code=404, detail="provider not found")
        try:
            client = LlamaClient(prov.base_url, timeout=8.0)
            try:
                raw = client.models()
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 - surface any client failure
            return {"models": [], "error": str(exc)}
        ids = []
        for e in raw or []:
            if isinstance(e, dict):
                mid = e.get("id") or e.get("name")
                if mid:
                    ids.append(str(mid))
        return {"models": sorted(set(ids))}

    # ------------------------------------------------------- model pricing
    @app.get("/api/settings/models")
    def api_settings_models(provider: int):
        from observatory.models import Model as ModelRow
        with odb.new_session() as s:
            prov = s.get(Provider, provider)
            if prov is None:
                raise HTTPException(status_code=404, detail="provider not found")
            models = s.exec(select(ModelRow).where(
                ModelRow.provider_id == provider).order_by(ModelRow.name)).all()
            return {"models": [
                {"id": m.id, "key": m.key, "name": m.name,
                 "family": m.family, "quant": m.quant,
                 "catalog_available": m.catalog_available,
                 "catalog_last_seen_at": m.catalog_last_seen_at,
                 "input_price_per_million": m.input_price_per_million,
                 "output_price_per_million": m.output_price_per_million,
                 "cache_write_price_per_million": m.cache_write_price_per_million,
                 "cache_read_price_per_million": m.cache_read_price_per_million,
                 "pricing_mode": m.pricing_mode,
                 "pricing_stale": m.pricing_stale,
                 "pricing_source": m.pricing_source,
                 "pricing_litellm_key": m.pricing_litellm_key,
                 "pricing_synced_at": m.pricing_synced_at,
                 "pricing_last_error": m.pricing_last_error}
                for m in models
            ]}

    @app.put("/api/settings/model-pricing")
    async def api_model_pricing_put(request: Request):
        from decimal import Decimal
        from observatory.models import Model as ModelRow
        from observatory.pricing import PricingValidationError, validate_price
        data = await request.json()
        provider_id = data.get("provider_id")
        if not provider_id or not isinstance(provider_id, int):
            raise HTTPException(status_code=422, detail="provider_id is required")
        models_list = data.get("models")
        if not isinstance(models_list, list) or not models_list:
            raise HTTPException(status_code=422, detail="models list is required")
        # input/output are always present from the G05 UI; the two cache fields
        # are only applied when the key is sent, so a partial edit never wipes
        # a rate the form did not show.
        price_fields = ("input_price_per_million", "output_price_per_million",
                        "cache_write_price_per_million",
                        "cache_read_price_per_million")
        with odb.new_session() as s:
            prov = s.get(Provider, provider_id)
            if prov is None:
                raise HTTPException(status_code=404, detail="provider not found")
            seen_ids: set[int] = set()
            updates: list[tuple[int, dict]] = []
            for item in models_list:
                mid = item.get("model_id")
                if not mid or not isinstance(mid, int):
                    raise HTTPException(status_code=422,
                                        detail=f"model_id missing or not int: {mid!r}")
                if mid in seen_ids:
                    raise HTTPException(status_code=422,
                                        detail=f"duplicate model_id in request: {mid}")
                seen_ids.add(mid)
                row = s.get(ModelRow, mid)
                if row is None or row.provider_id != provider_id:
                    raise HTTPException(status_code=422,
                                        detail=f"model_id {mid} not found for provider {provider_id}")
                vals: dict = {}
                try:
                    for f in price_fields:
                        if f in item:
                            vals[f] = validate_price(item.get(f), mid, f)
                except PricingValidationError as exc:
                    raise HTTPException(status_code=422, detail=str(exc))
                updates.append((mid, vals))
            for mid, vals in updates:
                row = s.get(ModelRow, mid)
                for f, v in vals.items():
                    setattr(row, f, v)
                # A hand edit is how a model becomes manually owned. From here
                # the daily sync leaves it alone until the user flips it back.
                row.pricing_mode = "manual"
                row.pricing_stale = False
                row.pricing_last_error = None
            s.commit()
            saved_rows = []
            for mid, _vals in updates:
                row = s.get(ModelRow, mid)
                saved_rows.append({
                    "model_id": row.id,
                    "key": row.key,
                    "input_price_per_million": row.input_price_per_million,
                    "output_price_per_million": row.output_price_per_million,
                    "cache_write_price_per_million": row.cache_write_price_per_million,
                    "cache_read_price_per_million": row.cache_read_price_per_million,
                    "pricing_mode": row.pricing_mode,
                })
        for rk in RANGE_KEYS:
            app.state.snapshots.invalidate((provider_id, rk))
        return {"ok": True, "updated": len(saved_rows), "models": saved_rows}

    @app.put("/api/settings/model-pricing/mode")
    async def api_model_pricing_mode(request: Request):
        from observatory.models import Model as ModelRow
        data = await request.json()
        mid = data.get("model_id")
        mode = data.get("mode")
        if not isinstance(mid, int):
            raise HTTPException(status_code=422, detail="model_id must be an int")
        if mode not in ("manual", "auto"):
            raise HTTPException(status_code=422,
                                detail="mode must be 'manual' or 'auto'")
        with odb.new_session() as s:
            row = s.get(ModelRow, mid)
            if row is None:
                raise HTTPException(status_code=422, detail=f"model {mid} not found")
            row.pricing_mode = mode
            if mode == "auto":
                row.pricing_last_error = None
            else:
                row.pricing_stale = False
            s.commit()
            return {"ok": True, "model_id": mid, "mode": mode}

    @app.on_event("startup")
    async def start_screenshot_cleanup():
        nonlocal live_thread, pricing_thread
        app.state.screenshot_cleanup_task = asyncio.create_task(_screenshot_cleanup_loop())
        app.state.fx_refresh_task = asyncio.create_task(_fx_refresh_loop())
        try:
            await asyncio.to_thread(refresh_overview)
            await asyncio.to_thread(refresh_live_snapshot)
            live_thread = threading.Thread(target=live_snapshot_loop,
                                           name="realtime-snapshot", daemon=True)
            live_thread.start()
            await asyncio.to_thread(_sweep_interrupted_pricing_runs)
            pricing_thread = threading.Thread(target=pricing_sync_loop,
                                              name="pricing-sync", daemon=True)
            pricing_thread.start()
        except RuntimeError:
            # Artifact-only tests create the app without configuring a database.
            pass

    @app.on_event("shutdown")
    def shutdown_collector():
        live_stop.set()
        pricing_stop.set()
        if live_thread is not None:
            live_thread.join(timeout=2.0)
        if pricing_thread is not None:
            # A match call may be mid-flight; give it the collector's grace.
            pricing_thread.join(timeout=5.0)
        cleanup_task = getattr(app.state, "screenshot_cleanup_task", None)
        if cleanup_task is not None:
            cleanup_task.cancel()
        fx_task = getattr(app.state, "fx_refresh_task", None)
        if fx_task is not None:
            fx_task.cancel()
        collector = app.state.collector
        if collector is not None:
            collector.stop()

    return app


def _display_settings(s: Session) -> dict:
    out = {"default_range": "7d", "default_group": "family", "theme": "dark"}
    for k in out:
        row = s.get(Setting, k)
        if row and row.value:
            try:
                out[k] = json.loads(row.value)
            except ValueError:
                pass
    return out


def _write_setting(s: Session, key: str, value) -> None:
    row = s.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        s.add(row)
    row.value = json.dumps(value)


# Hourly wake; the once-per-day gate itself lives in fx.refresh_quote, keyed
# on the persisted fx_fetched_at so restarts cannot turn it into an abuse.
FX_TICK_S = 3600

# Pricing-sync wake cadence. The real throttle is the daily gate + run_time in
# pricing_sync.refresh; this only asks "is it time yet".
PRICING_TICK_S = 300


def _currency_settings(s: Session) -> dict:
    """The canonical secondary-currency block for the API.

    ``enabled`` folds the three-way display guard -- a currency is picked, a
    rate exists, and that rate is for the picked currency -- and is the only
    field the display JS consults. USD stays authoritative; this is display
    metadata, nothing here rewrites a stored price.
    """
    code = None
    row = s.get(Setting, "secondary_currency")
    if row and row.value:
        try:
            code = json.loads(row.value)
        except ValueError:
            code = None
    if code not in fx.SUPPORTED:
        code = None

    block = {"code": code, "symbol": None, "decimals": None, "rate": None,
             "quote_date": None, "fetched_at": None, "source": None,
             "enabled": False}
    if code is None:
        return block

    meta = fx.SUPPORTED[code]
    block["symbol"] = meta["symbol"]
    block["decimals"] = meta["decimals"]

    stored = fx.stored_quote(s)
    if stored and stored["currency"] == code and stored["rate"]:
        block["rate"] = stored["rate"]
        block["quote_date"] = stored["quote_date"]
        block["fetched_at"] = stored["fetched_at"]
        block["source"] = stored["source"]
        block["enabled"] = True
    return block


def _fx_refresh_blocking(*, force: bool = False, timeout: float | None = None):
    """Read the selected currency and refresh its rate. Blocking; run in a
    worker thread so the httpx call never touches the event loop. Returns the
    stored block, or None when nothing was fetched (no currency, or the
    once-per-day gate was closed)."""
    with odb.new_session() as s:
        row = s.get(Setting, "secondary_currency")
        code = None
        if row and row.value:
            try:
                code = json.loads(row.value)
            except ValueError:
                code = None
        if code not in fx.SUPPORTED:
            return None
        return fx.refresh_quote(s, code, force=force, timeout=timeout)


async def _fx_refresh_loop() -> None:
    """One outbound FX request per day in steady state, an immediate catch-up
    after downtime. Makes no network call at all while no currency is selected."""
    while True:
        try:
            await asyncio.to_thread(_fx_refresh_blocking)
        except fx.FxError:
            pass  # keep the last known-good rate; the next tick retries
        except Exception:  # pragma: no cover - defensive
            log.exception("fx refresh loop iteration failed")
        await asyncio.sleep(FX_TICK_S)


def _sweep_interrupted_pricing_runs() -> None:
    """Mark any pricing-sync run left 'running' by a killed process as
    'interrupted', mirroring how the collector terminalizes stale sessions."""
    from observatory.models import PricingSyncRun
    with odb.new_session() as s:
        stale = s.exec(select(PricingSyncRun).where(
            PricingSyncRun.finished_at.is_(None))).all()
        if not stale:
            return
        now = now_ms()
        for row in stale:
            row.result = "interrupted"
            row.finished_at = now
            s.add(row)
        s.commit()


def _unload_provider_models(provider: Provider) -> dict:
    """Unload every currently loaded model for one enabled router provider."""
    result = {"id": provider.id, "name": provider.name, "status": "empty",
              "models": [], "error": None}
    client = LlamaClient(provider.base_url, timeout=5.0)
    try:
        models = client.models()
        loaded = [
            entry.get("id") or entry.get("name") for entry in models
            if isinstance(entry, dict)
            and isinstance(entry.get("status"), dict)
            and entry["status"].get("value") == "loaded"
            and (entry.get("id") or entry.get("name"))
        ]
        for model in loaded:
            client.unload(model)
            result["models"].append(model)
        if loaded:
            result["status"] = "unloaded"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:200] or "unload request failed"
    finally:
        client.close()
    return result


def _provider_out(p: Provider) -> dict:
    return {
        "id": p.id, "name": p.name, "ptype": p.ptype, "base_url": p.base_url,
        "agent_url": p.agent_url, "enabled": p.enabled, "is_default": p.is_default,
        "poll_interval_s": p.poll_interval_s, "notes": p.notes,
        "status": m.effective_provider_status(p, now_ms()),
        "last_success_at": p.last_success_at,
        "latency_ms": p.latency_ms, "last_error": p.last_error,
        "created_at": p.created_at,
    }


def _set_defaults(s: Session, data: dict):
    if data.get("is_default"):
        for p in s.exec(select(Provider)).all():
            p.is_default = False


def _apply_provider(s: Session, p: Provider, data: dict):
    if "name" in data:
        p.name = str(data["name"]).strip() or p.name
    if "ptype" in data:
        p.ptype = str(data["ptype"])
    if "base_url" in data:
        p.base_url = str(data["base_url"]).strip().rstrip("/")
    if "agent_url" in data:
        p.agent_url = (str(data["agent_url"]).strip().rstrip("/")
                       if str(data["agent_url"]).strip() else None)
    if "enabled" in data:
        p.enabled = bool(data["enabled"])
    if "is_default" in data:
        make_default = bool(data["is_default"])
        if make_default:
            for other in s.exec(select(Provider)).all():
                other.is_default = other.id == p.id
        else:
            p.is_default = False
    if "poll_interval_s" in data:
        try:
            p.poll_interval_s = max(0.25, float(data["poll_interval_s"]))
        except (TypeError, ValueError):
            pass
    if "notes" in data:
        p.notes = str(data["notes"])


def _test_provider(p: Provider) -> dict:
    """Passive connection test: read-only endpoints only, no prompts."""
    import httpx
    out = {"name": p.name, "ok": False, "endpoints": {}, "model": None,
           "health_status": None, "latency_ms": None, "error": None}
    base = (p.base_url or "").rstrip("/")
    try:
        t0 = time.time()
        r = httpx.get(base + "/health", timeout=5.0)
        r.raise_for_status()
        out["endpoints"]["health"] = True
        try:
            out["health_status"] = r.json().get("status")
        except ValueError:
            out["health_status"] = "ok"
        model_id = None
        try:
            r4 = httpx.get(base + "/v1/models", timeout=5.0)
            r4.raise_for_status()
            body = r4.json()
            data = body.get("data", []) if isinstance(body, dict) else []
            out["endpoints"]["models"] = bool(data)
            loaded = [x for x in data
                      if ((x.get("status") or {}).get("value") == "loaded")]
            candidate = loaded[0] if loaded else (data[0] if data else None)
            if candidate:
                model_id = candidate.get("id") or candidate.get("name")
                out["model"] = model_id
        except Exception:
            out["endpoints"]["models"] = False
        try:
            params = {"model": model_id} if model_id else None
            r2 = httpx.get(base + "/metrics", params=params, timeout=5.0)
            r2.raise_for_status()
            out["endpoints"]["metrics"] = True
        except Exception:
            out["endpoints"]["metrics"] = False
        if model_id:
            try:
                r5 = httpx.get(base + "/slots", params={"model": model_id}, timeout=5.0)
                r5.raise_for_status()
                out["endpoints"]["slots"] = isinstance(r5.json(), list)
            except Exception:
                out["endpoints"]["slots"] = False
        try:
            r3 = httpx.get(base + "/props", timeout=5.0)
            r3.raise_for_status()
            props = r3.json()
            out["endpoints"]["props"] = True
            mi = props.get("model_info") or {}
            if not out["model"]:
                out["model"] = mi.get("general_name")
        except Exception:
            out["endpoints"]["props"] = False
        out["latency_ms"] = round((time.time() - t0) * 1000, 1)
        out["ok"] = out["endpoints"].get("health") is True
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def main():
    ap = argparse.ArgumentParser(description="Observatory - passive llama.cpp observability")
    ap.add_argument("--demo", action="store_true",
                    help="run with synthetic data (no network calls)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--db", default=None, help="SQLite path override")
    args = ap.parse_args()

    configure_logging()

    db_path = args.db or (DB_PATH_DEMO if args.demo else DB_PATH_DEFAULT)
    app_state.STARTED = time.time()
    app_state.DEMO = args.demo

    odb.init_db(db_path)
    ensure_default_provider()

    make_client = lambda p: LlamaClient(p.base_url)
    make_agent = None
    if args.demo:
        from observatory.demo import get_world
        world = get_world()
        if world.seed_if_empty(odb.get_engine()):
            log.info("demo history seeded into %s", db_path)
        make_client = world.client
        make_agent = None  # demo provider serves fake agent data via client

    app = create_app(demo=args.demo)
    collector = Collector(make_client, make_agent)
    app.state.collector = collector
    collector.start()
    log.info("observatory listening on http://%s:%d (%s)",
             args.host, args.port, "demo" if args.demo else "live")
    import uvicorn
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level="warning",
                            # Safety net: bound the wait well below the unit's
                            # TimeoutStopSec so an unforeseen long-lived
                            # response can never earn a SIGKILL again.
                            timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
