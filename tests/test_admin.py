import asyncio
from datetime import UTC

import bcrypt
import httpx
from fastapi.testclient import TestClient

from tests.test_sanity import make_app, make_logged_in_client


def make_client(tmp_path):
    return make_logged_in_client(tmp_path)

def make_app_and_client(tmp_path, username="tester", password="testpass123"):
    """Comme make_logged_in_client, mais expose aussi l'app (necessaire pour
    manipuler store.registry directement dans le test)."""
    app = make_app(tmp_path)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user(username, password_hash))
    client = TestClient(app)
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return app, client

def test_list_servers_empty_state(tmp_path):
    r = make_client(tmp_path).get("/api/servers")
    assert r.status_code == 200
    (srv,) = r.json()["servers"]
    assert srv["name"] == "palworld" and srv["state"] is None and srv["update_available"] is None
    assert srv["public_buildid"] == "24088465"

def test_update_order_created_then_conflict(tmp_path):
    c = make_client(tmp_path)
    assert c.post("/api/servers/palworld/update").status_code == 201
    assert c.post("/api/servers/palworld/update").status_code == 409
    assert c.post("/api/servers/palworld/restart").status_code == 201

def test_start_stop_orders_created_then_conflict(tmp_path):
    c = make_client(tmp_path)
    assert c.post("/api/servers/palworld/start").status_code == 201
    assert c.post("/api/servers/palworld/start").status_code == 409
    assert c.post("/api/servers/palworld/stop").status_code == 201

def test_unknown_server_404_and_auth(tmp_path):
    c = make_client(tmp_path)
    assert c.post("/api/servers/doom/update").status_code == 404
    anon = TestClient(make_app(tmp_path))
    assert anon.get("/api/servers").status_code == 401


def test_get_players_returns_empty_list_when_no_sessions(tmp_path):
    c = make_client(tmp_path)
    res = c.get("/api/servers/palworld/players")
    assert res.status_code == 200
    assert res.json() == {"players": []}


def test_get_players_returns_connected_since_seconds(tmp_path):
    import json
    from datetime import datetime, timedelta

    c = make_client(tmp_path)
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    state_path = tmp_path / "state.json"
    data = json.loads(state_path.read_text())
    data.setdefault("player_sessions", {})["palworld"] = {
        "123": {"name": "Alice", "steamid": "765611", "first_seen": past}
    }
    state_path.write_text(json.dumps(data))

    res = c.get("/api/servers/palworld/players")
    assert res.status_code == 200
    body = res.json()["players"]
    assert body[0]["name"] == "Alice"
    assert body[0]["steamid"] == "765611"
    assert body[0]["connected_since_seconds"] >= 120


def test_get_players_unknown_server_404(tmp_path):
    c = make_client(tmp_path)
    assert c.get("/api/servers/doom/players").status_code == 404


def test_server_detail_unknown_server_404(tmp_path):
    c = make_client(tmp_path)
    assert c.get("/api/servers/doom/detail").status_code == 404


def test_server_detail_empty_state(tmp_path):
    c = make_client(tmp_path)
    r = c.get("/api/servers/palworld/detail")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "rcon_info": None,
        "uptime_seconds": None,
        "process": {"cpu_percent": None, "mem_mb": None},
        "players": [],
        "playtime_totals": [],
        "connection_log": [],
        "save_backups": [],
        "order_history": [],
        "files_listing": {},
        "file_read": None,
    }


def test_server_detail_uptime_computed_when_process_up(tmp_path):
    c = make_client(tmp_path)
    from datetime import datetime, timedelta
    started = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    c.post(
        "/api/agent/state",
        headers={"Authorization": "Bearer agent-t"},
        json={"servers": {"palworld": {"process_up": True, "process_started_at": started}}},
    )
    body = c.get("/api/servers/palworld/detail").json()
    assert body["uptime_seconds"] >= 599  # ~10 min, marge pour le temps d'execution du test


def test_server_detail_uptime_null_when_process_down(tmp_path):
    c = make_client(tmp_path)
    from datetime import datetime
    started = datetime.now(UTC).isoformat()
    c.post(
        "/api/agent/state",
        headers={"Authorization": "Bearer agent-t"},
        json={"servers": {"palworld": {"process_up": False, "process_started_at": started}}},
    )
    body = c.get("/api/servers/palworld/detail").json()
    assert body["uptime_seconds"] is None


def test_server_detail_players_and_playtime(tmp_path):
    c = make_client(tmp_path)
    c.post(
        "/api/agent/state",
        headers={"Authorization": "Bearer agent-t"},
        json={"servers": {"palworld": {"players": 1, "players_list": [
            {"id": "1", "name": "Alice", "steamid": "765"}
        ]}}},
    )
    body = c.get("/api/servers/palworld/detail").json()
    assert body["players"] == [{"id": "1", "name": "Alice", "steamid": "765", "connected_since_seconds": body["players"][0]["connected_since_seconds"]}]
    assert body["connection_log"][0]["name"] == "Alice"

    c.post(
        "/api/agent/state",
        headers={"Authorization": "Bearer agent-t"},
        json={"servers": {"palworld": {"players": 0, "players_list": []}}},
    )
    body = c.get("/api/servers/palworld/detail").json()
    assert body["playtime_totals"][0]["player_key"] == "765"
    assert body["playtime_totals"][0]["name"] == "Alice"
    assert body["playtime_totals"][0]["total_seconds"] >= 0


