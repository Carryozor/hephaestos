"""Mise a jour manuelle des mods BepInEx (Valheim) : la detection est deja
exposee en lecture seule dans GET /api/servers (app/bepinex.py). Ici, l'action
qui declenche un ordre agent reel (arret + remplacement de fichiers +
redemarrage). Reserve aux admins (require_admin_role, PAS require_admin comme
le reste du panel serveur) : contrairement aux mods Workshop, cette action
coupe le serveur et installe du code telecharge depuis Thunderstore/GitHub --
meme decision de perimetre que routes_files.py (secrets/actions sensibles).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import bepinex as bepinex_service
from app.auth import require_admin_role
from app.storage import OrderConflict

router = APIRouter(prefix="/api/servers", dependencies=[Depends(require_admin_role)])


class BepInExUpdateRequest(BaseModel):
    slugs: list[str] | None = None


async def _require_bepinex_server(request: Request, name: str) -> None:
    if await request.app.state.store.registry.get(name) is None:
        raise HTTPException(404, "serveur inconnu")
    if not await request.app.state.store.bepinex.all(name):
        raise HTTPException(404, "aucun mod BepInEx suivi pour ce serveur")


@router.post("/{name}/bepinex/check")
async def check_bepinex_updates(request: Request, name: str):
    await _require_bepinex_server(request, name)
    await bepinex_service.force_refresh_bepinex_versions(request, name)
    return await bepinex_service.build_bepinex_entry_fields(request, name)


@router.post("/{name}/bepinex/update", status_code=201)
async def update_bepinex(request: Request, name: str, body: BepInExUpdateRequest):
    await _require_bepinex_server(request, name)
    try:
        return await bepinex_service.enqueue_bepinex_update_order(request, name, body.slugs)
    except bepinex_service.BepInExUnknownSlug as e:
        raise HTTPException(400, str(e)) from None
    except bepinex_service.BepInExOrderError as e:
        raise HTTPException(409, str(e)) from None
    except OrderConflict:
        raise HTTPException(409, "mise a jour BepInEx deja en attente pour ce serveur") from None
