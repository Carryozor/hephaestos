from fastapi.testclient import TestClient

from tests.test_sanity import make_app, make_logged_in_client

AGT = {"Authorization": "Bearer agent-t"}


def test_require_admin_no_cookie_401(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/servers").status_code == 401


def test_require_admin_valid_session_200(tmp_path):
    client = make_logged_in_client(tmp_path)
    assert client.get("/api/servers").status_code == 200


def test_require_admin_garbage_cookie_401(tmp_path):
    client = TestClient(make_app(tmp_path))
    client.cookies.set("hephaestos_session", "token-invente-au-hasard")
    assert client.get("/api/servers").status_code == 401


def test_require_agent_matrix(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/agent/orders").status_code == 401
    assert client.get("/api/agent/orders", headers=AGT).status_code == 200
    # un cookie de session admin valide ne doit PAS suffire sur les routes agent
    admin_client = make_logged_in_client(tmp_path)
    assert admin_client.get("/api/agent/orders").status_code == 401


def test_require_admin_purges_expired_sessions_periodically(tmp_path):
    import asyncio
    import json

    from app import auth
    client = make_logged_in_client(tmp_path)
    # cree une session expiree directement via le store de l'app
    store = client.app.state.store
    expired_token = asyncio.run(store.create_session("ghost", ttl_days=30))
    data = json.loads((tmp_path / "state.json").read_text())
    data["sessions"][expired_token]["expires"] = "2020-01-01T00:00:00+00:00"
    (tmp_path / "state.json").write_text(json.dumps(data))

    auth._purge_call_counter = auth._PURGE_EVERY_N_CALLS - 1  # force le declenchement au prochain appel
    client.get("/api/servers")  # declenche require_admin -> purge

    remaining = json.loads((tmp_path / "state.json").read_text())["sessions"]
    assert expired_token not in remaining


# --- Durcissement 2026-07-21 : cookie Secure conditionne a l'env ---

def test_session_cookie_secure_off_by_default(monkeypatch):
    from fastapi import Response

    from app.auth import set_session_cookie
    monkeypatch.delenv("HEPHAESTOS_COOKIE_SECURE", raising=False)
    r = Response()
    set_session_cookie(r, "tok")
    assert "secure" not in r.headers["set-cookie"].lower()


def test_session_cookie_secure_on_when_env_set(monkeypatch):
    from fastapi import Response

    from app.auth import set_session_cookie
    monkeypatch.setenv("HEPHAESTOS_COOKIE_SECURE", "1")
    r = Response()
    set_session_cookie(r, "tok")
    assert "secure" in r.headers["set-cookie"].lower()
