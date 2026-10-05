"""Effets de bord registre d'un ordre confirme "done" par l'agent. Fonctions PURES sur
le dict d'etat brut (aucun verrou, aucun _dump) : Store.complete_order les execute sous
son verrou, dans le meme _dump que le statut de l'ordre (atomicite, cf. B2.9).

Cycle de vie du deploiement : les transitions de statut du registre suivent les
confirmations agent, jamais l'optimisme du backend.
"""
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.storage_bepinex import validate_installed_paths

logger = logging.getLogger(__name__)

Effect = Callable[[dict, dict], None]


def _registry_entry(data: dict, order: dict) -> dict | None:
    return data.get("servers_registry", {}).get(order["server"])


def build_done_effect(result) -> Effect | None:
    """Effet associe au rapport `result` (OrderResult) ; None si le type d'ordre n'en a pas.
    Le type est lu sur l'ORDRE au moment de l'application (pas sur le rapport)."""
    def effect(data: dict, order: dict) -> None:
        entry = _registry_entry(data, order)
        kind = order["type"]
        if kind in ("install_game", "scan_exe"):
            if entry is not None:
                entry.update({"exe_candidates": list(result.exe_candidates or []),
                              "status": "awaiting_setup"})
        elif kind == "setup_server":
            if entry is not None:
                entry.update({"status": "active", "exe_candidates": None})
        elif kind == "list_files":
            if entry is not None:
                listing = dict(entry.get("files_listing") or {})
                listing[order["root"]] = list(result.files or [])
                entry["files_listing"] = listing
        elif kind == "read_file":
            if entry is not None:
                entry["file_read"] = {
                    "root": order["root"], "path": order["path"],
                    "content_b64": result.content_b64, "sha256": result.sha256,
                    # horodatage consomme par purge_stale_file_reads : ce bloc contient un
                    # secret en clair (AdminPassword) et ne doit pas survivre au-dela du TTL.
                    "read_at": datetime.now(UTC).isoformat()}
        elif kind == "update_bepinex_mods":
            _apply_bepinex_installed(data, order, result)
    return effect


def _apply_bepinex_installed(data: dict, order: dict, result) -> None:
    # Revue securite du 12/09/2026 (M2) : ne persiste que les slugs qui appartenaient
    # reellement a CET ordre -- un agent qui rapporterait un slug hors de l'ordre traite
    # ne doit jamais pouvoir ecraser le bookkeeping (installed_paths) d'un AUTRE mod, qui
    # serait ensuite passe en previous_paths a une future suppression
    # (Remove-BepInExPaths).
    server = order["server"]
    ordered_slugs = {m.get("slug") for m in (order.get("mods") or [])}
    mods = data.get("bepinex_mods", {}).get(server, {})
    for reported in (result.bepinex_installed or []):
        slug = reported.get("slug")
        if slug not in ordered_slugs:
            logger.warning("update_bepinex_mods [%s]: slug '%s' hors de l'ordre traite, ignore",
                           server, slug)
            continue
        try:
            mod = mods.get(slug)
            if mod is None:
                continue  # slug inconnu = no-op (pas d'entree fantome)
            paths = list(reported.get("paths") or [])
            version = reported.get("version")
            validate_installed_paths(paths, mod["target"])
            if not isinstance(version, str):
                raise TypeError("version absente ou invalide")
        except (ValueError, TypeError, KeyError):
            # Chemin invalide ou entree mal formee rapportee par l'agent (source non
            # fiable, P5) : ignoree plutot que de faire echouer tout le rapport -- les
            # autres mods du meme ordre restent persistes.
            logger.warning("update_bepinex_mods [%s]: entree bepinex_installed ignoree "
                           "(invalide): %r", server, reported)
            continue
        mod["installed_version"] = version
        mod["installed_paths"] = paths
        mod["installed_at"] = datetime.now(UTC).isoformat()
        mod["last_error"] = None
