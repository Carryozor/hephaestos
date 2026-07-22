from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_sanity import make_app, make_settings


def test_index_served(tmp_path):
    r = TestClient(create_app(make_settings(tmp_path))).get("/")
    assert r.status_code == 200 and "Hephaestos" in r.text

def test_login_page_has_form(tmp_path):
    r = TestClient(make_app(tmp_path)).get("/login")
    assert r.status_code == 200 and "loginForm" in r.text

def test_static_assets_served(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/static/assets/palworld-icon.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"

def test_app_js_served_with_ui_hooks(tmp_path):
    """Le JS applicatif vit dans /static/app.js (extrait de index.html)."""
    r = TestClient(create_app(make_settings(tmp_path))).get("/static/app.js")
    assert r.status_code == 200
    for hook in ("togglePlayers", "renderDetailMods", "renderWorkshopBrowser",
                 "cancelOrder", "auto_update_blocked"):
        assert hook in r.text, hook

def test_index_references_app_js_and_has_no_inline_app_script(tmp_path):
    r = TestClient(create_app(make_settings(tmp_path))).get("/")
    assert r.status_code == 200
    assert '/static/app.js' in r.text
    assert "togglePlayers" not in r.text

def test_version_endpoint(tmp_path):
    """La version applicative est exposee via /api/version (source unique = app.__version__)."""
    from app import __version__
    r = TestClient(create_app(make_settings(tmp_path))).get("/api/version")
    assert r.status_code == 200
    assert r.json() == {"version": __version__}
    assert __version__ == "1.0.0"

def test_index_shows_version(tmp_path):
    """La version est affichee en bas a gauche de l'app (element #appVersion peuple par app.js)."""
    r = TestClient(create_app(make_settings(tmp_path))).get("/")
    assert 'id="appVersion"' in r.text
    js = TestClient(create_app(make_settings(tmp_path))).get("/static/app.js").text
    assert "/api/version" in js
    assert "appVersion" in js

def test_index_has_annunciator_tiles(tmp_path):
    """Bandeau d'annonciateurs (pupitre braise) : 4 tuiles d'etat global au-dessus de la recherche."""
    r = TestClient(create_app(make_settings(tmp_path))).get("/")
    assert r.status_code == 200
    for id_ in ("annAgentLamp", "annAgentValue", "annOnlineLamp", "annOnlineValue",
                "annUpdatesLamp", "annUpdatesValue", "annModsLamp", "annModsValue"):
        assert f'id="{id_}"' in r.text, id_

def test_app_js_has_render_annunciators(tmp_path):
    r = TestClient(create_app(make_settings(tmp_path))).get("/static/app.js")
    assert r.status_code == 200
    assert "renderAnnunciators" in r.text

def test_app_js_renders_without_reference_errors():
    """Smoke node : rend une carte serveur complete dans un DOM stub -- attrape les
    ReferenceError de rendu invisibles pour node --check (regression anyPending 15/07)."""
    import pathlib
    import subprocess
    script = pathlib.Path(__file__).parent / "js" / "render-smoke.js"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "SMOKE OK" in result.stdout
