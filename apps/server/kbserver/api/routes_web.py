"""Web 收件箱页面入口（docs/02 §2.1、docs/05 §4.1）。

登录改走中心会话：GET /inbox 由页面脚本探测 /v1/auth/me，
未登录跳转 GET /login（302 到中心登录页，return_to 指回 KB）。
旧「配对码换 kb_session」通道已关闭（docs/05 §4.5）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["web-inbox"])

WEB_STATIC_DIR = Path(__file__).resolve().parents[1] / "web_static"

# 页面本身带登录态：no-store 让 Chromium 不把整份文档留进前进/后退缓存，
# 否则「退出 → 点登录 → 返回」会直接恢复退出前那份登录态 DOM（不发请求、不重跑启动检查）。
PAGE_HEADERS = {"Cache-Control": "no-store"}


@router.get("/inbox", include_in_schema=False)
def inbox_page() -> FileResponse:
    return FileResponse(WEB_STATIC_DIR / "inbox.html", media_type="text/html",
                        headers=PAGE_HEADERS)


@router.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """管理员页面：页面脚本探测 /v1/auth/me，非管理员就地展示「无权访问」。"""
    return FileResponse(WEB_STATIC_DIR / "admin.html", media_type="text/html",
                        headers=PAGE_HEADERS)


@router.get("/authorize", include_in_schema=False)
def authorize_page() -> FileResponse:
    """插件设备授权页：浏览器（中心会话）批准插件领取设备 Token。"""
    return FileResponse(WEB_STATIC_DIR / "authorize.html", media_type="text/html",
                        headers=PAGE_HEADERS)


# 共享设计令牌（docs/09 §4.6）：两页 <link> 引入，白名单避免路径穿越
@router.get("/tokens.css", include_in_schema=False)
def tokens_css() -> FileResponse:
    return FileResponse(WEB_STATIC_DIR / "tokens.css", media_type="text/css",
                        headers={"Cache-Control": "no-cache"})


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/inbox", status_code=307)
