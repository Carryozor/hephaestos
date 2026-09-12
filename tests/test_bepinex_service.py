import asyncio

import bcrypt
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

VALHEIM_MODS_SEED = {
    "valheim": [
        {
            "slug": "ValheimModding/Jotunn", "title": "Jötunn",
            "source": "thunderstore", "owner": "ValheimModding", "package": "Jotunn",
            "target": "plugins", "installed_version": "2.30.0",
            "installed_paths": ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"],
        },
        {
            "slug": "SpikeHimself/XPortal", "title": "XPortal",
            "source": "github_release", "owner": "SpikeHimself", "package": "XPortal",
            "asset_pattern": r"^XPortal-v.*\.zip$", "tag_prefix": "v",
            "target": "plugins", "installed_version": "1.2.25",
            "installed_paths": ["BepInEx/plugins/XPortal"],
        },
    ]
}


def thunderstore_response(version="2.30.0"):
    return httpx.Response(200, json={"latest": {
        "version_number": version,
        "download_url": f"https://thunderstore.io/package/download/ValheimModding/Jotunn/{version}/",
    }})


def github_response(version="1.2.25"):
    return httpx.Response(200, json={
        "tag_name": f"v{version}",
        "assets": [{"name": f"XPortal-v{version}.zip",
                    "browser_download_url": f"https://github.com/SpikeHimself/XPortal/releases/download/v{version}/XPortal-v{version}.zip"}],
    })


def make_app(tmp_path, handler=None, extra_servers=None):
    from app import bepinex as bepinex_service
    bepinex_service._latest_refreshed_at.clear()

    def default_handler(request):
        url = str(request.url)
        if "thunderstore.io" in url:
            return thunderstore_response()
        if "api.github.com" in url:
            return github_response()
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler or default_handler))
    servers = {"valheim": {"display_name": "Valheim", "server_appid": 896660},
               "palworld": {"display_name": "Palworld", "server_appid": 2394010}}
    if extra_servers:
        servers.update(extra_servers)
    settings = Settings(agent_token="agent-t", data_dir=tmp_path, servers=servers,
                        bepinex_mods=VALHEIM_MODS_SEED)
    return create_app(settings, http_client=client)


def make_logged_in_client(tmp_path, handler=None):
    app = make_app(tmp_path, handler=handler)
    password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", password_hash))
    client = TestClient(app)
    assert client.post("/api/login", json={"username": "tester", "password": "testpass123"}).status_code == 200
    return app, client


def poll_orders(client):
    return client.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"})


def test_version_is_newer_numeric_not_lexicographic():
    from app.bepinex import version_is_newer
    assert version_is_newer("2.31.0", "2.30.0") is True
    assert version_is_newer("2.30.0", "2.30.0") is False
    assert version_is_newer("2.9.0", "2.30.0") is False
    assert version_is_newer(None, "2.30.0") is False
    assert version_is_newer("2.30.0", None) is False


def test_version_is_newer_falls_back_to_inequality_when_unparseable():
    from app.bepinex import version_is_newer
    assert version_is_newer("abc", "def") is True
    assert version_is_newer("abc", "abc") is False


def test_get_orders_refreshes_bepinex_versions_and_sets_update_available(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    assert poll_orders(client).status_code == 200
    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    assert entries["ValheimModding/Jotunn"]["latest_version"] == "2.30.0"
    body = client.get("/api/servers").json()
    (valheim,) = [s for s in body["servers"] if s["name"] == "valheim"]
    jotunn = next(m for m in valheim["bepinex_mods"] if m["slug"] == "ValheimModding/Jotunn")
    assert jotunn["update_available"] is False  # 2.30.0 == 2.30.0 installe


def test_get_orders_refresh_detects_new_version(tmp_path):
    def handler(request):
        url = str(request.url)
        if "ValheimModding/Jotunn" in url:
            return thunderstore_response(version="2.31.0")
        if "api.github.com" in url:
            return github_response()
        return httpx.Response(404)

    app, client = make_logged_in_client(tmp_path, handler=handler)
    poll_orders(client)
    body = client.get("/api/servers").json()
    (valheim,) = [s for s in body["servers"] if s["name"] == "valheim"]
    jotunn = next(m for m in valheim["bepinex_mods"] if m["slug"] == "ValheimModding/Jotunn")
    assert jotunn["update_available"] is True
    assert valheim["bepinex_update_count"] == 1


def test_get_orders_refresh_ttl_only_one_network_call_per_mod(tmp_path):
    calls = {"thunderstore": 0}

    def handler(request):
        url = str(request.url)
        if "thunderstore.io" in url:
            calls["thunderstore"] += 1
            return thunderstore_response()
        if "api.github.com" in url:
            return github_response()
        return httpx.Response(404)

    app, client = make_logged_in_client(tmp_path, handler=handler)
    poll_orders(client)
    poll_orders(client)
    assert calls["thunderstore"] == 1


def test_get_orders_refresh_network_failure_keeps_latest_and_sets_error(tmp_path):
    calls = {"n": 0}

    def handler(request):
        url = str(request.url)
        calls["n"] += 1
        if "ValheimModding/Jotunn" in url:
            if calls["n"] == 1:
                return thunderstore_response(version="2.31.0")
            return httpx.Response(500)
        if "api.github.com" in url:
            return github_response()
        return httpx.Response(404)

    from app import bepinex as bepinex_service

    app, client = make_logged_in_client(tmp_path, handler=handler)
    poll_orders(client)
    bepinex_service._latest_refreshed_at.clear()  # force un 2e cycle reseau immediat
    poll_orders(client)
    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    assert entries["ValheimModding/Jotunn"]["latest_version"] == "2.31.0"  # conservee
    assert entries["ValheimModding/Jotunn"]["last_error"] is not None


def test_get_orders_never_fails_when_bepinex_refresh_raises(tmp_path):
    def handler(request):
        raise RuntimeError("panne reseau totale")

    app, client = make_logged_in_client(tmp_path, handler=handler)
    assert poll_orders(client).status_code == 200


def test_list_servers_has_no_bepinex_keys_for_server_without_mods(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    body = client.get("/api/servers").json()
    (palworld,) = [s for s in body["servers"] if s["name"] == "palworld"]
    assert "bepinex_mods" not in palworld
    assert "bepinex_update_count" not in palworld


def test_list_servers_makes_no_network_call_for_bepinex(tmp_path):
    # steamcmd.net (buildid jeu) est un appel legitime preexistant, hors perimetre
    # de ce test -- seul thunderstore.io/api.github.com (verification bepinex) ne
    # doit JAMAIS etre appele par GET /api/servers (cf. D4 du plan).
    def handler(request):
        url = str(request.url)
        if "thunderstore.io" in url or "api.github.com" in url:
            raise AssertionError(f"appel bepinex inattendu sur GET /api/servers: {url}")
        return httpx.Response(200, json={"data": {}})

    app, client = make_logged_in_client(tmp_path, handler=handler)
    assert client.get("/api/servers").status_code == 200
