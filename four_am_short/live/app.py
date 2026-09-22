"""Local dashboard for the independent 4am short strategy.

The dashboard only reads state on page load. Starting entries, running a backtest,
and covering positions are explicit, authenticated same-origin controls.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

STATIC_DIR = Path(__file__).with_name("static")
PROJECT_DIR = Path(__file__).resolve().parents[2]
COVER_CONFIRMATION = "COVER 4AM SHORT"
LOGGER = logging.getLogger(__name__)


class CoverRequest(BaseModel):
    confirmation: str = ""


def create_app(settings: Any = None, engine: Any = None) -> FastAPI:
    """Create the dashboard; ``engine`` injection supports offline verification."""
    if engine is None:
        from .config import load_live_settings
        from .engine import LiveEngine

        settings = settings or load_live_settings(PROJECT_DIR / "live_4am_short.json")
        engine = LiveEngine(settings)

    control_token = secrets.token_urlsafe(32)
    control_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.open()
        try:
            yield
        finally:
            await engine.close()

    app = FastAPI(title="4am short", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine = engine
    @app.middleware("http")
    async def protect_local_session(request: Request, call_next):
        # Parse bracketed IPv6 correctly (some TrustedHostMiddleware versions
        # split Host at its first colon). Exact literal loopback names prevent
        # attacker-controlled DNS names rebinding to the trading service.
        try:
            hosts = request.headers.getlist("host")
            parsed_host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else None
            valid_host = bool(
                parsed_host and parsed_host.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed_host.username is None and parsed_host.password is None
                and not parsed_host.path and not parsed_host.query and not parsed_host.fragment
            )
            if parsed_host:
                parsed_host.port  # Reject malformed/non-numeric ports.
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse({"detail": "A loopback Host is required."}, status_code=400)
        if request.url.path.startswith("/api/"):
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    expected = urlsplit(str(request.base_url))
                    same_origin = (
                        (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)
                        and not parsed.path and not parsed.query and not parsed.fragment
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return JSONResponse({"detail": "A same-origin request is required."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    def authenticate(request: Request) -> None:
        supplied = request.headers.get("x-control-token", "")
        if not supplied or not secrets.compare_digest(supplied.encode("utf-8"), control_token.encode("utf-8")):
            raise HTTPException(status_code=403, detail="Refresh the dashboard to authorize this control.")

    async def control(request: Request, action: str) -> dict:
        authenticate(request)
        async with control_lock:
            try:
                await getattr(engine, action)()
            except (ValueError, RuntimeError) as exc:
                # Configuration/transport exception text can contain endpoints or
                # secrets. Detailed operational state belongs to the engine's
                # intentionally public, redacted event stream.
                LOGGER.warning("4am short control %s declined (%s)", action, type(exc).__name__)
                raise HTTPException(
                    status_code=409,
                    detail="Action unavailable. Review the connection status and activity log.",
                ) from None
            except Exception as exc:
                LOGGER.error("4am short control %s failed (%s)", action, type(exc).__name__)
                raise HTTPException(
                    status_code=503,
                    detail="The action could not complete. Review the connection status and activity log.",
                ) from None
        return engine.snapshot()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/session")
    async def session():
        return {"control_token": control_token}

    @app.get("/api/state")
    async def state():
        return engine.snapshot()

    @app.post("/api/start")
    async def start(request: Request):
        return await control(request, "start")

    @app.post("/api/stop")
    async def stop(request: Request):
        return await control(request, "stop_entries")

    @app.post("/api/cover")
    async def cover(request: Request, body: CoverRequest):
        authenticate(request)
        if body.confirmation != COVER_CONFIRMATION:
            raise HTTPException(status_code=400, detail=f"Type {COVER_CONFIRMATION} to confirm.")
        return await control(request, "cover_all")

    @app.post("/api/backtest")
    async def backtest(request: Request):
        return await control(request, "start_backtest")

    return app
