"""设备 last_seen_at 心跳。

2026-09-15 修复：字段此前从未被写入，设置页「最近连接」恒显示 —。
现在设备 Bearer 鉴权成功即视为在线（60 秒节流防轮询写放大）。
"""
from __future__ import annotations

import pytest

from kbserver.models import Device


@pytest.fixture()
def wc(client):
    return client


def _bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def test_device_bearer_request_updates_last_seen(wc, user_a, db):
    token = user_a["desktop"]["token"]
    r = wc.get("/v1/devices/summary", headers=_bearer(token))
    assert r.status_code == 200
    db.expire_all()
    device = db.get(Device, user_a["desktop"]["device_id"])
    assert device.last_seen_at is not None
    # 列表接口把时间带给前端（settings.js 的展示数据源）
    listed = wc.get("/v1/devices", headers=_bearer(token)).json()
    row = next(d for d in listed if d["device_id"] == device.id)
    assert row["last_seen_at"] is not None


def test_device_last_seen_throttled_within_60s(wc, user_a, db):
    token = user_a["desktop"]["token"]
    hdr = _bearer(token)
    wc.get("/v1/devices/summary", headers=hdr)
    db.expire_all()
    first = db.get(Device, user_a["desktop"]["device_id"]).last_seen_at
    wc.get("/v1/devices/summary", headers=hdr)
    db.expire_all()
    second = db.get(Device, user_a["desktop"]["device_id"]).last_seen_at
    assert first is not None
    assert first == second  # 60 秒内不重复写库
