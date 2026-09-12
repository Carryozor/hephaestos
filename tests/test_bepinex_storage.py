from datetime import UTC, datetime, timedelta

import pytest

from app.storage import Store

VALHEIM_SEED = {
    "valheim": [
        {
            "slug": "denikson/BepInExPack_Valheim", "title": "BepInExPack Valheim",
            "source": "thunderstore", "owner": "denikson", "package": "BepInExPack_Valheim",
            "target": "root", "installed_version": "5.4.2350",
            "installed_paths": ["BepInEx/core", "winhttp.dll", "doorstop_config.ini"],
        },
        {
            "slug": "ValheimModding/Jotunn", "title": "Jötunn",
            "source": "thunderstore", "owner": "ValheimModding", "package": "Jotunn",
            "target": "plugins", "installed_version": "2.30.0",
            "installed_paths": ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"],
        },
        {
            "slug": "SpikeHimself/XPortal", "title": "XPortal",
            "source": "github_release", "owner": "SpikeHimself", "package": "XPortal",
            "asset_pattern": r"^XPortal-v.*\.zip$", "tag_prefix": "v",
            "target": "plugins", "installed_version": "1.2.25",
            "installed_paths": ["BepInEx/plugins/XPortal"],
        },
    ]
}


def make_store(tmp_path):
    return Store(tmp_path / "state.json")


async def test_seed_if_empty_creates_entries(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    entries = await store.bepinex.all("valheim")
    assert set(entries) == {
        "denikson/BepInExPack_Valheim", "ValheimModding/Jotunn", "SpikeHimself/XPortal",
    }
    jotunn = entries["ValheimModding/Jotunn"]
    assert jotunn["installed_version"] == "2.30.0"
    assert jotunn["plugin_version"] is None
    assert jotunn["latest_version"] is None
    assert jotunn["last_error"] is None
    assert jotunn["installed_paths"] == ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"]


async def test_seed_if_empty_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", version="2.31.0",
                                   download_url="https://x/y.zip")
    store.bepinex.seed_if_empty(VALHEIM_SEED)  # 2e appel : ne doit pas ecraser
    entries = await store.bepinex.all("valheim")
    assert entries["ValheimModding/Jotunn"]["latest_version"] == "2.31.0"


async def test_seed_if_empty_noop_on_empty_seed(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty({})
    assert await store.bepinex.all("valheim") == {}


async def test_seed_adds_new_server_without_touching_existing(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", version="2.31.0",
                                   download_url="https://x/y.zip")
    store.bepinex.seed_if_empty({**VALHEIM_SEED, "otherserver": [
        {"slug": "a/b", "title": "T", "source": "thunderstore", "owner": "a", "package": "b",
         "target": "plugins", "installed_version": "1.0.0", "installed_paths": ["BepInEx/plugins/b.dll"]},
    ]})
    assert (await store.bepinex.all("valheim"))["ValheimModding/Jotunn"]["latest_version"] == "2.31.0"
    assert "a/b" in await store.bepinex.all("otherserver")


async def test_all_empty_for_unknown_server(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    assert await store.bepinex.all("palworld") == {}


async def test_seed_rejects_traversal_in_installed_paths(tmp_path):
    store = make_store(tmp_path)
    bad_seed = {"valheim": [
        {"slug": "a/b", "title": "T", "source": "thunderstore", "owner": "a", "package": "b",
         "target": "plugins", "installed_version": "1.0.0",
         "installed_paths": ["BepInEx/plugins/../../evil.dll"]},
    ]}
    with pytest.raises(ValueError):
        store.bepinex.seed_if_empty(bad_seed)


async def test_seed_rejects_plugins_path_outside_plugins_dir(tmp_path):
    store = make_store(tmp_path)
    bad_seed = {"valheim": [
        {"slug": "a/b", "title": "T", "source": "thunderstore", "owner": "a", "package": "b",
         "target": "plugins", "installed_version": "1.0.0",
         "installed_paths": ["winhttp.dll"]},
    ]}
    with pytest.raises(ValueError):
        store.bepinex.seed_if_empty(bad_seed)


async def test_set_latest_updates_version_and_url_and_clears_error(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", error="boom")
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", version="2.31.0",
                                   download_url="https://x/y.zip")
    entry = (await store.bepinex.all("valheim"))["ValheimModding/Jotunn"]
    assert entry["latest_version"] == "2.31.0"
    assert entry["download_url"] == "https://x/y.zip"
    assert entry["last_error"] is None
    assert entry["latest_checked_at"] is not None


async def test_set_latest_preserves_installed_fields(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", version="2.31.0",
                                   download_url="https://x/y.zip")
    entry = (await store.bepinex.all("valheim"))["ValheimModding/Jotunn"]
    assert entry["installed_version"] == "2.30.0"
    assert entry["installed_paths"] == ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"]


async def test_set_latest_error_does_not_erase_known_latest_version(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", version="2.31.0",
                                   download_url="https://x/y.zip")
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", error="reseau down")
    entry = (await store.bepinex.all("valheim"))["ValheimModding/Jotunn"]
    assert entry["latest_version"] == "2.31.0"
    assert entry["download_url"] == "https://x/y.zip"
    assert entry["last_error"] == "reseau down"


async def test_set_latest_unknown_slug_is_noop(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "unknown/slug", version="1.0.0", download_url="https://x")
    assert "unknown/slug" not in await store.bepinex.all("valheim")


async def test_set_installed_updates_version_paths_and_clears_error(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_latest("valheim", "ValheimModding/Jotunn", error="boom")
    before = datetime.now(UTC) - timedelta(seconds=1)
    await store.bepinex.set_installed(
        "valheim", "ValheimModding/Jotunn", version="2.31.0",
        paths=["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"], plugin_version="2.31.0",
    )
    entry = (await store.bepinex.all("valheim"))["ValheimModding/Jotunn"]
    assert entry["installed_version"] == "2.31.0"
    assert entry["plugin_version"] == "2.31.0"
    assert entry["last_error"] is None
    assert datetime.fromisoformat(entry["installed_at"]) >= before


async def test_set_installed_unknown_slug_is_noop(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    await store.bepinex.set_installed("valheim", "unknown/slug", version="1.0.0",
                                      paths=["BepInEx/plugins/x.dll"])
    assert "unknown/slug" not in await store.bepinex.all("valheim")


async def test_set_installed_rejects_bad_paths(tmp_path):
    store = make_store(tmp_path)
    store.bepinex.seed_if_empty(VALHEIM_SEED)
    with pytest.raises(ValueError):
        await store.bepinex.set_installed(
            "valheim", "ValheimModding/Jotunn", version="2.31.0",
            paths=["/etc/passwd"],
        )
