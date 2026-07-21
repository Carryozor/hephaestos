import time

import httpx

CACHE_TTL_SECONDS = 900
API_URL = "https://api.steamcmd.net/v1/info/{appid}"


class SteamBuildIds:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._cache: dict[int, tuple[float, str]] = {}
        self._name_cache: dict[int, tuple[float, str]] = {}

    async def public_buildid(self, appid: int) -> str | None:
        now = time.monotonic()
        hit = self._cache.get(appid)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            resp = await self._client.get(API_URL.format(appid=appid), timeout=15)
            resp.raise_for_status()
            buildid = resp.json()["data"][str(appid)]["depots"]["branches"]["public"]["buildid"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return hit[1] if hit else None
        self._cache[appid] = (now, buildid)
        return buildid

    async def public_app_name(self, appid: int) -> str | None:
        """Nom public de l'app (pré-remplissage du wizard de déploiement).
        Échec = None, jamais d'exception : le lookup est un confort, pas un prérequis."""
        now = time.monotonic()
        hit = self._name_cache.get(appid)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            resp = await self._client.get(API_URL.format(appid=appid), timeout=15)
            resp.raise_for_status()
            name = resp.json()["data"][str(appid)]["common"]["name"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return hit[1] if hit else None
        if not isinstance(name, str) or not name:
            return hit[1] if hit else None
        self._name_cache[appid] = (now, name)
        return name

    async def search_apps(self, term: str) -> list[dict]:
        """Recherche boutique Steam (repli, en plus de la liste locale connue) : ne
        trouve jamais les appids d'outils "serveur dedie" (verifie en reel le 19/07 --
        categorie non indexee par la recherche grand public), utile seulement pour les
        jeux de base/DLC. Echec reseau = liste vide, jamais d'exception."""
        try:
            resp = await self._client.get(
                "https://store.steampowered.com/api/storesearch/",
                params={"term": term, "l": "english", "cc": "us"}, timeout=15)
            resp.raise_for_status()
            items = resp.json()["items"]
            return [{"appid": it["id"], "name": it["name"]} for it in items[:20]]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return []

    async def app_details(self, appid: int) -> dict | None:
        """Details boutique Steam (image + description) pour un appid direct. None si
        l'appid n'a pas de page boutique (categorie "outil"/serveur dedie -- verifie en
        reel le 19/07 : success=false systematique) ou en cas d'echec reseau/JSON."""
        try:
            resp = await self._client.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": appid}, timeout=15)
            resp.raise_for_status()
            entry = resp.json()[str(appid)]
            if not entry.get("success"):
                return None
            data = entry["data"]
            return {"header_image": data["header_image"], "description": data["short_description"][:300]}
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
            return None
