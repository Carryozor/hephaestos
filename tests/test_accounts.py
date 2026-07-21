"""Comptes scopes par serveur + gestion des comptes + auteur des ordres.

Modele : role "admin" (tout + gestion des comptes) / role "user" (controle complet
mais uniquement sur ses serveurs assignes). Les comptes legacy sans champ role
sont traites comme admin (migration douce).
"""
import asyncio

import bcrypt
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TWO_SERVERS = {
    "palworld": {"display_name": "Palworld", "server_appid": 2394010, "workshop_appid": 1623730},
    "valheim": {"display_name": "Valheim", "server_appid": 896660},
}


def make_app_two_servers(tmp_path):
    def handler(request):
        url = str(request.url)
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {}})
        if "GetPublishedFileDetails" in url:
            return httpx.Response(200, json={"response": {"publishedfiledetails": [
                {"result": 1, "consumer_app_id": 1623730, "title": "Cool Mod",
                 "preview_url": "https://x/t.jpg", "file_description": "d", "time_updated": 1752400000}
            ]}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(agent_token="agent-t", data_dir=tmp_path, servers=TWO_SERVERS)
    return create_app(settings, http_client=client)


def login(app, username, password="testpass123"):
    c = TestClient(app)
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def make_clients(tmp_path):
    """admin 'boss' (role admin) + 'gardien' scope sur palworld uniquement."""
    app = make_app_two_servers(tmp_path)
    h = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    store = app.state.store
    asyncio.run(store.create_user("boss", h, role="admin"))
    asyncio.run(
        store.create_user("gardien", h, role="user", servers=["palworld"]))
    return app, login(app, "boss"), login(app, "gardien")


# --- scoping des serveurs ---

def test_scoped_user_sees_only_assigned_servers(tmp_path):
    _, admin, scoped = make_clients(tmp_path)
    assert {s["name"] for s in admin.get("/api/servers").json()["servers"]} == {"palworld", "valheim"}
    assert {s["name"] for s in scoped.get("/api/servers").json()["servers"]} == {"palworld"}


def test_scoped_user_can_act_on_assigned_server_only(tmp_path):
    _, _, scoped = make_clients(tmp_path)
    assert scoped.post("/api/servers/palworld/restart").status_code == 201
    assert scoped.post("/api/servers/valheim/restart").status_code == 403
    assert scoped.get("/api/servers/valheim/detail").status_code == 403
    assert scoped.post("/api/servers/valheim/stop").status_code == 403
    # serveur inconnu : 404 (pas 403) pour tout le monde
    assert scoped.post("/api/servers/doom/restart").status_code == 404


def test_scoped_user_mods_routes_enforced(tmp_path):
    _, _, scoped = make_clients(tmp_path)
    assert scoped.post("/api/servers/palworld/mods/update-all").status_code == 200
    # valheim n'a pas de workshop mais le scoping doit primer : 403, pas 404
    assert scoped.post("/api/servers/valheim/mods/update-all").status_code == 403


def test_legacy_user_without_role_is_admin(tmp_path):
    app = make_app_two_servers(tmp_path)
    h = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    # compte pre-feature : seulement password_hash+created en base
    asyncio.run(app.state.store.create_user("ancien", h))
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    data["users"]["ancien"] = {"password_hash": h, "created": "2026-07-13T00:00:00+00:00"}
    (tmp_path / "state.json").write_text(json.dumps(data))
    c = login(app, "ancien")
    assert {s["name"] for s in c.get("/api/servers").json()["servers"]} == {"palworld", "valheim"}
    assert c.get("/api/users").status_code == 200


# --- /api/me ---

def test_me_returns_identity_and_scope(tmp_path):
    _, admin, scoped = make_clients(tmp_path)
    assert admin.get("/api/me").json() == {"username": "boss", "role": "admin", "servers": []}
    assert scoped.get("/api/me").json() == {"username": "gardien", "role": "user", "servers": ["palworld"]}


# --- gestion des comptes (admin only) ---

def test_users_management_is_admin_only(tmp_path):
    _, _, scoped = make_clients(tmp_path)
    assert scoped.get("/api/users").status_code == 403
    assert scoped.post("/api/users", json={"username": "x", "password": "longpassword123",
                                           "role": "user", "servers": []}).status_code == 403
    assert scoped.delete("/api/users/boss").status_code == 403


def test_list_users_no_password_hash(tmp_path):
    _, admin, _ = make_clients(tmp_path)
    users = admin.get("/api/users").json()["users"]
    assert {u["username"] for u in users} == {"boss", "gardien"}
    for u in users:
        assert "password_hash" not in u and "password" not in u
    gardien = next(u for u in users if u["username"] == "gardien")
    assert gardien["role"] == "user" and gardien["servers"] == ["palworld"]


def test_create_user_and_login(tmp_path):
    app, admin, _ = make_clients(tmp_path)
    r = admin.post("/api/users", json={"username": "ami", "password": "supermotdepasse1",
                                       "role": "user", "servers": ["valheim"]})
    assert r.status_code == 201
    c = login(app, "ami", "supermotdepasse1")
    assert {s["name"] for s in c.get("/api/servers").json()["servers"]} == {"valheim"}


def test_create_user_validations(tmp_path):
    _, admin, _ = make_clients(tmp_path)
    base = {"password": "supermotdepasse1", "role": "user", "servers": []}
    assert admin.post("/api/users", json={**base, "username": "gardien"}).status_code == 409
    assert admin.post("/api/users", json={**base, "username": "a b!"}).status_code == 422
    assert admin.post("/api/users", json={**base, "username": "ok", "password": "court"}).status_code == 422
    assert admin.post("/api/users", json={**base, "username": "ok", "role": "superadmin"}).status_code == 422
    r = admin.post("/api/users", json={**base, "username": "ok", "servers": ["doom"]})
    assert r.status_code == 400  # serveur inconnu de la config


def test_delete_user_guards(tmp_path):
    app, admin, _ = make_clients(tmp_path)
    assert admin.delete("/api/users/boss").status_code == 400        # pas soi-meme
    assert admin.delete("/api/users/inconnu").status_code == 404
    assert admin.delete("/api/users/gardien").status_code == 200
    # le compte supprime ne peut plus se connecter, sa session existante meurt
    c = TestClient(app)
    assert c.post("/api/login", json={"username": "gardien", "password": "testpass123"}).status_code == 401


def test_delete_last_admin_forbidden(tmp_path):
    app = make_app_two_servers(tmp_path)
    h = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("boss", h, role="admin"))
    asyncio.run(app.state.store.create_user("boss2", h, role="admin"))
    admin = login(app, "boss")
    assert admin.delete("/api/users/boss2").status_code == 200   # il reste boss
    # boss est le dernier admin : le retrograder est refuse aussi
    r = admin.post("/api/users/boss/access", json={"role": "user", "servers": ["palworld"]})
    assert r.status_code == 400


def test_deleted_user_session_is_revoked(tmp_path):
    _, admin, scoped = make_clients(tmp_path)
    assert scoped.get("/api/servers").status_code == 200
    admin.delete("/api/users/gardien")
    assert scoped.get("/api/servers").status_code == 401


def test_reset_password(tmp_path):
    app, admin, scoped = make_clients(tmp_path)
    r = admin.post("/api/users/gardien/password", json={"password": "nouveaumdp12345"})
    assert r.status_code == 200
    assert TestClient(app).post("/api/login", json={"username": "gardien", "password": "testpass123"}).status_code == 401
    # reset = revocation des sessions existantes (cas mdp compromis)
    assert scoped.get("/api/servers").status_code == 401
    login(app, "gardien", "nouveaumdp12345")


def test_change_access(tmp_path):
    app, admin, scoped = make_clients(tmp_path)
    r = admin.post("/api/users/gardien/access", json={"role": "user", "servers": ["palworld", "valheim"]})
    assert r.status_code == 200
    # prise d'effet immediate (le role est relu a chaque requete)
    assert {s["name"] for s in scoped.get("/api/servers").json()["servers"]} == {"palworld", "valheim"}


# --- auteur des ordres + historique ---

def test_order_records_author(tmp_path):
    _, _, scoped = make_clients(tmp_path)
    scoped.post("/api/servers/palworld/restart")
    scoped.get("/api/servers")  # via agent plutot
    import json
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["orders"][-1]["author"] == "gardien"


def _complete_order(app, order_id, status="done"):
    TestClient(app).post(f"/api/agent/orders/{order_id}", headers={"Authorization": "Bearer agent-t"},
                         json={"status": status, "detail": "x"})


def test_started_by_recorded_on_start_restart_update_done(tmp_path):
    app, admin, scoped = make_clients(tmp_path)
    oid = scoped.post("/api/servers/palworld/start").json()["id"]
    _complete_order(app, oid)
    pal = next(s for s in admin.get("/api/servers").json()["servers"] if s["name"] == "palworld")
    assert pal["started_by"]["author"] == "gardien"
    assert pal["started_by"]["at"]

    oid = admin.post("/api/servers/palworld/update").json()["id"]
    _complete_order(app, oid)
    pal = next(s for s in admin.get("/api/servers").json()["servers"] if s["name"] == "palworld")
    assert pal["started_by"]["author"] == "boss"  # update = stop+maj+start par l'agent


def test_started_by_not_updated_on_failed_or_stop(tmp_path):
    app, admin, _ = make_clients(tmp_path)
    oid = admin.post("/api/servers/palworld/start").json()["id"]
    _complete_order(app, oid, status="failed")
    pal = next(s for s in admin.get("/api/servers").json()["servers"] if s["name"] == "palworld")
    assert pal["started_by"] is None
    oid = admin.post("/api/servers/palworld/stop").json()["id"]
    _complete_order(app, oid)
    pal = next(s for s in admin.get("/api/servers").json()["servers"] if s["name"] == "palworld")
    assert pal["started_by"] is None


def test_started_by_admin_only(tmp_path):
    app, admin, scoped = make_clients(tmp_path)
    oid = scoped.post("/api/servers/palworld/restart").json()["id"]
    _complete_order(app, oid)
    pal_admin = next(s for s in admin.get("/api/servers").json()["servers"] if s["name"] == "palworld")
    assert pal_admin["started_by"]["author"] == "gardien"
    pal_scoped = next(s for s in scoped.get("/api/servers").json()["servers"] if s["name"] == "palworld")
    assert "started_by" not in pal_scoped


def test_detail_includes_order_history_with_author(tmp_path):
    app, admin, _ = make_clients(tmp_path)
    r = admin.post("/api/servers/palworld/restart")
    oid = r.json()["id"]
    agent = TestClient(app)
    agent.post(f"/api/agent/orders/{oid}", headers={"Authorization": "Bearer agent-t"},
               json={"status": "done", "detail": "ok"})
    hist = admin.get("/api/servers/palworld/detail").json()["order_history"]
    assert hist[0]["type"] == "restart" and hist[0]["author"] == "boss"
    assert hist[0]["status"] == "done"
    assert "password" not in str(hist)
