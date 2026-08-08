"""Auto-redemarrage sur crash : si un serveur actif avec registry.auto_restart_on_crash
actif rapporte process_up=False sans qu'aucun ordre ne soit deja en file pour lui NI
qu'un arret ait ete demande volontairement, un ordre "start" est cree automatiquement.
Meme pattern d'accroche que app/game_updates.py (auto_enqueue_game_updates) : adosse
au poll agent (GET /api/agent/orders), jamais d'exception qui casse le poll.

Ne doit JAMAIS annuler un arret volontaire (POST .../stop) : `servers_desired_state`
(pose par Store.set_order_status a la confirmation agent d'un ordre stop/start/
restart/update) distingue "arret demande" de "crash" -- un `stop` reste un `stop`
tant qu'aucun ordre start/restart/update n'a ete relance, meme apres confirmation
agent (le garde-fou `servers_with_pending` seul ne couvre que la fenetre ou l'ordre
est encore en file, pas apres son execution).

Garde-fou anti-boucle (cas reel : crash-loop Palworld du 25/07/2026, mod natif casse
rechargeant la meme save corrompue a chaque redemarrage) : au-dela de
CRASH_RESTART_MAX_ATTEMPTS tentatives dans une fenetre glissante de
CRASH_RESTART_WINDOW_MINUTES, le disjoncteur se declenche : plus aucun ordre auto
n'est cree et une alerte webhook part une seule fois. L'historique n'est efface que
lorsque l'uptime est CONFIRME stable sur CRASH_RESTART_CONFIRM_POLLS polls consecutifs
(pas un seul) : dans un crash-loop ou le process reboote puis retombe, un unique poll
process_up=True intermediaire ne doit pas desarmer le disjoncteur avant le prochain
crash."""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import Request

logger = logging.getLogger(__name__)

CRASH_RESTART_WINDOW_MINUTES = 60
CRASH_RESTART_MAX_ATTEMPTS = 3
CRASH_RESTART_CONFIRM_POLLS = 2


async def auto_enqueue_crash_restarts(request: Request) -> None:
    store = request.app.state.store
    try:
        snap = await store.snapshot()
        pending = await store.pending_orders()
        servers_with_pending = {o["server"] for o in pending}
        desired_state = snap.get("servers_desired_state", {})
        for name, cfg in (await store.registry.all()).items():
            if cfg.get("status") != "active" or not cfg.get("auto_restart_on_crash"):
                continue
            state = snap["servers"].get(name) or {}
            process_up = state.get("process_up")
            info = await store.get_crash_recovery(name)
            attempts = info.get("attempts", [])
            breaker = info.get("breaker_tripped_at")
            streak = info.get("up_streak", 0)

            if process_up is True:
                streak += 1
                if streak >= CRASH_RESTART_CONFIRM_POLLS:
                    await store.clear_crash_recovery(name)
                else:
                    await store.set_crash_recovery(name, attempts, breaker, streak)
                continue
            if process_up is not False:
                continue  # jamais rapporte / inconnu : rien a decider

            if streak:
                # la panne casse toute preuve de stabilite en cours d'accumulation
                await store.set_crash_recovery(name, attempts, breaker, 0)
            if desired_state.get(name) == "stopped":
                continue  # arret volontaire, pas un crash -- attendre une action explicite
            if name in servers_with_pending:
                # un ordre (start/stop/restart/update...) deja en file resout deja
                # la situation -- ne pas en empiler un second par-dessus.
                continue
            if breaker:
                # deja abandonne + alerte pour cet episode : silencieux jusqu'au
                # retour confirme (process_up=True stable, gere ci-dessus).
                continue
            cutoff = datetime.now(UTC) - timedelta(minutes=CRASH_RESTART_WINDOW_MINUTES)
            attempts = [a for a in attempts if _within_window(a, cutoff)]
            if len(attempts) >= CRASH_RESTART_MAX_ATTEMPTS:
                now = datetime.now(UTC).isoformat()
                await store.set_crash_recovery(name, attempts, now, 0)
                from app.notify import send_alert
                await send_alert(
                    request.app,
                    f"\U0001F501 Hephaestos [{name}] auto-reboot desactive apres "
                    f"{CRASH_RESTART_MAX_ATTEMPTS} tentatives en "
                    f"{CRASH_RESTART_WINDOW_MINUTES} min -- intervention manuelle requise")
                logger.warning("auto-reboot %s : disjoncteur declenche", name)
                continue
            attempts.append(datetime.now(UTC).isoformat())
            await store.set_crash_recovery(name, attempts, None, 0)
            await store.add_order(name, "start", author="auto-crash-recovery")
            logger.info("auto-reboot %s : ordre start cree (tentative %d/%d)",
                        name, len(attempts), CRASH_RESTART_MAX_ATTEMPTS)
    except Exception:
        logger.exception("auto-enqueue des redemarrages sur crash : erreur ignoree (poll agent preserve)")


def _within_window(iso_value: str, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(iso_value) > cutoff
    except (ValueError, TypeError):
        return False  # horodatage illisible : ne compte pas dans la fenetre
