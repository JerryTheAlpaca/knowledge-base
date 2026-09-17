"""每用户平台登录态托管接口（docs/18 §7.2）：小红书 / 微信视频号 / 知乎。

- {platform} 是固定枚举，不接受请求方提供任意 endpoint；
  B 站继续使用 /v1/bilibili-session（暂不迁移，避免破坏现有客户端）。
- GET 只返回 configured、verification、更新时间和脱敏检测结果；任何响应不含明文。
- PUT 接受 Cookie 文本：服务端清洗、只保留最小字段、信封加密、新版本替换
  旧版并按登录原因定向重排队。
- POST test 发起只读的账号/会话检测，不读取或修改用户内容。
- DELETE 撤销活跃凭据；后续回到匿名提取或补充材料。
- 写操作与 B 站一致要求 profiles:manage；CSRF/Origin 校验由认证依赖统一处理。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..api.deps import require_scope
from ..domain import platform_sessions
from ..domain.errors import ApiError
from ..domain.platform_sessions import SPECS, PlatformSessionSpec
from ..security.safe_fetch import SafeFetchError, safe_fetch

router = APIRouter(tags=["platform-sessions"])

# 新端点固定支持的平台；bilibili 有专属旧接口
SUPPORTED_PLATFORMS = ("xiaohongshu", "wechat_channels", "zhihu")


def _spec(platform: str) -> PlatformSessionSpec:
    if platform == "bilibili":
        raise ApiError("NOT_FOUND", "B 站登录态请继续使用 /v1/bilibili-session 接口")
    spec = SPECS.get(platform)
    if spec is None or platform not in SUPPORTED_PLATFORMS:
        raise ApiError("NOT_FOUND", "不支持的平台登录态")
    return spec


class SessionSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(min_length=16, max_length=8192)


def _generic_probe(spec: PlatformSessionSpec, value: dict[str, str]) -> dict:
    """通用只读探测：携带会话访问平台页面，只区分网络/风控与无法判定。

    登录态是否真正有效取决于平台页面结构（P0 真实样本确认后再收紧判定），
    页面可达时如实返回 unverified，不伪造 valid（docs/18 §5.3、§7.2 规则 6）。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": platform_sessions.cookie_header(value),
    }
    try:
        result = safe_fetch(spec.probe_url, max_bytes=65536, timeout=15.0, headers=headers)
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            return {"status": "network_error", "detail": f"检测请求失败：{exc}"}
        return {"status": "blocked", "detail": f"平台拒绝访问：{exc}"}
    if result.status_code >= 500:
        return {"status": "network_error", "detail": f"平台临时错误（HTTP {result.status_code}）"}
    if result.status_code >= 400:
        return {"status": "blocked", "detail": f"平台拒绝访问（HTTP {result.status_code}）"}
    return {"status": "unverified",
            "detail": "已能携带会话访问平台；登录态是否有效需在真实提取中确认。"}


@router.get("/v1/platform-sessions/{platform}")
def get_platform_session(platform: str, principal=Depends(require_scope("profiles:manage")),
                         db: Session = Depends(get_db)):
    return platform_sessions.session_status(db, principal.user.id, _spec(platform))


@router.put("/v1/platform-sessions/{platform}")
def put_platform_session(platform: str, body: SessionSecret,
                         principal=Depends(require_scope("profiles:manage")),
                         db: Session = Depends(get_db)):
    spec = _spec(platform)
    cookies = platform_sessions.parse_cookie_text(body.secret, spec)
    # 多字段规范化为确定性 secret JSON 后整体加密（docs/18 §7.2 规则 1）
    plaintext = json.dumps({"cookies": cookies}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    out, _requeued = platform_sessions.store_session(db, principal.user.id, spec, plaintext)
    return out


@router.post("/v1/platform-sessions/{platform}/test")
def test_platform_session(platform: str, principal=Depends(require_scope("profiles:manage")),
                          db: Session = Depends(get_db)):
    spec = _spec(platform)
    return platform_sessions.test_session(db, principal.user.id, spec,
                                          check_fn=lambda value: _generic_probe(spec, value))


@router.delete("/v1/platform-sessions/{platform}")
def delete_platform_session(platform: str, principal=Depends(require_scope("profiles:manage")),
                            db: Session = Depends(get_db)):
    return platform_sessions.revoke_session(db, principal.user.id, _spec(platform))
