"""Persistance des mods BepInEx (Valheim, pas de Steam Workshop pour ce jeu) :
racine `bepinex_mods` de state.json. Cf. docs/plans/2026-09-12-bepinex-mod-updates.md.

Meme principe que ModsRepository : partage le fichier/verrou/format JSON du
Store, pas de fichier ni de verrou separe.
"""
import re
from datetime import UTC, datetime


def validate_installed_paths(paths: list[str], target: str) -> None:
    """Rejette tout chemin de traversal/absolu, et pour target="plugins" exige
    qu'il soit sous BepInEx/plugins/ -- ces chemins sont plus tard passes a
    l'agent comme cibles de suppression (Remove-BepInExPaths), jamais faire
    confiance a une entree qui n'a pas ete validee ici."""
    for path in paths:
        normalized = path.replace("\\", "/")
        segments = normalized.split("/")
        if ".." in segments or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError(f"chemin invalide (traversal ou absolu): {path}")
        if target == "plugins" and not normalized.startswith("BepInEx/plugins/"):
            raise ValueError(f"chemin hors de BepInEx/plugins/ pour une cible plugins: {path}")


class BepInExRepository:
    def __init__(self, store):
        self._store = store

    def seed_if_empty(self, seed: dict[str, list[dict]]) -> None:
        """Seed one-shot PAR SERVEUR (pas global comme ServersRepository) : un
        serveur deja present dans bepinex_mods n'est jamais re-seede, mais un
        nouveau serveur ajoute au seed plus tard (nouveau jeu module) l'est bien
        -- appele de facon synchrone dans create_app, avant de servir."""
        data = self._store._load()
        bucket = data.setdefault("bepinex_mods", {})
        changed = False
        for server, mod_list in seed.items():
            if server in bucket:
                continue
            now = datetime.now(UTC).isoformat()
            entries = {}
            for m in mod_list:
                validate_installed_paths(m.get("installed_paths", []), m["target"])
                entries[m["slug"]] = {
                    "slug": m["slug"],
                    "title": m["title"],
                    "source": m["source"],
                    "owner": m["owner"],
                    "package": m["package"],
                    "asset_pattern": m.get("asset_pattern"),
                    "tag_prefix": m.get("tag_prefix"),
                    "target": m["target"],
                    "installed_version": m["installed_version"],
                    "plugin_version": None,
                    "installed_paths": list(m.get("installed_paths", [])),
                    "installed_at": now,
                    "latest_version": None,
                    "download_url": None,
                    "latest_checked_at": None,
                    "last_error": None,
                }
            bucket[server] = entries
            changed = True
        if changed:
            self._store._dump(data)

    async def all(self, server: str) -> dict:
        async with self._store._lock:
            return self._store._load().get("bepinex_mods", {}).get(server, {})

    async def set_latest(self, server: str, slug: str, *, version: str | None = None,
                         download_url: str | None = None, error: str | None = None) -> None:
        """Persiste le resultat d'une verification de version. `error` fourni :
        pose last_error SANS toucher latest_version/download_url (une panne
        reseau ne doit jamais effacer la derniere version connue). Sans erreur :
        met a jour version+url et efface last_error. latest_checked_at est
        toujours pose (meme en echec) : sert d'horodatage d'affichage, le TTL de
        rafraichissement lui-meme vit en cache memoire cote service."""
        async with self._store._lock:
            data = self._store._load()
            entry = data.get("bepinex_mods", {}).get(server, {}).get(slug)
            if entry is None:
                return
            entry["latest_checked_at"] = datetime.now(UTC).isoformat()
            if error is not None:
                entry["last_error"] = error
            else:
                entry["latest_version"] = version
                entry["download_url"] = download_url
                entry["last_error"] = None
            self._store._dump(data)

    async def set_installed(self, server: str, slug: str, *, version: str,
                            paths: list[str], plugin_version: str | None = None) -> None:
        """Confirme une installation reussie (rapport agent `bepinex_installed`).
        Slug inconnu = no-op (pas d'entree fantome, meme regle que
        ModsRepository.update_mods_state)."""
        async with self._store._lock:
            data = self._store._load()
            entry = data.get("bepinex_mods", {}).get(server, {}).get(slug)
            if entry is None:
                return
            validate_installed_paths(paths, entry["target"])
            entry["installed_version"] = version
            entry["installed_paths"] = list(paths)
            entry["installed_at"] = datetime.now(UTC).isoformat()
            entry["last_error"] = None
            if plugin_version is not None:
                entry["plugin_version"] = plugin_version
            self._store._dump(data)
