import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from tests.test_sanity import make_app, make_logged_in_client

AGT = {"Authorization": "Bearer agent-t"}


def make_client(tmp_path):
    return make_logged_in_client(tmp_path)


def test_agent_order_flow(tmp_path):
    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    assert [o["id"] for o in c.get("/api/agent/orders", headers=AGT).json()["orders"]] == [oid]
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "running"}).status_code == 200
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "done", "detail": "build 1->2"}).status_code == 200
    assert c.get("/api/agent/orders", headers=AGT).json()["orders"] == []
    assert c.post("/api/agent/orders/nope", headers=AGT, json={"status": "done"}).status_code == 404
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "explose"}).status_code == 422


def test_agent_state_report(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "24088465", "process_up": True, "players": 2}}})
    assert r.status_code == 200
    srv = c.get("/api/servers").json()["servers"][0]
    assert srv["state"]["players"] == 2 and srv["state"]["last_seen"]
    anon = TestClient(make_app(tmp_path))
    assert anon.post("/api/agent/state", json={"servers": {}}).status_code == 401


def test_agent_state_ignores_unknown_servers(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "1", "process_up": True, "players": 0},
                                 "ghost": {"buildid": "666", "process_up": True, "players": 9}}})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "ignored": ["ghost"]}
    servers = c.get("/api/servers").json()["servers"]
    assert [s["name"] for s in servers] == ["palworld"]
    import json
    persisted = json.loads((tmp_path / "state.json").read_text())["servers"]
    assert "ghost" not in persisted and "palworld" in persisted


def test_agent_state_no_ignored(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "1", "process_up": True, "players": 0}}})
    assert r.json() == {"ok": True, "ignored": []}


def test_agent_state_rejects_xss_buildid(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "<img src=x onerror=alert(1)>", "process_up": True}}})
    assert r.status_code == 422


def test_agent_state_ignores_unknown_field(tmp_path):
    """Version-skew agent/backend : un agent plus recent peut envoyer des champs que ce
    backend ne connait pas encore. Un 422 rejetterait le rapport ENTIER (tous les
    serveurs paraitraient morts) -- on ignore le champ inconnu, on garde le reste."""
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "1", "process_up": True, "foo": "bar"}}})
    assert r.status_code == 200
    srv = c.get("/api/servers").json()["servers"][0]
    assert srv["state"]["buildid"] == "1"
    assert "foo" not in srv["state"]


def test_agent_state_ignores_unknown_field_in_player(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "1", "process_up": True, "players": 1,
                     "players_list": [{"id": "1", "name": "Alice", "clan": "future-field"}]}}})
    assert r.status_code == 200


def test_agent_state_accepts_legal_payload(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"buildid": "24088465", "process_up": True, "players": 3}}})
    assert r.status_code == 200
    srv = c.get("/api/servers").json()["servers"][0]
    assert srv["state"]["buildid"] == "24088465" and srv["state"]["players"] == 3


def test_report_state_with_players_list_updates_sessions(tmp_path):
    import json
    c = make_client(tmp_path)
    res = c.post(
        "/api/agent/state", headers=AGT,
        json={"servers": {"palworld": {
            "buildid": "100", "process_up": True, "players": 1,
            "players_list": [{"id": "123", "name": "Alice", "steamid": "765611"}],
        }}},
    )
    assert res.status_code == 200

    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["player_sessions"]["palworld"]["123"]["name"] == "Alice"


def test_report_state_without_players_list_does_not_touch_sessions(tmp_path):
    import json
    c = make_client(tmp_path)
    res = c.post(
        "/api/agent/state", headers=AGT,
        json={"servers": {"palworld": {"buildid": "100", "process_up": True, "players": None}}},
    )
    assert res.status_code == 200

    persisted = json.loads((tmp_path / "state.json").read_text())
    assert "palworld" not in persisted.get("player_sessions", {})


def test_report_state_with_installed_mod_ids_updates_mods_state(tmp_path):
    import json
    c = make_client(tmp_path)
    res = c.post(
        "/api/agent/state", headers=AGT,
        json={"servers": {"palworld": {
            "buildid": "100", "process_up": True, "players": 0,
            "installed_mod_ids": ["123"], "process_started_at": "2026-07-14T10:00:00+00:00",
        }}},
    )
    assert res.status_code == 200

    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["mods_state"]["palworld"]["installed_mod_ids"] == ["123"]
    assert persisted["servers"]["palworld"]["process_started_at"] == "2026-07-14T10:00:00+00:00"


