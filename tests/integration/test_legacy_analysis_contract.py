"""提炼提示词契约（v3，docs/24 §3）+ 旧 Schema 1.0/2.0 校验器测试。

- 提示词契约：材料以 `R` 阅读单元给出，主体只有四类内容块，程序字段不由模型回填；
- 旧校验器：`analysis.validate_analysis` 现在只服务历史产物转换（docs/23 §5.1、§8.1），
  这里守住它继续能读 1.0/2.0，迁移时才不用重新猜旧结构。
"""
from __future__ import annotations

import json


from kbserver.domain import analysis, templates

SEGMENTS = [
    {"segment_id": "s0001", "text": "采集与总结应该分开处理，避免混在一起。"},
    {"segment_id": "s0002", "text": "本地整理才决定是否晋升为长期知识。"},
]
SEGMENT_IDS = {s["segment_id"] for s in SEGMENTS}
SEGMENT_TEXTS = {s["segment_id"]: s["text"] for s in SEGMENTS}
MATERIAL = [{"ref": f"R{i}", "text": s["text"]} for i, s in enumerate(SEGMENTS, start=1)]


def base_doc(**overrides) -> dict:
    doc = {
        "schema_version": "2.0",
        "source_revision": 1,
        "summary": "演示摘要。",
        "key_points": [{"claim_id": "c0001", "text": "采集与总结应分开。",
                        "conditions": "适用于多来源采集。", "evidence_ids": ["s0001"]}],
        "excerpts": [{"claim_id": "c0001", "text": SEGMENTS[0]["text"],
                      "evidence_ids": ["s0001"]}],
        "methods": [],
        "insights": [{"text": "候选启发。", "kind": "ai_suggestion", "basis_ids": ["s0002"]}],
        "limitations": [],
        "workflow": None,
    }
    doc.update(overrides)
    return doc


def validate(doc: dict) -> list[str]:
    return analysis.validate_analysis(
        doc, source_revision=1, segment_ids=SEGMENT_IDS, segment_texts=SEGMENT_TEXTS
    )


# ---- 提示词契约 ----

def test_prompt_asks_for_content_blocks_with_task_refs():
    prompt = json.loads(templates.build_digest_user_prompt(
        source_meta={"platform": "web", "coverage": "full_text", "title": "示例"},
        user_note=None, material=MATERIAL, conversation_mode=False,
    ))
    assert [m["ref"] for m in prompt["material"]] == ["R1", "R2"]
    subject = prompt["output_subject"]
    kinds = {b["kind"] for s in subject["sections"] for b in s["blocks"]}
    assert kinds == {"claim", "quote", "suggestion"}
    # 程序字段不进主体，也不要求模型回填（docs/24 §3）
    flat_subject = json.dumps(subject, ensure_ascii=False)
    for forbidden in ("schema_version", "evidence_map", "claim_id", "evidence_ids",
                      "format_version", "references", "source_text_hash"):
        assert forbidden not in flat_subject
    # 云端不输出知识关联、晋升、主题与标签
    flat = json.dumps(prompt, ensure_ascii=False)
    assert "knowledge_id" not in flat and "promotion" not in flat
    rules = "".join(prompt["output_rules"])
    assert "refs 只能使用 material 中出现过的 R 编号" in rules
    assert "不要输出知识关联" in templates.SYSTEM_PROMPT


def test_merge_prompt_carries_only_validated_candidates():
    candidates = [{"chunk": 1, "sections": [{"heading": "主要判断", "blocks": [
        {"kind": "claim", "text": "要点。", "refs": ["R1"]}]}]}]
    prompt = json.loads(templates.build_merge_user_prompt(
        source_meta={"platform": "web"}, user_note=None, candidates=candidates,
        conversation_mode=False,
    ))
    assert prompt["candidates"] == candidates
    assert "material" not in prompt, "汇总阶段不再阅读全文，只能沿用已校验候选"
    rules = "".join(prompt["output_rules"])
    assert "refs 只能沿用被合并候选里的 R 编号" in rules
    assert "quote 必须整条沿用候选" in rules


# ---- 校验 ----

def test_valid_schema_2_document_passes():
    assert validate(base_doc()) == []


