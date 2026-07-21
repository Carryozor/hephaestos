from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import mods
from app.auth import require_admin, require_admin_role
from app.steam_workshop import (
    WorkshopFetchError,
    WorkshopInvalidReference,
    WorkshopItemNotFound,
    WorkshopWrongGame,
    get_workshop_item,
    search_workshop_items,
)

router = APIRouter(prefix="/api/servers", dependencies=[Depends(require_admin)])


@router.get("")
async def list_servers(request: Request):
    store, steam = request.app.state.store, request.app.state.steam
    user = request.state.user
    snap = await store.snapshot()
    pending = await store.pending_orders()
    servers = []
    for name, cfg in (await store.registry.all()).items():
        # compte scope : ne voit que ses serveurs assignes
        if user["role"] != "admin" and name not in user["servers"]:
            continue
        public = await steam.public_buildid(cfg["server_appid"])
        state = snap["servers"].get(name)
        local = (state or {}).get("buildid")
        update_available = (local != public) if (local and public) else None
        entry = {
            "name": name, "display_name": cfg["display_name"], "server_appid": cfg["server_appid"],
            "status": cfg.get("status", "active"),
            "public_buildid": public, "state": state,
            "update_available": update_available,
            "pending_orders": [o["type"] for o in pending if o["server"] == name],
            # La file est GLOBALE (l'agent traite un seul ordre par cycle de 2 min,
            # tous serveurs confondus) : position = rang dans la file complete.
            "order_queue": [
                {"id": o["id"], "type": o["type"], "status": o["status"],
                 "position": i + 1, "total": len(pending)}
                for i, o in enumerate(pending) if o["server"] == name
            ],
            # MAJ dispo mais nombre de joueurs inconnu de l'agent : la condition
            # "0 joueur" de l'auto-update ne peut jamais etre verifiee -> la MAJ
            # auto n'aura jamais lieu, seul le bouton manuel fonctionne.
            "auto_update_blocked": bool(update_available) and (state or {}).get("players") is None,
        }
        if user["role"] == "admin":
            # qui a lance le process courant (dernier start/restart/update abouti)
            entry["started_by"] = snap.get("servers_started_by", {}).get(name)
        if "workshop_appid" in cfg:
            entry["workshop_appid"] = cfg["workshop_appid"]
            entry.update(await mods.build_mods_entry_fields(request, name, cfg, state))
        servers.append(entry)
    result = {"servers": servers}
    if user["role"] == "admin":
        # Jeux installés sur la machine Windows mais absents du registre (rapport-seul
        # de l'agent) : matière de la section « Jeux détectés non gérés » + version
        # agent pour l'UI (bouton déployer grisé si agent trop ancien).
        meta = await store.get_agent_meta()
        result["discovered_games"] = meta.get("discovered_games", [])
        result["agent_version"] = meta.get("agent_version")
    return result


async def _create_order(request: Request, name: str, type_: str):
    store = request.app.state.store
    if name not in await store.registry.all():
        raise HTTPException(404, "serveur inconnu")
    if any(o["server"] == name and o["type"] == type_ for o in await store.pending_orders()):
        raise HTTPException(409, f"ordre {type_} deja en attente")
    return await store.add_order(name, type_, author=request.state.user["username"])


@router.post("/{name}/update", status_code=201)
async def order_update(request: Request, name: str):
    return await _create_order(request, name, "update")


@router.post("/{name}/restart", status_code=201)
async def order_restart(request: Request, name: str):
    return await _create_order(request, name, "restart")


@router.post("/{name}/start", status_code=201)
async def order_start(request: Request, name: str):
    return await _create_order(request, name, "start")


@router.post("/{name}/stop", status_code=201)
async def order_stop(request: Request, name: str):
    return await _create_order(request, name, "stop")