def make_client_with_workshop(tmp_path, workshop_result=None, steam_api_key=None, workshop_handler=None):
    # le cache TTL de backfill est module-level : sans reset, un echec enregistre par un
    # test contamine les suivants (meme cle name+workshop_id)
    from app import mods as mods_service
    mods_service._backfill_failed_at.clear()
    mods_service._steam_dates_refreshed_at.clear()
    import asyncio

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    result = workshop_result or {
        "result": 1, "consumer_app_id": 1623730, "title": "Cool Mod",
        "preview_url": "https://x/thumb.jpg", "file_description": "une description",
        "time_updated": 1752400000,
    }

    def default_handler(request):
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}})
        if "GetPublishedFileDetails" in url:
            return httpx.Response(200, json={"response": {"publishedfiledetails": [result]}})
        if "QueryFiles" in url:
            return httpx.Response(200, json={"response": {"total": 0}})
        return httpx.Response(404)

    handler = workshop_handler or default_handler

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        agent_token="agent-t", data_dir=tmp_path,
        servers={"palworld": {"display_name": "Palworld", "server_appid": 2394010, "workshop_appid": 1623730}},
        steam_api_key=steam_api_key,
    )
    app = create_app(settings, http_client=client)
    password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", password_hash))
    c = TestClient(app)
    r = c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    assert r.status_code == 200
    return c


def test_mods_preview_success(tmp_path):
    c = make_client_with_workshop(tmp_path)
    res = c.post("/api/servers/palworld/mods/preview", json={"workshop_ref": "3147025543"})
    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "Cool Mod"
    assert body["workshop_id"] == "3147025543"


def test_mods_preview_wrong_game_returns_400(tmp_path):
    c = make_client_with_workshop(tmp_path, workshop_result={
        "result": 1, "consumer_app_id": 896660, "title": "Mod Valheim",
    })
    res = c.post("/api/servers/palworld/mods/preview", json={"workshop_ref": "999"})
    assert res.status_code == 400


def test_mods_preview_not_found_returns_404(tmp_path):
    c = make_client_with_workshop(tmp_path, workshop_result={"result": 9})
    res = c.post("/api/servers/palworld/mods/preview", json={"workshop_ref": "999"})
    assert res.status_code == 404


def test_mods_preview_404_when_no_workshop_appid(tmp_path):
    c = make_client(tmp_path)  # fixture existante du fichier, sans workshop_appid
    res = c.post("/api/servers/palworld/mods/preview", json={"workshop_ref": "999"})
    assert res.status_code == 404


def test_mods_install_creates_order_and_metadata(tmp_path):
    c = make_client_with_workshop(tmp_path)
    res = c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
    })
    assert res.status_code == 201
    assert res.json()["type"] == "install_mod"
    assert res.json()["workshop_id"] == "3147025543"

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    assert palworld["pending_orders"] == ["install_mod"]


def test_mods_install_revalidates_server_side_even_with_falsified_payload(tmp_path):
    # Le client pretend que le mod est valide pour Palworld, mais le serveur Steam
    # (mocke) repond un consumer_app_id different -- le backend doit rejeter malgre
    # le payload client, jamais lui faire confiance aveuglement.
    c = make_client_with_workshop(tmp_path, workshop_result={
        "result": 1, "consumer_app_id": 896660, "title": "Mod Valheim",
    })
    res = c.post("/api/servers/palworld/mods", json={
        "workshop_id": "999", "title": "Cool Mod (mensonger)", "thumbnail_url": "https://x/thumb.jpg",
    })
    assert res.status_code == 400


def test_mods_install_duplicate_pending_returns_409(tmp_path):
    c = make_client_with_workshop(tmp_path)
    body = {"workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg"}
    assert c.post("/api/servers/palworld/mods", json=body).status_code == 201
    assert c.post("/api/servers/palworld/mods", json=body).status_code == 409


def test_mods_remove_creates_order(tmp_path):
    c = make_client_with_workshop(tmp_path)
    res = c.delete("/api/servers/palworld/mods/3147025543")
    assert res.status_code == 201
    assert res.json()["type"] == "remove_mod"
    assert res.json()["workshop_id"] == "3147025543"


def test_mods_remove_purges_metadata_immediately_for_mod_never_confirmed_installed(tmp_path):
    # Regression : annuler un install qui n'a jamais atteint le disque (steamcmd
    # echoue, ou annulation avant que l'agent n'ait traite l'ordre install_mod) doit
    # nettoyer les metadonnees tout de suite -- sinon elles restent orphelines pour
    # toujours (update_mods_state ne purge que les IDs deja confirmes installes).
    c = make_client_with_workshop(tmp_path)
    c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
    })

    c.delete("/api/servers/palworld/mods/3147025543")

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    assert palworld["mods"] == []


def test_mods_remove_does_not_purge_metadata_for_mod_currently_installed(tmp_path):
    # Un mod REELLEMENT installe ne doit pas perdre son titre pendant la fenetre ou
    # l'ordre remove_mod est encore en attente de traitement par l'agent -- la purge
    # normale (via update_mods_state) se fera au prochain rapport d'etat confirmant
    # le retrait reel.
    c = make_client_with_workshop(tmp_path)
    c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
    })
    agent_headers = {"Authorization": "Bearer agent-t"}
    c.post("/api/agent/state", headers=agent_headers, json={
        "servers": {"palworld": {"buildid": "100", "process_up": True, "players": 0, "installed_mod_ids": ["3147025543"]}}
    })

    c.delete("/api/servers/palworld/mods/3147025543")

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    (mod,) = palworld["mods"]
    assert mod["installed_at"] is not None
    mod.pop("installed_at")
    assert mod == {"workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
                   "installed": True, "steam_updated_at": "2025-07-13T09:46:40+00:00", "update_available": False}


