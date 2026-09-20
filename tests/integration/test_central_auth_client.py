"""中心认证客户端的失败处理策略（docs/05 §4.1）。

用 httpx.MockTransport 假装中心，不碰真实网络。盯的是两条互相拉扯的要求：
部署/重启那一两秒的抖动要吃掉（否则 KB 整站 503），但明确未登录（401）
一次都不许多试，也不许留下任何可被复用的「还算有效」的结论。
"""
from __future__ import annotations

import time

import httpx
import pytest

from kbserver.security import central_auth

OK_DOC = {"data": {"user": {"id": "u1", "username": "jerry"},
                   "expiresAt": "2026-09-20T00:00:00Z"}}


@pytest.fixture()
def central_env(monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_URL", "https://auth.example.com/api/auth/session")
    monkeypatch.setenv("AUTH_LOGOUT_URL", "https://auth.example.com/api/auth/logout")
    monkeypatch.setenv("AUTH_COOKIE_NAME", "test_session")
    monkeypatch.setenv("AUTH_COOKIE_DOMAIN", "")


def _use_transport(monkeypatch, handler) -> list[httpx.Request]:
    """换成经过 _new_client 的 MockTransport：连 Cookie jar 策略一起验，不走真实网络。"""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler()

    monkeypatch.setattr(central_auth, "_client",
                        central_auth._new_client(transport=httpx.MockTransport(record)))
    return requests


def test_only_the_one_credential_is_sent(monkeypatch, central_env):
    """只带这一颗凭据，不整串转发浏览器 Cookie，也不接受中心存下的任何 Cookie。"""
    requests = _use_transport(monkeypatch, lambda: httpx.Response(200, json=OK_DOC))
    central_auth.validate_central_session("cookie-value")
    assert requests[0].headers["cookie"] == "test_session=cookie-value"


def test_central_set_cookie_never_leaks_into_next_request(monkeypatch, central_env):
    """中心每次校验都回一颗滑动续期 Set-Cookie。共享客户端一旦把它收进 jar，
    下一个用户的请求就会带上上一个用户的会话凭据——这是必须为零的泄露面。
    """
    def handler():
        return httpx.Response(200, json=OK_DOC,
                              headers=[("set-cookie", "test_session=planted; Path=/")])

    requests = _use_transport(monkeypatch, handler)
    central_auth.validate_central_session("first-user-cookie")
    _, renewal = central_auth.validate_central_session("second-user-cookie")

    assert [r.headers["cookie"] for r in requests] == [
        "test_session=first-user-cookie",
        "test_session=second-user-cookie",
    ]
    assert not dict(central_auth._client.cookies)
    # 不收进 jar，但续期值仍要原样交给上层中继回浏览器，否则滑动续期就断了
    assert renewal is not None and renewal["value"] == "planted"


def test_transient_502_is_retried_once(monkeypatch, central_env):
    """中心刚重启、Caddy 回了个 502：补试一次就成功，不该把用户弹回登录页。"""
    responses = [httpx.Response(502, text="bad gateway"), httpx.Response(200, json=OK_DOC)]
    calls = _use_transport(monkeypatch, lambda: responses.pop(0))
    data, renewal = central_auth.validate_central_session("cookie-value")
    assert data["user"]["id"] == "u1"
    assert renewal is None
    assert len(calls) == 2


def test_rejected_is_never_retried(monkeypatch, central_env):
    """中心明确说这颗会话无效：一次都不许多问，重试等于给撤销留时间窗。"""
    calls = _use_transport(monkeypatch, lambda: httpx.Response(401))
    with pytest.raises(central_auth.CentralAuthRejected):
        central_auth.validate_central_session("cookie-value")
    assert len(calls) == 1


def test_persistent_error_stays_bounded(monkeypatch, central_env):
    """中心真挂了：最多两次就放弃，不许把请求线程一个接一个拖死。"""
    calls = _use_transport(monkeypatch, lambda: httpx.Response(500))
    with pytest.raises(central_auth.CentralAuthUnavailable):
        central_auth.validate_central_session("cookie-value")
    assert len(calls) == 2


def test_no_retry_budget_left_when_central_hangs(monkeypatch, central_env):
    """首次尝试已花光超时预算（中心挂起而非秒回失败）时不再补试，最坏耗时不翻倍。"""
    monkeypatch.setattr(central_auth, "_budget_seconds", lambda: 1.0)

    def hang():
        time.sleep(1.2)
        return httpx.Response(504)

    calls = _use_transport(monkeypatch, hang)
    with pytest.raises(central_auth.CentralAuthUnavailable):
        central_auth.validate_central_session("cookie-value")
    assert len(calls) == 1


def test_logout_retries_because_a_missed_revoke_is_a_live_session(monkeypatch, central_env):
    """退出时中心不可达必须补试：漏掉一次撤销 = 会话仍然有效，正是要求里最糟的那种。"""
    responses = [httpx.Response(502), httpx.Response(200)]
    calls = _use_transport(monkeypatch, lambda: responses.pop(0))
    central_auth.central_logout("cookie-value")
    assert len(calls) == 2
