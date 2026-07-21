import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.auth import require_agent
from app.game_updates import auto_enqueue_game_updates
from app.mods import auto_enqueue_mod_updates

router = APIRouter(prefix="/api/agent", dependencies=[Depends(require_agent)])

# Protection croissance state.json : une entree config_servers/discovered_games venant
# de l'agent (moins privilegie) est ignoree si sa serialisation JSON depasse ce seuil.
AGENT_ENTRY_MAX_BYTES = 4096


class OrderResult(BaseModel):
    status: Literal["running", "done", "failed"]
    detail: str | None = None
    # Candidats .exe rapportés par install_game/scan_exe. L'agent est une source non
    # fiable pour l'UI : bornes strictes ici, échappement systématique côté rendu.
    exe_candidates: list[Annotated[str, StringConstraints(min_length=1, max_length=260)]] | None = \
        Field(default=None, max_length=30)
    # Resultats list_files/read_file (Lot 3). Meme principe que exe_candidates :
    # l'agent est une source non fiable pour l'UI, bornes strictes ici.
    files: list[Annotated[str, StringConstraints(min_length=1, max_length=260)]] | None = \
        Field(default=None, max_length=500)
    content_b64: Annotated[str, StringConstraints(max_length=700_000)] | None = None
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


# extra="ignore" (et non "forbid") sur les modeles remplis par l'agent : un agent
# plus recent que ce backend peut envoyer des champs inconnus ; un "forbid" ferait
# rejeter le rapport ENTIER en 422 (tous les serveurs paraitraient morts) jusqu'a
# mise a jour du backend. Les VALEURS des champs connus restent validees strictement.
class PlayerInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    steamid: str | None = None


class SaveBackupInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str
    size_mb: float | None = None
    created: str | None = None


class ServerState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    buildid: str | None = Field(default=None, pattern=r"^\d{1,20}$")
    process_up: bool | None = None
    players: int | None = Field(default=None, ge=0, le=1000)
    players_list: list[PlayerInfo] | None = None
    installed_mod_ids: list[str] | None = None
    save_backups: list[SaveBackupInfo] | None = None
    process_started_at: str | None = None
    rcon_info: str | None = None
    process_cpu_percent: float | None = None
    process_mem_mb: float | None = None


class StateReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    servers: dict[str, ServerState]
    agent_version: str | None = Field(default=None, max_length=20)
    config_hash: str | None = Field(default=None, max_length=64)
    config_servers: list[dict] | None = Field(default=None, max_length=50)
    discovered_games: list[dict] | None = Field(default=None, max_length=50)


@router.get("/orders")
async def get_orders(request: Request):
    await auto_enqueue_mod_updates(request)
    await auto_enqueue_game_updates(request)
    store = request.app.state.store
    orders = await store.pending_orders()  # groom : peut expirer des ordres > 24h
    expired = await store.pop_expired_unnotified()
    if expired:
        from app.notify import send_alert
    for exp in expired:
        await send_alert(request.app,
                         f"⏰ Hephaestos [{exp['server']}] ordre {exp['type']} (par {exp.get('author') or '?'}) "
                         f"expiré sans réponse de l'agent depuis sa création ({exp['created']})")
    meta = await store.get_agent_meta()
    config = await store.registry.agent_config() if meta.get("config_snapshot_seen") else None
    return {"orders": orders, "config": config}


@router.post("/orders/{order_id}")
async def report_order(request: Request, order_id: str, result: OrderResult):
    order = await request.app.state.store.set_order_status(order_id, result.status, result.detail)
    if order is None:
        raise HTTPException(404, "ordre inconnu")
    if order.get("terminal_refused"):
        raise HTTPException(409, "ordre deja en etat terminal")
    registry = request.app.state.store.registry
    # Cycle de vie du déploiement : les transitions de statut du registre suivent
    # les confirmations agent, jamais l'optimisme du backend.
    if order["status"] == "done" and order["type"] in ("install_game", "scan_exe"):
        await registry.update_entry(order["server"], {
            "exe_candidates": list(result.exe_candidates or []),
            "status": "awaiting_setup"})
    if order["status"] == "done" and order["type"] == "setup_server":
        await registry.update_entry(order["server"], {
            "status": "active", "exe_candidates": None})
    if order["status"] == "done" and order["type"] == "list_files":
        current = (await registry.get(order["server"])) or {}
        listing = dict(current.get("files_listing") or {})
        listing[order["root"]] = list(result.files or [])
        await registry.update_entry(order["server"], {"files_listing": listing})
    if order["status"] == "done" and order["type"] == "read_file":
        await registry.update_entry(order["server"], {"file_read": {
            "root": order["root"], "path": order["path"],
            "content_b64": result.content_b64, "sha256": result.sha256,
            # horodatage consomme par purge_stale_file_reads : ce bloc contient un
            # secret en clair (AdminPassword) et ne doit pas survivre au-dela du TTL.
            "read_at": datetime.now(UTC).isoformat()}})
    if order["status"] == "failed":
        # sans ca, un echec (steamcmd auth expiree, restore rate...) reste invisible
        # tant que personne n'ouvre le dashboard
        from app.notify import send_alert
        author = order.get("author") or "?"
        await send_alert(request.app,
                         f"❌ Hephaestos [{order['server']}] ordre {order['type']} (par {author}) échoué : {order.get('detail')}")
    return order


@router.post("/state")
async def report_state(request: Request, report: StateReport):
    known = await request.app.state.store.registry.all()
    # lu AVANT set_agent_meta plus bas : determine si le snapshot a deja ete adopte
    # une premiere fois par le passe.
    already_migrated = (await request.app.state.store.get_agent_meta()).get("config_snapshot_seen")
    ignored = []
    for name, state in report.servers.items():
        if name not in known:
            ignored.append(name)
            continue
        state_dict = state.model_dump(exclude={"players_list", "installed_mod_ids"})
        await request.app.state.store.set_server_state(name, state_dict)
        if state.players_list is not None:
            await request.app.state.store.update_player_sessions(
                name, [p.model_dump() for p in state.players_list]
            )
        if state.installed_mod_ids is not None:
            await request.app.state.store.mods.update_mods_state(name, state.installed_mod_ids)

    meta: dict = {}
    if report.agent_version is not None:
        meta["agent_version"] = report.agent_version
    if report.config_hash is not None:
        meta["config_hash"] = report.config_hash
    if report.discovered_games is not None:
        discovered = [g for g in report.discovered_games if _within_size_bound(g)]
        meta["discovered_games"] = discovered
    if report.config_servers is not None:
        meta["config_snapshot_seen"] = True
        # L'adoption ne sert QUE la migration initiale (Lot 1) : passe ce cap, la config
        # est poussee par le backend et un snapshot stale re-remplirait un champ que
        # l'admin vient d'effacer (boucle stable fausse, le revert n'atteint jamais l'agent).
        if not already_migrated:
            for entry in report.config_servers:
                if not _within_size_bound(entry):
                    continue
                entry_name = entry.get("name")
                if isinstance(entry_name, str) and entry_name in known:
                    await request.app.state.store.registry.adopt_agent_fields(entry_name, entry)
    if meta:
        await request.app.state.store.set_agent_meta(meta)
    return {"ok": True, "ignored": ignored}


def _within_size_bound(entry: dict) -> bool:
    return len(json.dumps(entry).encode()) <= AGENT_ENTRY_MAX_BYTES
