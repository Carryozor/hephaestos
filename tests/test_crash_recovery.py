"""Auto-redemarrage sur crash (app/crash_recovery.py) : toggle par serveur
(registry.auto_restart_on_crash), declenche par le poll agent (GET /api/agent/orders),
disjoncteur anti-boucle (3 tentatives / 1h) + alerte webhook a l'abandon."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import bcrypt
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SERVERS = {"palworld": {"display_name": "Palworld", "server_appid": 2394010}}
STEAM_JSON = {"data": {"2394010": {"depots": {"branches": {"public": {"buildid": "24088465"}}}}}}


def make_app_crash(tmp_path, alert_webhook=None, captured=None):
    def handler(request):
        url = str(request.url)
        if captured is not None and "hooks.example" in url:
            captured.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=STEAM_JSON)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(agent_token="agent-t", data_dir=tmp_path, servers=SERVERS,
                        alert_webhook=alert_webhook)
    app = create_app(settings, http_client=client)
    h = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    asyncio.run(app.state.store.create_user("boss", h, role="admin"))
    return app


def make_client(tmp_path, **kw):
    app = make_app_crash(tmp_path, **kw)
    c = TestClient(app)
    r = c.post("/api/login", json={"username": "boss", "password": "testpass123"})
    assert r.status_code == 200
    return c


def _pending(c):
    return [o["type"] for o in
            c.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"}).json()["orders"]]


_report_counter = 0


def _write_state(tmp_path, process_up, last_seen=None):
    """Simule UN rapport d'etat agent : last_seen frais ET distinct a chaque appel
    (le backend ne compte qu'un rapport distinct, cf. last_seen_counted)."""
    global _report_counter
    _report_counter += 1
    if last_seen is None:
        last_seen = (datetime.now(UTC) + timedelta(microseconds=_report_counter)).isoformat()
    data = json.loads((tmp_path / "state.json").read_text())
    data["servers"] = {"palworld": {"process_up": process_up, "players": None,
                                    "last_seen": last_seen}}
    (tmp_path / "state.json").write_text(json.dumps(data))


def _crash_recovery(tmp_path):
    data = json.loads((tmp_path / "state.json").read_text())
    return data.get("crash_recovery", {}).get(
        "palworld", {"attempts": [], "breaker_tripped_at": None, "up_streak": 0})


def _seed_recovery(tmp_path, attempts, breaker_tripped_at=None, up_streak=0):
    data = json.loads((tmp_path / "state.json").read_text())
    data.setdefault("crash_recovery", {})["palworld"] = {
        "attempts": attempts, "breaker_tripped_at": breaker_tripped_at, "up_streak": up_streak}
    (tmp_path / "state.json").write_text(json.dumps(data))


def test_auto_restart_when_process_down_and_toggle_on(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _write_state(tmp_path, process_up=False)
    assert _pending(c) == ["start"]
    assert len(_crash_recovery(tmp_path)["attempts"]) == 1


def test_no_auto_restart_when_toggle_off(tmp_path):
    c = make_client(tmp_path)
    _write_state(tmp_path, process_up=False)
    assert _pending(c) == []
    assert _crash_recovery(tmp_path)["attempts"] == []


def test_no_auto_restart_when_process_up(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _write_state(tmp_path, process_up=True)
    assert _pending(c) == []


def test_no_auto_restart_when_process_state_unknown(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _write_state(tmp_path, process_up=None)
    assert _pending(c) == []


def test_no_auto_restart_when_order_already_pending(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _write_state(tmp_path, process_up=False)
    c.post("/api/servers/palworld/stop")
    assert "start" not in _pending(c)


def test_no_reset_after_single_up_poll_only(tmp_path):
    # Regression : un seul poll process_up=True ne doit PAS effacer l'historique --
    # un crash-loop qui remonte brievement entre deux crashs desarmerait sinon le
    # disjoncteur avant meme le prochain crash (cf. NoBuildingLimits100, 25/07/2026).
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _seed_recovery(tmp_path, attempts=[datetime.now(UTC).isoformat()])
    _write_state(tmp_path, process_up=True)
    _pending(c)
    info = _crash_recovery(tmp_path)
    assert info["attempts"] != [] or info["up_streak"] == 1


def test_history_reset_only_after_confirm_polls_consecutive_up(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _seed_recovery(tmp_path, attempts=[datetime.now(UTC).isoformat()])
    _write_state(tmp_path, process_up=True)
    _pending(c)  # 1er rapport up : pas encore confirme
    _write_state(tmp_path, process_up=True)  # 2e rapport DISTINCT (nouveau last_seen)
    _pending(c)  # 2e rapport up consecutif : confirme -> reset
    info = _crash_recovery(tmp_path)
    assert info["attempts"] == [] and info["breaker_tripped_at"] is None and info["up_streak"] == 0


def test_crash_loop_with_brief_uptime_between_crashes_still_trips_breaker(tmp_path):
    # Reproduit le scenario cible du disjoncteur : le process reboote (l'ordre start
    # auto est resolu "done" par l'agent, comme en vrai), est vu up UNE fois, puis
    # re-crashe -- repete 3x. Le disjoncteur doit quand meme se declencher (l'ancien
    # code effacait attempts a chaque poll up et ne trippait jamais).
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    for _ in range(3):
        _write_state(tmp_path, process_up=False)
        orders = c.get("/api/agent/orders", headers={"Authorization": "Bearer agent-t"}).json()["orders"]
        assert [o["type"] for o in orders] == ["start"]
        c.post(f"/api/agent/orders/{orders[0]['id']}", headers={"Authorization": "Bearer agent-t"},
               json={"status": "done"})
        _write_state(tmp_path, process_up=True)
        _pending(c)
    _write_state(tmp_path, process_up=False)
    assert "start" not in _pending(c)
    assert _crash_recovery(tmp_path)["breaker_tripped_at"] is not None


def test_manual_stop_is_not_undone_by_auto_restart(tmp_path):
    # Regression : un arret volontaire (POST .../stop), une fois confirme "done" par
    # l'agent, ne doit jamais etre traite comme un crash.
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    r = c.post("/api/servers/palworld/stop")
    order_id = r.json()["id"]
    c.post(f"/api/agent/orders/{order_id}", headers={"Authorization": "Bearer agent-t"},
           json={"status": "done"})
    _write_state(tmp_path, process_up=False)
    assert "start" not in _pending(c)


def test_auto_restart_resumes_after_manual_start_following_a_stop(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    r = c.post("/api/servers/palworld/stop")
    c.post(f"/api/agent/orders/{r.json()['id']}", headers={"Authorization": "Bearer agent-t"},
           json={"status": "done"})
    r = c.post("/api/servers/palworld/start")
    c.post(f"/api/agent/orders/{r.json()['id']}", headers={"Authorization": "Bearer agent-t"},
           json={"status": "done"})
    _write_state(tmp_path, process_up=False)
    assert _pending(c) == ["start"]  # nouveau crash apres le restart manuel : auto-reboot actif de nouveau


def test_no_auto_restart_for_disabled_server(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry",
          json={"auto_restart_on_crash": True, "status": "disabled"})
    _write_state(tmp_path, process_up=False)
    assert _pending(c) == []


def test_breaker_trips_after_3_attempts_in_1h_and_alerts(tmp_path):
    captured = []
    c = make_client(tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", captured=captured)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _seed_recovery(tmp_path, attempts=[datetime.now(UTC).isoformat() for _ in range(3)])
    _write_state(tmp_path, process_up=False)
    assert "start" not in _pending(c)
    info = _crash_recovery(tmp_path)
    assert info["breaker_tripped_at"] is not None
    assert len(captured) == 1
    assert "palworld" in captured[0]["content"]


def test_breaker_stays_silent_on_subsequent_polls_once_tripped(tmp_path):
    captured = []
    c = make_client(tmp_path, alert_webhook="https://hooks.example/ts-webhook/hephaestos", captured=captured)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _seed_recovery(tmp_path, attempts=[], breaker_tripped_at=datetime.now(UTC).isoformat())
    _write_state(tmp_path, process_up=False)
    assert "start" not in _pending(c)
    assert "start" not in _pending(c)
    assert captured == []  # pas de spam : deja alerte pour cet episode


def test_old_attempts_outside_window_are_pruned(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _seed_recovery(tmp_path, attempts=[old, old, old])
    _write_state(tmp_path, process_up=False)
    assert _pending(c) == ["start"]  # les 3 tentatives perimees ne comptent plus


def test_no_crash_when_alert_webhook_not_configured(tmp_path):
    c = make_client(tmp_path)  # pas d'alert_webhook
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    _seed_recovery(tmp_path, attempts=[datetime.now(UTC).isoformat() for _ in range(3)])
    _write_state(tmp_path, process_up=False)
    assert _pending(c) == []  # pas d'exception, juste pas d'alerte envoyee


AGENT_AUTH = {"Authorization": "Bearer agent-t"}


def _setup_two_attempts(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    now = datetime.now(UTC)
    _seed_recovery(tmp_path, [(now - timedelta(minutes=20)).isoformat(),
                              (now - timedelta(minutes=10)).isoformat()])
    return c


def test_temoin_process_down_adds_third_attempt(tmp_path):
    c = _setup_two_attempts(tmp_path)
    _write_state(tmp_path, process_up=False)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    assert len(_crash_recovery(tmp_path)["attempts"]) == 3


def test_one_up_report_polled_twice_keeps_history(tmp_path):
    # Regression B2.1 : le long-poll fait ~4 GET /orders par rapport d'etat ; sans
    # rapport nouveau entre deux GET, le streak ne doit pas avancer (sinon UN seul
    # rapport confirmait la stabilite et desarmait le disjoncteur).
    c = _setup_two_attempts(tmp_path)
    _write_state(tmp_path, process_up=True)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    info = _crash_recovery(tmp_path)
    assert len(info["attempts"]) == 2
    assert info["up_streak"] == 1


def test_two_distinct_up_reports_clear_history(tmp_path):
    c = _setup_two_attempts(tmp_path)
    _write_state(tmp_path, process_up=True)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    _write_state(tmp_path, process_up=True)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    info = _crash_recovery(tmp_path)
    assert info["attempts"] == [] and info["up_streak"] == 0


def test_down_report_resets_streak_to_zero(tmp_path):
    c = _setup_two_attempts(tmp_path)
    _write_state(tmp_path, process_up=True)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    assert _crash_recovery(tmp_path)["up_streak"] == 1
    _write_state(tmp_path, process_up=False)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    assert _crash_recovery(tmp_path)["up_streak"] == 0


def test_legacy_crash_recovery_without_last_seen_counted_is_handled(tmp_path):
    # state.json existant : cle last_seen_counted absente => None, pas d'erreur
    c = _setup_two_attempts(tmp_path)
    assert "last_seen_counted" not in _crash_recovery(tmp_path)
    _write_state(tmp_path, process_up=True)
    c.get("/api/agent/orders", headers=AGENT_AUTH)
    assert _crash_recovery(tmp_path)["up_streak"] == 1


def test_stale_down_state_does_not_trigger_restart(tmp_path):
    # B2.3 : process_up=False lu dans un etat perime (agent muet) n'est pas fiable
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    _write_state(tmp_path, process_up=False, last_seen=old)
    assert _pending(c) == []
    assert _crash_recovery(tmp_path)["attempts"] == []


def test_down_state_without_last_seen_does_not_trigger_restart(tmp_path):
    c = make_client(tmp_path)
    c.put("/api/servers/palworld/registry", json={"auto_restart_on_crash": True, "status": "active"})
    data = json.loads((tmp_path / "state.json").read_text())
    data["servers"] = {"palworld": {"process_up": False, "players": None}}
    (tmp_path / "state.json").write_text(json.dumps(data))
    assert _pending(c) == []