@router.delete("/{name}/orders/{order_id}")
async def cancel_order(request: Request, name: str, order_id: str):
    store = request.app.state.store
    if name not in await store.registry.all():
        raise HTTPException(404, "serveur inconnu")
    pending = await store.pending_orders()
    if not any(o["id"] == order_id and o["server"] == name for o in pending):
        raise HTTPException(404, "ordre inconnu pour ce serveur")
    cancelled = await store.cancel_order(order_id)
    if cancelled is None:
        raise HTTPException(404, "ordre inconnu")
    if cancelled.get("cancel_refused"):
        raise HTTPException(409, "ordre deja en cours d'execution, annulation impossible")
    if cancelled["type"] == "install_mod" and cancelled.get("workshop_id"):
        await store.mods.remove_mod_metadata_if_never_installed(name, cancelled["workshop_id"])
    return cancelled


@router.get("/{name}/players")
async def get_players(request: Request, name: str):
    store = request.app.state.store
    if name not in await store.registry.all():
        raise HTTPException(404, "serveur inconnu")
    sessions = await store.get_player_sessions(name)
    now = datetime.now(UTC)
    players = []
    for pid, info in sessions.items():
        first_seen = datetime.fromisoformat(info["first_seen"])
        players.append({
            "id": pid,
            "name": info["name"],
            "steamid": info.get("steamid"),
            "connected_since_seconds": int((now - first_seen).total_seconds()),
        })
    return {"players": players}


@router.get("/{name}/detail")
async def get_server_detail(request: Request, name: str):
    store = request.app.state.store
    if name not in await store.registry.all():
        raise HTTPException(404, "serveur inconnu")

    snap = await store.snapshot()
    state = snap["servers"].get(name) or {}
    reg = await store.registry.get(name) or {}

    uptime_seconds = None
    started = state.get("process_started_at")
    if started and state.get("process_up"):
        started_dt = datetime.fromisoformat(started)
        uptime_seconds = int((datetime.now(UTC) - started_dt).total_seconds())

    sessions = await store.get_player_sessions(name)
    now = datetime.now(UTC)
    players = []
    for pid, info in sessions.items():
        first_seen = datetime.fromisoformat(info["first_seen"])
        players.append({
            "id": pid,
            "name": info["name"],
            "steamid": info.get("steamid"),
            "connected_since_seconds": int((now - first_seen).total_seconds()),
        })

    totals = await store.get_playtime_totals(name)
    playtime_totals = sorted(
        (
            {"player_key": k, "name": v["name"], "total_seconds": v["total_seconds"]}
            for k, v in totals.items()
        ),
        key=lambda e: e["total_seconds"],
        reverse=True,
    )

    return {
        "rcon_info": state.get("rcon_info"),
        "uptime_seconds": uptime_seconds,
        "process": {
            "cpu_percent": state.get("process_cpu_percent"),
            "mem_mb": state.get("process_mem_mb"),
        },
        "players": players,
        "playtime_totals": playtime_totals,
        "connection_log": await store.get_connection_log(name),
        "save_backups": state.get("save_backups") or [],
        # champs explicites (jamais l'ordre brut : il peut gagner des cles internes)
        "order_history": [
            {"type": o["type"], "status": o["status"], "author": o.get("author"),
             "created": o["created"], "detail": o.get("detail"), "title": o.get("title")}
            for o in await store.order_history(name)
        ],
        "files_listing": reg.get("files_listing") or {},
        "file_read": reg.get("file_read"),
    }


class RestoreSaveRequest(BaseModel):
    # nom de fichier STRICT (pas de separateur de chemin) : il part dans un ordre
    # execute par l'agent qui manipulera ce fichier sur disque
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.zip$")


@router.post("/{name}/saves/backup", status_code=201)
async def backup_save(request: Request, name: str):
    store = request.app.state.store
    if name not in await store.registry.all():
        raise HTTPException(404, "serveur inconnu")
    if any(o["server"] == name and o["type"] == "backup" for o in await store.pending_orders()):
        raise HTTPException(409, "backup deja en attente")
    return await store.add_order(name, "backup", author=request.state.user["username"])


