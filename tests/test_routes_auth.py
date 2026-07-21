import bcrypt
from fastapi.testclient import TestClient

from tests.test_sanity import make_app


def make_client_with_user(tmp_path, username="admin", password="change-me-password"):
    app = make_app(tmp_path)
    import asyncio
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user(username, password_hash))
    return TestClient(app), username, password


def test_login_page_served(tmp_path):
    r = TestClient(make_app(tmp_path)).get("/login")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_login_success_sets_cookie(tmp_path):
    client, username, password = make_client_with_user(tmp_path)
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    assert "hephaestos_session" in r.cookies


def test_login_wrong_password_401_no_cookie(tmp_path):
    client, username, _ = make_client_with_user(tmp_path)
    r = client.post("/api/login", json={"username": username, "password": "mauvais-mdp"})
    assert r.status_code == 401
    assert "hephaestos_session" not in r.cookies


def test_login_unknown_user_401_generic_message(tmp_path):
    client, _, password = make_client_with_user(tmp_path)
    r = client.post("/api/login", json={"username": "fantome", "password": password})
    assert r.status_code == 401
    assert "identifiants" in r.json()["detail"].lower()


def test_logout_clears_cookie(tmp_path):
    client, username, password = make_client_with_user(tmp_path)
    client.post("/api/login", json={"username": username, "password": password})
    r = client.post("/api/logout")
    assert r.status_code == 200
    assert client.cookies.get("hephaestos_session") is None


def test_login_rate_limited_after_5_failures(tmp_path):
    from app.routes_auth import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()  # isole du reste de la suite : IP simulee partagee par TestClient
    client, username, password = make_client_with_user(tmp_path)
    for _ in range(5):
        r = client.post("/api/login", json={"username": username, "password": "mauvais"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"username": username, "password": "mauvais"})
    assert r.status_code == 429
    # meme avec le BON mot de passe, toujours bloque pendant la fenetre
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 429


def test_login_success_resets_rate_limit(tmp_path):
    from app.routes_auth import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()  # isole du reste de la suite : IP simulee partagee par TestClient
    client, username, password = make_client_with_user(tmp_path)
    for _ in range(4):
        client.post("/api/login", json={"username": username, "password": "mauvais"})
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    # apres un succes, un nouvel echec ne doit pas etre immediatement bloque (compteur remis a zero)
    r = client.post("/api/login", json={"username": username, "password": "mauvais"})
    assert r.status_code == 401
