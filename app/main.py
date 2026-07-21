from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import (
    routes_admin,
    routes_agent,
    routes_auth,
    routes_deploy,
    routes_files,
    routes_setup,
    routes_users,
)
from app.config import Settings
from app.steam import SteamBuildIds
from app.storage import Store

STATIC_DIR = Path(__file__).parent / "static"

# Au-dela de ce delai sans rapport agent, l'etat n'est plus digne de confiance
# (cycle agent = 2 min ; 10 min = 5 cycles rates).
HEALTH_STALE_SECONDS = 600


def create_app(settings: Settings, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="Hephaestos")
    app.state.settings = settings
    app.state.store = Store(settings.data_dir / "state.json")
    app.state.store.registry.seed_if_empty(settings.servers)
    app.state.http_client = http_client or httpx.AsyncClient()
    app.state.steam = SteamBuildIds(app.state.http_client)
    app.include_router(routes_admin.router)
    app.include_router(routes_deploy.router)
    app.include_router(routes_files.router)
    app.include_router(routes_agent.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_users.router)
    app.include_router(routes_setup.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    async def health():
        return {"ok": True}

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
