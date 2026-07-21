import httpx
import pytest

from app.steam_workshop import (
    WorkshopFetchError,
    WorkshopInvalidReference,
    WorkshopItemNotFound,
    WorkshopWrongGame,
    extract_workshop_id,
    get_workshop_item,
    search_workshop_items,
)


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_extract_workshop_id_from_raw_id():
    assert extract_workshop_id("3147025543") == "3147025543"


def test_extract_workshop_id_from_url():
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=3147025543"
    assert extract_workshop_id(url) == "3147025543"


def test_extract_workshop_id_invalid_reference_raises():
    with pytest.raises(WorkshopInvalidReference):
        extract_workshop_id("pas-un-id-ni-une-url")


async def test_get_workshop_item_success():
    def handler(request):
        return httpx.Response(200, json={"response": {"publishedfiledetails": [
            {"result": 1, "consumer_app_id": 1623730, "title": "Cool Mod",
             "preview_url": "https://x/thumb.jpg", "description": "une description",
             "time_updated": 1752400000}
        ]}})

    item = await get_workshop_item(make_client(handler), "3147025543", expected_appid=1623730)
    assert item == {
        "workshop_id": "3147025543",
        "title": "Cool Mod",
        "thumbnail_url": "https://x/thumb.jpg",
        "description": "une description",
        "time_updated": 1752400000,
    }


async def test_get_workshop_item_time_updated_absent_or_zero_is_none():
    def handler(request):
        return httpx.Response(200, json={"response": {"publishedfiledetails": [
            {"result": 1, "consumer_app_id": 1623730, "title": "Cool Mod",
             "preview_url": "https://x/thumb.jpg", "description": "d", "time_updated": 0}
        ]}})

    item = await get_workshop_item(make_client(handler), "3147025543", expected_appid=1623730)
    assert item["time_updated"] is None


async def test_get_workshop_items_bulk_maps_by_id_and_skips_failures():
    def handler(request):
        body = request.content.decode()
        # les deux IDs doivent partir dans UN seul appel batché
        assert "itemcount=2" in body
        assert "publishedfileids%5B0%5D=111" in body
        assert "publishedfileids%5B1%5D=222" in body
        return httpx.Response(200, json={"response": {"publishedfiledetails": [
            {"result": 1, "publishedfileid": "111", "title": "Mod A",
             "preview_url": "https://x/a.jpg", "time_updated": 1752000000},
            {"result": 9, "publishedfileid": "222"},
        ]}})

    from app.steam_workshop import get_workshop_items_bulk
    items = await get_workshop_items_bulk(make_client(handler), ["111", "222"])
    assert items == {"111": {"title": "Mod A", "thumbnail_url": "https://x/a.jpg",
                             "time_updated": 1752000000}}


async def test_get_workshop_items_bulk_empty_list_no_network_call():
    def handler(request):
        raise AssertionError("aucun appel reseau attendu pour une liste vide")

    from app.steam_workshop import get_workshop_items_bulk
    assert await get_workshop_items_bulk(make_client(handler), []) == {}


async def test_get_workshop_items_bulk_malformed_response_raises():
    def handler(request):
        return httpx.Response(200, json={"pas": "la bonne forme"})

    from app.steam_workshop import get_workshop_items_bulk
    with pytest.raises(WorkshopFetchError):
        await get_workshop_items_bulk(make_client(handler), ["111"])


async def test_get_workshop_item_not_found():
    def handler(request):
        return httpx.Response(200, json={"response": {"publishedfiledetails": [{"result": 9}]}})

    with pytest.raises(WorkshopItemNotFound):
        await get_workshop_item(make_client(handler), "999", expected_appid=1623730)


async def test_get_workshop_item_wrong_game():
    def handler(request):
        return httpx.Response(200, json={"response": {"publishedfiledetails": [
            {"result": 1, "consumer_app_id": 896660, "title": "Mod Valheim"}
        ]}})

    with pytest.raises(WorkshopWrongGame):
        await get_workshop_item(make_client(handler), "123", expected_appid=1623730)


async def test_get_workshop_item_malformed_consumer_app_id_raises_fetch_error():
    def handler(request):
        return httpx.Response(200, json={"response": {"publishedfiledetails": [
            {"result": 1, "consumer_app_id": None, "title": "Mod bizarre"}
        ]}})

    with pytest.raises(WorkshopFetchError):
        await get_workshop_item(make_client(handler), "123", expected_appid=1623730)


