"""M1 可靠接收 + M2 Worker 基础语义的集成测试。

覆盖验收场景：A02（幂等重试）、A04（落盘后 202）、A06（跨用户隔离）、
A07/A08 前置（waiting_key 不丢材料）、回执幂等与 consumer_epoch、事件游标。
"""
from __future__ import annotations

import hashlib
import json

from tests.conftest import auth, make_user_with_tokens


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 健康检查 ----

def test_health_live(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"live": True}


# ---- 配对（docs/05 §4.5：配对交换接口已关闭，改为浏览器设备授权） ----

def test_pairing_exchange_disabled(db, client, session_factory):
    from kbserver.security.tokens import PHONE_SCOPES, issue_pairing_code

    user = make_user_with_tokens(db, "配对用户")
    raw_code, code_row = issue_pairing_code(user["user_id"], "phone", PHONE_SCOPES)
    db.add(code_row)
    db.commit()

    r = client.post("/v1/pairing/exchange", json={"code": raw_code, "device_name": "测试手机"})
    assert r.status_code == 404, "配对交换接口已随统一登录下线"


# ---- 上传与 Capture 幂等（A02/A03） ----

def _do_upload(client, token, content: bytes, filename="a.txt", key="up-key-1"):
    return client.post(
        "/v1/uploads",
        files={"file": (filename, content, "text/plain")},
        headers={**auth(token), "Idempotency-Key": key},
    )


def test_upload_and_capture_idempotency(client, user_a):
    token = user_a["phone"]["token"]
    content = b"hello, capture body"

    r1 = _do_upload(client, token, content)
    assert r1.status_code == 201
    up = r1.json()
    assert up["sha256"] == _sha(content)

    # 幂等上传：同键同内容返回同一 upload_id
    r2 = _do_upload(client, token, content, key="up-key-1")
    assert r2.json()["upload_id"] == up["upload_id"]

    capture_body = {
        "client_capture_id": "11111111-2222-3333-4444-555555555555",
        "input_kind": "text",
        "text": "先保存材料，再处理总结失败的问题。",
        "upload_ids": [up["upload_id"]],
    }
    headers = {**auth(token), "Idempotency-Key": "cap-key-1"}
    c1 = client.post("/v1/captures", json=capture_body, headers=headers)
    assert c1.status_code == 202
    first = c1.json()
    assert first["durable"] is True

    # 同键同内容 → 同一条目
    c2 = client.post("/v1/captures", json=capture_body, headers=headers)
    assert c2.status_code == 202
    assert c2.json()["capture_id"] == first["capture_id"]
    assert c2.json()["item_id"] == first["item_id"]

    # 同键不同内容 → 409
    other = {**capture_body, "text": "不同的内容"}
    c3 = client.post("/v1/captures", json=other, headers=headers)
    assert c3.status_code == 409
    assert c3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # 同 client_capture_id、新幂等键 → 仍不新建 Item（docs/02 §6.2）
    c4 = client.post("/v1/captures", json=capture_body, headers={**auth(token), "Idempotency-Key": "cap-key-2"})
    assert c4.status_code == 202
    assert c4.json()["item_id"] == first["item_id"]

    # 主动再次保存：新 client_capture_id → 新条目
    c5 = client.post(
        "/v1/captures",
        json={**capture_body, "client_capture_id": "66666666-2222-3333-4444-555555555555"},
        headers={**auth(token), "Idempotency-Key": "cap-key-3"},
    )
    assert c5.status_code == 202
    assert c5.json()["item_id"] != first["item_id"]


