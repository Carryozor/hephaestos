import asyncio
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ORDER_TYPES = {"update", "restart", "start", "stop", "install_mod", "remove_mod",
               "backup", "restore_save", "install_game", "scan_exe", "setup_server",
               "list_files", "read_file", "write_file"}
ORDER_STATUSES = {"running", "done", "failed"}
ORDER_TERMINAL_RETENTION_DAYS = 7
ORDER_STALE_HOURS = 24

class Store:
    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._dump({"servers": {}, "orders": [], "users": {}, "sessions": {}})
        self.mods = ModsRepository(self)
        self.registry = ServersRepository(self)

    def _load(self) -> dict:
        return json.loads(self._path.read_text())

    def _dump(self, data: dict) -> None:
        # Ecriture atomique DURABLE : write+fsync du tmp, replace, puis fsync du
        # repertoire. Sans les fsync, un crash/coupure apres le replace peut laisser
        # un state.json tronque ou perdre le rename (donnees en cache page non
        # flushees) -- l'etat SoT unique du service ne doit pas etre corruptible ainsi.
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._path)
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _groom_orders(self, data: dict) -> bool:
        """Nettoie la liste des ordres en place. Retourne True si quelque chose a change.

        - Purge les ordres terminaux (done/failed) plus vieux que
          ORDER_TERMINAL_RETENTION_DAYS -- sans ca, data["orders"] est append-only
          et state.json croit sans borne.
        - Expire en "failed" les ordres pending/running plus vieux que
          ORDER_STALE_HOURS : un agent reinstalle/mort ne rend jamais compte, et
          l'ordre fantome bloquerait indefiniment toute nouvelle demande du meme
          type (409 dans _create_order).
        """
        now = datetime.now(UTC)
        changed = False

        retention_cutoff = now - timedelta(days=ORDER_TERMINAL_RETENTION_DAYS)
        kept = [o for o in data["orders"]
                if o["status"] not in ("done", "failed")
                or datetime.fromisoformat(o["created"]) >= retention_cutoff]
        if len(kept) != len(data["orders"]):
            data["orders"] = kept
            changed = True

        stale_cutoff = now - timedelta(hours=ORDER_STALE_HOURS)
        for o in data["orders"]:
            if o["status"] in ("pending", "running") and datetime.fromisoformat(o["created"]) < stale_cutoff:
                o["status"] = "failed"
                o["detail"] = f"expire automatiquement (sans reponse de l'agent depuis plus de {ORDER_STALE_HOURS}h)"
                # trace pour notification : une mort par expiration est aussi grave
                # qu'un failed rapporte, elle ne doit pas rester invisible
                data.setdefault("orders_expired_unnotified", []).append(
                    {"server": o["server"], "type": o["type"],
                     "author": o.get("author"), "created": o["created"]})
                changed = True

        return changed

    async def add_order(self, server: str, type_: str, payload: dict | None = None,
                        author: str | None = None) -> dict:
        if type_ not in ORDER_TYPES:
            raise ValueError(f"type d'ordre invalide: {type_}")
        async with self._lock:
            data = self._load()
            self._groom_orders(data)
            # payload AVANT les champs internes : un payload contenant status/id/...
            # ne doit jamais pouvoir les ecraser (les payloads actuels sont construits
            # cote backend, mais l'invariant ne doit pas dependre de cette discipline)
            order = {**(payload or {}),
                     "id": uuid4().hex, "server": server, "type": type_, "status": "pending",
                     "created": datetime.now(UTC).isoformat(), "detail": None,
                     "author": author}
            data["orders"].append(order)
            self._dump(data)
            return order

    async def order_history(self, server: str, limit: int = 15) -> list[dict]:
        """Derniers ordres TERMINES (done/failed) du serveur, plus recent d'abord.
        Profondeur bornee par la retention (purge a ORDER_TERMINAL_RETENTION_DAYS)."""
        async with self._lock:
            data = self._load()
            done = [o for o in data["orders"]
                    if o["server"] == server and o["status"] in ("done", "failed")]
            done.sort(key=lambda o: o["created"], reverse=True)
            return done[:limit]

    async def pending_orders(self) -> list[dict]:
        async with self._lock:
            data = self._load()
            if self._groom_orders(data):
                self._dump(data)
            return [o for o in data["orders"] if o["status"] in ("pending", "running")]

    async def pop_expired_unnotified(self) -> list[dict]:
        """Consomme (retourne puis efface) les expirations d'ordres pas encore
        notifiees -- chaque expiration ne produit qu'une seule alerte."""
        async with self._lock:
            data = self._load()
            expired = data.get("orders_expired_unnotified", [])
            if expired:
                data["orders_expired_unnotified"] = []
                self._dump(data)
            return expired

    async def cancel_order(self, order_id: str) -> dict | None:
        """Annule un ordre encore en attente (jamais un running : l'agent est
        peut-etre en train de l'executer, l'annuler cote backend ne l'arreterait pas).

        Retourne None si inconnu, une copie {**ordre, cancel_refused=True} sans rien
        persister si l'ordre n'est pas pending, sinon l'ordre passe en failed/annule.
        """
        async with self._lock:
            data = self._load()
            for o in data["orders"]:
                if o["id"] == order_id:
                    if o["status"] != "pending":
                        return {**o, "cancel_refused": True}
                    o["status"] = "failed"
                    o["detail"] = "annule par l'admin"
                    self._dump(data)
                    return o
            return None

    async def set_order_status(self, order_id: str, status: str, detail: str | None = None) -> dict | None:
        """Change le statut d'un ordre.

        "done" et "failed" sont des etats terminaux : toute transition DEPUIS
        l'un d'eux est refusee — l'ordre est retourne inchange (copie) avec le
        marqueur `terminal_refused=True`, rien n'est persiste. Les transitions
        VERS un terminal (ex. running->done) restent legales.
        Retourne None si l'ordre est inconnu.
        """
        if status not in ORDER_STATUSES:
            raise ValueError(f"statut invalide: {status}")
        async with self._lock:
            data = self._load()
            for o in data["orders"]:
                if o["id"] == order_id:
                    if o["status"] in ("done", "failed"):
                        return {**o, "terminal_refused": True}
                    o["status"], o["detail"] = status, detail
                    # install_mod confirme "done" = le mod sur disque vient d'etre
                    # remplace par la derniere version Workshop : rafraichir
                    # installed_at, sinon le badge "maj dispo" (steam_updated_at >
                    # installed_at) resterait affiche a vie apres une mise a jour
                    # (update_mods_state ne pose installed_at qu'a la premiere
                    # apparition de l'id). Entree existante uniquement : ne pas
                    # recreer de metadonnees fantomes pour un mod purge entre-temps.
                    if status == "done" and o["type"] == "install_mod" and o.get("workshop_id"):
                        entry = data.get("mods_metadata", {}).get(o["server"], {}).get(o["workshop_id"])
                        if entry is not None:
                            entry["installed_at"] = datetime.now(UTC).isoformat()
                    # start/restart/update aboutis = le process courant a ete lance par
                    # l'auteur de cet ordre (update = stop+maj+start cote agent).
                    # Persiste hors de servers[name] : l'etat est REMPLACE a chaque
                    # rapport agent. Hors retention 7 j : un serveur up des semaines
                    # garde l'info.
                    if status == "done" and o["type"] in ("start", "restart", "update"):
                        data.setdefault("servers_started_by", {})[o["server"]] = {
                            "author": o.get("author"),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    self._dump(data)
                    return o
            return None

    async def set_server_state(self, name: str, state: dict) -> None:
        async with self._lock:
            data = self._load()
            data["servers"][name] = {**state, "last_seen": datetime.now(UTC).isoformat()}
            self._dump(data)

    async def set_agent_meta(self, meta: dict) -> None:
        """Metadonnees du dernier rapport agent (version, hash de config applique,
        jeux decouverts). Merge : un rapport sans un champ ne l'efface pas."""
        async with self._lock:
            data = self._load()
            current = data.get("agent_meta", {})
            current.update(meta)
            current["reported_at"] = datetime.now(UTC).isoformat()
            data["agent_meta"] = current
            self._dump(data)

    async def get_agent_meta(self) -> dict:
        async with self._lock:
            return self._load().get("agent_meta", {})

    async def get_game_auto_marker(self, name: str) -> str | None:
        """Horodatage ISO du dernier auto-enqueue de MAJ jeu pour ce serveur (cooldown
        anti-boucle, meme role que ModsRepository.get_mods_auto_marker)."""
        async with self._lock:
            return self._load().get("game_auto", {}).get(name)

    async def set_game_auto_marker(self, name: str) -> None:
        async with self._lock:
            data = self._load()
            data.setdefault("game_auto", {})[name] = datetime.now(UTC).isoformat()
            self._dump(data)

    async def snapshot(self) -> dict:
        async with self._lock:
            return self._load()

    @staticmethod
    def _with_role_defaults(username: str, record: dict) -> dict:
        """Comptes crees avant la feature roles : pas de champ role/servers en base.
        Ils sont traites comme admin (migration douce, comportement inchange)."""
        return {"username": username, "role": record.get("role", "admin"),
                "servers": record.get("servers", []), **record}

    async def create_user(self, username: str, password_hash: str,
                          role: str = "admin", servers: list[str] | None = None) -> None:
        if role not in ("admin", "user"):
            raise ValueError(f"role invalide: {role}")
        async with self._lock:
            data = self._load()
            users = data.setdefault("users", {})
            if username in users:
                raise ValueError(f"utilisateur deja existant: {username}")
            users[username] = {"password_hash": password_hash, "role": role,
                               "servers": servers or [],
                               "created": datetime.now(UTC).isoformat()}
            self._dump(data)

    async def list_users(self) -> list[dict]:
        """Comptes sans le hash de mot de passe (destine a l'API admin)."""
        async with self._lock:
            data = self._load()
            return [
                {k: v for k, v in self._with_role_defaults(u, rec).items() if k != "password_hash"}
                for u, rec in data.get("users", {}).items()
            ]

    async def delete_user(self, username: str) -> bool:
        """Supprime le compte ET toutes ses sessions (revocation immediate).
        Retourne False si le compte n'existe pas."""
        async with self._lock:
            data = self._load()
            if username not in data.get("users", {}):
                return False
            del data["users"][username]
            sessions = data.get("sessions", {})
            for token in [t for t, s in sessions.items() if s.get("username") == username]:
                del sessions[token]
            self._dump(data)
            return True

    async def set_user_password(self, username: str, password_hash: str) -> bool:
        """Change le hash ET revoque les sessions existantes du compte : un reset de
        mot de passe repond souvent a un mdp compromis, les sessions ouvertes avec
        l'ancien secret ne doivent pas survivre."""
        async with self._lock:
            data = self._load()
            user = data.get("users", {}).get(username)
            if user is None:
                return False
            user["password_hash"] = password_hash
            sessions = data.get("sessions", {})
            for token in [t for t, s in sessions.items() if s.get("username") == username]:
                del sessions[token]
            self._dump(data)
            return True

    async def set_user_access(self, username: str, role: str, servers: list[str]) -> bool:
        if role not in ("admin", "user"):
            raise ValueError(f"role invalide: {role}")
        async with self._lock:
            data = self._load()
            user = data.get("users", {}).get(username)
            if user is None:
                return False
            user["role"], user["servers"] = role, servers
            self._dump(data)
            return True

    async def count_admins(self) -> int:
        async with self._lock:
            data = self._load()
            return sum(1 for u, rec in data.get("users", {}).items()
                       if self._with_role_defaults(u, rec)["role"] == "admin")

    async def get_user(self, username: str) -> dict | None:
        async with self._lock:
            record = self._load().get("users", {}).get(username)
            return None if record is None else self._with_role_defaults(username, record)

    async def create_session(self, username: str, ttl_days: int = 30) -> str:
        token = secrets.token_hex(32)
        async with self._lock:
            data = self._load()
            expires = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
            data.setdefault("sessions", {})[token] = {"username": username, "expires": expires}
            self._dump(data)
        return token

    async def get_session(self, token: str) -> dict | None:
        async with self._lock:
            data = self._load()
            session = data.get("sessions", {}).get(token)
            if session is None:
                return None
            if datetime.fromisoformat(session["expires"]) < datetime.now(UTC):
                return None
            return session

    async def renew_session(self, token: str, ttl_days: int = 30) -> None:
        """Prolonge la session glissante. Throttle anti-usure : si la session a deja ete
        renouvelee il y a moins d'un jour (expiration a moins d'un jour du maximum),
        on ne reecrit pas state.json -- sinon chaque requete admin authentifiee
        provoquerait une reecriture complete du fichier."""
        async with self._lock:
            data = self._load()
            session = data.get("sessions", {}).get(token)
            if session is None:
                return
            new_expires = datetime.now(UTC) + timedelta(days=ttl_days)
            current = datetime.fromisoformat(session["expires"])
            if new_expires - current < timedelta(days=1):
                return
            session["expires"] = new_expires.isoformat()
            self._dump(data)

    async def delete_session(self, token: str) -> None:
        async with self._lock:
            data = self._load()
            data.get("sessions", {}).pop(token, None)
            self._dump(data)

    async def purge_expired_sessions(self) -> int:
        """Supprime les sessions expirees. Retourne le nombre de sessions supprimees."""
        async with self._lock:
            data = self._load()
            now = datetime.now(UTC)
            sessions = data.get("sessions", {})
            expired = [tok for tok, s in sessions.items() if datetime.fromisoformat(s["expires"]) < now]
            for tok in expired:
                del sessions[tok]
            if expired:
                self._dump(data)
            return len(expired)

    async def purge_orphan_sessions(self) -> int:
        """Supprime les sessions dont le compte n'existe plus. purge_expired_sessions
        ne couvre que l'expiration temporelle ; une session peut rester valide dans le
        temps mais pointer un `username` absent -- traces de tests E2E laissees en prod
        (`e2e_*`), ou compte supprime par un chemin qui n'a pas revoque ses sessions.
        Retourne le nombre de sessions supprimees."""
        async with self._lock:
            data = self._load()
            users = data.get("users", {})
            sessions = data.get("sessions", {})
            orphans = [tok for tok, s in sessions.items() if s.get("username") not in users]
            for tok in orphans:
                del sessions[tok]
            if orphans:
                self._dump(data)
            return len(orphans)

    async def update_player_sessions(self, name: str, players: list[dict]) -> None:
        async with self._lock:
            data = self._load()
            sessions = data.setdefault("player_sessions", {}).setdefault(name, {})
            now_dt = datetime.now(UTC)
            now = now_dt.isoformat()
            seen_ids = {p["id"] for p in players}
            connection_log = data.setdefault("connection_log", {}).setdefault(name, [])
            playtime_totals = data.setdefault("playtime_totals", {}).setdefault(name, {})

            for pid in list(sessions.keys()):
                if pid not in seen_ids:
                    old = sessions[pid]
                    player_key = old.get("steamid") or old["name"]
                    for entry in reversed(connection_log):
                        if entry["disconnected_at"] is None and entry.get("_session_pid") == pid:
                            entry["disconnected_at"] = now
                            connected_dt = datetime.fromisoformat(entry["connected_at"])
                            duration = int((now_dt - connected_dt).total_seconds())
                            total = playtime_totals.setdefault(player_key, {"name": old["name"], "total_seconds": 0})
                            total["name"] = old["name"]
                            total["total_seconds"] += duration
                            break
                    del sessions[pid]

            for p in players:
                if p["id"] not in sessions:
                    player_key = p.get("steamid") or p["name"]
                    sessions[p["id"]] = {"name": p["name"], "steamid": p.get("steamid"), "first_seen": now}
                    connection_log.append({
                        "player_key": player_key,
                        "name": p["name"],
                        "steamid": p.get("steamid"),
                        "connected_at": now,
                        "disconnected_at": None,
                        "_session_pid": p["id"],
                    })
                else:
                    sessions[p["id"]]["name"] = p["name"]
                    sessions[p["id"]]["steamid"] = p.get("steamid")

            cutoff = now_dt - timedelta(days=7)
            data["connection_log"][name] = [
                e for e in connection_log
                if e["disconnected_at"] is None or datetime.fromisoformat(e["disconnected_at"]) >= cutoff
            ]

            self._dump(data)

    async def get_player_sessions(self, name: str) -> dict:
        async with self._lock:
            data = self._load()
            return data.get("player_sessions", {}).get(name, {})

    async def get_connection_log(self, name: str) -> list[dict]:
        async with self._lock:
            data = self._load()
            log = data.get("connection_log", {}).get(name, [])
            return sorted(
                (
                    {
                        "name": e["name"],
                        "steamid": e["steamid"],
                        "connected_at": e["connected_at"],
                        "disconnected_at": e["disconnected_at"],
                    }
                    for e in log
                ),
                key=lambda e: e["connected_at"],
                reverse=True,
            )

    async def get_playtime_totals(self, name: str) -> dict[str, dict]:
        async with self._lock:
            data = self._load()
            return dict(data.get("playtime_totals", {}).get(name, {}))


class ModsRepository:
    """Persistance du domaine mods (Workshop Steam) : mods_metadata, mods_state,
    mods_auto dans state.json. Extrait du Store (god-object) le 17/07/2026 -- la
    logique metier mods vit deja dans app/mods.py, seule la persistance restait ici.

    Partage le fichier, le verrou et le format JSON du Store (une seule ecriture
    atomique pour tout state.json) : delegue a Store._load/_dump/_lock plutot que
    de gerer son propre fichier, pour ne jamais introduire de write concurrente non
    serialisee sur state.json.
    """

    def __init__(self, store: "Store"):
        self._store = store

    async def set_mod_metadata(
        self, name: str, workshop_id: str, title: str, thumbnail_url: str,
        steam_updated_at: str | None = None,
    ) -> None:
        """Cree/met a jour les metadonnees d'un mod. Preserve `installed_at` (pose
        uniquement par update_mods_state) et, si `steam_updated_at` n'est pas fourni,
        la valeur deja stockee -- un backfill/placeholder sans date ne doit jamais
        effacer une date connue."""
        async with self._store._lock:
            data = self._store._load()
            mods = data.setdefault("mods_metadata", {}).setdefault(name, {})
            existing = mods.get(workshop_id, {})
            mods[workshop_id] = {
                "title": title,
                "thumbnail_url": thumbnail_url,
                "installed_at": existing.get("installed_at"),
                "steam_updated_at": (
                    steam_updated_at if steam_updated_at is not None
                    else existing.get("steam_updated_at")
                ),
            }
            self._store._dump(data)

    async def get_mods_metadata(self, name: str) -> dict:
        async with self._store._lock:
            data = self._store._load()
            return data.get("mods_metadata", {}).get(name, {})

    async def remove_mod_metadata_if_never_installed(self, name: str, workshop_id: str) -> None:
        """Purge immediatement les metadonnees d'un mod qui n'est PAS actuellement
        confirme installe sur disque (annulation d'un install jamais aboutit -- ex.
        steamcmd echoue, ou l'utilisateur annule avant que l'agent n'ait traite l'ordre).

        Si le mod EST actuellement installe, ne fait rien : la purge normale se fera
        via `update_mods_state` une fois le retrait confirme par l'agent (le disque
        fait foi, pas cette route) -- evite de perdre le titre pendant la fenetre ou
        l'ordre remove_mod est encore en attente de traitement reel.
        """
        async with self._store._lock:
            data = self._store._load()
            installed_ids = set(data.get("mods_state", {}).get(name, {}).get("installed_mod_ids", []))
            if workshop_id in installed_ids:
                return
            metadata = data.get("mods_metadata", {}).get(name, {})
            if workshop_id in metadata:
                del metadata[workshop_id]
                self._store._dump(data)

    async def update_mods_state(self, name: str, installed_mod_ids: list[str]) -> None:
        """Met a jour l'etat des mods installes sur disque pour un serveur.

        Ne purge de `mods_metadata` QUE les IDs qui etaient precedemment confirmes
        installes (presents dans l'ancien `installed_mod_ids`) et qui ont reellement
        disparu (retrait confirme) -- ne jamais purger un ID absent simplement parce
        qu'il n'est pas encore sur disque (ex. mod tout juste ajoute, ordre install_mod
        pas encore traite par l'agent) : ca supprimerait ses metadonnees avant meme
        qu'il ait eu une chance d'apparaitre, le rendant invisible dans le dashboard.
        """
        async with self._store._lock:
            data = self._store._load()
            mods_state = data.setdefault("mods_state", {}).setdefault(
                name, {"installed_mod_ids": [], "changed_at": None}
            )
            old_ids = set(mods_state.get("installed_mod_ids", []))
            new_ids = set(installed_mod_ids)
            if old_ids != new_ids:
                mods_state["changed_at"] = datetime.now(UTC).isoformat()
            mods_state["installed_mod_ids"] = installed_mod_ids

            metadata = data.setdefault("mods_metadata", {}).setdefault(name, {})
            removed_ids = old_ids - new_ids
            for wid in removed_ids:
                metadata.pop(wid, None)

            # installed_at : pose a la PREMIERE confirmation d'installation seulement
            # (new - old). Un rapport ou les IDs sont inchanges n'est pas une nouvelle
            # confirmation -- les mods installes avant cette feature restent sans date.
            now_iso = datetime.now(UTC).isoformat()
            for wid in new_ids - old_ids:
                mod_entry = metadata.setdefault(wid, {})
                if not mod_entry.get("installed_at"):
                    mod_entry["installed_at"] = now_iso

            self._store._dump(data)

    async def get_mods_auto_marker(self, name: str) -> str | None:
        """Horodatage ISO du dernier auto-enqueue de MAJ de mods pour ce serveur
        (cooldown anti-boucle : un install_mod qui echoue laisse update_available
        vrai, sans cooldown le poll agent re-tenterait steamcmd toutes les 2 min)."""
        async with self._store._lock:
            data = self._store._load()
            return data.get("mods_auto", {}).get(name)

    async def set_mods_auto_marker(self, name: str) -> None:
        async with self._store._lock:
            data = self._store._load()
            data.setdefault("mods_auto", {})[name] = datetime.now(UTC).isoformat()
            self._store._dump(data)

    async def get_mods_state(self, name: str) -> dict:
        async with self._store._lock:
            data = self._store._load()
            return data.get("mods_state", {}).get(name, {"installed_mod_ids": [], "changed_at": None})


class ServersRepository:
    """Registre des serveurs : source de verite unique de leur definition,
    editable par l'UI admin et poussee a l'agent (bloc config du GET /orders).
    Compose sur le Store : partage son lock et ses helpers _load/_dump
    (une seule ecriture concurrente possible sur state.json).
    Schema d'une entree : display_name, server_appid, status
    (active|disabled), champs agent (AGENT_FIELDS, None = inconnu) et
    extra (cles rapportees par l'agent hors schema, preservees telles
    quelles pour que la config poussee soit sans perte)."""

    AGENT_FIELDS = ("process", "start_task", "launch_args", "stop_adapter", "rcon",
                    "query_port", "save_dir", "stop_warn_seconds", "kuma_maj_push",
                    "workshop_appid", "windrose_plus")

    # TTL du contenu file_read : un fichier de config jeu lu (ex. PalWorldSettings.ini)
    # contient AdminPassword en clair. On l'affiche a l'admin le temps de l'edition mais
    # il ne doit pas dormir dans state.json jusqu'au backup nightly (04:15). 15 min
    # couvrent largement un cycle lecture->edition->ecriture cote UI.
    FILE_READ_TTL_MINUTES = 15

    # Statuts qu'une creation d'entree peut poser : "active" se gagne par la
    # confirmation agent (setup_server done), jamais a la creation.
    CREATABLE_STATUSES = ("installing", "awaiting_setup")

    def __init__(self, store: "Store"):
        self._store = store

    def seed_if_empty(self, seed: dict) -> None:
        """Seed one-shot depuis deploy/servers.json (sync : appele une fois dans
        create_app, avant de servir). Un serveur sans etat deja rapporte par
        l'agent est seede 'disabled' : il n'existe que sur le papier (cas Valheim,
        liste backend mais jamais installe sur la machine Windows)."""
        data = self._store._load()
        if data.get("servers_registry"):
            return
        registry = {}
        for name, cfg in seed.items():
            entry = {"display_name": cfg["display_name"],
                     "server_appid": cfg["server_appid"],
                     "status": "active" if name in data.get("servers", {}) else "disabled",
                     "extra": {}}
            if "workshop_appid" in cfg:
                entry["workshop_appid"] = cfg["workshop_appid"]
            registry[name] = entry
        data["servers_registry"] = registry
        self._store._dump(data)

    async def all(self) -> dict:
        async with self._store._lock:
            return self._store._load().get("servers_registry", {})

    async def get(self, name: str) -> dict | None:
        return (await self.all()).get(name)

    async def create_entry(self, name: str, display_name: str, server_appid: int,
                           status: str) -> dict:
        """Creation par le wizard de deploiement (installing) ou l'adoption d'un jeu
        decouvert (awaiting_setup). Unicite du nom ET de l'appid : deux entrees sur le
        meme appid partageraient le meme dossier d'install steamcmd."""
        if status not in self.CREATABLE_STATUSES:
            raise ValueError(f"statut de creation invalide: {status}")
        async with self._store._lock:
            data = self._store._load()
            registry = data.setdefault("servers_registry", {})
            if name in registry:
                raise ValueError(f"nom deja au registre: {name}")
            if any(e.get("server_appid") == server_appid for e in registry.values()):
                raise ValueError(f"appid deja au registre: {server_appid}")
            entry = {"display_name": display_name, "server_appid": server_appid,
                     "status": status, "extra": {}}
            registry[name] = entry
            self._store._dump(data)
            return entry

    async def purge_stale_file_reads(self, ttl_minutes: int = FILE_READ_TTL_MINUTES) -> int:
        """Efface le bloc file_read (config jeu en clair, secret-at-rest) des entrees
        du registre des qu'il depasse ttl_minutes. `read_at` est pose a la persistance
        (routes_agent). Une entree file_read SANS read_at (heritee d'avant cette feature)
        est purgee inconditionnellement -- on ne garde jamais un secret d'age inconnu.
        Retourne le nombre d'entrees purgees."""
        cutoff = datetime.now(UTC) - timedelta(minutes=ttl_minutes)
        async with self._store._lock:
            data = self._store._load()
            registry = data.get("servers_registry", {})
            purged = 0
            for entry in registry.values():
                file_read = entry.get("file_read")
                if file_read is None:
                    continue
                read_at = file_read.get("read_at")
                if read_at is None or datetime.fromisoformat(read_at) < cutoff:
                    del entry["file_read"]
                    purged += 1
            if purged:
                self._store._dump(data)
            return purged

    async def update_entry(self, name: str, fields: dict) -> dict | None:
        async with self._store._lock:
            data = self._store._load()
            entry = data.get("servers_registry", {}).get(name)
            if entry is None:
                return None
            entry.update(fields)
            self._store._dump(data)
            return entry

    async def adopt_agent_fields(self, name: str, agent_entry: dict) -> None:
        """Enrichissement depuis le snapshot de config rapporte par l'agent :
        ne remplit QUE les champs encore inconnus (l'admin via l'UI a toujours
        le dernier mot), range les cles hors schema dans extra, et active
        l'entree (l'agent la gere reellement)."""
        known = set(self.AGENT_FIELDS) | {"name", "appid"}
        async with self._store._lock:
            data = self._store._load()
            entry = data.get("servers_registry", {}).get(name)
            if entry is None:
                return
            changed = False
            for key in self.AGENT_FIELDS:
                if entry.get(key) is None and agent_entry.get(key) is not None:
                    entry[key] = agent_entry[key]
                    changed = True
            extra = entry.setdefault("extra", {})
            for key, value in agent_entry.items():
                if key not in known and key not in extra:
                    extra[key] = value
                    changed = True
            if entry.get("status") != "active":
                entry["status"] = "active"
                changed = True
            if changed:
                self._store._dump(data)

    async def agent_config(self) -> dict:
        """Bloc config pousse a l'agent : entrees actives, cles agent non nulles,
        extra re-fusionne. Hash sha256 du JSON canonique -- l'agent compare ce
        hash a celui de sa derniere application, il ne recalcule rien."""
        servers = []
        for name, entry in sorted((await self.all()).items()):
            if entry.get("status") != "active":
                continue
            item = {"name": name, "appid": entry["server_appid"]}
            for key in self.AGENT_FIELDS:
                if entry.get(key) is not None:
                    item[key] = entry[key]
            item.update(entry.get("extra", {}))
            servers.append(item)
        blob = json.dumps(servers, sort_keys=True, separators=(",", ":"))
        return {"servers": servers, "hash": hashlib.sha256(blob.encode()).hexdigest()}
