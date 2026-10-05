"""Domaine B1 : mots de passe (limite bcrypt 72 octets), sessions hachees,
permissions de state.json, purge du contenu des ordres, en-tetes de securite,
sante, purge file_read par horloge."""
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.routes_auth import _LOGIN_ATTEMPTS
from app.storage import Store
from tests.test_sanity import make_app, make_logged_in_client

LONG_PW = "€" * 30          # 30 caracteres = 90 octets UTF-8 (> 72)
OK_PW = "mot-de-passe-valide-1"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    _LOGIN_ATTEMPTS.clear()
    yield
    _LOGIN_ATTEMPTS.clear()


def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- B1.1 mots de passe

def test_create_user_rejects_password_over_72_bytes(tmp_path):
    c = make_logged_in_client(tmp_path)
    r = c.post("/api/users", json={"username": "bob", "password": LONG_PW, "role": "user"})
    assert r.status_code == 422


def test_reset_password_rejects_password_over_72_bytes(tmp_path):
    c = make_logged_in_client(tmp_path)
    r = c.post("/api/users/tester/password", json={"password": LONG_PW})
    assert r.status_code == 422


def test_setup_rejects_password_over_72_bytes(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.post("/api/setup", json={"username": "root", "password": LONG_PW})
    assert r.status_code == 422


def test_password_of_exactly_72_bytes_is_accepted(tmp_path):
    c = make_logged_in_client(tmp_path)
    pw = "a" * 72
    r = c.post("/api/users", json={"username": "bob", "password": pw, "role": "user"})
    assert r.status_code == 201


def test_login_with_too_long_password_is_401_and_counted(tmp_path):
    app = make_app(tmp_path)
    asyncio.run(app.state.store.create_user("alice", _hash(OK_PW)))
    c = TestClient(app)
    r = c.post("/api/login", json={"username": "alice", "password": LONG_PW})
    assert r.status_code == 401
    assert "identifiants" in r.json()["detail"].lower()
    assert len(_LOGIN_ATTEMPTS["testclient"]) == 1


def test_login_bcrypt_valueerror_is_401_and_counted(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    asyncio.run(app.state.store.create_user("alice", _hash(OK_PW)))

    def boom(*a, **k):
        raise ValueError("hash invalide")
    monkeypatch.setattr(bcrypt, "checkpw", boom)
    r = TestClient(app).post("/api/login", json={"username": "alice", "password": OK_PW})
    assert r.status_code == 401
    assert len(_LOGIN_ATTEMPTS["testclient"]) == 1


def test_login_unknown_user_runs_dummy_checkpw(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = []
    real = bcrypt.checkpw

    def spy(pw, h):
        calls.append(h)
        return real(pw, h)
    monkeypatch.setattr(bcrypt, "checkpw", spy)
    r = TestClient(app).post("/api/login", json={"username": "fantome", "password": OK_PW})
    assert r.status_code == 401
    assert len(calls) == 1


def test_bcrypt_runs_off_event_loop(tmp_path, monkeypatch):
    """login, creation, reset et setup passent par asyncio.to_thread."""
    names = []
    real = asyncio.to_thread

    async def spy(func, *a, **k):
        names.append(getattr(func, "__name__", str(func)))
        return await real(func, *a, **k)
    monkeypatch.setattr(asyncio, "to_thread", spy)

    c = TestClient(make_app(tmp_path))
    assert c.post("/api/setup", json={"username": "root", "password": OK_PW}).status_code == 201
    assert "hashpw" in names                                   # setup
    names.clear()
    assert c.post("/api/users", json={"username": "bob", "password": OK_PW, "role": "user"}).status_code == 201
    assert "hashpw" in names                                   # creation
    names.clear()
    assert c.post("/api/users/bob/password", json={"password": OK_PW + "x"}).status_code == 200
    assert "hashpw" in names                                   # reset
    names.clear()
    assert c.post("/api/login", json={"username": "bob", "password": OK_PW + "x"}).status_code == 200
    assert "checkpw" in names                                  # login


def _load_create_user():
    spec = importlib.util.spec_from_file_location(
        "create_user_script", Path(__file__).parent.parent / "deploy" / "create-user.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_create_user_script_rejects_password_over_72_bytes(monkeypatch, capsys):
    mod = _load_create_user()

    def no_store(*a, **k):
        raise AssertionError("le Store (/data) ne doit pas etre ouvert avant la validation")
    monkeypatch.setattr(mod, "Store", no_store)
    monkeypatch.setattr(sys, "argv", ["create-user.py", "alice", LONG_PW])
    assert asyncio.run(mod.main()) == 1
    assert "72" in capsys.readouterr().err


# ---------------------------------------------------------------- B1.2 sessions hachees

async def test_session_key_stored_is_sha256_not_raw_token(tmp_path):
    s = Store(tmp_path / "state.json")
    token = await s.create_session("admin")
    sessions = json.loads((tmp_path / "state.json").read_text())["sessions"]
    assert token not in sessions
    assert _sha(token) in sessions
    assert (await s.get_session(token))["username"] == "admin"


async def test_session_token_hashed_across_renew_delete_purge(tmp_path):
    s = Store(tmp_path / "state.json")
    token = await s.create_session("admin")
    data = s._load()
    data["sessions"][_sha(token)]["expires"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    s._dump(data)
    await s.renew_session(token, ttl_days=30)
    renewed = datetime.fromisoformat(s._load()["sessions"][_sha(token)]["expires"])
    assert renewed > datetime.now(UTC) + timedelta(days=29)
    await s.delete_session(token)
    assert await s.get_session(token) is None
    assert s._load()["sessions"] == {}


async def test_delete_user_revokes_hashed_sessions(tmp_path):
    s = Store(tmp_path / "state.json")
    await s.create_user("bob", "h")
    token = await s.create_session("bob")
    await s.delete_user("bob")
    assert await s.get_session(token) is None
    assert s._load()["sessions"] == {}


async def test_set_user_password_revokes_hashed_sessions(tmp_path):
    s = Store(tmp_path / "state.json")
    await s.create_user("bob", "h")
    token = await s.create_session("bob")
    await s.set_user_password("bob", "h2")
    assert await s.get_session(token) is None


async def test_legacy_raw_sessions_invalidated_on_load(tmp_path, caplog):
    p = tmp_path / "state.json"
    legacy = {"servers": {}, "orders": [], "users": {"admin": {"password_hash": "h"}},
              "sessions": {"rawtoken": {"username": "admin",
                                        "expires": (datetime.now(UTC) + timedelta(days=5)).isoformat()}}}
    p.write_text(json.dumps(legacy))
    with caplog.at_level(logging.WARNING):
        s = Store(p)
    assert await s.get_session("rawtoken") is None
    data = json.loads(p.read_text())
    assert data["sessions"] == {}
    assert data["sessions_hashed"] is True
    assert any("session" in r.message.lower() for r in caplog.records)


async def test_session_migration_is_idempotent(tmp_path):
    p = tmp_path / "state.json"
    s = Store(p)
    token = await s.create_session("admin")
    s2 = Store(p)                       # second chargement : ne doit rien vider
    assert (await s2.get_session(token))["username"] == "admin"
    assert json.loads(p.read_text())["sessions_hashed"] is True


def test_legacy_cookie_401_then_new_login_ok(tmp_path):
    app = make_app(tmp_path)
    asyncio.run(app.state.store.create_user("alice", _hash(OK_PW)))
    # reouverture sur un state.json legacy (sessions brutes, pas de marqueur)
    p = tmp_path / "state.json"
    data = json.loads(p.read_text())
    data.pop("sessions_hashed", None)
    data["sessions"] = {"oldtoken": {"username": "alice",
                                     "expires": (datetime.now(UTC) + timedelta(days=5)).isoformat()}}
    p.write_text(json.dumps(data))
    app2 = make_app(tmp_path)
    c = TestClient(app2)
    c.cookies.set("hephaestos_session", "oldtoken")
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/login", json={"username": "alice", "password": OK_PW}).status_code == 200
    assert c.get("/api/me").status_code == 200
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").status_code == 401


# ---------------------------------------------------------------- B1.3 permissions

def test_state_file_and_tmp_written_0600(tmp_path):
    s = Store(tmp_path / "state.json")
    os.chmod(tmp_path / "state.json", 0o644)      # simule un fichier herite trop permissif
    s._dump({"servers": {}, "orders": [], "users": {}, "sessions": {}})
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


def test_fresh_state_file_is_0600(tmp_path):
    Store(tmp_path / "state.json")
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


# ---------------------------------------------------------------- B1.4 purge de contenu

PAYLOAD = {"path": "a.ini", "content_b64": "c2VjcmV0", "expected_sha256": "ab" * 32}


def _stored(s: Store, order_id: str) -> dict:
    return next(o for o in s._load()["orders"] if o["id"] == order_id)


@pytest.mark.parametrize("status", ["done", "failed"])
async def test_terminal_status_purges_write_file_content(tmp_path, status):
    s = Store(tmp_path / "state.json")
    o = await s.add_order("palworld", "write_file", PAYLOAD)
    res = await s.set_order_status(o["id"], status, "x")
    for d in (_stored(s, o["id"]), res):
        assert "content_b64" not in d and "expected_sha256" not in d
    assert _stored(s, o["id"])["path"] == "a.ini"


async def test_running_status_keeps_content(tmp_path):
    s = Store(tmp_path / "state.json")
    o = await s.add_order("palworld", "write_file", PAYLOAD)
    await s.set_order_status(o["id"], "running")
    assert _stored(s, o["id"])["content_b64"] == "c2VjcmV0"


async def test_cancel_order_purges_content(tmp_path):
    s = Store(tmp_path / "state.json")
    o = await s.add_order("palworld", "write_file", PAYLOAD)
    res = await s.cancel_order(o["id"])
    for d in (_stored(s, o["id"]), res):
        assert "content_b64" not in d and "expected_sha256" not in d


async def test_expiry_purges_content(tmp_path):
    s = Store(tmp_path / "state.json")
    o = await s.add_order("palworld", "write_file", PAYLOAD)
    data = s._load()
    data["orders"][0]["created"] = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    s._dump(data)
    await s.pending_orders()
    stored = _stored(s, o["id"])
    assert stored["status"] == "failed"
    assert "content_b64" not in stored and "expected_sha256" not in stored


# ---------------------------------------------------------------- B1.5 reponse d'annulation

def test_cancel_order_response_hides_content_for_role_user(tmp_path):
    app = make_app(tmp_path)
    store = app.state.store
    asyncio.run(store.create_user("gardien", _hash(OK_PW), role="user", servers=["palworld"]))
    o = asyncio.run(store.add_order("palworld", "write_file", PAYLOAD, author="x"))
    c = TestClient(app)
    assert c.post("/api/login", json={"username": "gardien", "password": OK_PW}).status_code == 200
    r = c.delete(f"/api/servers/palworld/orders/{o['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "type", "status", "detail", "server"}
    assert body["status"] == "failed" and body["id"] == o["id"]


# ---------------------------------------------------------------- B1.6 docs + en-tetes

def test_docs_and_openapi_disabled(tmp_path):
    c = TestClient(make_app(tmp_path))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
}


@pytest.mark.parametrize("path", ["/api/health", "/", "/static/app.js"])
def test_security_headers_present(tmp_path, path):
    r = TestClient(make_app(tmp_path)).get(path)
    assert r.status_code == 200
    for k, v in EXPECTED_HEADERS.items():
        assert r.headers.get(k) == v, k
    assert "script-src" not in r.headers["content-security-policy"]
    assert "default-src" not in r.headers["content-security-policy"]


def test_security_headers_keep_set_cookie(tmp_path):
    app = make_app(tmp_path)
    asyncio.run(app.state.store.create_user("alice", _hash(OK_PW)))
    r = TestClient(app).post("/api/login", json={"username": "alice", "password": OK_PW})
    assert "hephaestos_session=" in r.headers["set-cookie"]
    assert r.headers["x-frame-options"] == "DENY"


# ---------------------------------------------------------------- B1.7 health

def test_health_503_when_store_snapshot_fails(tmp_path, monkeypatch):
    app = make_app(tmp_path)

    async def boom():
        raise OSError("disque en lecture seule")
    monkeypatch.setattr(app.state.store, "snapshot", boom)
    assert TestClient(app).get("/api/health").status_code == 503


def test_health_ok_when_store_readable(tmp_path):
    assert TestClient(make_app(tmp_path)).get("/api/health").json() == {"ok": True}


# ---------------------------------------------------------------- B1.8 purge file_read par horloge

def test_file_read_purge_is_clock_based(tmp_path, monkeypatch):
    c = make_logged_in_client(tmp_path)
    store = c.app.state.store
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return 0
    monkeypatch.setattr(store.registry, "purge_stale_file_reads", spy)
    now = [1000.0]
    monkeypatch.setattr(auth, "_clock", lambda: now[0])
    monkeypatch.setattr(auth, "_last_purge_at", None)

    c.get("/api/me")
    assert len(calls) == 1               # premiere requete : purge
    now[0] += 30
    for _ in range(60):                  # beaucoup d'appels, < 60 s : aucune purge
        c.get("/api/me")
    assert len(calls) == 1
    now[0] += 31
    c.get("/api/me")
    assert len(calls) == 2


# ---------------------------------------------------------------- T2.5 cache de /api/health

def _counting_snapshot(app, monkeypatch, fail=False):
    calls = []
    real = app.state.store.snapshot

    async def counted():
        calls.append(1)
        if fail:
            raise OSError("disque en lecture seule")
        return await real()
    monkeypatch.setattr(app.state.store, "snapshot", counted)
    return calls


def _fake_clock(app):
    now = [1000.0]
    app.state.health_clock = lambda: now[0]
    return now


def test_health_success_cached_5s_then_refreshed(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = _counting_snapshot(app, monkeypatch)
    now = _fake_clock(app)
    c = TestClient(app)
    assert c.get("/api/health").json() == {"ok": True}
    now[0] += 4.9
    assert c.get("/api/health").json() == {"ok": True}
    assert len(calls) == 1
    now[0] += 0.2  # 5,1 s apres le 1er appel : cache expire
    assert c.get("/api/health").json() == {"ok": True}
    assert len(calls) == 2


def test_health_failure_cached_at_most_about_1s_and_recovery_visible(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = _counting_snapshot(app, monkeypatch, fail=True)
    now = _fake_clock(app)
    c = TestClient(app)
    assert c.get("/api/health").status_code == 503
    now[0] += 0.5
    assert c.get("/api/health").status_code == 503
    assert len(calls) == 1
    now[0] += 0.6  # > 1 s : l'echec n'est plus servi du cache
    monkeypatch.undo()
    assert c.get("/api/health").status_code == 200
