import re

import httpx

WORKSHOP_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


class WorkshopInvalidReference(Exception):
    pass


class WorkshopItemNotFound(Exception):
    pass


class WorkshopWrongGame(Exception):
    pass


class WorkshopFetchError(Exception):
    pass


def extract_workshop_id(ref: str) -> str:
    ref = ref.strip()
    if ref.isdigit():
        return ref
    match = re.search(r"[?&]id=(\d+)", ref)
    if match:
        return match.group(1)
    raise WorkshopInvalidReference(f"reference Workshop invalide: {ref}")


def _parse_time_updated(detail: dict) -> int | None:
    try:
        t = int(detail.get("time_updated", 0))
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


async def get_workshop_item(client: httpx.AsyncClient, ref: str, expected_appid: int) -> dict:
    workshop_id = extract_workshop_id(ref)

    try:
        resp = await client.post(
            WORKSHOP_URL,
            data={"itemcount": 1, "publishedfileids[0]": workshop_id},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise WorkshopFetchError(str(e)) from e

    try:
        detail = body["response"]["publishedfiledetails"][0]
    except (KeyError, IndexError) as e:
        raise WorkshopFetchError("reponse Steam inattendue") from e

    if detail.get("result") != 1:
        raise WorkshopItemNotFound(f"mod introuvable ou supprime: {workshop_id}")

    try:
        consumer_app_id = int(detail.get("consumer_app_id", 0))
    except (TypeError, ValueError) as e:
        raise WorkshopFetchError("consumer_app_id inattendu dans la reponse Steam") from e

    if consumer_app_id != expected_appid:
        raise WorkshopWrongGame(f"consumer_app_id {consumer_app_id} != {expected_appid}")

    return {
        "workshop_id": workshop_id,
        "title": detail.get("title", ""),
        "thumbnail_url": detail.get("preview_url", ""),
        "description": detail.get("description", ""),
        "time_updated": _parse_time_updated(detail),
    }


QUERY_FILES_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"

_SORT_QUERY_TYPES = {
    "text": 12,   # RankedByTextSearch
    "trend": 3,   # RankedByTrend (necessite "days")
    "recent": 1,  # RankedByPublicationDate
}


async def search_workshop_items(
    client: httpx.AsyncClient,
    api_key: str,
    appid: int,
    query_text: str | None,
    sort: str,
    page: int,
) -> list[dict]:
    if sort not in _SORT_QUERY_TYPES:
        raise ValueError(f"tri invalide: {sort}")

    params: dict[str, str | int | bool] = {
        "key": api_key,
        "query_type": _SORT_QUERY_TYPES[sort],
        "appid": appid,
        "page": page,
        "numperpage": 20,
        "return_short_description": True,
    }
    if sort == "trend":
        params["days"] = 7
    if query_text:
        params["search_text"] = query_text

    try:
        resp = await client.get(QUERY_FILES_URL, params=params, timeout=15)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise WorkshopFetchError(str(e)) from e

    try:
        details = body["response"].get("publishedfiledetails", [])
    except (KeyError, TypeError) as e:
        raise WorkshopFetchError("reponse Steam inattendue") from e

    try:
        return [
            {
                "workshop_id": str(item["publishedfileid"]),
                "title": item.get("title", ""),
                "description": item.get("short_description", ""),
                "thumbnail_url": item.get("preview_url", ""),
                "subscriptions": item.get("subscriptions", 0),
            }
            for item in details
        ]
    except (KeyError, TypeError) as e:
        raise WorkshopFetchError("item Workshop de forme inattendue dans la reponse") from e


async def get_workshop_items_bulk(
    client: httpx.AsyncClient, workshop_ids: list[str]
) -> dict[str, dict]:
    """Resout un lot de mods en UN seul appel Steam (GetPublishedFileDetails supporte
    itemcount=N). Retourne {wid: {title, thumbnail_url, time_updated}} ; les items
    introuvables/supprimes (result != 1) sont simplement absents du resultat.
    Pas de validation consumer_app_id : deja faite a l'installation de chaque mod.
    """
    if not workshop_ids:
        return {}
    data: dict = {"itemcount": len(workshop_ids)}
    for i, wid in enumerate(workshop_ids):
        data[f"publishedfileids[{i}]"] = wid
    try:
        resp = await client.post(WORKSHOP_URL, data=data, timeout=15)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise WorkshopFetchError(str(e)) from e
    try:
        details = body["response"]["publishedfiledetails"]
    except (KeyError, TypeError) as e:
        raise WorkshopFetchError("reponse Steam inattendue") from e
    items: dict[str, dict] = {}
    for detail in details:
        if not isinstance(detail, dict) or detail.get("result") != 1:
            continue
        wid = str(detail.get("publishedfileid", ""))
        if not wid:
            continue
        items[wid] = {
            "title": detail.get("title", ""),
            "thumbnail_url": detail.get("preview_url", ""),
            "time_updated": _parse_time_updated(detail),
        }
    return items
