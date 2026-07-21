import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.storage import Store


async def test_add_and_pending(tmp_path):
    s = Store(tmp_path / "state.json")
    o = await s.add_order("palworld", "update")
    assert o["status"] == "pending" and o["server"] == "palworld"
    assert [x["id"] for x in await s.pending_orders()] == [o["id"]]


async def test_invalid_type(tmp_path):
    with pytest.raises(ValueError):
        await Store(tmp_path / "s.json").add_order("palworld", "rm-rf")


async def test_order_lifecycle(tmp_path):
    s = Store(tmp_path / "s.json")
    o = await s.add_order("palworld", "restart")
    assert (await s.set_order_status(o["id"], "done", "ok"))["status"] == "done"
    assert await s.pending_orders() == []
    assert await s.set_order_status("inconnu", "done") is None


async def test_server_state_and_persistence(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    await s.set_server_state("valheim", {"buildid": "123", "process_up": True})
    snap = Store(p)  # relecture depuis disque = persistance
    data = await snap.snapshot()
    assert data["servers"]["valheim"]["buildid"] == "123"
    assert "last_seen" in data["servers"]["valheim"]


async def test_create_and_get_user(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.create_user("admin", "hash123")
    user = await s.get_user("admin")
    assert user["password_hash"] == "hash123"
    assert "created" in user
    assert await s.get_user("inconnu") is None


async def test_create_user_duplicate_rejected(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.create_user("admin", "hash123")
    with pytest.raises(ValueError):
        await s.create_user("admin", "autre-hash")


async def test_session_lifecycle(tmp_path):
    s = Store(tmp_path / "s.json")
    token = await s.create_session("admin", ttl_days=30)
    assert len(token) >= 32
    session = await s.get_session(token)
    assert session["username"] == "admin"
    await s.delete_session(token)
    assert await s.get_session(token) is None
    # delete d'un token inconnu ne leve pas
    await s.delete_session("token-inconnu")


async def test_session_expired_returns_none(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    token = await s.create_session("admin", ttl_days=30)
    # on retro-date manuellement l'expiration dans le fichier pour simuler l'expiration
    data = json.loads(p.read_text())
    data["sessions"][token]["expires"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    p.write_text(json.dumps(data))
    assert await s.get_session(token) is None


async def test_renew_session_extends_expiry_when_worthwhile(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    token = await s.create_session("admin", ttl_days=30)
    # session vieillie artificiellement : il ne reste que 2 jours
    data = json.loads(p.read_text())
    data["sessions"][token]["expires"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    p.write_text(json.dumps(data))
    await s.renew_session(token, ttl_days=30)
    after = datetime.fromisoformat((await s.get_session(token))["expires"])
    assert after > datetime.now(UTC) + timedelta(days=29)
    # renew d'un token inconnu ne leve pas
    await s.renew_session("token-inconnu")


async def test_renew_session_skips_write_when_fresh(tmp_path):
    """Throttle anti-usure : une session renouvelee il y a moins d'un jour ne doit pas
    provoquer une reecriture complete de state.json a chaque requete admin."""
    p = tmp_path / "s.json"
    s = Store(p)
    token = await s.create_session("admin", ttl_days=30)
    before = (await s.get_session(token))["expires"]
    mtime_before = p.stat().st_mtime_ns
    await s.renew_session(token, ttl_days=30)
    assert (await s.get_session(token))["expires"] == before
    assert p.stat().st_mtime_ns == mtime_before


async def test_purge_expired_sessions(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    token_valid = await s.create_session("alice", ttl_days=30)
    token_expired = await s.create_session("bob", ttl_days=30)
    # retro-date manuellement l'expiration du token bob
    data = json.loads(p.read_text())
    data["sessions"][token_expired]["expires"] = "2020-01-01T00:00:00+00:00"
    p.write_text(json.dumps(data))

    removed = await s.purge_expired_sessions()
    assert removed == 1
    assert await s.get_session(token_valid) is not None
    assert await s.get_session(token_expired) is None
    # verifie que la session expiree a bien disparu du fichier (pas juste ignoree en lecture)
    remaining = json.loads(p.read_text())["sessions"]
    assert token_expired not in remaining
    assert token_valid in remaining


async def test_purge_expired_sessions_no_op_when_none_expired(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.create_session("alice", ttl_days=30)
    removed = await s.purge_expired_sessions()
    assert removed == 0


async def test_legacy_state_file_without_users_sessions_keys(tmp_path):
    # simule un state.json de prod existant, cree avant cette fonctionnalite
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"servers": {}, "orders": []}))
    s = Store(p)
    # doit fonctionner sans erreur malgre l'absence des cles users/sessions
    await s.create_user("admin", "hash123")
    assert (await s.get_user("admin"))["password_hash"] == "hash123"


@pytest.mark.asyncio
async def test_update_player_sessions_adds_new_player(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.update_player_sessions("palworld", [{"id": "123", "name": "Alice", "steamid": "765611"}])

    sessions = await store.get_player_sessions("palworld")
    assert sessions["123"]["name"] == "Alice"
    assert sessions["123"]["steamid"] == "765611"
    assert "first_seen" in sessions["123"]


@pytest.mark.asyncio
async def test_update_player_sessions_removes_disconnected_player(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.update_player_sessions("palworld", [{"id": "123", "name": "Alice", "steamid": None}])
    await store.update_player_sessions("palworld", [])

    sessions = await store.get_player_sessions("palworld")
    assert sessions == {}


@pytest.mark.asyncio
async def test_update_player_sessions_keeps_first_seen_for_still_connected_player(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.update_player_sessions("palworld", [{"id": "123", "name": "Alice", "steamid": None}])
    sessions_first = await store.get_player_sessions("palworld")
    first_seen_1 = sessions_first["123"]["first_seen"]

    await store.update_player_sessions("palworld", [{"id": "123", "name": "Alice", "steamid": None}])
    sessions_second = await store.get_player_sessions("palworld")

    assert sessions_second["123"]["first_seen"] == first_seen_1


@pytest.mark.asyncio
async def test_get_player_sessions_empty_when_never_reported(tmp_path):
    store = Store(tmp_path / "state.json")
    assert await store.get_player_sessions("windrose") == {}


@pytest.mark.asyncio
async def test_add_order_with_payload_merges_extra_fields(tmp_path):
    store = Store(tmp_path / "state.json")
    order = await store.add_order("palworld", "install_mod", {"workshop_id": "123", "title": "Cool Mod"})

    assert order["type"] == "install_mod"
    assert order["workshop_id"] == "123"
    assert order["title"] == "Cool Mod"
    assert order["status"] == "pending"


@pytest.mark.asyncio
async def test_add_order_without_payload_unchanged(tmp_path):
    store = Store(tmp_path / "state.json")
    order = await store.add_order("palworld", "update")

    assert order["type"] == "update"
    assert "workshop_id" not in order


@pytest.mark.asyncio
async def test_add_order_rejects_unknown_type_still(tmp_path):
    store = Store(tmp_path / "state.json")
    with pytest.raises(ValueError):
        await store.add_order("palworld", "delete_everything")


@pytest.mark.asyncio
async def test_set_and_get_mod_metadata(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/thumb.jpg")

    meta = await store.mods.get_mods_metadata("palworld")
    assert meta["123"] == {
        "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
        "installed_at": None, "steam_updated_at": None,
    }


async def test_set_mod_metadata_stores_steam_updated_at(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/t.jpg",
                                 steam_updated_at="2026-07-14T10:00:00+00:00")
    meta = await store.mods.get_mods_metadata("palworld")
    assert meta["123"]["steam_updated_at"] == "2026-07-14T10:00:00+00:00"
    assert meta["123"]["installed_at"] is None


async def test_set_mod_metadata_preserves_dates_when_not_provided(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/t.jpg",
                                 steam_updated_at="2026-07-14T10:00:00+00:00")
    await store.mods.update_mods_state("palworld", ["123"])  # pose installed_at
    # re-set SANS date (cas backfill/placeholder) : rien ne doit etre efface
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod v2", "https://x/t2.jpg")
    meta = (await store.mods.get_mods_metadata("palworld"))["123"]
    assert meta["title"] == "Cool Mod v2"
    assert meta["steam_updated_at"] == "2026-07-14T10:00:00+00:00"
    assert meta["installed_at"] is not None


async def test_update_mods_state_sets_installed_at_on_first_confirmation(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/t.jpg")
    await store.mods.update_mods_state("palworld", ["123"])
    first = (await store.mods.get_mods_metadata("palworld"))["123"]["installed_at"]
    assert first is not None
    # rapports suivants : jamais ecrase
    await store.mods.update_mods_state("palworld", ["123"])
    assert (await store.mods.get_mods_metadata("palworld"))["123"]["installed_at"] == first


async def test_update_mods_state_preexisting_mods_get_no_installed_at(tmp_path):
    # mods deja confirmes AVANT le deploiement de cette feature : un rapport identique
    # ne doit pas leur inventer une date d'installation
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/t.jpg")
    await store.mods.update_mods_state("palworld", ["123"])
    # simule l'etat "ancien" : on retire installed_at a la main comme si la donnee datait d'avant
    state_path = tmp_path / "state.json"
    data = json.loads(state_path.read_text())
    data["mods_metadata"]["palworld"]["123"].pop("installed_at", None)
    state_path.write_text(json.dumps(data))
    await store.mods.update_mods_state("palworld", ["123"])  # IDs inchanges -> pas une nouvelle confirmation
    assert (await store.mods.get_mods_metadata("palworld"))["123"].get("installed_at") is None


async def test_update_mods_state_creates_partial_entry_for_unknown_confirmed_mod(tmp_path):
    # l'agent confirme un mod dont on n'a aucune metadonnee : installed_at doit etre
    # pose sur une entree partielle (sans title -> backfill Tache 3 la completera)
    store = Store(tmp_path / "state.json")
    await store.mods.update_mods_state("palworld", ["999"])
    entry = (await store.mods.get_mods_metadata("palworld"))["999"]
    assert entry["installed_at"] is not None
    assert "title" not in entry


@pytest.mark.asyncio
async def test_get_mods_metadata_empty_when_never_set(tmp_path):
    store = Store(tmp_path / "state.json")
    assert await store.mods.get_mods_metadata("palworld") == {}


@pytest.mark.asyncio
async def test_update_mods_state_sets_changed_at_on_first_report(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.update_mods_state("palworld", ["123"])

    state = await store.mods.get_mods_state("palworld")
    assert state["installed_mod_ids"] == ["123"]
    assert state["changed_at"] is not None


@pytest.mark.asyncio
async def test_update_mods_state_keeps_changed_at_when_ids_unchanged(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.update_mods_state("palworld", ["123"])
    first = await store.mods.get_mods_state("palworld")

    await store.mods.update_mods_state("palworld", ["123"])
    second = await store.mods.get_mods_state("palworld")

    assert second["changed_at"] == first["changed_at"]


@pytest.mark.asyncio
async def test_update_mods_state_bumps_changed_at_when_ids_differ(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.update_mods_state("palworld", ["123"])
    first = await store.mods.get_mods_state("palworld")

    await store.mods.update_mods_state("palworld", ["123", "456"])
    second = await store.mods.get_mods_state("palworld")

    assert second["changed_at"] != first["changed_at"]
    assert second["installed_mod_ids"] == ["123", "456"]


@pytest.mark.asyncio
async def test_update_mods_state_prunes_metadata_for_removed_mod(tmp_path):
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/thumb.jpg")
    await store.mods.update_mods_state("palworld", ["123"])

    await store.mods.update_mods_state("palworld", [])

    assert await store.mods.get_mods_metadata("palworld") == {}


@pytest.mark.asyncio
async def test_update_mods_state_does_not_prune_metadata_for_mod_not_yet_on_disk(tmp_path):
    # Regression : un mod tout juste ajoute (metadata posee par la route /mods AVANT
    # que l'agent ait eu le temps d'installer et de rapporter le nouvel etat) ne doit
    # PAS voir ses metadonnees purgees par le premier rapport d'etat qui ne le contient
    # pas encore -- seul un ID qui etait DEJA confirme installe puis disparait doit
    # etre purge (retrait reel), jamais un ID qui n'a simplement pas encore ete rapporte.
    store = Store(tmp_path / "state.json")
    await store.mods.set_mod_metadata("palworld", "123", "Cool Mod", "https://x/thumb.jpg")

    # Premier rapport d'etat APRES la creation de l'ordre install_mod, mais AVANT que
    # l'agent n'ait traite l'ordre -- le mod n'est pas encore sur le disque.
    await store.mods.update_mods_state("palworld", [])

    assert await store.mods.get_mods_metadata("palworld") == {
        "123": {
            "title": "Cool Mod", "thumbnail_url": "https://x/thumb.jpg",
            "installed_at": None, "steam_updated_at": None,
        }
    }

    # Cycle suivant : l'agent a installe le mod, il apparait desormais sur disque.
    await store.mods.update_mods_state("palworld", ["123"])

    meta = await store.mods.get_mods_metadata("palworld")
    assert meta["123"]["title"] == "Cool Mod"
    assert meta["123"]["thumbnail_url"] == "https://x/thumb.jpg"
    assert meta["123"]["installed_at"] is not None


@pytest.mark.asyncio
async def test_get_mods_state_defaults_when_never_reported(tmp_path):
    store = Store(tmp_path / "state.json")
    assert await store.mods.get_mods_state("palworld") == {"installed_mod_ids": [], "changed_at": None}


async def test_connection_log_opens_and_closes_entry(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.update_player_sessions("palworld", [{"id": "1", "name": "Alice", "steamid": "765"}])
    log = await s.get_connection_log("palworld")
    assert len(log) == 1
    assert log[0]["name"] == "Alice" and log[0]["steamid"] == "765"
    assert log[0]["connected_at"] and log[0]["disconnected_at"] is None

    await s.update_player_sessions("palworld", [])
    log = await s.get_connection_log("palworld")
    assert len(log) == 1
    assert log[0]["disconnected_at"] is not None


async def test_playtime_totals_accumulate_and_never_purge(tmp_path, monkeypatch):
    from datetime import datetime

    import app.storage as storage_mod

    times = iter([
        datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 10, 5, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 11, 2, 0, tzinfo=UTC),
    ])

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(storage_mod, "datetime", FakeDateTime)

    s = Store(tmp_path / "s.json")
    await s.update_player_sessions("palworld", [{"id": "1", "name": "Alice", "steamid": "765"}])
    await s.update_player_sessions("palworld", [])  # deconnexion apres 5 min
    await s.update_player_sessions("palworld", [{"id": "2", "name": "Alice", "steamid": "765"}])
    await s.update_player_sessions("palworld", [])  # deconnexion apres 2 min

    totals = await s.get_playtime_totals("palworld")
    assert totals["765"]["name"] == "Alice"
    assert totals["765"]["total_seconds"] == 5 * 60 + 2 * 60


async def test_connection_log_purges_entries_older_than_7_days(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    import app.storage as storage_mod

    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    times = iter([base, base + timedelta(minutes=1), base + timedelta(days=8)])

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(storage_mod, "datetime", FakeDateTime)

    s = Store(tmp_path / "s.json")
    await s.update_player_sessions("palworld", [{"id": "1", "name": "Alice", "steamid": "765"}])
    await s.update_player_sessions("palworld", [])  # entree fermee a base+1min
    await s.update_player_sessions("palworld", [])  # cycle a base+8j : purge

    log = await s.get_connection_log("palworld")
    assert log == []
    totals = await s.get_playtime_totals("palworld")
    assert totals["765"]["total_seconds"] == 60  # le total, lui, n'est jamais purge


async def test_player_key_falls_back_to_name_without_steamid(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.update_player_sessions("windrose", [{"id": "sess1", "name": "Bob", "steamid": None}])
    await s.update_player_sessions("windrose", [])
    totals = await s.get_playtime_totals("windrose")
    assert "Bob" in totals


async def test_reconnect_creates_distinct_connection_log_entries(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.update_player_sessions("palworld", [{"id": "1", "name": "Alice", "steamid": "765"}])
    await s.update_player_sessions("palworld", [])
    await s.update_player_sessions("palworld", [{"id": "2", "name": "Alice", "steamid": "765"}])
    log = await s.get_connection_log("palworld")
    assert len(log) == 2


async def test_two_players_sharing_same_player_key_dont_close_each_others_session(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.update_player_sessions("windrose", [
        {"id": "sessA", "name": "Bob", "steamid": None},
        {"id": "sessB", "name": "Bob", "steamid": None},
    ])
    # sessA se deconnecte, sessB reste connecte
    await s.update_player_sessions("windrose", [
        {"id": "sessB", "name": "Bob", "steamid": None},
    ])

    log = await s.get_connection_log("windrose")
    assert len(log) == 2
    open_entries = [e for e in log if e["disconnected_at"] is None]
    closed_entries = [e for e in log if e["disconnected_at"] is not None]
    assert len(open_entries) == 1  # sessB toujours ouverte
    assert len(closed_entries) == 1  # sessA fermee


# --- cycle de vie des ordres : purge, expiration, annulation ---

def _backdate_order(path, order_id, **fields):
    """Modifie un ordre directement dans le fichier d'etat (manipulation de test)."""
    data = json.loads(path.read_text())
    for o in data["orders"]:
        if o["id"] == order_id:
            o.update(fields)
    path.write_text(json.dumps(data))


async def test_terminal_orders_older_than_retention_are_purged(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    old = await s.add_order("palworld", "update")
    await s.set_order_status(old["id"], "done")
    _backdate_order(p, old["id"], created=(datetime.now(UTC) - timedelta(days=8)).isoformat())
    recent = await s.add_order("palworld", "restart")
    await s.set_order_status(recent["id"], "failed")
    # un nouvel add_order declenche le nettoyage
    await s.add_order("palworld", "stop")
    data = json.loads(p.read_text())
    ids = [o["id"] for o in data["orders"]]
    assert old["id"] not in ids
    assert recent["id"] in ids  # terminal mais recent : conserve


async def test_stale_pending_order_expires_to_failed(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    o = await s.add_order("palworld", "update")
    _backdate_order(p, o["id"], created=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    assert await s.pending_orders() == []  # expire, plus jamais servi a l'agent
    data = json.loads(p.read_text())
    (expired,) = [x for x in data["orders"] if x["id"] == o["id"]]
    assert expired["status"] == "failed"
    assert "expire" in expired["detail"]


async def test_fresh_pending_order_not_expired(tmp_path):
    s = Store(tmp_path / "s.json")
    o = await s.add_order("palworld", "update")
    assert [x["id"] for x in await s.pending_orders()] == [o["id"]]


async def test_cancel_pending_order(tmp_path):
    s = Store(tmp_path / "s.json")
    o = await s.add_order("palworld", "update")
    cancelled = await s.cancel_order(o["id"])
    assert cancelled["status"] == "failed"
    assert "annule" in cancelled["detail"]
    assert await s.pending_orders() == []


async def test_cancel_refused_when_running_or_terminal(tmp_path):
    s = Store(tmp_path / "s.json")
    o = await s.add_order("palworld", "update")
    await s.set_order_status(o["id"], "running")
    refused = await s.cancel_order(o["id"])
    assert refused["cancel_refused"] is True
    # l'ordre n'a pas bouge
    assert [x["id"] for x in await s.pending_orders()] == [o["id"]]
    assert await s.cancel_order("inconnu") is None


async def test_install_mod_done_refreshes_installed_at(tmp_path):
    # Une reinstallation (= mise a jour de mod) doit rafraichir installed_at a la
    # confirmation "done", sinon le badge "maj dispo" resterait affiche a vie
    # (update_mods_state ne pose installed_at qu'a la PREMIERE apparition de l'id).
    s = Store(tmp_path / "s.json")
    await s.mods.set_mod_metadata("palworld", "42", "Mod", "https://x/t.jpg",
                             steam_updated_at="2026-07-10T00:00:00+00:00")
    await s.mods.update_mods_state("palworld", ["42"])  # 1re confirmation -> installed_at pose
    old = (await s.mods.get_mods_metadata("palworld"))["42"]["installed_at"]

    o = await s.add_order("palworld", "install_mod", {"workshop_id": "42", "title": "Mod", "thumbnail_url": ""})
    await s.set_order_status(o["id"], "done", "ok")

    new = (await s.mods.get_mods_metadata("palworld"))["42"]["installed_at"]
    assert new is not None and new > old


async def test_install_mod_failed_does_not_touch_installed_at(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.mods.set_mod_metadata("palworld", "42", "Mod", "https://x/t.jpg")
    await s.mods.update_mods_state("palworld", ["42"])
    old = (await s.mods.get_mods_metadata("palworld"))["42"]["installed_at"]

    o = await s.add_order("palworld", "install_mod", {"workshop_id": "42", "title": "Mod", "thumbnail_url": ""})
    await s.set_order_status(o["id"], "failed", "steamcmd KO")

    assert (await s.mods.get_mods_metadata("palworld"))["42"]["installed_at"] == old


async def test_non_mod_order_done_does_not_create_metadata(tmp_path):
    # un ordre restart/update "done" ne doit pas creer d'entree mods_metadata fantome
    s = Store(tmp_path / "s.json")
    o = await s.add_order("palworld", "restart")
    await s.set_order_status(o["id"], "done", "ok")
    assert await s.mods.get_mods_metadata("palworld") == {}


# --- integrite des ordres : payload vs champs internes ---

async def test_add_order_payload_cannot_shadow_internal_fields(tmp_path):
    # un payload contenant des cles internes (status, id, server...) ne doit JAMAIS
    # les ecraser : l'ordre reste pending avec un id genere
    s = Store(tmp_path / "s.json")
    o = await s.add_order("palworld", "install_mod", {
        "workshop_id": "42", "status": "done", "id": "forge", "server": "autre",
        "created": "1970-01-01T00:00:00+00:00", "author": "pirate",
    })
    assert o["status"] == "pending"
    assert o["id"] != "forge"
    assert o["server"] == "palworld"
    assert o["author"] is None
    assert o["created"].startswith("20")
    assert o["workshop_id"] == "42"  # le payload legitime passe toujours


# --- expiration d'ordre : traçage pour notification ---

async def test_expired_order_recorded_for_notification(tmp_path):
    p = tmp_path / "s.json"
    s = Store(p)
    o = await s.add_order("palworld", "update", author="boss")
    _backdate_order(p, o["id"], created=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    await s.pending_orders()  # declenche le groom -> expiration
    expired = await s.pop_expired_unnotified()
    assert len(expired) == 1
    assert expired[0]["server"] == "palworld"
    assert expired[0]["type"] == "update"
    assert expired[0]["author"] == "boss"
    # pop = consomme : un second appel ne renvoie rien (pas de double alerte)
    assert await s.pop_expired_unnotified() == []


async def test_no_expiration_nothing_to_notify(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.add_order("palworld", "update")
    await s.pending_orders()
    assert await s.pop_expired_unnotified() == []


# ---------------------------------------------------------------- ServersRepository

SEED = {
    "palworld": {"display_name": "Palworld", "server_appid": 2394010, "workshop_appid": 1623730},
    "valheim": {"display_name": "Valheim", "server_appid": 896660},
}


def _registry_store(tmp_path):
    return Store(tmp_path / "state.json")


def test_registry_seed_status_depends_on_reported_state(tmp_path):
    store = _registry_store(tmp_path)
    # palworld a deja un etat rapporte -> active ; valheim jamais rapporte -> disabled
    asyncio.run(store.set_server_state("palworld", {"process_up": True}))
    store.registry.seed_if_empty(SEED)
    reg = asyncio.run(store.registry.all())
    assert reg["palworld"]["status"] == "active"
    assert reg["palworld"]["server_appid"] == 2394010
    assert reg["palworld"]["workshop_appid"] == 1623730
    assert reg["valheim"]["status"] == "disabled"
    assert "workshop_appid" not in reg["valheim"]


def test_registry_seed_is_one_shot(tmp_path):
    store = _registry_store(tmp_path)
    store.registry.seed_if_empty(SEED)
    asyncio.run(store.registry.update_entry("palworld", {"display_name": "Renomme"}))
    store.registry.seed_if_empty(SEED)  # re-seed = no-op
    reg = asyncio.run(store.registry.all())
    assert reg["palworld"]["display_name"] == "Renomme"


def test_registry_adopt_agent_fields_fills_only_missing_and_activates(tmp_path):
    store = _registry_store(tmp_path)
    store.registry.seed_if_empty(SEED)
    asyncio.run(store.registry.update_entry("valheim", {"process": "deja_la"}))
    asyncio.run(store.registry.adopt_agent_fields("valheim", {
        "name": "valheim", "appid": 896660, "process": "valheim_server",
        "start_task": "valheim", "launch_args": "-nogui",
        "cle_inconnue": {"x": 1},
    }))
    entry = asyncio.run(store.registry.get("valheim"))
    assert entry["process"] == "deja_la"          # jamais ecrase
    assert entry["start_task"] == "valheim"        # absent -> adopte
    assert entry["launch_args"] == "-nogui"
    assert entry["extra"] == {"cle_inconnue": {"x": 1}}  # cle hors schema preservee
    assert entry["status"] == "active"             # snapshot agent = serveur reellement gere


def test_registry_agent_config_actives_only_extra_merged_hash_stable(tmp_path):
    store = _registry_store(tmp_path)
    store.registry.seed_if_empty(SEED)
    asyncio.run(store.registry.update_entry("palworld", {
        "status": "active", "process": "PalServer-Win64-Shipping-Cmd",
        "start_task": "PalServer", "stop_adapter": "palworld-rcon",
        "rcon": {"host": "127.0.0.1", "port": 25575},
        "extra": {"windrose_plus": True},
    }))
    cfg = asyncio.run(store.registry.agent_config())
    names = [s["name"] for s in cfg["servers"]]
    assert names == ["palworld"]                   # valheim disabled exclu
    pal = cfg["servers"][0]
    assert pal["appid"] == 2394010                 # server_appid -> appid cote agent
    assert pal["rcon"] == {"host": "127.0.0.1", "port": 25575}
    assert pal["windrose_plus"] is True            # extra re-fusionne tel quel
    assert "status" not in pal and "display_name" not in pal
    cfg2 = asyncio.run(store.registry.agent_config())
    assert cfg["hash"] == cfg2["hash"] and len(cfg["hash"]) == 64
    asyncio.run(store.registry.update_entry("palworld", {"launch_args": "-x"}))
    assert asyncio.run(store.registry.agent_config())["hash"] != cfg["hash"]


def test_create_entry_installing(tmp_path):
    store = Store(tmp_path / "state.json")
    entry = asyncio.run(store.registry.create_entry("vrising", "V Rising", 1829350, "installing"))
    assert entry == {"display_name": "V Rising", "server_appid": 1829350,
                     "status": "installing", "extra": {}}
    assert asyncio.run(store.registry.get("vrising"))["status"] == "installing"


def test_create_entry_rejects_duplicates_and_bad_status(tmp_path):
    store = Store(tmp_path / "state.json")
    asyncio.run(store.registry.create_entry("vrising", "V Rising", 1829350, "awaiting_setup"))
    with pytest.raises(ValueError):  # nom deja pris
        asyncio.run(store.registry.create_entry("vrising", "Autre", 999, "installing"))
    with pytest.raises(ValueError):  # appid deja au registre
        asyncio.run(store.registry.create_entry("autre", "Autre", 1829350, "installing"))
    with pytest.raises(ValueError):  # statut de creation interdit (active se gagne, ne se decrete pas)
        asyncio.run(store.registry.create_entry("autre2", "Autre", 998, "active"))


def test_new_order_types_accepted(tmp_path):
    store = Store(tmp_path / "state.json")
    for t in ("install_game", "scan_exe", "setup_server"):
        order = asyncio.run(store.add_order("vrising", t, {"appid": 1829350}, author="admin"))
        assert order["type"] == t and order["appid"] == 1829350


# --- Durcissement 2026-07-21 : purge sessions orphelines ---

async def test_purge_orphan_sessions_removes_sessions_without_user(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.create_user("real", "h")
    tok_valid = await s.create_session("real")
    # session orpheline injectee directement (cas trace e2e_* laissee en prod)
    data = s._load()
    data["sessions"]["e2e_orphan"] = {"username": "disparu",
                                      "expires": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    s._dump(data)
    removed = await s.purge_orphan_sessions()
    assert removed == 1
    remaining = s._load()["sessions"]
    assert tok_valid in remaining and "e2e_orphan" not in remaining


async def test_purge_orphan_sessions_no_op_when_all_valid(tmp_path):
    s = Store(tmp_path / "s.json")
    await s.create_user("real", "h")
    await s.create_session("real")
    assert await s.purge_orphan_sessions() == 0


# --- Durcissement 2026-07-21 : purge file_read (secret-at-rest) ---

async def test_purge_stale_file_reads_removes_old_and_dateless(tmp_path):
    s = Store(tmp_path / "s.json")
    s.registry.seed_if_empty({"pal": {"display_name": "Pal", "server_appid": 1}})
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    await s.registry.update_entry("pal", {"status": "active", "file_read": {
        "root": "install", "path": "a.ini", "content_b64": "c2VjcmV0", "sha256": "x", "read_at": old}})
    purged = await s.registry.purge_stale_file_reads(ttl_minutes=15)
    assert purged == 1
    assert "file_read" not in (await s.registry.get("pal"))
    # une entree fraiche est conservee
    await s.registry.update_entry("pal", {"file_read": {
        "root": "install", "path": "a.ini", "content_b64": "c2VjcmV0", "sha256": "x", "read_at": fresh}})
    assert await s.registry.purge_stale_file_reads(ttl_minutes=15) == 0
    assert (await s.registry.get("pal"))["file_read"]["read_at"] == fresh


async def test_purge_stale_file_reads_dateless_entry_purged(tmp_path):
    s = Store(tmp_path / "s.json")
    s.registry.seed_if_empty({"pal": {"display_name": "Pal", "server_appid": 1}})
    await s.registry.update_entry("pal", {"file_read": {
        "root": "install", "path": "a.ini", "content_b64": "c2VjcmV0", "sha256": "x"}})
    assert await s.registry.purge_stale_file_reads(ttl_minutes=15) == 1
    assert "file_read" not in (await s.registry.get("pal"))


# --- Durcissement 2026-07-21 : fsync durabilite de _dump ---

async def test_dump_fsyncs_file_and_dir(tmp_path, monkeypatch):
    import os
    calls = {"n": 0}
    real_fsync = os.fsync
    def counting_fsync(fd):
        calls["n"] += 1
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", counting_fsync)
    s = Store(tmp_path / "s.json")
    calls["n"] = 0
    await s.set_server_state("pal", {"up": True})
    # au moins un fsync fichier + un fsync repertoire
    assert calls["n"] >= 2
    assert (await s.snapshot())["servers"]["pal"]["up"] is True
