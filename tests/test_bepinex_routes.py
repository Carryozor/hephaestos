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
            "target": "plugins", "log_plugin_name": "Jotunn", "installed_version": "2.30.0",
            "installed_paths": ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"],
        },
        {
            "slug": "denikson/BepInExPack_Valheim", "title": "BepInExPack Valheim",
            "source": "thunderstore", "owner": "denikson", "package": "BepInExPack_Valheim",
            "target": "root", "installed_version": "5.4.2350",
            "installed_paths": ["BepInEx/core", "winhttp.dll", "doorstop_config.ini"],
        },
    ]
}


def make_app(tmp_path, handler=None, alert_webhook=None, alert_captured=None):
    from app import bepinex as bepinex_service
    bepinex_service._latest_refreshed_at.clear()

    def default_handler(request):
        url = str(request.url)
        if alert_captured is not None and "hooks.example" in url:
            import json as _json
            alert_captured.append({"url": url, "body": _json.loads(request.content.decode())})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"latest": {
            "version_number": "2.31.0",
            "download_url": "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.31.0/",
        }})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler or default_handler))
    settings = Settings(
        agent_token="agent-t", data_dir=tmp_path,
        servers={"valheim": {"display_name": "Valheim", "server_appid": 896660},
                 "palworld": {"display_name": "Palworld", "server_appid": 2394010}},
        bepinex_mods=VALHEIM_MODS_SEED,
        alert_webhook=alert_webhook,
    )
    return create_app(settings, http_client=client)


def make_logged_in_client(tmp_path, handler=None, role="admin", servers=None, username="tester",
                          alert_webhook=None, alert_captured=None):
    app = make_app(tmp_path, handler=handler, alert_webhook=alert_webhook, alert_captured=alert_captured)
    password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user(username, password_hash, role=role, servers=servers or []))
    client = TestClient(app)
    assert client.post("/api/login", json={"username": username, "password": "testpass123"}).status_code == 200
    return app, client


def set_latest(app, server, slug, version="2.31.0", download_url="https://thunderstore.io/package/download/ValheimModding/Jotunn/2.31.0/"):
    asyncio.run(app.state.store.bepinex.set_latest(server, slug, version=version, download_url=download_url))


def test_update_unknown_server_404(tmp_path):
    _, client = make_logged_in_client(tmp_path)
    assert client.post("/api/servers/doom/bepinex/update", json={}).status_code == 404


def test_update_server_without_bepinex_mods_404(tmp_path):
    _, client = make_logged_in_client(tmp_path)
    assert client.post("/api/servers/palworld/bepinex/update", json={}).status_code == 404


def test_update_no_update_available_returns_409(tmp_path):
    _, client = make_logged_in_client(tmp_path)
    r = client.post("/api/servers/valheim/bepinex/update", json={})
    assert r.status_code == 409


