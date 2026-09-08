"""Web 收件箱入口（docs/02 §2.1、§9.1）。

- POST /v1/web/session：一次性配对码（kind=web）换会话。会话 Token 存 HttpOnly
  Cookie（SameSite=Strict），CSRF Token 存可读 Cookie，写操作由 deps._check_csrf 校验。
- POST /v1/web/logout：撤销当前会话 Token 并清理 Cookie。
- GET /inbox：收件箱单页应用；GET / 跳转过去。

会话有效期 WEB_SESSION_TTL_DAYS（默认 7 天），独立于 90 天的设备 Token。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .deps import CSRF_COOKIE, SESSION_COOKIE, current_principal
from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..models import Device, PairingCode, Token, utcnow
from ..security.tokens import WEB_SCOPES, hash_token, new_service_token, pairing_code_valid

router = APIRouter(tags=["web-inbox"])

WEB_STATIC_DIR = Path(__file__).resolve().parents[1] / "web_static"

# 登录尝试限流：每 IP 每 10 分钟最多 20 次（与配对接口同级）
_login_attempts: dict[str, list] = {}
_LOGIN_RATE_LIMIT = 20
_LOGIN_RATE_WINDOW = timedelta(minutes=10)


def _cookie_secure() -> bool:
    return get_settings().public_base_url.startswith("https://")


def _set_session_cookies(response: Response, raw_token: str, csrf: str, max_age: int) -> None:
    secure = _cookie_secure()
    response.set_cookie(
        SESSION_COOKIE, raw_token, max_age=max_age, httponly=True,
        samesite="strict", secure=secure, path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False,
        samesite="strict", secure=secure, path="/",
    )


class WebLoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=10, max_length=64)
    device_name: str = Field(default="Web 收件箱", min_length=1, max_length=120)


class WebLoginResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    device_id: str
    csrf_token: str
    expires_at: str


@router.post("/v1/web/session", response_model=WebLoginResult)
def web_login(body: WebLoginInput, request: Request, response: Response, db: Session = Depends(get_db)) -> WebLoginResult:
    ip = request.client.host if request.client else "unknown"
    now = utcnow()
    window = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_RATE_WINDOW]
    if len(window) >= _LOGIN_RATE_LIMIT:
        raise ApiError("RATE_LIMITED", "登录尝试过于频繁，请稍后再试", status_code=429)
    window.append(now)
    _login_attempts[ip] = window

    code = db.query(PairingCode).filter(PairingCode.code_hash == hash_token(body.code)).one_or_none()
    if code is None or not pairing_code_valid(code) or code.device_kind != "web":
        raise ApiError("FORBIDDEN", "配对码无效或不是 Web 配对码", status_code=403)

    settings = get_settings()
    ttl_days = settings.web_session_ttl_days
    device = Device(user_id=code.user_id, kind="web", name=body.device_name[:120])
    db.add(device)
    db.flush()
    raw = new_service_token()
    token = Token(
        user_id=code.user_id,
        device_id=device.id,
        token_hash=hash_token(raw),
        scopes_json=list(WEB_SCOPES),
        expires_at=now + timedelta(days=ttl_days),
    )
    db.add(token)
    code.used_at = now
    db.flush()

    csrf = new_service_token()
    _set_session_cookies(response, raw, csrf, max_age=ttl_days * 24 * 3600)
    return WebLoginResult(
        user_id=code.user_id, device_id=device.id,
        csrf_token=csrf, expires_at=token.expires_at.isoformat(),
    )


@router.post("/v1/web/logout")
def web_logout(request: Request, response: Response, principal=Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    _user, _device, token = principal
    token.revoked_at = utcnow()
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/inbox", include_in_schema=False)
def inbox_page() -> FileResponse:
    return FileResponse(WEB_STATIC_DIR / "inbox.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/inbox", status_code=307)
