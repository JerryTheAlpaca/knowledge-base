"""B 站登录态托管（docs/04 §5，2026-09-07 实测决策：按用户托管最小凭据 SESSDATA）。

- 复用模型 Key 的凭据架构：ProviderProfile(kind=bilibili_session) + Credential
  信封加密（AAD 绑定 user/profile/version），明文只进不出。
- 最小凭据：只保存 SESSDATA 值（实测仅 SESSDATA 即可取得 AI 字幕轨，
  整串 Cookie 反而拿不到可下载 URL）。允许粘贴 "SESSDATA=…" 或完整 Cookie，
  服务端只提取 SESSDATA 值，其余字段丢弃、不落任何存储。
- 更新/新增后，needs_input 条目自动重新提取（此前因无登录态降级的条目自动续跑）。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import require_scope
from ..domain import pipeline
from ..domain.errors import ApiError
from ..models import Capture, Credential, Item, ProviderProfile, SourceRevision, new_id, utcnow
from ..security import credentials as cred_crypto
from ..security.safe_fetch import SafeFetchError, safe_fetch

router = APIRouter(tags=["bilibili-session"])

SESSION_KIND = "bilibili_session"
SESSION_ADAPTER = "bilibili-web"
SESSION_ENDPOINT = "https://api.bilibili.com"
SESSION_MODEL = "sessdata"


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


def _session_profile(db: Session, user_id: str) -> ProviderProfile | None:
    return (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_id, ProviderProfile.kind == SESSION_KIND)
        .order_by(ProviderProfile.created_at)
        .first()
    )


def _active_credential(db: Session, profile: ProviderProfile) -> Credential | None:
    return (
        db.query(Credential)
        .filter(Credential.profile_id == profile.id, Credential.revoked_at.is_(None))
        .order_by(Credential.created_at.desc())
        .first()
    )


def _out(db: Session, profile: ProviderProfile | None) -> dict:
    if profile is None:
        return {
            "configured": False,
            "credential_version": None,
            "updated_at": None,
            "verification": "unconfigured",
            "last_check": None,
            "note": "未托管 B 站登录态：需要登录才能取得字幕的视频将进入补充材料。",
        }
    cred = _active_credential(db, profile)
    last_check = (profile.meta_json or {}).get("bilibili_last_check")
    if last_check:
        verification = last_check.get("status") or "unverified"
    else:
        verification = "unverified"  # 已保存但未验证
    return {
        "configured": cred is not None,
        "credential_version": cred.version if cred else None,
        "updated_at": cred.created_at.isoformat() if cred else None,
        "verification": verification,
        "last_check": last_check,
        "note": "只保存 SESSDATA 值并加密存储；任何接口不返回明文。失效时更新即可。"
                if cred is not None else "凭据已撤销。",
    }


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


def _is_bilibili_item(db: Session, item: Item) -> bool:
    """条目是否属于 B 站来源：按最新来源版本元数据或采集输入判断。"""
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == item.source_revision)
        .one_or_none()
    )
    meta = source.metadata_json if source else {}
    if meta.get("platform") == "bilibili":
        return True
    capture = db.get(Capture, item.capture_id)
    payload = (capture.input_json or {}) if capture else {}
    candidates = [payload.get("original_url")]
    m = re.search(r"https?://[^\s，,、）)】\]]+", payload.get("share_text") or "")
    if m:
        candidates.append(m.group(0))
    for url in candidates:
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if host == "bilibili.com" or host.endswith(".bilibili.com") or host.endswith("b23.tv"):
            return True
    return False


def _requeue_needs_input(db: Session, user_id: str) -> int:
    """登录态变化：仅重提「B 站来源 + 因字幕/登录态问题等待」的条目（docs/05 §3.2）。

    不重抓公众号/网页/其他来源；已有用户补充正文的条目跳过，避免覆盖人工补充。
    """
    items = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.pipeline_state == "needs_input", Item.deleted_at.is_(None))
        .all()
    )
    requeued = 0
    for it in items:
        if not _is_bilibili_item(db, it):
            continue
        source = (
            db.query(SourceRevision)
            .filter(SourceRevision.item_id == it.id, SourceRevision.revision == it.source_revision)
            .one_or_none()
        )
        meta = source.metadata_json if source else {}
        if (meta.get("supplement_text") or "").strip():
            continue  # 已有人工补充正文，不覆盖
        detail = it.state_detail or ""
        if not any(k in detail for k in ("字幕", "登录", "SESSDATA", "凭据")):
            continue  # 非字幕/会话原因的待补充条目不重排
        pipeline.enqueue_stage(
            db, user_id=user_id, item_id=it.id, source_revision=it.source_revision,
            stage="extract", reset_attempt=True,
        )
        requeued += 1
    return requeued


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

    profile = _session_profile(db, user_id)
    if profile is None:
        profile = ProviderProfile(
            user_id=user_id, kind=SESSION_KIND, adapter=SESSION_ADAPTER,
            endpoint=SESSION_ENDPOINT, model=SESSION_MODEL,
            capabilities_json={}, version=1,
        )
        db.add(profile)
        db.flush()

    current = _active_credential(db, profile)
    next_version = (current.version if current else 0) + 1
    if current:
        current.revoked_at = utcnow()
    encrypted = cred_crypto.encrypt_secret(
        sessdata, get_settings().load_master_key(),
        user_id=user_id, profile_id=profile.id, credential_version=next_version,
    )
    db.add(Credential(user_id=user_id, profile_id=profile.id, version=next_version,
                      master_key_version=get_settings().master_key_version, **encrypted))

    requeued = _requeue_needs_input(db, user_id)
    db.commit()
    db.refresh(profile)
    out = _out(db, profile)
    out["requeued_items"] = requeued
    pipeline.emit_event(db, user_id, item_id=None, bundle_revision=None,
                        event_type="bilibili_session_updated",
                        payload={"credential_version": next_version, "requeued": requeued})
    db.commit()
    return out


def revoke_session_for_user(db: Session, user_id: str) -> dict:
    profile = _session_profile(db, user_id)
    if profile is None:
        return {"revoked": True, "note": "本来就没有托管登录态。"}
    now = utcnow()
    revoked = 0
    for cred in db.query(Credential).filter(
        Credential.profile_id == profile.id, Credential.revoked_at.is_(None)
    ).all():
        cred.revoked_at = now
        revoked += 1
    db.commit()
    return {"revoked": True, "revoked_versions": revoked,
            "note": "后续提取回到匿名路径；需要登录的视频将进入补充材料。"}


def test_session_for_user(db: Session, user_id: str) -> dict:
    """检测目标用户的 B 站登录态；结果写入 profile.meta_json（脱敏）。"""
    profile = _session_profile(db, user_id)
    cred = _active_credential(db, profile) if profile else None
    if cred is None:
        raise ApiError("SCHEMA_INVALID", "尚未托管 B 站登录态", status_code=422)
    try:
        sessdata = cred_crypto.decrypt_secret(
            cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
            get_settings().load_master_key(),
            user_id=user_id, profile_id=profile.id, credential_version=cred.version,
        )
    except Exception as exc:
        check = {"status": "invalid", "detail": f"凭据解密失败（{type(exc).__name__}）；请重新提交 SESSDATA",
                 "checked_at": utcnow().isoformat()}
        profile.meta_json = {**(profile.meta_json or {}), "bilibili_last_check": check}
        db.commit()
        return check
    check = _check_sessdata_online(sessdata, get_settings().subtitle_download_limit)
    check["checked_at"] = utcnow().isoformat()
    profile.meta_json = {**(profile.meta_json or {}), "bilibili_last_check": check}
    db.commit()
    return check


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
