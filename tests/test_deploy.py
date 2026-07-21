from tests.test_sanity import make_logged_in_client

AGT = {"Authorization": "Bearer agent-t"}


def see_agent(c, version="2.1.0"):
    """Le backend ne crée les ordres de déploiement qu'après avoir vu un agent capable."""
    r = c.post("/api/agent/state", headers=AGT, json={"servers": {}, "agent_version": version})
    assert r.status_code == 200


def test_deploy_gated_on_agent_version(tmp_path):
    c = make_logged_in_client(tmp_path)
    body = {"name": "vrising", "display_name": "V Rising", "server_appid": 1829350}
    # aucun agent jamais vu -> 409
    assert c.post("/api/deploy/servers", json=body).status_code == 409
    see_agent(c, "2.0.0")  # agent trop ancien -> 409
    assert c.post("/api/deploy/servers", json=body).status_code == 409
    see_agent(c, "2.1.0")
    assert c.post("/api/deploy/servers", json=body).status_code == 201


def test_deploy_creates_entry_and_order(tmp_path):
    c = make_logged_in_client(tmp_path)
    see_agent(c)
    r = c.post("/api/deploy/servers",
               json={"name": "vrising", "display_name": "V Rising", "server_appid": 1829350})
    assert r.status_code == 201
    assert r.json()["server"]["status"] == "installing"
    order = r.json()["order"]
    assert order["type"] == "install_game" and order["appid"] == 1829350
    reg = c.get("/api/servers/vrising/registry").json()
    assert reg["status"] == "installing" and reg["server_appid"] == 1829350
    # l'entrée installing apparaît dans la liste avec son statut
    srv = [s for s in c.get("/api/servers").json()["servers"] if s["name"] == "vrising"]
    assert srv and srv[0]["status"] == "installing"


def test_deploy_rejects_duplicates_and_bad_slug(tmp_path):
    c = make_logged_in_client(tmp_path)
    see_agent(c)
    ok = {"name": "vrising", "display_name": "V Rising", "server_appid": 1829350}
    assert c.post("/api/deploy/servers", json=ok).status_code == 201
    # nom déjà actif au registre (palworld seedé) -> 409
    assert c.post("/api/deploy/servers",
                  json={"name": "palworld", "display_name": "X", "server_appid": 111}).status_code == 409
    # appid déjà au registre -> 409
    assert c.post("/api/deploy/servers",
                  json={"name": "autre", "display_name": "X", "server_appid": 1829350}).status_code == 409
    # slug invalide -> 422
    assert c.post("/api/deploy/servers",
                  json={"name": "V Rising!", "display_name": "X", "server_appid": 222}).status_code == 422


def test_deploy_retry_after_failed_install(tmp_path):
    """Une install échouée laisse l'entrée en installing sans ordre pendant :
    re-poster le même couple nom/appid relance un ordre, sans dupliquer l'entrée."""
    c = make_logged_in_client(tmp_path)
    see_agent(c)
    r = c.post("/api/deploy/servers",
               json={"name": "vrising", "display_name": "V Rising", "server_appid": 1829350})
    oid = r.json()["order"]["id"]
    # tant que l'ordre est pending, re-poster -> 409
    assert c.post("/api/deploy/servers",
                  json={"name": "vrising", "display_name": "V Rising", "server_appid": 1829350}).status_code == 409
    c.post(f"/api/agent/orders/{oid}", headers=AGT, json={"status": "failed", "detail": "steamcmd ko"})
    r2 = c.post("/api/deploy/servers",
                json={"name": "vrising", "display_name": "V Rising", "server_appid": 1829350})
    assert r2.status_code == 201 and r2.json()["order"]["type"] == "install_game"


