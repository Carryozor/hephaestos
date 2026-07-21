import httpx

from app.steam import SteamBuildIds


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_and_cache():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}},
        )

    s = SteamBuildIds(make_client(handler))
    assert await s.public_buildid(2394010) == "24088465"
    assert await s.public_buildid(2394010) == "24088465"
    assert len(calls) == 1  # 2e appel servi par le cache


async def test_failure_returns_stale_then_none():
    ok = {"data": {"896660": {"depots": {"branches": {"public": {"buildid": "777"}}}}}}
    responses = [httpx.Response(200, json=ok), httpx.Response(500)]
    s = SteamBuildIds(make_client(lambda r: responses.pop(0)))
    assert await s.public_buildid(896660) == "777"
    s._cache[896660] = (-99999.0, "777")  # force expiration du TTL
    assert await s.public_buildid(896660) == "777"  # échec -> valeur périmée conservée
    assert await SteamBuildIds(make_client(lambda r: httpx.Response(500))).public_buildid(1) is None


async def test_unexpected_json_shape_returns_none():
    # {"data": null} -> indexation sur None = TypeError, doit etre absorbee
    s = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"data": None})))
    assert await s.public_buildid(42) is None


async def test_unexpected_json_shape_returns_stale():
    s = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"data": None})))
    s._cache[42] = (-99999.0, "123")  # cache perime pre-rempli
    assert await s.public_buildid(42) == "123"


async def test_public_app_name_fetch_and_cache():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200, json={"data": {"1829350": {"common": {"name": "V Rising Dedicated Server"},
                                            "depots": {"branches": {"public": {"buildid": "1"}}}}}})

    s = SteamBuildIds(make_client(handler))
    assert await s.public_app_name(1829350) == "V Rising Dedicated Server"
    assert await s.public_app_name(1829350) == "V Rising Dedicated Server"
    assert len(calls) == 1  # 2e appel servi par le cache


async def test_public_app_name_failure_returns_none():
    assert await SteamBuildIds(make_client(lambda r: httpx.Response(500))).public_app_name(1) is None
    # {"data": null} -> TypeError absorbée
    s = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"data": None})))
    assert await s.public_app_name(42) is None


async def test_search_apps_returns_bounded_list():
    def handler(request):
        return httpx.Response(200, json={"total": 2, "items": [
            {"id": 892970, "name": "Valheim"}, {"id": 1620250, "name": "Valheim Soundtrack"}]})

    s = SteamBuildIds(make_client(handler))
    results = await s.search_apps("valheim")
    assert results == [{"appid": 892970, "name": "Valheim"}, {"appid": 1620250, "name": "Valheim Soundtrack"}]


async def test_search_apps_failure_returns_empty_list():
    s = SteamBuildIds(make_client(lambda r: httpx.Response(500)))
    assert await s.search_apps("x") == []
    s2 = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"total": 0})))
    assert await s2.search_apps("x") == []  # pas de cle "items" -> absorbee, liste vide


async def test_search_apps_bounded_to_20():
    def handler(request):
        items = [{"id": i, "name": f"App {i}"} for i in range(30)]
        return httpx.Response(200, json={"total": 30, "items": items})

    s = SteamBuildIds(make_client(handler))
    results = await s.search_apps("x")
    assert len(results) == 20


async def test_search_apps_malformed_items_returns_empty_list_not_exception():
    # {"items": null} : JSON valide, forme inattendue -- items[:20] leverait TypeError
    # si le retour n'est pas dans le meme bloc try que le parsing (trouve en revue finale).
    s = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"items": None})))
    assert await s.search_apps("x") == []
    # item sans cle "id"/"name" -> KeyError potentielle
    s2 = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"items": [{"foo": "bar"}]})))
    assert await s2.search_apps("x") == []


async def test_app_details_success():
    def handler(request):
        return httpx.Response(200, json={"1623730": {"success": True, "data": {
            "header_image": "https://x/header.jpg", "short_description": "Une description. " * 30}}})

    s = SteamBuildIds(make_client(handler))
    details = await s.app_details(1623730)
    assert details["header_image"] == "https://x/header.jpg"
    assert len(details["description"]) <= 300


async def test_app_details_unavailable_returns_none():
    # cas reel des appids de serveur dedie : success=false
    s = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"2394010": {"success": False}})))
    assert await s.app_details(2394010) is None


async def test_app_details_failure_returns_none():
    s = SteamBuildIds(make_client(lambda r: httpx.Response(500)))
    assert await s.app_details(1) is None
    s2 = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={})))
    assert await s2.app_details(1) is None


async def test_app_details_null_entry_returns_none_not_exception():
    # Steam renvoie parfois {"<appid>": null} (app region-restreinte/delistee) --
    # entry.get() sur None leve AttributeError, absente du tuple except d'origine
    # (trouve en revue finale).
    s = SteamBuildIds(make_client(lambda r: httpx.Response(200, json={"1": None})))
    assert await s.app_details(1) is None
