"""agent 编排容器的中继与事件回传（docs/27 §身份打通、§对话记录）。

前端只跟 A 机说话（既有 Bearer 或中心会话 Cookie），A 机认证通过后**代表用户**
调 agent 容器：

- 不把中心 Cookie 传给容器 —— 它是 `Domain=jerrythealpaca.cn` 的凭据，搬到 B 机后
  是不同可注册域，拿不到也不该拿；同机部署时同样不该拿。中继是同一套代码，
  换机器不改代码，只改部署。
- A→容器用签名中继凭据，载荷里的 `site` **硬编码在 A 机的这条路由里**
  （知识库的中继只签 "kb"），编排服务不接受请求体传 site。这是站点隔离在身份层的落点。
- 容器把用户可见事件回传入库（`POST /v1/agent/internal/events`），A 机 SQLite 才是
  权威记录，容器里的 `$DSH_HOME` 可以丢。

同源收益：网页 ↔ /v1/agent/* 同源，零 CORS；A 机 ↔ 容器是服务端到服务端，也零 CORS。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..models import AgentEvent, AgentSession, utcnow
from ..security.agent_relay import (
    SUB_INGEST,
    SUB_USER,
    issue_relay_token,
    read_relay_token,
    relay_signing_key,
)
from .deps import current_principal

router = APIRouter(prefix="/v1/agent", tags=["agent"])

SITE = "kb"  # 知识库的中继只签这个值；换站点是另写一条路由，不是改参数

# 编排容器内部端口，见 deploy/agent/
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_SSE_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


def _base_url() -> str:
    settings = get_settings()
    if not settings.agent_enabled or not settings.agent_base_url:
        raise ApiError("AGENT_DISABLED", "Agent 对话未启用")
    return settings.agent_base_url.rstrip("/")


def _relay_headers(user_id: str) -> dict[str, str]:
    settings = get_settings()
    token, _ = issue_relay_token(
        relay_signing_key(settings.load_master_key()),
        site=SITE, user_id=user_id, subject=SUB_USER,
        ttl_seconds=settings.agent_relay_ttl_seconds,
    )
    return {"Authorization": f"Bearer {token}"}


async def _forward(method: str, path: str, *, user_id: str, params: dict | None = None,
                   json_body: Any = None) -> Any:
    """代表用户调编排容器。容器不可达时给明确错误，不静默降级成假数据。"""
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, params=params, json=json_body,
                                        headers=_relay_headers(user_id))
    except httpx.HTTPError as exc:
        raise ApiError("AGENT_UNAVAILABLE",
                       "对话服务暂时不可用，收件箱其他功能不受影响；请稍后重试",
                       status_code=503, retryable=True) from exc
    if resp.status_code >= 400:
        raise ApiError("AGENT_UNAVAILABLE",
                       f"对话服务返回异常（HTTP {resp.status_code}）", status_code=502,
                       retryable=resp.status_code >= 500)
    try:
        return resp.json()
    except ValueError as exc:
        raise ApiError("AGENT_UNAVAILABLE", "对话服务响应格式不正确", status_code=502) from exc


# ---- 会话与消息：中继 ----

@router.get("/sessions")
async def list_sessions(principal=Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    """会话列表优先问容器；容器不可达时退回 A 机镜像并如实标注 degraded。

    退回的是**已落库的真实历史**，不是猜测：前端据此显示「同步中/离线」，
    不会看到一条编造的会话。
    """
    try:
        data = await _forward("GET", "/agent/sessions", user_id=principal.user.id)
        return {"sessions": data.get("sessions", []), "degraded": False}
    except ApiError:
        rows = db.scalars(
            select(AgentSession)
            .where(AgentSession.user_id == principal.user.id, AgentSession.site == SITE,
                   AgentSession.state == "open")
            .order_by(AgentSession.updated_at.desc())
        ).all()
        return {
            "sessions": [{"session_id": r.session_id, "title": r.title, "last_seq": r.last_seq,
                          "updated_at": r.updated_at.isoformat()} for r in rows],
            "degraded": True,
        }


@router.post("/sessions", status_code=201)
async def create_session(body: dict, principal=Depends(current_principal)) -> Any:
    """新建会话：标题可由用户给，其余状态一律由容器分配（不采信客户端的 site/user）。"""
    return await _forward("POST", "/agent/sessions", user_id=principal.user.id,
                          json_body={"title": str(body.get("title") or "")[:200]})


@router.post("/sessions/{session_id}/messages", status_code=202)
async def send_message(session_id: str, body: dict,
                       principal=Depends(current_principal)) -> Any:
    """发一轮消息：立即返回 turn_id，过程与结果走 /events 的 SSE。"""
    text = str(body.get("text") or "").strip()
    if not text:
        raise ApiError("SCHEMA_INVALID", "消息内容不能为空", status_code=422)
    if len(text) > 20000:
        raise ApiError("PAYLOAD_TOO_LARGE", "单条消息超过 20000 字符", status_code=413)
    return await _forward("POST", f"/agent/sessions/{session_id}/messages",
                          user_id=principal.user.id, json_body={"text": text})


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request, after: int = 0,
                         principal=Depends(current_principal)):
    """SSE 原样转发（同源，因此前端不引入 CORS；服务端到服务端也不引入）。

    `after=<seq>` 由前端带上，断线重连按序号续传，容器侧从事件表追，不靠内存队列。
    """
    url = f"{_base_url()}/agent/sessions/{session_id}/events"
    headers = _relay_headers(principal.user.id)
    headers["Accept"] = "text/event-stream"
    client = httpx.AsyncClient(timeout=_SSE_TIMEOUT)
    stream = client.stream("GET", url, params={"after": after}, headers=headers)
    try:
        resp = await stream.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise ApiError("AGENT_UNAVAILABLE",
                       "对话服务暂时不可用，请稍后重试", status_code=503, retryable=True) from exc
    if resp.status_code >= 400:
        await resp.aread()
        await stream.aclose()
        await client.aclose()
        raise ApiError("AGENT_UNAVAILABLE", f"对话服务返回异常（HTTP {resp.status_code}）",
                       status_code=502)

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
                if await request.is_disconnected():
                    break
        finally:
            await stream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sessions/{session_id}/history")
def session_history(session_id: str, after_seq: int = 0, limit: int = 200,
                    principal=Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    """历史从 A 机镜像读：刷新页面、容器重启都不影响用户看到自己的对话。"""
    rows = db.scalars(
        select(AgentEvent)
        .where(AgentEvent.user_id == principal.user.id, AgentEvent.site == SITE,
               AgentEvent.session_id == session_id, AgentEvent.seq > after_seq)
        .order_by(AgentEvent.seq)
        .limit(max(1, min(int(limit), 500)))
    ).all()
    mirror = db.scalar(
        select(AgentSession).where(AgentSession.user_id == principal.user.id,
                                   AgentSession.site == SITE,
                                   AgentSession.session_id == session_id)
    )
    return {
        "session_id": session_id,
        "last_seq": mirror.last_seq if mirror else 0,
        "events": [{"seq": r.seq, "kind": r.kind, "payload": r.payload_json,
                    "created_at": r.created_at.isoformat()} for r in rows],
    }


@router.put("/sessions/{session_id}")
async def rename_session(session_id: str, body: dict,
                         principal=Depends(current_principal)) -> Any:
    return await _forward("PUT", f"/agent/sessions/{session_id}", user_id=principal.user.id,
                          json_body={"title": str(body.get("title") or "")[:200]})


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str, principal=Depends(current_principal),
                        db: Session = Depends(get_db)) -> Any:
    """关闭会话：容器侧结束子进程，但**不删 $DSH_HOME**（历史可续），A 机镜像只标 closed。

    镜像不删：用户以后回来还要能看到这段对话；真正的清理走保留期清扫。
    """
    result = await _forward("DELETE", f"/agent/sessions/{session_id}", user_id=principal.user.id)
    row = _owned_session(db, principal.user.id, session_id)
    if row is not None:
        row.state = "closed"
        row.closed_at = utcnow()
        db.commit()
    return result


# ---- 容器 → A 机：事件回传（双写的另一头）----

def _require_ingest(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ApiError("AUTH_EXPIRED", "缺少回传凭据", status_code=401)
    settings = get_settings()
    payload = read_relay_token(relay_signing_key(settings.load_master_key()), auth[7:].strip(),
                               expect_subject=SUB_INGEST)
    if payload is None:
        raise ApiError("AUTH_EXPIRED", "回传凭据无效或已过期", status_code=401)
    if payload.get("site") != SITE:
        # 中继凭据的 site 由 A 机自己签；不是本站点的一律不写
        raise ApiError("FORBIDDEN", "回传凭据不属于本站点", status_code=403)
    return payload


@router.post("/internal/llm-token")
def issue_llm_token_for_session(body: dict, db: Session = Depends(get_db),
                                claims: dict = Depends(_require_ingest)) -> dict:
    """给编排容器签一枚该用户的 LLM 代理会话 token（每个 dsh 进程一枚）。

    profile 由 A 机用**和网页对话同一条规则**（`domain/provider_select.pick_profile`）
    选定，容器不参与选择、也拿不到 Key —— 否则同一个人在两处会用上不同配置。
    token 里只有归属，没有 Key 也没有原文。
    """
    from ..domain.provider_select import pick_profile
    from ..security.agent_llm_tokens import issue_llm_token, issue_mcp_capability
    from ..security.tokens import MCP_READ_SCOPE, MCP_WRITE_SCOPE

    settings = get_settings()
    user_id = str(claims["user_id"])
    requested = str(body.get("profile_id") or "") or None
    picked = pick_profile(db, user_id, requested)
    if picked is None:
        raise ApiError("PROVIDER_AUTH_FAILED", "该账号下没有可用的模型配置", status_code=422)
    profile, _credential = picked
    token, expires_at = issue_llm_token(settings.load_master_key(), site=SITE, user_id=user_id,
                                        profile_id=profile.id)
    # 容器里的 dsh 连 /mcp 用的能力凭据：同一把 TTL，随进程回收一起换新。
    # 读写两档都给，是因为面板允许 agent 往用户自己的收件箱投草稿——
    # 这与「用户自己在面板里操作」同权，不扩权（docs/27 §4）。
    capability, _cap_exp = issue_mcp_capability(
        settings.load_master_key(), site=SITE, user_id=user_id,
        scopes=[MCP_READ_SCOPE, MCP_WRITE_SCOPE])
    return {
        "token": token,
        "expires_at": expires_at,
        "mcp_token": capability,
        "profile_id": profile.id,
        "model": profile.model,
        # dsh 的 provider.baseURL：OpenAI 兼容形态，运行时会自己接 /chat/completions
        "base_url": f"{settings.public_base_url}/v1/llm",
    }


def _owned_session(db: Session, user_id: str, session_id: str) -> AgentSession | None:
    return db.scalar(
        select(AgentSession).where(AgentSession.site == SITE, AgentSession.session_id == session_id,
                                   AgentSession.user_id == user_id)
    )


@router.post("/internal/sessions")
def upsert_sessions(body: dict, db: Session = Depends(get_db),
                    claims: dict = Depends(_require_ingest)) -> dict:
    """容器声明「这个用户开了这个会话」；归属仍按 (site, session_id, user_id) 核对。"""
    user_id = str(claims["user_id"])
    session_id = str(body.get("session_id") or "")
    if not session_id:
        raise ApiError("SCHEMA_INVALID", "缺少 session_id", status_code=422)
    row = _owned_session(db, user_id, session_id)
    if row is None:
        # 别人已经占用这个会话 ID：不接受，也不覆盖（结构性隔离而不是查询过滤）
        taken = db.scalar(select(AgentSession).where(AgentSession.site == SITE,
                                                     AgentSession.session_id == session_id))
        if taken is not None:
            raise ApiError("FORBIDDEN", "会话归属不符", status_code=403)
        row = AgentSession(user_id=user_id, site=SITE, session_id=session_id,
                           title=str(body.get("title") or "")[:200])
        db.add(row)
    else:
        title = str(body.get("title") or "")[:200]
        if title:
            row.title = title
    db.commit()
    return {"session_id": session_id, "last_seq": row.last_seq}


@router.post("/internal/events")
def ingest_events(body: dict, db: Session = Depends(get_db),
                  claims: dict = Depends(_require_ingest)) -> dict:
    """批量回传：按 (session_id, seq) upsert，重复投递不产生第二条。

    seq 由编排服务单调分配，A 机不自己编号；这样断连重发、容器重启都是幂等的。
    只接受用户可见事件；流式 delta 已在容器侧合并成整条 assistant_message。
    """
    user_id = str(claims["user_id"])
    session_id = str(body.get("session_id") or "")
    events = body.get("events")
    if not session_id or not isinstance(events, list):
        raise ApiError("SCHEMA_INVALID", "缺少 session_id 或 events", status_code=422)
    row = _owned_session(db, user_id, session_id)
    if row is None:
        raise ApiError("NOT_FOUND", "会话未登记", status_code=404)
    accepted = 0
    max_seq = row.last_seq
    for item in events[:200]:
        if not isinstance(item, dict):
            continue
        try:
            seq = int(item.get("seq"))
        except (TypeError, ValueError):
            continue
        kind = str(item.get("kind") or "")[:40]
        if not kind:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        existing = db.scalar(
            select(AgentEvent).where(AgentEvent.session_id == session_id, AgentEvent.seq == seq)
        )
        if existing is None:
            db.add(AgentEvent(user_id=user_id, site=SITE, session_id=session_id, seq=seq,
                              kind=kind, payload_json=payload))
            accepted += 1
        max_seq = max(max_seq, seq)
    row.last_seq = max_seq
    row.updated_at = utcnow()
    db.commit()
    return {"session_id": session_id, "accepted": accepted, "last_seq": max_seq}
