"""Service mods BepInEx (Valheim, pas de Steam Workshop pour ce jeu) : verification
de version Thunderstore/GitHub adossee au poll agent (PAS au dashboard, cf. D4 du
plan) et exposition read-only dans GET /api/servers.
Cf. docs/plans/2026-09-12-bepinex-mod-updates.md.
"""
import logging
import time

from fastapi import Request

from app.bepinex_sources import (
    BepInExFetchError,
    fetch_github_release_latest,
    fetch_thunderstore_latest,
)

logger = logging.getLogger(__name__)


class BepInExOrderError(Exception):
    """Aucun mod cible / version pas encore connue -- 409 cote route (l'appelant
    doit relancer une verification ou attendre le prochain poll agent)."""


class BepInExUnknownSlug(Exception):
    """Slug demande absent du registre de ce serveur -- 400 cote route."""

# TTL de rafraichissement par serveur -- adosse au poll agent (2 min), jamais au
# dashboard (10 s) : Thunderstore/GitHub n'ont pas d'API bulk comme Steam, un
# appel reseau par mod a chaque poll dashboard serait un martelement inutile.
_LATEST_TTL_SECONDS = 3600
_latest_refreshed_at: dict[str, float] = {}


def _parse_version(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return None


def version_is_newer(latest: str | None, installed: str | None) -> bool:
    """Compare deux versions numeriquement (2.9.0 < 2.30.0), PAS lexicographiquement.
    Repli sur une simple inegalite de chaine si l'une des deux n'est pas parsable
    en tuple d'entiers -- mieux qu'un crash sur un format de version exotique."""
    if not latest or not installed:
        return False
    lp, ip = _parse_version(latest), _parse_version(installed)
    if lp is not None and ip is not None:
        return lp > ip
    return latest != installed


async def _fetch_latest(request: Request, mod: dict) -> dict:
    client = request.app.state.http_client
    if mod["source"] == "thunderstore":
        return await fetch_thunderstore_latest(client, mod["owner"], mod["package"])
    return await fetch_github_release_latest(
        client, mod["owner"], mod["package"],
        tag_prefix=mod.get("tag_prefix"), asset_pattern=mod["asset_pattern"],
    )


async def refresh_bepinex_versions(request: Request, name: str) -> None:
    """Rafraichit latest_version/download_url de tous les mods BepInEx suivis pour
    ce serveur, au plus une fois par _LATEST_TTL_SECONDS. Un echec reseau sur un
    mod pose last_error et conserve le dernier latest_version connu -- les autres
    mods du meme serveur ne sont pas affectes (boucle continue)."""
    last = _latest_refreshed_at.get(name)
    if last is not None and time.monotonic() - last < _LATEST_TTL_SECONDS:
        return
    store = request.app.state.store
    mods = await store.bepinex.all(name)
    if not mods:
        return
    _latest_refreshed_at[name] = time.monotonic()
    for slug, mod in mods.items():
        try:
            result = await _fetch_latest(request, mod)
        except BepInExFetchError as e:
            await store.bepinex.set_latest(name, slug, error=str(e))
            continue
        await store.bepinex.set_latest(name, slug, version=result["version"],
                                       download_url=result["download_url"])


async def refresh_all_bepinex_versions(request: Request) -> None:
    """Adosse au poll agent (GET /api/agent/orders) : une exception ici ne doit
    JAMAIS faire echouer le poll (meme contrat que mods.auto_enqueue_mod_updates)."""
    try:
        store = request.app.state.store
        for name in await store.registry.all():
            await refresh_bepinex_versions(request, name)
    except Exception:
        logger.exception("rafraichissement des versions BepInEx : erreur ignoree (poll agent preserve)")


async def force_refresh_bepinex_versions(request: Request, name: str) -> None:
    """Verification demandee explicitement par un admin (bouton "verifier") :
    contourne le TTL du poll agent automatique -- un humain qui clique attend un
    resultat immediat, pas jusqu'a une heure."""
    _latest_refreshed_at.pop(name, None)
    await refresh_bepinex_versions(request, name)


async def enqueue_bepinex_update_order(request: Request, name: str, slugs: list[str] | None) -> dict:
    """Cree l'ordre `update_bepinex_mods` pour les slugs demandes (ou, si `slugs`
    est None, tous ceux avec update_available). L'URL et la version de chaque mod
    dans l'ordre viennent TOUJOURS du store (jamais du corps de la requete
    client) -- P6 du plan : un download_url arbitraire envoye par le client ne
    doit jamais pouvoir atteindre l'agent, qui l'executerait (DLL)."""
    store = request.app.state.store
    entries = await store.bepinex.all(name)
    if not entries:
        raise BepInExOrderError("aucun mod BepInEx suivi pour ce serveur")

    if slugs is None:
        targets = [slug for slug, m in entries.items()
                   if version_is_newer(m.get("latest_version"), m.get("installed_version"))]
    else:
        unknown = [s for s in slugs if s not in entries]
        if unknown:
            raise BepInExUnknownSlug(f"slug(s) inconnu(s): {', '.join(unknown)}")
        targets = slugs

    if not targets:
        raise BepInExOrderError("aucun mod a mettre a jour")

    mods_payload = []
    for slug in targets:
        m = entries[slug]
        if not m.get("latest_version") or not m.get("download_url"):
            raise BepInExOrderError(f"version/URL non connue pour '{slug}' -- relancer une verification")
        mods_payload.append({
            "slug": slug,
            "package": m["package"],
            "version": m["latest_version"],
            "download_url": m["download_url"],
            "target": m["target"],
            "previous_paths": list(m.get("installed_paths") or []),
            "expected_plugin": m.get("log_plugin_name") if m["target"] == "plugins" else None,
        })

    return await store.add_order(name, "update_bepinex_mods", {"mods": mods_payload},
                                 author=request.state.user["username"], reject_if=lambda o: True)


async def build_bepinex_entry_fields(request: Request, name: str) -> dict:
    """Champs bepinex_* de l'entree /api/servers : PURE LECTURE, aucun appel
    reseau (le rafraichissement vit dans refresh_all_bepinex_versions, adosse au
    poll agent) -- sinon GET /api/servers, polle toutes les 10 s par le dashboard,
    martelerait Thunderstore/GitHub. Retourne {} pour un serveur sans mod BepInEx
    suivi (pas de cle bepinex_* ajoutee, non-regression sur les autres jeux)."""
    store = request.app.state.store
    entries = await store.bepinex.all(name)
    if not entries:
        return {}
    mods_list = []
    update_count = 0
    for slug, mod in sorted(entries.items()):
        available = version_is_newer(mod.get("latest_version"), mod.get("installed_version"))
        if available:
            update_count += 1
        mods_list.append({
            "slug": slug,
            "title": mod["title"],
            "source": mod["source"],
            "installed_version": mod.get("installed_version"),
            "plugin_version": mod.get("plugin_version"),
            "latest_version": mod.get("latest_version"),
            "latest_checked_at": mod.get("latest_checked_at"),
            "last_error": mod.get("last_error"),
            "update_available": available,
        })
    return {"bepinex_mods": mods_list, "bepinex_update_count": update_count}