def test_update_creates_one_order_with_mods_from_store_not_client(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")

    # download_url malveillant envoye par le client : DOIT etre ignore (P6) --
    # l'ordre ne doit contenir que l'URL connue du store.
    r = client.post("/api/servers/valheim/bepinex/update", json={
        "slugs": ["ValheimModding/Jotunn"],
        "download_url": "https://evil.example.com/payload.zip",
    })
    assert r.status_code == 201
    order = r.json()
    assert order["type"] == "update_bepinex_mods"
    assert len(order["mods"]) == 1
    mod = order["mods"][0]
    assert mod["slug"] == "ValheimModding/Jotunn"
    assert mod["download_url"] == "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.31.0/"
    assert mod["version"] == "2.31.0"
    assert mod["expected_plugin"] == "Jotunn"
    assert mod["previous_paths"] == ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"]


def test_update_root_target_mod_has_no_expected_plugin(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "denikson/BepInExPack_Valheim", version="5.4.2400",
               download_url="https://thunderstore.io/package/download/denikson/BepInExPack_Valheim/5.4.2400/")
    r = client.post("/api/servers/valheim/bepinex/update", json={"slugs": ["denikson/BepInExPack_Valheim"]})
    assert r.status_code == 201
    assert r.json()["mods"][0]["expected_plugin"] is None


def test_update_without_slugs_targets_all_with_update_available(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    r = client.post("/api/servers/valheim/bepinex/update", json={})
    assert r.status_code == 201
    assert [m["slug"] for m in r.json()["mods"]] == ["ValheimModding/Jotunn"]


def test_update_unknown_slug_returns_400(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    r = client.post("/api/servers/valheim/bepinex/update", json={"slugs": ["Nobody/Nothing"]})
    assert r.status_code == 400


def test_update_second_call_conflicts_409(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    assert client.post("/api/servers/valheim/bepinex/update", json={}).status_code == 201
    assert client.post("/api/servers/valheim/bepinex/update", json={}).status_code == 409


def test_update_forbidden_for_non_admin_role(tmp_path):
    app, client = make_logged_in_client(tmp_path, role="user", servers=["valheim"])
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    assert client.post("/api/servers/valheim/bepinex/update", json={}).status_code == 403


def test_check_forces_refresh_bypassing_ttl(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"latest": {
            "version_number": "2.31.0",
            "download_url": "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.31.0/",
        }})

    app, client = make_logged_in_client(tmp_path, handler=handler)
    r1 = client.post("/api/servers/valheim/bepinex/check")
    assert r1.status_code == 200
    r2 = client.post("/api/servers/valheim/bepinex/check")
    assert r2.status_code == 200
    # 2 mods suivis (Jotunn + BepInExPack) x 2 appels /check = 4, aucun TTL ne doit
    # bloquer un appel explicite admin (contrairement au poll agent automatique).
    assert calls["n"] == 4
    body = r2.json()
    jotunn = next(m for m in body["bepinex_mods"] if m["slug"] == "ValheimModding/Jotunn")
    assert jotunn["latest_version"] == "2.31.0"


AGT = {"Authorization": "Bearer agent-t"}


def test_agent_report_done_persists_installed_version_and_paths(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    oid = client.post("/api/servers/valheim/bepinex/update", json={}).json()["id"]

    r = client.post(f"/api/agent/orders/{oid}", headers=AGT, json={
        "status": "done", "detail": "mise a jour reussie",
        "bepinex_installed": [{"slug": "ValheimModding/Jotunn", "version": "2.31.0",
                               "paths": ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"]}],
    })
    assert r.status_code == 200

    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    jotunn = entries["ValheimModding/Jotunn"]
    assert jotunn["installed_version"] == "2.31.0"
    assert jotunn["installed_paths"] == ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"]
    # update_available doit retomber a faux : installed_version a rattrape latest_version
    body = client.get("/api/servers").json()
    valheim = next(s for s in body["servers"] if s["name"] == "valheim")
    jotunn_ui = next(m for m in valheim["bepinex_mods"] if m["slug"] == "ValheimModding/Jotunn")
    assert jotunn_ui["update_available"] is False


def test_agent_report_done_with_invalid_path_is_ignored_not_500(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    oid = client.post("/api/servers/valheim/bepinex/update", json={}).json()["id"]

    r = client.post(f"/api/agent/orders/{oid}", headers=AGT, json={
        "status": "done", "detail": "x",
        "bepinex_installed": [{"slug": "ValheimModding/Jotunn", "version": "2.31.0",
                               "paths": ["../../evil.dll"]}],
    })
    assert r.status_code == 200
    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    # chemin invalide -> entree ignoree, l'ancienne version installee reste en place
    assert entries["ValheimModding/Jotunn"]["installed_version"] == "2.30.0"


def test_agent_report_done_ignores_slug_outside_the_order(tmp_path):
    """Revue securite 12/09 (M2) : un agent qui rapporterait un slug HORS de
    l'ordre traite ne doit jamais pouvoir ecraser le bookkeeping d'un AUTRE mod
    (qui serait ensuite repasse en previous_paths a une future suppression)."""
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    oid = client.post("/api/servers/valheim/bepinex/update", json={}).json()["id"]

    r = client.post(f"/api/agent/orders/{oid}", headers=AGT, json={
        "status": "done", "detail": "x",
        "bepinex_installed": [{"slug": "denikson/BepInExPack_Valheim", "version": "9.9.9",
                               "paths": ["BepInEx/core"]}],
    })
    assert r.status_code == 200
    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    # le mod HORS ordre n'a pas ete touche
    assert entries["denikson/BepInExPack_Valheim"]["installed_version"] == "5.4.2350"


def test_agent_report_failed_does_not_touch_installed_version(tmp_path):
    app, client = make_logged_in_client(tmp_path)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    oid = client.post("/api/servers/valheim/bepinex/update", json={}).json()["id"]

    client.post(f"/api/agent/orders/{oid}", headers=AGT, json={
        "status": "failed", "detail": "verification post-demarrage echouee",
    })
    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    assert entries["ValheimModding/Jotunn"]["installed_version"] == "2.30.0"


def test_agent_report_done_but_installed_version_not_updated_triggers_alert(tmp_path):
    """Incident reel du 18/09/2026 : l'agent a rapporte 'done' (detail correct,
    3 mods listes) sur update_bepinex_mods, mais installed_version n'a PAS ete
    persiste pour ces mods -- root cause jamais reproduite avec certitude malgre
    une investigation approfondie (le meme payload rejoue manuellement fonctionne
    a chaque fois). Filet de securite : un rapport 'done' dont le contenu reel du
    store ne correspond pas a la version demandee par l'ordre doit etre visible
    (alerte webhook), jamais silencieux -- l'agent est une source non fiable (P5/P6),
    et un ecart ici laisse le dashboard mentir indefiniment sur l'etat reel des mods."""
    captured = []
    app, client = make_logged_in_client(
        tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", alert_captured=captured)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    oid = client.post("/api/servers/valheim/bepinex/update", json={}).json()["id"]

    # bepinex_installed vide malgre un statut "done" -- reproduit la classe du
    # bug observe (rapport de succes sans le contenu attendu), quelle qu'en soit
    # la cause reelle chez l'agent.
    r = client.post(f"/api/agent/orders/{oid}", headers=AGT, json={
        "status": "done", "detail": "mise a jour reussie: ValheimModding/Jotunn -> 2.31.0",
        "bepinex_installed": [],
    })
    assert r.status_code == 200

    entries = asyncio.run(app.state.store.bepinex.all("valheim"))
    assert entries["ValheimModding/Jotunn"]["installed_version"] == "2.30.0"

    assert len(captured) == 1
    body = captured[0]["body"]["content"]
    assert "valheim" in body and "ValheimModding/Jotunn" in body


def test_agent_report_done_with_matching_installed_version_triggers_no_alert(tmp_path):
    """Non-regression : le chemin nominal (deja teste plus haut) ne doit pas
    declencher l'alerte de derive ajoutee ci-dessus."""
    captured = []
    app, client = make_logged_in_client(
        tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", alert_captured=captured)
    set_latest(app, "valheim", "ValheimModding/Jotunn")
    oid = client.post("/api/servers/valheim/bepinex/update", json={}).json()["id"]

    r = client.post(f"/api/agent/orders/{oid}", headers=AGT, json={
        "status": "done", "detail": "mise a jour reussie",
        "bepinex_installed": [{"slug": "ValheimModding/Jotunn", "version": "2.31.0",
                               "paths": ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"]}],
    })
    assert r.status_code == 200
    assert captured == []