def test_mods_remove_duplicate_pending_returns_409(tmp_path):
    c = make_client_with_workshop(tmp_path)
    assert c.delete("/api/servers/palworld/mods/3147025543").status_code == 201
    assert c.delete("/api/servers/palworld/mods/3147025543").status_code == 409


def test_list_servers_exposes_mods_info(tmp_path):
    c = make_client_with_workshop(tmp_path)
    c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
    })
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    data["mods_state"] = {"palworld": {"installed_mod_ids": ["3147025543"], "changed_at": "2026-07-14T10:00:00+00:00"}}
    (tmp_path / "state.json").write_text(json.dumps(data))

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    assert palworld["workshop_appid"] == 1623730
    (mod,) = palworld["mods"]
    assert mod == {"workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
                   "installed": True, "installed_at": None, "steam_updated_at": "2025-07-13T09:46:40+00:00", "update_available": False}
    assert palworld["mods_restart_required"] is True  # process_started_at absent (jamais demarre) -> requis


def test_list_servers_shows_mod_installed_via_preview_after_pending_then_reported(tmp_path):
    # Regression : reproduit exactement le flux reel -- POST /mods (metadata posee),
    # PUIS un rapport d'etat agent qui NE contient PAS encore le mod (ordre pas encore
    # traite), PUIS un second rapport qui le contient (mod installe). Le mod ne doit
    # jamais disparaitre de la liste entre les deux rapports.
    c = make_client_with_workshop(tmp_path)
    c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
    })

    agent_headers = {"Authorization": "Bearer agent-t"}
    c.post("/api/agent/state", headers=agent_headers, json={
        "servers": {"palworld": {"buildid": "100", "process_up": True, "players": 0, "installed_mod_ids": []}}
    })

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    (mod,) = palworld["mods"]
    assert mod == {"workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
                   "installed": False, "installed_at": None, "steam_updated_at": "2025-07-13T09:46:40+00:00", "update_available": False}

    c.post("/api/agent/state", headers=agent_headers, json={
        "servers": {"palworld": {"buildid": "100", "process_up": True, "players": 0, "installed_mod_ids": ["3147025543"]}}
    })

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    (mod,) = palworld["mods"]
    assert mod["installed_at"] is not None
    mod.pop("installed_at")
    assert mod == {"workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
                   "installed": True, "steam_updated_at": "2025-07-13T09:46:40+00:00", "update_available": False}


def test_list_servers_resolves_title_for_mod_installed_without_metadata(tmp_path):
    # Un mod present sur disque mais jamais passe par /mods (installe manuellement hors
    # dashboard, ou metadonnees perdues) apparait avec son VRAI titre, backfille depuis
    # l'API Steam (changement 15/07 : avant, titre generique "Mod <id>" pour toujours).
    c = make_client_with_workshop(tmp_path)
    agent_headers = {"Authorization": "Bearer agent-t"}
    c.post("/api/agent/state", headers=agent_headers, json={
        "servers": {"palworld": {"buildid": "100", "process_up": True, "players": 0, "installed_mod_ids": ["999"]}}
    })

    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    (mod,) = palworld["mods"]
    assert mod["installed_at"] is not None
    mod.pop("installed_at")
    assert mod == {"workshop_id": "999", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
                   "installed": True, "steam_updated_at": "2025-07-13T09:46:40+00:00", "update_available": False}


def _write_installed_mod(tmp_path, installed_at):
    # Pose directement un mod installe avec metadonnees completes : title present ->
    # pas de backfill, et steam_updated_at pose -> independant du refresh batche
    # (le stub par defaut renvoie un item sans publishedfileid, que le bulk ignore).
    # Les installed_at des tests se placent avant/apres la date Steam du 2025-07-13.
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    data["mods_state"] = {"palworld": {"installed_mod_ids": ["3147025543"], "changed_at": None}}
    data["mods_metadata"] = {"palworld": {"3147025543": {
        "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
        "installed_at": installed_at, "steam_updated_at": "2025-07-13T09:46:40+00:00"}}}
    (tmp_path / "state.json").write_text(json.dumps(data))


def _get_single_mod(c):
    servers = c.get("/api/servers").json()["servers"]
    (mod,) = next(s for s in servers if s["name"] == "palworld")["mods"]
    return mod


def test_mod_update_available_when_steam_date_newer_than_install(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_installed_mod(tmp_path, installed_at="2025-07-01T00:00:00+00:00")
    assert _get_single_mod(c)["update_available"] is True


def test_mod_update_not_available_when_install_newer_than_steam_date(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_installed_mod(tmp_path, installed_at="2026-01-01T00:00:00+00:00")
    assert _get_single_mod(c)["update_available"] is False


def test_mod_update_not_available_when_installed_at_unknown(tmp_path):
    # mods installes avant la feature installed_at : pas de date de reference ->
    # jamais de badge (pas de faux positif permanent)
    c = make_client_with_workshop(tmp_path)
    _write_installed_mod(tmp_path, installed_at=None)
    assert _get_single_mod(c)["update_available"] is False


def test_mod_update_not_available_when_not_installed(tmp_path):
    # mod en attente d'installation (ordre pas encore traite) : pas de badge
    c = make_client_with_workshop(tmp_path)
    c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
    })
    mod = _get_single_mod(c)
    assert mod["installed"] is False
    assert mod["update_available"] is False