async def test_get_workshop_item_network_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(WorkshopFetchError):
        await get_workshop_item(make_client(handler), "123", expected_appid=1623730)


async def test_get_workshop_item_unexpected_response_shape():
    def handler(request):
        return httpx.Response(200, json={"response": {}})

    with pytest.raises(WorkshopFetchError):
        await get_workshop_item(make_client(handler), "123", expected_appid=1623730)


QUERY_FILES_SUCCESS_RESPONSE = {
    "response": {
        "total": 2,
        "publishedfiledetails": [
            {
                "publishedfileid": "3762679914",
                "title": "No Building Limits",
                "short_description": "Removes building height limits",
                "preview_url": "https://example.com/thumb1.jpg",
                "subscriptions": 14700,
            },
            {
                "publishedfileid": "3764277507",
                "title": "Palbox Range x2",
                "short_description": "Doubles palbox range",
                "preview_url": "https://example.com/thumb2.jpg",
                "subscriptions": 960,
            },
        ],
    }
}


async def test_search_workshop_items_text_search():
    captured = {}

    def handler(request):
        captured["params"] = dict(httpx.QueryParams(request.url.query.decode()))
        return httpx.Response(200, json=QUERY_FILES_SUCCESS_RESPONSE)

    results = await search_workshop_items(
        make_client(handler), api_key="fake-key", appid=1623730,
        query_text="building", sort="text", page=1,
    )

    assert len(results) == 2
    assert results[0] == {
        "workshop_id": "3762679914",
        "title": "No Building Limits",
        "description": "Removes building height limits",
        "thumbnail_url": "https://example.com/thumb1.jpg",
        "subscriptions": 14700,
    }
    assert captured["params"]["query_type"] == "12"
    assert captured["params"]["search_text"] == "building"
    assert captured["params"]["appid"] == "1623730"
    assert captured["params"]["key"] == "fake-key"
    assert captured["params"]["page"] == "1"


async def test_search_workshop_items_trend_sort_includes_days():
    captured = {}

    def handler(request):
        captured["params"] = dict(httpx.QueryParams(request.url.query.decode()))
        return httpx.Response(200, json=QUERY_FILES_SUCCESS_RESPONSE)

    await search_workshop_items(
        make_client(handler), api_key="fake-key", appid=1623730,
        query_text=None, sort="trend", page=1,
    )

    assert captured["params"]["query_type"] == "3"
    assert captured["params"]["days"] == "7"
    assert "search_text" not in captured["params"]


async def test_search_workshop_items_recent_sort():
    captured = {}

    def handler(request):
        captured["params"] = dict(httpx.QueryParams(request.url.query.decode()))
        return httpx.Response(200, json=QUERY_FILES_SUCCESS_RESPONSE)

    await search_workshop_items(
        make_client(handler), api_key="fake-key", appid=1623730,
        query_text=None, sort="recent", page=1,
    )

    assert captured["params"]["query_type"] == "1"


async def test_search_workshop_items_invalid_sort_raises_value_error():
    with pytest.raises(ValueError):
        await search_workshop_items(
            make_client(lambda r: httpx.Response(200, json=QUERY_FILES_SUCCESS_RESPONSE)),
            api_key="fake-key", appid=1623730, query_text=None, sort="bogus", page=1,
        )


async def test_search_workshop_items_empty_results():
    def handler(request):
        return httpx.Response(200, json={"response": {"total": 0}})

    results = await search_workshop_items(
        make_client(handler), api_key="fake-key", appid=1623730,
        query_text="zzzznothingmatches", sort="text", page=1,
    )

    assert results == []


async def test_search_workshop_items_network_error_raises_fetch_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(WorkshopFetchError):
        await search_workshop_items(
            make_client(handler), api_key="fake-key", appid=1623730,
            query_text="building", sort="text", page=1,
        )


async def test_search_workshop_items_unexpected_shape_raises_fetch_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(WorkshopFetchError):
        await search_workshop_items(
            make_client(handler), api_key="fake-key", appid=1623730,
            query_text="building", sort="text", page=1,
        )


async def test_search_workshop_items_malformed_item_missing_id_raises_fetch_error():
    def handler(request):
        return httpx.Response(200, json={"response": {"publishedfiledetails": [{"title": "no id here"}]}})

    with pytest.raises(WorkshopFetchError):
        await search_workshop_items(
            make_client(handler), api_key="fake-key", appid=1623730,
            query_text="building", sort="text", page=1,
        )
