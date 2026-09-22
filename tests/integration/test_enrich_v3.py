"""M1 云端生成链路 v3 闭环测试（docs/23 §5、§11「内容与引用」；docs/24 §3–§5）。

用假供应商跑真实 worker 路径：提炼 → 组装 → 发布 content.json / preview.md → 状态映射。
覆盖块级失败发布部分结果、每次逻辑操作一份额度修复、分块未覆盖范围、AI 纠错产生
新的不可变来源修订、以及最终失败只留诊断文件。
"""
from __future__ import annotations

import json
import re

import pytest

from tests.conftest import auth
from tests.integration.test_m2 import (
    FakeProvider,
    _capture_text,
    _create_profile,
    _get_item,
    llm_result,
)

from kbserver.domain import content_v3
from kbserver.models import Item, SourceRevision


@pytest.fixture()
def fake_llm(monkeypatch):
    FakeProvider.behavior = None
    FakeProvider.instances = []
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    return FakeProvider


def _drain(session_factory, max_rounds=20):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _paragraphing_none(prompt: str):
    """让「AI 自动纠错与分段」这条支路干净地回退，不干扰提炼断言。"""
    return llm_result({"paragraph_starts": "不是数组"})


def _digest_output(prompt: str, *, bad_refs: bool = False, fake_quote: bool = False,
                   fix_claim: bool = False) -> dict:
    """按提示词里的 material/candidates 造一份 v3 主体输出。"""
    payload = json.loads(prompt)
    candidates = payload.get("candidates")
    if candidates:  # 合并阶段：只沿用已校验候选里的块与引用
        blocks = [b for c in candidates for s in c["sections"] for b in s["blocks"]]
        return {"title": "演示标题", "summary": "跨段汇总。",
                "sections": [{"heading": "主要判断", "blocks": blocks}], "limitations": []}
    material = payload["material"]
    first = material[0]
    claim_refs = ["R99"] if bad_refs and not fix_claim else [first["ref"]]
    quote_text = "这句是编出来的摘录" if fake_quote else first["text"]
    return {
        "title": "演示标题",
        "summary": "先保存，再提炼。",
        "sections": [
            {"heading": "主要判断", "blocks": [
                {"kind": "claim", "text": "采集与总结应分开处理。", "refs": claim_refs},
                {"kind": "quote", "text": quote_text, "refs": [first["ref"]]},
                {"kind": "suggestion", "text": "可以分别统计两个阶段的失败原因。", "refs": []},
            ]},
        ],
        "limitations": [],
    }


def _item_flags(session_factory, item_id: str) -> tuple[str, str]:
    """(pipeline_state, state_reason)：状态原因只在库里，接口只给中文文案。"""
    with session_factory() as db:
        item = db.get(Item, item_id)
        return item.pipeline_state, item.state_reason or ""


def _bundle_file(client, token, item_id, revision, path):
    m = client.get(f"/v1/items/{item_id}/bundles/{revision}/manifest", headers=auth(token)).json()
    entry = next((f for f in m["files"] if f["relative_path"] == path), None)
    if entry is None:
        return None, m
    raw = client.get(
        f"/v1/items/{item_id}/bundles/{revision}/files/{entry['file_id']}", headers=auth(token)
    ).content
    return raw, m


