"""原文编辑（POST /v1/items/{id}/source-text）契约测试。

验收：
- 编辑生成新的不可变来源版本与 Bundle；旧版本与其提炼结果保留；
- expected_source_revision 冲突 409；空正文 422；跨用户 404；
- 内容无变化不新增版本（幂等）；
- 「AI 自动加工」开启时编辑后自动重新提炼，新提炼基于编辑后的片段；
- 关闭时停在 extracted，等待手动加工。
"""
from __future__ import annotations

from tests.conftest import auth

from tests.integration.test_m2 import FakeProvider, chunk_aware_behavior


def _drain(session_factory, max_rounds=20):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _create_profile(client, token):
    return client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible",
        "endpoint": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        "secret": "sk-edit-test-1234567890",
    }, headers=auth(token))


def _capture(client, token, key, text):
    return client.post("/v1/captures", json={
        "client_capture_id": f"{key}-1111-2222-3333-444444444444",
        "input_kind": "text", "text": text,
    }, headers={**auth(token), "Idempotency-Key": key})


def test_source_edit_creates_new_revision_and_reruns_digest(
    client, user_a, session_factory, monkeypatch,
):
    """编辑后新版本生效；旧提炼对应旧来源（stale 提示）；自动重新加工基于编辑文本。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture(client, user_a["phone"]["token"], "editbase", "原始的第一段。\n原始的第二段。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    before = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    assert before["cloud_digest"]["state"] == "ready"
    old_revision = before["item"]["source_revision"]

    r = client.post(f"/v1/items/{item_id}/source-text", json={
        "expected_source_revision": old_revision,
        "text": "编辑后的第一段。\n## 小标题\n编辑后的第二段。",
    }, headers=auth(token))
    assert r.status_code == 202
    assert r.json()["source_revision"] == old_revision + 1

    body = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    sm = body["source_material"]
    assert body["item"]["source_revision"] == old_revision + 1
    # 编辑后的正文生效（块 ID 由服务端重新分配；标题行保留 ## 形态）
    assert "编辑后的第一段" in sm["readable_md"]
    assert "## 小标题" in sm["readable_md"]
    assert "原始的第一段" not in sm["readable_md"]
    # 旧提炼仍对应旧来源：显式提示，不混用新片段
    g = body["cloud_digest"]
    assert g["state"] == "ready"
    assert g["source_revision"] == old_revision
    assert g["stale_note"] and f"r{old_revision}" in g["stale_note"]
    assert all("原始的" in t for t in g["segments"].values())
    # 新 Bundle 警告注明编辑
    assert any("编辑" in w for w in sm["warnings"])

    # 「AI 自动加工」默认开启：编辑后自动重新提炼，且新提炼基于 r(n+1) 的编辑文本
    _drain(session_factory)
    body2 = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    g2 = body2["cloud_digest"]
    assert g2["state"] == "ready"
    assert g2["source_revision"] == old_revision + 1
    assert g2["stale_note"] is None
    assert g2["segments"] and all(
        ("编辑后" in t) or ("小标题" in t) for t in g2["segments"].values()
    )


def test_source_edit_no_change_keeps_revision(client, user_a, session_factory, monkeypatch):
    """提交与当前原文一致的文本：不新增版本（幂等）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    c = _capture(client, user_a["phone"]["token"], "editsame", "第一段原文。\n第二段原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    before = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    # 预填文本即当前原文去块 ID 的形态：原样提交应判定无变化
    text = "\n".join(
        line for line in (before["source_material"]["readable_md"] or "").splitlines()
    )
    r = client.post(f"/v1/items/{item_id}/source-text", json={
        "expected_source_revision": before["item"]["source_revision"], "text": text,
    }, headers=auth(token))
    assert r.status_code == 202
    assert r.json()["source_revision"] == before["item"]["source_revision"]


def test_source_edit_rejects_bad_requests(client, user_a, session_factory, monkeypatch):
    """版本冲突 409；空正文 422。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    c = _capture(client, user_a["phone"]["token"], "editbad", "会被编辑的原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    it = client.get(f"/v1/items/{item_id}", headers=auth(token)).json()

    conflict = client.post(f"/v1/items/{item_id}/source-text", json={
        "expected_source_revision": it["source_revision"] + 5, "text": "编辑。",
    }, headers=auth(token))
    assert conflict.status_code == 409

    empty = client.post(f"/v1/items/{item_id}/source-text", json={
        "expected_source_revision": it["source_revision"], "text": "  \n  ",
    }, headers=auth(token))
    assert empty.status_code == 422


def test_source_edit_isolates_users(client, user_a, user_b, session_factory, monkeypatch):
    """跨用户不能编辑他人条目。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    c = _capture(client, user_a["phone"]["token"], "editiso", "用户A的原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    it = client.get(f"/v1/items/{item_id}", headers=auth(token)).json()

    r = client.post(f"/v1/items/{item_id}/source-text", json={
        "expected_source_revision": it["source_revision"], "text": "用户B的编辑。",
    }, headers=auth(user_b["desktop"]["token"]))
    assert r.status_code == 404


def test_source_edit_auto_enrich_off_stays_extracted(
    client, user_a, session_factory, db, monkeypatch,
):
    """「AI 自动加工」关闭：编辑后停在 extracted，不自动排队提炼。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    from kbserver.models import User

    u = db.get(User, user_a["user_id"])
    u.settings_json = {"ai": {"auto_enrich": False}}
    db.commit()

    token = user_a["desktop"]["token"]
    c = _capture(client, user_a["phone"]["token"], "editmanual", "关闭自动加工的原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    it = client.get(f"/v1/items/{item_id}", headers=auth(token)).json()

    r = client.post(f"/v1/items/{item_id}/source-text", json={
        "expected_source_revision": it["source_revision"], "text": "手动加工的编辑。",
    }, headers=auth(token))
    assert r.status_code == 202
    out = r.json()
    assert out["source_revision"] == it["source_revision"] + 1
    assert out["pipeline_state"] == "extracted"