def test_report_state_without_installed_mod_ids_does_not_touch_mods_state(tmp_path):
    import json
    c = make_client(tmp_path)
    res = c.post(
        "/api/agent/state", headers=AGT,
        json={"servers": {"palworld": {"buildid": "100", "process_up": True, "players": None}}},
    )
    assert res.status_code == 200

    persisted = json.loads((tmp_path / "state.json").read_text())
    assert "palworld" not in persisted.get("mods_state", {})


def test_agent_state_report_accepts_rcon_and_process_metrics(tmp_path):
    c = make_client(tmp_path)
    r = c.post(
        "/api/agent/state",
        headers=AGT,
        json={"servers": {"palworld": {
            "process_up": True,
            "rcon_info": "Welcome to Pal Server[Version:0.1.2] MyWorld",
            "process_cpu_percent": 3.2,
            "process_mem_mb": 812.0,
        }}},
    )
    assert r.status_code == 200
    detail = c.get("/api/servers/palworld/detail").json()
    assert detail["rcon_info"] == "Welcome to Pal Server[Version:0.1.2] MyWorld"
    assert detail["process"] == {"cpu_percent": 3.2, "mem_mb": 812.0}


def test_order_terminal_state_is_final(tmp_path):
    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "done"}).status_code == 200
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "running"}).status_code == 409
    assert c.get("/api/agent/orders", headers=AGT).json()["orders"] == []
    oid2 = c.post("/api/servers/palworld/restart").json()["id"]
    assert c.post(f"/api/agent/orders/{oid2}", headers=AGT, json={"status": "failed", "detail": "boom"}).status_code == 200
    assert c.post(f"/api/agent/orders/{oid2}", headers=AGT, json={"status": "done"}).status_code == 409


def test_orders_config_block_absent_until_snapshot_then_served(tmp_path):
    c = make_client(tmp_path)
    # avant tout snapshot agent : pas de bloc config (protege un agent pas encore migre
    # et empeche de pousser un registre non enrichi, qui viderait la config Windows)
    resp = c.get("/api/agent/orders", headers=AGT)
    assert resp.status_code == 200 and resp.json()["config"] is None

    report = {"servers": {}, "agent_version": "2.0.0",
              "config_servers": [{"name": "palworld", "appid": 2394010,
                                  "process": "PalServer-Win64-Shipping-Cmd",
                                  "start_task": "PalServer", "stop_adapter": "palworld-rcon",
                                  "cle_future": 42}]}
    assert c.post("/api/agent/state", headers=AGT, json=report).status_code == 200

    cfg = c.get("/api/agent/orders", headers=AGT).json()["config"]
    assert cfg is not None and len(cfg["hash"]) == 64
    pal = next(s for s in cfg["servers"] if s["name"] == "palworld")
    assert pal["process"] == "PalServer-Win64-Shipping-Cmd"  # enrichi depuis le snapshot
    assert pal["cle_future"] == 42                            # cle inconnue sans perte


def test_state_report_stores_agent_meta_and_discovered(tmp_path):
    c = make_client(tmp_path)
    report = {"servers": {}, "agent_version": "2.0.0", "config_hash": "abc",
              "discovered_games": [{"appid": 896660, "name": "Valheim",
                                    "installdir": "valheim", "buildid": "123"}]}
    c.post("/api/agent/state", headers=AGT, json=report)
    meta = asyncio.run(c.app.state.store.get_agent_meta())
    assert meta["agent_version"] == "2.0.0"
    assert meta["config_hash"] == "abc"
    assert meta["discovered_games"][0]["appid"] == 896660
    assert meta["reported_at"]  # horodatage pose par le store


def test_state_report_snapshot_ignores_unknown_server(tmp_path):
    c = make_client(tmp_path)
    report = {"servers": {}, "config_servers": [{"name": "intrus", "appid": 999,
                                                 "process": "evil"}]}
    c.post("/api/agent/state", headers=AGT, json=report)
    assert asyncio.run(c.app.state.store.registry.get("intrus")) is None


