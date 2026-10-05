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


# --- T2.1 : rate-limit non contournable par la concurrence ---------------------------

async def _concurrent_client(tmp_path, password="correct horse battery"):
    import httpx

    from app import routes_auth
    routes_auth._LOGIN_ATTEMPTS.clear()
    app = make_app(tmp_path)
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    await app.state.store.create_user("admin", hashed)
    transport = httpx.ASGITransport(app=app, client=("1.2.3.4", 5555))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_concurrent_wrong_logins_capped_by_rate_limit(tmp_path):
    import asyncio
    from collections import Counter
    async with await _concurrent_client(tmp_path) as c:
        rs = await asyncio.gather(*[
            c.post("/api/login", json={"username": "admin", "password": f"faux-{i}-xxxxxxxx"})
            for i in range(30)])
    counts = Counter(r.status_code for r in rs)
    assert counts[401] <= 5 and counts[401] + counts[429] == 30, counts


async def test_valid_login_works_after_concurrent_flood_reset(tmp_path):
    import asyncio

    from app import routes_auth
    async with await _concurrent_client(tmp_path) as c:
        await asyncio.gather(*[
            c.post("/api/login", json={"username": "admin", "password": f"faux-{i}-xxxxxxxx"})
            for i in range(10)])
        routes_auth._LOGIN_ATTEMPTS.clear()
        r = await c.post("/api/login", json={"username": "admin",
                                             "password": "correct horse battery"})
    assert r.status_code == 200


async def test_legit_user_failing_twice_then_succeeding_not_blocked(tmp_path):
    async with await _concurrent_client(tmp_path) as c:
        for _ in range(2):
            r = await c.post("/api/login", json={"username": "admin", "password": "mauvais-mdp-xx"})
            assert r.status_code == 401
        ok = await c.post("/api/login", json={"username": "admin",
                                              "password": "correct horse battery"})
        assert ok.status_code == 200
        # compteur remis a zero : 5 nouveaux echecs permis avant le 429
        codes = [(await c.post("/api/login", json={"username": "admin",
                                                    "password": "mauvais-mdp-xx"})).status_code
                 for _ in range(6)]
    assert codes == [401] * 5 + [429]


async def test_concurrent_bcrypt_checks_are_bounded(tmp_path, monkeypatch):
    import asyncio

    from app import routes_auth
    running = peak = 0

    async def slow_verify(password, password_hash):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return False
    monkeypatch.setattr(routes_auth, "verify_password", slow_verify)
    routes_auth._LOGIN_ATTEMPTS.clear()
    async with await _concurrent_client(tmp_path) as c:
        # IPs distinctes : le rate-limit par IP ne masque pas la borne de concurrence
        import httpx
        async def one(i):
            t = httpx.ASGITransport(app=c._transport.app, client=(f"9.9.9.{i}", 1))
            async with httpx.AsyncClient(transport=t, base_url="http://t") as cc:
                return await cc.post("/api/login", json={"username": "admin", "password": "x" * 12})
        await asyncio.gather(*[one(i) for i in range(20)])
    assert 1 <= peak <= routes_auth._MAX_CONCURRENT_CHECKS
