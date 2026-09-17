"""平台登录态托管测试（docs/18 §7.2 P2 底座 + §8.1 相关验收项）。

覆盖：三平台会话保存/更新/检测/撤销、Cookie 清洗与拒绝规则、明文只进不出、
跨用户隔离、按登录原因定向重排队（不重跑适配器未上线或他人条目）、
B 站旧接口兼容、kind 列 40 字符容量。
"""
from __future__ import annotations

import json

from kbserver.domain import platform_sessions
from kbserver.workers import worker

from tests.conftest import auth


ZHIHU_COOKIE = "z_c0=fake-zhihu-login-value-1234567890; d_c0=fake-device-value-0987654321; __uid=fakeuid"


def _put_session(client, token, platform, secret):
    return client.put(f"/v1/platform-sessions/{platform}", json={"secret": secret},
                      headers=auth(token))


def _get_session(client, token, platform):
    return client.get(f"/v1/platform-sessions/{platform}", headers=auth(token))


def _drain(session_factory, max_rounds: int = 20) -> None:
    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _capture_url(client, token, key: str, url: str):
    return client.post(
        "/v1/captures",
        json={"client_capture_id": f"{key}-1111-2222-3333-444444444444",
              "input_kind": "url", "original_url": url},
        headers={**auth(token), "Idempotency-Key": key},
    )


# ---- 保存 / 读取 / 不回显明文 ----

def test_put_and_get_zhihu_session_roundtrip(client, user_a):
    r = _put_session(client, user_a["desktop"]["token"], "zhihu", ZHIHU_COOKIE)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["platform"] == "zhihu"
    assert out["configured"] is True
    assert out["credential_version"] == 1
    assert out["verification"] == "unverified"

    g = _get_session(client, user_a["desktop"]["token"], "zhihu")
    assert g.status_code == 200
    body = g.json()
    # 任何响应不含明文（docs/18 §7.2 规则 3）
    for frag in ("fake-zhihu-login-value", "fake-device-value"):
        assert frag not in json.dumps(body)


def test_session_value_decrypts_cookie_dict(db, client, user_a):
    from kbserver.domain.platform_sessions import SPECS

    r = _put_session(client, user_a["desktop"]["token"], "zhihu", ZHIHU_COOKIE)
    assert r.status_code == 200
    value, err = platform_sessions.session_value(db, user_a["user_id"], SPECS["zhihu"])
    assert err is None
    assert value == {"z_c0": "fake-zhihu-login-value-1234567890",
                     "d_c0": "fake-device-value-0987654321",
                     "__uid": "fakeuid"}


def test_update_creates_new_version_and_revokes_old(client, user_a, db):
    p1 = _put_session(client, user_a["desktop"]["token"], "zhihu", ZHIHU_COOKIE)
    assert p1.json()["credential_version"] == 1
    p2 = _put_session(client, user_a["desktop"]["token"], "zhihu",
                      "z_c0=fake-rotated-value-abcdefabcdef")
    assert p2.status_code == 200
    assert p2.json()["credential_version"] == 2

    # 只有一个活跃凭据：旧版本已撤销（docs/18 §7.2 规则 3）
    from kbserver.models import Credential, ProviderProfile

    profile = (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_a["user_id"],
                ProviderProfile.kind == "zhihu_session")
        .one()
    )
    creds = db.query(Credential).filter(Credential.profile_id == profile.id).all()
    assert len(creds) == 2
    active = [c for c in creds if c.revoked_at is None]
    assert len(active) == 1 and active[0].version == 2

    # 新值生效
    value, err = platform_sessions.session_value(db, user_a["user_id"], platform_sessions.SPECS["zhihu"])
    assert err is None and value == {"z_c0": "fake-rotated-value-abcdefabcdef"}


# ---- Cookie 清洗与拒绝 ----

def test_cookie_rejections(client, user_a):
    token = user_a["desktop"]["token"]
    # 换行
    r = _put_session(client, token, "zhihu", "z_c0=abc\nReferer=x")
    assert r.status_code == 422
    # 重复字段名
    r = _put_session(client, token, "zhihu", "z_c0=aaa; z_c0=bbb")
    assert r.status_code == 422
    # 其他平台登录字段（SESSDATA 是 B 站凭据）
    r = _put_session(client, token, "zhihu", "SESSDATA=fake-sessdata-value-1234567890")
    assert r.status_code == 422
    # 超长字段值
    r = _put_session(client, token, "zhihu", "z_c0=" + "x" * 5000)
    assert r.status_code == 413
    # 非法字段名字符
    r = _put_session(client, token, "zhihu", "z/c0=abc-def-ghi-jkl")
    assert r.status_code == 422
    # 错误信息不回显原文
    assert "fake-sessdata" not in r.text