def test_enrich_publishes_v3_content_document(client, user_a, session_factory, fake_llm):
    def behavior(request):
        payload = json.loads(request.user)
        if "paragraph_starts" in json.dumps(payload.get("output_schema") or {}, ensure_ascii=False):
            return _paragraphing_none(request.user)
        return llm_result(_digest_output(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    item_id = _capture_text(
        client, token, "v3cap1", "采集与总结应该分开处理，避免混在一起。\n本地整理才决定晋升。"
    ).json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it
    assert _item_flags(session_factory, item_id) == ("ready", "")
    rev = it["bundle_revision"]

    raw, manifest = _bundle_file(client, token, item_id, rev, "content.json")
    assert raw is not None, manifest
    assert manifest["processing"]["format_version"] == content_v3.CONTENT_FORMAT_VERSION
    assert manifest["processing"]["completeness"] == "complete"
    assert manifest["processing"]["recipe_version"] == content_v3.CONTENT_RECIPE_VERSION
    paths = {f["relative_path"] for f in manifest["files"]}
    # 旧协议产物不再写出（docs/23 §5.1）
    assert "analysis.json" not in paths and "preview.md" in paths

    doc = json.loads(raw)
    assert doc["format_version"] == "3.0"
    assert doc["kind"] == "digest" and doc["document_id"] == f"dig-{item_id}"
    assert doc["revision"] == rev, "文档版本由程序在发布时盖章"
    assert {b["kind"] for s in doc["sections"] for b in s["blocks"]} == {
        "claim", "quote", "suggestion"}
    for block in doc["sections"][0]["blocks"]:
        assert all(ref in doc["references"] for ref in block["refs"])
    # 模型不该看到的程序字段，由程序填成真实身份
    ref = next(iter(doc["references"].values()))
    assert ref["item_id"] == item_id and ref["source_revision"] == it["source_revision"]
    assert ref["segment_ids"] and ref["source_text_hash"]

    preview = _bundle_file(client, token, item_id, rev, "preview.md")[0].decode("utf-8")
    assert "采集与总结应分开处理" in preview
    for internal in ("R1", doc["sections"][0]["blocks"][0]["refs"][0], ref["segment_ids"][0]):
        assert internal not in preview, "阅读产物不暴露裸内部编号"


def test_bad_reference_and_fabricated_quote_publish_partial(client, user_a, session_factory, fake_llm):
    """块级失败不作废整篇：坏引用与假摘录被剔除，其余内容发布为部分结果。"""
    calls: list[str] = []

    def behavior(request):
        payload = json.loads(request.user)
        if "paragraph_starts" in json.dumps(payload.get("output_schema") or {}, ensure_ascii=False):
            return _paragraphing_none(request.user)
        if "validation_errors" in payload:  # 修复调用：修好引用，摘录仍然编造
            return llm_result(_digest_output(request.user, fake_quote=True, fix_claim=True))
        calls.append("first")
        return llm_result(_digest_output(request.user, bad_refs=True, fake_quote=True))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    item_id = _capture_text(
        client, token, "v3cap2", "采集与总结应该分开处理，避免混在一起。\n本地整理才决定晋升。"
    ).json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert _item_flags(session_factory, item_id) == ("ready", "partial_result")
    raw, manifest = _bundle_file(client, token, item_id, it["bundle_revision"], "content.json")
    assert manifest["processing"]["completeness"] == "partial"
    doc = json.loads(raw)
    blocks = doc["sections"][0]["blocks"]
    kinds = [b["kind"] for b in blocks]
    assert "quote" not in kinds, "逐字校验不过的摘录不能发布"
    assert any(b["kind"] == "claim" and b["refs"] for b in blocks), "修好的主张保留依据"
    completeness = doc["completeness"]
    assert completeness["state"] == "partial"
    assert completeness["dropped_blocks"] >= 1
    assert any(g["code"] == "quote_not_verbatim" for g in completeness["gaps"])
    assert all(g.get("message") for g in completeness["gaps"]), "缺口要给人看得懂的话"
    for gap in completeness["gaps"]:
        # 界面红线：给用户看的文案里不得出现 R/e/s0001 这类内部编号
        assert not re.search(r"R\d+|e\d{1,4}|s\d{4}", gap["message"]), gap
    preview = _bundle_file(client, token, item_id, it["bundle_revision"], "preview.md")[0]
    assert "这句是编出来的摘录" not in preview.decode("utf-8")


def test_final_failure_keeps_diagnostic_and_original(client, user_a, session_factory, fake_llm):
    """修复额度只有一次：仍不可解析就落 failed，原始材料不受影响。"""
    calls: list[int] = []

    def behavior(request):
        payload = json.loads(request.user)
        if "paragraph_starts" in json.dumps(payload.get("output_schema") or {}, ensure_ascii=False):
            return _paragraphing_none(request.user)
        calls.append(1)
        return llm_result("{不是 JSON")

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    item_id = _capture_text(client, token, "v3cap3", "采集与总结应该分开处理，避免混在一起。").json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert _item_flags(session_factory, item_id) == ("failed", "model_output_invalid")
    assert len(calls) == 2, f"提炼一次 + 修复一次，不再叠加：{len(calls)}"
    raw, manifest = _bundle_file(client, token, item_id, it["bundle_revision"], "content.error.json")
    assert raw is not None, manifest
    diag = json.loads(raw)
    assert diag["kind"] == "content_validation_error"
    assert diag["errors"][0]["code"] in ("json_unparsable", "truncated")
    # 原始材料还在：可以重新加工而不是丢内容
    assert "normalized.md" in {f["relative_path"] for f in manifest["files"]}


def test_chunked_failure_reports_uncovered_chunk(client, user_a, session_factory, fake_llm):
    """长材料分块：坏掉的那块记成未覆盖范围，其余块照常汇总发布部分结果。"""
    chunk_totals: list[int] = []

    def behavior(request):
        payload = json.loads(request.user)
        if "paragraph_starts" in json.dumps(payload.get("output_schema") or {}, ensure_ascii=False):
            return _paragraphing_none(request.user)
        chunk = payload.get("chunk")
        if chunk:
            chunk_totals.append(chunk["total"])
            if chunk["index"] == 2:
                return llm_result("{这一块坏了")
        return llm_result(_digest_output(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token, capabilities={"context_tokens": 2000, "max_output_tokens": 1000})
    paragraphs = [
        f"第{i}段：采集与总结应该分开处理，避免混在一起；加工失败时原始材料必须仍然可读。"
        for i in range(60)
    ]
    long_text = "\n\n".join(paragraphs)
    item_id = _capture_text(client, token, "v3cap4", long_text).json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert len(chunk_totals) > 1, f"这条材料没有真的分块：{chunk_totals}"
    assert _item_flags(session_factory, item_id) == ("ready", "partial_result")
    raw, manifest = _bundle_file(client, token, item_id, it["bundle_revision"], "content.json")
    assert manifest["processing"]["completeness"] == "partial"
    doc = json.loads(raw)
    assert "chunk:2" in doc["completeness"]["missing_stages"], doc["completeness"]
    assert doc["sections"], "其余分块的有效内容仍要发布"


def test_ai_correction_creates_new_immutable_source_revision(
    client, user_a, session_factory, fake_llm
):
    """听错词修正写新版本，不改旧版本：被旧产物引用的原文永远还能读出来。"""
    from kbserver.workers import worker

    original = "第一段：可靠保存材料。\n第二段：加工不丢原文。"
    corrected = "第一段：可靠保存材料已修正。"

    def behavior(request):
        payload = json.loads(request.user)
        if "paragraph_starts" in json.dumps(payload.get("output_schema") or {}, ensure_ascii=False):
            segs = json.loads(payload["segments"])
            return llm_result({
                "paragraph_starts": [segs[0]["segment_id"]],
                "corrections": [{"segment_id": segs[0]["segment_id"], "text": corrected}],
            })
        return llm_result(_digest_output(request.user))

    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    item_id = _capture_text(client, token, "v3cap5", original).json()["item_id"]
    worker.run_once(session_factory)  # 只跑提取，先记下未纠错原文
    extracted = _get_item(client, token, item_id)
    assert extracted["source_revision"] == 1
    old_segments = json.loads(
        _bundle_file(client, token, item_id, extracted["bundle_revision"], "segments.json")[0]
    )
    assert old_segments["source_revision"] == 1
    assert old_segments["segments"][0]["text"] == "第一段：可靠保存材料。"

    fake_llm.behavior = behavior
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it
    assert it["source_revision"] == 2, "AI 纠错要产生新的来源修订而不是原地覆盖"
    raw, manifest = _bundle_file(client, token, item_id, it["bundle_revision"], "content.json")
    assert manifest["source_revision"] == 2
    doc = json.loads(raw)
    assert {r["source_revision"] for r in doc["references"].values()} == {2}
    quote = next(b for s in doc["sections"] for b in s["blocks"] if b["kind"] == "quote")
    assert "已修正" in quote["text"], "提炼按修正后的文本进行"

    with session_factory() as db:
        parent = db.query(SourceRevision).filter(
            SourceRevision.item_id == item_id, SourceRevision.revision == 1).one()
        derived = db.query(SourceRevision).filter(
            SourceRevision.item_id == item_id, SourceRevision.revision == 2).one()
        assert parent.metadata_json.get("origin") != "ai_correction"
        meta = derived.metadata_json
        assert meta["origin"] == "ai_correction"
        assert meta["parent_revision"] == 1
        assert meta["correction_rule_version"]

    # 未纠错原件仍可读取：旧 Bundle 里的 segments.json 内容不变
    stale = json.loads(
        _bundle_file(client, token, item_id, extracted["bundle_revision"], "segments.json")[0]
    )
    assert stale["segments"][0]["text"] == "第一段：可靠保存材料。"


def test_reprocess_reuses_the_same_derived_revision(client, user_a, session_factory, fake_llm):
    """重新加工遇到同一份修正：复用既有派生修订，不叠加版本号（docs/24 §7）。"""
    corrected = "第一段：可靠保存材料已修正。"

    def behavior(request):
        payload = json.loads(request.user)
        if "paragraph_starts" in json.dumps(payload.get("output_schema") or {}, ensure_ascii=False):
            segs = json.loads(payload["segments"])
            return llm_result({
                "paragraph_starts": [segs[0]["segment_id"]],
                "corrections": [{"segment_id": segs[0]["segment_id"], "text": corrected}],
            })
        return llm_result(_digest_output(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    item_id = _capture_text(
        client, token, "v3cap6", "第一段：可靠保存材料。\n第二段：加工不丢原文。"
    ).json()["item_id"]
    _drain(session_factory)
    assert _get_item(client, token, item_id)["source_revision"] == 2

    r = client.post(f"/v1/items/{item_id}/reprocess", json={"reason": "再跑一次"},
                    headers=auth(token))
    assert r.status_code == 202, r.text
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it
    assert it["source_revision"] == 2, "同一轮纠错不应再顶出一个来源版本"
    with session_factory() as db:
        revisions = db.query(SourceRevision).filter(SourceRevision.item_id == item_id).count()
        assert revisions == 2
