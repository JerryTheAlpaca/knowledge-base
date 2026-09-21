"""分享站点的公开与短时预览只读路由（docs/20 §9.4、§9.5、§12）。

- 只暴露 /s/{token} 与 /preview/{token}：不代理 /v1、中心登录中继或通用对象存储。
- SHARE_ENABLED=false 时两种页面都不交付，与创建/发布侧共用同一个总开关。
- 每次请求都核验令牌、发布状态与有效期；无效／撤销／到期统一返回不泄露标题的不可用页面。
- 响应不种 Cookie、不转发传入 Cookie，禁用或脱敏这类路径的访问日志。
- 即使使用独立域名，页面本身仍是可信外层 + sandbox iframe，域名不替代页面隔离。
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..models import ShareRevision, ShareWork, utcnow
from ..security import share_tokens
from ..storage.objects import ObjectStore

router = APIRouter(tags=["share-public"])

UNAVAILABLE_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>链接不可用</title>
<style>body{font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
background:#f7f4ee;color:#211d17;display:grid;place-items:center;min-height:90vh;margin:0}
main{max-width:34rem;padding:2rem;text-align:center}h1{font-size:1.15rem;font-weight:600}
p{color:#6b6157;font-size:.92rem}</style>
<main><h1>这个链接已经不可用</h1>
<p>链接可能已被撤销、过期，或对应的作品已被删除。</p></main></html>"""


def _settings() -> Settings:
    return get_settings()


def _host_allowed(request: Request, settings: Settings) -> bool:
    """独立站点由反代限定域名；这里再兜一层，避免主站域名直接执行分享路由。"""
    base = settings.share_public_base_url.strip()
    if not base:
        return False
    expected = (urlparse(base).hostname or "").lower()
    actual = (request.url.hostname or "").lower()
    return bool(expected) and (actual == expected or actual.endswith(f".{expected}"))


def _deliver(revision: ShareRevision) -> HTMLResponse:
    html = ObjectStore().read_object(revision.html_key).decode("utf-8")
    return HTMLResponse(
        content=html,
        headers={
            # 首版公开 HTML 也 no-store：链接本身就是访问凭据，不留中间副本
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        },
    )


def _unavailable() -> HTMLResponse:
    return HTMLResponse(content=UNAVAILABLE_HTML, status_code=404, headers={
        "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
    })


def _published_revision(db: Session, token_hash: str) -> ShareRevision | None:
    work = db.query(ShareWork).filter(ShareWork.share_token_hash == token_hash).one_or_none()
    if work is None or work.deleted_at is not None or work.share_status != "published":
        return None
    if work.share_expires_at is not None and work.share_expires_at < utcnow():
        return None
    if not work.published_revision_id:
        return None
    revision = db.get(ShareRevision, work.published_revision_id)
    if revision is None or revision.user_id != work.user_id:
        return None
    return revision


@router.get("/s/{token}")
def public_view(token: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = _settings()
    if not settings.share_enabled or not _host_allowed(request, settings):
        return _unavailable()
    revision = _published_revision(db, share_tokens.hash_share_token(token))
    return _deliver(revision) if revision else _unavailable()


@router.get("/preview/{token}")
def preview_view(token: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """作者本人的短时预览：不公开列出，凭据有效即可取指定版本。"""
    settings = _settings()
    if not settings.share_enabled or not _host_allowed(request, settings):
        return _unavailable()
    payload = share_tokens.read_preview_token(settings.load_master_key(), token)
    if not payload:
        return _unavailable()
    work = db.get(ShareWork, payload.get("work_id", ""))
    if work is None or work.deleted_at is not None:
        return _unavailable()  # 删除作品立即使此前预览链接失效
    revision = db.get(ShareRevision, payload.get("rev", ""))
    if revision is None or revision.work_id != work.id:
        return _unavailable()
    return _deliver(revision)
