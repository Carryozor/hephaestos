"""Première configuration (first-run) : création du compte administrateur initial
via l'UI, sans passer par un accès shell au conteneur. La route n'est ouverte que
tant qu'AUCUN compte n'existe — une fois le premier admin créé, elle est verrouillée
(sinon n'importe qui pourrait s'octroyer un compte admin)."""
import asyncio

import httpx
from fastapi.testclient import TestClient

from tests.test_sanity import make_app, make_logged_in_client

VALID_PW = "change-me-password"  # ≥ 12 caractères


def test_setup_needed_true_when_no_users(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/setup/needed").json() == {"needed": True}


def test_setup_needed_false_when_user_exists(tmp_path):
    client = make_logged_in_client(tmp_path)
    assert client.get("/api/setup/needed").json() == {"needed": False}


def test_setup_creates_first_admin_and_auto_logs_in(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/setup", json={"username": "admin", "password": VALID_PW})
    assert r.status_code == 201
    assert r.json()["ok"] is True
    assert "hephaestos_session" in r.cookies  # auto-login : session posée
    # la session est valide : accès à une route protégée
    assert client.get("/api/servers").status_code == 200
    # setup désormais verrouillé
    assert client.get("/api/setup/needed").json() == {"needed": False}


def test_setup_grants_admin_role(tmp_path):
    client = TestClient(make_app(tmp_path))
    client.post("/api/setup", json={"username": "boss", "password": VALID_PW})
    # rôle admin => accès à la gestion des comptes (require_admin_role)
    assert client.get("/api/users").status_code == 200


def test_setup_rejected_when_a_user_already_exists(tmp_path):
    client = make_logged_in_client(tmp_path)  # un compte existe déjà
    r = client.post("/api/setup", json={"username": "intrus", "password": VALID_PW})
    assert r.status_code == 409


def test_setup_rejects_short_password(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/setup", json={"username": "admin", "password": "court"})
    assert r.status_code == 422


def test_setup_rejects_invalid_username(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/setup", json={"username": "a b!", "password": VALID_PW})
    assert r.status_code == 422


async def test_setup_concurrent_different_usernames_creates_only_one_admin(tmp_path, monkeypatch):
    """Course sur le verrou anti-takeover : deux POST /api/setup concurrents avec des
    usernames DIFFERENTS (pas le meme, deja couvert par test_setup_rejected_when_a_user_already_exists)
    ne doivent jamais aboutir tous les deux -- sinon un second admin invisible peut se
    glisser dans la fenetre de premier demarrage, avant que l'operateur legitime n'ait
    fini sa propre configuration."""
    app = make_app(tmp_path)
    orig_list_users = app.state.store.list_users

    async def slow_list_users():
        # Elargit artificiellement la fenetre entre le check "aucun compte" et la
        # creation, pour rendre la course reproductible sans dependre du hasard de
        # l'ordonnanceur asyncio.
        result = await orig_list_users()
        await asyncio.sleep(0.05)
        return result

    monkeypatch.setattr(app.state.store, "list_users", slow_list_users)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1, r2 = await asyncio.gather(
            client.post("/api/setup", json={"username": "alice", "password": VALID_PW}),
            client.post("/api/setup", json={"username": "bob", "password": VALID_PW}),
        )

    assert sorted([r1.status_code, r2.status_code]) == [201, 409]
    assert len(await app.state.store.list_users()) == 1
