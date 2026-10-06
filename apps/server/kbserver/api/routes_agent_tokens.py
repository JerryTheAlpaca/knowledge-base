"""Agent Token 管理（docs/27 §agent token 生命周期）。

一类专门给 MCP / agent 用的 Token，**不复用设备 Token 语义**：撤销设备 Token 会
连带掐掉 Obsidian 同步的回执通道，而这里的一键停用只影响 MCP 接入。

- 签发：用户在网页「Agent 接入」设置页自己点，明文只显示一次；
- 校验：走 `api/deps.py:_load_device_principal` 同一条路径，撤销后下一个请求即 401
  ——不存在 TTL 正缓存，也不给「等它自然过期」这种半失效状态；
- 轮换：dsh 那边的 header 是启动时读一次的静态配置，没有刷新钩子，所以 TTL 长
  （`AGENT_TOKEN_TTL_DAYS`）；编排服务改写 env/patch 时旧 Token 留 24 小时重叠宽限。

Token 不进 URL、不进日志：只有 POST 的响应体里出现一次。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..models import Device, Token, utcnow
from ..security.tokens import (
    AGENT_DEVICE_KIND,
    AGENT_SCOPES,
    MCP_READ_SCOPE,
    MCP_WRITE_SCOPE,
    issue_agent_token,
)
from .deps import require_scope

router = APIRouter(prefix="/v1/agent-tokens", tags=["agent"])


class AgentTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    # 只允许这两项，其余静默忽略：不能因为拼错就放大权限（同设备授权的做法）
    scopes: list[str] = Field(default_factory=lambda: list(AGENT_SCOPES))


class AgentTokenOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_id: str
    name: str
    scopes: list[str]
    created_at: str
    expires_at: str
    revoked: bool
    last_seen_at: str | None


def _require_enabled() -> None:
    if not get_settings().agent_enabled:
        raise ApiError("AGENT_DISABLED", "Agent 接入未启用")


def _agent_devices(db: Session, user_id: str) -> list[Device]:
    return list(db.scalars(
        select(Device).where(Device.user_id == user_id, Device.kind == AGENT_DEVICE_KIND)
        .order_by(Device.created_at)
    ))


@router.post("", response_model=dict, status_code=201)
def create_agent_token(body: AgentTokenCreate, principal=Depends(require_scope("devices:manage")),
                       db: Session = Depends(get_db)) -> dict:
    """签发一枚 Agent Token；明文只在这里返回一次。"""
    _require_enabled()
    user = principal.user
    scopes = [s for s in dict.fromkeys(body.scopes) if s in (MCP_READ_SCOPE, MCP_WRITE_SCOPE)]
    if not scopes:
        raise ApiError("SCHEMA_INVALID", "至少需要 mcp:read 或 mcp:write 一项权限", status_code=422)
    device = Device(user_id=user.id, kind=AGENT_DEVICE_KIND, name=body.name.strip()[:120])
    db.add(device)
    db.flush()
    raw, token = issue_agent_token(user.id, device.id, scopes)
    db.add(token)
    db.commit()
    return {
        "token": raw,
        "token_id": token.id,
        "name": device.name,
        "scopes": scopes,
        "expires_at": token.expires_at.isoformat(),
    }


@router.get("", response_model=list[AgentTokenOut])
def list_agent_tokens(principal=Depends(require_scope("devices:manage")),
                      db: Session = Depends(get_db)) -> list[AgentTokenOut]:
    user = principal.user
    out: list[AgentTokenOut] = []
    for device in _agent_devices(db, user.id):
        tokens = list(db.scalars(
            select(Token).where(Token.user_id == user.id, Token.device_id == device.id)
            .order_by(Token.created_at)
        ))
        for token in tokens:
            out.append(AgentTokenOut(
                token_id=token.id,
                name=device.name,
                scopes=list(token.scopes_json or []),
                created_at=token.created_at.isoformat(),
                expires_at=token.expires_at.isoformat(),
                revoked=token.revoked_at is not None,
                last_seen_at=device.last_seen_at.isoformat() if device.last_seen_at else None,
            ))
    return out


@router.post("/{token_id}/revoke")
def revoke_agent_token(token_id: str, principal=Depends(require_scope("devices:manage")),
                       db: Session = Depends(get_db)) -> dict:
    """一键停用：下一个请求即 401（`token_valid` 读 revoked_at，没有正缓存可等）。

    设备行只服务这一类 Token，最后一枚停用后把设备一起标掉，免得设备列表里
    留一堆空壳。
    """
    user = principal.user
    token = db.scalar(select(Token).where(Token.id == token_id, Token.user_id == user.id))
    if token is None:
        raise ApiError("NOT_FOUND", "Token 不存在", status_code=404)
    now = utcnow()
    if token.revoked_at is None:
        token.revoked_at = now
    device = db.get(Device, token.device_id)
    if device is not None and device.kind == AGENT_DEVICE_KIND:
        remaining = db.scalar(
            select(Token.id).where(Token.device_id == device.id, Token.revoked_at.is_(None))
        )
        if remaining is None and device.revoked_at is None:
            device.revoked_at = now
    db.commit()
    return {"token_id": token_id, "revoked": True}
