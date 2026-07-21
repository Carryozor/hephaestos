"""Backups de saves (ordres backup/restore_save) + notification webhook sur echec."""
import asyncio
import json
from datetime import UTC

import bcrypt
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SERVERS = {"palworld": {"display_name": "Palworld", "server_appid": 2394010}}
BACKUPS = [
    {"file": "20260717-020000-pre-update.zip", "size_mb": 71.2, "created": "2026-07-17T02:00:00+00:00"},
    {"file": "20260716-020000-daily.zip", "size_mb": 70.9, "created": "2026-07-16T02:00:00+00:00"},
]


def make_app_saves(tmp_path, alert_webhook=None, captured=None):
    def handler(request):
        url = str(request.url)
        if captured is not None and "hooks.example" in url:
            captured.append({"url": url, "body": json.loads(request.content.decode())})
            return httpx.Response(200, json={"ok": True})
        if "steamcmd.net" in url:
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(agent_token="agent-t", data_dir=tmp_path, servers=SERVERS,
                        alert_webhook=alert_webhook)
    app = create_app(settings, http_client=client)
    h = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("boss", h, role="admin"))
    asyncio.run(
        app.state.store.create_user("gardien", h, role="user", servers=["palworld"]))
    return app


def login(app, username):
    c = TestClient(app)
    assert c.post("/api/login", json={"username": username, "password": "testpass123"}).status_code == 200
    return c


def report_backups(app, backups=BACKUPS):
    c = TestClient(app)
    r = c.post("/api/agent/state", headers={"Authorization": "Bearer agent-t"}, json={
        "servers": {"palworld": {"process_up": True, "players": 0, "save_backups": backups}}
    })
    assert r.status_code == 200


# --- rapport agent + exposition ---

def test_save_backups_reported_and_exposed_in_detail(tmp_path):
    app = make_app_saves(tmp_path)
    report_backups(app)
    c = login(app, "boss")
    detail = c.get("/api/servers/palworld/detail").json()
    assert detail["save_backups"] == BACKUPS


def test_save_backups_empty_when_never_reported(tmp_path):
    app = make_app_saves(tmp_path)
    c = login(app, "boss")
    assert c.get("/api/servers/palworld/detail").json()["save_backups"] == []


# --- ordre backup manuel ---

def test_manual_backup_creates_order(tmp_path):
    app = make_app_saves(tmp_path)
    c = login(app, "gardien")  # compte scope : autorise sur son serveur
    r = c.post("/api/servers/palworld/saves/backup")
    assert r.status_code == 201
    order = r.json()
    assert order["type"] == "backup" and order["author"] == "gardien"
    assert c.post("/api/servers/palworld/saves/backup").status_code == 409


# --- restauration (admin only) ---

def test_restore_requires_admin(tmp_path):
    app = make_app_saves(tmp_path)
    report_backups(app)
    c = login(app, "gardien")
    r = c.post("/api/servers/palworld/saves/restore", json={"file": BACKUPS[0]["file"]})
    assert r.status_code == 403


def test_restore_creates_order_for_known_backup(tmp_path):
    app = make_app_saves(tmp_path)
    report_backups(app)
    c = login(app, "boss")
    r = c.post("/api/servers/palworld/saves/restore", json={"file": BACKUPS[1]["file"]})
    assert r.status_code == 201
    order = r.json()
    assert order["type"] == "restore_save" and order["backup_file"] == BACKUPS[1]["file"]
    assert order["author"] == "boss"
    assert c.post("/api/servers/palworld/saves/restore",
                  json={"file": BACKUPS[0]["file"]}).status_code == 409  # un restore a la fois


def test_restore_rejects_unknown_or_malicious_file(tmp_path):
    app = make_app_saves(tmp_path)
    report_backups(app)
    c = login(app, "boss")
    # zip jamais rapporte par l'agent -> 404 (pas d'ordre aveugle)
    assert c.post("/api/servers/palworld/saves/restore",
                  json={"file": "20990101-000000-daily.zip"}).status_code == 404
    # traversal / format invalide -> 422 (pattern pydantic)
    for bad in ["../../secret.zip", "a\\b.zip", "x.txt", ".zip", "a/b.zip"]:
        assert c.post("/api/servers/palworld/saves/restore",
                      json={"file": bad}).status_code == 422, bad


# --- notification webhook sur echec ---

def test_failed_order_triggers_alert_webhook(tmp_path):
    captured = []
    app = make_app_saves(tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", captured=captured)
    c = login(app, "boss")
    oid = c.post("/api/servers/palworld/restart").json()["id"]
    agent = TestClient(app)
    agent.post(f"/api/agent/orders/{oid}", headers={"Authorization": "Bearer agent-t"},
               json={"status": "failed", "detail": "exception: boum"})
    assert len(captured) == 1
    body = captured[0]["body"]
    assert "palworld" in body["content"] and "restart" in body["content"] and "boum" in body["content"]


def test_done_order_triggers_no_alert(tmp_path):
    captured = []
    app = make_app_saves(tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", captured=captured)
    c = login(app, "boss")
    oid = c.post("/api/servers/palworld/restart").json()["id"]
    TestClient(app).post(f"/api/agent/orders/{oid}", headers={"Authorization": "Bearer agent-t"},
                         json={"status": "done", "detail": "ok"})
    assert captured == []


def test_no_webhook_configured_is_fine(tmp_path):
    app = make_app_saves(tmp_path, alert_webhook=None)
    c = login(app, "boss")
    oid = c.post("/api/servers/palworld/restart").json()["id"]
    r = TestClient(app).post(f"/api/agent/orders/{oid}", headers={"Authorization": "Bearer agent-t"},
                             json={"status": "failed", "detail": "boum"})
    assert r.status_code == 200  # pas d'erreur, juste pas d'alerte


def test_expired_order_triggers_alert_webhook_on_agent_poll(tmp_path):
    # un ordre qui meurt d'expiration (24h sans reponse agent) est aussi grave qu'un
    # failed rapporte : le poll agent suivant doit envoyer l'alerte webhook
    import asyncio as _asyncio
    from datetime import datetime, timedelta

    captured = []
    app = make_app_saves(tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", captured=captured)
    order = _asyncio.run(
        app.state.store.add_order("palworld", "update", author="boss"))
    state_path = tmp_path / "state.json"
    data = json.loads(state_path.read_text())
    for o in data["orders"]:
        if o["id"] == order["id"]:
            o["created"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    state_path.write_text(json.dumps(data))

    c = TestClient(app)
    r = c.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"})
    assert r.status_code == 200
    assert r.json()["orders"] == []  # l'ordre expire n'est plus servi
    assert len(captured) == 1
    body = captured[0]["body"]["content"]
    assert "expir" in body and "palworld" in body and "update" in body and "boss" in body

    # second poll : pas de double alerte
    c.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"})
    assert len(captured) == 1
