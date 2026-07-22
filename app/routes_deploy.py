"""Wizard de déploiement (Lot 2 v2) : création d'un serveur au registre + ordres
install_game/scan_exe/setup_server exécutés par l'agent Windows. Admin uniquement.

Version-skew : ces types d'ordres n'existent que depuis l'agent 2.1.0 — un agent
plus ancien les verrait comme "type d'ordre inconnu" (failed systématique). Gate
explicite : 409 tant qu'un agent_version compatible n'a pas été vu dans un rapport
d'état (déploiement backend-d'abord sans fenêtre de casse).

Install SANS +force_install_dir (incident 14-15/07, cf. Update-GameServer) : le jeu
va dans la bibliothèque steamcmd par défaut, le dossier réel est résolu par manifest
côté agent. Le payload install_game ne porte donc que {appid}.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin_role
from app.known_servers import search_known
from app.routes_admin import RconUpdate

router = APIRouter(prefix="/api/deploy", dependencies=[Depends(require_admin_role)])

AGENT_MIN_VERSION = (2, 1, 0)
SLUG_PATTERN = r"^[a-z0-9-]{1,32}$"


def version_tuple(raw: str) -> tuple[int, ...] | None:
    """"2.1.0" -> (2, 1, 0) ; None si le format ne ressemble pas à une version."""
    if not re.fullmatch(r"\d+(\.\d+){0,3}", raw or ""):
        return None
    return tuple(int(p) for p in raw.split("."))


async def _require_capable_agent(request: Request) -> None:
    meta = await request.app.state.store.get_agent_meta()
    seen = meta.get("agent_version") or ""
    vt = version_tuple(seen)
    if vt is None or vt < AGENT_MIN_VERSION:
        required = ".".join(str(p) for p in AGENT_MIN_VERSION)
        raise HTTPException(409, f"agent trop ancien pour le déploiement "
                                 f"(requis >= {required}, vu : {seen or 'aucun'})")


@router.get("/appinfo/{appid}")
async def appinfo(request: Request, appid: int):
    """Pré-remplissage du wizard : nom public de l'appid, None si inconnu (non bloquant)."""
    return {"name": await request.app.state.steam.public_app_name(appid)}


@router.get("/search")
async def search_deploy_candidates(request: Request, q: str):
    """Recherche de jeu par nom : liste locale (serveurs dedies connus, installables
    en anonyme) en tete, repli boutique Steam pour le reste (jeux de base/DLC -- ne
    trouve jamais les appids d'outils serveur dedie, verifie le 19/07)."""
    if len(q) < 2:
        raise HTTPException(400, "recherche trop courte (2 caracteres minimum)")
    known = search_known(q)
    known_appids = {e["appid"] for e in known}
    steam_results = await request.app.state.steam.search_apps(q)
    # Pas de troncature globale : la liste locale (curee, <=67 entrees) est toujours
    # pertinente en entier -- un terme large ("server") correspond a la quasi-totalite
    # d'entre elles (toutes nommees "... Dedicated Server"). Seul le repli boutique
    # Steam est deja borne (search_apps limite lui-meme a 20).
    merged = known + [r for r in steam_results if r["appid"] not in known_appids]
    return {"results": merged}


@router.get("/details")
async def deploy_candidate_details(request: Request, appid: int, name: str):
    """Image + description pour la colonne de detail du wizard. Repli heuristique sur
    le jeu de base si l'appid est un outil serveur dedie (jamais de page boutique
    propre) : on retire "dedicated server" du nom, on cherche, on prend le 1er resultat."""
    steam = request.app.state.steam
    details = await steam.app_details(appid)
    if details:
        return {**details, "is_proxy": False}
    stripped = re.sub(r"(?i)\s*dedicated server\s*$", "", name).strip()
    if stripped and stripped.lower() != name.lower():
        candidates = await steam.search_apps(stripped)
        if candidates:
            base_details = await steam.app_details(candidates[0]["appid"])
            if base_details:
                return {**base_details, "is_proxy": True}
    return {"header_image": None, "description": None, "is_proxy": False}


class DeployRequest(BaseModel):
    name: str = Field(pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=60)
    server_appid: int = Field(ge=1, le=99999999)


