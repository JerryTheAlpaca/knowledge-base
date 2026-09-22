"""docs/23 §8.1、docs/24 §7：旧提炼产物 → ContentDocument v3 的历史转换器。

只覆盖迁移真正会出错的地方：
- 旧证据定位不到 → legacy_evidence_unresolved，不编造来源；
- 同一 s0001 属于两个来源版本 → 按产物当时依据的那一版解析，不取最新 segments.json；
- 同来源版本被 AI 纠错改写过正文 → 回退成产物引用的那版文本；
- workflow 的提出/接受/否定/未知状态不丢失；
- 同输入重跑是空操作，且旧 Bundle、manifest 与历史回执不被修改。

apply 路径注入一个按 docs/24 §11 签名实现的最小组装器：它只验证 W4 的编排（引用键、
写新版本、迁移台账、幂等），组装器自身的行为由 W1 的契约测试负责。
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

from kbserver.domain import content_migration as migrate
from kbserver.domain import pipeline
from kbserver.models import (
    BundleRevision,
    ContentMigration,
    Device,
    Job,
    Receipt,
    SourceRevision,
    StoredFile,
    User,
    new_id,
)
from kbserver.storage.objects import ObjectStore

SEG_R1 = [
    {"segment_id": "s0001", "text": "采集与总结应该分开处理。"},
    {"segment_id": "s0002", "text": "本地整理才决定是否晋升为长期知识。"},
]
# 同一批 segment_id、不同文本：来源版本 r2（用户补充/编辑后的新正文）
SEG_R2 = [
    {"segment_id": "s0001", "text": "采集与总结分开处理，这一点后来被推翻。"},
    {"segment_id": "s0002", "text": "本地整理不再是唯一入口。"},
]


def legacy_doc(**overrides) -> dict:
    """Schema 2.0 的真实形状（domain/analysis.py、templates._output_schema_v2）。"""
    doc = {
        "schema_version": "2.0",
        "source_revision": 1,
        "summary": "先保存，再提炼。",
        "key_points": [{"claim_id": "c0001", "text": "采集与整理应分开。",
                        "conditions": "多来源时更明显。", "evidence_ids": ["s0001"]}],
        "excerpts": [{"claim_id": "c0001", "text": SEG_R1[0]["text"], "evidence_ids": ["s0001"]}],
        "methods": [{"text": "分两阶段处理。", "steps": ["先落原文", "再提炼"],
                     "conditions": "需要固定版本", "evidence_ids": ["s0001", "s0002"]}],
        "insights": [{"text": "可以分别统计两阶段失败。", "kind": "ai_suggestion",
                      "basis_ids": ["s0002"]}],
        "limitations": ["字幕可能不完整。"],
        "workflow": None,
        "evidence_map": {"c0001": {"kind": "key_point", "text": "采集与整理应分开。",
                                   "conditions": "多来源时更明显。", "evidence_ids": ["s0001"]}},
    }
    doc.update(overrides)
    return doc


def segments_doc(segments: list[dict], revision: int, **extra) -> dict:
    return {"source_revision": revision, "segments": [dict(s) for s in segments],
            "paragraphs": [], **extra}


def _user(db) -> str:
    user = User(name="迁移用户")
    db.add(user)
    db.flush()
    return user.id


def _drop_jobs(db, item_id: str) -> None:
    """本文件不跑 worker：清掉 create_capture 入队的任务。

    tests/integration 共用一个 SQLite 库，别的用例用 `_drain_worker` 空跑队列，
    留着排队任务会让它把轮数花在本用例的条目上。
    """
    db.query(Job).filter(Job.item_id == item_id).delete()


def _drop_queued_jobs(db, item_id: str) -> None:
    """create_capture 会入队 extract；本文件不跑 worker，留着会让同库其它用例的
    `_drain_worker` 先处理这些任务（tests/integration 共用一个 SQLite 库）。"""
    db.query(Job).filter(Job.item_id == item_id).delete(synchronize_session=False)


def make_item(db, store, *, user_id: str, seg_doc: dict | None = None,
              doc: dict | None = None, source_revision: int = 1,
              with_source_text: bool = True, processing_state: str = "ready"):
    """按真实接收路径建条目，再发布一个带旧产物的 Bundle（等同 enrich 落盘的结果）。"""
    _capture, item = pipeline.create_capture(
        db, store, user_id=user_id,
        payload={"schema_version": "1.0", "client_capture_id": new_id(),
                 "input_kind": "text", "text": "演示正文",
                 "archive_policy": "source_materials"},
        uploads={},
    )
    _drop_jobs(db, item.id)
    files = []
    if with_source_text:
        payload = seg_doc if seg_doc is not None else segments_doc(SEG_R1, source_revision)
        files.append(pipeline.register_file(
            db, store, user_id=user_id, item_id=item.id,
            data=pipeline.canonical_json(payload), relative_path="segments.json",
            role="source_material", mime="application/json"))
        files.append(pipeline.register_file(
            db, store, user_id=user_id, item_id=item.id,
            data="\n".join(s["text"] for s in payload["segments"]).encode("utf-8"),
            relative_path="normalized.md", role="source_material", mime="text/markdown"))
    analysis = doc if doc is not None else legacy_doc()
    files.append(pipeline.register_file(
        db, store, user_id=user_id, item_id=item.id,
        data=pipeline.canonical_json(analysis), relative_path="analysis.json",
        role="generated", mime="application/json"))
    source = db.query(SourceRevision).filter(
        SourceRevision.item_id == item.id,
        SourceRevision.revision == source_revision).one()
    pipeline.publish_bundle(
        db, store, item=item, source=source, files=files,
        processing_state=processing_state,
        pipeline_state="ready" if processing_state == "ready" else "extracted",
        result_file_id=files[-1].file_id)
    db.commit()
    return item


def load_artifact(db, store, item, revision: int | None = None):
    bundle = db.query(BundleRevision).filter(
        BundleRevision.item_id == item.id,
        BundleRevision.revision == (revision or item.bundle_revision)).one()
    art = migrate.load_legacy_artifact(db, store, item=item, bundle=bundle)
    assert art is not None
    return art


def sections_of(plan) -> dict[str, list[dict]]:
    return {s["heading"]: s["blocks"] for s in plan.subject["sections"]}


# ---- 证据解析 ----

def test_legacy_evidence_resolves_to_real_segment_ranges(db):
    store = ObjectStore()
    item = make_item(db, store, user_id=_user(db))
    plan = migrate.plan_conversion(load_artifact(db, store, item))

    assert plan.unresolved_ids == []
    assert plan.status == "complete"
    material = plan.materials[0]
    # 依据落成该版原文里真实的片段范围，顺序即原文顺序
    assert material["item_id"] == item.id and material["source_revision"] == 1
    assert [s["segment_id"] for s in material["segments"]] == ["s0001", "s0002"]
    blocks = sections_of(plan)
    assert blocks[migrate.H_KEY_POINTS][0]["evidence"] == ["s0001"]
    assert blocks[migrate.H_EXCERPTS][0]["evidence"] == ["s0001"]
    # 跨相邻片段的方法依据按原文顺序保留，R 键由组装器的阅读单元分组决定
    assert blocks[migrate.H_METHODS][0]["evidence"] == ["s0001", "s0002"]
    assert blocks[migrate.H_METHODS][1]["evidence"] == ["s0001", "s0002"]  # 步骤沿用方法依据
    assert blocks[migrate.H_INSIGHTS][0]["kind"] == "suggestion"
    assert "适用条件：多来源时更明显。" in blocks[migrate.H_KEY_POINTS][0]["text"]
    assert plan.subject["limitations"][:1] == ["字幕可能不完整。"]


class _Units:
    """假引用表：R1 覆盖 s0001+s0002，R2 覆盖 s0003（一个 R 可跨多个片段）。"""

    class _Entry:
        def __init__(self, ids):
            self.segment_ids = ids

    def keys(self):
        return ["R1", "R2"]

    def get(self, ref):
        return self._Entry({"R1": ["s0001", "s0002"], "R2": ["s0003"]}[ref])


def test_evidence_maps_onto_assembler_reading_units(db):
    """旧证据 → R 键的换算跟随组装器的分组，不按下标自造编号（docs/24 §2）。"""
    store = ObjectStore()
    doc = legacy_doc(key_points=[
        {"claim_id": "c0001", "text": "同单元内两点。", "evidence_ids": ["s0002", "s0001"]},
        {"claim_id": "c0002", "text": "跨到下一单元。", "evidence_ids": ["s0002", "s0003"]},
    ], excerpts=[], methods=[], insights=[])
    item = make_item(db, store, user_id=_user(db), doc=doc,
                     seg_doc=segments_doc(SEG_R1 + [{"segment_id": "s0003", "text": "第三段。"}], 1))
    plan = migrate.plan_conversion(load_artifact(db, store, item))
    output = migrate.model_output_for(plan, _Units())

    first, second = output["sections"][0]["blocks"]
    assert first["refs"] == ["R1"]           # 乱序引用按表序归一
    assert second["refs"] == ["R1", "R2"]
    assert "evidence" not in first           # 交给组装器的只有 R 键


def test_unresolvable_evidence_is_marked_and_never_fabricated(db):
    store = ObjectStore()
    doc = legacy_doc(
        key_points=[{"claim_id": "c0001", "text": "引用了不存在的片段。",
                     "evidence_ids": ["s0009"]}],
        excerpts=[], methods=[], insights=[])
    item = make_item(db, store, user_id=_user(db), doc=doc)
    plan = migrate.plan_conversion(load_artifact(db, store, item))

    assert plan.unresolved_ids == ["s0009"]
    assert plan.status == "unresolved"
    assert migrate.UNRESOLVED in plan.missing_stages
    # 定位不到就交空依据：不拿别的片段或整篇原文冒充来源
    assert sections_of(plan)[migrate.H_KEY_POINTS][0]["evidence"] == []
    gap = next(g for g in plan.gaps if g["code"] == migrate.UNRESOLVED)
    assert gap["segment_ids"] == ["s0009"]
    assert any("未能定位" in lim for lim in plan.subject["limitations"])


def test_missing_source_text_marks_all_cited_evidence_unresolved(db):
    store = ObjectStore()
    item = make_item(db, store, user_id=_user(db), with_source_text=False)
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)

    assert art.source is None and art.source_note == "none"
    assert set(plan.unresolved_ids) == {"s0001", "s0002"}
    assert plan.status == "unresolved"
    assert plan.materials == []


def test_same_segment_id_in_two_source_versions_resolves_to_referenced_one(db):
    """两份材料都有 s0001：旧产物按它当时依据的那一版解析，不取最新 segments.json。"""
    store = ObjectStore()
    user_id = _user(db)
    item = make_item(db, store, user_id=user_id)
    legacy_bundle = item.bundle_revision

    # 用户编辑原文 → 新不可变来源版本 r2：同名的 s0001 已是另一段话
    source2 = SourceRevision(
        item_id=item.id, user_id=user_id, revision=2, content_hash="x" * 64,
        metadata_json={"platform": "web", "edited_by_user": True}, artifacts_json={})
    db.add(source2)
    item.source_revision = 2
    seg2 = pipeline.register_file(
        db, store, user_id=user_id, item_id=item.id,
        data=pipeline.canonical_json(segments_doc(SEG_R2, 2)),
        relative_path="segments.json", role="source_material", mime="application/json")
    pipeline.publish_bundle(db, store, item=item, source=source2, files=[seg2],
                            processing_state="original_only", pipeline_state="extracted")
    db.commit()

    art = load_artifact(db, store, item, revision=legacy_bundle)
    plan = migrate.plan_conversion(art)

    assert art.source_revision == 1 and art.stale_source is True
    assert art.edited_by_user is False          # 标记在新版本上，不改历史判定
    assert plan.materials[0]["source_revision"] == 1
    assert plan.materials[0]["segments"][0]["text"] == SEG_R1[0]["text"]
    assert plan.unresolved_ids == []
    assert sections_of(plan)[migrate.H_EXCERPTS][0]["text"] == SEG_R1[0]["text"]


def test_ai_corrected_text_under_same_revision_is_reverted(db):
    """同一 source_revision 被 AI 纠错改写过：产物引用的是纠错前的文本。"""
    store = ObjectStore()
    user_id = _user(db)
    corrected = dict(SEG_R1[0], text="采集与总结应该分开处理，避免混在一起。")
    item = make_item(db, store, user_id=user_id, seg_doc=segments_doc(
        [corrected, SEG_R1[1]], 1, paragraph_source="ai",
        ai_corrections=[{"segment_id": "s0001", "original": SEG_R1[0]["text"],
                         "corrected": corrected["text"]}]))
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)

    assert art.ai_corrected is True
    assert plan.materials[0]["segments"][0]["text"] == SEG_R1[0]["text"]
    assert plan.unresolved_ids == []


def test_ai_correction_without_original_is_unresolved(db):
    """记了纠错却没留下原句：当时的文本无从判定，标 unresolved 而不是照用纠错版。"""
    store = ObjectStore()
    corrected = dict(SEG_R1[0], text="采集与总结应该分开处理，避免混在一起。")
    item = make_item(db, store, user_id=_user(db), seg_doc=segments_doc(
        [corrected, SEG_R1[1]], 1, paragraph_source="ai",
        ai_corrections=[{"segment_id": "s0001", "corrected": corrected["text"]}]))
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)

    assert art.source is None
    assert plan.status == "unresolved"
    assert plan.unresolved_ids


# ---- 旧协议差异与 workflow ----

def test_legacy_schema_1_has_no_claim_ids_and_still_converts(db):
    store = ObjectStore()
    doc = legacy_doc(schema_version="1.0", source_revision=None, excerpts=[], methods=[],
                     insights=[], topics=["知识管理"],
                     key_points=[{"text": "旧版观点没有编号。", "evidence_ids": ["s0001"]}])
    item = make_item(db, store, user_id=_user(db), doc=doc)
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)

    assert art.schema_version == "1.0"
    assert art.provenance_unverified is False   # 产物没自报版本时按清单，不算不一致
    assert sections_of(plan)[migrate.H_KEY_POINTS][0]["evidence"] == ["s0001"]
    assert plan.status == "complete"


def test_workflow_conversation_state_is_preserved(db):
    store = ObjectStore()
    doc = legacy_doc(workflow={
        "problem": "要不要保留观点编号",
        "constraints": ["不能把接口改两次"],
        "decisions": [
            {"text": "取消持久观点 ID", "status": "accepted", "evidence_ids": ["s0001"]},
            {"text": "保留两跳引用", "status": "rejected", "evidence_ids": ["s0002"]},
            {"text": "改成按段落引用", "status": "proposed", "evidence_ids": []},
            {"text": "旧插件是否需要同步升级", "status": "unknown", "evidence_ids": []},
        ],
        "abandoned": ["放弃双写两套协议"],
        "attempts": ["先试兼容层，再迁移"],
        "result": "选定 ContentDocument v3",
    })
    item = make_item(db, store, user_id=_user(db), doc=doc)
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)

    assert art.has_workflow is True
    blocks = sections_of(plan)[migrate.H_WORKFLOW]
    assert any(b["text"].startswith("要解决的问题：") for b in blocks)
    assert any(b["text"].startswith("约束：") for b in blocks)
    accepted = next(b for b in blocks if "【接受】" in b["text"])
    rejected = next(b for b in blocks if "【否定】" in b["text"])
    proposed = next(b for b in blocks if "【提出】" in b["text"])
    unknown = next(b for b in blocks if "【未确认】" in b["text"])
    # 接受/否定是来源表态过的结论 → claim 并带依据；提出/未知仍是候选 → suggestion
    assert accepted["kind"] == "claim" and accepted["evidence"] == ["s0001"]
    assert rejected["kind"] == "claim" and rejected["evidence"] == ["s0002"]
    assert proposed["kind"] == "suggestion" and unknown["kind"] == "suggestion"
    assert any("【放弃】" in b["text"] for b in blocks)
    assert any("【试错】" in b["text"] for b in blocks)
    assert any(b["text"].startswith("结果：") for b in blocks)
    assert plan.status == "complete"


def test_preview_only_bundle_is_not_reported_as_converted(db):
    store = ObjectStore()
    user_id = _user(db)
    _capture, item = pipeline.create_capture(
        db, store, user_id=user_id,
        payload={"schema_version": "1.0", "client_capture_id": new_id(),
                 "input_kind": "text", "text": "演示正文",
                 "archive_policy": "source_materials"},
        uploads={},
    )
    _drop_jobs(db, item.id)
    source = db.query(SourceRevision).filter(SourceRevision.item_id == item.id).one()
    preview = pipeline.register_file(
        db, store, user_id=user_id, item_id=item.id, data="# 旧预览\n".encode("utf-8"),
        relative_path="preview.md", role="preview", mime="text/markdown")
    pipeline.publish_bundle(db, store, item=item, source=source, files=[preview],
                            processing_state="ready", pipeline_state="ready")
    db.commit()

    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)
    assert art.kind == "preview_only"
    # 没有主体可转：不能算进完整成功的迁移结果
    assert plan.status == "failed"
    assert any(g["code"] == "empty_document" for g in plan.gaps)


# ---- apply：新 Bundle + 台账，且可重入 ----

class FakeContentV3:
    """docs/24 §11 签名的最小实现：只验证 W4 编排（引用表、写版本、台账、幂等）。

    分组取「一片段一单元」，组装规则本身由 W1 的契约测试负责，这里不重复实现。
    """

    def __init__(self):
        self.calls: list[dict] = []

    def install(self, monkeypatch):
        module = types.ModuleType("kbserver.domain.content_v3")
        module.CONTENT_FORMAT_VERSION = "3.0"
        module.CONTENT_RECIPE_VERSION = "content-v3-1"

        class Material:
            def __init__(self, item_id, source_revision, segments):
                self.item_id = item_id
                self.source_revision = source_revision
                self.segments = segments

        class Entry:
            def __init__(self, item_id, source_revision, segment_ids):
                self.item_id = item_id
                self.source_revision = source_revision
                self.segment_ids = segment_ids

        class Table:
            def __init__(self, entries):
                self._entries = entries

            def keys(self):
                return list(self._entries)

            def get(self, ref):
                return self._entries[ref]

        def build_ref_table(materials):
            entries: dict[str, Entry] = {}
            counter = 0
            for material in materials:
                for seg in material.segments:
                    counter += 1
                    entries[f"R{counter}"] = Entry(
                        material.item_id, material.source_revision, [seg["segment_id"]])
            return Table(entries)

        def assemble(model_output, **kw):
            self.calls.append({"subject": model_output, **kw})
            references = {ref: {"segment_ids": [ref]}
                          for sec in model_output["sections"] for b in sec["blocks"]
                          for ref in b["refs"]}
            missing = list(kw.get("missing_stages") or [])
            gaps = list(kw.get("extra_gaps") or [])
            completeness = {
                "state": "partial" if (missing or gaps) else "complete",
                "missing_stages": missing, "gaps": gaps,
                "dropped_blocks": 0, "repair_calls": 0,
            }
            doc = {
                "format_version": "3.0", "document_id": kw["document_id"],
                "kind": kw["kind"], "revision": kw["revision"],
                "title": model_output["title"] or (kw.get("source_title") or ""),
                "summary": model_output["summary"],
                "sections": model_output["sections"], "references": references,
                "limitations": model_output["limitations"],
                "created_at": kw.get("created_at") or "",
                "completeness": completeness,
            }
            return doc, SimpleNamespace(document=doc, errors=[],
                                        completeness=completeness,
                                        dropped_blocks=0, repair_calls=0)

        module.Material = Material
        module.build_ref_table = build_ref_table
        module.assemble_content_document = assemble
        module.validate_content_document = lambda doc: []
        module.render_content_markdown = lambda doc: "# 迁移产物\n"
        monkeypatch.setitem(sys.modules, "kbserver.domain.content_v3", module)
        monkeypatch.setattr("kbserver.domain.content_v3", module, raising=False)
        return self


def test_apply_writes_new_bundle_and_re_run_is_noop(db, monkeypatch):
    store = ObjectStore()
    user_id = _user(db)
    item = make_item(db, store, user_id=user_id)
    old_revision = item.bundle_revision
    old_bundle = db.query(BundleRevision).filter(
        BundleRevision.item_id == item.id, BundleRevision.revision == old_revision).one()
    old_key, old_sha = old_bundle.manifest_key, old_bundle.manifest_sha256
    old_analysis_sha = db.query(StoredFile).filter(
        StoredFile.item_id == item.id, StoredFile.relative_path == "analysis.json").one().sha256
    device = Device(user_id=user_id, kind="desktop", name="本机")
    db.add(device)
    db.flush()
    db.add(Receipt(user_id=user_id, item_id=item.id, bundle_revision=old_revision,
                   device_id=device.id, manifest_sha256=old_sha))
    db.commit()

    assembly = FakeContentV3().install(monkeypatch)
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)
    result = migrate.apply_migration(db, store, item=item, artifact=art, plan=plan)
    db.commit()

    assert result["status"] == "complete"
    assert result["bundle_revision"] == old_revision + 1
    call = assembly.calls[0]
    assert call["kind"] == "digest" and call["document_id"] == f"dig-{item.id}"
    assert call["recipe_version"] == "content-v3-1" and call.get("repair_calls", 0) == 0
    assert call["subject"]["sections"]  # 主体与 R 键一起交给组装器

    new_bundle = db.query(BundleRevision).filter(
        BundleRevision.item_id == item.id,
        BundleRevision.revision == old_revision + 1).one()
    manifest = json.loads(store.read_object(new_bundle.manifest_key).decode("utf-8"))
    paths = pipeline.manifest_files_by_path(manifest)
    assert {"content.json", "preview.md", "segments.json"} <= set(paths)
    assert "analysis.json" not in paths          # 旧产物不带进新版本
    assert manifest["processing"]["format_version"] == "3.0"
    assert manifest["processing"]["completeness"] == "complete"
    assert manifest["processing"]["recipe_version"] == "content-v3-1"
    assert manifest["processing"]["source_revision"] == 1
    assert paths["content.json"]["role"] == "generated"

    # 旧 Bundle、manifest 与历史回执原样不动
    db.expire_all()
    kept = db.query(BundleRevision).filter(
        BundleRevision.item_id == item.id, BundleRevision.revision == old_revision).one()
    assert (kept.manifest_key, kept.manifest_sha256) == (old_key, old_sha)
    assert db.query(StoredFile).filter(
        StoredFile.item_id == item.id, StoredFile.relative_path == "analysis.json").one().sha256 \
        == old_analysis_sha
    assert db.query(Receipt).filter(Receipt.item_id == item.id).count() == 1

    row = db.query(ContentMigration).filter(ContentMigration.item_id == item.id).one()
    assert (row.status, row.converter_version) == ("complete", migrate.CONVERTER_VERSION)
    assert row.new_bundle_revision == old_revision + 1

    # 重跑：同输入 + 同转换器版本 → 空操作，不新增 Bundle
    art2 = load_artifact(db, store, item, revision=old_revision)
    plan2 = migrate.plan_conversion(art2)
    assert plan2.input_sha256 == plan.input_sha256
    again = migrate.apply_migration(db, store, item=item, artifact=art2, plan=plan2)
    db.commit()
    assert again["status"] == "skipped"
    assert db.query(ContentMigration).filter(ContentMigration.item_id == item.id).count() == 1
    assert db.query(BundleRevision).filter(
        BundleRevision.item_id == item.id).count() == old_revision + 1
    # 已经是 v3 的条目不再进待迁移清单
    migrated_items = {i.id for i, _b in migrate.legacy_candidate_bundles(db, user_id=user_id)}
    assert item.id not in migrated_items


def test_unresolved_result_is_not_recorded_complete(db, monkeypatch):
    store = ObjectStore()
    doc = legacy_doc(key_points=[{"claim_id": "c0001", "text": "无据结论。",
                                 "evidence_ids": ["s0009"]}],
                     excerpts=[], methods=[], insights=[])
    item = make_item(db, store, user_id=_user(db), doc=doc)
    FakeContentV3().install(monkeypatch)
    art = load_artifact(db, store, item)
    plan = migrate.plan_conversion(art)
    result = migrate.apply_migration(db, store, item=item, artifact=art, plan=plan)
    db.commit()

    assert result["status"] == "unresolved"
    latest = db.query(BundleRevision).filter(
        BundleRevision.item_id == item.id).order_by(BundleRevision.revision.desc()).first()
    manifest = json.loads(store.read_object(latest.manifest_key).decode("utf-8"))
    # 有缺口就不借用「完成」：completeness、缺口与 state_reason 都要说清
    assert manifest["processing"]["completeness"] == "partial"
    assert item.state_reason == "partial_result"
    content = json.loads(store.read_object(
        db.query(StoredFile).filter(StoredFile.user_id == item.user_id,
                                    StoredFile.file_id
                                    == manifest["processing"]["content_file_id"]).one().storage_key
    ).decode("utf-8"))
    assert migrate.UNRESOLVED in json.dumps(content["completeness"], ensure_ascii=False)
    assert db.query(ContentMigration).filter(
        ContentMigration.item_id == item.id).one().status == "unresolved"


# ---- M0 统计 ----

def test_inventory_reports_counts_and_labels_only(db):
    store = ObjectStore()
    user_id = _user(db)
    make_item(db, store, user_id=user_id)
    make_item(db, store, user_id=user_id, doc=legacy_doc(
        schema_version="1.0", source_revision=None, excerpts=[], methods=[], insights=[],
        key_points=[{"text": "旧版没有观点编号。", "evidence_ids": ["s0001"]}],
        workflow={"problem": "问题", "constraints": [],
                  "decisions": [{"text": "先迁移", "status": "accepted",
                                 "evidence_ids": ["s0001"]}],
                  "abandoned": [], "attempts": [], "result": None}))

    counts = migrate.inventory(db, store, user_id=user_id)
    assert counts["bundles"] == 2 and counts["items"] == 2
    assert counts["schema_1_0"] == 1 and counts["schema_2_0"] == 1
    assert counts["workflow_bearing"] == 1
    assert counts["status_complete"] == 2
    assert counts["files"] >= 6 and counts["bytes"] > 0

    text = migrate.format_inventory(counts)
    assert "待迁移产物统计" in text
    # 只出计数与类别标签：不打印私人正文、标题或凭据
    assert "采集与总结" not in text and "旧版没有观点编号" not in text
