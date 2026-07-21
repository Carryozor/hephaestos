import time
from pathlib import Path

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import SESSION_COOKIE, SESSION_TTL_DAYS, set_session_cookie

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _WINDOW_SECONDS]
    _LOGIN_ATTEMPTS[ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.monotonic())


def _reset_attempts(ip: str) -> None:
    _LOGIN_ATTEMPTS.pop(ip, None)


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@router.post("/api/login")
async def login(request: Request, response: Response, body: LoginRequest):
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        raise HTTPException(429, "trop de tentatives, reessayez dans quelques minutes")

    store = request.app.state.store
    user = await store.get_user(body.username)
    if user is None or not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        _record_failed_attempt(client_ip)
        raise HTTPException(401, "identifiants invalides")

    _reset_attempts(client_ip)
    token = await store.create_session(body.username, ttl_days=SESSION_TTL_DAYS)
    set_session_cookie(response, token)
    return {"ok": True}


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await request.app.state.store.delete_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