def _write_mods_fleet(tmp_path, process_up=True, players=0):
    # 3 mods installes : "1" a jour, "2" maj dispo, "3" sans installed_at (pre-feature)
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    data["servers"] = {"palworld": {"process_up": process_up, "players": players,
                                    "last_seen": "2026-07-16T00:00:00+00:00"}}
    data["mods_state"] = {"palworld": {"installed_mod_ids": ["1", "2", "3"], "changed_at": None}}
    data["mods_metadata"] = {"palworld": {
        "1": {"title": "AJour", "thumbnail_url": "", "installed_at": "2026-07-16T00:00:00+00:00",
              "steam_updated_at": "2026-07-01T00:00:00+00:00"},
        "2": {"title": "Perime", "thumbnail_url": "", "installed_at": "2026-07-01T00:00:00+00:00",
              "steam_updated_at": "2026-07-15T00:00:00+00:00"},
        "3": {"title": "Legacy", "thumbnail_url": "", "installed_at": None,
              "steam_updated_at": "2026-07-15T00:00:00+00:00"},
    }}
    (tmp_path / "state.json").write_text(json.dumps(data))


def _pending(c):
    return [(o["type"], o.get("workshop_id")) for o in
            c.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"}).json()["orders"]]


def test_update_all_mods_enqueues_outdated_and_unknown_then_restart(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path)
    r = c.post("/api/servers/palworld/mods/update-all")
    assert r.status_code == 200
    assert r.json() == {"orders_created": 2, "restart": True}
    # ordre FIFO : les installs PUIS le restart (le drainage agent les traite dans l'ordre)
    assert _pending(c) == [("install_mod", "2"), ("install_mod", "3"), ("restart", None)]


def test_update_all_mods_no_restart_when_process_down(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path, process_up=False)
    r = c.post("/api/servers/palworld/mods/update-all")
    assert r.json() == {"orders_created": 2, "restart": False}
    assert ("restart", None) not in _pending(c)


def test_update_all_mods_skips_mod_with_pending_install(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path)
    c.post("/api/servers/palworld/mods", json={"workshop_id": "2", "title": "x", "thumbnail_url": "x"})
    r = c.post("/api/servers/palworld/mods/update-all")
    assert r.json()["orders_created"] == 1  # seul "3" ajoute, "2" deja en attente
    assert [t for t, w in _pending(c) if t == "install_mod"].count("install_mod") == 2


def test_update_all_mods_nothing_to_do(tmp_path):
    import json
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path)
    data = json.loads((tmp_path / "state.json").read_text())
    data["mods_state"]["palworld"]["installed_mod_ids"] = ["1"]
    (tmp_path / "state.json").write_text(json.dumps(data))
    assert c.post("/api/servers/palworld/mods/update-all").json() == {"orders_created": 0, "restart": False}
    assert _pending(c) == []


def test_update_all_mods_404_without_workshop(tmp_path):
    assert make_client(tmp_path).post("/api/servers/palworld/mods/update-all").status_code == 404


def test_agent_poll_auto_enqueues_mod_updates_when_server_empty(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path, players=0)
    # le GET agent declenche l'auto-enqueue : mod perime + restart, PAS le mod sans
    # installed_at (etat inconnu = re-basage manuel uniquement)
    assert _pending(c) == [("install_mod", "2"), ("restart", None)]
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["mods_auto"]["palworld"]  # cooldown pose


def test_agent_poll_no_auto_update_when_players_present_or_unknown(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path, players=3)
    assert _pending(c) == []
    _write_mods_fleet(tmp_path, players=None)  # Valheim-like : jamais prouve vide
    assert _pending(c) == []


def test_agent_poll_no_auto_update_when_pending_order_exists(tmp_path):
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path)
    c.post("/api/servers/palworld/stop")
    assert [t for t, _ in _pending(c) if t == "install_mod"] == []


def test_agent_poll_auto_update_respects_cooldown(tmp_path):
    import json
    from datetime import datetime
    c = make_client_with_workshop(tmp_path)
    _write_mods_fleet(tmp_path)
    data = json.loads((tmp_path / "state.json").read_text())
    data["mods_auto"] = {"palworld": datetime.now(UTC).isoformat()}
    (tmp_path / "state.json").write_text(json.dumps(data))
    assert _pending(c) == []


def _write_game_state(tmp_path, buildid="100", players=0, process_up=True):
    # STEAM_JSON (test_sanity.py) renvoie toujours le buildid public "24088465" pour
    # l'appid palworld (2394010) : "100" simule une MAJ dispo, "24088465" = a jour.
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    data["servers"] = {"palworld": {"buildid": buildid, "process_up": process_up, "players": players,
                                    "last_seen": "2026-07-17T00:00:00+00:00"}}
    (tmp_path / "state.json").write_text(json.dumps(data))


def test_agent_poll_auto_enqueues_game_update_when_outdated_and_empty(tmp_path):
    import json
    c = make_client(tmp_path)
    _write_game_state(tmp_path, buildid="100", players=0)
    assert [t for t, _ in _pending(c)] == ["update"]
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["game_auto"]["palworld"]  # cooldown pose


def test_agent_poll_no_game_auto_update_when_up_to_date(tmp_path):
    c = make_client(tmp_path)
    _write_game_state(tmp_path, buildid="24088465", players=0)
    assert _pending(c) == []


def test_agent_poll_no_game_auto_update_when_buildid_unknown(tmp_path):
    c = make_client(tmp_path)
    _write_game_state(tmp_path, buildid=None, players=0)
    assert _pending(c) == []


