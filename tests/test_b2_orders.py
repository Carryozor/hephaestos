"""B2.4 : _create_order refuse un ordre d'execution sur un serveur non actif
(installing / awaiting_setup / disabled) ; un status absent (entree heritee) = actif.
B2.5 : validation de save_dir / start_task / process (registry PUT + setup)."""
import asyncio
import json

import pytest

from tests.test_deploy import make_awaiting
from tests.test_sanity import make_logged_in_client


def _set_status(c, status, name="palworld"):
    asyncio.run(c.app.state.store.registry.update_entry(name, {"status": status}))


def _drop_status(tmp_path, name="palworld"):
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    del data["servers_registry"][name]["status"]
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("status", ["installing", "awaiting_setup", "disabled"])
@pytest.mark.parametrize("action", ["update", "restart", "start", "stop"])
def test_execution_order_refused_409_on_non_active_server(tmp_path, status, action):
    c = make_logged_in_client(tmp_path)
    _set_status(c, status)
    r = c.post(f"/api/servers/palworld/{action}")
    assert r.status_code == 409
    assert status in r.json()["detail"]
    assert asyncio.run(c.app.state.store.pending_orders()) == []


def test_execution_order_created_201_on_active_server(tmp_path):
    c = make_logged_in_client(tmp_path)
    _set_status(c, "active")
    assert c.post("/api/servers/palworld/restart").status_code == 201


def test_execution_order_created_201_when_status_absent_legacy_entry(tmp_path):
    c = make_logged_in_client(tmp_path)
    _drop_status(tmp_path)
    assert "status" not in c.app.state.store._load()["servers_registry"]["palworld"]
    assert c.post("/api/servers/palworld/restart").status_code == 201


def test_backup_refused_409_on_installing_server(tmp_path):
    c = make_logged_in_client(tmp_path)
    _set_status(c, "installing")
    assert c.post("/api/servers/palworld/saves/backup").status_code == 409


def test_setup_server_order_not_blocked_on_awaiting_setup(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c)
    r = c.post("/api/deploy/servers/vrising/setup", json={"exe_path": "VRisingServer.exe"})
    assert r.status_code == 201 and r.json()["type"] == "setup_server"


def test_deploy_install_order_not_blocked_on_installing_server(tmp_path):
    c = make_logged_in_client(tmp_path)
    from tests.test_deploy import see_agent
    see_agent(c)
    body = {"name": "vrising", "display_name": "V Rising", "server_appid": 1829350}
    assert c.post("/api/deploy/servers", json=body).status_code == 201  # statut installing


# --- B2.5 validations -----------------------------------------------------------

BAD_SAVE_DIRS = ["../etc", "a/../b", "..", "a\\..\\b", "C:\\saves", "C:/saves", "c:saves",
                 "\\\\srv\\share", "//srv/share", "/abs/path", "\\abs", "sa\x00ve", "sa\nve",
                 "sa\rve"]
GOOD_SAVE_DIRS = ["Pal/Saved/SaveGames", "Pal\\Saved\\SaveGames", "saves", "R2Play/Saved/SaveProfiles",
                  "a b/c-d_e.f", "..hidden/x", "file..name"]


@pytest.mark.parametrize("value", BAD_SAVE_DIRS)
def test_registry_put_rejects_bad_save_dir(tmp_path, value):
    c = make_logged_in_client(tmp_path)
    assert c.put("/api/servers/palworld/registry", json={"save_dir": value}).status_code == 422


@pytest.mark.parametrize("value", GOOD_SAVE_DIRS)
def test_registry_put_accepts_real_save_dir(tmp_path, value):
    c = make_logged_in_client(tmp_path)
    r = c.put("/api/servers/palworld/registry", json={"save_dir": value})
    assert r.status_code == 200 and r.json()["save_dir"] == value


# T2.2 : liste BLANCHE alignee sur Test-HephSafeName (agent) -- [\w .\\-]+ , 1..100.
# "a\\b" quitte BAD_TOKENS : le backslash est desormais accepte (tache dans un dossier).
BAD_TOKENS = ["a*b", "a?b", 'a"b', "a'b", "a;b", "a&b", "a|b", "a<b", "a>b", "a`b",
              "a$b", "a\nb", "a\rb", "a(b)", "%PATH%", "x /S", "a+b", "a,b", "a:b", "a@b",
              "a/b", "", "a" * 101]