@router.post("/{name}/saves/restore", status_code=201)
async def restore_save(request: Request, name: str, body: RestoreSaveRequest):
    store = request.app.state.store
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "restauration reservee aux administrateurs")
    if name not in await store.registry.all():
        raise HTTPException(404, "serveur inconnu")
    # seul un backup effectivement rapporte par l'agent est restaurable : pas
    # d'ordre aveugle vers un fichier arbitraire
    snap = await store.snapshot()
    reported = {b.get("file") for b in (snap["servers"].get(name) or {}).get("save_backups") or []}
    if body.file not in reported:
        raise HTTPException(404, "backup inconnu de l'agent")
    if any(o["server"] == name and o["type"] == "restore_save" for o in await store.pending_orders()):
        raise HTTPException(409, "restauration deja en attente")
    return await store.add_order(name, "restore_save", {"backup_file": body.file},
                                 author=request.state.user["username"])


class ModPreviewRequest(BaseModel):
    workshop_ref: str


class ModInstallRequest(BaseModel):
    workshop_id: str
    title: str
    thumbnail_url: str


async def _require_workshop_server(request: Request, name: str) -> dict:
    servers = await request.app.state.store.registry.all()
    cfg = servers.get(name)
    if cfg is None or "workshop_appid" not in cfg:
        raise HTTPException(404, "mods non disponibles pour ce serveur")
    # ne retourner que les champs necessaires (pas le rcon.password ajoute plus tard
    # au registre) : ne pas exposer d'attributs internes ajoutes hors de la config seed
    return {"display_name": cfg["display_name"], "server_appid": cfg["server_appid"],
            "workshop_appid": cfg["workshop_appid"]}


async def _fetch_workshop_item(request: Request, workshop_appid: int, ref: str) -> dict:
    try:
        return await get_workshop_item(request.app.state.http_client, ref, workshop_appid)
    except WorkshopInvalidReference:
        raise HTTPException(400, "reference Workshop invalide") from None
    except WorkshopItemNotFound:
        raise HTTPException(404, "mod introuvable ou supprime") from None
    except WorkshopWrongGame:
        raise HTTPException(400, "ce mod n'est pas pour ce jeu") from None
    except WorkshopFetchError:
        raise HTTPException(502, "erreur reseau vers Steam") from None


@router.post("/{name}/mods/preview")
async def preview_mod(request: Request, name: str, body: ModPreviewRequest):
    cfg = await _require_workshop_server(request, name)
    return await _fetch_workshop_item(request, cfg["workshop_appid"], body.workshop_ref)


@router.post("/{name}/mods", status_code=201)
async def install_mod(request: Request, name: str, body: ModInstallRequest):
    cfg = await _require_workshop_server(request, name)
    store = request.app.state.store
    # Revalidation serveur -- ne jamais faire confiance au payload client meme si
    # /mods/preview a deja ete appele juste avant.
    info = await _fetch_workshop_item(request, cfg["workshop_appid"], body.workshop_id)

    if any(o["server"] == name and o["type"] == "install_mod" and o.get("workshop_id") == info["workshop_id"]
           for o in await store.pending_orders()):
        raise HTTPException(409, "installation deja en attente pour ce mod")

    await store.mods.set_mod_metadata(name, info["workshop_id"], info["title"], info["thumbnail_url"],
                                 steam_updated_at=mods.epoch_to_iso(info["time_updated"]))
    return await store.add_order(name, "install_mod", {
        "workshop_id": info["workshop_id"], "title": info["title"], "thumbnail_url": info["thumbnail_url"],
    }, author=request.state.user["username"])


@router.post("/{name}/mods/update-all")
async def update_all_mods(request: Request, name: str):
    await _require_workshop_server(request, name)
    return await mods.enqueue_mod_update_orders(request, name, include_unknown_dates=True,
                                                author=request.state.user["username"])


