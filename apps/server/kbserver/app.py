"""FastAPI 应用装配。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import (routes_asr, routes_audio_uploads, routes_auth, routes_bilibili,
                  routes_captures, routes_devices, routes_health, routes_items,
                  routes_profiles, routes_sync, routes_uploads, routes_web)
from .api.deps import CSRF_COOKIE
from .config import get_settings
from .domain.errors import ApiError, status_for
from .models import new_id
from .security.tokens import new_service_token


def create_app() -> FastAPI:
    app = FastAPI(
        title="Knowledge Inbox Server",
        version="0.2.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code or status_for(exc.code),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
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
        """
        response = await call_next(request)
        renewal = getattr(request.state, "central_renewal", None)
        if renewal is not None:
            kwargs = {"max_age": renewal["max_age"], "secure": renewal["secure"],
                      "httponly": True, "samesite": "lax", "path": "/"}
            if renewal.get("domain"):
                kwargs["domain"] = renewal["domain"]
            response.set_cookie(get_settings().auth_cookie_name, renewal["value"], **kwargs)
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
    app.include_router(routes_asr.router)
    app.include_router(routes_web.router)
    return app


app = create_app()
