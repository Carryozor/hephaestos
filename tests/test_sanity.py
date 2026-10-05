import asyncio

import bcrypt
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

STEAM_JSON = {"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}}

def make_settings(tmp_path):
    return Settings(agent_token="agent-t",
                    data_dir=tmp_path, servers={"palworld": {"display_name": "Palworld", "server_appid": 2394010}})

def activate_all_servers(app):
    """Le registre seede les serveurs jamais rapportes en "disabled" ; B2.4 refuse les
    ordres d'execution sur un serveur non actif : les fixtures d'ordres partent d'un
    registre ACTIF (les tests de statut non actif le fixent explicitement)."""
    # synchrone a dessein (_load/_dump, sans verrou) : appelable aussi depuis un test
    # async, ou asyncio.run() est interdit ; l'app ne sert encore aucune requete.
    store = app.state.store
    data = store._load()
    for entry in data.get("servers_registry", {}).values():
        entry["status"] = "active"
    store._dump(data)
    return app


def make_app(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STEAM_JSON)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return activate_all_servers(create_app(make_settings(tmp_path), http_client=client))

def make_logged_in_client(tmp_path, username="tester", password="testpass123"):
    """Cree un utilisateur directement via le Store puis se logue via l'API.
    Retourne un TestClient authentifie : le cookie de session est conserve
    automatiquement par httpx pour toutes les requetes suivantes sur ce client."""
    app = make_app(tmp_path)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user(username, password_hash))
    client = TestClient(app)
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return client

def test_health(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/health").json() == {"ok": True}