def test_state_report_ignores_oversized_agent_entry(tmp_path):
    """Toute donnee venant de l'agent est bornee en TAILLE, pas seulement en nombre :
    une entree config_servers dont la serialisation JSON depasse AGENT_ENTRY_MAX_BYTES
    est ignorée silencieusement (protege state.json d'un agent moins privilegie)."""
    c = make_client(tmp_path)
    report = {"servers": {},
              "config_servers": [
                  {"name": "palworld", "appid": 2394010, "process": "PalServer-Win64-Shipping-Cmd"},
                  {"name": "palworld", "appid": 2394010, "bloated": "x" * 10_000},
              ]}
    assert c.post("/api/agent/state", headers=AGT, json=report).status_code == 200
    entry = asyncio.run(c.app.state.store.registry.get("palworld"))
    assert "bloated" not in entry.get("extra", {})
    assert entry["process"] == "PalServer-Win64-Shipping-Cmd"


def test_state_report_ignores_oversized_discovered_game(tmp_path):
    """Meme borne de taille (AGENT_ENTRY_MAX_BYTES) appliquee a discovered_games,
    independamment de config_servers : une entree surdimensionnee est ecartee du
    meta stocke, la petite entree passe."""
    c = make_client(tmp_path)
    report = {"servers": {},
              "discovered_games": [
                  {"appid": 896660, "name": "Valheim", "installdir": "valheim", "buildid": "123"},
                  {"appid": 999999, "name": "bloated", "installdir": "x" * 10_000},
              ]}
    assert c.post("/api/agent/state", headers=AGT, json=report).status_code == 200
    discovered = asyncio.run(c.app.state.store.get_agent_meta())["discovered_games"]
    appids = [g["appid"] for g in discovered]
    assert 896660 in appids
    assert 999999 not in appids


def test_state_report_does_not_readopt_snapshot_after_admin_clears_field(tmp_path):
    """Effacer un champ via l'admin (PUT stop_warn_seconds=None) doit rester efface :
    l'adoption du snapshot agent ne sert QUE la migration initiale (premier rapport
    apres le Lot 1). Une fois config_snapshot_seen pose, un snapshot stale renvoye par
    l'agent (qui n'a pas encore recu la nouvelle config poussee par le backend) ne doit
    plus re-remplir un champ que l'admin vient d'effacer -- sinon boucle stable fausse
    ou le revert n'atteint jamais l'agent."""
    c = make_client(tmp_path)
    snapshot = {"servers": {}, "config_servers": [{"name": "palworld", "appid": 2394010,
                                                    "stop_warn_seconds": 45}]}
    # premier rapport : migration initiale, adoption normale (comportement inchange)
    assert c.post("/api/agent/state", headers=AGT, json=snapshot).status_code == 200
    entry = asyncio.run(c.app.state.store.registry.get("palworld"))
    assert entry["stop_warn_seconds"] == 45

    # l'admin efface le champ
    asyncio.run(c.app.state.store.registry.update_entry("palworld", {"stop_warn_seconds": None}))
    entry = asyncio.run(c.app.state.store.registry.get("palworld"))
    assert entry["stop_warn_seconds"] is None

    # second rapport : meme snapshot stale (l'agent n'a pas encore recu le null pousse)
    assert c.post("/api/agent/state", headers=AGT, json=snapshot).status_code == 200
    entry = asyncio.run(c.app.state.store.registry.get("palworld"))
    assert entry["stop_warn_seconds"] is None


def deploy_vrising(c):
    c.post("/api/agent/state", headers=AGT, json={"servers": {}, "agent_version": "2.1.0"})
    r = c.post("/api/deploy/servers",
               json={"name": "vrising", "display_name": "V Rising", "server_appid": 1829350})
    assert r.status_code == 201
    return r.json()["order"]["id"]


def test_install_game_done_stores_candidates_and_transitions(tmp_path):
    c = make_client(tmp_path)
    oid = deploy_vrising(c)
    r = c.post(f"/api/agent/orders/{oid}", headers=AGT,
               json={"status": "done", "detail": "installe",
                     "exe_candidates": ["VRisingServer.exe", "sub\\Tool.exe"]})
    assert r.status_code == 200
    reg = c.get("/api/servers/vrising/registry").json()
    assert reg["status"] == "awaiting_setup"
    assert reg["exe_candidates"] == ["VRisingServer.exe", "sub\\Tool.exe"]


