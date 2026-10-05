"""Endpoint /metrics (format Prometheus) : couvre les metriques par serveur deja
collectees par l'agent mais jamais exposees en serie temporelle. Public (aucune
auth) : Prometheus scrape sans gerer de cookie de session, meme modele de
confiance que /api/public/health/{name}."""
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.freshness import STATE_STALE_SECONDS
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


def _post_state(client, **state):
    r = client.post("/api/agent/state", json={"servers": {"palworld": state}},
                    headers={"Authorization": "Bearer agent-t"})
    assert r.status_code == 200


def _patch_server_state(tmp_path, **fields):
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    data["servers"]["palworld"].update(fields)
    path.write_text(json.dumps(data))


def test_metrics_stale_state_omits_up_players_uptime_and_flags_stale(tmp_path):
    client = TestClient(make_app(tmp_path))
    _post_state(client, process_up=True, players=3,
                process_started_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    old = (datetime.now(UTC) - timedelta(seconds=STATE_STALE_SECONDS + 60)).isoformat()
    _patch_server_state(tmp_path, last_seen=old)
    body = client.get("/metrics").text
    assert 'hephaestos_server_up{server="palworld"}' not in body
    assert 'hephaestos_server_players{server="palworld"}' not in body
    assert 'hephaestos_server_uptime_seconds{server="palworld"}' not in body
    assert 'hephaestos_server_state_stale{server="palworld"} 1' in body


def test_metrics_fresh_state_keeps_up_and_flags_not_stale(tmp_path):
    client = TestClient(make_app(tmp_path))
    _post_state(client, process_up=True, players=3,
                process_started_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    body = client.get("/metrics").text
    assert 'hephaestos_server_up{server="palworld"} 1' in body
    assert 'hephaestos_server_uptime_seconds{server="palworld"}' in body
    assert 'hephaestos_server_state_stale{server="palworld"} 0' in body


def test_metrics_server_without_state_is_not_flagged_stale_nor_up(tmp_path):
    client = TestClient(make_app(tmp_path))
    body = client.get("/metrics").text
    assert 'hephaestos_server_up{server="palworld"}' not in body
    assert 'hephaestos_server_state_stale{server="palworld"} 0' in body


def test_metrics_invalid_process_started_at_does_not_500(tmp_path):
    client = TestClient(make_app(tmp_path))
    _post_state(client, process_up=True, players=1, process_started_at="pas-une-date")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert 'hephaestos_server_up{server="palworld"} 1' in r.text
    assert 'hephaestos_server_uptime_seconds{server="palworld"}' not in r.text


def test_metrics_missing_or_garbage_last_seen_counts_as_stale(tmp_path):
    client = TestClient(make_app(tmp_path))
    _post_state(client, process_up=True, players=1)
    _patch_server_state(tmp_path, last_seen="n'importe quoi")
    body = client.get("/metrics").text
    assert 'hephaestos_server_up{server="palworld"}' not in body
    assert 'hephaestos_server_state_stale{server="palworld"} 1' in body
