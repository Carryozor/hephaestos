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

def test_app_js_renders_without_reference_errors():
    """Smoke node : rend une carte serveur complete dans un DOM stub -- attrape les
    ReferenceError de rendu invisibles pour node --check (regression anyPending 15/07)."""
    import pathlib
    import subprocess
    script = pathlib.Path(__file__).parent / "js" / "render-smoke.js"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "SMOKE OK" in result.stdout
