"""B 站登录态托管（docs/04 §5，2026-09-07 实测决策：按用户托管最小凭据 SESSDATA）。

- 复用模型 Key 的凭据架构：ProviderProfile(kind=bilibili_session) + Credential
  信封加密（AAD 绑定 user/profile/version），明文只进不出。
- 通用生命周期（保存/检测/撤销/定向重排队）已抽到 domain/platform_sessions.py
  （docs/18 §7.2），本模块只保留 B 站特有的 SESSDATA 清洗与 nav 接口检测；
  接口路径、公开函数签名与响应语义保持兼容（routes_admin 仍在复用）。
- 最小凭据：只保存 SESSDATA 值（实测仅 SESSDATA 即可取得 AI 字幕轨，
  整串 Cookie 反而拿不到可下载 URL）。允许粘贴 "SESSDATA=…" 或完整 Cookie，
  服务端只提取 SESSDATA 值，其余字段丢弃、不落任何存储。
- 更新/新增后，needs_input 条目自动重新提取（此前因无登录态降级的条目自动续跑）。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import require_scope
from ..domain import platform_sessions
from ..domain.errors import ApiError
from ..domain.platform_sessions import SPECS
from ..models import ProviderProfile
from ..security.safe_fetch import SafeFetchError, safe_fetch

router = APIRouter(tags=["bilibili-session"])

SPEC = SPECS["bilibili"]


def _extract_sessdata(raw: str) -> str:
    """接受裸 SESSDATA 值、`SESSDATA=…` 或完整 Cookie 串，只提取 SESSDATA 值。"""
    value = (raw or "").strip()
    if not value:
        raise ApiError("SCHEMA_INVALID", "SESSDATA 不能为空")
    if "=" in value:
        for pair in value.split(";"):
            pair = pair.strip()
            if pair.lower().startswith("sessdata="):
                value = pair.split("=", 1)[1].strip()
                break
    value = value.strip().strip('"')
    if not value or "=" in value or ";" in value or " " in value:
        raise ApiError("SCHEMA_INVALID", "未能从输入中提取出合法的 SESSDATA 值；请只提交 SESSDATA 的值")
    if len(value) < 20 or len(value) > 512:
        raise ApiError("SCHEMA_INVALID", "SESSDATA 长度异常（应为几十到一百多字符）")
    if not re.match(r"^[A-Za-z0-9%,*_\-./+]+$", value):
        raise ApiError("SCHEMA_INVALID", "SESSDATA 含非法字符")
    return value


# 供 routes_admin 复用的展示/生命周期薄委托：查询走通用会话服务，行为不变


def _session_profile(db: Session, user_id: str) -> ProviderProfile | None:
    return platform_sessions.session_profile(db, user_id, SPEC)


def _active_credential(db: Session, profile: ProviderProfile):
    return platform_sessions.active_credential(db, profile)


def _out(db: Session, profile: ProviderProfile | None) -> dict:
    return platform_sessions.session_status(db, profile.user_id if profile else "", SPEC, profile)


def _check_sessdata_online(sessdata: str, max_bytes: int) -> dict:
    """用当前凭据请求 B 站 nav 接口验证登录态；只返回脱敏状态，不触发重抓。"""
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        from ..extractors.bilibili import _browser_headers

        result = safe_fetch(url, max_bytes=max_bytes, timeout=15.0,
                            headers=_browser_headers(url, sessdata))
    except SafeFetchError as exc:
        return {"status": "network_error", "detail": f"检测请求失败：{exc}"}
    if result.status_code >= 500:
        return {"status": "network_error", "detail": f"平台临时错误（HTTP {result.status_code}）"}
    try:
        import json as _json

        doc = _json.loads(result.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"status": "blocked", "detail": "接口返回内容异常（可能被风控）"}
    code = doc.get("code")
    if code == 0:
        uname = str(((doc.get("data") or {}).get("uname")) or "")
        masked = (uname[:1] + "***") if uname else ""
        return {"status": "valid",
                "detail": f"登录态有效{('（B 站用户 ' + masked + '）') if masked else ''}"}
    if code in (-101, -111):
        return {"status": "invalid", "detail": "B 站返回未登录：凭据已失效或被拒绝，请重新提交 SESSDATA"}
    return {"status": "blocked", "detail": f"平台拒绝访问（code={code}）"}


def _requeue_needs_input(db: Session, user_id: str) -> int:
    """登录态变化：仅重提「B 站来源 + 因字幕/登录态问题等待」的条目（docs/05 §3.2）。"""
    return platform_sessions.requeue_waiting_items(db, user_id, SPEC)


class SessionSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(min_length=16, max_length=8192)


@router.get("/v1/bilibili-session")
def get_session(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    return _out(db, _session_profile(db, user.id))


def upsert_session_for_user(db: Session, user_id: str, raw_secret: str) -> dict:
    """把清洗后的 SESSDATA 写入目标用户的 bilibili_session 配置（本人或管理员代配）。"""
    sessdata = _extract_sessdata(raw_secret)
    out, requeued = platform_sessions.store_session(db, user_id, SPEC, sessdata)
    out["requeued_items"] = requeued
    return out


def revoke_session_for_user(db: Session, user_id: str) -> dict:
    return platform_sessions.revoke_session(db, user_id, SPEC)


def test_session_for_user(db: Session, user_id: str) -> dict:
    """检测目标用户的 B 站登录态；结果写入 profile.meta_json（脱敏）。"""

    def _nav_check(value) -> dict:
        return _check_sessdata_online(value, get_settings().subtitle_download_limit)

    return platform_sessions.test_session(db, user_id, SPEC, _nav_check)


@router.put("/v1/bilibili-session")
def put_session(body: SessionSecret, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    return upsert_session_for_user(db, principal.user.id, body.secret)


@router.delete("/v1/bilibili-session")
def delete_session(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    return revoke_session_for_user(db, principal.user.id)


@router.post("/v1/bilibili-session/test")
def test_session(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    """检测当前用户的 B 站登录态是否有效（docs/05 §3.3）。"""
    return test_session_for_user(db, principal.user.id)
