"""Auto-update jeu : rapatrie cote backend la decision jusque-la geree seule par
l'agent PowerShell (hephaestos-agent.ps1). Meme pattern que app/mods.py (auto_enqueue_mod_updates) :
adosse au poll agent (pas de scheduler), pas de fenetre horaire -- des qu'un serveur est
PROUVE vide (players == 0 strict) et a une MAJ dispo, un ordre "update" est cree, protege
par un cooldown pour ne pas re-tenter steamcmd a chaque poll en cas d'echec repete.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import Request

logger = logging.getLogger(__name__)

# Meme garde-fou que MODS_AUTO_COOLDOWN_HOURS (app/mods.py) : sans cooldown, un
# steamcmd qui echoue (ex. credentials expires) laisse le buildid perime et le poll
# agent (toutes les 2 min) relancerait la MAJ en boucle.
GAME_AUTO_COOLDOWN_HOURS = 6


async def auto_enqueue_game_updates(request: Request) -> None:
    """Cree un ordre "update" pour chaque serveur eligible : buildid local connu et
    different du buildid public, players == 0 strict (None = pas de source de
    comptage, jamais auto -- meme gap documente que Valheim cote mods), aucun ordre
    deja en attente sur ce serveur, cooldown ecoule. Une erreur ici ne doit JAMAIS
    faire echouer le GET /orders de l'agent."""
    store, steam = request.app.state.store, request.app.state.steam
    try:
        snap = await store.snapshot()
        pending = await store.pending_orders()
        servers_with_pending = {o["server"] for o in pending}
        for name, cfg in (await store.registry.all()).items():
            state = snap["servers"].get(name) or {}
            local = state.get("buildid")
            if not local:
                continue
            if state.get("players") != 0:
                continue
            if name in servers_with_pending:
                continue
            public = await steam.public_buildid(cfg["server_appid"])
            if not public or local == public:
                continue
            marker = await store.get_game_auto_marker(name)
            if marker is not None:
                try:
                    age = datetime.now(UTC) - datetime.fromisoformat(marker)
                    if age < timedelta(hours=GAME_AUTO_COOLDOWN_HOURS):
                        continue
                except (ValueError, TypeError):
                    pass  # marqueur illisible = pas de cooldown
            await store.add_order(name, "update", author="auto")
            await store.set_game_auto_marker(name)
            logger.info("auto-update jeu %s : ordre update cree (build %s -> %s)",
                        name, local, public)
    except Exception:
        logger.exception("auto-enqueue des MAJ jeu : erreur ignoree (poll agent preserve)")
