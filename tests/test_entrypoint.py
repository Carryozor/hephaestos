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


def test_load_settings_ok_without_bepinex_file(tmp_path, monkeypatch):
    """Contrairement a servers.json, l'absence de bepinex-mods.json n'est PAS
    fatale : Palworld/Windrose n'ont pas de mods BepInEx a suivre."""
    servers_file = tmp_path / "servers.json"
    servers_file.write_text('{"palworld": {"display_name": "Palworld", "server_appid": 2394010}}')
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HEPHAESTOS_SERVERS_FILE", str(servers_file))
    monkeypatch.setenv("HEPHAESTOS_BEPINEX_FILE", str(tmp_path / "absent-bepinex-mods.json"))

    settings = load_settings()
    assert settings.bepinex_mods == {}


def test_load_settings_loads_valid_bepinex_file(tmp_path, monkeypatch):
    servers_file = tmp_path / "servers.json"
    servers_file.write_text('{"valheim": {"display_name": "Valheim", "server_appid": 896660}}')
    bepinex_file = tmp_path / "bepinex-mods.json"
    bepinex_file.write_text('{"valheim": [{"slug": "a/b", "title": "T"}]}')
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HEPHAESTOS_SERVERS_FILE", str(servers_file))
    monkeypatch.setenv("HEPHAESTOS_BEPINEX_FILE", str(bepinex_file))

    settings = load_settings()
    assert settings.bepinex_mods["valheim"][0]["slug"] == "a/b"


def test_load_settings_raises_clear_error_on_malformed_bepinex_json(tmp_path, monkeypatch):
    servers_file = tmp_path / "servers.json"
    servers_file.write_text('{"palworld": {"display_name": "Palworld", "server_appid": 2394010}}')
    bepinex_file = tmp_path / "bepinex-mods.json"
    bepinex_file.write_text("{not valid json")
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HEPHAESTOS_SERVERS_FILE", str(servers_file))
    monkeypatch.setenv("HEPHAESTOS_BEPINEX_FILE", str(bepinex_file))

    with pytest.raises(SystemExit) as exc_info:
        load_settings()
    assert "bepinex-mods.json" in str(exc_info.value) or str(bepinex_file) in str(exc_info.value)
