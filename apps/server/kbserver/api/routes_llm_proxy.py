"""per-user LLM 代理（docs/27 §Phase 2）。

目的：让 agent 用「每个用户自己的 Key」跑模型调用，而**真实 Key 永不离开 api 容器**。

⚠️ 这里绝不复用 `providers/llm.generate_conversation()`，后来者请不要「顺手改回去」：
它不发 `tools` 字段，且当 `choices[0].message.content` 为空时（模型这一轮只想调用工具）
直接抛 `ProviderRetryable`（见 llm.py 的空内容分支）。而 agent 循环的核心恰恰是
tool_calls —— 走那条路会把每一次工具调用变成一次重试。所以本代理做**原始 HTTP 透传**：
只换 Key、只强制 model、只按 capabilities 白名单重注私有字段，其余原样进出。

流式同样原样透传字节（`aiter_raw`），因此 DeepSeek 的 `reasoning_content` 这类
wire 扩展自动保留，不需要维护一份会过期的字段清单；只在末帧抓 `usage` 记账。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db, get_session_factory
from ..domain.errors import ApiError
from ..domain.provider_select import pick_profile, reveal_key
from ..models import AgentBudget, ProviderProfile, utcnow
from ..providers.llm import OpenAICompatibleProvider, normalize_usage
from ..security.agent_llm_tokens import read_llm_token

router = APIRouter(prefix="/v1/llm", tags=["llm-proxy"])

# 连接/读写超时：agent 的一轮可能很长。read 600s 与 Caddy 侧 read_timeout 同量级
_TIMEOUT = httpx.Timeout(600.0, connect=15.0, write=30.0, pool=10.0)
RETRY_AFTER_SECONDS = "5"


class _Gates:
    """在途请求计数：每用户一个上限 + 全局上限，超出就 503 + Retry-After。

    用计数而不是 asyncio.Semaphore：这里的「排队」不是等待被唤醒，而是明确告诉
    调用方「现在不行，稍后再来」——2GB 的机器上把并发压住比让请求堆积更有用，
    堆着的请求最终也会在 httpx 池里超时，还占着 api 容器的 256MiB。
    """

    def __init__(self) -> None:
        self.per_user: dict[str, int] = {}
        self.global_inflight = 0
        self.user_limit = 0
        self.global_limit = 0

    def _limits(self) -> tuple[int, int]:
        settings = get_settings()
        return (max(1, settings.agent_llm_per_user_concurrency),
                max(1, settings.agent_llm_max_concurrency))

    def acquire(self, user_id: str) -> bool:
        user_limit, global_limit = self._limits()
        if (self.user_limit, self.global_limit) != (user_limit, global_limit):
            # 配置在运行期被改（测试注入、部署调参）：按新值重算已占用量，不复活旧上限
            self.user_limit, self.global_limit = user_limit, global_limit
        if self.global_inflight >= global_limit:
            return False
        if self.per_user.get(user_id, 0) >= user_limit:
            return False
        self.global_inflight += 1
        self.per_user[user_id] = self.per_user.get(user_id, 0) + 1
        return True

    def release(self, user_id: str) -> None:
        self.global_inflight = max(0, self.global_inflight - 1)
        left = self.per_user.get(user_id, 1) - 1
        if left <= 0:
            self.per_user.pop(user_id, None)
        else:
            self.per_user[user_id] = left


_gates = _Gates()

# 测试注入 httpx.MockTransport 的接缝（与 OpenAICompatibleProvider 的 transport 参数
# 同一个约定）；生产不设置，保持 None 走默认网络传输。
_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _client() -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": _TIMEOUT}
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    return httpx.AsyncClient(**kwargs)


class LlmSession:
    """一次代理调用的归属：site / user / profile 全部来自签名 token，永不来自请求体。"""

    def __init__(self, *, site: str, user_id: str, profile_id: str):
        self.site = site
        self.user_id = user_id
        self.profile_id = profile_id


def _require_llm_session(request: Request, db: Session = Depends(get_db)) -> LlmSession:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ApiError("AUTH_EXPIRED", "缺少 LLM 会话凭据", status_code=401)
    settings = get_settings()
    if not settings.agent_enabled:
        raise ApiError("AGENT_DISABLED", "Agent 接入未启用")
    payload = read_llm_token(settings.load_master_key(), auth[7:].strip())
    if payload is None:
        raise ApiError("AUTH_EXPIRED", "会话凭据无效或已过期", status_code=401)
    return LlmSession(site=str(payload["site"]), user_id=str(payload["user_id"]),
                      profile_id=str(payload["profile_id"]))


def _budget_period() -> str:
    return utcnow().strftime("%Y-%m-%d")


def _budget_row(db: Session, user_id: str, profile_id: str, settings: Settings) -> AgentBudget:
    period = _budget_period()
    row = db.query(AgentBudget).filter(
        AgentBudget.user_id == user_id, AgentBudget.profile_id == profile_id,
        AgentBudget.period == period,
    ).one_or_none()
    if row is None:
        row = AgentBudget(user_id=user_id, profile_id=profile_id, period=period)
        db.add(row)
        db.flush()
    return row


def _over_budget(row: AgentBudget, settings: Settings) -> bool:
    return (row.input_tokens_used >= settings.agent_budget_input_tokens_per_day
            or row.output_tokens_used >= settings.agent_budget_output_tokens_per_day
            or row.requests_used >= settings.agent_budget_requests_per_day)


def _assert_budget_available(row: AgentBudget, settings: Settings) -> None:
    """入口先查：超限直接 429，不转发，因此不产生任何供应商费用。"""
    if row.exhausted_at is not None or _over_budget(row, settings):
        row.exhausted_at = utcnow()
        raise ApiError("BUDGET_EXHAUSTED", "今日 Agent 用量额度已用完", status_code=429)


def _apply_usage(db: Session, user_id: str, profile_id: str, usage: dict | None) -> None:
    """出口累加（流式路径用自己的短事务）。

    只认归一化后的技术计数，缺失的不填 0：填 0 会把「供应商没返回 usage」
    伪装成「这次没花钱」。
    """
    settings = get_settings()
    row = _budget_row(db, user_id, profile_id, settings)
    row.requests_used = (row.requests_used or 0) + 1
    if usage:
        inc_in = usage.get("input_tokens_total")
        inc_out = usage.get("output_tokens")
        if isinstance(inc_in, int) and inc_in > 0:
            row.input_tokens_used += inc_in
        if isinstance(inc_out, int) and inc_out > 0:
            row.output_tokens_used += inc_out
    if _over_budget(row, settings):
        row.exhausted_at = utcnow()
    db.commit()


def _resolve_profile(db: Session, session: LlmSession) -> tuple[ProviderProfile, str]:
    """按 token 里的 profile_id 定位配置，并核对归属。

    这里刻意区分两种「拿不到」：指向**别人的**配置是越权，直接 403；自己的配置没了
    （撤销凭据、删了配置）才是 422 的配置问题。前者不给出「没有可用配置」这种
    含糊说法，免得越权探测看起来像用户配置错了。
    """
    settings = get_settings()
    requested = db.get(ProviderProfile, session.profile_id)
    if requested is not None and requested.user_id != session.user_id:
        raise ApiError("FORBIDDEN", "模型配置不属于当前用户", status_code=403)
    picked = pick_profile(db, session.user_id, session.profile_id)
    if picked is None:
        raise ApiError("PROVIDER_AUTH_FAILED", "该账号下没有可用的模型配置", status_code=422)
    profile, credential = picked
    key = reveal_key(db, user_id=session.user_id, profile_id=profile.id, credential=credential,
                     master_key=settings.load_master_key())
    return profile, key


def _gateway(profile: ProviderProfile) -> OpenAICompatibleProvider:
    """只为复用 endpoint 规范化、缓存字段白名单与 usage 协议判定，不发请求。"""
    return OpenAICompatibleProvider(endpoint=profile.endpoint, api_key="", model=profile.model,
                                     capabilities=profile.capabilities_json)


def _cache_policy(settings: Settings, profile: ProviderProfile) -> dict:
    """稳定不透明的 prompt_cache_key：同一用户同一配置的多轮才谈得上命中前缀缓存。"""
    material = f"agent-llm|{profile.user_id}|{profile.id}|{profile.version}|{profile.model}"
    digest = hmac_key(settings, material)
    policy: dict[str, Any] = {"prompt_cache_key": f"kbagent-{digest[:48]}"}
    retention = (profile.capabilities_json or {}).get("cache_retention")
    if retention in ("in_memory", "24h"):
        policy["prompt_cache_retention"] = retention
    return policy


def hmac_key(settings: Settings, material: str) -> str:
    import hashlib
    import hmac

    return hmac.new(settings.load_master_key(), material.encode("utf-8"), hashlib.sha256).hexdigest()


def _forward_body(body: dict, profile: ProviderProfile, gateway: OpenAICompatibleProvider,
                  settings: Settings) -> dict:
    """透传换 Key，不重新装配请求：删客户端 model → 强制 profile 的 model → 重注私有缓存字段。"""
    forward = dict(body)
    forward["model"] = profile.model
    forward.update(gateway._cache_fields(_cache_policy(settings, profile)))
    return forward


def _usage_from_sse(text: str) -> dict | None:
    """从 SSE 文本里找带 usage 的那一帧（DeepSeek/OpenAI 把它放在末帧）。"""
    found: dict | None = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("usage"):
            found = data
    return found


def _error_payload(resp: httpx.Response) -> dict:
    """401/403 归一成「凭据被拒」，其余只给状态码。

    不透传上游正文：里面可能带账号标识与请求 ID；用户语言契约（docs/17 §10.3）
    要求错误文案受控，也不给「顺手把上游调试信息带回浏览器」留口子。
    """
    if resp.status_code in (401, 403):
        return {"error": {"code": "PROVIDER_AUTH_FAILED",
                          "message": "模型凭据被拒绝，请在设置里检查你的 API Key",
                          "user_message": "模型凭据被拒绝，请在设置里检查你的 API Key",
                          "action": "check_key", "retryable": False}}
    return {"error": {"code": "PROVIDER_REQUEST_FAILED",
                      "message": f"模型服务拒绝了本次请求（HTTP {resp.status_code}）",
                      "user_message": "模型服务拒绝了本次请求，请稍后重试或检查模型配置",
                      "retryable": resp.status_code >= 500}}


def _upstream_response(resp: httpx.Response) -> JSONResponse:
    """非流式的上游响应：2xx 原样交回 JSON，错误按受控文案回。"""
    if resp.status_code >= 400:
        return JSONResponse(status_code=resp.status_code, content=_error_payload(resp))
    try:
        payload = resp.json()
    except ValueError:
        # 兼容网关返了非 JSON：不猜内容，原样作为文本交回，让调用方自己判断
        return JSONResponse(status_code=502, content={
            "error": {"code": "AGENT_UNAVAILABLE", "message": "模型服务返回了非 JSON 响应",
                      "user_message": "模型服务返回异常，请稍后重试", "retryable": True}})
    return JSONResponse(status_code=resp.status_code, content=payload)


@router.post("/chat/completions")
async def chat_completions(request: Request, session: LlmSession = Depends(_require_llm_session),
                           db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        body = await request.json()
    except ValueError as exc:
        raise ApiError("SCHEMA_INVALID", "请求体不是合法 JSON", status_code=422) from exc
    if not isinstance(body, dict):
        raise ApiError("SCHEMA_INVALID", "请求体必须是 JSON 对象", status_code=422)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ApiError("SCHEMA_INVALID", "messages 不能为空", status_code=422)

    profile, api_key = _resolve_profile(db, session)
    budget = _budget_row(db, session.user_id, profile.id, settings)
    _assert_budget_available(budget, settings)
    db.commit()

    gateway = _gateway(profile)
    forward_body = _forward_body(body, profile, gateway, settings)
    headers = {
        "Authorization": f"Bearer {api_key}",  # 明文只活在这个字典与这一次请求里
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if body.get("stream") else "application/json",
    }
    wants_stream = bool(body.get("stream"))

    if not _gates.acquire(session.user_id):
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "RATE_LIMITED",
                               "message": "Agent 模型调用并发已满",
                               "user_message": "服务器忙，请稍后重试",
                               "action": "retry", "retryable": True}},
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )

    # 释放必须是幂等的：非流式路径先释放、再记账，记账或响应装配出错时外层还会
    # 兜一次释放，计数器多减一次就等于凭空多出一个并发位
    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            released = True
            _gates.release(session.user_id)

    client = _client()
    try:
        if not wants_stream:
            try:
                resp = await client.post(gateway.endpoint, json=forward_body, headers=headers)
            except httpx.HTTPError as exc:
                await client.aclose()
                release_once()
                raise ApiError("AGENT_UNAVAILABLE", f"模型服务不可达：{type(exc).__name__}",
                               status_code=502) from exc
            usage = None
            if resp.status_code < 400:
                try:
                    usage = normalize_usage(resp.json(), gateway.usage_protocol)
                except ValueError:
                    usage = None
            await client.aclose()
            release_once()
            _apply_usage(db, session.user_id, profile.id, usage)
            return _upstream_response(resp)

        stream = client.stream("POST", gateway.endpoint, json=forward_body, headers=headers)
        try:
            resp = await stream.__aenter__()
        except httpx.HTTPError as exc:
            await client.aclose()
            release_once()
            raise ApiError("AGENT_UNAVAILABLE", f"模型服务不可达：{type(exc).__name__}",
                           status_code=502) from exc
        if resp.status_code >= 400:
            await resp.aread()
            await stream.__aexit__(None, None, None)
            await client.aclose()
            release_once()
            return _upstream_response(resp)
        return StreamingResponse(
            _relay(resp, stream, client, user_id=session.user_id, profile_id=profile.id,
                   usage_protocol=gateway.usage_protocol, release=release_once),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except BaseException:
        release_once()
        await client.aclose()
        raise


async def _relay(resp: httpx.Response, stream, client: httpx.AsyncClient, *, user_id: str,
                 profile_id: str, usage_protocol: str, release) -> AsyncIterator[bytes]:
    """原样透传字节；记账用**自己的短会话**，不碰请求级 Session。

    StreamingResponse 的生成器在依赖退出之后才跑，那时请求级 db 已经关了；
    而 SQLite 单写端下这里只是一行累加，短事务最合适。
    """
    tail = ""
    usage: dict | None = None
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
            tail = (tail + chunk.decode("utf-8", errors="ignore"))[-8192:]
            found = _usage_from_sse(tail)
            if found is not None:
                usage = normalize_usage(found, usage_protocol)
    finally:
        release()
        try:
            with get_session_factory()() as db:
                _apply_usage(db, user_id, profile_id, usage)
        finally:
            await stream.__aexit__(None, None, None)
            await client.aclose()
