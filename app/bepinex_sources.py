"""Verification de version pour les mods BepInEx (Thunderstore, GitHub Releases) :
appels reseau LEGERS (metadonnees JSON), jamais de telechargement de binaire ici
-- le binaire n'est jamais recupere par le backend, cf.
docs/plans/2026-09-12-bepinex-mod-updates.md section 1.
"""
import re
from urllib.parse import urlsplit

import httpx

THUNDERSTORE_URL = "https://thunderstore.io/api/experimental/package/{owner}/{package}/"
GITHUB_RELEASE_URL = "https://api.github.com/repos/{owner}/{repo}/releases/latest"

# Hotes verifies empiriquement le 12/09/2026 (curl -sI sur les download_url reels) :
# thunderstore.io redirige vers gcdn.thunderstore.io, GitHub vers
# release-assets.githubusercontent.com. objects.githubusercontent.com est l'ancien
# hote GitHub (avant la migration vers release-assets.*) -- garde par prudence,
# GitHub a deja change ce nom par le passe sans prevenir.
ALLOWED_DOWNLOAD_HOSTS = {
    "thunderstore.io",
    "gcdn.thunderstore.io",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class BepInExFetchError(Exception):
    pass


class BepInExPackageNotFound(BepInExFetchError):
    pass


class BepInExAssetNotFound(BepInExFetchError):
    pass


class BepInExUntrustedUrl(BepInExFetchError):
    pass


def _check_url_allowed(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        raise BepInExUntrustedUrl(f"URL de telechargement non fiable: {url}")
    if parts.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise BepInExUntrustedUrl(f"hote de telechargement non autorise: {parts.hostname}")


async def fetch_thunderstore_latest(client: httpx.AsyncClient, owner: str, package: str) -> dict:
    """Retourne {"version": str, "download_url": str} pour la derniere version
    publiee d'un package Thunderstore. Ne suit/ne telecharge jamais le zip."""
    url = THUNDERSTORE_URL.format(owner=owner, package=package)
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code == 404:
            raise BepInExPackageNotFound(f"package Thunderstore introuvable: {owner}/{package}")
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BepInExFetchError(str(e)) from e
    try:
        latest = body["latest"]
        version = latest["version_number"]
        download_url = latest["download_url"]
    except (KeyError, TypeError) as e:
        raise BepInExFetchError("reponse Thunderstore inattendue") from e
    _check_url_allowed(download_url)
    return {"version": version, "download_url": download_url}


async def fetch_github_release_latest(
    client: httpx.AsyncClient, owner: str, repo: str, *,
    tag_prefix: str | None, asset_pattern: str,
) -> dict:
    """Retourne {"version": str, "download_url": str} pour la derniere release
    GitHub d'un repo. `tag_prefix` (ex. "v") est retire du tag pour normaliser
    la version ; `asset_pattern` (regex) selectionne le premier asset dont le
    nom matche (ex. XPortal publie aussi un zip -debug- a ignorer)."""
    url = GITHUB_RELEASE_URL.format(owner=owner, repo=repo)
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code == 404:
            raise BepInExPackageNotFound(f"repo/release GitHub introuvable: {owner}/{repo}")
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BepInExFetchError(str(e)) from e
    try:
        tag = body["tag_name"]
        assets = body["assets"]
    except (KeyError, TypeError) as e:
        raise BepInExFetchError("reponse GitHub inattendue") from e

    version = tag[len(tag_prefix):] if tag_prefix and tag.startswith(tag_prefix) else tag

    try:
        match = next(a for a in assets if re.match(asset_pattern, a["name"]))
    except (StopIteration, KeyError, TypeError) as e:
        if isinstance(e, StopIteration):
            raise BepInExAssetNotFound(
                f"aucun asset ne correspond a {asset_pattern!r} pour {owner}/{repo}@{tag}"
            ) from e
        raise BepInExFetchError("asset GitHub de forme inattendue") from e

    download_url = match["browser_download_url"]
    _check_url_allowed(download_url)
    return {"version": version, "download_url": download_url}