def test_install_game_failed_keeps_installing(tmp_path):
    c = make_client(tmp_path)
    oid = deploy_vrising(c)
    c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "failed", "detail": "ko"})
    assert c.get("/api/servers/vrising/registry").json()["status"] == "installing"


def test_exe_candidates_bounds(tmp_path):
    c = make_client(tmp_path)
    oid = deploy_vrising(c)
    too_many = {"status": "done", "exe_candidates": [f"a{i}.exe" for i in range(31)]}
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json=too_many).status_code == 422
    too_long = {"status": "done", "exe_candidates": ["x" * 261]}
    assert c.post(f"/api/agent/orders/{oid}", headers=AGT, json=too_long).status_code == 422


def test_exe_candidates_ignored_on_other_order_types(tmp_path):
    """Un agent (source non fiable) ne doit pas pouvoir muter le registre via un
    ordre update classique."""
    c = make_client(tmp_path)
    oid = c.post("/api/servers/palworld/update").json()["id"]
    r = c.post(f"/api/agent/orders/{oid}", headers=AGT,
               json={"status": "done", "exe_candidates": ["evil.exe"]})
    assert r.status_code == 200
    reg = c.get("/api/servers/palworld/registry").json()
    assert "exe_candidates" not in reg or not reg["exe_candidates"]
    assert reg["status"] != "awaiting_setup"


def test_list_files_done_stores_listing_per_root(tmp_path):
    c = make_client(tmp_path)
    asyncio.run(c.app.state.store.registry.update_entry("palworld", {"status": "active"}))
    oid = c.post("/api/servers/palworld/files/list", json={"root": "install"}).json()["id"]
    r = c.post(f"/api/agent/orders/{oid}", headers=AGT,
               json={"status": "done", "files": ["Config/Settings.ini", "Saved/log.txt"]})
    assert r.status_code == 200
    reg = c.get("/api/servers/palworld/registry").json()
    assert reg["files_listing"]["install"] == ["Config/Settings.ini", "Saved/log.txt"]


def test_list_files_second_root_does_not_erase_first(tmp_path):
    c = make_client(tmp_path)
    asyncio.run(c.app.state.store.registry.update_entry(
        "palworld", {"status": "active", "save_dir": "C:\\saves"}))
    oid1 = c.post("/api/servers/palworld/files/list", json={"root": "install"}).json()["id"]
    c.post(f"/api/agent/orders/{oid1}", headers=AGT, json={"status": "done", "files": ["a.ini"]})
    oid2 = c.post("/api/servers/palworld/files/list", json={"root": "save"}).json()["id"]
    c.post(f"/api/agent/orders/{oid2}", headers=AGT, json={"status": "done", "files": ["b.cfg"]})
    reg = c.get("/api/servers/palworld/registry").json()
    assert reg["files_listing"] == {"install": ["a.ini"], "save": ["b.cfg"]}


def test_read_file_done_stores_single_slot(tmp_path):
    c = make_client(tmp_path)
    asyncio.run(c.app.state.store.registry.update_entry("palworld", {"status": "active"}))
    oid = c.post("/api/servers/palworld/files/read",
                 json={"root": "install", "path": "Config/Settings.ini"}).json()["id"]
    c.post(f"/api/agent/orders/{oid}", headers=AGT,
           json={"status": "done", "content_b64": "aGVsbG8=", "sha256": "a" * 64})
    reg = c.get("/api/servers/palworld/registry").json()
    file_read = reg["file_read"]
    # read_at (horodatage TTL secret-at-rest) present et ISO-parsable, le reste inchange
    assert datetime.fromisoformat(file_read.pop("read_at"))
    assert file_read == {"root": "install", "path": "Config/Settings.ini",
                         "content_b64": "aGVsbG8=", "sha256": "a" * 64}


def test_read_file_failed_does_not_touch_file_read(tmp_path):
    c = make_client(tmp_path)
    asyncio.run(c.app.state.store.registry.update_entry("palworld", {"status": "active"}))
    oid = c.post("/api/servers/palworld/files/read",
                 json={"root": "install", "path": "Config/Settings.ini"}).json()["id"]
    c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "failed", "detail": "trop gros"})
    reg = c.get("/api/servers/palworld/registry").json()
    assert "file_read" not in reg or reg["file_read"] is None
