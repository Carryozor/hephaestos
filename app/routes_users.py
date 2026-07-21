"""Gestion des comptes (role admin) + identite de la session courante.

Le mot de passe n'apparait JAMAIS dans une reponse ; le hash reste confine au
storage. Garde-fous : pas d'auto-suppression, jamais moins d'un admin.
"""
import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin, require_admin_role

router = APIRouter(prefix="/api")

USERNAME_PATTERN = r"^[A-Za-z0-9_-]{2,32}$"
PASSWORD_MIN_LENGTH = 12


class CreateUserRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)
    role: str = Field(pattern="^(admin|user)$")
    servers: list[str] = []


class PasswordRequest(BaseModel):
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)


class AccessRequest(BaseModel):
    role: str = Field(pattern="^(admin|user)$")
    servers: list[str] = []


async def _check_servers_known(request: Request, servers: list[str]) -> None:
    known = await request.app.state.store.registry.all()
    unknown = [s for s in servers if s not in known]
    if unknown:
        raise HTTPException(400, f"serveur(s) inconnu(s): {', '.join(unknown)}")


@router.get("/me", dependencies=[Depends(require_admin)])
async def me(request: Request):
    user = request.state.user
    return {"username": user["username"], "role": user["role"], "servers": user["servers"]}


@router.get("/users", dependencies=[Depends(require_admin_role)])
async def list_users(request: Request):
    return {"users": await request.app.state.store.list_users()}


@router.post("/users", status_code=201, dependencies=[Depends(require_admin_role)])
async def create_user(request: Request, body: CreateUserRequest):
    await _check_servers_known(request, body.servers)
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    try:
        await request.app.state.store.create_user(
            body.username, password_hash, role=body.role, servers=body.servers)
    except ValueError:
        raise HTTPException(409, "compte deja existant") from None
    return {"username": body.username, "role": body.role, "servers": body.servers}


@router.delete("/users/{username}", dependencies=[Depends(require_admin_role)])
async def delete_user(request: Request, username: str):
    store = request.app.state.store
    if username == request.state.user["username"]:
        raise HTTPException(400, "impossible de supprimer son propre compte")
    target = await store.get_user(username)
    if target is None:
        raise HTTPException(404, "compte inconnu")
    if target["role"] == "admin" and await store.count_admins() <= 1:
        raise HTTPException(400, "impossible de supprimer le dernier administrateur")
    await store.delete_user(username)
    return {"ok": True}


@router.post("/users/{username}/password", dependencies=[Depends(require_admin_role)])
async def reset_password(request: Request, username: str, body: PasswordRequest):
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    if not await request.app.state.store.set_user_password(username, password_hash):
        raise HTTPException(404, "compte inconnu")
    return {"ok": True}


@router.post("/users/{username}/access", dependencies=[Depends(require_admin_role)])
async def set_access(request: Request, username: str, body: AccessRequest):
    await _check_servers_known(request, body.servers)
    store = request.app.state.store
    target = await store.get_user(username)
    if target is None:
        raise HTTPException(404, "compte inconnu")
    if target["role"] == "admin" and body.role != "admin" and await store.count_admins() <= 1:
        raise HTTPException(400, "impossible de retrograder le dernier administrateur")
    await store.set_user_access(username, body.role, body.servers)
    return {"username": username, "role": body.role, "servers": body.servers}
