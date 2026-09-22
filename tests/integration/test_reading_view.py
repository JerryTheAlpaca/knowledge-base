"""docs/24 §6：阅读 API 只承载 ContentDocument v3（GET /v1/items/{id}/reading）。

覆盖真实会出错的地方（不在这里重复 W1 的组装器契约测试）：
- v3 Bundle：清单里的 content.json 校验通过后原样给出；旧固定数组字段已彻底移出响应；
- 只有旧 analysis.json 的条目仍可阅读：当场转成 v3、`legacy_converted=true`，且不回写产物；
- 认不出的 `format_version` 如实成为状态，不会变成空整理结果（界面据此不会显示「没有整理结果」）；
- `partial` 的 completeness 进入阅读载荷与列表状态（docs/24 §4 映射：ready + partial_result）；
- 旧提炼对应旧来源版本、到期、跨用户与跨文件隔离。

条目与 Bundle 直接按真实发布路径（`pipeline.create_capture` + `publish_bundle`）构造，
不经 worker：提炼产物长什么样由 W2 负责，本文件只裁判读取路径。
"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import auth

from kbserver.api import routes_items
from kbserver.domain import content_migration, content_v3, pipeline
from kbserver.models import (
    BundleRevision,
    Item,
    Job,
    SourceRevision,
    StoredFile,
    new_id,
    utcnow,
)
from kbserver.storage.objects import ObjectStore

SEGMENTS = [
    {"segment_id": "s0001", "text": "采集与总结应该分开处理。"},
    {"segment_id": "s0002", "text": "本地整理才决定是否晋升为长期知识。"},
]
DIGEST_KEYS = {
    "state", "state_detail", "source_revision", "bundle_revision", "created_at",
    "format_version", "content_document", "completeness", "segments", "stale_note",
    "legacy_converted", "unresolved_count",
}


# ---- 构造材料与产物（与 W2/W4 的真实登记方式一致）----

def _segments_payload(revision: int) -> dict:
    return {
        "source_revision": revision,
        "segments": [
            {**s, "paragraph_id": f"p{i:04d}", "locator": {"type": "paragraph", "index": i},
             "origin": "extract", "kind": "paragraph", "start_ms": None, "end_ms": None}
            for i, s in enumerate(SEGMENTS, 1)
        ],
        "paragraphs": [
            {"paragraph_id": f"p{i:04d}", "segment_ids": [s["segment_id"]], "kind": "paragraph",
             "start_ms": None, "end_ms": None, "char_count": len(s["text"]), "text": s["text"]}
            for i, s in enumerate(SEGMENTS, 1)
        ],
    }


def _item(db, user_id: str, *, title: str = "演示标题", text: str = "演示正文") -> Item:
    """按真实接收路径建条目；清掉排队任务，避免别的用例的 `_drain` 吃到它。"""
    _capture, item = pipeline.create_capture(
        db, ObjectStore(), user_id=user_id,
        payload={"schema_version": "1.0", "client_capture_id": new_id(),
                 "input_kind": "text", "text": text, "archive_policy": "source_materials"},
        uploads={},
    )
    db.query(Job).filter(Job.item_id == item.id).delete(synchronize_session=False)
    source = _source(db, item)
    meta = dict(source.metadata_json)
    meta["title"] = title
    source.metadata_json = meta
    return item


def _source(db, item: Item, revision: int | None = None) -> SourceRevision:
    return (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id,
                SourceRevision.revision == (revision or item.source_revision))
        .one()
    )


def _register(db, store, *, item, relative_path: str, data, role: str, mime: str) -> StoredFile:
    return pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=data if isinstance(data, bytes) else pipeline.canonical_json(data),
        relative_path=relative_path, role=role, mime=mime,
    )


def _source_files(db, store, *, item, revision: int) -> list[StoredFile]:
    payload = _segments_payload(revision)
    return [
        _register(db, store, item=item, relative_path="segments.json", data=payload,
                  role="source_material", mime="application/json"),
        _register(db, store, item=item, relative_path="normalized.md",
                  data="\n".join(s["text"] for s in SEGMENTS).encode("utf-8"),
                  role="source_material", mime="text/markdown"),
    ]


def _publish(db, store, *, item, files, processing_state: str, pipeline_state: str,
             revision: int | None = None, processing_extra: dict | None = None,
             result: StoredFile | None = None) -> BundleRevision:
    bundle = pipeline.publish_bundle(
        db, store, item=item, source=_source(db, item, revision), files=files,
        processing_state=processing_state, pipeline_state=pipeline_state,
        result_file_id=result.file_id if result else None,
        processing_extra=processing_extra,
        # v3 产物登记自己的规则版本（docs/24 §5）；旧产物沿用发布方默认值
        recipe_version=content_v3.CONTENT_RECIPE_VERSION
        if (processing_extra or {}).get("content_file_id") else None,
    )
    db.commit()
    return bundle


def _v3_document(*, item, model_output: dict, title: str = "演示标题") -> tuple[dict, dict]:
    """用真实组装器生成 content.json 与其完整性：测试不手写哈希与 e 键。"""
    ref_table = content_v3.build_ref_table([content_v3.Material(
        item_id=item.id, source_revision=item.source_revision,
        segments=[dict(s) for s in SEGMENTS])])
    doc, report = content_v3.assemble_content_document(
        model_output, ref_table=ref_table,
        document_id=content_migration.digest_document_id(item.id), kind="digest",
        revision=item.bundle_revision + 1, item_id=item.id,
        source_revision=item.source_revision, task="digest",
        recipe_version=content_v3.CONTENT_RECIPE_VERSION, source_title=title,
    )
    assert doc is not None, report.errors
    assert content_v3.validate_content_document(doc) == []
    return doc, report.completeness


def _v3_bundle(db, *, user_id: str, model_output: dict, title: str = "演示标题"):
    """发布一个 v3 Bundle：源材料 + content.json + preview.md + docs/24 §5 的清单字段。"""
    store = ObjectStore()
    item = _item(db, user_id, title=title)
    files = _source_files(db, store, item=item, revision=item.source_revision)
    doc, completeness = _v3_document(item=item, model_output=model_output, title=title)
    content = _register(db, store, item=item, relative_path="content.json", data=doc,
                        role="generated", mime="application/json")
    preview = _register(db, store, item=item, relative_path="preview.md",
                        data=content_v3.render_content_markdown(doc).encode("utf-8"),
                        role="preview", mime="text/markdown")
    bundle = _publish(
        db, store, item=item, files=files + [content, preview],
        processing_state="ready", pipeline_state="ready",
        result=content,
        processing_extra={"format_version": content_v3.CONTENT_FORMAT_VERSION,
                          "completeness": completeness["state"],
                          "content_file_id": content.file_id},
    )
    return item, bundle, doc, completeness


def _complete_output(quote: str | None = None) -> dict:
    return {
        "title": "两阶段处理",
        "summary": "先保存原文，再由本地决定是否晋升为长期知识。",
        "sections": [{"heading": "核心结构", "blocks": [
            {"kind": "text", "text": "这篇材料讲的是采集与整理的分工。", "refs": []},
            {"kind": "claim", "text": "采集与整理应当分开处理。", "refs": ["R1"]},
            {"kind": "quote", "text": quote or SEGMENTS[0]["text"], "refs": ["R1"]},
            {"kind": "suggestion", "text": "可以分别统计两个阶段的失败。", "refs": []},
        ]}],
        "limitations": ["字幕可能不完整。"],
    }


def _legacy_analysis_doc(**overrides) -> dict:
    """Schema 2.0 的历史形状（旧产物只有这种结构，读取时当场转 v3）。"""
    doc = {
        "schema_version": "2.0",
        "source_revision": 1,
        "summary": "先保存，再提炼。",
        "key_points": [{"claim_id": "c0001", "text": "采集与整理应分开。",
                        "conditions": "多来源时更明显。", "evidence_ids": ["s0001"]}],
        "excerpts": [{"claim_id": "c0001", "text": SEGMENTS[0]["text"], "evidence_ids": ["s0001"]}],
        "methods": [],
        "insights": [{"text": "可以分别统计两阶段失败。", "kind": "ai_suggestion",
                      "basis_ids": ["s0002"]}],
        "limitations": ["字幕可能不完整。"],
        "workflow": None,
        "evidence_map": {},
    }
    doc.update(overrides)
    return doc


def _legacy_bundle(db, *, user_id: str, analysis: dict) -> tuple[Item, BundleRevision]:
    store = ObjectStore()
    item = _item(db, user_id)
    files = _source_files(db, store, item=item, revision=item.source_revision)
    analysis_file = _register(db, store, item=item, relative_path="analysis.json",
                              data=analysis, role="generated", mime="application/json")
    bundle = _publish(db, store, item=item, files=files + [analysis_file],
                      processing_state="ready", pipeline_state="ready", result=analysis_file)
    return item, bundle


def _digest(client, token, item_id) -> dict:
    r = client.get(f"/v1/items/{item_id}/reading", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _bundles(db, item: Item) -> int:
    return db.query(BundleRevision).filter(BundleRevision.item_id == item.id).count()


# ---- v3 阅读 ----

def test_reading_view_returns_v3_document_only(client, user_a, db):
    """v3 Bundle：content_document/completeness/format_version 原样给出，无固定数组字段。"""
    item, _bundle, doc, _comp = _v3_bundle(db, user_id=user_a["user_id"],
                                           model_output=_complete_output())
    g = _digest(client, user_a["desktop"]["token"], item.id)["cloud_digest"]

    assert set(g) == DIGEST_KEYS  # docs/24 §6 冻结的字段集，不双写旧数组
    assert g["state"] == "ready" and g["state_detail"] == ""
    assert g["format_version"] == content_v3.CONTENT_FORMAT_VERSION
    assert g["legacy_converted"] is False and g["unresolved_count"] == 0
    assert g["completeness"]["state"] == "complete"
    assert g["bundle_revision"] == item.bundle_revision
    assert g["source_revision"] == item.source_revision

    served = g["content_document"]
    assert served == doc  # 校验通过即原样服务，不重新拼装
    assert [b["kind"] for b in served["sections"][0]["blocks"]] == [
        "text", "claim", "quote", "suggestion"]
    # 引用是文档内 e 键 → 真实原文范围，界面不需要知道 R 编号
    ref = served["references"]["e1"]
    assert ref["segment_ids"] == ["s0001", "s0002"] and ref["source_revision"] == 1
    assert served["limitations"] == ["字幕可能不完整。"]
    assert g["segments"] == {s["segment_id"]: s["text"] for s in SEGMENTS}
    # 阅读不写回任何产物：仍是接收时的 original_only + 发布时的 v3 两个版本
    assert _bundles(db, item) == 2


def test_reading_view_legacy_only_item_still_readable(client, user_a, db):
    """只有 analysis.json 的历史条目：当场转成 v3 视图，标记 legacy_converted，不回写。"""
    item, bundle = _legacy_bundle(db, user_id=user_a["user_id"], analysis=_legacy_analysis_doc())
    body = _digest(client, user_a["desktop"]["token"], item.id)
    g = body["cloud_digest"]

    assert g["state"] == "ready", g["state_detail"]
    assert g["legacy_converted"] is True
    assert g["format_version"] == content_v3.CONTENT_FORMAT_VERSION
    doc = g["content_document"]
    headings = [s["heading"] for s in doc["sections"]]
    assert "核心观点与证据" in headings and "值得保留的原文摘录" in headings
    blocks = [b for s in doc["sections"] for b in s["blocks"]]
    refs = doc["references"]
    # 旧 evidence_ids 落成真实片段范围，摘录仍指向被引原文
    assert any(b["kind"] == "quote" and refs[b["refs"][0]]["segment_ids"] == ["s0001"] for b in blocks)
    assert g["unresolved_count"] == 0 and g["completeness"]["state"] == "complete"
    # 旧 Bundle 与产物一字未动：读取不产生新版本、不登记 content.json
    assert _bundles(db, item) == 2
    assert bundle.revision == item.bundle_revision
    paths = [f.relative_path for f in db.query(StoredFile).filter(StoredFile.item_id == item.id).all()]
    assert "content.json" not in paths


def test_reading_view_legacy_unresolved_evidence_counts(client, user_a, db):
    """旧证据定位不到：如实计入 unresolved_count 与 partial，不拿别的片段冒充。"""
    analysis = _legacy_analysis_doc(key_points=[
        {"claim_id": "c0001", "text": "采集与整理应分开。", "evidence_ids": ["s0001"]},
        {"claim_id": "c0002", "text": "这条依据已找不到原文。", "evidence_ids": ["s9999"]},
    ])
    item, _bundle = _legacy_bundle(db, user_id=user_a["user_id"], analysis=analysis)
    g = _digest(client, user_a["desktop"]["token"], item.id)["cloud_digest"]

    assert g["state"] == "ready" and g["completeness"]["state"] == "partial"
    assert g["unresolved_count"] == 1
    assert g["format_version"] == content_v3.CONTENT_FORMAT_VERSION


def test_reading_view_unknown_format_version_is_honest_state(client, user_a, db):
    """认不出的 format_version：状态与说明照实给出，绝不降级成「没有整理结果」。"""
    item, _bundle, doc, completeness = _v3_bundle(db, user_id=user_a["user_id"],
                                                  model_output=_complete_output())
    # 模拟以后版本写入的产物：同一 Bundle 里换成未发布的格式版本
    store = ObjectStore()
    future = {**doc, "format_version": "4.0"}
    entry = pipeline.manifest_files_by_path(
        routes_items._bundle_manifest(db, item, item.bundle_revision))
    row = db.query(StoredFile).filter(StoredFile.file_id == entry["content.json"]["file_id"]).one()
    sha, key, size = store.put_bytes(pipeline.canonical_json(future))
    row.storage_key, row.sha256, row.bytes = key, sha, size
    db.commit()

    g = _digest(client, user_a["desktop"]["token"], item.id)["cloud_digest"]
    assert g["state"] == "unknown_format"
    assert g["content_document"] is None and g["completeness"] == {}
    assert "4.0" in g["state_detail"] and "没有整理结果" not in g["state_detail"]


def test_reading_view_corrupt_content_json_reports_failure(client, user_a, db):
    """content.json 坏了不能当空整理结果：给出可定位的失败说明。"""
    item, _bundle, _doc, _comp = _v3_bundle(db, user_id=user_a["user_id"],
                                            model_output=_complete_output())
    store = ObjectStore()
    entry = pipeline.manifest_files_by_path(
        routes_items._bundle_manifest(db, item, item.bundle_revision))
    row = db.query(StoredFile).filter(StoredFile.file_id == entry["content.json"]["file_id"]).one()
    sha, key, size = store.put_bytes(b'{"format_version": "3.0"')
    row.storage_key, row.sha256, row.bytes = key, sha, size
    db.commit()

    g = _digest(client, user_a["desktop"]["token"], item.id)["cloud_digest"]
    assert g["state"] == "failed" and g["content_document"] is None
    assert "JSON" in g["state_detail"]


def test_partial_completeness_reaches_ui_payload(client, user_a, db):
    """partial：整理仍是 ready，缺口用给用户看的中文文案，列表状态同时可见。"""
    output = _complete_output()
    output["sections"][0]["blocks"].append(
        {"kind": "claim", "text": "这条结论引用了不存在的引用号。", "refs": ["R7"]})
    item, _bundle, doc, completeness = _v3_bundle(db, user_id=user_a["user_id"], model_output=output)
    assert completeness["state"] == "partial"

    it = db.get(Item, item.id)
    it.state_reason = "partial_result"  # docs/24 §4：ready + partial_result，不新增枚举
    db.commit()

    body = _digest(client, user_a["desktop"]["token"], item.id)
    g = body["cloud_digest"]
    assert g["state"] == "ready" and g["content_document"]
    assert g["completeness"]["state"] == "partial"
    assert g["completeness"]["dropped_blocks"] == 1
    messages = [gap["message"] for gap in g["completeness"]["gaps"]]
    assert messages and all(m.strip() for m in messages)  # 界面只显示这些文案
    organize = next(s for s in body["item"]["workflow"]["steps"] if s["id"] == "organize")
    assert organize["status"] == "completed"
    assert organize["reason_code"] == "ORGANIZE_PARTIAL"
    assert "部分" in organize["message"]
    # 完整文档仍按 complete 显示为已生成整理结果
    other, _b, _d, _c = _v3_bundle(db, user_id=user_a["user_id"], model_output=_complete_output())
    other_wf = _digest(client, user_a["desktop"]["token"], other.id)["item"]["workflow"]
    assert next(s for s in other_wf["steps"] if s["id"] == "organize")["reason_code"] == "ORGANIZE_DONE"


# ---- 版本、到期与隔离 ----

def test_reading_view_before_digest_shows_pending_without_faking(client, user_a, db):
    """尚无提炼结果：pending + 原始材料可读，不出现空的 content_document。"""
    item = _item(db, user_a["user_id"])
    store = ObjectStore()
    _publish(db, store, item=item, files=_source_files(db, store, item=item,
                                                       revision=item.source_revision),
             processing_state="original_only", pipeline_state="extracted")
    body = _digest(client, user_a["desktop"]["token"], item.id)
    g = body["cloud_digest"]

    assert g["state"] == "pending" and g["content_document"] is None
    assert g["completeness"] == {} and g["format_version"] is None
    assert g["bundle_revision"] is None and g["unresolved_count"] == 0
    assert body["source_material"]["normalized_available"] is True
    assert "采集与总结应该分开处理" in body["source_material"]["normalized_md"]


def test_reading_view_old_digest_points_to_old_source_revision(client, user_a, db):
    """旧提炼对应旧来源：显式提示且不出现内部修订号，片段不与新版本混用（docs/08 §8.4）。"""
    item, _bundle = _v3_bundle(db, user_id=user_a["user_id"], model_output=_complete_output())[:2]
    store = ObjectStore()
    # 用户补充材料 → 新来源版本 + 新的 original_only Bundle；旧提炼Bundle 仍是唯一 ready
    new_source = SourceRevision(
        item_id=item.id, user_id=item.user_id, revision=item.source_revision + 1,
        content_hash=pipeline.sha256_hex(pipeline.canonical_json({"supplement": "new"})),
        metadata_json=dict(_source(db, item).metadata_json), artifacts_json={},
    )
    db.add(new_source)
    db.flush()
    item.source_revision = new_source.revision
    files = _source_files(db, store, item=item, revision=new_source.revision)
    _publish(db, store, item=item, files=files, processing_state="original_only",
             pipeline_state="queued", revision=new_source.revision)

    g = _digest(client, user_a["desktop"]["token"], item.id)["cloud_digest"]
    assert g["state"] == "ready"
    assert g["source_revision"] == 1 and g["bundle_revision"] == 2
    assert g["stale_note"] and "更新" in g["stale_note"] and "r1" not in g["stale_note"]
    assert set(g["segments"]) == {"s0001", "s0002"}


def test_reading_view_expired_bundle_keeps_document_readable(client, user_a, db):
    """到期如实提示，但不把仍可阅读的内容说成没有。"""
    item, _bundle, _doc, _comp = _v3_bundle(db, user_id=user_a["user_id"],
                                            model_output=_complete_output())
    for b in db.query(BundleRevision).filter(BundleRevision.item_id == item.id).all():
        b.expires_at = utcnow() - timedelta(days=1)
    db.commit()

    body = _digest(client, user_a["desktop"]["token"], item.id)
    assert body["expired"] is True and "已过期" in body["note"]
    g = body["cloud_digest"]
    assert g["state"] == "expired" and "已过期" in g["state_detail"]
    assert g["content_document"] is not None  # 材料还在：如实提示到期，不假装没有结果


def test_reading_view_isolates_users_and_files(client, user_a, user_b, db):
    """跨用户不能读取条目，也不能借条目路径读取他人 file_id。"""
    item, _bundle = _v3_bundle(db, user_id=user_a["user_id"], model_output=_complete_output())[:2]
    body = _digest(client, user_a["desktop"]["token"], item.id)
    other = user_b["desktop"]["token"]

    assert client.get(f"/v1/items/{item.id}", headers=auth(other)).status_code == 404
    assert client.get(f"/v1/items/{item.id}/reading", headers=auth(other)).status_code == 404
    fid = body["source_material"]["files"][0]["file_id"]
    rev = body["item"]["bundle_revision"]
    assert client.get(f"/v1/items/{item.id}/bundles/{rev}/files/{fid}",
                      headers=auth(other)).status_code == 404


def test_reading_view_reports_missing_materials(client, user_a):
    """只有 URL 的条目如实说明缺正文，不用标题补写内容。"""
    c = client.post("/v1/captures", json={
        "client_capture_id": "readmiss-1111-2222-3333-444444444444",
        "input_kind": "url", "original_url": "https://example.com/article",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "readmiss"})
    body = _digest(client, user_a["desktop"]["token"], c.json()["item_id"])
    assert "main_content" in body["item"]["missing_materials"]
    assert body["cloud_digest"]["content_document"] is None