def test_agent_poll_no_game_auto_update_when_players_present_or_unknown(tmp_path):
    c = make_client(tmp_path)
    _write_game_state(tmp_path, buildid="100", players=3)
    assert _pending(c) == []
    _write_game_state(tmp_path, buildid="100", players=None)
    assert _pending(c) == []


def test_agent_poll_no_game_auto_update_when_pending_order_exists(tmp_path):
    c = make_client(tmp_path)
    _write_game_state(tmp_path, buildid="100", players=0)
    c.post("/api/servers/palworld/stop")
    assert [t for t, _ in _pending(c) if t == "update"] == []


def test_agent_poll_game_auto_update_respects_cooldown(tmp_path):
    import json
    from datetime import datetime
    c = make_client(tmp_path)
    _write_game_state(tmp_path, buildid="100", players=0)
    data = json.loads((tmp_path / "state.json").read_text())
    data["game_auto"] = {"palworld": datetime.now(UTC).isoformat()}
    (tmp_path / "state.json").write_text(json.dumps(data))
    assert _pending(c) == []


def test_agent_poll_auto_enqueues_updates_for_all_eligible_servers(tmp_path):
    # Pas de "un seul par cycle" contrairement a l'ancien bloc PS : deux serveurs
    # eligibles en meme temps recoivent chacun un ordre update (draines ensuite
    # sequentiellement par l'agent dans le meme cycle de 2 min).
    import asyncio
    import json

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    def handler(request):
        appid = request.url.path.rsplit("/", 1)[-1]
        public = {"2394010": "24088465", "4129620": "999"}.get(appid, "0")
        return httpx.Response(200, json={"data": {appid: {"depots": {"branches": {"public": {"buildid": public}}}}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(agent_token="agent-t", data_dir=tmp_path, servers={
        "palworld": {"display_name": "Palworld", "server_appid": 2394010},
        "windrose": {"display_name": "Windrose", "server_appid": 4129620},
    })
    app = create_app(settings, http_client=http_client)
    password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", password_hash))
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})

    data = json.loads((tmp_path / "state.json").read_text())
    data["servers"] = {
        "palworld": {"buildid": "100", "process_up": True, "players": 0, "last_seen": "2026-07-17T00:00:00+00:00"},
        "windrose": {"buildid": "1", "process_up": True, "players": 0, "last_seen": "2026-07-17T00:00:00+00:00"},
    }
    (tmp_path / "state.json").write_text(json.dumps(data))

    orders = c.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"}).json()["orders"]
    assert sorted((o["server"], o["type"]) for o in orders) == [("palworld", "update"), ("windrose", "update")]


def test_list_servers_no_workshop_appid_field_when_not_configured(tmp_path):
    c = make_client(tmp_path)
    servers = c.get("/api/servers").json()["servers"]
    palworld = next(s for s in servers if s["name"] == "palworld")
    assert "workshop_appid" not in palworld


def test_mods_search_requires_workshop_appid(tmp_path):
    c = make_client(tmp_path)  # sans workshop_appid
    res = c.get("/api/servers/palworld/mods/search?q=building")
    assert res.status_code == 404


def test_mods_search_503_when_no_api_key(tmp_path):
    c = make_client_with_workshop(tmp_path)  # steam_api_key absent par defaut
    res = c.get("/api/servers/palworld/mods/search?q=building")
    assert res.status_code == 503


def test_mods_search_returns_results_when_configured(tmp_path):
    c = make_client_with_workshop(tmp_path, steam_api_key="fake-key")
    res = c.get("/api/servers/palworld/mods/search?q=building&sort=text&page=1")
    assert res.status_code == 200
    body = res.json()
    assert "results" in body


def test_mods_search_502_on_steam_error(tmp_path):
    def workshop_handler(request):
        return httpx.Response(500)

    c = make_client_with_workshop(tmp_path, steam_api_key="fake-key", workshop_handler=workshop_handler)
    res = c.get("/api/servers/palworld/mods/search?q=building")
    assert res.status_code == 502


def test_cancel_order_endpoint(tmp_path):
    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    r = c.delete(f"/api/servers/palworld/orders/{oid}")
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    # la place est liberee : un nouvel ordre update passe
    assert c.post("/api/servers/palworld/update").status_code == 201


def test_cancel_order_endpoint_409_when_running(tmp_path):
    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    c.post(f"/api/agent/orders/{oid}", headers={"Authorization": "Bearer agent-t"}, json={"status": "running"})
    assert c.delete(f"/api/servers/palworld/orders/{oid}").status_code == 409


def test_cancel_order_endpoint_404s(tmp_path):
    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    assert c.delete("/api/servers/palworld/orders/inconnu").status_code == 404
    assert c.delete(f"/api/servers/doom/orders/{oid}").status_code == 404


def test_stale_order_expiry_unblocks_new_order(tmp_path):
    import json as _json
    from datetime import datetime, timedelta

    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    state_path = tmp_path / "state.json"
    data = _json.loads(state_path.read_text())
    for o in data["orders"]:
        if o["id"] == oid:
            o["created"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    state_path.write_text(_json.dumps(data))
    # l'ordre fantome est expire automatiquement : plus de 409
    assert c.post("/api/servers/palworld/update").status_code == 201


# --- health public (sonde Kuma, sans auth) ---

def _post_state(c, process_up=True):
    return c.post("/api/agent/state", headers={"Authorization": "Bearer agent-t"},
                  json={"servers": {"palworld": {"buildid": "1", "process_up": process_up}}})


def test_public_health_up(tmp_path):
    c = make_client(tmp_path)
    _post_state(c, process_up=True)
    r = c.get("/api/public/health/palworld")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_public_health_no_auth_required(tmp_path):
    c = make_client(tmp_path)
    _post_state(c, process_up=True)
    anon = TestClient(make_app(tmp_path))
    assert anon.get("/api/public/health/palworld").status_code == 200


def test_public_health_down_when_process_down_or_no_state(tmp_path):
    c = make_client(tmp_path)
    assert c.get("/api/public/health/palworld").status_code == 503  # jamais vu
    _post_state(c, process_up=False)
    assert c.get("/api/public/health/palworld").status_code == 503


def test_public_health_down_when_agent_stale(tmp_path):
    import json as _json
    from datetime import datetime, timedelta

    c = make_client(tmp_path)
    _post_state(c, process_up=True)
    state_path = tmp_path / "state.json"
    data = _json.loads(state_path.read_text())
    data["servers"]["palworld"]["last_seen"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    state_path.write_text(_json.dumps(data))
    r = c.get("/api/public/health/palworld")
    assert r.status_code == 503
    assert "stale" in r.json()["reason"]


def test_public_health_unknown_server_404(tmp_path):
    c = make_client(tmp_path)
    assert c.get("/api/public/health/doom").status_code == 404


# --- file d'attente des ordres exposee a l'UI ---

def test_list_servers_exposes_order_queue_positions(tmp_path):
    c = make_client(tmp_path)
    o1 = c.post("/api/servers/palworld/update").json()
    o2 = c.post("/api/servers/palworld/restart").json()
    (srv,) = c.get("/api/servers").json()["servers"]
    queue = srv["order_queue"]
    assert [(q["id"], q["position"], q["total"]) for q in queue] == [(o1["id"], 1, 2), (o2["id"], 2, 2)]
    assert queue[0]["type"] == "update" and queue[0]["status"] == "pending"


def test_auto_update_blocked_when_players_unknown(tmp_path):
    c = make_client(tmp_path)
    # buildid local != public (24088465) et players inconnu -> MAJ auto impossible
    c.post("/api/agent/state", headers={"Authorization": "Bearer agent-t"},
           json={"servers": {"palworld": {"buildid": "1", "process_up": True}}})
    (srv,) = c.get("/api/servers").json()["servers"]
    assert srv["update_available"] is True
    assert srv["auto_update_blocked"] is True


def test_auto_update_not_blocked_when_players_known_or_up_to_date(tmp_path):
    c = make_client(tmp_path)
    c.post("/api/agent/state", headers={"Authorization": "Bearer agent-t"},
           json={"servers": {"palworld": {"buildid": "1", "process_up": True, "players": 0}}})
    (srv,) = c.get("/api/servers").json()["servers"]
    assert srv["auto_update_blocked"] is False
    c.post("/api/agent/state", headers={"Authorization": "Bearer agent-t"},
           json={"servers": {"palworld": {"buildid": "24088465", "process_up": True}}})
    (srv,) = c.get("/api/servers").json()["servers"]
    assert srv["auto_update_blocked"] is False


# --- backfill des metadonnees de mods manquantes (regression 15/07 : IDs affiches) ---

def _install_mods_state(c, ids):
    """Simule le rapport agent : mods presents sur disque, sans passer par install_mod."""
    return c.post("/api/agent/state", headers={"Authorization": "Bearer agent-t"},
                  json={"servers": {"palworld": {"buildid": "1", "process_up": True,
                                                 "installed_mod_ids": ids}}})


def test_installed_mod_without_metadata_is_backfilled_from_steam(tmp_path):
    """Un mod installe dont les metadonnees ont ete perdues (purge, perte d'etat) doit
    retrouver titre+vignette via l'API Steam au lieu d'afficher 'Mod <id>' pour toujours."""
    calls = {"n": 0}

    def handler(request):
        import httpx
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}})
        if "GetPublishedFileDetails" in url:
            calls["n"] += 1
            return httpx.Response(200, json={"response": {"publishedfiledetails": [
                {"result": 1, "consumer_app_id": 1623730, "publishedfileid": "42",
                 "title": "Mod Retrouve", "preview_url": "https://x/thumb.jpg"}]}})
        return httpx.Response(404)

    c = make_client_with_workshop(tmp_path, workshop_handler=handler)
    _install_mods_state(c, ["42"])

    (srv,) = c.get("/api/servers").json()["servers"]
    (mod,) = srv["mods"]
    assert mod["title"] == "Mod Retrouve"
    assert mod["thumbnail_url"] == "https://x/thumb.jpg"

    # persiste : le second listing ne refetch pas Steam
    n_after_first = calls["n"]
    c.get("/api/servers")
    assert calls["n"] == n_after_first