def test_platform_enum_is_fixed(client, user_a):
    token = user_a["desktop"]["token"]
    # B 站不在新端点：指向旧接口
    assert _get_session(client, token, "bilibili").status_code == 404
    # 未知平台拒绝
    assert _get_session(client, token, "twitter").status_code == 404
    # 三个新平台全部可用
    for platform in ("xiaohongshu", "wechat_channels", "zhihu"):
        r = _put_session(client, token, platform, f"sid=fake-{platform}-session-value-01")
        assert r.status_code == 200, (platform, r.text)
        assert r.json()["configured"] is True


def test_wechat_channels_session_kind_fits_column(db, client, user_a):
    """wechat_channels_session 长 23 字符：kind 列扩到 40 后可落库（docs/18 §7.2）。"""
    from kbserver.models import ProviderProfile

    assert len("wechat_channels_session") == 23
    r = _put_session(client, user_a["desktop"]["token"], "wechat_channels",
                     "sid=fake-wx-channels-session-01")
    assert r.status_code == 200, r.text
    kinds = [p.kind for p in db.query(ProviderProfile).filter(
        ProviderProfile.user_id == user_a["user_id"]).all()]
    assert "wechat_channels_session" in kinds
    assert len(max(kinds, key=len)) <= 40


# ---- 撤销 ----

def test_revoke_returns_to_anonymous(client, user_a, db):
    token = user_a["desktop"]["token"]
    _put_session(client, token, "zhihu", ZHIHU_COOKIE)
    d = client.delete("/v1/platform-sessions/zhihu", headers=auth(token))
    assert d.status_code == 200
    g = _get_session(client, token, "zhihu")
    assert g.json()["configured"] is False
    value, err = platform_sessions.session_value(db, user_a["user_id"], platform_sessions.SPECS["zhihu"])
    assert value is None and err is None


# ---- 检测 ----

def test_test_endpoint_unconfigured_and_probe(client, user_a, monkeypatch):
    token = user_a["desktop"]["token"]
    # 未托管时发起检测 → 422
    r0 = client.post("/v1/platform-sessions/zhihu/test", headers=auth(token))
    assert r0.status_code == 422

    _put_session(client, token, "zhihu", ZHIHU_COOKIE)

    from kbserver.api import routes_platform_sessions as mod
    from kbserver.security.safe_fetch import FetchResult, SafeFetchError

    # 平台拒绝访问 → blocked，明文不出现在结果里
    def fake_blocked(url, **kwargs):
        raise SafeFetchError("SOURCE_BLOCKED", "平台拒绝访问")

    monkeypatch.setattr(mod, "safe_fetch", fake_blocked)
    r1 = client.post("/v1/platform-sessions/zhihu/test", headers=auth(token))
    assert r1.status_code == 200
    check = r1.json()
    assert check["status"] == "blocked"
    assert "fake-zhihu" not in json.dumps(check)
    assert "checked_at" in check

    # 页面可达 → 如实 unverified，不伪造 valid（docs/18 §5.3）
    def fake_ok(url, **kwargs):
        return FetchResult(url=url, status_code=200, mime="text/html", content=b"<html>ok</html>")

    monkeypatch.setattr(mod, "safe_fetch", fake_ok)
    r2 = client.post("/v1/platform-sessions/zhihu/test", headers=auth(token))
    assert r2.json()["status"] == "unverified"

    # 检测结果已写入状态（脱敏）
    g = _get_session(client, token, "zhihu")
    assert g.json()["verification"] == "unverified"
    assert g.json()["last_check"]["status"] == "unverified"


# ---- 隔离与定向重排队 ----

def test_session_isolation_between_users(client, user_a, user_b, db):
    _put_session(client, user_a["desktop"]["token"], "zhihu", ZHIHU_COOKIE)
    # B 用户未配置
    g = _get_session(client, user_b["desktop"]["token"], "zhihu")
    assert g.json()["configured"] is False
    # B 用户读不到 A 的凭据（docs/18 §8.1：A 的登录态绝不用于 B 用户）
    value, err = platform_sessions.session_value(db, user_b["user_id"], platform_sessions.SPECS["zhihu"])
    assert value is None and err is None


