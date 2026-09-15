"""管理员接口（Web 收件箱「管理」页签）。

- 邀请码：以当前管理员的中心会话 Cookie 代理中心认证站点（Ledger）的
  /api/invitations*；权限由本端 is_admin 与中心端双重校验，完整码只在
  创建响应中出现一次。
- ASR 总览：聚合本库 asr_runs，只输出计数、进度与累计分钟（不含文件名）。
- 服务器状态：直读 /proc/stat、/proc/meminfo 与数据盘 statvfs（与
  workers/idle.py 同一方式，容器内读到的即宿主机整机指标），不加依赖。
- 凭据代配：按目标用户写入 llm / bilibili_session 凭据（docs/16）。
  密文 AAD 绑定目标用户；响应不回显 secret。代配的 LLM 配置标记
  local_export=denied，暂不允许经本机绑定下发到试用用户电脑。
- 删除账户：先删中心认证账号再硬删本地全部数据（条目、凭据、设备、
  在线对象与临时目录），仅限管理员且不能删自己。
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import current_principal
from ..domain import pipeline
from ..domain.errors import ApiError
from ..models import (
    AsrRun,
    AudioAsset,
    AudioUploadSession,
    BundleRevision,
    Capture,
    Credential,
    Device,
    Event,
    IdempotencyRecord,
    Item,
    Job,
    LocalKeyBinding,
    PairingCode,
    ProviderOperation,
    ProviderProfile,
    Receipt,
    SourceRevision,
    StoredFile,
    SuppressedItem,
    Token,
    Upload,
    User,
    utcnow,
)
from ..security import credentials as cred_crypto
from . import routes_bilibili, routes_profiles

router = APIRouter(prefix="/v1/admin", tags=["admin"])

PROXIED_STATUS = {401, 403, 404, 409, 422, 429}


def require_admin(principal=Depends(current_principal)):
    """管理页签专用守卫：中心角色不是 admin 一律 403。"""
    if not principal.is_admin:
        raise ApiError("FORBIDDEN", "需要管理员权限", status_code=403)
    return principal


def _central_base(settings) -> str:
    """由中心登录地址推导中心站点根（同一部署，邀请码 API 就在那里）。"""
    if not settings.auth_login_url:
        raise ApiError("ADMIN_UNAVAILABLE", "未配置中心登录地址（AUTH_LOGIN_URL）", status_code=503)
    parts = urlsplit(settings.auth_login_url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _proxy_central(request: Request, method: str, path: str, json_body: dict | None = None) -> dict:
    """以当前管理员会话调用中心站点 API，展开 {data}/{error} 信封。"""
    settings = get_settings()
    cookie = request.cookies.get(settings.auth_cookie_name)
    if not cookie:
        raise ApiError("AUTH_EXPIRED", "未登录", status_code=401)
    headers = {"Accept": "application/json"}
    if settings.public_base_url:
        # 中心侧 Origin 白名单已含 KB 站点；写操作（新建/撤销邀请码）必需
        headers["Origin"] = settings.public_base_url
    try:
        resp = httpx.request(
            method, _central_base(settings) + path,
            cookies={settings.auth_cookie_name: cookie},
            json=json_body, headers=headers,
            timeout=httpx.Timeout(get_settings().auth_timeout_seconds, connect=3.0),
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise ApiError("ADMIN_UNAVAILABLE", f"中心服务不可达：{type(exc).__name__}", status_code=503) from exc
    try:
        doc = resp.json()
    except ValueError:
        doc = None
    if resp.status_code >= 500:
        raise ApiError("ADMIN_UNAVAILABLE", f"中心服务错误（HTTP {resp.status_code}）", status_code=503)
    if resp.status_code >= 400:
        err = (doc or {}).get("error") or {}
        raise ApiError(
            err.get("code") or "ADMIN_REJECTED",
            err.get("message") or f"中心拒绝了该操作（HTTP {resp.status_code}）",
            status_code=resp.status_code if resp.status_code in PROXIED_STATUS else 502,
        )
    data = (doc or {}).get("data")
    if not isinstance(data, dict):
        raise ApiError("ADMIN_UPSTREAM_INVALID", "中心返回了意外的数据格式", status_code=502)
    return data


@router.get("/invitations")
def list_invitations(request: Request, _admin=Depends(require_admin)):
    """邀请码列表（尾号/状态/使用者用户名；不含完整码）。"""
    return _proxy_central(request, "GET", "/api/invitations")


@router.post("/invitations")
def create_invitation(request: Request, _admin=Depends(require_admin)):
    """新建邀请码：完整码只在本次响应中出现。"""
    return _proxy_central(request, "POST", "/api/invitations", {})


@router.post("/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: str, request: Request, _admin=Depends(require_admin)):
    """撤销一条未使用的邀请码。"""
    return _proxy_central(request, "POST", f"/api/invitations/{invitation_id}/revoke", {})


@router.delete("/invitations/{invitation_id}")
def delete_invitation(invitation_id: str, request: Request, _admin=Depends(require_admin)):
    """删除一条已撤销/已过期的邀请码记录（已使用/未使用的不允许删）。"""
    return _proxy_central(request, "DELETE", f"/api/invitations/{invitation_id}")


@router.get("/asr-overview")
def asr_overview(_admin=Depends(require_admin), db: Session = Depends(get_db)):
    """全部用户的 ASR 提交/排队概况与每人累计处理分钟（不含文件名）。"""
    # 聚合查询代替全表加载（审查 C-08）；active 明细行数少，单独取。
    # active 一律排除已删除条目：历史脏 run（如删除前卡在 preparing 的孤儿）
    # 不应让管理页永远显示「进行中」
    ACTIVE = ("queued", "preparing", "transcribing", "paused")
    from sqlalchemy import case, func

    agg = (
        db.query(
            AsrRun.user_id,
            func.count(AsrRun.id).label("total_runs"),
            func.coalesce(func.sum(AsrRun.processed_seconds), 0.0).label("processed_seconds"),
            func.sum(case((AsrRun.state.in_(ACTIVE) & Item.deleted_at.is_(None), 1), else_=0)).label("active_runs"),
        )
        .join(Item, Item.id == AsrRun.item_id)
        .group_by(AsrRun.user_id)
        .all()
    )
    names = {u.id: u.name for u in db.query(User.id, User.name).all()}
    active_rows = (
        db.query(AsrRun)
        .join(Item, Item.id == AsrRun.item_id)
        .filter(AsrRun.state.in_(ACTIVE), Item.deleted_at.is_(None))
        .all()
    )

    users: dict[str, dict] = {}
    for r in agg:
        users[r.user_id] = {
            "user_id": r.user_id, "username": names.get(r.user_id, r.user_id),
            "total_runs": r.total_runs, "active_runs": int(r.active_runs or 0),
            "processed_seconds": float(r.processed_seconds), "active": [],
        }
    for r in active_rows:
        users.setdefault(r.user_id, {
            "user_id": r.user_id, "username": names.get(r.user_id, r.user_id),
            "total_runs": 0, "active_runs": 0, "processed_seconds": 0.0, "active": [],
        })
        users[r.user_id]["active"].append({
            "state": r.state,
            "done_chunks": r.next_chunk_index,
            "chunk_count": r.chunk_count,
            "processed_minutes": round(float(r.processed_seconds or 0.0) / 60.0, 1),
        })

    totals = {
        "submitted_users": len(users),
        "queued_users": len({r.user_id for r in active_rows if r.state in ("queued", "preparing")}),
        "queued_runs": sum(1 for r in active_rows if r.state in ("queued", "preparing")),
        "transcribing_runs": sum(1 for r in active_rows if r.state == "transcribing"),
        "paused_runs": sum(1 for r in active_rows if r.state == "paused"),
    }
    user_list = sorted(
        ({
            "user_id": u["user_id"], "username": u["username"],
            "total_runs": u["total_runs"], "active_runs": u["active_runs"],
            "processed_minutes": round(u["processed_seconds"] / 60.0, 1),
            "active": sorted(u["active"], key=lambda a: {"transcribing": 0, "preparing": 1,
                                                         "queued": 2, "paused": 3}.get(a["state"], 9)),
        } for u in users.values()),
        key=lambda u: (u["active_runs"] == 0, -u["processed_minutes"]),
    )
    return {"totals": totals, "users": user_list}


def _read_cpu_percent(sample_interval: float = 0.25) -> float | None:
    """两次采样 /proc/stat 计算 CPU 占用率；读不到（非 Linux）返回 None。"""
    from ..workers.idle import sample_host

    first = sample_host()
    if first is None or first.total <= 0:
        return None
    time.sleep(sample_interval)
    second = sample_host()
    if second is None or second.total <= first.total:
        return None
    busy = (second.total - second.idle) - (first.total - first.idle)
    total = second.total - first.total
    return round(max(0.0, min(100.0, busy / total * 100.0)), 1)


def _read_memory() -> dict | None:
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            info: dict[str, float] = {}
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = float(rest.strip().split()[0])  # kB
        if "MemTotal" not in info or "MemAvailable" not in info:
            return None
    except (OSError, ValueError, IndexError):
        return None
    total = info["MemTotal"] * 1024.0
    used = total - info["MemAvailable"] * 1024.0
    return {"used_bytes": int(used), "total_bytes": int(total),
            "percent": round(used / total * 100.0, 1)}


def _read_disk() -> dict | None:
    settings = get_settings()
    path = str(settings.objects_dir)
    try:
        st = os.statvfs(path)  # Unix 专属；Windows 等环境返回 None（前端显示「指标不可用」）
    except (OSError, AttributeError):
        return None
    total = st.f_blocks * st.f_frsize
    available = st.f_bavail * st.f_frsize
    if total <= 0:
        return None
    used = total - available
    return {"path": path, "used_bytes": int(used), "total_bytes": int(total),
            "percent": round(used / total * 100.0, 1)}


@router.get("/server-stats")
def server_stats(_admin=Depends(require_admin)):
    """宿主机 CPU / 内存 / 数据盘占用（指标读不到时对应字段为 null）。"""
    return {"cpu_percent": _read_cpu_percent(), "memory": _read_memory(), "disk": _read_disk()}


# ---- 用户凭据代配（docs/16） ----

class AdminLlmCredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    model: str = Field(min_length=1, max_length=120)
    capabilities: dict | None = None
    secret: str = Field(min_length=8, max_length=4096)


class AdminBiliSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(min_length=16, max_length=8192)


def _require_target_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("NOT_FOUND", "用户不存在", status_code=404)
    if user.status != "active":
        raise ApiError("SCHEMA_INVALID", "用户不可用", status_code=422)
    return user


def _primary_llm_profile(db: Session, user_id: str) -> ProviderProfile | None:
    """该用户最近一条 llm 配置（管理页只维护这一条主配置）。"""
    return (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_id, ProviderProfile.kind == "llm")
        .order_by(ProviderProfile.created_at.desc())
        .first()
    )


def _active_cred(db: Session, profile_id: str) -> Credential | None:
    return (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.revoked_at.is_(None))
        .order_by(Credential.created_at.desc())
        .first()
    )


def _mask_subject(subject: str | None) -> str:
    if not subject:
        return ""
    if len(subject) <= 6:
        return subject[:2] + "***"
    return subject[:4] + "…" + subject[-2:]


@router.get("/users")
def list_users(_admin=Depends(require_admin), db: Session = Depends(get_db)):
    """本地用户列表：配置状态摘要与条目统计，不含任何密钥。"""
    users = db.query(User).order_by(User.created_at).all()
    # 条目统计：一次分组查询带出每人的有效条目数与最近一条时间
    from sqlalchemy import func

    item_stats = {
        row[0]: (int(row[1]), row[2])
        for row in db.query(Item.user_id, func.count(Item.id), func.max(Item.created_at))
        .filter(Item.deleted_at.is_(None))
        .group_by(Item.user_id)
        .all()
    }
    out = []
    for u in users:
        llm = _primary_llm_profile(db, u.id)
        llm_cred = _active_cred(db, llm.id) if llm else None
        bili = (
            db.query(ProviderProfile)
            .filter(ProviderProfile.user_id == u.id, ProviderProfile.kind == "bilibili_session")
            .order_by(ProviderProfile.created_at)
            .first()
        )
        bili_cred = _active_cred(db, bili.id) if bili else None
        bili_check = ((bili.meta_json or {}) if bili else {}).get("bilibili_last_check")
        item_count, last_item_at = item_stats.get(u.id, (0, None))
        out.append({
            "user_id": u.id,
            "name": u.name,
            "status": u.status,
            "auth_subject": _mask_subject(u.auth_subject),
            "created_at": u.created_at.isoformat(),
            "item_count": item_count,
            "last_item_at": last_item_at.isoformat() if last_item_at else None,
            "llm": {
                "configured": llm_cred is not None,
                "profile_id": llm.id if llm else None,
                "model": llm.model if llm else None,
                "endpoint_host": (urlsplit(llm.endpoint).hostname if llm else None),
                "credential_version": llm_cred.version if llm_cred else None,
                "updated_at": llm_cred.created_at.isoformat() if llm_cred else None,
                "local_export": (llm.meta_json or {}).get("local_export") if llm else None,
            },
            "bilibili": {
                "configured": bili_cred is not None,
                "verification": (bili_check or {}).get("status") if bili_cred else None,
                "updated_at": bili_cred.created_at.isoformat() if bili_cred else None,
            },
        })
    return {"users": out}


@router.get("/users/{user_id}/credentials")
def user_credentials(user_id: str, _admin=Depends(require_admin), db: Session = Depends(get_db)):
    """单个用户的凭据状态（复用用户侧展示字段，无 secret）。"""
    _require_target_user(db, user_id)
    llm = _primary_llm_profile(db, user_id)
    bili = (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_id, ProviderProfile.kind == "bilibili_session")
        .order_by(ProviderProfile.created_at)
        .first()
    )
    return {
        "llm": routes_profiles._profile_out(db, llm).model_dump() if llm else None,
        "llm_local_export": (llm.meta_json or {}).get("local_export") if llm else None,
        "bilibili": routes_bilibili._out(db, bili),
    }


@router.put("/users/{user_id}/llm-credential")
def put_user_llm_credential(
    user_id: str, body: AdminLlmCredentialIn,
    _admin=Depends(require_admin), db: Session = Depends(get_db),
):
    """为目标用户新建/覆盖主 LLM 配置与 Key；标记暂不允许本地下发。"""
    user = _require_target_user(db, user_id)
    endpoint = routes_profiles._validate_endpoint(body.endpoint)
    caps = routes_profiles._validate_capabilities(body.capabilities)
    settings = get_settings()

    profile = _primary_llm_profile(db, user.id)
    if profile is None:
        profile = ProviderProfile(
            user_id=user.id, kind="llm", adapter="openai-compatible",
            endpoint=endpoint, model=body.model, capabilities_json=caps, version=1,
            meta_json={"admin_provisioned": True, "local_export": "denied",
                       "provisioned_at": utcnow().isoformat()},
        )
        db.add(profile)
        db.flush()
        next_version = 1
    else:
        profile.endpoint = endpoint
        profile.model = body.model
        profile.capabilities_json = caps
        profile.version += 1
        profile.meta_json = {
            **(profile.meta_json or {}),
            "admin_provisioned": True,
            "local_export": "denied",
            "provisioned_at": utcnow().isoformat(),
        }
        current = _active_cred(db, profile.id)
        next_version = (current.version if current else 0) + 1
        if current:
            current.revoked_at = utcnow()

    encrypted = cred_crypto.encrypt_secret(
        body.secret, settings.load_master_key(),
        user_id=user.id, profile_id=profile.id, credential_version=next_version,
    )
    db.add(Credential(
        user_id=user.id, profile_id=profile.id, version=next_version,
        master_key_version=settings.master_key_version, **encrypted,
    ))
    requeued = routes_profiles._requeue_waiting(db, user.id, "waiting_key", "enrich")
    pipeline.emit_event(
        db, user.id, item_id=None, bundle_revision=None,
        event_type="credentials_updated",
        payload={"profile_id": profile.id, "requeued": requeued, "source": "admin_panel"},
    )
    db.commit()
    db.refresh(profile)
    out = routes_profiles._profile_out(db, profile).model_dump()
    out["local_export"] = "denied"
    out["requeued_items"] = requeued
    out["note"] = "已写入该用户账号；暂不支持下发到本机绑定。"
    return out


@router.delete("/users/{user_id}/llm-credential")
def delete_user_llm_credential(
    user_id: str, _admin=Depends(require_admin), db: Session = Depends(get_db),
):
    user = _require_target_user(db, user_id)
    profile = _primary_llm_profile(db, user.id)
    if profile is None:
        return {"revoked": True, "revoked_versions": 0, "note": "该用户没有 LLM 配置。"}
    now = utcnow()
    revoked = 0
    for cred in db.query(Credential).filter(
        Credential.profile_id == profile.id, Credential.revoked_at.is_(None)
    ).all():
        cred.revoked_at = now
        revoked += 1
    db.commit()
    return {
        "profile_id": profile.id, "revoked": True, "revoked_versions": revoked,
        "note": "后续加工任务将进入 waiting_key；本机若曾绑定的副本需用户自行删除或供应商作废。",
    }


@router.put("/users/{user_id}/bilibili-session")
def put_user_bilibili_session(
    user_id: str, body: AdminBiliSessionIn,
    _admin=Depends(require_admin), db: Session = Depends(get_db),
):
    """为目标用户写入 B 站 SESSDATA（仅云端使用，不下发插件）。"""
    user = _require_target_user(db, user_id)
    out = routes_bilibili.upsert_session_for_user(db, user.id, body.secret)
    out["note"] = "仅用于服务器提取字幕，不会下发到 Obsidian 插件。"
    return out


@router.delete("/users/{user_id}/bilibili-session")
def delete_user_bilibili_session(
    user_id: str, _admin=Depends(require_admin), db: Session = Depends(get_db),
):
    user = _require_target_user(db, user_id)
    return routes_bilibili.revoke_session_for_user(db, user.id)


@router.post("/users/{user_id}/bilibili-session/test")
def test_user_bilibili_session(
    user_id: str, _admin=Depends(require_admin), db: Session = Depends(get_db),
):
    user = _require_target_user(db, user_id)
    return routes_bilibili.test_session_for_user(db, user.id)


# ---- 删除用户账户 ----

def purge_user_local_data(db: Session, user_id: str) -> int:
    """硬删除一个本地用户在 KB 的全部数据（行 + 在线对象 + 临时目录）。

    供管理端点与服务器侧脚本共用。必须先删磁盘对象再删行：行删掉之后
    retention sweep 看不到这些登记，对象必须在此处显式回收。
    """
    from ..storage.objects import ObjectStore
    from ..workers.asr import _cleanup_by_work_dir

    store = ObjectStore()
    settings = get_settings()

    # 1) 磁盘对象：文件登记、上传原件、Bundle 清单（同 key 去重）
    keys = [k for (k,) in db.query(StoredFile.storage_key).filter(StoredFile.user_id == user_id).all()]
    keys += [k for (k,) in db.query(Upload.storage_key).filter(Upload.user_id == user_id).all()]
    keys += [k for (k,) in db.query(BundleRevision.manifest_key).filter(BundleRevision.user_id == user_id).all()]
    for key in dict.fromkeys(keys):
        store.delete_object(key)
    # ASR 工作目录（PCM 与临时输入）与未完成音频会话的 staging 占用
    for (work_dir,) in db.query(AsrRun.work_dir).filter(
        AsrRun.user_id == user_id, AsrRun.work_dir != ""
    ).all():
        _cleanup_by_work_dir(settings, work_dir)
    for (staging,) in db.query(AudioUploadSession.staging_path).filter(
        AudioUploadSession.user_id == user_id, AudioUploadSession.state == "receiving"
    ).all():
        store.discard_staging(staging)

    # 2) 行：先子后父。usage_ledger 是去计费前的历史遗留表（模型已移除但
    #    生产库仍保留），FK 引用 provider_operations.id，必须先删。
    from sqlalchemy import text

    if db.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_ledger'")
    ).fetchone():
        db.execute(text("DELETE FROM usage_ledger WHERE user_id = :uid"), {"uid": user_id})
    deleted = 0
    for model in (
        ProviderOperation, Job, AsrRun, Receipt, SuppressedItem, AudioAsset,
        SourceRevision, BundleRevision, StoredFile, LocalKeyBinding,
        Credential, ProviderProfile, Item, Capture, Upload, AudioUploadSession,
        Token, PairingCode, Event, IdempotencyRecord, Device,
    ):
        deleted += db.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
    deleted += db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
    return deleted


@router.delete("/users/{user_id}")
def delete_user(user_id: str, request: Request, _admin=Depends(require_admin), db: Session = Depends(get_db)):
    """删除用户账户：先删中心认证账号，再清本地全部数据。不可恢复。"""
    user = db.get(User, user_id)
    if user is None:
        raise ApiError("NOT_FOUND", "用户不存在", status_code=404)
    if user.id == _admin.user.id:
        raise ApiError("FORBIDDEN", "不能删除当前登录的账号", status_code=403)

    # 先删中心账号（失败则本地不动，可直接重试）；中心已无此账号（404）视为成功。
    if user.auth_subject:
        try:
            _proxy_central(request, "DELETE", f"/api/admin/users/{user.auth_subject}")
        except ApiError as exc:
            if exc.status_code != 404:
                raise
    purge_user_local_data(db, user.id)
    db.commit()
    return {"user_id": user.id, "deleted": True}
