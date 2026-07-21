import json

from app import known_servers
from app.known_servers import search_known


def _use_fixture(monkeypatch, tmp_path, data):
    path = tmp_path / "known.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("HEPHAESTOS_KNOWN_SERVERS_FILE", str(path))
    known_servers._cache = None  # cache module-level : reset obligatoire entre tests


def test_search_known_filters_by_substring_case_insensitive(monkeypatch, tmp_path):
    _use_fixture(monkeypatch, tmp_path, [
        {"appid": 2394010, "name": "Palworld Dedicated Server"},
        {"appid": 896660, "name": "Valheim Dedicated Server"},
    ])
    results = search_known("palworld")
    assert results == [{"appid": 2394010, "name": "Palworld Dedicated Server"}]
    assert search_known("PALWORLD") == results


def test_search_known_no_match_returns_empty_list(monkeypatch, tmp_path):
    _use_fixture(monkeypatch, tmp_path, [{"appid": 1, "name": "Autre Jeu Dedicated Server"}])
    assert search_known("ceciNexistePasDuTout") == []


def test_search_known_missing_file_returns_empty_list_not_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("HEPHAESTOS_KNOWN_SERVERS_FILE", str(tmp_path / "absent.json"))
    known_servers._cache = None
    assert search_known("palworld") == []


def test_search_known_malformed_top_level_returns_empty_list(monkeypatch, tmp_path):
    # JSON valide mais forme inattendue (dict au lieu de liste) : edition manuelle
    # fautive, ne doit jamais faire planter la recherche (trouve en revue finale).
    _use_fixture(monkeypatch, tmp_path, {"not": "a list"})
    assert search_known("palworld") == []


def test_search_known_malformed_entry_ignored_not_fatal(monkeypatch, tmp_path):
    path = tmp_path / "known.json"
    path.write_text(json.dumps([
        {"appid": 1, "name": "Bon Serveur Dedicated Server"},
        {"appid": 2},  # entree malformee (pas de "name") : ignoree, pas fatale
    ]))
    monkeypatch.setenv("HEPHAESTOS_KNOWN_SERVERS_FILE", str(path))
    known_servers._cache = None
    assert search_known("serveur") == [{"appid": 1, "name": "Bon Serveur Dedicated Server"}]