def test_deploy_appinfo_lookup(tmp_path):
    import asyncio

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_sanity import make_settings

    def handler(request):
        return httpx.Response(200, json={"data": {"1829350": {"common": {"name": "V Rising"}}}})

    app = create_app(make_settings(tmp_path),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", pw))
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    assert c.get("/api/deploy/appinfo/1829350").json() == {"name": "V Rising"}


def test_deploy_admin_only(tmp_path):
    c = make_logged_in_client(tmp_path)
    see_agent(c)
    # rétrograder le compte en user simple, sans serveur assigné
    import asyncio
    asyncio.run(c.app.state.store.set_user_access("tester", "user", []))
    assert c.post("/api/deploy/servers",
                  json={"name": "x", "display_name": "X", "server_appid": 1}).status_code == 403


def make_awaiting(c, candidates=None):
    see_agent(c)
    oid = c.post("/api/deploy/servers",
                 json={"name": "vrising", "display_name": "V Rising",
                       "server_appid": 1829350}).json()["order"]["id"]
    c.post(f"/api/agent/orders/{oid}", headers=AGT,
           json={"status": "done",
                 "exe_candidates": candidates or ["VRisingServer.exe", "tools\\Extra.exe"]})


def test_setup_creates_order_and_fields(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c)
    r = c.post("/api/deploy/servers/vrising/setup",
               json={"exe_path": "VRisingServer.exe", "launch_args": "-persistentDataPath save",
                     "stop_adapter": "generic-graceful", "query_port": 27016,
                     "stop_warn_seconds": 30, "start_now": True})
    assert r.status_code == 201
    order = r.json()
    assert order["type"] == "setup_server" and order["task_name"] == "vrising"
    assert order["process"] == "VRisingServer" and order["start_now"] is True
    reg = c.get("/api/servers/vrising/registry").json()
    assert reg["process"] == "VRisingServer" and reg["start_task"] == "vrising"
    assert reg["stop_adapter"] == "generic-graceful" and reg["query_port"] == 27016
    assert reg["status"] == "awaiting_setup"  # active seulement à la confirmation agent
    # confirmation agent -> active
    c.post(f"/api/agent/orders/{order['id']}", headers=AGT, json={"status": "done"})
    assert c.get("/api/servers/vrising/registry").json()["status"] == "active"


def test_setup_rejects_unknown_exe_and_wrong_status(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c)
    # exe hors des candidats scannés -> 400 (jamais de chemin arbitraire vers l'agent)
    assert c.post("/api/deploy/servers/vrising/setup",
                  json={"exe_path": "C:\\evil.exe"}).status_code == 400
    # serveur actif (palworld) -> 409
    assert c.post("/api/deploy/servers/palworld/setup",
                  json={"exe_path": "x.exe"}).status_code == 409
    assert c.post("/api/deploy/servers/ghost/setup",
                  json={"exe_path": "x.exe"}).status_code == 404


def test_setup_rcon_generic_requires_password(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c)
    body = {"exe_path": "VRisingServer.exe", "stop_adapter": "rcon-generic"}
    assert c.post("/api/deploy/servers/vrising/setup", json=body).status_code == 400
    body["rcon"] = {"host": "127.0.0.1", "port": 25580, "password": "s3cret",
                    "shutdown_command": "shutdown", "announce_command": "announce arret dans {delay}s"}
    r = c.post("/api/deploy/servers/vrising/setup", json=body)
    assert r.status_code == 201
    reg = c.get("/api/servers/vrising/registry").json()
    # password write-only dans les GET (même masquage que l'éditeur registre)
    assert reg["rcon"]["password_set"] is True and "password" not in reg["rcon"]
    assert reg["rcon"]["shutdown_command"] == "shutdown"


def report_discovered(c):
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {}, "agent_version": "2.1.0",
                     "discovered_games": [{"appid": 1829350, "name": "V Rising Dedicated Server",
                                           "installdir": "VRisingDedicatedServer", "buildid": "42"}]})
    assert r.status_code == 200


def test_discovered_games_exposed_to_admin_only(tmp_path):
    c = make_logged_in_client(tmp_path)
    report_discovered(c)
    body = c.get("/api/servers").json()
    assert body["agent_version"] == "2.1.0"
    assert body["discovered_games"][0]["appid"] == 1829350
    import asyncio
    asyncio.run(c.app.state.store.set_user_access("tester", "user", ["palworld"]))
    body_user = c.get("/api/servers").json()
    assert "discovered_games" not in body_user and "agent_version" not in body_user


def test_adopt_discovered_game(tmp_path):
    c = make_logged_in_client(tmp_path)
    report_discovered(c)
    r = c.post("/api/deploy/adopt",
               json={"appid": 1829350, "name": "vrising", "display_name": "V Rising"})
    assert r.status_code == 201
    order = r.json()
    assert order["type"] == "scan_exe" and order["appid"] == 1829350
    assert c.get("/api/servers/vrising/registry").json()["status"] == "awaiting_setup"


def test_adopt_rejects_unknown_appid(tmp_path):
    """Seul un jeu réellement signalé par l'agent est adoptable : pas d'entrée
    registre aveugle vers un appid arbitraire."""
    c = make_logged_in_client(tmp_path)
    see_agent(c)  # agent capable mais rien de découvert
    assert c.post("/api/deploy/adopt",
                  json={"appid": 999, "name": "x", "display_name": "X"}).status_code == 404


