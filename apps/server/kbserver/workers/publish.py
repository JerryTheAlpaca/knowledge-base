"""共享发布路径：适配器产出 segments → 新不可变来源版本 + Bundle → enrich。

从 worker._publish_segments_revision 小范围移出（docs/11 §8），供字幕/网页
提取与 ASR 发布共用；语义不变：旧版本不修改，enrich 按 item.source_revision
校验片段；重新提取内容无变化则不新增版本。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..domain import pipeline
from ..extractors import paragraphs as parafmt
from ..extractors import subtitles as subfmt
from ..models import BundleRevision, Item, Job, SourceRevision, StoredFile
from ..storage.objects import ObjectStore


def bundle_files(db: Session, item: Item) -> list[StoredFile]:
    rows = list(db.query(StoredFile).filter(StoredFile.item_id == item.id, StoredFile.user_id == item.user_id))
    return pipeline.latest_files_per_path(rows)


def auto_enrich_enabled(db: Session, user_id: str) -> bool:
    """用户级「AI 自动加工」开关（默认开）：关闭时提取完成后不做 AI 加工，
    条目停在 extracted 状态等待手动「重新加工」。手动重试不受它影响。"""
    from ..models import User

    user = db.get(User, user_id)
    ai = (user.settings_json or {}).get("ai") if user else None
    if not isinstance(ai, dict):
        return True
    return bool(ai.get("auto_enrich", True))


def publish_segments_revision(db: Session, store: ObjectStore, job: Job, item: Item,
                              source: SourceRevision, *, segments: list[dict],
                              warnings: list[str], extra_files: list,
                              meta_updates: dict,
                              missing_materials: list[str] | None = None) -> None:
    """适配器产出了新材料/新规范正文 → 新增不可变来源版本并发布，随后入 enrich。

    旧版本不修改（docs/02 §6.1）；enrich 按 item.source_revision 校验片段（A13）。
    重新提取时内容与缺失情况均无变化则不新增版本（docs/02 §10.1 refetch 语义）。
    """
    missing = missing_materials if missing_materials is not None else []
    content_hash = pipeline.sha256_hex(
        pipeline.canonical_json({"segments": segments, "meta_updates": meta_updates})
    )
    if content_hash == source.content_hash and source.metadata_json.get("missing_materials", []) == missing:
        job.state = "succeeded"
        bundle = None
        if item.bundle_revision:
            bundle = db.query(BundleRevision).filter(
                BundleRevision.user_id == item.user_id,
                BundleRevision.item_id == item.id,
                BundleRevision.revision == item.bundle_revision,
            ).one_or_none()
        auto_enrich = auto_enrich_enabled(db, item.user_id)
        if item.pipeline_state == "failed" or (auto_enrich and bundle is not None and bundle.processing_state != "ready"):
            # 提取结果没变：在同一版本上重新加工（failed 重试不受「AI 自动加工」
            # 开关影响；等待 Key/预算的会在 enrich 预备阶段回到原等待状态）
            pipeline.enqueue_stage(
                db, user_id=item.user_id, item_id=item.id, source_revision=source.revision,
                stage="enrich", reset_attempt=True,
            )
            item.pipeline_state = "queued"
            item.state_detail = "重新提取：内容无变化，重新加工"
        else:
            ready = bundle is not None and bundle.processing_state == "ready"
            item.pipeline_state = "ready" if ready else "extracted"
            item.state_detail = ("重新提取：来源内容无变化" if ready
                                 else "重新提取：来源内容无变化；AI 自动加工已关闭")
        pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                            event_type="refetch_unchanged", payload={"revision": source.revision})
        return

    new_revision = source.revision + 1
    meta2 = dict(source.metadata_json)
    meta2.update(meta_updates)
    meta2["missing_materials"] = missing
    source2 = SourceRevision(
        item_id=item.id, user_id=item.user_id, revision=new_revision,
        content_hash=content_hash,
        metadata_json=meta2, artifacts_json={},
    )
    db.add(source2)
    db.flush()
    item.source_revision = new_revision

    # extra_files 是本次最新登记（可能覆盖同 path 旧版本），按 path 去重合并
    merged = {f.relative_path: f for f in bundle_files(db, item)}
    for f in extra_files:
        merged[f.relative_path] = f
    files = list(merged.values())
    # 阅读层：段落只是合并相邻片段，segments 仍是引用粒度（extractors/paragraphs.py）
    paragraph_list = parafmt.group_paragraphs(segments)
    segment_para = parafmt.segment_paragraph_map(paragraph_list)
    indexed_segments = [
        dict(seg, paragraph_id=segment_para.get(seg.get("segment_id")))
        for seg in segments
    ]
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=subfmt.segments_to_normalized_md(segments).encode("utf-8"),
        relative_path="normalized.md", role="source_material", mime="text/markdown",
    ))
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=parafmt.paragraphs_to_readable_md(paragraph_list).encode("utf-8"),
        relative_path="readable.md", role="source_material", mime="text/markdown",
    ))
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=pipeline.canonical_json({
            "source_revision": new_revision,
            "segments": indexed_segments,
            "paragraphs": paragraph_list,
        }),
        relative_path="segments.json", role="source_material", mime="application/json",
    ))
    db.flush()

    auto_enrich = auto_enrich_enabled(db, item.user_id)
    pipeline.publish_bundle(
        db, store, item=item, source=source2, files=files,
        processing_state="original_only",
        pipeline_state="enriching" if auto_enrich else "extracted",
        warnings=warnings,
    )
    job.state = "succeeded"
    if auto_enrich:
        pipeline.enqueue_stage(
            db, user_id=item.user_id, item_id=item.id, source_revision=new_revision, stage="enrich"
        )
