"""FastAPI 应用装配。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import (routes_bilibili, routes_captures, routes_devices, routes_health,
                  routes_items, routes_pairing, routes_profiles, routes_sync,
                  routes_uploads, routes_web)
from .domain.errors import ApiError, status_for
from .models import new_id


def create_app() -> FastAPI:
    app = FastAPI(
        title="Knowledge Inbox Server",
        version="0.1.0",
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

    app.include_router(routes_health.router)
    app.include_router(routes_pairing.router)
    app.include_router(routes_uploads.router)
    app.include_router(routes_captures.router)
    app.include_router(routes_items.router)
    app.include_router(routes_sync.router)
    app.include_router(routes_devices.router)
    app.include_router(routes_profiles.router)
    app.include_router(routes_bilibili.router)
    app.include_router(routes_web.router)
    return app


app = create_app()
