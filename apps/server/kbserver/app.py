"""FastAPI 应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.routing import Match

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import (mcp_server, routes_admin, routes_asr, routes_audio_uploads, routes_auth,
                  routes_bilibili, routes_captures, routes_devices, routes_health,
                  routes_items, routes_llm_proxy, routes_onboarding, routes_agent_tokens,
                  routes_agent_relay, routes_platform_sessions, routes_plugin,
                  routes_profiles, routes_share_public, routes_shares, routes_sync,
                  routes_uploads, routes_web)
from .api.deps import CSRF_COOKIE
from .config import get_settings
from .domain.errors import ApiError, status_for
from .models import new_id
from .security import central_auth
from .security.tokens import new_service_token


class ExactPathAsgi:
    """把不带尾斜杠的规范路径直接交给子应用。

    Starlette 的 Mount 把 `/mcp` 判成 partial match 并 307 到 `/mcp/`。用静态
    Bearer 头的客户端（dsh 的 mcp-client）按文档配的正是 `/mcp`：多一跳重定向
    没有收益，而代理链上任何一环在跳转时丢掉 Authorization 头，表现就是一个很难查
    的 401。`/mcp/...` 这类带子路径的请求继续走 Mount。
    """

    def __init__(self, path: str, app) -> None:
        self.path = path
        self.app = app

    def matches(self, scope: dict) -> tuple[Match, dict]:
        if (scope.get("path") or "").rstrip("/") == self.path:
            return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope, receive, send) -> None:
        await self.app(dict(scope, path="/", root_path=""), receive, send)

    async def __call__(self, scope, receive, send) -> None:  # Starlette 遍历路由表用
        await self.handle(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    # MCP server 只在启用 Agent 接入时装配：会话管理器要跟着应用生命周期走，
    # 关闭时既不起管理器也不挂载 /mcp（docs/27 §Phase 0a）
    mcp_manager, mcp_asgi = mcp_server.build_mcp_asgi() if settings.agent_enabled else (None, None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # StreamableHTTP 的 task group 必须由宿主应用的 lifespan 起：挂载子应用时
        # Starlette 不会代跑它的 lifespan，起了不了就等于 /mcp 每个请求都报错。
        if mcp_manager is None:
            yield
            return
        async with mcp_manager.run():
            yield

    app = FastAPI(
        title="Knowledge Inbox Server",
        version="0.2.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        # 用户语言契约（docs/17 §10.3）：user_message 受控生成，不透传异常原文；
        # code 供程序分支，前端降级文案兜底非协议错误
        return JSONResponse(
            status_code=exc.status_code or status_for(exc.code),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "user_message": exc.message,
                    "action": exc.action,
                    "retryable": exc.retryable,
                    "trace_id": getattr(request.state, "request_id", None) or new_id(),
                    "request_id": getattr(request.state, "request_id", None) or new_id(),
                    "details": exc.details,
                }
            },
        )

    @app.middleware("http")
    async def auth_cookie_relay(request: Request, call_next):
        """中心会话续期/CSRF Cookie 转发（docs/05 §4.1 第 5 条）。

        认证依赖把中心续期 Cookie 暂存在 request.state.central_renewal，
        需要补发的 kb_csrf 暂存在 request.state.kb_csrf_issue；
        这里统一写回浏览器。续期 Cookie 的名称/域/路径已在认证层校验过。

        写回之前再查一次撤销表：退出前已发出的请求（中心校验要一个公网 RTT）
        可能晚于 logout 落地，不查就会把刚 delete_cookie 掉的那颗凭据又按父域
        种回浏览器——KB 侧有 5 分钟撤销表兜着，同域的其他应用没有（审查 C-08）。
        """
        response = await call_next(request)
        renewal = getattr(request.state, "central_renewal", None)
        if renewal is not None:
            settings = get_settings()
            presented = request.cookies.get(settings.auth_cookie_name) or ""
            revoked = central_auth.is_revoked(renewal["value"]) or (
                bool(presented) and central_auth.is_revoked(presented))
            if not revoked:
                kwargs = {"max_age": renewal["max_age"], "secure": renewal["secure"],
                          "httponly": True, "samesite": "lax", "path": "/"}
                if renewal.get("domain"):
                    kwargs["domain"] = renewal["domain"]
                response.set_cookie(settings.auth_cookie_name, renewal["value"], **kwargs)
        csrf_issue = getattr(request.state, "kb_csrf_issue", None)
        if csrf_issue:
            secure = get_settings().public_base_url.startswith("https://")
            response.set_cookie(CSRF_COOKIE, csrf_issue, httponly=False,
                                samesite="lax", secure=secure, path="/")
        return response

    app.include_router(routes_health.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_uploads.router)
    app.include_router(routes_audio_uploads.router)
    app.include_router(routes_captures.router)
    app.include_router(routes_items.router)
    app.include_router(routes_sync.router)
    app.include_router(routes_devices.router)
    app.include_router(routes_profiles.router)
    app.include_router(routes_bilibili.router)
    app.include_router(routes_platform_sessions.router)
    app.include_router(routes_asr.router)
    app.include_router(routes_onboarding.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_shares.router)
    app.include_router(routes_share_public.router)
    app.include_router(routes_llm_proxy.router)
    app.include_router(routes_agent_tokens.router)
    app.include_router(routes_agent_relay.router)
    app.include_router(routes_plugin.router)
    app.include_router(routes_web.router)

    # Web 前端模块（docs/17 §11 ES modules）：/webstatic/js/app.js 等；
    # 路由在前、挂载在后，/inbox、/tokens.css 等显式路由优先
    import mimetypes

    from fastapi.staticfiles import StaticFiles

    # 内嵌展示字：mimetypes 表里没有 .woff2，不补会按 application/octet-stream 发出去
    mimetypes.add_type("font/woff2", ".woff2")

    from .api.routes_web import WEB_STATIC_DIR

    class RevalidateStaticFiles(StaticFiles):
        """部署后浏览器立刻拿到新前端：强制 revalidate（未变走 304，开销极小）。
        否则启发式缓存会拿旧 JS 配新 HTML，出现「按钮点了没反应」这类错位。"""

        async def get_response(self, path: str, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/webstatic", RevalidateStaticFiles(directory=WEB_STATIC_DIR), name="webstatic")
    if mcp_asgi is not None:
        # 路由在前、挂载在后：/mcp 交给 MCP SDK 自己的 ASGI 应用，
        # 认证由它的 token_verifier 中间件负责（不走 current_principal，
        # 也不接受浏览器 Cookie）
        app.router.routes.append(ExactPathAsgi("/mcp", mcp_asgi))
        app.mount("/mcp", mcp_asgi, name="mcp")
    return app


app = create_app()
