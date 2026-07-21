import os
import secrets

from fastapi import HTTPException, Request, Response

SESSION_COOKIE = "hephaestos_session"
SESSION_TTL_DAYS = 30

_PURGE_EVERY_N_CALLS = 50
_purge_call_counter = 0


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
    global _purge_call_counter
    store = request.app.state.store
    _purge_call_counter += 1
    if _purge_call_counter >= _PURGE_EVERY_N_CALLS:
        _purge_call_counter = 0
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
