import asyncio
import os
import secrets
import time

import bcrypt
from fastapi import HTTPException, Request, Response

SESSION_COOKIE = "hephaestos_session"
SESSION_TTL_DAYS = 30

# bcrypt 5 leve ValueError au-dela de 72 octets (il tronquait silencieusement avant).
MAX_PASSWORD_BYTES = 72

# Purge periodique (file_read, sessions) declenchee par l'horloge, pas par un compteur
# d'appels : au plus une fois par _PURGE_INTERVAL_SECONDS. `_clock` est un point
# d'injection pour les tests (ne pas patcher time.monotonic : la boucle asyncio s'en sert).
_PURGE_INTERVAL_SECONDS = 60
_clock = time.monotonic
_last_purge_at: float | None = None

# Hash factice (cout par defaut) : un login sur un utilisateur inconnu execute quand
# meme un checkpw, pour que le temps de reponse ne revele pas l'existence du compte.
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"hephaestos-dummy", bcrypt.gensalt()).decode()


def check_password_bytes(value: str) -> str:
    """Validator pydantic : refuse un mot de passe > 72 octets UTF-8 (limite bcrypt)."""
    if len(value.encode()) > MAX_PASSWORD_BYTES:
        raise ValueError(f"mot de passe trop long (max {MAX_PASSWORD_BYTES} octets UTF-8)")
    return value


async def hash_password(password: str) -> str:
    """bcrypt hors event loop (~200 ms de CPU) : ne bloque pas les autres requetes."""
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())
    return hashed.decode()


async def verify_password(password: str, password_hash: str) -> bool:
    """bcrypt.checkpw hors event loop. Toute ValueError de bcrypt (mot de passe > 72
    octets, hash malforme) = faux, jamais une exception (donc jamais un 500)."""
    try:
        return await asyncio.to_thread(bcrypt.checkpw, password.encode(), password_hash.encode())
    except ValueError:
        return False


def set_session_cookie(response: Response, token: str) -> None:
    # secure=False par defaut : l'UI est servie en HTTP sur le tailnet (pas de TLS).
    # Passer HEPHAESTOS_COOKIE_SECURE=1 si l'UI est un jour exposee derriere un reverse-proxy
    # HTTPS -- le cookie de session ne partira alors qu'en clair chiffre.
    response.set_cookie(
        key=SESSION_COOKIE, value=token, httponly=True, samesite="strict",
        secure=os.environ.get("HEPHAESTOS_COOKIE_SECURE") == "1",
        max_age=SESSION_TTL_DAYS * 24 * 3600, path="/",
    )


async def require_admin(request: Request, response: Response) -> None:
    """Session valide requise. Charge le compte dans request.state.user et applique
    le scoping par serveur : si la route a un path param `name` qui designe un
    serveur CONNU non assigne a un compte role=user -> 403. Un nom inconnu passe
    (la route repond 404, identique pour tous les roles). La suppression du compte
    revoque ses sessions, mais on re-verifie quand meme que le compte existe.
    NB : historiquement cette dependance s'appelle require_admin (= "session du
    dashboard") ; le role admin au sens gestion des comptes = require_admin_role."""
    global _last_purge_at
    store = request.app.state.store
    now = _clock()
    if _last_purge_at is None or now - _last_purge_at >= _PURGE_INTERVAL_SECONDS:
        _last_purge_at = now
        await store.purge_expired_sessions()
        await store.purge_orphan_sessions()
        await store.registry.purge_stale_file_reads()

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="session invalide")
    session = await store.get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="session invalide")
    user = await store.get_user(session["username"])
    if user is None:
        raise HTTPException(status_code=401, detail="session invalide")
    await store.renew_session(token, ttl_days=SESSION_TTL_DAYS)
    set_session_cookie(response, token)
    request.state.user = user

    name = request.path_params.get("name")
    if (name is not None and user["role"] != "admin"
            and name in await store.registry.all()
            and name not in user["servers"]):
        raise HTTPException(status_code=403, detail="serveur non autorise pour ce compte")


async def require_admin_role(request: Request, response: Response) -> None:
    """Gestion des comptes : reserve au role admin."""
    await require_admin(request, response)
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="reserve aux administrateurs")


async def require_agent(request: Request) -> None:
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not (token and secrets.compare_digest(token, request.app.state.settings.agent_token)):
        raise HTTPException(status_code=401, detail="token invalide")
