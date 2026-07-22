import pytest

from deploy.entrypoint import load_settings


def _base_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HEPHAESTOS_AGENT_TOKEN", "t")
    monkeypatch.setenv("HEPHAESTOS_DATA_DIR", str(tmp_path))


def test_load_settings_raises_clear_error_on_malformed_servers_json(tmp_path, monkeypatch):
    servers_file = tmp_path / "servers.json"
    servers_file.write_text("{not valid json")
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HEPHAESTOS_SERVERS_FILE", str(servers_file))

    with pytest.raises(SystemExit) as exc_info:
        load_settings()
    assert "servers.json" in str(exc_info.value) or str(servers_file) in str(exc_info.value)


def test_load_settings_ok_on_valid_servers_json(tmp_path, monkeypatch):
    servers_file = tmp_path / "servers.json"
    servers_file.write_text('{"palworld": {"display_name": "Palworld", "server_appid": 2394010}}')
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HEPHAESTOS_SERVERS_FILE", str(servers_file))

    settings = load_settings()
    assert settings.servers["palworld"]["server_appid"] == 2394010


def test_load_settings_exits_without_agent_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HEPHAESTOS_AGENT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        load_settings()
    assert "HEPHAESTOS_AGENT_TOKEN" in str(exc_info.value)
