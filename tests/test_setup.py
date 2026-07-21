"""Première configuration (first-run) : création du compte administrateur initial
via l'UI, sans passer par un accès shell au conteneur. La route n'est ouverte que
tant qu'AUCUN compte n'existe — une fois le premier admin créé, elle est verrouillée
(sinon n'importe qui pourrait s'octroyer un compte admin)."""
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
