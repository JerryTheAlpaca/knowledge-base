"""知识库 MCP server（docs/27）。

落点在现有 api 容器：无状态短请求、不做模型调用，换不到新容器的隔离收益
（同一 SQLite、同一 data 卷、同一 master_key），却多一份运维面。

三条硬边界：
- **json_response + stateless**：每个 POST 返回单个 JSON，不开 server→client
  常驻 SSE 流。dsh 侧「Streamable HTTP 的重连归属」语义还未定，无状态短响应
  直接绕开这条风险，而不是去猜它的重连行为。
- **认证只认 agent Token 的 Bearer**：复用 `api/deps.py` 的
  `_load_device_principal`（显式 Bearer 无效直接拒绝，不回退浏览器 Cookie）。
  身份与用户隔离由这一条查询落实，工具内部不再自行判断「这是谁的库」。
- **未注册的工具即不可调用**：改原文、删除、refetch、模型 Key、admin、同步回执
  一律不在这里出现。不注册比运行时判断更硬。

工具实现全部复用 HTTP 路由抽出的实现体（`_list_items_impl` 等），不维护第二套
收件箱语义；返回内容一律限长，避免一次读取把模型上下文占满。
"""
from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from ..config import get_settings
from ..db import get_session_factory
from ..domain.errors import ApiError
from ..security.tokens import MCP_READ_SCOPE, MCP_WRITE_SCOPE, has_scope
from .deps import _load_device_principal


def _surface_errors(fn):
    """把 ApiError 换成带原因与错误码的 ToolError。

    SDK 对普通异常只给一句「Error executing tool X」——模型既不知道是 404 还是 403，
    也就无从判断该重试、该换个参数，还是该回头问用户。工具面把原因说清楚，
    是这套 MCP 能用的前提，不是锦上添花。
    """
    import functools

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    return wrapped

# 工具面：读取收件箱 + 往收件箱投递 + 异步任务句柄。参数上限是模型可见的契约，
# 改数字要同步 docs/27，不要只改代码。
MAX_LIST_LIMIT = 50
MAX_READ_CHARS_DEFAULT = 20_000
MAX_READ_CHARS_LIMIT = 60_000
# 工具描述里写死的轮询口径：超时 60s 之下，任何要等模型的活都只能提交后轮询
POLL_GUIDANCE = ("异步任务：先用 kb_submit_job 拿 job_id，再用 kb_get_job 轮询；"
                 "轮询间隔不少于 5 秒、最多 20 次；不要用同一个 idempotency_key 重复提交。")


