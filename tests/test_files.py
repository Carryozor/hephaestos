import asyncio

from tests.test_sanity import make_logged_in_client

AGT = {"Authorization": "Bearer agent-t"}


def _activate(c, name="palworld"):
    # make_settings() seede palworld en "disabled" (aucun etat agent jamais rapporte
    # -- cf. ServersRepository.seed_if_empty : "active" seulement si le nom est deja
    # dans data["servers"], ce qui n'est jamais le cas sur un state.json neuf). Les
    # routes de ce lot gatent explicitement sur status=="active" : l'activer est un
    # prealable explicite, pas une hypothese sur le fixture partage.
    asyncio.run(c.app.state.store.registry.update_entry(name, {"status": "active"}))


def test_list_files_creates_order(tmp_path):
    c = make_logged_in_client(tmp_path)
    _activate(c)
    r = c.post("/api/servers/palworld/files/list", json={"root": "install"})
    assert r.status_code == 201
    order = r.json()
    assert order["type"] == "list_files" and order["server"] == "palworld" and order["root"] == "install"


def test_list_files_save_root_requires_save_dir(tmp_path):
    c = make_logged_in_client(tmp_path)
    _activate(c)
    r = c.post("/api/servers/palworld/files/list", json={"root": "save"})
    assert r.status_code == 400


def test_list_files_rejects_inactive_server(tmp_path):
    # palworld est "disabled" par defaut dans ce fixture (aucun etat agent jamais
    # rapporte) -- exactement le cas qu'on veut tester ici, aucune activation.
    c = make_logged_in_client(tmp_path)
    r = c.post("/api/servers/palworld/files/list", json={"root": "install"})
    assert r.status_code == 409


def test_read_file_rejects_traversal_and_bad_extension(tmp_path):
    c = make_logged_in_client(tmp_path)
    _activate(c)
    assert c.post("/api/servers/palworld/files/read",
                  json={"root": "install", "path": "../../evil.ini"}).status_code == 400
    assert c.post("/api/servers/palworld/files/read",
                  json={"root": "install", "path": "C:\\evil.ini"}).status_code == 400
    assert c.post("/api/servers/palworld/files/read",
                  json={"root": "install", "path": "server.exe"}).status_code == 400  # extension hors whitelist
    r = c.post("/api/servers/palworld/files/read",
               json={"root": "install", "path": "Config/Settings.ini"})
    assert r.status_code == 201 and r.json()["type"] == "read_file"


def test_write_file_creates_order_with_payload(tmp_path):
    c = make_logged_in_client(tmp_path)
    _activate(c)
    r = c.post("/api/servers/palworld/files/write",
               json={"root": "install", "path": "Config/Settings.ini",
                     "content_b64": "aGVsbG8=", "expected_sha256": "a" * 64})
    assert r.status_code == 201
    order = r.json()
    assert (order["type"] == "write_file" and order["path"] == "Config/Settings.ini"
            and order["content_b64"] == "aGVsbG8=" and order["expected_sha256"] == "a" * 64)


def test_write_file_rejects_bad_sha256_shape(tmp_path):
    c = make_logged_in_client(tmp_path)
    r = c.post("/api/servers/palworld/files/write",
               json={"root": "install", "path": "Config/Settings.ini",
                     "content_b64": "aGVsbG8=", "expected_sha256": "trop-court"})
    assert r.status_code == 422


def test_files_routes_admin_only(tmp_path):
    # Revue finale (18/07) : un fichier de config .ini legitime (PalWorldSettings.ini)
    # contient AdminPassword en clair -- le mot de passe RCON masque partout ailleurs
    # (_masked_registry_entry, /registry en require_admin_role). Un "user" assigne
    # a SON serveur ne doit PAS pouvoir le lire via ce lot, meme sur son propre serveur :
    # ce serait un contournement du masquage RCON par un chemin detourne.
    c = make_logged_in_client(tmp_path)
    _activate(c)
    asyncio.run(c.app.state.store.set_user_access("tester", "user", ["palworld"]))
    assert c.post("/api/servers/palworld/files/list", json={"root": "install"}).status_code == 403
    assert c.post("/api/servers/palworld/files/read",
                  json={"root": "install", "path": "a.ini"}).status_code == 403
    assert c.post("/api/servers/palworld/files/write",
                  json={"root": "install", "path": "a.ini",
                        "content_b64": "aGVsbG8=", "expected_sha256": "a" * 64}).status_code == 403


def test_files_routes_allowed_for_admin(tmp_path):
    c = make_logged_in_client(tmp_path)
    _activate(c)
    assert c.post("/api/servers/palworld/files/list", json={"root": "install"}).status_code == 201
