"""Edition des fichiers de config des serveurs (Lot 3 v2) : list_files/read_file/
write_file, executes par l'agent sous deux racines autorisees (dossier d'install,
save_dir). Contrairement aux ordres de deploiement du Lot 2 (auto-porteurs), ces
ordres ne concernent que des serveurs deja actifs -- dispatch normal cote agent.

Admin uniquement (require_admin_role), PAS "users assignes" comme le reste de l'admin
panel pour leurs propres serveurs : revu et restreint en revue finale (18/07) apres
avoir constate qu'un fichier .ini legitime (PalWorldSettings.ini) contient
AdminPassword en clair -- exactement le secret RCON masque partout ailleurs
(_masked_registry_entry, /registry deja en require_admin_role). Ouvrir la lecture de
fichiers a un "user" aurait rouvert ce secret par un chemin detourne.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin_role

router = APIRouter(prefix="/api/servers", dependencies=[Depends(require_admin_role)])

ALLOWED_EXTENSIONS = {".ini", ".json", ".cfg", ".txt", ".yml", ".yaml", ".xml",
                      ".properties", ".lua", ".toml"}
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _reject_path_traversal(path: str) -> None:
    # Chemin envoye par le client JS (arborescence deja whitelistee), mais l'agent
    # comme le backend re-valident -- defense en profondeur (meme principe que
    # Invoke-SetupServer, Lot 2). Detecte ".." en segment quel que soit le
    # separateur, et tout chemin absolu (unix, windows, ou lettre de lecteur).
    normalized = path.replace("\\", "/")
    segments = normalized.split("/")
    if ".." in segments or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise HTTPException(400, "chemin invalide (traversal ou absolu)")


def _reject_bad_extension(path: str) -> None:
    lower = path.lower()
    if not any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise HTTPException(400, "extension de fichier non autorisee")


async def _active_entry(request: Request, name: str) -> dict:
    entry = await request.app.state.store.registry.get(name)
    if entry is None:
        raise HTTPException(404, "serveur inconnu")
    if entry.get("status") != "active":
        raise HTTPException(409, f"serveur pas actif (statut : {entry.get('status')})")
    return entry


class ListFilesRequest(BaseModel):
    root: str = Field(pattern="^(install|save)$")


class ReadFileRequest(BaseModel):
    root: str = Field(pattern="^(install|save)$")
    path: str = Field(min_length=1, max_length=500)


class WriteFileRequest(BaseModel):
    root: str = Field(pattern="^(install|save)$")
    path: str = Field(min_length=1, max_length=500)
    content_b64: str = Field(max_length=700_000)  # ~512 Ko apres decodage base64
    expected_sha256: str = Field(pattern=SHA256_PATTERN)


async def _require_save_dir_if_needed(entry: dict, root: str) -> None:
    if root == "save" and not entry.get("save_dir"):
        raise HTTPException(400, "save_dir non configure pour ce serveur")


@router.post("/{name}/files/list", status_code=201)
async def list_files(request: Request, name: str, body: ListFilesRequest):
    entry = await _active_entry(request, name)
    await _require_save_dir_if_needed(entry, body.root)
    store = request.app.state.store
    return await store.add_order(name, "list_files", {"root": body.root},
                                 author=request.state.user["username"])


@router.post("/{name}/files/read", status_code=201)
async def read_file(request: Request, name: str, body: ReadFileRequest):
    entry = await _active_entry(request, name)
    await _require_save_dir_if_needed(entry, body.root)
    _reject_path_traversal(body.path)
    _reject_bad_extension(body.path)
    store = request.app.state.store
    return await store.add_order(name, "read_file", {"root": body.root, "path": body.path},
                                 author=request.state.user["username"])


@router.post("/{name}/files/write", status_code=201)
async def write_file(request: Request, name: str, body: WriteFileRequest):
    entry = await _active_entry(request, name)
    await _require_save_dir_if_needed(entry, body.root)
    _reject_path_traversal(body.path)
    _reject_bad_extension(body.path)
    store = request.app.state.store
    return await store.add_order(name, "write_file", {
        "root": body.root, "path": body.path,
        "content_b64": body.content_b64, "expected_sha256": body.expected_sha256,
    }, author=request.state.user["username"])