def test_requeue_only_login_reason_items(client, user_a, user_b, session_factory, db, monkeypatch):
    """更新登录态只重排同平台、因登录原因等待的条目（docs/18 §7.2 规则 7）。"""
    from kbserver.extractors import fetch_base
    from kbserver.security.safe_fetch import SafeFetchError

    def _blocked(*args, **kwargs):
        raise SafeFetchError("SOURCE_BLOCKED", "测试固定拒绝（确定性降级，不触真实网络）")

    monkeypatch.setattr(fetch_base, "safe_fetch", _blocked)
    token = user_a["desktop"]["token"]
    # 1) 适配器未上线降级的知乎条目：非登录原因 → 不重排
    c1 = _capture_url(client, token, "ps1zhihu", "https://www.zhihu.com/question/1/answer/2")
    item1 = c1.json()["item_id"]
    _drain(session_factory)
    # 2) 登录原因等待的知乎条目
    c2 = _capture_url(client, token, "ps2zhihu", "https://www.zhihu.com/question/3/answer/4")
    item2 = c2.json()["item_id"]
    _drain(session_factory)
    from kbserver.models import Item

    it2 = db.get(Item, item2)
    it2.state_detail = "页面要求登录后才能读取，请连接知乎后重试。"
    db.commit()
    # 3) 他人（用户 B）的登录等待条目：不被 A 的登录态重排
    c3 = _capture_url(client, user_b["desktop"]["token"], "ps3zhihu",
                      "https://www.zhihu.com/question/5/answer/6")
    item3 = c3.json()["item_id"]
    _drain(session_factory)
    it3 = db.get(Item, item3)
    it3.state_detail = "页面要求登录后才能读取，请连接知乎后重试。"
    db.commit()
    # 4) 公众号条目：不同平台，不因知乎登录态重排
    c4 = _capture_url(client, token, "ps4mp", "https://mp.weixin.qq.com/s/abc")
    item4 = c4.json()["item_id"]
    _drain(session_factory)
    it4 = db.get(Item, item4)
    it4.state_detail = "页面要求登录后才能读取，请连接平台后重试。"
    db.commit()

    r = _put_session(client, token, "zhihu", ZHIHU_COOKIE)
    assert r.status_code == 200
    assert r.json()["requeued_items"] == 1  # 只有 item2

    # item2 的提取任务被重新排队；item1/item4 的任务保持原状
    from kbserver.models import Job

    def _extract_job(item_id):
        return db.query(Job).filter(Job.item_id == item_id, Job.stage == "extract").one()

    assert _extract_job(item2).state == "queued"
    assert _extract_job(item1).state == "succeeded"
    assert _extract_job(item4).state == "succeeded"
    # 重排队仍走同一来源版本（定向，不重跑全部内容）
    assert _extract_job(item2).source_revision == 1

    # 收尾：不留 queued 任务污染共享库（全量回归中后续文件的 _drain 会捡起执行）
    _extract_job(item2).state = "succeeded"
    db.commit()


def test_session_events_do_not_contain_secrets(client, user_a, db):
    _put_session(client, user_a["desktop"]["token"], "zhihu", ZHIHU_COOKIE)
    from kbserver.models import Event

    rows = db.query(Event).filter(Event.event_type == "platform_session_updated").all()
    assert rows, "缺少 platform_session_updated 事件"
    for row in rows:
        payload = json.dumps(row.payload_json, ensure_ascii=False)
        assert "fake-zhihu-login-value" not in payload
        assert set(row.payload_json) <= {"platform", "credential_version", "requeued"}


def test_bilibili_legacy_endpoint_still_works(client, user_a):
    """B 站旧接口保持兼容：保存/读取语义不变（docs/18 §7.2）。"""
    token = user_a["desktop"]["token"]
    p = client.put("/v1/bilibili-session", json={"secret": "fake-sessdata-value-123456"},
                   headers=auth(token))
    assert p.status_code == 200
    assert p.json()["configured"] is True
    g = client.get("/v1/bilibili-session", headers=auth(token))
    assert g.json()["verification"] == "unverified"
    assert "fake-sessdata" not in json.dumps(g.json())
