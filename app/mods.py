"""Service mods (Workshop Steam) : logique metier partagee par les routes admin
(boutons du dashboard) et la route agent (auto-update adosse au poll).

HYPOTHESE MONO-PROCESS (assumee, a ne pas casser silencieusement) : les caches
TTL de ce module (_backfill_failed_at, _steam_dates_refreshed_at) sont des dicts
module-level, et le Store relit/reecrit state.json a chaque operation sous un
asyncio.Lock local au process. Tout cela n'est correct que parce que le backend
tourne en UN SEUL process uvicorn (cf. deploy/entrypoint.py). Passer a `workers=N` ou a
gunicorn multi-worker multiplierait les caches (martelement de l'API Steam) et
creerait des ecritures concurrentes non verrouillees sur state.json.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import Request

from app.steam_workshop import (
    WorkshopFetchError,
    WorkshopInvalidReference,
    WorkshopItemNotFound,
    WorkshopWrongGame,
    get_workshop_item,
    get_workshop_items_bulk,
)

logger = logging.getLogger(__name__)

# Backfill : delai avant de re-tenter l'API Steam pour un mod dont la resolution a
# echoue (erreur reseau) -- le dashboard poll toutes les 10 s, sans ce garde-fou un
# mod insoluble martelerait Steam a chaque poll.
_BACKFILL_RETRY_SECONDS = 900
_backfill_failed_at: dict[tuple[str, str], float] = {}

# Refresh des dates Steam (time_updated) : au plus un appel batche par serveur par
# heure -- le dashboard poll toutes les 10 s, et time_updated ne bouge que quand
# l'auteur du mod publie une MAJ.
_STEAM_DATES_TTL_SECONDS = 3600
_steam_dates_refreshed_at: dict[str, float] = {}

# Auto-update des mods : re-tente au plus toutes les N heures par serveur. Sans ce
# cooldown, un install_mod qui echoue (ex. credentials steamcmd expires) laisse
# update_available vrai et le poll agent relancerait steamcmd toutes les 2 minutes.
MODS_AUTO_COOLDOWN_HOURS = 6


def mod_update_available(installed: bool, installed_at: str | None, steam_updated_at: str | None) -> bool:
    """MAJ de mod disponible = mod installe dont l'auteur a publie sur Steam APRES
    notre derniere installation. Sans installed_at (mods d'avant la feature, tant
    qu'ils n'ont pas ete re-installes une fois) : False — pas de badge permanent
    invérifiable."""
    if not installed or not installed_at or not steam_updated_at:
        return False
    try:
        # TypeError inclus : comparer un datetime naif (donnee legacy sans tz) a un
        # aware leve TypeError, pas ValueError -- sans lui, 500 sur tout /api/servers.
        return datetime.fromisoformat(steam_updated_at) > datetime.fromisoformat(installed_at)
    except (ValueError, TypeError):
        return False


def epoch_to_iso(epoch: int | None) -> str | None:
    if not epoch or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


async def refresh_mods_steam_dates(request: Request, name: str, wids: list[str]) -> None:
    """Rafraichit steam_updated_at (et titre/vignette au passage) des mods installes
    via UN appel Steam batche. Echec reseau : valeurs stockees conservees, TTL pose
    quand meme (pas de martelement de Steam au poll suivant)."""
    if not wids:
        return
    last = _steam_dates_refreshed_at.get(name)
    if last is not None and time.monotonic() - last < _STEAM_DATES_TTL_SECONDS:
        return
    _steam_dates_refreshed_at[name] = time.monotonic()
    store = request.app.state.store
    try:
        items = await get_workshop_items_bulk(request.app.state.http_client, wids)
    except WorkshopFetchError:
        return
    for wid, info in items.items():
        await store.mods.set_mod_metadata(
            name, wid, info["title"], info["thumbnail_url"],
            steam_updated_at=epoch_to_iso(info["time_updated"]),
        )


async def backfill_mod_metadata(request: Request, name: str, workshop_appid: int, wid: str) -> dict | None:
    """Re-resout titre+vignette d'un mod installe dont les metadonnees ont ete perdues
    (incident 15/07 : mods_metadata vide alors que 4 mods etaient installes -- les
    metadonnees n'etaient creees qu'a l'ordre install_mod, donc toute purge etait
    definitive). Persiste le resultat ; un mod retire du Workshop recoit un placeholder
    persiste ; une erreur reseau retourne None (fallback non persiste, re-tente apres TTL).
    """
    failed_at = _backfill_failed_at.get((name, wid))
    if failed_at is not None and time.monotonic() - failed_at < _BACKFILL_RETRY_SECONDS:
        return None
    store = request.app.state.store
    try:
        info = await get_workshop_item(request.app.state.http_client, wid, workshop_appid)
    except (WorkshopItemNotFound, WorkshopWrongGame, WorkshopInvalidReference):
        meta = {"title": f"Mod {wid} (retire du Workshop)", "thumbnail_url": ""}
        await store.mods.set_mod_metadata(name, wid, meta["title"], meta["thumbnail_url"])
        return meta
    except WorkshopFetchError:
        _backfill_failed_at[(name, wid)] = time.monotonic()
        return None
    _backfill_failed_at.pop((name, wid), None)
    steam_updated_at = epoch_to_iso(info["time_updated"])
    await store.mods.set_mod_metadata(name, wid, info["title"], info["thumbnail_url"],
                                 steam_updated_at=steam_updated_at)
    return {"title": info["title"], "thumbnail_url": info["thumbnail_url"],
            "steam_updated_at": steam_updated_at}


async def build_mods_entry_fields(request: Request, name: str, cfg: dict, state: dict | None) -> dict:
    """Champs mods de l'entree /api/servers d'un serveur Workshop : liste des mods
    (metadonnees + backfill auto-reparant) et drapeau mods_restart_required."""
    store = request.app.state.store
    mods_state = await store.mods.get_mods_state(name)
    installed_ids = set(mods_state["installed_mod_ids"])
    await refresh_mods_steam_dates(request, name, sorted(installed_ids))
    metadata = await store.mods.get_mods_metadata(name)  # relu APRES le refresh
    all_ids = set(metadata.keys()) | installed_ids
    mods = []
    for wid in all_ids:
        meta = metadata.get(wid)
        # entree absente OU partielle (installed_at pose par update_mods_state
        # sans title) : backfill auto-reparant (incident 15/07)
        if (meta is None or "title" not in meta) and wid in installed_ids:
            backfilled = await backfill_mod_metadata(request, name, cfg["workshop_appid"], wid)
            if backfilled is not None:
                meta = {**(meta or {}), **backfilled}
        meta = meta or {}
        mods.append({
            "workshop_id": wid,
            "title": meta.get("title") or f"Mod {wid}",
            "thumbnail_url": meta.get("thumbnail_url") or None,
            "installed": wid in installed_ids,
            "installed_at": meta.get("installed_at"),
            "steam_updated_at": meta.get("steam_updated_at"),
            "update_available": mod_update_available(
                wid in installed_ids, meta.get("installed_at"), meta.get("steam_updated_at")),
        })
    process_started_at = (state or {}).get("process_started_at")
    changed_at = mods_state["changed_at"]
    return {
        "mods": mods,
        "mods_restart_required": bool(
            changed_at and (not process_started_at or process_started_at < changed_at)
        ),
    }


async def enqueue_mod_update_orders(request: Request, name: str, *, include_unknown_dates: bool,
                                    author: str | None = None, require_players_zero: bool = False) -> dict:
    """Cree les ordres install_mod pour les mods installes a mettre a jour, puis UN
    restart final si le process tourne (le drainage agent traite la file dans l'ordre :
    tous les installs, puis le restart applique les mods).

    - cible : update_available (publication Steam posterieure a notre installation) ;
      `include_unknown_dates` ajoute les mods sans installed_at (installes avant la
      feature) — voulu pour le bouton manuel, PAS pour l'auto-update (pas de
      re-telechargement aveugle sans preuve de retard).
    - saute les mods ayant deja un install_mod en attente ; pas de restart si le
      process est down (les mods seront pris au prochain demarrage) ou si un restart
      est deja en file.
    - `require_players_zero` (chemin auto uniquement, jamais le bouton manuel) :
      re-verifie l'etat juste apres l'appel reseau Steam ci-dessous avant de creer le
      moindre ordre -- ferme le TOCTOU entre le players==0 lu par l'appelant et cet
      appel reseau, pendant lequel un joueur a pu se connecter.
    Retourne {"orders_created": n, "restart": bool}.
    """
    store = request.app.state.store
    mods_state = await store.mods.get_mods_state(name)
    installed = list(mods_state["installed_mod_ids"])
    await refresh_mods_steam_dates(request, name, sorted(installed))
    if require_players_zero:
        fresh = await store.snapshot()
        if (fresh["servers"].get(name) or {}).get("players") != 0:
            return {"orders_created": 0, "restart": False}
    metadata = await store.mods.get_mods_metadata(name)
    pending = [o for o in await store.pending_orders() if o["server"] == name]
    pending_install_ids = {o.get("workshop_id") for o in pending if o["type"] == "install_mod"}

    created = 0
    for wid in installed:
        meta = metadata.get(wid) or {}
        needs = mod_update_available(True, meta.get("installed_at"), meta.get("steam_updated_at"))
        if not needs and include_unknown_dates and not meta.get("installed_at"):
            needs = True
        if needs and wid not in pending_install_ids:
            await store.add_order(name, "install_mod", {
                "workshop_id": wid,
                "title": meta.get("title") or f"Mod {wid}",
                "thumbnail_url": meta.get("thumbnail_url") or "",
            }, author=author)
            created += 1

    restart_created = False
    if created:
        snap = await store.snapshot()
        state = snap["servers"].get(name) or {}
        if state.get("process_up") and not any(o["type"] == "restart" for o in pending):
            await store.add_order(name, "restart", author=author)
            restart_created = True
    return {"orders_created": created, "restart": restart_created}


async def auto_enqueue_mod_updates(request: Request) -> None:
    """Fenetre d'auto-update des mods, adossee au poll agent (pas de scheduler) :
    memes gardes que l'auto-update jeu cote agent — serveur PROUVE vide (players == 0
    strict ; None = pas de source de comptage, jamais auto, meme gap documente que
    l'auto-update Valheim) et aucun ordre en attente pour ce serveur (ne pas
    s'intercaler dans des actions en cours). Seuls les mods avec update_available
    sont cibles (jamais ceux sans installed_at : re-basage manuel). Une erreur ici
    ne doit JAMAIS faire echouer le GET /orders de l'agent."""
    store = request.app.state.store
    try:
        snap = await store.snapshot()
        # Chargee une seule fois (comme game_updates.auto_enqueue_game_updates) :
        # sans ca, pending_orders() relit et re-parse state.json en entier pour
        # CHAQUE serveur Workshop de la boucle.
        pending = await store.pending_orders()
        servers_with_pending = {o["server"] for o in pending}
        for name, cfg in (await store.registry.all()).items():
            if "workshop_appid" not in cfg:
                continue
            state = snap["servers"].get(name) or {}
            if state.get("players") != 0:
                continue
            if name in servers_with_pending:
                continue
            marker = await store.mods.get_mods_auto_marker(name)
            if marker is not None:
                try:
                    if datetime.now(UTC) - datetime.fromisoformat(marker) < timedelta(hours=MODS_AUTO_COOLDOWN_HOURS):
                        continue
                except (ValueError, TypeError):
                    pass  # marqueur illisible = pas de cooldown
            result = await enqueue_mod_update_orders(request, name, include_unknown_dates=False,
                                                     author="auto", require_players_zero=True)
            if result["orders_created"]:
                await store.mods.set_mods_auto_marker(name)
                logger.info("auto-update mods %s : %d install(s), restart=%s",
                            name, result["orders_created"], result["restart"])
    except Exception:
        logger.exception("auto-enqueue des MAJ de mods : erreur ignoree (poll agent preserve)")
