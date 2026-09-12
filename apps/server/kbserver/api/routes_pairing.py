"""一次性配对（docs/02 §4.5、§9.1）：配对码换设备 Token。失败限流；码一次使用、10 分钟有效。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..api.rate_limit import SlidingWindowLimiter
from ..db import get_db
from ..domain.errors import ApiError
from ..models import Device, PairingCode, utcnow
from ..repositories import core as repo
from ..security.tokens import (
    DESKTOP_SCOPES,
    PHONE_SCOPES,
    WEB_SCOPES,
    hash_token,
    issue_token,
    pairing_code_valid,
)

router = APIRouter(prefix="/v1/pairing", tags=["pairing"])

# 每 IP 每 10 分钟最多 20 次尝试（惰性清理见 rate_limit.py）
_rate_limiter = SlidingWindowLimiter(20, timedelta(minutes=10))


def _check_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    _rate_limiter.hit(ip, utcnow(), "配对尝试过于频繁，请稍后再试")


class PairingExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=10, max_length=64)
    device_name: str = Field(min_length=1, max_length=120)


class PairingResult(BaseModel):
    token: str
    device_id: str
    user_id: str
    scopes: list[str]
    expires_at: datetime


@router.post("/exchange", response_model=PairingResult)
def exchange(body: PairingExchange, request: Request, db: Session = Depends(get_db)) -> PairingResult:
    _check_rate(request)
    code = db.query(PairingCode).filter(PairingCode.code_hash == hash_token(body.code)).one_or_none()
    if code is None or not pairing_code_valid(code):
        raise ApiError("FORBIDDEN", "配对码无效或已过期", status_code=403)

    scopes = {"phone": PHONE_SCOPES, "desktop": DESKTOP_SCOPES, "web": WEB_SCOPES}.get(code.device_kind, PHONE_SCOPES)
    device = Device(user_id=code.user_id, kind=code.device_kind, name=body.device_name)
    db.add(device)
    db.flush()
    raw, token = issue_token(code.user_id, device.id, scopes)
    db.add(token)

    code.used_at = utcnow()
    db.flush()

    return PairingResult(
        token=raw,
        device_id=device.id,
        user_id=code.user_id,
        scopes=scopes,
        expires_at=token.expires_at,
    )