def test_capture_requires_at_least_one_input(client, user_a):
    token = user_a["phone"]["token"]
    r = client.post(
        "/v1/captures",
        json={"client_capture_id": "99999999-2222-3333-4444-555555555555", "input_kind": "text"},
        headers={**auth(token), "Idempotency-Key": "cap-empty"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SCHEMA_INVALID"


# ---- 落盘后 202：原始材料可下载（A04） ----

def test_capture_manifest_and_files(client, user_a):
    token = user_a["phone"]["token"]
    content = b"attachment-content-bytes"
    up = _do_upload(client, token, content, filename="notes.txt", key="up-key-manifest").json()

    c = client.post(
        "/v1/captures",
        json={
            "client_capture_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "input_kind": "text",
            "text": "演示正文",
            "upload_ids": [up["upload_id"]],
            "user_note": "我的备注",
        },
        headers={**auth(token), "Idempotency-Key": "cap-manifest"},
    )
    item_id = c.json()["item_id"]

    m = client.get(f"/v1/items/{item_id}/bundles/1/manifest", headers=auth(user_a["desktop"]["token"]))
    assert m.status_code == 200
    manifest = m.json()
    assert m.headers["X-Manifest-SHA256"] == _sha(m.content)
    assert manifest["bundle_revision"] == 1
    paths = {f["relative_path"] for f in manifest["files"]}
    assert "capture.json" in paths
    assert any(p.startswith("uploads/") for p in paths)

    # 逐文件下载并校验摘要
    for f in manifest["files"]:
        fr = client.get(
            f"/v1/items/{item_id}/bundles/1/files/{f['file_id']}", headers=auth(user_a["desktop"]["token"])
        )
        assert fr.status_code == 200
        assert _sha(fr.content) == f["sha256"]

    # capture.json 包含原始提交
    cap_file = next(f for f in manifest["files"] if f["relative_path"] == "capture.json")
    cap_content = client.get(
        f"/v1/items/{item_id}/bundles/1/files/{cap_file['file_id']}", headers=auth(user_a["desktop"]["token"])
    ).content
    assert "我的备注" in cap_content.decode("utf-8")


# ---- 租户隔离（A06） ----

def test_tenant_isolation(client, user_a, user_b):
    token = user_a["phone"]["token"]
    c = client.post(
        "/v1/captures",
        json={"client_capture_id": "bbbbbbbb-2222-3333-4444-555555555555", "input_kind": "text", "text": "A 的私有内容"},
        headers={**auth(token), "Idempotency-Key": "cap-iso"},
    )
    item_id = c.json()["item_id"]

    # B 的任何 Token 访问 A 的条目 → 404（不区分不存在与他人对象）
    assert client.get(f"/v1/items/{item_id}", headers=auth(user_b["desktop"]["token"])).status_code == 404
    assert client.get(f"/v1/items/{item_id}/bundles/1/manifest", headers=auth(user_b["desktop"]["token"])).status_code == 404
    assert client.get(
        f"/v1/items/{item_id}/bundles/1/files/capture-000000000000", headers=auth(user_b["desktop"]["token"])
    ).status_code == 404

    # B 猜文件 ID 也拿不到（file_id 查询绑定 user_id）
    fr = client.get(f"/v1/items/{item_id}/bundles/1/files/whatever", headers=auth(user_b["desktop"]["token"]))
    assert fr.status_code == 404


# ---- Worker：文本条目生成规范稿并进入 waiting_key（A07 前置） ----

def _drain_worker(session_factory, max_rounds=10):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def test_worker_text_flow(client, user_a, session_factory):
    token = user_a["phone"]["token"]
    c = client.post(
        "/v1/captures",
        json={
            "client_capture_id": "cccccccc-2222-3333-4444-555555555555",
            "input_kind": "text",
            "text": "第一段：可靠保存。\n第二段：AI 加工不丢材料。",
            "original_url": "https://example.com/article/1",
        },
        headers={**auth(token), "Idempotency-Key": "cap-worker"},
    )
    item_id = c.json()["item_id"]
    _drain_worker(session_factory)

    it = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    # 无模型凭据 → waiting_key，材料保留（不伪装 ready）
    assert it["pipeline_state"] == "waiting_key"

    # 最新 Bundle 含 normalized.md 与 segments.json
    rev = it["bundle_revision"]
    m = client.get(f"/v1/items/{item_id}/bundles/{rev}/manifest", headers=auth(user_a["desktop"]["token"])).json()
    paths = {f["relative_path"] for f in m["files"]}
    assert "normalized.md" in paths
    assert "segments.json" in paths

    norm = next(f for f in m["files"] if f["relative_path"] == "normalized.md")
    content = client.get(
        f"/v1/items/{item_id}/bundles/{rev}/files/{norm['file_id']}", headers=auth(user_a["desktop"]["token"])
    ).content.decode("utf-8")
    assert "^s0001" in content and "可靠保存" in content

    # segments.json 结构符合契约
    seg = next(f for f in m["files"] if f["relative_path"] == "segments.json")
    seg_doc = json.loads(
        client.get(f"/v1/items/{item_id}/bundles/{rev}/files/{seg['file_id']}", headers=auth(user_a["desktop"]["token"])).content
    )
    assert seg_doc["segments"][0]["segment_id"] == "s0001"


def test_worker_url_only_needs_input(client, user_a, session_factory, monkeypatch):
    """只有链接且页面不可达：不伪造正文，进入待补充（M4 适配器失败降级路径）。"""
    from kbserver.extractors import webpages
    from kbserver.security.safe_fetch import SafeFetchError

    def _blocked(*args, **kwargs):
        raise SafeFetchError("SOURCE_BLOCKED", "页面无法访问")
    monkeypatch.setattr(webpages, "safe_fetch", _blocked)

    token = user_a["phone"]["token"]
    c = client.post(
        "/v1/captures",
        json={
            "client_capture_id": "dddddddd-2222-3333-4444-555555555555",
            "input_kind": "url",
            "original_url": "https://example.com/no-text",
        },
        headers={**auth(token), "Idempotency-Key": "cap-url-only"},
    )
    item_id = c.json()["item_id"]
    _drain_worker(session_factory)

    it = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    # 只有链接：不伪造正文，进入待补充
    assert it["pipeline_state"] == "needs_input"
    assert "main_content" in it["missing_materials"]


# ---- 回执（A09/A10）与 consumer_epoch ----

def test_receipt_flow_and_epoch(client, user_a, session_factory):
    token = user_a["phone"]["token"]
    c = client.post(
        "/v1/captures",
        json={"client_capture_id": "eeeeeeee-2222-3333-4444-555555555555", "input_kind": "text", "text": "回执测试"},
        headers={**auth(token), "Idempotency-Key": "cap-receipt"},
    )
    item_id = c.json()["item_id"]
    _drain_worker(session_factory)

    desktop = user_a["desktop"]["token"]
    m = client.get(f"/v1/items/{item_id}/bundles/1/manifest", headers=auth(desktop))
    sha = m.headers["X-Manifest-SHA256"]

    r1 = client.post(
        "/v1/receipts",
        json={"item_id": item_id, "bundle_revision": 1, "manifest_sha256": sha, "local_commit_id": "commit-1"},
        headers=auth(desktop),
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "stored"

    # 重复回执幂等
    r2 = client.post(
        "/v1/receipts",
        json={"item_id": item_id, "bundle_revision": 1, "manifest_sha256": sha, "local_commit_id": "commit-1"},
        headers=auth(desktop),
    )
    assert r2.status_code == 200

    # 错误摘要 → 409
    r3 = client.post(
        "/v1/receipts",
        json={"item_id": item_id, "bundle_revision": 1, "manifest_sha256": "f" * 64, "local_commit_id": "commit-1"},
        headers=auth(desktop),
    )
    assert r3.status_code == 409

    # 新桌面设备接管（递增 consumer_epoch）后，旧设备回执被拒
    from tests.conftest import make_user_with_tokens

    # 在 A 名下再配对一台桌面设备
    from kbserver.models import Device
    from kbserver.security.tokens import DESKTOP_SCOPES, issue_token

    db = session_factory()
    device2 = Device(user_id=user_a["user_id"], kind="desktop", name="新电脑")
    db.add(device2)
    db.flush()
    raw2, tok2 = issue_token(user_a["user_id"], device2.id, DESKTOP_SCOPES)
    db.add(tok2)
    db.commit()

    act = client.post(f"/v1/devices/{device2.id}/activate-consumer", headers=auth(raw2))
    assert act.status_code == 200

    r4 = client.post(
        "/v1/receipts",
        json={"item_id": item_id, "bundle_revision": 1, "manifest_sha256": sha, "local_commit_id": "commit-2"},
        headers=auth(desktop),
    )
    assert r4.status_code == 409
    assert r4.json()["error"]["code"] == "REVISION_CONFLICT"


# ---- 事件游标 ----

def test_events_cursor(client, user_a, session_factory):
    token = user_a["phone"]["token"]
    client.post(
        "/v1/captures",
        json={"client_capture_id": "ffffffff-2222-3333-4444-555555555555", "input_kind": "text", "text": "事件测试"},
        headers={**auth(token), "Idempotency-Key": "cap-events"},
    )
    _drain_worker(session_factory)

    e1 = client.get("/v1/events?after=0&limit=2", headers=auth(user_a["desktop"]["token"])).json()
    assert e1["events"], "应至少有一个事件"
    assert e1["has_more"] is True
    assert e1["next_cursor"] == e1["events"][-1]["seq"]

    e2 = client.get(f"/v1/events?after={e1['next_cursor']}", headers=auth(user_a["desktop"]["token"])).json()
    seqs1 = {e["seq"] for e in e1["events"]}
    seqs2 = {e["seq"] for e in e2["events"]}
    assert not (seqs1 & seqs2), "游标分页不应重复"

    # 按用户过滤：B 看不到 A 的事件
    e3 = client.get("/v1/events", headers=auth(user_b_token(client, session_factory))).json()
    all_seqs_b = {e["seq"] for e in e3["events"]}
    assert seqs1.isdisjoint(all_seqs_b | seqs2)


def user_b_token(client, session_factory):
    db = session_factory()
    try:
        return make_user_with_tokens(db, "事件隔离用户")["desktop"]["token"]
    finally:
        db.close()


# ---- 删除条目（A21 服务器侧） ----

def test_delete_item(client, user_a):
    token = user_a["phone"]["token"]
    c = client.post(
        "/v1/captures",
        json={"client_capture_id": "abababab-2222-3333-4444-555555555555", "input_kind": "text", "text": "删除测试"},
        headers={**auth(token), "Idempotency-Key": "cap-delete"},
    )
    item_id = c.json()["item_id"]
    d = client.delete(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"]))
    assert d.status_code == 200

    assert client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).status_code == 410
    # tombstone 后禁止文件访问
    assert client.get(f"/v1/items/{item_id}/bundles/1/manifest", headers=auth(user_a["desktop"]["token"])).status_code == 410
