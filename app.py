"""Observatory - passive llama.cpp observability dashboard.

Run:
    python app.py            # real mode (reads from Nautilus by default)
    python app.py --demo     # demo mode (synthetic data, no network)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select

from observatory import app_state, database as odb, metrics as m
from observatory.collector import Collector
from observatory.llama_provider import LlamaClient
from observatory.models import Provider, Setting, now_ms
from observatory.settings import (DB_PATH_DEFAULT, DB_PATH_DEMO, NAUTILUS_AGENT_URL,
                                  NAUTILUS_NAME, NAUTILUS_TYPE, NAUTILUS_URL)

log = logging.getLogger("observatory")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOT_DIR = os.path.join(BASE_DIR, "data", "screenshots")
SCREENSHOT_TTL_S = 24 * 60 * 60


def _screenshot_path(capture_id: str) -> str:
    try:
        normalized = str(UUID(capture_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if normalized != capture_id.lower():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return os.path.join(SCREENSHOT_DIR, normalized + ".png")


def _cleanup_screenshots(now: float | None = None) -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    cutoff = (time.time() if now is None else now) - SCREENSHOT_TTL_S
    for entry in os.scandir(SCREENSHOT_DIR):
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
    """Nautilus exists from first start, in every mode."""
    with odb.new_session() as s:
        p = s.exec(select(Provider).where(Provider.name == NAUTILUS_NAME)).first()
        if p is None:
            p = Provider(name=NAUTILUS_NAME, ptype=NAUTILUS_TYPE, base_url=NAUTILUS_URL,
                         agent_url=NAUTILUS_AGENT_URL, enabled=True, is_default=True,
                         poll_interval_s=1.0, notes="Default llama.cpp server")
            s.add(p)
            s.commit()


def create_app(demo: bool = False) -> FastAPI:
    app = FastAPI(title="LLM-Telemetry", docs_url=None, redoc_url=None)
    app.state.demo = demo
    app.state.started = time.time()
    app.state.collector = None
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    overview_cache = {"data": None, "refreshed": 0.0, "refreshing": False}
    overview_lock = threading.Lock()

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
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
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
            "subtitle": "Compare observed model families across the same time range.",
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
            return {
                "demo": demo,
                "providers": [{"id": p.id, "name": p.name, "status": p.status,
                               "enabled": p.enabled, "is_default": p.is_default}
                              for p in provs],
                "display": disp,
            }

    @app.get("/api/overview")
    def api_overview():
        return cached_overview()

    @app.get("/api/models")
    def api_models(range: str = "7d", group: str = "family",
                   provider: Optional[int] = None):
        with odb.new_session() as s:
            return m.models_page(s, provider, range, group)

    @app.get("/api/models/selected")
    def api_selected(ids: str = "", range: str = "7d", provider: Optional[int] = None):
        ids_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        with odb.new_session() as s:
            return m.selected_stats(s, ids_list, provider, range)

    @app.get("/api/model/{mid}")
    def api_model(mid: int, range: str = "24h"):
        with odb.new_session() as s:
            return m.model_detail(s, mid, range)

    @app.get("/api/sessions")
    def api_sessions(provider: Optional[int] = None, model: Optional[int] = None,
                     quant: Optional[str] = None, mtp: Optional[str] = None,
                     reasoning: Optional[str] = None, range: str = "7d"):
        with odb.new_session() as s:
            return m.sessions_page(s, provider, model, quant, mtp, reasoning, range)

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
    def api_compare_model_candidates(provider: Optional[int] = None,
                                     range: str = "7d"):
        with odb.new_session() as s:
            return {"models": m.compare_model_candidates(s, provider, range),
                    "range": range}

    @app.get("/api/compare/models")
    def api_compare_models(keys: str = "", provider: Optional[int] = None,
                           range: str = "7d"):
        family_keys = [x for x in keys.split("|") if x]
        with odb.new_session() as s:
            return m.compare_models(s, family_keys, provider, range)

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

    @app.get("/api/stream")
    async def api_stream():
        def snapshot():
            with odb.new_session() as s:
                return m.live_snapshot(s)

        async def gen():
            while True:
                try:
                    data = await asyncio.to_thread(snapshot)
                except Exception:
                    data = {"error": "unavailable", "now": now_ms()}
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(2.0)
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
                    row = s.get(Setting, k)
                    if row is None:
                        row = Setting(key=k)
                        s.add(row)
                    row.value = json.dumps(data[k])
            s.commit()
            return {"display": _display_settings(s)}

    @app.on_event("startup")
    async def start_screenshot_cleanup():
        app.state.screenshot_cleanup_task = asyncio.create_task(_screenshot_cleanup_loop())
        try:
            await asyncio.to_thread(refresh_overview)
        except RuntimeError:
            # Artifact-only tests create the app without configuring a database.
            pass

    @app.on_event("shutdown")
    def shutdown_collector():
        cleanup_task = getattr(app.state, "screenshot_cleanup_task", None)
        if cleanup_task is not None:
            cleanup_task.cancel()
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


def _provider_out(p: Provider) -> dict:
    return {
        "id": p.id, "name": p.name, "ptype": p.ptype, "base_url": p.base_url,
        "agent_url": p.agent_url, "enabled": p.enabled, "is_default": p.is_default,
        "poll_interval_s": p.poll_interval_s, "notes": p.notes,
        "status": p.status, "last_success_at": p.last_success_at,
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

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