def test_backfill_steam_error_falls_back_without_persisting(tmp_path):
    def handler(request):
        import httpx
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}})
        return httpx.Response(500)

    c = make_client_with_workshop(tmp_path, workshop_handler=handler)
    _install_mods_state(c, ["42"])

    r = c.get("/api/servers")
    assert r.status_code == 200  # jamais de crash du listing a cause du backfill
    (mod,) = r.json()["servers"][0]["mods"]
    assert mod["title"] == "Mod 42"  # fallback lisible

    import json as _json
    data = _json.loads((tmp_path / "state.json").read_text())
    # entree partielle legitime (installed_at pose par update_mods_state, cf. Tache 2) :
    # pas de faux titre persiste, seulement l'horodatage d'installation.
    meta_42 = data.get("mods_metadata", {}).get("palworld", {}).get("42", {})
    assert "title" not in meta_42
    assert "thumbnail_url" not in meta_42


def test_backfill_delisted_mod_persists_placeholder(tmp_path):
    """Mod retire du Workshop (result != 1) : placeholder persiste pour ne pas re-tenter
    a chaque poll de 10s."""
    calls = {"n": 0}

    def handler(request):
        import httpx
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}})
        if "GetPublishedFileDetails" in url:
            calls["n"] += 1
            return httpx.Response(200, json={"response": {"publishedfiledetails": [{"result": 9}]}})
        return httpx.Response(404)

    c = make_client_with_workshop(tmp_path, workshop_handler=handler)
    _install_mods_state(c, ["42"])

    (mod,) = c.get("/api/servers").json()["servers"][0]["mods"]
    assert "42" in mod["title"] and "retir" in mod["title"]
    n = calls["n"]
    c.get("/api/servers")
    assert calls["n"] == n