def test_search_merges_known_and_steam_dedup_by_appid(tmp_path, monkeypatch):
    import asyncio
    import json

    import bcrypt
    import httpx

    from app import known_servers
    from app.main import create_app
    from tests.test_sanity import make_settings

    known_path = tmp_path / "known.json"
    known_path.write_text(json.dumps([{"appid": 2394010, "name": "Palworld Dedicated Server"}]))
    monkeypatch.setenv("HEPHAESTOS_KNOWN_SERVERS_FILE", str(known_path))
    known_servers._cache = None  # cache module-level : reset obligatoire entre tests

    def handler(request):
        # la boutique renvoie aussi Palworld (deja dans la liste locale, sous un nom
        # legerement different) -- doit etre dedupliqué par appid, version locale gardee
        return httpx.Response(200, json={"total": 1, "items": [{"id": 2394010, "name": "Palworld"}]})

    app = create_app(make_settings(tmp_path),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", pw))
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    r = c.get("/api/deploy/search?q=palworld")
    assert r.status_code == 200
    results = r.json()["results"]
    appids = [x["appid"] for x in results]
    assert appids.count(2394010) == 1
    assert next(x for x in results if x["appid"] == 2394010)["name"] == "Palworld Dedicated Server"


def test_search_rejects_short_query(tmp_path):
    c = make_logged_in_client(tmp_path)
    assert c.get("/api/deploy/search?q=a").status_code == 400


def test_search_admin_only(tmp_path):
    c = make_logged_in_client(tmp_path)
    import asyncio
    asyncio.run(c.app.state.store.set_user_access("tester", "user", []))
    assert c.get("/api/deploy/search?q=valheim").status_code == 403


def test_details_direct_appid_success(tmp_path):
    import asyncio

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_sanity import make_settings

    def handler(request):
        if "appdetails" in str(request.url):
            return httpx.Response(200, json={"1623730": {"success": True, "data": {
                "header_image": "https://x/header.jpg", "short_description": "Desc du jeu de base."}}})
        return httpx.Response(200, json={"total": 0, "items": []})

    app = create_app(make_settings(tmp_path),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", pw))
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    r = c.get("/api/deploy/details?appid=1623730&name=Palworld")
    assert r.status_code == 200
    body = r.json()
    assert body == {"header_image": "https://x/header.jpg", "description": "Desc du jeu de base.", "is_proxy": False}


def test_details_falls_back_to_base_game_search(tmp_path):
    import asyncio

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_sanity import make_settings

    def handler(request):
        url = str(request.url)
        if "appdetails" in url and "appids=2394010" in url:
            return httpx.Response(200, json={"2394010": {"success": False}})  # serveur dedie : rien
        if "appdetails" in url and "appids=1623730" in url:
            return httpx.Response(200, json={"1623730": {"success": True, "data": {
                "header_image": "https://x/header.jpg", "short_description": "Desc du jeu de base."}}})
        if "storesearch" in url:
            return httpx.Response(200, json={"total": 1, "items": [{"id": 1623730, "name": "Palworld"}]})
        return httpx.Response(404)

    app = create_app(make_settings(tmp_path),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", pw))
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    r = c.get("/api/deploy/details?appid=2394010&name=Palworld+Dedicated+Server")
    assert r.status_code == 200
    body = r.json()
    assert body == {"header_image": "https://x/header.jpg", "description": "Desc du jeu de base.", "is_proxy": True}


def test_details_double_failure_returns_null_fields_not_error(tmp_path):
    import asyncio

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_sanity import make_settings

    app = create_app(make_settings(tmp_path),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", pw))
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    r = c.get("/api/deploy/details?appid=999999&name=Jeu+Inconnu+Dedicated+Server")
    assert r.status_code == 200
    assert r.json() == {"header_image": None, "description": None, "is_proxy": False}


def test_details_admin_only(tmp_path):
    c = make_logged_in_client(tmp_path)
    import asyncio
    asyncio.run(c.app.state.store.set_user_access("tester", "user", []))
    assert c.get("/api/deploy/details?appid=1&name=x").status_code == 403


def test_search_does_not_truncate_local_results_below_actual_count(tmp_path, monkeypatch):
    # Trouve en usage reel : une recherche large ("server") correspond a TOUTES les
    # entrees locales (elles contiennent toutes "Server" dans leur nom) -- le total
    # fusionne ne doit jamais tronquer la liste locale (curee, toujours pertinente),
    # seul le repli boutique Steam est deja borne (search_apps lui-meme).
    import asyncio
    import json

    import bcrypt
    import httpx
    from fastapi.testclient import TestClient

    from app import known_servers
    from app.main import create_app
    from tests.test_sanity import make_settings

    known_path = tmp_path / "known.json"
    known_path.write_text(json.dumps([
        {"appid": i, "name": f"Jeu {i} Dedicated Server"} for i in range(30)
    ]))
    monkeypatch.setenv("HEPHAESTOS_KNOWN_SERVERS_FILE", str(known_path))
    known_servers._cache = None

    app = create_app(make_settings(tmp_path),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(
                         lambda r: httpx.Response(200, json={"total": 0, "items": []}))))
    pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("tester", pw))
    c = TestClient(app)
    c.post("/api/login", json={"username": "tester", "password": "testpass123"})
    r = c.get("/api/deploy/search?q=server")
    assert r.status_code == 200
    assert len(r.json()["results"]) == 30