class AgentTokenVerifier:
    """MCP Bearer -> AccessToken。

    两类凭据都认，走的是同一个入口：

    1. **agent token**（用户在设置页自己签发、可一键停用）：复用
       `api/deps.py:_load_device_principal`，显式 Bearer 无效就直接拒绝，
       不回退浏览器 Cookie —— 撤销后下一个请求即 401，没有 TTL 正缓存可等。
    2. **MCP 能力 token**（A 机为第一方网页对话现签的 15 分钟凭据）：用户在网页里
       本来就是登录态，容器替他读自己的库不该再要一枚长期凭据；它不扩权，
       能做的事与「这个用户自己在面板里操作」一样。
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        capability = self._from_capability(token)
        if capability is not None:
            return capability
        with get_session_factory()() as db:
            try:
                principal = _load_device_principal(db, token)
                db.commit()  # _load_device_principal 会更新设备 last_seen_at
            except ApiError:
                return None
        mcp_scopes = [s for s in principal.scopes if s in (MCP_READ_SCOPE, MCP_WRITE_SCOPE)]
        if not mcp_scopes:
            return None
        token_row = principal.token
        return AccessToken(
            token=token,
            # client_id 在 SDK 里表示「哪个客户端」，这里填设备 ID：只用于会话归属
            # 比对，不参与权限判断
            client_id=principal.device.id if principal.device else "agent",
            scopes=mcp_scopes,
            expires_at=int(token_row.expires_at.timestamp()) if token_row else None,
            subject=principal.user.id,
        )

    def _from_capability(self, token: str) -> AccessToken | None:
        from ..security.agent_llm_tokens import read_mcp_capability

        settings = get_settings()
        if not settings.agent_enabled:
            return None
        payload = read_mcp_capability(settings.load_master_key(), token)
        if payload is None or payload.get("site") != "kb":
            return None
        scopes = [s for s in payload["scopes"] if s in (MCP_READ_SCOPE, MCP_WRITE_SCOPE)]
        if not scopes:
            return None
        return AccessToken(token=token, client_id="web-panel", scopes=scopes,
                           expires_at=int(payload["exp"]), subject=str(payload["user_id"]))


def _principal_user_id() -> str:
    """当前请求的用户 ID：只从已验签的 AccessToken 取，永不从工具参数取。"""
    access = get_access_token()
    if access is None or not access.subject:
        raise ApiError("AUTH_EXPIRED", "MCP 会话未认证", status_code=401)
    return access.subject


def _require_write_scope() -> None:
    access = get_access_token()
    if access is None or not has_scope(access.scopes or [], MCP_WRITE_SCOPE):
        raise ApiError("FORBIDDEN", f"缺少权限：{MCP_WRITE_SCOPE}", status_code=403)


def _new_cid() -> str:
    """逻辑采集 ID：与 HTTP 侧同格式（8–64 字符），重复投递靠它收敛。"""
    import uuid

    return "mcp" + uuid.uuid4().hex


def _checked_cid(client_capture_id: str | None) -> str | None:
    """客户端给的逻辑 ID 按 HTTP 侧同一口径校验（8–64 字符），不放过短到没意义的值。"""
    if client_capture_id is None:
        return None
    cid = str(client_capture_id).strip()
    if not cid:
        return None
    if len(cid) < 8 or len(cid) > 64:
        raise ApiError("SCHEMA_INVALID", "client_capture_id 需要 8–64 字符", status_code=422)
    return cid


def _note_with_title(title: str | None, user_note: str | None) -> str | None:
    """标题并进用户备注：Capture 的 payload 没有独立标题字段，标题由正文首行承载。"""
    if not title:
        return user_note
    return f"{user_note}\n\n（标题：{title}）" if user_note else f"标题：{title}"


def build_mcp_asgi() -> tuple[StreamableHTTPSessionManager, Any]:
    """装配 MCP server，返回（会话管理器, 挂载用的 ASGI 应用）。

    每次 create_app() 新建一份：会话管理器跟着应用生命周期走，测试里多个应用
    实例互不干扰。
    """
    settings = get_settings()
    server = MCPServer(
        name="kb-inbox",
        instructions=(
            "这是用户自己的知识库收件箱。只能读用户本人的条目、把整理结果投进"
            "用户本人的收件箱；不能改原文、不能删除条目、不会替用户确认任何发布动作。"
            " 读取用 kb_list_items / kb_search_items 定位，再用 kb_read_source 取正文；"
            + POLL_GUIDANCE
        ),
        token_verifier=AgentTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.public_base_url),
            resource_server_url=AnyHttpUrl(f"{settings.public_base_url}/mcp"),
            required_scopes=[MCP_READ_SCOPE],
            # 我们的 Token 是库里的随机摘要，不是 OAuth JWT，没有 RFC 8707 的
            # resource 声明；受众由 verify_token 自己认（只认本库 tokens 表里的行）
            validate_token_resource=False,
        ),
    )
    _register_tools(server)

    # api 容器只绑 127.0.0.1，公网流量统一走宿主机 Caddy 的整站 reverse_proxy，
    # Host 栅栏在那一层已经等价于「只有这一个域名进得来」；这里再按 127.0.0.1
    # 白名单会把 Caddy 转发进来的真实 Host 挡掉。跨站风险由 Bearer 校验挡住：
    # 没有有效 agent Token，连 initialize 都进不来。
    app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return server.session_manager, app


def _register_tools(server: MCPServer) -> None:
    """9 个工具：读收件箱、读原文与整理稿、投递笔记/草稿、提交并查询异步任务。

    全部复用 HTTP 路由抽出的实现体；参数上限是模型可见的契约。
    """
    from sqlalchemy import select

    from .routes_captures import create_capture_impl
    from .routes_items import (
        _cloud_digest, _item_out, _latest_source, _list_items_impl, _require_item,
        _source_material,
    )

    def _clip(text: str, max_chars: int) -> tuple[str, bool]:
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    def _read_limit(max_chars: int) -> int:
        try:
            value = int(max_chars)
        except (TypeError, ValueError):
            return MAX_READ_CHARS_DEFAULT
        if value <= 0:
            return MAX_READ_CHARS_DEFAULT
        return min(value, MAX_READ_CHARS_LIMIT)

    @server.tool(name="kb_list_items", description=(
        "列出用户收件箱里的条目。state 是内部流水线状态，view 是面向用户的三分组"
        "（attention|working|published）；两者都不传就是全部。返回带 item_id，"
        "后续用 kb_get_item / kb_read_source 取详情与原文。"))
    @_surface_errors
    def kb_list_items(state: str | None = None, view: str | None = None,
                      source_type: str | None = None, limit: int = 20,
                      offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        offset = max(0, int(offset))
        with get_session_factory()() as db:
            outs, total = _list_items_impl(db, _principal_user_id(), state=state, view=view,
                                            source_type=source_type, limit=limit, offset=offset)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [o.model_dump() for o in outs],
        }

    @server.tool(name="kb_search_items", description=(
        "在用户自己的收件箱里按关键词搜索（标题、原始链接、来源标签、用户备注）。"
        "服务端全库搜索，不只看已加载的一页。"))
    @_surface_errors
    def kb_search_items(query: str, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        if not (query or "").strip():
            raise ApiError("SCHEMA_INVALID", "query 不能为空", status_code=422)
        with get_session_factory()() as db:
            outs, total = _list_items_impl(db, _principal_user_id(), search=query.strip(),
                                           limit=limit, offset=0)
        return {"total": total, "limit": limit, "query": query.strip(),
                "items": [o.model_dump() for o in outs]}

    @server.tool(name="kb_get_item", description=(
        "取一条条目的详情：来源、状态、缺失材料、版本与到期时间。"
        "正文请用 kb_read_source，整理稿请用 kb_read_digest。"))
    @_surface_errors
    def kb_get_item(item_id: str) -> dict[str, Any]:
        with get_session_factory()() as db:
            item = _require_item(db, _principal_user_id(), item_id)
            return _item_out(item, _latest_source(db, item), db).model_dump()

    @server.tool(name="kb_read_source", description=(
        "读一条条目的原始正文（服务器里已归档的那一版，不重新抓取）。"
        "kind=normalized 是归档原文，kind=readable 是分了段的阅读层。"
        "返回一定带 truncated 与来源/投递版本号：被截断时不要当成读完了全文，"
        "可以调大 max_chars（上限 60000）或换一段读。"))
    @_surface_errors
    def kb_read_source(item_id: str, kind: str = "readable",
                      max_chars: int = MAX_READ_CHARS_DEFAULT) -> dict[str, Any]:
        if kind not in ("normalized", "readable"):
            raise ApiError("SCHEMA_INVALID", "kind 只能是 normalized 或 readable", status_code=422)
        limit = _read_limit(max_chars)
        user_id = _principal_user_id()
        with get_session_factory()() as db:
            item = _require_item(db, user_id, item_id)
            material = _source_material(db, item)
            if material is None:
                return {"item_id": item_id, "available": False,
                        "reason": "这条条目还没有归档正文（可能还在排队提取，或只有链接）。",
                        "source_revision": item.source_revision, "bundle_revision": item.bundle_revision}
            text = getattr(material, f"{kind}_md") or ""
            if not text:
                other = "normalized" if kind == "readable" else "readable"
                return {"item_id": item_id, "available": False, "kind": kind,
                        "reason": f"这一版没有 {kind} 正文；可以试 kind={other}。",
                        "source_revision": material.source_revision,
                        "bundle_revision": material.bundle_revision}
            body, truncated = _clip(text, limit)
            return {
                "item_id": item_id, "available": True, "kind": kind,
                "source_revision": material.source_revision,
                "bundle_revision": material.bundle_revision,
                "coverage": material.coverage,
                "missing_materials": material.missing_materials,
                # 归档侧本来就超限（超过内联阅读上限）：与本次截断分开说
                "archived_too_large": material.truncated,
                "truncated": truncated,
                "chars_returned": len(body),
                "text": body,
            }

    @server.tool(name="kb_read_digest", description=(
        "读一条条目已有的云端整理稿（只读库里已有的结果，不会现跑模型）。"
        "state 说明它到底是没有、还在排队、失败、过期，还是格式不认识——"
        "不要把 pending/failed 当成「这篇没有重点」。"))
    @_surface_errors
    def kb_read_digest(item_id: str, max_chars: int = MAX_READ_CHARS_DEFAULT) -> dict[str, Any]:
        limit = _read_limit(max_chars)
        user_id = _principal_user_id()
        with get_session_factory()() as db:
            item = _require_item(db, user_id, item_id)
            digest = _cloud_digest(db, item)
            doc = digest.content_document or {}
            parts: list[str] = []
            for section in doc.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("heading") or "").strip()
                if heading:
                    parts.append(f"## {heading}")
                for block in section.get("blocks") or []:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        kind = block.get("kind") or "paragraph"
                        parts.append(f"[{kind}] {block['text']}" if kind != "paragraph"
                                     else block["text"])
            text = "\n\n".join(parts)
            body, truncated = _clip(text, limit)
            return {
                "item_id": item_id, "state": digest.state, "state_detail": digest.state_detail,
                "source_revision": digest.source_revision, "bundle_revision": digest.bundle_revision,
                "created_at": digest.created_at, "format_version": digest.format_version,
                "completeness": digest.completeness, "stale_note": digest.stale_note,
                "legacy_converted": digest.legacy_converted,
                "unresolved_count": digest.unresolved_count,
                "truncated": truncated, "chars_returned": len(body), "text": body,
            }

    def _capture_payload(**fields: Any) -> dict:
        payload = {
            "schema_version": "1.0", "input_kind": "text", "source_hint": "unknown",
            "capture_channel": fields["capture_channel"], "include_images": False,
            "include_asr": False, "original_url": fields.get("original_url"),
            "share_text": None, "text": fields.get("text"),
            "user_note": fields.get("user_note"), "content_scope": "full",
            "upload_ids": [], "processing_intent": "default",
            "primary_audio_upload_id": None, "processing_profile_id": "profile-default",
            "archive_policy": "source_materials", "captured_at": None,
            "client_capture_id": fields["client_capture_id"],
        }
        return payload

    @server.tool(name="kb_capture_note", description=(
        "把一段文字投进用户自己的收件箱（等同用户在网页里贴一段文字收藏）。"
        "这是写入用户材料的动作：只投用户明确要求保存的内容。"
        "同一 idempotency_key 重复调用不会建两条。"))
    @_surface_errors
    def kb_capture_note(text: str, title: str | None = None, user_note: str | None = None,
                        original_url: str | None = None, client_capture_id: str | None = None,
                        idempotency_key: str | None = None) -> dict[str, Any]:
        _require_write_scope()
        if not (text or "").strip():
            raise ApiError("SCHEMA_INVALID", "text 不能为空", status_code=422)
        body = text if not title else f"# {title.strip()}\n\n{text}"
        cid = _checked_cid(client_capture_id) or f"mcp-{idempotency_key or _new_cid()}"
        cid = cid[:64]
        payload = _capture_payload(capture_channel="agent_note", text=body,
                                   user_note=_note_with_title(title, user_note),
                                   original_url=original_url, client_capture_id=cid)
        with get_session_factory()() as db:
            result = create_capture_impl(db, user_id=_principal_user_id(), payload=payload,
                                         idempotency_key=idempotency_key or cid)
            db.commit()
        return {**result, "kind": "note"}

    @server.tool(name="kb_draft_note", description=(
        "把一份 AI 整理出来的 Markdown 草稿投进用户收件箱，并记下它依据的是哪几条原文。"
        "草稿不是成品：用户会自己决定是否留下；来源条目只写用户库里真实存在的 ID，"
        "不要凭记忆编标题或链接。同一 idempotency_key 重复调用不会建两份。"))
    @_surface_errors
    def kb_draft_note(title: str, markdown: str, source_item_ids: list[str] | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        _require_write_scope()
        if not (markdown or "").strip():
            raise ApiError("SCHEMA_INVALID", "markdown 不能为空", status_code=422)
        user_id = _principal_user_id()
        ids = [str(i) for i in (source_item_ids or [])][:50]
        sources: list[dict[str, Any]] = []
        with get_session_factory()() as db:
            for one in ids:
                try:
                    item = _require_item(db, user_id, one)
                except ApiError:
                    # 不属于这个用户或已删除：如实丢掉，不把别人的条目引进草稿
                    continue
                source = _latest_source(db, item)
                meta = source.metadata_json or {}
                sources.append({"item_id": item.id, "title": meta.get("title"),
                                "url": meta.get("original_url"),
                                "source_revision": item.source_revision})
            body = markdown if not title else f"# {title.strip()}\n\n{markdown}"
            if sources:
                lines = "\n".join(
                    f"- {s['title'] or s['item_id']}（{s['url'] or '无链接'}，"
                    f"来源版本 r{s['source_revision']}，item_id={s['item_id']}）"
                    for s in sources)
                body = f"{body}\n\n## 这份草稿依据的原文\n\n{lines}\n"
            cid = f"mcp-draft-{idempotency_key or _new_cid()}"[:64]
            payload = _capture_payload(capture_channel="agent_draft", text=body,
                                       user_note=title or "AI 草稿",
                                       original_url=None, client_capture_id=cid)
            result = create_capture_impl(db, user_id=user_id, payload=payload,
                                         idempotency_key=idempotency_key or cid)
            db.commit()
        return {**result, "kind": "draft", "sources": sources,
                "note": "草稿已进入收件箱，等待用户自己确认；服务器不会替用户发布到 Vault。"}

    @server.tool(name="kb_submit_job", description=(
        "提交一个需要等一会儿的服务器任务，立刻返回 job_id（工具调用有 60 秒上限，"
        "任何要跑模型的活都走这里）。kind：reprocess_item=用这条条目已有的材料重新"
        "整理一遍；optimize_text=只做纠错与分段，不生成知识笔记。"
        "提交后用 kb_get_job 轮询：" + POLL_GUIDANCE))
    @_surface_errors
    def kb_submit_job(kind: str, item_id: str | None = None,
                      params: dict[str, Any] | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        _require_write_scope()
        from ..domain.agent_tasks import submit_agent_task

        with get_session_factory()() as db:
            task = submit_agent_task(db, user_id=_principal_user_id(), site="kb", kind=kind,
                                     item_id=item_id, params=params or {},
                                     idempotency_key=idempotency_key or _new_cid())
            db.commit()
            return {"job_id": task.id, "kind": task.kind, "state": task.state,
                    "item_id": task.item_id,
                    "poll": "用 kb_get_job 传这个 job_id 轮询；间隔不少于 5 秒，最多 20 次"}

    @server.tool(name="kb_get_job", description=(
        "查一个已提交任务的状态、结果或失败原因。state：queued/running/retry_wait/"
        "succeeded/failed/cancelled/unknown_outcome。unknown_outcome 表示上一次执行"
        "中断且结果不确定——不要重复提交同一个任务，如实告诉用户。"))
    @_surface_errors
    def kb_get_job(job_id: str) -> dict[str, Any]:
        from ..models import AgentTask

        user_id = _principal_user_id()
        with get_session_factory()() as db:
            task = db.scalar(select(AgentTask).where(AgentTask.id == job_id,
                                                     AgentTask.user_id == user_id))
            if task is None:
                # 别人的 job_id 与不存在的 job_id 一律同一个答案
                raise ApiError("NOT_FOUND", "任务不存在", status_code=404)
            return {"job_id": task.id, "kind": task.kind, "state": task.state,
                    "item_id": task.item_id, "attempt": task.attempt,
                    "result": task.result_json, "error": task.last_error,
                    "created_at": task.created_at.isoformat()}

