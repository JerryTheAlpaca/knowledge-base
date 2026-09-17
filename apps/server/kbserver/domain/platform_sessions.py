"""每用户平台登录态托管（docs/18 §7.2）。

- 平台会话 = ProviderProfile(kind=<platform>_session) + Credential 信封加密；
  AAD 绑定 user_id/profile_id/version，与模型 Key 凭据同一套加密（不改 AAD
  purpose，否则历史凭据无法解密）。
- 从 B 站路由抽出的通用生命周期：保存（新版本替换+撤销旧版）、脱敏检测、
  撤销、按登录原因定向重排队；B 站 /v1/bilibili-session 保持行为兼容。
- 明文只在读取服务内短暂存在：不写日志、不落库、不进事件/Bundle/响应。
- Cookie 只保留本平台确实需要的最小字段（docs/18 §7.2 规则 1）；最小集合
  待 P0 真实样本确认后收紧，此前保留解析出的全部字段并如实标注。
- 适配器不直接查询 Credential 表：一律经 session_value 读取短生命周期内存值。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.errors import ApiError
from ..models import Capture, Credential, Item, ProviderProfile, SourceRevision, utcnow
from ..security import credentials as cred_crypto
from . import pipeline


@dataclass(frozen=True)
class PlatformSessionSpec:
    """一个平台的登录态规格：存储模型、清洗规则、检测与重排队语义。"""

    platform: str
    kind: str                    # ProviderProfile.kind
    label: str                   # 用户可见平台名
    adapter: str
    endpoint: str                # 会话归属站点（仅记录，不用于发送）
    model: str                   # 凭据存储模型标记：sessdata | cookie
    cookie_mode: str             # single_value | cookie_dict
    url_domains: tuple[str, ...]     # 条目平台归属匹配（定向重排队用）
    cookie_send_domains: tuple[str, ...]  # 登录态只允许发往的精确一方域名（docs/18 §7.2 规则 4）
    probe_url: str               # 只读会话检测地址
    requeue_keywords: tuple[str, ...]  # state_detail 命中才重排队（登录原因等待）
    allowed_cookie_names: frozenset[str] | None  # P0 实测后收紧；None=暂保留全部解析字段
    max_secret_bytes: int
    event_type: str
    status_notes: dict[str, str] = field(default_factory=dict)
    revoke_notes: dict[str, str] = field(default_factory=dict)
    not_configured_error: str = ""  # 未托管时发起检测的错误文案
    decrypt_error: str = ""
    stored_format_error: str = ""


_STATUS_NOTE_TPL = {
    "unconfigured": "未托管{label}登录态：需要登录才能读取的内容将进入补充材料。",
    "configured": "已加密保存{mode}；任何接口不返回明文。失效时更新即可。",
    "revoked": "凭据已撤销。",
}
_REVOKE_NOTE_TPL = {
    "absent": "本来就没有托管{label}登录态。",
    "revoked": "后续提取回到匿名路径；需要登录的内容将进入补充材料。",
}

SPECS: dict[str, PlatformSessionSpec] = {
    "bilibili": PlatformSessionSpec(
        platform="bilibili", kind="bilibili_session", label="B 站",
        adapter="bilibili-web", endpoint="https://api.bilibili.com",
        model="sessdata", cookie_mode="single_value",
        url_domains=("bilibili.com", "b23.tv"),
        cookie_send_domains=("api.bilibili.com", "www.bilibili.com"),
        probe_url="https://api.bilibili.com/x/web-interface/nav",
        requeue_keywords=("字幕", "登录", "SESSDATA", "凭据"),
        allowed_cookie_names=None,  # B 站只存 SESSDATA 单值，不走 Cookie 清洗
        max_secret_bytes=8192,
        event_type="bilibili_session_updated",
        status_notes={
            "unconfigured": "未托管 B 站登录态：需要登录才能取得字幕的视频将进入补充材料。",
            "configured": "只保存 SESSDATA 值并加密存储；任何接口不返回明文。失效时更新即可。",
            "revoked": _STATUS_NOTE_TPL["revoked"],
        },
        revoke_notes={
            "absent": "本来就没有托管登录态。",
            "revoked": "后续提取回到匿名路径；需要登录的视频将进入补充材料。",
        },
        not_configured_error="尚未托管 B 站登录态",
        decrypt_error="B 站登录凭据解密失败（{exc_type}）；请重新提交 SESSDATA。",
        stored_format_error="B 站登录凭据内容异常；请重新提交 SESSDATA。",
    ),
    "xiaohongshu": PlatformSessionSpec(
        platform="xiaohongshu", kind="xiaohongshu_session", label="小红书",
        adapter="xiaohongshu-web", endpoint="https://www.xiaohongshu.com",
        model="cookie", cookie_mode="cookie_dict",
        url_domains=("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
        cookie_send_domains=("www.xiaohongshu.com", "xiaohongshu.com"),  # P0 实测后收紧
        probe_url="https://www.xiaohongshu.com/explore",
        requeue_keywords=("登录", "凭据", "会话", "字幕"),
        allowed_cookie_names=None,  # docs/18 §7.2 规则 1：P0 实测最小集合后收紧
        max_secret_bytes=8192,
        event_type="platform_session_updated",
        status_notes={
            "unconfigured": _STATUS_NOTE_TPL["unconfigured"].format(label="小红书"),
            "configured": _STATUS_NOTE_TPL["configured"].format(label="小红书", mode="Cookie"),
            "revoked": _STATUS_NOTE_TPL["revoked"],
        },
        revoke_notes={
            "absent": _REVOKE_NOTE_TPL["absent"].format(label="小红书"),
            "revoked": _REVOKE_NOTE_TPL["revoked"],
        },
        not_configured_error="尚未托管小红书登录态",
        decrypt_error="小红书登录凭据解密失败（{exc_type}）；请重新提交 Cookie。",
        stored_format_error="小红书登录凭据内容异常；请重新提交 Cookie。",
    ),
    "wechat_channels": PlatformSessionSpec(
        platform="wechat_channels", kind="wechat_channels_session", label="微信视频号",
        adapter="wechat-channels-web", endpoint="https://channels.weixin.qq.com",
        model="cookie", cookie_mode="cookie_dict",
        url_domains=("channels.weixin.qq.com",),  # 公众号 mp.weixin.qq.com 不属于视频号
        cookie_send_domains=("channels.weixin.qq.com",),  # P0 实测后收紧
        probe_url="https://channels.weixin.qq.com",
        requeue_keywords=("登录", "凭据", "会话"),
        allowed_cookie_names=None,  # docs/18 §7.2 规则 1：P0 实测最小集合后收紧
        max_secret_bytes=8192,
        event_type="platform_session_updated",
        status_notes={
            "unconfigured": _STATUS_NOTE_TPL["unconfigured"].format(label="微信视频号"),
            "configured": _STATUS_NOTE_TPL["configured"].format(label="微信视频号", mode="Cookie"),
            "revoked": _STATUS_NOTE_TPL["revoked"],
        },
        revoke_notes={
            "absent": _REVOKE_NOTE_TPL["absent"].format(label="微信视频号"),
            "revoked": _REVOKE_NOTE_TPL["revoked"],
        },
        not_configured_error="尚未托管微信视频号登录态",
        decrypt_error="视频号登录凭据解密失败（{exc_type}）；请重新提交 Cookie。",
        stored_format_error="视频号登录凭据内容异常；请重新提交 Cookie。",
    ),
    "zhihu": PlatformSessionSpec(
        platform="zhihu", kind="zhihu_session", label="知乎",
        adapter="zhihu-web", endpoint="https://www.zhihu.com",
        model="cookie", cookie_mode="cookie_dict",
        url_domains=("zhihu.com",),
        cookie_send_domains=("www.zhihu.com", "zhuanlan.zhihu.com"),  # P0 实测后收紧
        probe_url="https://www.zhihu.com/",
        requeue_keywords=("登录", "凭据", "会话", "字幕"),
        allowed_cookie_names=None,  # docs/18 §7.2 规则 1：P0 实测最小集合后收紧
        max_secret_bytes=8192,
        event_type="platform_session_updated",
        status_notes={
            "unconfigured": _STATUS_NOTE_TPL["unconfigured"].format(label="知乎"),
            "configured": _STATUS_NOTE_TPL["configured"].format(label="知乎", mode="Cookie"),
            "revoked": _STATUS_NOTE_TPL["revoked"],
        },
        revoke_notes={
            "absent": _REVOKE_NOTE_TPL["absent"].format(label="知乎"),
            "revoked": _REVOKE_NOTE_TPL["revoked"],
        },
        not_configured_error="尚未托管知乎登录态",
        decrypt_error="知乎登录凭据解密失败（{exc_type}）；请重新提交 Cookie。",
        stored_format_error="知乎登录凭据内容异常；请重新提交 Cookie。",
    ),
}

# 明确属于其他平台的 Cookie 字段：提交到当前平台时直接拒绝（docs/18 §7.2 规则 2）
_FOREIGN_COOKIE_NAMES = frozenset({"sessdata"})

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")
_MAX_COOKIE_NAME_LEN = 128
_MAX_COOKIE_VALUE_LEN = 4096
_MAX_COOKIE_PAIRS = 64


# ---- Cookie 清洗 ----

def parse_cookie_text(raw: str, spec: PlatformSessionSpec) -> dict[str, str]:
    """解析浏览器 Cookie 头文本为 name→value；错误信息不回显原文（docs/18 §7.2 规则 2）。

    拒绝：换行、重复字段名、超长字段、非本平台字段（如把 B 站 SESSDATA
    提交给其他平台）；字段名/值字符集按 Cookie 头常规约定。
    """
    if "\r" in raw or "\n" in raw:
        raise ApiError("SCHEMA_INVALID", "Cookie 不能包含换行；请提交浏览器 Cookie 请求头的一行文本")
    if len(raw.encode("utf-8")) > spec.max_secret_bytes:
        raise ApiError("PAYLOAD_TOO_LARGE", f"Cookie 超过 {spec.max_secret_bytes} 字节上限")
    pairs: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ApiError("SCHEMA_INVALID", "Cookie 格式无法解析：应为 name=value; name2=value2 的形式")
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if not name or len(name) > _MAX_COOKIE_NAME_LEN:
            raise ApiError("SCHEMA_INVALID", "Cookie 字段名缺失或过长")
        if not _COOKIE_NAME_RE.fullmatch(name):
            raise ApiError("SCHEMA_INVALID", "Cookie 字段名含有不允许的字符")
        if len(value) > _MAX_COOKIE_VALUE_LEN:
            raise ApiError("PAYLOAD_TOO_LARGE", "Cookie 字段值过长")
        if not all(0x20 <= ord(ch) <= 0x7E for ch in value):
            raise ApiError("SCHEMA_INVALID", "Cookie 字段值含有不允许的字符")
        if name in pairs:
            raise ApiError("SCHEMA_INVALID", f"Cookie 存在重复字段名：{name}")
        if spec.platform != "bilibili" and name.lower() in _FOREIGN_COOKIE_NAMES:
            raise ApiError("SCHEMA_INVALID", "Cookie 含有其他平台的登录字段，请确认复制的是本平台的 Cookie")
        pairs[name] = value
    if not pairs:
        raise ApiError("SCHEMA_INVALID", "没有解析到任何 Cookie 字段")
    if len(pairs) > _MAX_COOKIE_PAIRS:
        raise ApiError("PAYLOAD_TOO_LARGE", f"Cookie 字段数超过 {_MAX_COOKIE_PAIRS} 个上限")
    if spec.allowed_cookie_names is not None:
        pairs = {n: v for n, v in pairs.items() if n in spec.allowed_cookie_names}
        if not pairs:
            raise ApiError("SCHEMA_INVALID", "Cookie 中没有本平台需要的字段")
    return pairs


def cookie_header(cookies: dict[str, str]) -> str:
    """把清洗后的 Cookie 字典拼成 Cookie 请求头值（适配器按白名单域名发送）。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ---- 通用生命周期 ----

