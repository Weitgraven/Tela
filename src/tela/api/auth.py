from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .. import auth
from ..deps import SESSION_COOKIE, Cfg, CurrentPrincipal, Db, client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Db, config: Cfg):
    ip = client_ip(request)
    try:
        session = auth.login(
            db,
            body.username,
            body.password,
            ip=ip,
            user_agent=request.headers.get("user-agent", ""),
            session_hours=config.session_hours,
        )
    except auth.ThrottledError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    auth.audit(db, "auth.login", user_id=session.user.id, ip=ip)
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=config.session_hours * 3600,
        httponly=True,
        secure=config.tls_enabled,
        samesite="strict",
        path="/",
    )
    return {"user": session.user.public(), "csrf": session.csrf_token}


@router.post("/logout")
def logout(principal: CurrentPrincipal, request: Request, response: Response, db: Db):
    auth.logout(db, principal.token)
    auth.audit(db, "auth.logout", user_id=principal.user.id, ip=client_ip(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(principal: CurrentPrincipal):
    return {"user": principal.user.public(), "csrf": principal.csrf_token}