def test_claim_id_format_and_uniqueness_enforced():
    """key_points 的 claim_id 必须 c + 4 位且不重复（docs/08 §6.1）。"""
    bad = base_doc(key_points=[{"claim_id": "c1", "text": "x", "evidence_ids": ["s0001"]}])
    assert any("claim_id" in e for e in validate(bad))

    dup = base_doc(key_points=[
        {"claim_id": "c0001", "text": "x", "evidence_ids": ["s0001"]},
        {"claim_id": "c0001", "text": "y", "evidence_ids": ["s0002"]},
    ])
    assert any("重复" in e for e in validate(dup))


def test_excerpt_claim_id_references_existing_key_point():
    """摘录的 claim_id 引用观点，可重复；引用不存在的 id 要拒绝。

    evidence_map 按 claim_id 把摘录合并进观点条目（docs/08 §6.1），因此摘录
    不能占用独立 id——否则观点与摘录在证据链上被拆成两条。
    """
    ok = base_doc(excerpts=[{"claim_id": "c0001", "text": SEGMENTS[0]["text"],
                             "evidence_ids": ["s0001"]}])
    assert validate(ok) == []

    dangling = base_doc(excerpts=[{"claim_id": "c0009", "text": SEGMENTS[0]["text"],
                                   "evidence_ids": ["s0001"]}])
    assert any("必须引用某条 key_points 已有的 claim_id" in e for e in validate(dangling))

    malformed = base_doc(excerpts=[{"claim_id": "e1", "text": SEGMENTS[0]["text"],
                                    "evidence_ids": ["s0001"]}])
    assert any("claim_id" in e for e in validate(malformed))


def test_excerpt_must_be_verbatim_from_cited_segment():
    """摘录改写/拼接/引用错片段都拒绝（docs/08 §3.2、§6.1）。"""
    rewritten = base_doc(excerpts=[{"claim_id": "c0001", "text": "采集与总结要分开。",
                                    "evidence_ids": ["s0001"]}])
    assert any("未在被引用的原文片段中逐字出现" in e for e in validate(rewritten))

    wrong_segment = base_doc(excerpts=[{"claim_id": "c0001", "text": SEGMENTS[1]["text"],
                                        "evidence_ids": ["s0001"]}])
    assert any("未在被引用的原文片段中逐字出现" in e for e in validate(wrong_segment))

    empty = base_doc(excerpts=[])
    assert validate(empty) == []


def test_evidence_ids_must_exist():
    bad = base_doc(key_points=[{"claim_id": "c0001", "text": "x", "evidence_ids": ["s9999"]}])
    assert any("不存在的片段" in e for e in validate(bad))


def test_legacy_schema_1_still_readable():
    """旧版产物仍可读取：1.0 允许 topics、不要求 claim_id（docs/08 §9 旧版兼容）。"""
    legacy = {
        "schema_version": "1.0",
        "source_revision": 1,
        "summary": "旧版摘要。",
        "key_points": [{"text": "旧观点。", "evidence_ids": ["s0001"]}],
        "methods": [],
        "insights": [],
        "topics": ["知识管理"],
        "limitations": [],
    }
    assert validate(legacy) == []

    legacy_no_topics = dict(legacy)
    legacy_no_topics["topics"] = []
    assert validate(legacy_no_topics) == []


def test_evidence_map_is_structured_ids_only():
    """证据映射只保存 ID 与文本，不生成 Obsidian 双链（docs/08 §6.1）。"""
    mapping = analysis.evidence_map(base_doc())
    assert mapping["c0001"]["evidence_ids"] == ["s0001"]
    assert mapping["c0001"]["conditions"] == "适用于多来源采集。"
    # 摘录并入被引用的观点条目，不占独立 claim_id
    assert mapping["c0001"]["excerpt"] == SEGMENTS[0]["text"]
    assert "c0002" not in mapping
    assert "[[" not in json.dumps(mapping)


def test_preview_md_has_no_knowledge_links():
    md = analysis.render_preview_md(base_doc(), user_note=None)
    assert "一句话总结" in md
    assert "核心观点与证据" in md
    assert "值得保留的原文摘录" in md
    assert "方法、适用条件与局限" in md
    assert "AI 候选启发" in md
    # 云端产物不得出现 Obsidian 链接、晋升或知识关联（docs/08 §3.2、§9）
    assert "[[" not in md
    assert "晋升" not in md
    assert "Knowledge" not in md