def session_profile(db: Session, user_id: str, spec: PlatformSessionSpec) -> ProviderProfile | None:
    return (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_id, ProviderProfile.kind == spec.kind)
        .order_by(ProviderProfile.created_at)
        .first()
    )


def active_credential(db: Session, profile: ProviderProfile) -> Credential | None:
    return (
        db.query(Credential)
        .filter(Credential.profile_id == profile.id, Credential.revoked_at.is_(None))
        .order_by(Credential.created_at.desc())
        .first()
    )


def session_status(db: Session, user_id: str, spec: PlatformSessionSpec,
                   profile: ProviderProfile | None = None) -> dict:
    """脱敏状态：configured/verification/更新时间/最近检测结果；不含任何明文。"""
    if profile is None:
        profile = session_profile(db, user_id, spec)
    if profile is None:
        return {"platform": spec.platform, "label": spec.label, "configured": False,
                "credential_version": None, "updated_at": None,
                "verification": "unconfigured", "last_check": None,
                "note": spec.status_notes["unconfigured"]}
    cred = active_credential(db, profile)
    last_check = (profile.meta_json or {}).get(f"{spec.platform}_last_check")
    verification = (last_check.get("status") or "unverified") if last_check else "unverified"
    return {
        "platform": spec.platform, "label": spec.label,
        "configured": cred is not None,
        "credential_version": cred.version if cred else None,
        "updated_at": cred.created_at.isoformat() if cred else None,
        "verification": verification,
        "last_check": last_check,
        "note": spec.status_notes["configured"] if cred is not None else spec.status_notes["revoked"],
    }


