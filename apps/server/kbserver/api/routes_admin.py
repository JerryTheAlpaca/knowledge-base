"""管理员接口（Web 收件箱「管理」页签）。

- 邀请码：以当前管理员的中心会话 Cookie 代理中心认证站点（Ledger）的
  /api/invitations*；权限由本端 is_admin 与中心端双重校验，完整码只在
  创建响应中出现一次。
- ASR 总览：聚合本库 asr_runs，只输出计数、进度与累计分钟（不含文件名）。
- 服务器状态：直读 /proc/stat、/proc/meminfo 与数据盘 statvfs（与
  workers/idle.py 同一方式，容器内读到的即宿主机整机指标），不加依赖。
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import current_principal
from ..domain.errors import ApiError
from ..models import AsrRun, User

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


@router.get("/asr-overview")
def asr_overview(_admin=Depends(require_admin), db: Session = Depends(get_db)):
    """全部用户的 ASR 提交/排队概况与每人累计处理分钟（不含文件名）。"""
    runs = db.query(AsrRun).all()
    names = {u.id: u.name for u in db.query(User).all()}
    ACTIVE = ("queued", "preparing", "transcribing", "paused")
    users: dict[str, dict] = {}
    for r in runs:
        u = users.setdefault(r.user_id, {
            "user_id": r.user_id, "username": names.get(r.user_id, r.user_id),
            "total_runs": 0, "active_runs": 0, "processed_seconds": 0.0, "active": [],
        })
        u["total_runs"] += 1
        u["processed_seconds"] += float(r.processed_seconds or 0.0)
        if r.state in ACTIVE:
            u["active_runs"] += 1
            u["active"].append({
                "state": r.state,
                "done_chunks": r.next_chunk_index,
                "chunk_count": r.chunk_count,
                "processed_minutes": round(float(r.processed_seconds or 0.0) / 60.0, 1),
            })

    queued_runs = sum(1 for r in runs if r.state in ("queued", "preparing"))
    totals = {
        "submitted_users": len(users),
        "queued_users": len({r.user_id for r in runs if r.state in ("queued", "preparing")}),
        "queued_runs": queued_runs,
        "transcribing_runs": sum(1 for r in runs if r.state == "transcribing"),
        "paused_runs": sum(1 for r in runs if r.state == "paused"),
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
