"""Endpoint /metrics (format Prometheus) : couvre les metriques par serveur deja
collectees par l'agent mais jamais exposees en serie temporelle. Public (aucune
auth) : Prometheus scrape sans gerer de cookie de session, meme modele de
confiance que /api/public/health/{name}."""
from fastapi.testclient import TestClient

from tests.test_sanity import make_app


def test_metrics_public_no_auth_required(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_metrics_empty_registry_still_returns_totals(tmp_path):
    client = TestClient(make_app(tmp_path))
    body = client.get("/metrics").text
    assert "hephaestos_orders_pending_total 0" in body
    # aucun serveur n'a encore rapporte d'etat : pas de ligne process_up
    assert 'hephaestos_server_up{server="palworld"}' not in body


def test_metrics_reflects_reported_server_state(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/agent/state", json={
        "servers": {"palworld": {
            "process_up": True, "players": 3,
            "process_cpu_percent": 12.5, "process_mem_mb": 2048.0,
        }},
    }, headers={"Authorization": "Bearer agent-t"})
    assert r.status_code == 200

    body = client.get("/metrics").text
    assert 'hephaestos_server_up{server="palworld"} 1' in body
    assert 'hephaestos_server_players{server="palworld"} 3' in body
    assert 'hephaestos_server_cpu_percent{server="palworld"} 12.5' in body
    assert 'hephaestos_server_mem_mb{server="palworld"} 2048.0' in body
    # buildid public mocke = 24088465 (STEAM_JSON) ; aucun buildid local rapporte
    # ici -> update_available non calculable, pas de ligne emise
    assert 'hephaestos_server_update_available{server="palworld"}' not in body


def test_metrics_orders_pending_counts_per_server_and_total(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/agent/state", json={"servers": {}},
                headers={"Authorization": "Bearer agent-t"})
    import asyncio
    asyncio.run(app.state.store.add_order("palworld", "restart"))

    body = client.get("/metrics").text
    assert 'hephaestos_server_orders_pending{server="palworld"} 1' in body
    assert "hephaestos_orders_pending_total 1" in body


def test_metrics_crash_breaker_tripped_reflected(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    import asyncio
    asyncio.run(app.state.store.set_crash_recovery("palworld", ["x"], "2026-08-10T00:00:00+00:00", 0))

    body = client.get("/metrics").text
    assert 'hephaestos_server_crash_breaker_tripped{server="palworld"} 1' in body


def test_metrics_agent_last_seen_absent_before_any_report(tmp_path):
    client = TestClient(make_app(tmp_path))
    body = client.get("/metrics").text
    assert "hephaestos_agent_last_seen_seconds" not in body