def _item_matches_platform(db: Session, item: Item, spec: PlatformSessionSpec) -> bool:
    """条目是否属于该平台：按最新来源版本元数据或采集输入 URL 判断。"""
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == item.source_revision)
        .one_or_none()
    )
    meta = source.metadata_json if source else {}
    if meta.get("platform") == spec.platform:
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
        for d in spec.url_domains:
            if host == d or host.endswith("." + d) or host.endswith(d):
                return True
    return False


def requeue_waiting_items(db: Session, user_id: str, spec: PlatformSessionSpec) -> int:
    """登录态更新：仅重提同用户、同平台、因登录原因等待且无人工补充正文的条目
    （docs/18 §7.2 规则 7）。不像全量 refetch 一样重跑所有内容。"""
    items = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.pipeline_state == "needs_input", Item.deleted_at.is_(None))
        .all()
    )
    requeued = 0
    for it in items:
        if not _item_matches_platform(db, it, spec):
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
        if not any(k in detail for k in spec.requeue_keywords):
            continue  # 非登录原因的待补充条目不重排
        pipeline.enqueue_stage(
            db, user_id=user_id, item_id=it.id, source_revision=it.source_revision,
            stage="extract", reset_attempt=True,
        )
        requeued += 1
    return requeued


def store_session(db: Session, user_id: str, spec: PlatformSessionSpec, plaintext: str) -> tuple[dict, int]:
    """保存平台登录态：新 Credential version 替换旧版（只进不出），定向重排队。

    返回 (脱敏状态, 重排队条目数)。每次更新都创建新版本并撤销旧版；
    AES-256-GCM AAD 绑定 user_id/profile_id/version。
    """
    profile = session_profile(db, user_id, spec)
    if profile is None:
        profile = ProviderProfile(
            user_id=user_id, kind=spec.kind, adapter=spec.adapter,
            endpoint=spec.endpoint, model=spec.model,
            capabilities_json={}, version=1,
        )
        db.add(profile)
        db.flush()

    current = active_credential(db, profile)
    next_version = (current.version if current else 0) + 1
    if current:
        current.revoked_at = utcnow()
    encrypted = cred_crypto.encrypt_secret(
        plaintext, get_settings().load_master_key(),
        user_id=user_id, profile_id=profile.id, credential_version=next_version,
    )
    db.add(Credential(user_id=user_id, profile_id=profile.id, version=next_version,
                      master_key_version=get_settings().master_key_version, **encrypted))

    requeued = requeue_waiting_items(db, user_id, spec)
    db.commit()
    db.refresh(profile)
    out = session_status(db, user_id, spec, profile)
    out["requeued_items"] = requeued
    pipeline.emit_event(db, user_id, item_id=None, bundle_revision=None,
                        event_type=spec.event_type,
                        payload={"platform": spec.platform, "credential_version": next_version,
                                 "requeued": requeued})
    db.commit()
    return out, requeued


