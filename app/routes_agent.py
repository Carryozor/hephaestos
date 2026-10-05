import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from app.auth import require_agent
from app.bepinex import refresh_all_bepinex_versions
from app.crash_recovery import auto_enqueue_crash_restarts
from app.game_updates import auto_enqueue_game_updates
from app.mods import auto_enqueue_mod_updates
from app.order_effects import build_done_effect

router = APIRouter(prefix="/api/agent", dependencies=[Depends(require_agent)])

# Protection croissance state.json : une entree config_servers/discovered_games venant
# de l'agent (moins privilegie) est ignoree si sa serialisation JSON depasse ce seuil.
AGENT_ENTRY_MAX_BYTES = 4096

# Long-poll GET /orders : plafond dur du temps qu'on garde une requete agent ouverte
# (l'agent boucle des long-polls plus courts, cf. hephaestos-agent.ps1) et pas de
# scrutation de la file pendant l'attente. LONG_POLL_INTERVAL_SECONDS est surcharge a
# une valeur minuscule par les tests pour ne pas attendre en temps reel.
LONG_POLL_MAX_WAIT_SECONDS = 50
LONG_POLL_INTERVAL_SECONDS = 1.0


# Bornes LARGES des rapports d'etat : au-dela, on TRONQUE (jamais de 422). Un rejet
# ferait echouer tout le POST /state, l'agent paraitrait mort (last_seen fige, sonde
# Kuma 503, auto-enqueue gele) alors qu'il fonctionne -- le rapport demesure est un
# probleme de volume, pas une raison de perdre la totalite de l'etat.
MAX_TEXT_CHARS = 500
MAX_PLAYERS_LISTED = 1000
MAX_BACKUPS = 500
MAX_LIST_ITEMS = 2000
MAX_MOD_ID_CHARS = 64
MAX_AGENT_ENTRIES = 50


def _cut_text(limit: int = MAX_TEXT_CHARS):
    def cut(value):
        return value[:limit] if isinstance(value, str) else value
    return cut


def _cut_list(limit: int):
    def cut(value):
        return value[:limit] if isinstance(value, list) else value
    return cut


def _cut_mod_ids(value):
    """Ids de mods : tronque la LISTE ; un id demesure est ecarte (un id coupe serait
    un autre id, pire qu'un id absent)."""
    if not isinstance(value, list):
        return value
    return [v for v in value if not isinstance(v, str) or len(v) <= MAX_MOD_ID_CHARS][:MAX_LIST_ITEMS]


# Chaine rapportee par l'agent : tronquee avant validation de type.
BoundedText = Annotated[str, BeforeValidator(_cut_text())]


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
    # Resultat update_bepinex_mods : l'agent est une source non fiable (P5/P6),
    # bornes strictes + revalidation des chemins par BepInExRepository.set_installed
    # (jamais confiance a une seule couche pour un chemin ensuite exploitable en
    # suppression).
    bepinex_installed: list[dict] | None = Field(default=None, max_length=20)


# extra="ignore" (et non "forbid") sur les modeles remplis par l'agent : un agent
# plus recent que ce backend peut envoyer des champs inconnus ; un "forbid" ferait
# rejeter le rapport ENTIER en 422 (tous les serveurs paraitraient morts) jusqu'a
# mise a jour du backend. Les VALEURS des champs connus restent validees strictement.
class PlayerInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: BoundedText
    name: BoundedText
    steamid: BoundedText | None = None


class SaveBackupInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: BoundedText
    size_mb: float | None = None
    created: BoundedText | None = None


