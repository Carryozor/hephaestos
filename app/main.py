import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import (
    __version__,
    metrics,
    routes_admin,
    routes_agent,
    routes_auth,
    routes_bepinex,
    routes_deploy,
    routes_files,
    routes_setup,
    routes_users,
)
from app.config import Settings
from app.freshness import STATE_STALE_SECONDS
from app.steam import SteamBuildIds
from app.storage import Store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HEALTH_SUCCESS_TTL_SECONDS = 5.0
HEALTH_FAILURE_TTL_SECONDS = 1.0

# Seuil de peremption : constante unique dans app/freshness.py (partagee avec /metrics).
HEALTH_STALE_SECONDS = STATE_STALE_SECONDS

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
}


def create_app(settings: Settings, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    # Pas de documentation interactive ni de schema OpenAPI exposes (surface d'attaque inutile).
    app = FastAPI(title="Hephaestos", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # Pas de script-src/default-src : app.js utilise des onclick inline.
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        return response

    app.state.store = Store(settings.data_dir / "state.json")
    app.state.store.registry.seed_if_empty(settings.servers)
    app.state.store.bepinex.seed_missing(settings.bepinex_mods)
    app.state.http_client = http_client or httpx.AsyncClient()
    app.state.steam = SteamBuildIds(app.state.http_client)
    app.include_router(routes_admin.router)
    app.include_router(routes_deploy.router)
    app.include_router(routes_files.router)
    app.include_router(routes_bepinex.router)
    app.include_router(routes_agent.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_users.router)
    app.include_router(routes_setup.router)
    app.include_router(metrics.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # /api/health est public : sans cache, chaque appel relit et parse tout le state.json
    # sous le verrou global. Succes servi 5 s ; echec 1 s seulement (retour a la
    # normale visible vite). Horloge injectable (tests) via app.state.health_clock.
    app.state.health_clock = time.monotonic
    health_cache: dict = {"until": 0.0, "ok": True}

    @app.get("/api/health")
    async def health():
        """Liveness + lisibilite du state.json : un fichier illisible/corrompu = 503."""
        now = app.state.health_clock()
        if now >= health_cache["until"]:
            try:
                await app.state.store.snapshot()
            except Exception:
                logger.exception("health : lecture du state.json impossible")
                health_cache.update(ok=False, until=now + HEALTH_FAILURE_TTL_SECONDS)
            else:
                health_cache.update(ok=True, until=now + HEALTH_SUCCESS_TTL_SECONDS)
        if not health_cache["ok"]:
            return JSONResponse({"ok": False, "reason": "state.json illisible"}, status_code=503)
        return {"ok": True}

    @app.get("/api/version")
    async def version():
        return {"version": __version__}

    @app.get("/api/public/health/{name}")
    async def public_health(name: str):
        """Sonde Kuma par serveur, sans auth (le port n'est expose que sur le tailnet).
        Reflete l'etat rapporte par l'agent : 200 seulement si le process est up ET
        que le rapport est frais -- un last_seen perime rend le 200 mensonger."""
        if name not in await app.state.store.registry.all():
            raise HTTPException(404, "serveur inconnu")
        snap = await app.state.store.snapshot()
        state = snap["servers"].get(name)
        if state is None:
            return JSONResponse({"ok": False, "reason": "aucun etat rapporte par l'agent"}, status_code=503)
        last_seen = datetime.fromisoformat(state["last_seen"])
        if datetime.now(UTC) - last_seen > timedelta(seconds=HEALTH_STALE_SECONDS):
            return JSONResponse({"ok": False, "reason": f"etat agent stale (last_seen {state['last_seen']})"},
                                status_code=503)
        if not state.get("process_up"):
            return JSONResponse({"ok": False, "reason": "process serveur down"}, status_code=503)
        return {"ok": True, "last_seen": state["last_seen"]}

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
