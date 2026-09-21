"""中心认证客户端的失败处理策略（docs/05 §4.1）。

用 httpx.MockTransport 假装中心，不碰真实网络。盯的是两条互相拉扯的要求：
部署/重启那一两秒的抖动要吃掉（否则 KB 整站 503），但明确未登录（401）
一次都不许多试，也不许留下任何可被复用的「还算有效」的结论。
最后两条走完整应用：退出这件事不能因为中心不可达而变成「点了没退出」，
也不能被一条晚落地的在飞续期响应把凭据种回浏览器（审查 C-08）。
"""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
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


def test_logout_clears_local_state_even_if_central_is_unreachable(monkeypatch, central_env, engine):
    """中心重启那一两秒点退出：本地要真的退出去，不能只回一个 503 就什么都不做。

    原来 logout 依赖 current_principal，中心不可达时在依赖里先抛 503，处理器根本不
    进来——既不记撤销也不清 Cookie，前端吞掉异常照样 showLogin（审查 C-08）。
    写操作防护不因此放松：少了 CSRF 双提交照样 403，没有凭据照样 401。
    """
    calls = _use_transport(monkeypatch, lambda: httpx.Response(500))
    client = TestClient(create_app())
    client.cookies.set("test_session", "unreachable-logout")
    client.cookies.set("kb_csrf", "csrf-token")
    resp = client.post("/v1/auth/logout", headers={"X-CSRF-Token": "csrf-token"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["logged_out"] is True
    assert len(calls) == 2, "本地清理之后仍然尽力通知过中心（不可达补试一次）"
    assert central_auth.is_revoked("unreachable-logout")
    planted = " ".join(resp.headers.get_list("set-cookie")).lower()
    assert "test_session=" in planted, "没把会话 Cookie 过期掉"
    assert "kb_csrf=" in planted, "没清 CSRF Cookie"
    # CSRF 与 Origin 校验仍然生效
    bare = TestClient(create_app())
    bare.cookies.set("test_session", "unreachable-logout-2")
    assert bare.post("/v1/auth/logout").status_code == 403
    anon = TestClient(create_app())
    anon.cookies.set("kb_csrf", "csrf-token")
    no_cred = anon.post("/v1/auth/logout", headers={"X-CSRF-Token": "csrf-token"})
    assert no_cred.status_code == 401


def test_logout_reports_401_when_central_says_the_session_was_already_dead(
        monkeypatch, central_env, engine):
    """中心明确不认这颗凭据（401）：本地照清，但如实按 401 回，也不补试。"""
    calls = _use_transport(monkeypatch, lambda: httpx.Response(401))
    client = TestClient(create_app())
    client.cookies.set("test_session", "already-out")
    client.cookies.set("kb_csrf", "csrf-token")
    resp = client.post("/v1/auth/logout", headers={"X-CSRF-Token": "csrf-token"})
    assert resp.status_code == 401, resp.text
    assert len(calls) == 1, "明确未登录一次都不许多问"
    assert central_auth.is_revoked("already-out")
    assert "test_session=" in " ".join(resp.headers.get_list("set-cookie"))


def test_in_flight_renewal_must_not_resurrect_a_revoked_credential(monkeypatch, central_env, engine):
    """晚落地的在飞续期响应不得覆盖已撤销凭据（审查 C-08 第一处）。

    中心校验要一个公网 RTT；这期间用户点了退出，响应回来时那颗凭据已经作废，
    中间件按域写回去就等于把刚 delete_cookie 掉的钥匙又塞回浏览器——KB 侧靠 5 分钟
    撤销表挡得住，auth 站与同域其他应用挡不住。
    """
    real = central_auth.validate_central_session   # 打补丁之前先抓住真的那一个

    def _me(cookie: str, *, in_flight_logout: bool) -> httpx.Response:
        """走一次 Cookie 通道的 /v1/auth/me：中心每次都回一颗同值的滑动续期 Set-Cookie。"""
        _use_transport(monkeypatch, lambda: httpx.Response(
            200, json=OK_DOC, headers=[("set-cookie", f"test_session={cookie}; Path=/; Max-Age=3600")]))

        def validate_then_logout(cookie_value: str):
            data, renewal = real(cookie_value)
            if renewal:   # 校验与写响应之间，这次退出完成了
                central_auth.remember_revoked(renewal["value"])
            return data, renewal

        # 第二次调用要显式换回真的那个：monkeypatch 是整条用例结束才撤销的
        monkeypatch.setattr(central_auth, "validate_central_session",
                            validate_then_logout if in_flight_logout else real)
        client = TestClient(create_app())
        client.cookies.set("test_session", cookie)
        resp = client.get("/v1/auth/me")
        assert resp.status_code == 200, resp.text
        return resp

    resp = _me("in-flight-renewal", in_flight_logout=True)
    planted = " ".join(resp.headers.get_list("set-cookie"))
    # 会话 Cookie 不在响应里（补发 kb_csrf 不在这条要求之内）
    assert "test_session=" not in planted, f"已撤销的凭据被种回浏览器：{planted}"
    # 对照：没被撤销时滑动续期照常写回，别把这条路整个写死
    live = _me("still-live-session", in_flight_logout=False)
    assert "test_session=still-live-session" in " ".join(live.headers.get_list("set-cookie"))