GOOD_TOKENS = ["PalServer-Win64-Shipping-Cmd", "WindroseServer-Win64-Shipping", "valheim_server",
               "VRisingServer", "PalServer", "My Server 2.0", "vrising", "\\Hephaestos\\palworld-start",
               "Mon Serveur 2", "Serveur \u00c9t\u00e9", "a" * 100]


@pytest.mark.parametrize("field", ["start_task", "process"])
@pytest.mark.parametrize("value", BAD_TOKENS)
def test_registry_put_rejects_dangerous_chars_in_task_and_process(tmp_path, field, value):
    c = make_logged_in_client(tmp_path)
    assert c.put("/api/servers/palworld/registry", json={field: value}).status_code == 422


@pytest.mark.parametrize("field", ["start_task", "process"])
@pytest.mark.parametrize("value", GOOD_TOKENS)
def test_registry_put_accepts_real_task_and_process_names(tmp_path, field, value):
    c = make_logged_in_client(tmp_path)
    r = c.put("/api/servers/palworld/registry", json={field: value})
    assert r.status_code == 200 and r.json()[field] == value


def test_setup_rejects_derived_process_outside_whitelist_422(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c, candidates=["Bin/My(Server).exe"])
    before = c.get("/api/servers/vrising/registry").json()
    r = c.post("/api/deploy/servers/vrising/setup", json={"exe_path": "Bin/My(Server).exe"})
    assert r.status_code == 422
    after = c.get("/api/servers/vrising/registry").json()
    assert after == before and after["status"] == "awaiting_setup"
    assert asyncio.run(c.app.state.store.pending_orders()) == []


def test_setup_accepts_derived_process_within_whitelist(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c, candidates=["Bin/PalServer-Win64-Shipping-Cmd.exe"])
    r = c.post("/api/deploy/servers/vrising/setup",
               json={"exe_path": "Bin/PalServer-Win64-Shipping-Cmd.exe"})
    assert r.status_code == 201
    assert c.get("/api/servers/vrising/registry").json()["process"] == "PalServer-Win64-Shipping-Cmd"


def test_setup_rejects_bad_save_dir_422(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c)
    r = c.post("/api/deploy/servers/vrising/setup",
               json={"exe_path": "VRisingServer.exe", "save_dir": "../../Windows"})
    assert r.status_code == 422


def test_setup_accepts_relative_save_dir(tmp_path):
    c = make_logged_in_client(tmp_path)
    make_awaiting(c)
    r = c.post("/api/deploy/servers/vrising/setup",
               json={"exe_path": "VRisingServer.exe", "save_dir": "save-data/Saves"})
    assert r.status_code == 201
    assert c.get("/api/servers/vrising/registry").json()["save_dir"] == "save-data/Saves"


def test_registry_put_null_save_dir_still_accepted(tmp_path):
    c = make_logged_in_client(tmp_path)
    assert c.put("/api/servers/palworld/registry", json={"save_dir": None}).status_code == 200



# --- T2.4 : save_dir -- segments vides / '.' / finissant par espace ou point ----------

BAD_SAVE_DIRS_STRICT = ["./", ".\\", " ", "   ", "Saves/./", "./Saves", "a//b", "a\\\\b", "Saves/",
                        "Saves\\", "a /b", "a./b", "a/b.", "a/b ", ".", "/", "\\", "a/ /b"]
GOOD_SAVE_DIRS_STRICT = ["Pal/Saved/SaveGames", "Saved/SaveGames", "v1..2", "a b/c", ".hidden/x"]


@pytest.mark.parametrize("value", BAD_SAVE_DIRS_STRICT)
def test_registry_put_rejects_degenerate_save_dir(tmp_path, value):
    c = make_logged_in_client(tmp_path)
    assert c.put("/api/servers/palworld/registry", json={"save_dir": value}).status_code == 422


@pytest.mark.parametrize("value", GOOD_SAVE_DIRS_STRICT)
def test_registry_put_still_accepts_valid_save_dir(tmp_path, value):
    c = make_logged_in_client(tmp_path)
    r = c.put("/api/servers/palworld/registry", json={"save_dir": value})
    assert r.status_code == 200 and r.json()["save_dir"] == value