@router.delete("/{name}/mods/{workshop_id}", status_code=201)
async def remove_mod(request: Request, name: str, workshop_id: str):
    await _require_workshop_server(request, name)
    store = request.app.state.store

    if any(o["server"] == name and o["type"] == "remove_mod" and o.get("workshop_id") == workshop_id
           for o in await store.pending_orders()):
        raise HTTPException(409, "suppression deja en attente pour ce mod")

    await store.mods.remove_mod_metadata_if_never_installed(name, workshop_id)
    return await store.add_order(name, "remove_mod", {"workshop_id": workshop_id},
                                 author=request.state.user["username"])


class RconUpdate(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    password: str | None = Field(default=None, max_length=128)
    shutdown_command: str | None = Field(default=None, max_length=200)
    announce_command: str | None = Field(default=None, max_length=200)


class RegistryUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=60)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    process: str | None = Field(default=None, max_length=200)
    start_task: str | None = Field(default=None, max_length=100)
    launch_args: str | None = Field(default=None, max_length=500)
    stop_adapter: str | None = Field(default=None,
                                     pattern="^(palworld-rcon|generic-graceful|generic-force|rcon-generic)$")
    rcon: RconUpdate | None = None
    query_port: int | None = Field(default=None, ge=1, le=65535)
    save_dir: str | None = Field(default=None, max_length=260)
    stop_warn_seconds: int | None = Field(default=None, ge=0, le=600)
    kuma_maj_push: str | None = Field(default=None, max_length=300)


def _masked_registry_entry(entry: dict) -> dict:
    out = dict(entry)
    rcon = out.get("rcon")
    if isinstance(rcon, dict):
        rcon = dict(rcon)
        rcon["password_set"] = bool(rcon.pop("password", None))
        out["rcon"] = rcon
    return out


@router.get("/{name}/registry", dependencies=[Depends(require_admin_role)])
async def get_registry_entry(request: Request, name: str):
    entry = await request.app.state.store.registry.get(name)
    if entry is None:
        raise HTTPException(404, "serveur inconnu")
    return _masked_registry_entry(entry)


@router.put("/{name}/registry", dependencies=[Depends(require_admin_role)])
async def put_registry_entry(request: Request, name: str, body: RegistryUpdate):
    """Semantique du champ rcon : absent du body = inchange ; rcon=null explicite =
    no-op (jamais d'effacement implicite, cf. commentaire ci-dessous) ; bloc rcon
    fourni = remplace tout le sous-dict SAUF password omis (conserve l'existant)."""
    registry = request.app.state.store.registry
    current = await registry.get(name)
    if current is None:
        raise HTTPException(404, "serveur inconnu")
    fields = body.model_dump(exclude_unset=True)
    if "rcon" in fields and fields["rcon"] is None:
        # rcon: null explicite = no-op : effacer une config rcon se fera par un
        # champ dedie si le besoin arrive, jamais par null implicite (sinon perte
        # silencieuse du password existant sous 200 OK).
        del fields["rcon"]
    elif "rcon" in fields and fields["rcon"] is not None:
        # password write-only : absent du PUT = on conserve l'existant
        if fields["rcon"].get("password") is None:
            existing = (current.get("rcon") or {}).get("password")
            if existing:
                fields["rcon"]["password"] = existing
        fields["rcon"] = {k: v for k, v in fields["rcon"].items() if v is not None}
    entry = await registry.update_entry(name, fields)
    return _masked_registry_entry(entry)


@router.get("/{name}/mods/search")
async def search_mods(request: Request, name: str, q: str | None = None, sort: str = "trend", page: int = 1):
    cfg = await _require_workshop_server(request, name)
    settings = request.app.state.settings
    if not settings.steam_api_key:
        raise HTTPException(503, "recherche Workshop non configuree (cle API absente)")
    try:
        results = await search_workshop_items(
            request.app.state.http_client, settings.steam_api_key,
            cfg["workshop_appid"], q, sort, page,
        )
    except WorkshopFetchError:
        raise HTTPException(502, "erreur reseau vers Steam") from None
    except ValueError:
        raise HTTPException(400, "parametre de tri invalide") from None
    return {"results": results}