def revoke_session(db: Session, user_id: str, spec: PlatformSessionSpec) -> dict:
    """撤销活跃凭据；后续提取回到匿名路径（docs/18 §7.2 规则 8）。"""
    profile = session_profile(db, user_id, spec)
    if profile is None:
        return {"revoked": True, "note": spec.revoke_notes["absent"]}
    now = utcnow()
    revoked = 0
    for cred in db.query(Credential).filter(
        Credential.profile_id == profile.id, Credential.revoked_at.is_(None)
    ).all():
        cred.revoked_at = now
        revoked += 1
    db.commit()
    return {"revoked": True, "revoked_versions": revoked, "note": spec.revoke_notes["revoked"]}


def session_value(db: Session, user_id: str, spec: PlatformSessionSpec,
                  profile: ProviderProfile | None = None,
                  cred: Credential | None = None) -> tuple[str | dict | None, str | None]:
    """适配器读取入口：按 user_id + platform 解密，返回短生命周期内存值。

    没有托管 → (None, None)；解密失败/内容异常 → (None, 用户文案)。
    明文不缓存到进程全局，不写日志（docs/18 §7.2）。
    """
    if profile is None or cred is None:
        profile = session_profile(db, user_id, spec)
        cred = active_credential(db, profile) if profile else None
    if cred is None:
        return None, None
    try:
        raw = cred_crypto.decrypt_secret(
            cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
            get_settings().load_master_key(),
            user_id=user_id, profile_id=profile.id, credential_version=cred.version,
        )
    except Exception as exc:  # 解密失败由调用方进入补充材料，不掩盖
        return None, spec.decrypt_error.format(exc_type=type(exc).__name__)
    if spec.cookie_mode == "cookie_dict":
        try:
            doc = json.loads(raw)
            cookies = doc.get("cookies") if isinstance(doc, dict) else None
        except ValueError:
            cookies = None
        if not isinstance(cookies, dict) or not cookies:
            return None, spec.stored_format_error
        return {str(k): str(v) for k, v in cookies.items()}, None
    return raw, None


def test_session(db: Session, user_id: str, spec: PlatformSessionSpec,
                 check_fn) -> dict:
    """发起只读会话检测；结果写入 profile.meta_json（脱敏，不含页面内容）。

    check_fn(明文值) → {"status": valid|invalid|blocked|network_error|unverified,
    "detail": ...}；不读取或修改用户内容（docs/18 §7.2 规则 6）。
    """
    profile = session_profile(db, user_id, spec)
    cred = active_credential(db, profile) if profile else None
    if cred is None:
        raise ApiError("SCHEMA_INVALID", spec.not_configured_error, status_code=422)
    value, err = session_value(db, user_id, spec, profile, cred)
    if err is not None:
        check = {"status": "invalid", "detail": err}
    else:
        check = dict(check_fn(value))
    check["checked_at"] = utcnow().isoformat()
    profile.meta_json = {**(profile.meta_json or {}), f"{spec.platform}_last_check": check}
    db.commit()
    return check
