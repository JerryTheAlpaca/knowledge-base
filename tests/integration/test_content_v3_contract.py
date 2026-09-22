"""docs/24 §1–§4、§10：ContentDocument v3 契约测试（M0，W1 交付）。

覆盖三件事，断言取自 `tests/fixtures/content_v3/expectations_*.json` 与
`document_*.json` 里写死的期望，不重复实现本身：

- 引用表：多来源同号片段互不覆盖、JSON 往返稳定、定位元数据保留；
- 组装：R→真实原文→e 的改写、逐字校验、块级失败范围、完整性状态；
- 镜像一致性：同一批文档同时通过 `validate_content_document` 与
  `contracts/content_document_v3.schema.json`，且 Schema 的上限/枚举与 Python 常量一致。

旧协议样本（`legacy_*`）按 `domain/analysis.py` 现学校验器构造，供 W4 迁移器读取；
`ref_table_short_article.json` 里的片段原文就是它们的来源。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from kbserver.domain import analysis
from kbserver.domain import content_v3 as c3

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "content_v3"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "content_document_v3.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

ITEM_A = "itm-11111111-1111-4111-8111-111111111111"
ITEM_B = "itm-22222222-2222-4222-8222-222222222222"
ITEM_C = "itm-33333333-3333-4333-8333-333333333333"
ITEM_D = "itm-44444444-4444-4444-8444-444444444444"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def table(name: str) -> c3.RefTable:
    return c3.RefTable.from_json(load(name))


def long_subtitle_assembly(**overrides):
    """长字幕样本的主体在 model_output_long_subtitle.json，期望在 expectations_long_subtitle.json。"""
    return assemble("expectations_long_subtitle.json", ref_table_name="ref_table_long_subtitle.json",
                    output=load("model_output_long_subtitle.json"), **overrides)


def assemble(fixtures_name: str, *, ref_table_name: str, output=None, **overrides):
    """用期望文件里写死的组装参数跑一次组装（fixture 与插件 smoke 共用同一份调用形式）。"""
    fx = load(fixtures_name)
    kwargs = {**fx["assembly"], **overrides}
    subject = output if output is not None else fx.get("model_output", fx.get("raw_output"))
    return c3.assemble_content_document(subject, ref_table=table(ref_table_name), **kwargs)


def short_article_assembly(output, **overrides) -> tuple:
    kwargs = {
        "document_id": f"dig-{ITEM_A}", "kind": "digest", "revision": 4, "item_id": ITEM_A,
        "source_revision": 1, "task": "digest", "recipe_version": c3.CONTENT_RECIPE_VERSION,
        "created_at": "2026-09-22T02:10:00Z", **overrides,
    }
    return c3.assemble_content_document(output, ref_table=table("ref_table_short_article.json"), **kwargs)


# ---------------------------------------------------------------- 引用表


def test_build_ref_table_is_deterministic_and_round_trips():
    """材料顺序决定 R1..Rn；JSON 往返不丢定位元数据，也不改变顺序。"""
    material = c3.Material(
        item_id=ITEM_A,
        source_revision=1,
        segments=[
            {"segment_id": "s0001", "text": "第一句。", "paragraph_id": "p0001"},
            {"segment_id": "s0002", "text": "第二句。", "paragraph_id": "p0001"},
            {"segment_id": "s0003", "text": "第三句。", "paragraph_id": "p0002"},
        ],
    )
    first = c3.build_ref_table([material])
    second = c3.build_ref_table([copy.deepcopy(material)])
    assert first.keys() == ["R1", "R2"]
    assert first.to_json() == second.to_json()
    assert first.text_of("R1") == "第一句。\n第二句。"
    rebuilt = c3.RefTable.from_json(first.to_json())
    assert rebuilt.to_json() == first.to_json()
    assert rebuilt.keys() == first.keys()
    assert rebuilt.text_of("R2") == "第三句。"
    assert rebuilt.get("R2").locator == {"kind": "paragraph", "paragraph_id": "p0002"}
    assert "R3" not in rebuilt
    assert rebuilt.integrity_problems() == []


def test_long_reading_units_split_at_segment_boundaries():
    """单元超过字符预算时按片段边界拆开，不为了少占编号牺牲定位精度。"""
    segments = [{"segment_id": f"s{i:04d}", "text": "字" * 300} for i in range(1, 6)]
    built = c3.build_ref_table([c3.Material(item_id=ITEM_A, source_revision=1, segments=segments)])
    assert built.keys() == ["R1", "R2"]
    assert built.get("R1").segment_ids == ["s0001", "s0002", "s0003", "s0004"]
    assert built.get("R2").segment_ids == ["s0005"]
    assert built.get("R1").locator is None  # 没有时间/段落信息就不假装能定位


def test_two_sources_with_same_segment_id_never_collide():
    """两份材料都有 s0001：各占自己的 R 键与来源版本，合并成一张表也不覆盖。"""
    one = c3.Material(
        item_id=ITEM_C,
        source_revision=2,
        segments=[{"segment_id": "s0001", "text": "材料一的 s0001。", "start_ms": 0, "end_ms": 1500}],
    )
    two = c3.Material(
        item_id=ITEM_D,
        source_revision=1,
        segments=[{"segment_id": "s0001", "text": "材料二的 s0001。", "start_ms": 60000, "end_ms": 62000}],
    )
    built = c3.build_ref_table([one, two])
    assert built.keys() == ["R1", "R2"]
    assert (built.get("R1").item_id, built.get("R1").source_revision) == (ITEM_C, 2)
    assert (built.get("R2").item_id, built.get("R2").source_revision) == (ITEM_D, 1)
    assert built.get("R1").locator == {"kind": "time", "start_ms": 0, "end_ms": 1500}
    assert built.integrity_problems() == []

    # 同一原文范围被绑定两次是程序侧不一致，必须被检出而不是静默覆盖
    duplicated = c3.RefTable.from_json({
        "R1": built.get("R1").to_json(),
        "R2": built.get("R1").to_json(),
    })
    assert duplicated.integrity_problems()


# ---------------------------------------------------------------- 组装：完整与部分


def test_short_article_assembly_ignores_model_program_fields():
    subject = load("model_output_short_article.json")
    doc, report = short_article_assembly(subject)
    assert report.errors == []
    assert report.completeness["state"] == "complete"
    assert doc == load("document_short_article.json")
    # 模型抄来的 format_version/document_id/source_revision/hash 一律不采用
    assert doc["format_version"] == "3.0"
    assert doc["document_id"] == f"dig-{ITEM_A}"
    assert doc["provenance"]["source_revisions"] == [{"item_id": ITEM_A, "source_revision": 1}]
    # " R1 " 与 "R1" 是同一条依据：去空白后精确去重，只生成一个 e 键
    assert doc["sections"][0]["blocks"][0]["refs"] == ["e1"]
    assert doc["sections"][0]["blocks"][1]["refs"] == ["e1"]
    assert len(doc["references"]) == 2


def test_source_text_hash_uses_the_frozen_join_rule():
    """哈希算法与写入/下载/迁移一致：sha256("\\n".join(片段原文))，不做空白归一。"""
    raw = load("ref_table_short_article.json")
    assert raw["R1"]["text"] == "采集与总结应该分开处理，避免混在一起。\n接收环节只负责把原始材料可靠保存下来。"
    doc = load("document_short_article.json")
    assert doc["references"]["e1"]["source_text_hash"] == hashlib.sha256(
        raw["R1"]["text"].encode("utf-8")
    ).hexdigest()


def test_quote_is_checked_against_the_joined_adjacent_range():
    doc, report = short_article_assembly(load("model_output_short_article.json"))
    quote = doc["sections"][0]["blocks"][1]["text"]
    assert " " in quote  # 跨片段、带空白的摘录：只规范化空白，不改动文字
    assert report.completeness["state"] == "complete"
    assert c3.normalize_for_quote(quote) in c3.normalize_for_quote(table("ref_table_short_article.json").text_of("R1"))


def test_long_subtitle_chunk_failure_is_partial_not_complete():
    doc, report = long_subtitle_assembly()
    expect = load("expectations_long_subtitle.json")["expect"]
    assert doc["summary"] == expect["summary"]
    assert doc["title"] == expect["title"]
    assert report.completeness["state"] == expect["state"]
    assert report.completeness["missing_stages"] == expect["missing_stages"]
    assert [g["code"] for g in report.completeness["gaps"]] == expect["gap_codes"]
    assert [e["code"] for e in report.errors] == expect["error_codes"]
    assert report.dropped_blocks == expect["dropped_blocks"]
    assert report.repair_calls == expect["repair_calls"]
    assert [[s["heading"], [b["kind"] for b in s["blocks"]]] for s in doc["sections"]] == [
        [e["heading"], e["kinds"]] for e in expect["sections"]
    ]
    for key, want in expect["references"].items():
        assert {k: doc["references"][key][k] for k in want} == want
    assert doc["references"]["e1"]["locator"] == expect["locator_of"]["e1"]
    # 被丢摘录的依据没有进入文档引用表，缺口仍保留可追溯的片段号
    gap = report.completeness["gaps"][0]
    assert gap["segment_ids"] == ["s0005", "s0006"]
    assert doc["completeness"]["gaps"] == report.completeness["gaps"]


def test_multi_source_references_stay_separate():
    fx = load("expectations_multi_source.json")
    doc, report = assemble(
        "expectations_multi_source.json", ref_table_name="ref_table_multi_source.json",
        output=fx["model_output"],
    )
    expect = fx["expect"]
    assert report.completeness["state"] == expect["state"]
    assert doc["summary"] == expect["summary"]
    assert report.dropped_blocks == expect["dropped_blocks"]
    for key, want in expect["references"].items():
        assert {k: doc["references"][key][k] for k in want} == want
    assert doc["references"]["e1"]["segment_ids"][0] == "s0001"
    assert doc["references"]["e2"]["segment_ids"] == ["s0001"]
    assert doc["references"]["e1"]["item_id"] != doc["references"]["e2"]["item_id"]
    assert doc["references"]["e1"]["source_text_hash"] != doc["references"]["e2"]["source_text_hash"]
    assert doc["provenance"]["source_revisions"] == expect["provenance_source_revisions"]
    assert len(doc["provenance"]["input_documents"]) == expect["input_documents"]


def test_conversation_workflow_states_become_natural_sections():
    doc = load("document_conversation.json")
    assert doc["completeness"]["state"] == "complete"
    assert [s["heading"] for s in doc["sections"]] == ["讨论起点", "已采纳", "被否定", "仍未确定"]
    assert "c00" not in json.dumps(doc, ensure_ascii=False)  # 不再产生观点编号
    rebuilt, report = c3.assemble_content_document(
        subject_from_document(doc, load("ref_table_conversation.json")),
        ref_table=table("ref_table_conversation.json"), document_id=doc["document_id"], kind="digest",
        revision=doc["revision"], item_id=doc["provenance"]["source_revisions"][0]["item_id"],
        source_revision=1, task="digest", recipe_version=c3.CONTENT_RECIPE_VERSION,
        created_at=doc["created_at"], source_title="我和 AI 关于提炼时机的对话",
    )
    assert report.errors == []
    assert rebuilt == doc


def subject_from_document(doc: dict, raw_table: dict) -> dict:
    """把已组装文档还原成模型侧主体（e 键 → R 键），用于验证组装可复现。"""
    to_r = {
        (entry["item_id"], entry["source_revision"], tuple(entry["segment_ids"])): key
        for key, entry in raw_table.items()
    }
    sections = []
    for section in doc["sections"]:
        blocks = []
        for block in section["blocks"]:
            refs = []
            for e_key in block["refs"]:
                ref = doc["references"][e_key]
                refs.append(to_r[(ref["item_id"], ref["source_revision"], tuple(ref["segment_ids"]))])
            blocks.append({"kind": block["kind"], "text": block["text"], "refs": refs})
        sections.append({"heading": section["heading"], "blocks": blocks})
    return {
        "title": doc["title"], "summary": doc["summary"],
        "sections": sections, "limitations": doc["limitations"],
    }


# ---------------------------------------------------------------- 失败按影响范围


BAD_CASES = load("expectations_bad_outputs.json")["cases"]


@pytest.mark.parametrize("case", BAD_CASES, ids=[c["file"] for c in BAD_CASES])
def test_bad_outputs_match_pinned_expectations(case):
    subject = load(case["file"]).get("raw_output") or load(case["file"])["model_output"]
    doc, report = assemble("expectations_bad_outputs.json", ref_table_name=case["ref_table"], output=subject)
    expect = case["expect"]
    assert (doc is None) == expect.get("document_is_none", False)
    assert report.completeness["state"] == expect["state"]
    assert [e["code"] for e in report.errors] == expect["error_codes"]
    assert [g["code"] for g in report.completeness["gaps"]] == expect["gap_codes"]
    assert report.completeness["missing_stages"] == expect["missing_stages"]
    assert report.dropped_blocks == expect["dropped_blocks"]
    if doc is None:
        return
    assert len(doc["references"]) == expect["reference_count"]
    assert doc["summary"] == expect["summary"]
    assert [[b["kind"] for b in s["blocks"]] for s in doc["sections"]] == expect["kinds"]


def test_quote_spanning_adjacent_reading_units_is_checked_on_joined_source():
    """相邻阅读单元可拼接比对；跨来源或跳过中间单元的拼接一律拒绝。"""
    ref = table("ref_table_long_subtitle.json")
    spanning = "接收失败要留下可查的记录。云端提炼按来源版本走，不按最新正文走。"
    ok = {
        "title": "跨单元摘录", "summary": "摘录可以覆盖相邻两段字幕。",
        "sections": [{"heading": "摘录", "blocks": [
            {"kind": "quote", "text": spanning, "refs": ["R1", "R2"]}]}],
        "limitations": [],
    }
    doc, report = c3.assemble_content_document(
        ok, ref_table=ref, document_id=f"dig-{ITEM_B}", kind="digest", revision=1, item_id=ITEM_B,
        source_revision=3, task="digest", recipe_version=c3.CONTENT_RECIPE_VERSION,
    )
    assert report.completeness["state"] == "complete"
    assert doc["sections"][0]["blocks"][0]["refs"] == ["e1", "e2"]

    # 跳过 R2 直接拼接 R1 与 R3：不是真实原文顺序，不能当逐字摘录发布
    skippy = copy.deepcopy(ok)
    skippy["sections"][0]["blocks"][0]["refs"] = ["R1", "R3"]
    skippy["sections"][0]["blocks"][0]["text"] = (
        "先把内容收进来，再谈加工质量。缺口写进元数据，界面只显示人话。"
    )
    doc2, report2 = c3.assemble_content_document(
        skippy, ref_table=ref, document_id=f"dig-{ITEM_B}", kind="digest", revision=1, item_id=ITEM_B,
        source_revision=3, task="digest", recipe_version=c3.CONTENT_RECIPE_VERSION,
    )
    assert doc2 is None
    assert [e["code"] for e in report2.errors] == ["quote_not_verbatim", "empty_document"]


def test_missing_subject_and_non_object_responses_fail():
    for subject in (None, "", "[1, 2]", '{"sections": "不是数组"}'):
        doc, report = short_article_assembly(subject)
        assert doc is None
        assert report.completeness["state"] == "failed"
        # 主体本身不合法就是失败，不降级成“有内容的部分结果”
        assert report.errors[0]["code"] == "missing_subject"


def test_invalid_refs_are_never_guessed_or_demoted():
    """R99/r1 不做模糊匹配；引用全无效的 claim 不降级成 suggestion。"""
    doc, report = assemble(
        "expectations_bad_outputs.json", ref_table_name="ref_table_short_article.json",
        output=load("bad_output_bad_ref.json")["model_output"],
    )
    error = [e for e in report.errors if e["code"] == "bad_ref"][0]
    assert error["refs"] == ["R99", "r1"]
    texts = [b["text"] for s in doc["sections"] for b in s["blocks"]]
    assert "这条结论引用了本次任务不存在的引用号。" not in texts
    assert [b["kind"] for s in doc["sections"] for b in s["blocks"]] == ["claim"]


def test_mixed_valid_and_invalid_refs_hold_back_the_whole_block():
    doc, report = assemble(
        "expectations_bad_outputs.json", ref_table_name="ref_table_short_article.json",
        output=load("bad_output_mixed_refs.json")["model_output"],
    )
    assert report.dropped_blocks == 1
    assert [[b["kind"] for b in s["blocks"]] for s in doc["sections"]] == [["suggestion"], ["claim"]]
    # 有效的那一份依据没有单独留下：整条结论暂不发布
    assert [e["refs"] for e in report.errors if e["code"] == "bad_ref"] == [["R77"]]


def test_dropped_quote_clears_summary_only_when_its_evidence_disappears():
    subject = copy.deepcopy(load("model_output_short_article.json"))
    subject["sections"][0]["blocks"][1]["text"] = "这句不是原文里的话。"
    doc, report = short_article_assembly(subject)
    # 摘录与同节 claim 共用 R1，依据仍在文档里 → 摘要保留
    assert doc["summary"] == load("document_short_article.json")["summary"]
    assert report.completeness["missing_stages"] == ["quote_verification"]
    assert report.completeness["state"] == "partial"
    assert report.dropped_blocks == 1
    # 面向诊断的错误保留模型给的 R 原词；进文档的缺口用文档内 e 键，读者能跳回原文
    assert report.errors[0]["refs"] == ["R1"]
    assert report.completeness["gaps"][0]["refs"] == ["e1"]


def test_ref_table_mismatch_is_a_program_failure_not_model_repair():
    broken = c3.RefTable.from_json({
        "R1": {"item_id": ITEM_A, "source_revision": 1, "segment_ids": ["s0001"], "text": "同一句。"},
        "R2": {"item_id": ITEM_A, "source_revision": 1, "segment_ids": ["s0001"], "text": "同一句。"},
    })
    subject = {"title": "T", "summary": "S", "sections": [
        {"heading": "H", "blocks": [{"kind": "claim", "text": "结论。", "refs": ["R1"]}]}]}
    doc, report = c3.assemble_content_document(
        subject, ref_table=broken, document_id=f"dig-{ITEM_A}", kind="digest", revision=1,
        item_id=ITEM_A, source_revision=1, task="digest", recipe_version=c3.CONTENT_RECIPE_VERSION,
    )
    assert doc is None
    assert [e["code"] for e in report.errors] == ["ref_table_mismatch"]
    assert report.completeness["state"] == "failed"
    # 程序侧不一致不让模型修：错误里也不应出现要求模型改引用号的暗示
    assert "引用表" in report.errors[0]["message"]


def test_knowledge_fusion_no_op_is_not_a_failure():
    doc, report = c3.assemble_content_document(
        {"no_op": True, "change_summary": "没有新增依据，保留现有主题正文。", "conflicts": []},
        ref_table=table("ref_table_short_article.json"), document_id="kn-主题", kind="knowledge",
        revision=3, item_id="", source_revision=0, task="knowledge_fusion",
        recipe_version=c3.CONTENT_RECIPE_VERSION,
    )
    assert doc is None
    assert report.no_op is True
    assert report.completeness["state"] == "complete"
    assert report.task_extras["change_summary"] == "没有新增依据，保留现有主题正文。"


def test_share_synthesis_task_extras_are_reported_not_written_into_document():
    doc, report = c3.assemble_content_document(
        {
            "title": "分享整合稿", "summary": "两篇材料的对照。",
            "sections": [{"heading": "对照", "blocks": [
                {"kind": "claim", "text": "两份材料都把接收放在提炼之前。", "refs": ["R1"]}]}],
            "limitations": [],
            "reader_goal": "让读者看清接收与提炼的顺序",
            "visualization_intent": "两列时间线",
            "material_usage": [{"ref": " R1 ", "note": "作为第一步的依据"}],
        },
        ref_table=table("ref_table_short_article.json"), document_id="syn-run-1", kind="synthesis",
        revision=1, item_id=ITEM_A, source_revision=1, task="share_synthesis",
        recipe_version=c3.CONTENT_RECIPE_VERSION,
    )
    assert doc["format_version"] == c3.CONTENT_FORMAT_VERSION
    assert "reader_goal" not in doc and "material_usage" not in doc
    assert report.task_extras["reader_goal"] == "让读者看清接收与提炼的顺序"
    assert report.task_extras["material_usage"] == [{"ref": " R1 ", "note": "作为第一步的依据"}]


def test_document_level_limits_hold_back_blocks_not_the_whole_document():
    """超出块数/引用数上限的块被挡下并记为缺口，已核实的部分继续可用。"""
    segments = [
        {"segment_id": f"s{i:04d}", "text": f"第{i}句原文。", "paragraph_id": f"p{i:04d}"}
        for i in range(1, 63)
    ]
    built = c3.build_ref_table([c3.Material(item_id=ITEM_A, source_revision=1, segments=segments)])
    assert len(built.keys()) == 62
    subject = {
        "title": "上限", "summary": "引用与块数超上限时只挡超出的部分。",
        "sections": [{"heading": "逐条", "blocks": [
            {"kind": "claim", "text": f"结论{i}。", "refs": [f"R{i}"]} for i in range(1, 63)
        ] + [{"kind": "claim", "text": "多余的块。", "refs": ["R1"]}] * 150}],
        "limitations": [],
    }
    doc, report = c3.assemble_content_document(
        subject, ref_table=built, document_id=f"dig-{ITEM_A}", kind="digest", revision=1,
        item_id=ITEM_A, source_revision=1, task="digest", recipe_version=c3.CONTENT_RECIPE_VERSION,
    )
    assert "limit_exceeded" in [e["code"] for e in report.errors]
    assert report.completeness["state"] == "partial"
    # 第 61、62 条引用超出 60 条上限，末尾 12 块超出 200 块上限：都只作废自己
    assert len(doc["references"]) == c3.MAX_REFERENCES
    assert sum(len(s["blocks"]) for s in doc["sections"]) == 198
    assert report.dropped_blocks == 14
    assert c3.validate_content_document(doc) == []


def test_assembly_rejects_blocks_with_forbidden_links_without_losing_siblings():
    subject = {
        "title": "链接检查", "summary": "块正文不允许内部链接。",
        "sections": [{"heading": "判断", "blocks": [
            {"kind": "claim", "text": "看这条：[[02 Digests/某摘要]]", "refs": ["R1"]},
            {"kind": "claim", "text": "这条正常。", "refs": ["R1"]},
        ]}],
        "limitations": [],
    }
    doc, report = short_article_assembly(subject)
    assert report.dropped_blocks == 1
    assert [b["text"] for s in doc["sections"] for b in s["blocks"]] == ["这条正常。"]
    assert report.errors[0]["code"] == "missing_subject"


# ---------------------------------------------------------------- 旧协议样本


def test_legacy_analysis_fixtures_match_the_current_v2_validator():
    """旧输入必须能被 analysis.py 现学校验通过，否则 W4 迁移器拿到的样本是假的。"""
    raw = c3.RefTable.from_json(load("ref_table_short_article.json"))
    segment_texts: dict[str, str] = {}
    for key in raw.keys():
        entry = raw.get(key)
        segment_texts.update(zip(entry.segment_ids, entry.text.split("\n")))
    segment_order = [f"s{i:04d}" for i in range(1, len(segment_texts) + 1)]

    v2 = load("legacy_analysis_v2.json")
    assert analysis.validate_analysis(
        v2, source_revision=1, segment_ids=set(segment_texts), segment_texts=segment_texts,
        segment_order=segment_order,
    ) == []
    assert set(analysis.evidence_map(v2)) == {"c0001", "c0002"}

    v1 = load("legacy_analysis_v1.json")
    assert analysis.validate_analysis(v1, source_revision=1, segment_ids=set(segment_texts)) == []
    assert v1["schema_version"] == "1.0" and v1["topics"] and v1.get("key_points")


def test_legacy_knowledge_note_keeps_anchors_human_regions_and_unknown_keys():
    note = (FIX / "legacy_knowledge_note.md").read_text(encoding="utf-8")
    mapping = load("legacy_evidence_map.json")
    assert "^c0001" in note and "^c0002" in note  # 外部可能引用的旧块锚点
    assert "<!-- kb:knowledge:start -->" in note and "<!-- kb:knowledge:end -->" in note
    assert "## 我自己补充的部分" in note  # 人工区：自动融合不改写
    assert "kb_custom_local_field" in note  # 未知 frontmatter：迁移原样保留
    assert set(mapping["claims"]) == {"c0001", "c0002"}
    assert mapping["claims"]["c0001"]["evidence_ids"] == ["s0001", "s0002"]
    assert mapping["extra_anchors"]["c0003"]["evidence_ids"] == ["s0001", "s0002"]
    assert mapping["normalized_note"].endswith(f"--{ITEM_A}.md")  # 旧文件名带长 item_id


# ---------------------------------------------------------------- 校验与镜像


def document_cases() -> list[dict]:
    docs = [load("document_short_article.json"), load("document_conversation.json")]
    for name, ref in (
        ("expectations_long_subtitle.json", "ref_table_long_subtitle.json"),
        ("expectations_multi_source.json", "ref_table_multi_source.json"),
    ):
        doc, _ = (long_subtitle_assembly() if name.startswith("expectations_long")
                  else assemble(name, ref_table_name=ref))
        docs.append(doc)
    for case in BAD_CASES:
        subject = load(case["file"]).get("raw_output") or load(case["file"])["model_output"]
        doc, _ = assemble("expectations_bad_outputs.json", ref_table_name=case["ref_table"], output=subject)
        if doc is not None:
            docs.append(doc)
    return docs


def schema_errors(doc: dict) -> list[str]:
    validator = Draft202012Validator(SCHEMA)
    return sorted(f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(doc))


def test_every_fixture_document_validates_clean():
    for doc in document_cases():
        assert doc["format_version"] == c3.CONTENT_FORMAT_VERSION
        assert c3.validate_content_document(doc) == []


def test_schema_and_python_validator_agree_on_the_same_documents():
    for doc in document_cases():
        assert schema_errors(doc) == []
        assert c3.validate_content_document(doc) == []


def test_schema_rejects_the_same_problems_as_the_python_validator():
    broken = copy.deepcopy(load("document_short_article.json"))
    broken["format_version"] = "2.0"
    broken["references"]["e1"]["source_revision"] = "1"
    broken["sections"][0]["blocks"][0]["refs"] = ["R1"]
    broken["completeness"]["state"] = "mostly_done"
    broken["completeness"]["gaps"] = [{"code": "not_a_code", "message": "缺少字段"}]
    assert schema_errors(broken)
    errors = c3.validate_content_document(broken)
    assert any("format_version" in e for e in errors)
    assert any("source_revision" in e for e in errors)
    assert any("文档内 e 引用" in e for e in errors)
    assert any("state" in e for e in errors)
    assert any("code 非法" in e for e in errors)

    oversized = copy.deepcopy(load("document_short_article.json"))
    oversized["summary"] = "长" * (c3.MAX_SUMMARY + 1)
    assert any("summary" in e for e in c3.validate_content_document(oversized))
    assert schema_errors(oversized)

    unknown_ref = copy.deepcopy(load("document_short_article.json"))
    unknown_ref["sections"][0]["blocks"][0]["refs"] = ["e9"]
    assert any("e9" in e for e in c3.validate_content_document(unknown_ref))
    # 跨对象的存在性检查（refs 指向的 e 键必须真的在 references 里）只有 Python 校验器
    # 能表达；Schema 只约束键名格式，因此这条是 Schema 有意比 Python 松的地方。
    assert schema_errors(unknown_ref) == []

    extra_field = copy.deepcopy(load("document_short_article.json"))
    extra_field["evidence_map"] = {"c0001": ["s0001"]}
    assert schema_errors(extra_field)  # 旧协议字段不允许混进 v3 文档


def test_schema_limits_and_enums_mirror_the_python_constants():
    props = SCHEMA["properties"]
    defs = SCHEMA["$defs"]
    assert props["format_version"]["const"] == c3.CONTENT_FORMAT_VERSION
    assert props["title"]["maxLength"] == c3.MAX_TITLE
    assert props["summary"]["maxLength"] == c3.MAX_SUMMARY
    assert props["sections"]["maxItems"] == c3.MAX_SECTIONS
    assert props["references"]["maxProperties"] == c3.MAX_REFERENCES
    assert props["limitations"]["maxItems"] == c3.MAX_LIMITATIONS
    assert props["limitations"]["items"]["maxLength"] == c3.MAX_LIMITATION_LEN
    assert defs["section"]["properties"]["heading"]["maxLength"] == c3.MAX_HEADING
    assert defs["block"]["properties"]["text"]["maxLength"] == c3.MAX_BLOCK_TEXT
    assert SCHEMA["x-max-total-blocks"] == c3.MAX_BLOCKS
    assert SCHEMA["x-max-serialized-utf8-bytes"] == c3.MAX_DOC_BYTES
    assert tuple(props["kind"]["enum"]) == c3.DOC_KINDS
    assert tuple(defs["block"]["properties"]["kind"]["enum"]) == c3.BLOCK_KINDS
    assert tuple(defs["completeness"]["properties"]["state"]["enum"]) == c3.COMPLETENESS_STATES
    assert tuple(defs["provenance"]["properties"]["task"]["enum"]) == c3.TASKS
    assert tuple(defs["locator"]["properties"]["kind"]["enum"]) == c3.LOCATOR_KINDS
    assert tuple(defs["gap"]["properties"]["code"]["enum"]) == c3.GAP_CODES
    assert props["references"]["propertyNames"]["pattern"] == "^e[0-9]{1,4}$"
    # Schema 用 ECMA 字符类，Python 用 \d：按同样本比对行为，不比对写法
    assert defs["segmentId"]["pattern"].replace("[0-9]", r"\d") == c3.SEGMENT_ID_RE.pattern
    assert defs["sha256"]["pattern"] == c3.SHA256_RE.pattern
    for sample, allowed in (("s0001", True), ("s00012", True), ("s001", False), ("e1", False)):
        assert bool(re.search(defs["segmentId"]["pattern"], sample)) is allowed
    for sample, allowed in (("0" * 64, True), ("0" * 63, False), ("F" * 64, False)):
        assert bool(re.search(defs["sha256"]["pattern"], sample)) is allowed
    assert SCHEMA["required"] == [
        "format_version", "document_id", "kind", "revision", "created_at", "title", "summary",
        "sections", "references", "limitations", "completeness", "provenance",
    ]
    assert re.compile(props["created_at"]["pattern"]).match("2026-09-22T02:10:00Z")


def test_python_validator_rejects_documents_the_contract_forbids():
    doc = copy.deepcopy(load("document_short_article.json"))
    doc["sections"][0]["blocks"][0]["text"] = "这里不该出现 [[01 Sources/某笔记]]"
    assert any("Obsidian 内部链接" in e for e in c3.validate_content_document(doc))

    doc = copy.deepcopy(load("document_short_article.json"))
    doc["sections"][0]["blocks"][1]["text"] = "旧观点锚点尾巴 ^c0001"
    assert any("块锚点" in e for e in c3.validate_content_document(doc))

    doc = copy.deepcopy(load("document_short_article.json"))
    doc["completeness"]["missing_stages"] = ["chunk:07", "legacy_evidence_unresolved"]
    assert c3.validate_content_document(doc) == []
    assert schema_errors(doc) == []

    doc = copy.deepcopy(load("document_short_article.json"))
    doc["created_at"] = "2026-09-22 02:10:00"
    assert any("created_at" in e for e in c3.validate_content_document(doc))
    assert schema_errors(doc)


# ---------------------------------------------------------------- 渲染与导出


def test_render_content_markdown_is_readable_and_hides_internal_ids():
    for doc in document_cases():
        markdown = c3.render_content_markdown(doc)
        assert doc["title"] in markdown
        for internal in ("R1", "e1", "s0001", "p0001", doc["document_id"], "source_text_hash",
                        doc["provenance"]["recipe_version"]):
            assert internal not in markdown
        assert "[[" not in markdown
        for section in doc["sections"]:
            if section["heading"]:
                assert f"## {section['heading']}" in markdown


def test_render_marks_suggestions_and_quotes_and_explains_partiality():
    markdown = c3.render_content_markdown(load("document_short_article.json"))
    assert "- AI 建议/待验证：可以分别统计两个阶段的失败原因。" in markdown
    assert "> 避免混在一起。 接收环节只负责把原始材料可靠保存下来。" in markdown
    assert "### 局限" in markdown

    doc, _ = long_subtitle_assembly()
    partial = c3.render_content_markdown(doc)
    assert "本次加工只得到部分内容" in partial
    assert "第 2 段字幕加工失败" in partial
    assert "原文 00:01–00:07" in partial  # 自然时间位置，不是编号
    assert "chunk:2" not in partial and "quote_not_verbatim" not in partial


def test_ref_table_from_document_maps_e_keys_to_ranges():
    doc = load("document_short_article.json")
    exported = c3.ref_table_from_document(doc)
    assert set(exported) == {"e1", "e2"}
    assert exported["e1"]["segment_ids"] == ["s0001", "s0002"]
    assert exported["e1"]["item_id"] == ITEM_A
    assert "text" not in exported["e1"]  # 文档内引用表只给范围与哈希，不回传原文
    exported["e1"]["segment_ids"].append("改写本地副本")
    assert doc["references"]["e1"]["segment_ids"] == ["s0001", "s0002"]


def test_recipe_and_format_version_constants_match_the_contract():
    assert (c3.CONTENT_FORMAT_VERSION, c3.CONTENT_RECIPE_VERSION) == ("3.0", "content-v3-1")
    assert set(c3.ERROR_CODES) == {
        "json_unparsable", "missing_subject", "truncated", "bad_ref", "quote_not_verbatim",
        "empty_document", "ref_table_mismatch", "limit_exceeded",
    }
    doc = load("document_short_article.json")
    assert doc["provenance"]["recipe_version"] == c3.CONTENT_RECIPE_VERSION
