"""docs/08 §8.4、§11：云端条目详情阅读视图契约测试。

验收要求（§11）：
- 网页原文／提炼视图覆盖全文、部分资料、等待提炼、旧提炼对应旧来源、失败与到期；
- 跨用户文件不能读取；
- 不显示本地晋升与 Knowledge 状态。
"""
from __future__ import annotations

import json

from tests.conftest import auth

from kbserver.models import BundleRevision, Item, StoredFile, utcnow
from kbserver.providers.llm import GenerateResult
from kbserver.storage.objects import ObjectStore

from tests.integration.test_m2 import FakeProvider, llm_result, doc_from_prompt, chunk_aware_behavior


def _drain(session_factory, max_rounds=20):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _create_profile(client, token):
    return client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible",
        "endpoint": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        "secret": "sk-reading-test-1234567890",
    }, headers=auth(token))


def _capture(client, token, key, text, note=None):
    body = {"client_capture_id": f"{key}-1111-2222-3333-444444444444",
            "input_kind": "text", "text": text}
    if note:
        body["user_note"] = note
    return client.post("/v1/captures", json=body,
                       headers={**auth(token), "Idempotency-Key": key})


def test_reading_view_before_enrichment_shows_pending(client, user_a, session_factory, monkeypatch):
    """尚无提炼时仅显示待提炼，原文依然可读（docs/08 §8.4、§11）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    c = _capture(client, user_a["phone"]["token"], "readpending",
                 "第一段原文。\n第二段原文。", note="我的采集备注")
    item_id = c.json()["item_id"]
    # 不跑 worker：处于 queued，尚无可读正文
    r = client.get(f"/v1/items/{item_id}/reading", headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["cloud_digest"]["state"] == "pending"
    assert body["item"]["user_note"] == "我的采集备注"
    assert "key_points" in body["cloud_digest"]


def test_reading_view_ready_with_evidence_locators(client, user_a, session_factory, monkeypatch):
    """提炼完成后可读全文 + 结构化提炼，证据可定位到同版本片段（docs/08 §8.4）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture(client, user_a["phone"]["token"], "readready",
                 "第一段原文内容。\n第二段原文内容。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    r = client.get(f"/v1/items/{item_id}/reading", headers=auth(token))
    body = r.json()
    assert body["cloud_digest"]["state"] == "ready"
    g = body["cloud_digest"]
    assert g["schema_version"] == "2.0"
    assert g["summary"] == "演示摘要。"
    assert g["key_points"][0]["claim_id"] == "c0001"
    assert g["excerpts"], "应有逐字摘录"
    assert g["evidence_map"], "应保存结构化证据映射"
    # 证据定位到同版本原文片段
    sid = g["key_points"][0]["evidence_ids"][0]
    assert sid in g["segments"]
    assert "第一段原文内容" in g["segments"][sid]

    sm = body["source_material"]
    assert sm["normalized_available"] is True
    assert "第一段原文内容" in sm["normalized_md"]
    assert sm["download_base"].endswith(f"/bundles/{body['item']['bundle_revision']}/files")
    # 不显示本地晋升 / Knowledge 状态（docs/08 §8.4、§8.5）
    assert "promotion" not in json.dumps(body)
    assert "knowledge" not in json.dumps(body).lower()


def test_reading_view_old_digest_points_to_old_source_revision(
    client, user_a, session_factory, db, monkeypatch
):
    """r2 待提炼时，旧提炼仍对应 r1 的原文，不混用 r2 片段（docs/08 §8.4、§11）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture(client, user_a["phone"]["token"], "readstale", "旧的第一段原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    first = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    assert first["cloud_digest"]["source_revision"] == 1

    # 补充新材料 -> 来源 r2；此时不重新加工
    it = client.get(f"/v1/items/{item_id}", headers=auth(token)).json()
    sup = client.post(f"/v1/items/{item_id}/supplements", json={
        "expected_source_revision": it["source_revision"], "text": "新的第二段原文。",
    }, headers=auth(token))
    assert sup.status_code == 202

    r = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    g = r["cloud_digest"]
    assert r["item"]["source_revision"] == 2
    # 旧提炼对应旧来源：显式提示且不出现内部修订号（docs/17 §14.4 用户语言契约）
    assert g["source_revision"] == 1
    assert g["stale_note"] and "更新" in g["stale_note"] and "r1" not in g["stale_note"]
    assert all("旧的第一段原文" in t for t in g["segments"].values())


def test_reading_view_reports_missing_and_coverage(client, user_a, session_factory, monkeypatch):
    """部分资料/缺失材料如实显示，不用标题补写（docs/08 §3.1、§11）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    # 只有 URL、没有正文：初始 missing_materials 含 main_content
    c = client.post("/v1/captures", json={
        "client_capture_id": "readmiss-1111-2222-3333-444444444444",
        "input_kind": "url", "original_url": "https://example.com/article",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "readmiss"})
    item_id = c.json()["item_id"]
    body = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    assert "main_content" in body["item"]["missing_materials"]


def test_reading_view_expired_bundle(client, user_a, session_factory, db, monkeypatch):
    """云端材料过期如实提示，不假装能从 Vault 回读（docs/08 §8.4）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture(client, user_a["phone"]["token"], "readexpire", "会过期的原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    from datetime import timedelta
    bundles = db.query(BundleRevision).filter(BundleRevision.item_id == item_id).all()
    for b in bundles:
        b.expires_at = utcnow() - timedelta(days=1)
    db.commit()

    body = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    assert body["expired"] is True
    assert "已过期" in body["note"]
    assert body["cloud_digest"]["state"] == "expired"


def test_reading_view_isolates_users(client, user_a, user_b, session_factory, monkeypatch):
    """跨用户不能读取条目或其文件（docs/08 §8.4、§11）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture(client, user_a["phone"]["token"], "readiso", "用户A的原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    r = client.get(f"/v1/items/{item_id}/reading", headers=auth(user_b["desktop"]["token"]))
    assert r.status_code == 404


def test_reading_view_does_not_return_other_users_files(client, user_a, user_b, session_factory, monkeypatch):
    """已鉴权文件路由拒绝跨用户 file_id（docs/08 §8.4）。"""
    FakeProvider.behavior = chunk_aware_behavior
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture(client, user_a["phone"]["token"], "readfile", "原文。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    body = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
    fid = body["source_material"]["files"][0]["file_id"]
    rev = body["item"]["bundle_revision"]

    r = client.get(f"/v1/items/{item_id}/bundles/{rev}/files/{fid}",
                   headers=auth(user_b["desktop"]["token"]))
    assert r.status_code == 404