class ServerState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    buildid: str | None = Field(default=None, pattern=r"^\d{1,20}$")
    process_up: bool | None = None
    players: int | None = Field(default=None, ge=0, le=1000)
    players_list: Annotated[list[PlayerInfo], BeforeValidator(_cut_list(MAX_PLAYERS_LISTED))] | None = None
    installed_mod_ids: Annotated[list[BoundedText], BeforeValidator(_cut_mod_ids)] | None = None
    save_backups: Annotated[list[SaveBackupInfo], BeforeValidator(_cut_list(MAX_BACKUPS))] | None = None
    process_started_at: BoundedText | None = None
    rcon_info: BoundedText | None = None
    process_cpu_percent: float | None = Field(default=None, ge=0)
    process_mem_mb: float | None = Field(default=None, ge=0)


class StateReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    servers: dict[str, ServerState]
    # anciennes bornes (20/64) conservees, mais par troncature et non par rejet
    agent_version: Annotated[str, BeforeValidator(_cut_text(20))] | None = None
    config_hash: Annotated[str, BeforeValidator(_cut_text(64))] | None = None
    config_servers: Annotated[list[dict], BeforeValidator(_cut_list(MAX_AGENT_ENTRIES))] | None = None
    discovered_games: Annotated[list[dict], BeforeValidator(_cut_list(MAX_AGENT_ENTRIES))] | None = None


@router.get("/orders")
async def get_orders(request: Request, wait: int = 0):
    await auto_enqueue_mod_updates(request)
    await auto_enqueue_game_updates(request)
    await auto_enqueue_crash_restarts(request)
    await refresh_all_bepinex_versions(request)
    store = request.app.state.store
    orders = await store.pending_orders()  # groom : peut expirer des ordres > 24h

    # Long-poll (activation reactive) : si l'agent a passe ?wait=<sec> et que la file
    # est vide, on garde la requete ouverte jusqu'a `wait` secondes (bornees), en
    # re-verifiant la file toutes les LONG_POLL_INTERVAL_SECONDS -- un ordre cree par un
    # clic admin est ainsi pris en ~1s au lieu d'attendre le prochain tick agent (2 min).
    # L'attente est un simple `await sleep` : AUCUN verrou tenu, l'event loop reste libre
    # pour le dashboard et les autres requetes. Les auto-enqueue ne sont evalues qu'une
    # fois (au debut) -- un ordre AUTO apparu pendant l'attente sera pris au tick suivant,
    # seule la reactivite des ordres MANUELS motive cette feature.
    wait = max(0, min(wait, LONG_POLL_MAX_WAIT_SECONDS))
    if not orders and wait > 0:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait
        while not orders and loop.time() < deadline:
            await asyncio.sleep(min(LONG_POLL_INTERVAL_SECONDS, max(0.0, deadline - loop.time())))
            orders = await store.pending_orders()

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
    store = request.app.state.store
    # statut + effet de bord registre persistes ATOMIQUEMENT (cf. Store.complete_order)
    order = await store.complete_order(order_id, result.status, result.detail,
                                       registry_effect=build_done_effect(result))
    if order is None:
        raise HTTPException(404, "ordre inconnu")
    if order.get("terminal_refused"):
        raise HTTPException(409, "ordre deja en etat terminal")
    if order["status"] == "done" and order["type"] == "update_bepinex_mods":
        # Filet de securite (incident du 18/09/2026, root cause jamais reproduite
        # avec certitude malgre investigation approfondie) : un ordre rapporte
        # "done" dont le contenu REEL du store ne correspond pas a la version
        # demandee doit etre visible, jamais silencieux -- sans ca le dashboard
        # peut mentir indefiniment sur l'etat reel des mods installes.
        current = await store.bepinex.all(order["server"])
        drifted = [m["slug"] for m in (order.get("mods") or [])
                  if (current.get(m["slug"]) or {}).get("installed_version") != m.get("version")]
        if drifted:
            from app.notify import send_alert
            await send_alert(request.app,
                f"⚠️ Hephaestos [{order['server']}] MAJ BepInEx rapportee 'done' mais version(s) "
                f"non persistee(s) dans le suivi : {', '.join(drifted)} -- verifier manuellement "
                f"(le fichier sur le serveur peut deja etre a jour, seul le suivi affiche est en doute)")
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