# ---- 摘录语义完整性：跨相邻片段（docs/02 §11.3）----

def _spans():
    return [
        {"segment_id": "s0001", "text": "他说，其实关键不在于工具。"},
        {"segment_id": "s0002", "text": "而在于你是否真的理解需求。"},
        {"segment_id": "s0003", "text": "之后再去谈方法论才有意义。"},
    ]


def _validate_with_order(doc: dict, segments: list[dict]) -> list[str]:
    return analysis.validate_analysis(
        doc, source_revision=1, segment_ids={s["segment_id"] for s in segments},
        segment_texts={s["segment_id"]: s["text"] for s in segments},
        segment_order=[s["segment_id"] for s in segments],
    )


def test_excerpt_may_span_adjacent_segments():
    """摘录跨相邻片段：按原文顺序拼接后逐字比对，乱序列出也接受。"""
    segments = _spans()
    text = "关键不在于工具。而在于你是否真的理解需求。"
    doc = base_doc(excerpts=[{"claim_id": "c0001", "text": text,
                              "evidence_ids": ["s0001", "s0002"]}])
    assert _validate_with_order(doc, segments) == []

    unsorted = base_doc(excerpts=[{"claim_id": "c0001", "text": text,
                                   "evidence_ids": ["s0002", "s0001"]}])
    assert _validate_with_order(unsorted, segments) == []


def test_excerpt_must_not_join_nonadjacent_segments():
    """被引片段不相邻（跳过中间片段）要拒绝，防止拼接不相邻的话。"""
    segments = _spans()
    doc = base_doc(excerpts=[{"claim_id": "c0001",
                              "text": "关键不在于工具。之后再去谈方法论才有意义。",
                              "evidence_ids": ["s0001", "s0003"]}])
    errs = _validate_with_order(doc, segments)
    assert any("相邻" in e for e in errs)


def test_quote_rules_reach_every_generation_prompt():
    """逐字摘录与「只选给定 R」写进提炼、分块与修复三类提示词。"""
    raw_digest = templates.build_digest_user_prompt(
        source_meta={"platform": "web"}, user_note=None, material=MATERIAL,
        conversation_mode=False)
    digest = json.loads(raw_digest)
    assert digest["material"] == MATERIAL, "材料以阅读单元 + R 编号交给模型"
    assert "逐字" in templates.SYSTEM_PROMPT

    chunk = json.loads(templates.build_chunk_user_prompt(
        source_meta={"platform": "web"}, material=MATERIAL[:1], chunk_index=1, chunk_total=2))
    assert chunk["chunk"] == {"index": 1, "total": 2}
    chunk_rules = "".join(chunk["output_rules"])
    assert "refs 只能使用本段 material 中出现过的 R 编号" in chunk_rules
    assert "逐字" in chunk_rules

    repair = json.loads(templates.build_repair_user_prompt(
        raw_digest, "{}",
        [{"code": "quote_not_verbatim", "message": "摘录未逐字", "block": [1, 2]}]))
    assert repair["material"] == MATERIAL, "修复调用仍要能选到正确的 R"
    assert repair["validation_errors"][0]["block"] == [1, 2]


def test_plan_chunks_keeps_paragraphs_whole():
    """分块边界对齐段落：同一段落的片段不拆到两块。"""
    segments = [{"segment_id": f"s{i:04d}", "text": "字" * 50} for i in range(1, 25)]
    paragraphs = [
        {"paragraph_id": f"p{i:04d}", "segment_ids": [f"s{i * 2 - 1:04d}", f"s{i * 2:04d}"]}
        for i in range(1, 13)
    ]
    chunks = templates.plan_chunks(segments, 1801, paragraphs)
    assert len(chunks) > 1  # 确实分了块
    owner = {}
    for ci, chunk in enumerate(chunks):
        for s in chunk:
            owner[s["segment_id"]] = ci
    for p in paragraphs:
        blocks = {owner[sid] for sid in p["segment_ids"]}
        assert len(blocks) == 1, f"段落 {p['paragraph_id']} 被拆到多块"
