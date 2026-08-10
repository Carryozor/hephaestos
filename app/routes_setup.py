"""Première configuration (first-run) : création du compte administrateur initial.

La route POST /api/setup n'est ouverte que tant qu'AUCUN compte n'existe. Dès qu'un
premier admin est créé, elle renvoie 409 — sans ce verrou, la page de setup resterait
un moyen pour n'importe quel visiteur de s'octroyer un compte administrateur. Le compte
créé est toujours de rôle `admin` (le tout premier compte doit pouvoir tout gérer), et
la session est ouverte immédiatement (auto-login) pour éviter un second écran de
connexion juste après l'inscription.

Réutilise les contraintes de validation de routes_users (même politique de mot de passe
et de nom d'utilisateur pour tout le projet).
"""
import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import SESSION_TTL_DAYS, set_session_cookie
from app.routes_users import PASSWORD_MIN_LENGTH, USERNAME_PATTERN

router = APIRouter(prefix="/api/setup")


class SetupRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)


async def _users_exist(request: Request) -> bool:
    return len(await request.app.state.store.list_users()) > 0


@router.get("/needed")
async def setup_needed(request: Request) -> dict:
    """Public : indique seulement s'il faut créer le premier compte. Ne révèle rien
    d'autre (ni noms de comptes ni configuration) — juste l'état first-run, que la
    page de connexion consulte pour décider quel formulaire afficher."""
    return {"needed": not await _users_exist(request)}


@router.post("", status_code=201)
async def setup(request: Request, response: Response, body: SetupRequest) -> dict:
    store = request.app.state.store
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    try:
        await store.create_first_admin(body.username, password_hash)
    except ValueError:
        # course sur deux POST /api/setup simultanés, y compris avec des usernames
        # DIFFERENTS : create_first_admin fait le check + l'insertion sous le même
        # verrou, un seul des deux aboutit — même issue métier qu'un 409 normal.
        raise HTTPException(409, "configuration déjà effectuée : un compte existe déjà") from None

    token = await store.create_session(body.username, ttl_days=SESSION_TTL_DAYS)
    set_session_cookie(response, token)
    return {"ok": True}