@router.post("/servers", status_code=201)
async def deploy_server(request: Request, body: DeployRequest):
    await _require_capable_agent(request)
    store = request.app.state.store
    entry = await store.registry.get(body.name)
    if entry is not None:
        # Ré-essai : une install échouée laisse l'entrée "installing" sans ordre
        # pendant — re-poster le MÊME couple nom/appid relance juste l'ordre.
        if entry.get("status") != "installing" or entry.get("server_appid") != body.server_appid:
            raise HTTPException(409, "nom déjà utilisé au registre")
    else:
        try:
            entry = await store.registry.create_entry(
                body.name, body.display_name, body.server_appid, "installing")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if any(o["server"] == body.name and o["type"] == "install_game"
           for o in await store.pending_orders()):
        raise HTTPException(409, "installation déjà en attente")
    order = await store.add_order(body.name, "install_game", {"appid": body.server_appid},
                                  author=request.state.user["username"])
    return {"server": {"name": body.name, **entry}, "order": order}


class SetupRequest(BaseModel):
    exe_path: str = Field(min_length=1, max_length=260)
    launch_args: str = Field(default="", max_length=500)
    stop_adapter: str = Field(default="generic-graceful",
                              pattern="^(generic-graceful|generic-force|rcon-generic)$")
    rcon: RconUpdate | None = None
    query_port: int | None = Field(default=None, ge=1, le=65535)
    save_dir: str | None = Field(default=None, max_length=260)
    stop_warn_seconds: int | None = Field(default=None, ge=0, le=600)
    start_now: bool = False


class AdoptRequest(BaseModel):
    appid: int = Field(ge=1, le=99999999)
    name: str = Field(pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=60)


@router.post("/adopt", status_code=201)
async def adopt_discovered(request: Request, body: AdoptRequest):
    """Adoption d'un jeu déjà installé (découvert par l'agent) : pas d'install,
    directement le scan des exe puis le même écran de finalisation que le wizard."""
    await _require_capable_agent(request)
    store = request.app.state.store
    meta = await store.get_agent_meta()
    discovered = {g.get("appid") for g in meta.get("discovered_games", [])}
    if body.appid not in discovered:
        raise HTTPException(404, "appid non signalé par l'agent (jeux détectés uniquement)")
    try:
        await store.registry.create_entry(body.name, body.display_name, body.appid,
                                          "awaiting_setup")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await store.add_order(body.name, "scan_exe", {"appid": body.appid},
                                 author=request.state.user["username"])


@router.post("/servers/{name}/setup", status_code=201)
async def setup_server(request: Request, name: str, body: SetupRequest):
    await _require_capable_agent(request)
    store = request.app.state.store
    entry = await store.registry.get(name)
    if entry is None:
        raise HTTPException(404, "serveur inconnu")
    if entry.get("status") != "awaiting_setup":
        raise HTTPException(409, f"serveur pas en attente de finalisation "
                                 f"(statut : {entry.get('status')})")
    # Jamais de chemin arbitraire dans un ordre exécuté sur la machine Windows :
    # seuls les candidats scannés par l'agent lui-même sont acceptés (l'agent
    # re-vérifie en plus l'anti-traversal de son côté — défense en profondeur).
    if body.exe_path not in (entry.get("exe_candidates") or []):
        raise HTTPException(400, "exe_path absent des candidats scannés par l'agent")
    if body.stop_adapter == "rcon-generic" and (body.rcon is None or not body.rcon.password):
        raise HTTPException(400, "rcon-generic exige un bloc rcon avec password")
    if any(o["server"] == name and o["type"] == "setup_server"
           for o in await store.pending_orders()):
        raise HTTPException(409, "finalisation déjà en attente")

    # Nom de process = basename de l'exe sans extension : meilleur défaut générique,
    # corrigeable ensuite via l'éditeur de registre (cas type Palworld où le process
    # supervisé est un enfant *-Shipping-Cmd).
    process = body.exe_path.replace("/", "\\").rsplit("\\", 1)[-1]
    if process.lower().endswith(".exe"):
        process = process[:-4]

    fields: dict = {"process": process, "start_task": name, "exe_path": body.exe_path,
              "launch_args": body.launch_args or None, "stop_adapter": body.stop_adapter,
              "query_port": body.query_port, "save_dir": body.save_dir,
              "stop_warn_seconds": body.stop_warn_seconds}
    if body.rcon is not None:
        fields["rcon"] = {k: v for k, v in body.rcon.model_dump().items() if v is not None}
    await store.registry.update_entry(name, fields)

    return await store.add_order(name, "setup_server", {
        "appid": entry["server_appid"], "exe_path": body.exe_path,
        "launch_args": body.launch_args or "", "task_name": name,
        "process": process, "start_now": body.start_now,
    }, author=request.state.user["username"])
