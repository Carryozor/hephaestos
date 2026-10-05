"""B2.6 : un rapport d'etat demesure est TRONQUE, jamais rejete en 422 (un 422 ferait
passer l'agent pour mort : last_seen plus jamais mis a jour)."""
import json

from fastapi.testclient import TestClient

from app.routes_agent import (
    MAX_BACKUPS,
    MAX_LIST_ITEMS,
    MAX_PLAYERS_LISTED,
    MAX_TEXT_CHARS,
)
from tests.test_sanity import make_app

AGT = {"Authorization": "Bearer agent-t"}


def _post(tmp_path, server_state, **top):
    c = TestClient(make_app(tmp_path))
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": server_state}, **top})
    return c, r


def _persisted(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


def test_normal_report_unchanged(tmp_path):
    players = [{"id": "1", "name": "Alice", "steamid": "765"}]
    backups = [{"file": "a.zip", "size_mb": 1.5, "created": "2026-10-05T00:00:00+00:00"}]
    _, r = _post(tmp_path, {"buildid": "123", "process_up": True, "players": 1,
                            "players_list": players, "installed_mod_ids": ["1", "2"],
                            "save_backups": backups, "process_started_at": "2026-10-05T00:00:00+00:00",
                            "rcon_info": "127.0.0.1:25575"})
    assert r.status_code == 200
    state = _persisted(tmp_path)
    assert state["servers"]["palworld"]["save_backups"] == backups
    assert state["servers"]["palworld"]["rcon_info"] == "127.0.0.1:25575"
    assert state["player_sessions"]["palworld"]["1"]["name"] == "Alice"
    assert state["mods_state"]["palworld"]["installed_mod_ids"] == ["1", "2"]


def test_huge_players_list_is_accepted_and_truncated(tmp_path):
    players = [{"id": str(i), "name": f"p{i}"} for i in range(10_000)]
    _, r = _post(tmp_path, {"process_up": True, "players": 10, "players_list": players})
    assert r.status_code == 200
    assert len(_persisted(tmp_path)["player_sessions"]["palworld"]) == MAX_PLAYERS_LISTED


def test_huge_mod_ids_and_backups_are_truncated(tmp_path):
    backups = [{"file": f"{i}.zip"} for i in range(5_000)]
    _, r = _post(tmp_path, {"process_up": True, "installed_mod_ids": [str(i) for i in range(20_000)],
                            "save_backups": backups})
    assert r.status_code == 200
    state = _persisted(tmp_path)
    assert len(state["servers"]["palworld"]["save_backups"]) == MAX_BACKUPS
    assert len(state["mods_state"]["palworld"]["installed_mod_ids"]) == MAX_LIST_ITEMS


def test_giant_strings_are_truncated(tmp_path):
    huge = "x" * 1_000_000
    _, r = _post(tmp_path, {"process_up": True, "rcon_info": huge, "process_started_at": huge,
                            "players_list": [{"id": huge, "name": huge, "steamid": huge}],
                            "save_backups": [{"file": huge, "created": huge}]})
    assert r.status_code == 200
    state = _persisted(tmp_path)
    srv = state["servers"]["palworld"]
    assert len(srv["rcon_info"]) == MAX_TEXT_CHARS
    assert len(srv["process_started_at"]) <= MAX_TEXT_CHARS
    assert len(srv["save_backups"][0]["file"]) <= MAX_TEXT_CHARS
    (session_id, session), = state["player_sessions"]["palworld"].items()
    assert len(session_id) <= MAX_TEXT_CHARS and len(session["name"]) <= MAX_TEXT_CHARS


def test_oversized_top_level_fields_truncated_not_rejected(tmp_path):
    _, r = _post(tmp_path, {"process_up": True}, agent_version="v" * 500, config_hash="h" * 500,
                 config_servers=[{"name": f"s{i}"} for i in range(500)],
                 discovered_games=[{"appid": i} for i in range(500)])
    assert r.status_code == 200
    meta = _persisted(tmp_path)["agent_meta"]
    assert len(meta["agent_version"]) == 20 and len(meta["config_hash"]) == 64
    assert len(meta["discovered_games"]) <= 50


def test_empty_players_list_still_closes_sessions_via_post(tmp_path):
    c, _ = _post(tmp_path, {"process_up": True, "players_list": [{"id": "1", "name": "A"}]})
    r = c.post("/api/agent/state", headers=AGT,
               json={"servers": {"palworld": {"process_up": True, "players_list": []}}})
    assert r.status_code == 200
    assert _persisted(tmp_path)["player_sessions"]["palworld"] == {}