def _confirm_installed(c, mod_ids):
    # l'agent confirme les mods presents sur disque
    r = c.post(
        "/api/agent/state",
        headers={"Authorization": "Bearer agent-t"},
        json={"servers": {"palworld": {"installed_mod_ids": mod_ids}}},
    )
    assert r.status_code == 200


def test_list_servers_mods_expose_dates(tmp_path):
    c = make_client_with_workshop(tmp_path)
    assert c.post("/api/servers/palworld/mods", json={
        "workshop_id": "3147025543", "title": "x", "thumbnail_url": "x",
    }).status_code == 201
    _confirm_installed(c, ["3147025543"])
    (srv,) = c.get("/api/servers").json()["servers"]
    (mod,) = srv["mods"]
    assert mod["installed_at"] is not None          # pose a la confirmation agent
    assert mod["steam_updated_at"] == "2025-07-13T09:46:40+00:00"  # epoch 1752400000 en ISO UTC


def test_steam_dates_refresh_is_batched_and_throttled(tmp_path):
    calls = {"bulk": 0}

    def handler(request):
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}})
        if "GetPublishedFileDetails" in url:
            body = request.content.decode()
            if "itemcount=2" in body:
                calls["bulk"] += 1
                return httpx.Response(200, json={"response": {"publishedfiledetails": [
                    {"result": 1, "publishedfileid": "111", "title": "Mod A",
                     "preview_url": "https://x/a.jpg", "time_updated": 1752000000},
                    {"result": 1, "publishedfileid": "222", "title": "Mod B",
                     "preview_url": "https://x/b.jpg", "time_updated": 1752100000},
                ]}})
            # resolutions unitaires (install/backfill)
            return httpx.Response(200, json={"response": {"publishedfiledetails": [
                {"result": 1, "consumer_app_id": 1623730, "title": "Cool Mod",
                 "preview_url": "https://x/t.jpg", "time_updated": 1752400000}
            ]}})
        return httpx.Response(404)

    c = make_client_with_workshop(tmp_path, workshop_handler=handler)
    for wid in ("111", "222"):
        assert c.post("/api/servers/palworld/mods", json={
            "workshop_id": wid, "title": "x", "thumbnail_url": "x",
        }).status_code == 201
    _confirm_installed(c, ["111", "222"])

    assert c.get("/api/servers").status_code == 200
    assert c.get("/api/servers").status_code == 200
    assert c.get("/api/servers").status_code == 200
    assert calls["bulk"] == 1  # UN seul appel batche malgre 3 polls (TTL 1 h)

    (srv,) = c.get("/api/servers").json()["servers"]
    by_id = {m["workshop_id"]: m for m in srv["mods"]}
    assert by_id["111"]["steam_updated_at"] == "2025-07-08T18:40:00+00:00"  # epoch 1752000000
    assert by_id["222"]["title"] == "Mod B"  # le refresh rafraichit aussi titre/vignette


def test_steam_dates_refresh_network_failure_keeps_stored_values(tmp_path):
    state = {"fail_bulk": False, "failed_bulk_calls": 0}

    def handler(request):
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}})
        if "GetPublishedFileDetails" in url:
            if "itemcount=1" in request.content.decode() and state["fail_bulk"]:
                state["failed_bulk_calls"] += 1
                return httpx.Response(500)
            return httpx.Response(200, json={"response": {"publishedfiledetails": [
                {"result": 1, "consumer_app_id": 1623730, "publishedfileid": "111",
                 "title": "Cool Mod", "preview_url": "https://x/t.jpg",
                 "time_updated": 1752400000}
            ]}})
        return httpx.Response(404)

    c = make_client_with_workshop(tmp_path, workshop_handler=handler)
    assert c.post("/api/servers/palworld/mods", json={
        "workshop_id": "111", "title": "x", "thumbnail_url": "x",
    }).status_code == 201
    _confirm_installed(c, ["111"])

    from app import mods as mods_service
    mods_service._steam_dates_refreshed_at.clear()
    state["fail_bulk"] = True
    (srv,) = c.get("/api/servers").json()["servers"]
    (mod,) = srv["mods"]
    # echec reseau du refresh : la valeur posee a l'install est conservee, pas de 500
    assert mod["steam_updated_at"] == "2025-07-13T09:46:40+00:00"
    # le TTL est pose MEME en echec : un second poll ne re-appelle pas Steam
    assert state["failed_bulk_calls"] == 1
    assert c.get("/api/servers").status_code == 200
    assert state["failed_bulk_calls"] == 1


