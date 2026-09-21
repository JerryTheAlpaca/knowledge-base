"""中心认证客户端（docs/05 §4.1）：固定目标、有限超时、不跟随重定向。

知识库只提取指定中心 Cookie 发给固定的会话校验接口，读取其真实返回的
用户 ID；不转发整串 Cookie，不放宽抓取器的公网限制（本模块是独立、固定
目标的认证客户端）。默认不缓存校验结果：中心会话撤销后下一次请求即失效。
唯一记下来的是「本站已经退出过哪颗凭据」，见 remember_revoked。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import time
from http.cookies import SimpleCookie

import httpx

from ..config import get_settings

# 退出后在这段时间内，那颗凭据在 KB 侧一律按未登录处理。中心每次校验都会回一颗
# 同值的滑动续期 Set-Cookie，任何在飞的请求晚到一步就把刚清掉的凭据又种回浏览器；
# 中心偶尔撤销得慢（或这次没撤销成），返回键回去就是登录态主页。
REVOKED_TTL_SECONDS = 300
# 进程内内存：uvicorn 目前 --workers 1（apps/server/Dockerfile）。加 worker 会让
# 退出只记在一个进程里，另一进程的晚到续期响应又能把凭据种回浏览器。
_revoked: dict[str, float] = {}


def _revoked_key(cookie_value: str) -> str:
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


def remember_revoked(cookie_value: str) -> None:
    now = time.monotonic()
    _revoked[_revoked_key(cookie_value)] = now + REVOKED_TTL_SECONDS
    for key, until in list(_revoked.items()):   # 退出是低频操作，顺手回收就够
        if until <= now:
            _revoked.pop(key, None)


def is_revoked(cookie_value: str) -> bool:
    until = _revoked.get(_revoked_key(cookie_value))
    if until is None:
        return False
    if until <= time.monotonic():
        _revoked.pop(_revoked_key(cookie_value), None)
        return False
    return True


class CentralAuthUnavailable(Exception):
    """中心认证服务超时/异常：可恢复，按 503 处理，不当作未登录。"""


class CentralAuthRejected(Exception):
    """中心明确返回未登录/会话无效：按 401 处理。"""


# 共享客户端：浏览器每个 Cookie 通道请求都要校验一次中心会话，走的是公网 HTTPS。
# 用 httpx.get() 每次新建客户端等于每个请求重做一遍 DNS+TCP+TLS 握手——握手是这台
# 2 核机器上真实的大头，两边都要付，而且每次请求都占着一个请求线程。
class _NeverStoreCookies(http.cookiejar.DefaultCookiePolicy):
    """共享客户端的 Cookie jar 必须永远为空。

    中心每次校验都回一颗同值的滑动续期 Set-Cookie。jar 一旦收下，下一个用户的请求
    就会带上上一个用户的会话凭据。续期值由 extract_renewal_cookie 单独取出后中继给
    浏览器，服务端自己不允许保管任何中心 Cookie；发送也只走显式 Cookie 头。
    """

    def set_ok(self, cookie, request):
        return False


def _new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    client = httpx.Client(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=False,
        transport=transport,
    )
    client.cookies.jar.set_policy(_NeverStoreCookies())
    return client


_client = _new_client()


def _cookie_header(settings, cookie_value: str) -> dict[str, str]:
    """按名字只带这一颗凭据，不整串转发浏览器 Cookie，也不经手 jar。"""
    return {"Cookie": f"{settings.auth_cookie_name}={cookie_value}"}


# 中心重启/重新部署的那一两秒里，KB 全部 Cookie 通道请求都会 503。这类失败是「秒回」
# 的（连接被拒、Caddy 502、复用的长连接变陈旧），所以按剩余预算补试一次就能吃掉抖动。
# 中心真的挂起时首次尝试已花光预算，剩下的时间不足就不再重试，最坏耗时仍约等于
# AUTH_TIMEOUT_SECONDS，不会翻倍。401 走 Rejected，永不重试。
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.4
_RETRY_MIN_REMAINING_SECONDS = 1.0


def _budget_seconds() -> float:
    return max(get_settings().auth_timeout_seconds, 1.0)


def _run_with_retry(call):
    deadline = time.monotonic() + _budget_seconds()
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return call(deadline)
        except CentralAuthUnavailable:
            if attempt >= _MAX_ATTEMPTS:
                raise
            if deadline - time.monotonic() < _RETRY_MIN_REMAINING_SECONDS:
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS)


def _timeout_until(deadline: float) -> httpx.Timeout:
    remaining = max(deadline - time.monotonic(), 0.05)
    return httpx.Timeout(remaining, connect=min(3.0, remaining))


def _validate_once(cookie_value: str, deadline: float) -> tuple[dict, dict | None]:
    settings = get_settings()
    headers = {"Accept": "application/json", **_cookie_header(settings, cookie_value)}
    try:
        resp = _client.get(settings.auth_session_url, timeout=_timeout_until(deadline),
                           headers=headers)
    except httpx.HTTPError as exc:
        raise CentralAuthUnavailable(f"中心认证服务不可达：{type(exc).__name__}") from exc
    if resp.status_code >= 500:
        raise CentralAuthUnavailable(f"中心认证服务错误（HTTP {resp.status_code}）")
    if resp.status_code in (401, 403):
        raise CentralAuthRejected("中心会话无效或已过期")
    if resp.status_code != 200:
        raise CentralAuthUnavailable(f"中心认证服务异常响应（HTTP {resp.status_code}）")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise CentralAuthUnavailable("中心认证服务返回非 JSON") from exc
    data = doc.get("data") if isinstance(doc, dict) else None
    user = (data or {}).get("user") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(user, dict) or not user.get("id"):
        raise CentralAuthRejected("中心会话无效")
    renewal = extract_renewal_cookie(resp.headers.get_list("set-cookie"))
    return data, renewal


def validate_central_session(cookie_value: str) -> tuple[dict, dict | None]:
    """校验中心会话，返回 (中心的 data 节点, 续期 Cookie 或 None)。

    data 节点形如 {user: {id, username, role?}, expiresAt}。
    - 中心明确未登录 -> CentralAuthRejected（KB 返回 401）。
    - 中心超时/5xx/非 JSON -> CentralAuthUnavailable（KB 返回 503）。

    不缓存校验结果：在记账侧退出、退出所有设备、管理员撤销，都在下一个请求立即生效。
    """
    settings = get_settings()
    if not settings.auth_session_url:
        raise CentralAuthUnavailable("未配置中心认证接口（AUTH_SESSION_URL）")
    return _run_with_retry(lambda deadline: _validate_once(cookie_value, deadline))


def _logout_once(cookie_value: str, deadline: float) -> None:
    """转发退出到中心 logout：固定目标、固定 Origin；调用方先完成 CSRF/Origin 校验。"""
    settings = get_settings()
    headers = _cookie_header(settings, cookie_value)
    if settings.auth_forward_origin:
        headers["Origin"] = settings.auth_forward_origin
    try:
        resp = _client.post(settings.auth_logout_url, timeout=_timeout_until(deadline),
                            headers=headers)
    except httpx.HTTPError as exc:
        raise CentralAuthUnavailable(f"中心认证服务不可达：{type(exc).__name__}") from exc
    if resp.status_code >= 500:
        raise CentralAuthUnavailable(f"中心认证服务错误（HTTP {resp.status_code}）")
    if resp.status_code in (401, 403):
        # 中心本来就不认这颗凭据：调用方要能把它和「暂时联系不上」区分开
        raise CentralAuthRejected("中心会话无效或已过期")


def central_logout(cookie_value: str) -> None:
    """撤销中心会话。重试只在「这次没撤销成」的不可用情形上发生，重复撤销是幂等的。"""
    settings = get_settings()
    if not settings.auth_logout_url:
        raise CentralAuthUnavailable("未配置中心退出接口（AUTH_LOGOUT_URL）")
    _run_with_retry(lambda deadline: _logout_once(cookie_value, deadline))


def extract_renewal_cookie(set_cookie_headers: list[str]) -> dict | None:
    """从中心响应的 Set-Cookie 中取出匹配预期名称/域/路径的续期 Cookie。

    返回 {value, max_age, domain, secure}；不匹配预期则丢弃（docs/05 §4.1 第 5 条）。
    """
    settings = get_settings()
    for header in set_cookie_headers:
        jar = SimpleCookie()
        try:
            jar.load(header)
        except Exception:
            continue
        morsel = jar.get(settings.auth_cookie_name)
        if morsel is None or not morsel.value:
            continue
        domain = (morsel["domain"] or "").lstrip(".").lower()
        expected_domain = (settings.auth_cookie_domain or "").lstrip(".").lower()
        if expected_domain:
            if domain != expected_domain:
                continue
        elif domain:
            continue  # 未配置预期域时，只接受无域属性（当前主机）的 Cookie
        path = morsel["path"] or "/"
        if path not in ("/", ""):
            continue
        max_age = None
        if morsel["max-age"]:
            try:
                max_age = int(morsel["max-age"])
            except ValueError:
                max_age = None
        return {
            "value": morsel.value,
            "max_age": max_age,
            "domain": domain or None,
            "secure": settings.public_base_url.startswith("https://"),
        }
    return None