def test_partial_metadata_entry_is_backfilled(tmp_path):
    # entree partielle (installed_at sans title, cf. Tache 2) : le backfill doit se
    # declencher et le titre re-resolu doit coexister avec installed_at
    c = make_client_with_workshop(tmp_path)
    _confirm_installed(c, ["3147025543"])  # mod inconnu du backend, confirme par l'agent
    (srv,) = c.get("/api/servers").json()["servers"]
    (mod,) = srv["mods"]
    assert mod["title"] == "Cool Mod"           # backfill passe
    assert mod["installed_at"] is not None      # pose par update_mods_state, preserve


# --- Tache 4 : GET/PUT /api/servers/{name}/registry (admin uniquement) ---

def make_admin_and_user_clients(tmp_path):
    """admin (role admin) + user1 (role user, scope palworld) sur le meme store --
    sert a verifier le 403 de require_admin_role independamment du scoping serveur
    (user1 EST assigne a palworld, donc seul le controle de role peut le bloquer)."""
    app = make_app(tmp_path)
    password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("admin1", password_hash, role="admin"))
    asyncio.run(app.state.store.create_user("user1", password_hash, role="user", servers=["palworld"]))
    admin = TestClient(app)
    r = admin.post("/api/login", json={"username": "admin1", "password": "testpass123"})
    assert r.status_code == 200
    user = TestClient(app)
    r = user.post("/api/login", json={"username": "user1", "password": "testpass123"})
    assert r.status_code == 200
    return app, admin, user


def test_registry_get_masks_password_put_partial_update(tmp_path):
    app, client, _ = make_admin_and_user_clients(tmp_path)
    r = client.put("/api/servers/palworld/registry", json={
        "stop_warn_seconds": 30,
        "rcon": {"host": "127.0.0.1", "port": 25575, "password": "secret1"}})
    assert r.status_code == 200
    body = r.json()
    assert body["rcon"]["password_set"] is True and "password" not in body["rcon"]

    # PUT partiel : rcon SANS password -> password conserve, autres champs intacts
    r = client.put("/api/servers/palworld/registry",
                   json={"rcon": {"host": "127.0.0.1", "port": 25580}})
    assert r.status_code == 200
    entry = asyncio.run(app.state.store.registry.get("palworld"))
    assert entry["rcon"]["password"] == "secret1"
    assert entry["rcon"]["port"] == 25580
    assert entry["stop_warn_seconds"] == 30

    g = client.get("/api/servers/palworld/registry").json()
    assert "password" not in g["rcon"] and g["rcon"]["password_set"] is True


def test_registry_put_validation_and_403_for_user(tmp_path):
    app, client, client_user = make_admin_and_user_clients(tmp_path)
    assert client.put("/api/servers/palworld/registry",
                      json={"status": "detruit"}).status_code == 422
    assert client.put("/api/servers/inconnu/registry", json={}).status_code == 404
    assert client_user.get("/api/servers/palworld/registry").status_code == 403


def test_registry_put_rcon_null_is_noop(tmp_path):
    # rcon: null explicite ne doit RIEN effacer (no-op) -- seul un futur champ dedie
    # pourra effacer une config rcon ; jamais un null implicite (perte silencieuse
    # sinon : exclude_unset inclut rcon=None quand le client l'envoie explicitement).
    app, client, _ = make_admin_and_user_clients(tmp_path)
    r = client.put("/api/servers/palworld/registry", json={
        "rcon": {"host": "127.0.0.1", "port": 25575, "password": "secret1"}})
    assert r.status_code == 200

    r = client.put("/api/servers/palworld/registry", json={"rcon": None})
    assert r.status_code == 200

    entry = asyncio.run(app.state.store.registry.get("palworld"))
    assert entry["rcon"]["host"] == "127.0.0.1"
    assert entry["rcon"]["port"] == 25575
    assert entry["rcon"]["password"] == "secret1"


def test_dynamic_registry_server_visible_and_scoped(tmp_path):
    """Un serveur ajoute au registre APRES le demarrage est servi par l'API sans
    redemarrage — la preuve que plus personne ne lit settings.servers (fige)."""
    app, client = make_app_and_client(tmp_path)
    store = app.state.store
    data = store._load()
    data["servers_registry"]["nouveau"] = {
        "display_name": "Nouveau", "server_appid": 111, "status": "active", "extra": {}}
    store._dump(data)
    names = [s["name"] for s in client.get("/api/servers").json()["servers"]]
    assert "nouveau" in names
    # /players sur ce serveur dynamique : ne doit pas repondre 404 "serveur inconnu"
    assert client.get("/api/servers/nouveau/players").status_code == 200


def test_detail_exposes_files_listing_and_file_read(tmp_path):
    c = make_logged_in_client(tmp_path)
    assert c.get("/api/servers/palworld/detail").json()["files_listing"] == {}
    assert c.get("/api/servers/palworld/detail").json()["file_read"] is None
    import asyncio
    asyncio.run(c.app.state.store.registry.update_entry("palworld", {
        "files_listing": {"install": ["a.ini"]},
        "file_read": {"root": "install", "path": "a.ini", "content_b64": "eA==", "sha256": "a" * 64},
        "rcon": {"host": "127.0.0.1", "port": 25575, "password": "secret"}}))
    detail = c.get("/api/servers/palworld/detail").json()
    assert detail["files_listing"] == {"install": ["a.ini"]}
    assert detail["file_read"]["path"] == "a.ini"
    assert "rcon" not in detail  # detail ne spread jamais le registre brut
