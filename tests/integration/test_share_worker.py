"""分享编排 Worker 的端到端流程（docs/20 §3、§5、§6、§13；模拟模型响应）。

先用模拟模型响应验证阶段推进与失败恢复，再接本人真实模型配置试用（M2 要求）。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from kbserver.config import get_settings
from kbserver.domain import provider_ops, share_prompts, share_conversations, sharing
from kbserver.models import (
    Capture,
    Credential,
    Item,
    ProviderProfile,
    ShareArtifact,
    ShareConversation,
    ShareRevision,
    ShareRun,
    ShareWork,
    SourceRevision,
    StoredFile,
    User,
    new_id,
    utcnow,
)
from kbserver.providers.llm import ConversationMessage, GenerateResult
from kbserver.repositories import shares as repo
from kbserver.security import credentials as cred_crypto
from kbserver.storage.objects import ObjectStore
from kbserver.workers import share as share_worker

CLARIFY_1 = {
    "schema_version": "1.0",
    "understanding": "你想把材料做成便于分享的对照页面。",
    "brief": {"goal": "比较两篇的主要差异", "audience": None, "content_priorities": [],
              "presentation_preferences": [], "must_keep": [], "must_avoid": [], "assumptions": []},
    "questions": [{"id": "q1", "text": "读者完全没有基础，还是已经了解一些相关概念？",
                   "reason": "影响术语解释深度。",
                   "options": [{"id": "beginner", "label": "完全没有基础"},
                               {"id": "familiar", "label": "已有一些基础"}],
                   "required_for_generation": False}],
    "next_action": "ask_user",
}
CLARIFY_2 = {
    "schema_version": "1.0",
    "understanding": "给没有基础的读者做对照讲解。",
    "brief": {"goal": "比较两篇的主要差异", "audience": "没有基础的读者",
              "content_priorities": ["差异与成立条件"], "presentation_preferences": ["手机优先"],
              "must_keep": [], "must_avoid": [], "assumptions": ["不预设图表"]},
    "questions": [],
    "next_action": "confirm_brief",
}
PAGE_SOURCE = {
    "schema_version": "1.0",
    "title": "两篇材料的对照",
    "html_body": "<main><h1>两篇材料的对照</h1><p>两篇都强调条件，粒度不同。</p>"
                 "<button type=\"button\" data-ref=\"ref1\">来源</button></main>",
    "css": "main{max-width:44rem;margin:auto}",
    "javascript": "",
    "dependencies": [],
    "asset_ids": [],
    "reference_ids": ["ref1"],
    "interactions": [],
}


def synthesis_for(pack: dict) -> dict:
    keys = sorted(sharing.pack_source_keys(pack))
    citations = [sharing.citation_id(k, "seg0001") for k in keys]
    return {
        "schema_version": "1.0",
        "title": "两篇材料的对照",
        "reader_goal": "让没有基础的读者看清差异",
        "sections": [{"id": "sec1", "heading": "要点对照", "body": "两篇都强调条件，粒度不同。"}],
        "claims": [
            {"id": "k1", "kind": "source_claim", "text": "先分型，再谈用量。",
             "citations": [citations[0]], "conditions": None},
            {"id": "k2", "kind": "synthesis", "text": "两篇的分歧在条件的粒度。",
             "citations": citations, "conditions": None},
        ],
        "source_usage": [{"source_key": k, "use": "提供主要观点"} for k in keys],
        "visual_intents": [],
        "public_references": [{"ref_id": "ref1", "source_key": keys[0], "title": "材料一",
                               "url": "https://example.org/a", "citation": citations[0],
                               "quote": "先分型，再谈用量。"}],
        "limitations": ["没有取得原始统计数据"],
    }


class FakeProvider:
    """按尾部消息判断该回哪一步的输出，并记录每次请求的消息序列。"""

    calls: list[list[dict]] = []
    next_docs: list[dict] = []

    def __init__(self, **kwargs):
        pass

    def generate_conversation(self, request):
        FakeProvider.calls.append([m.as_request_message() for m in request.messages])
        tail = request.messages[-1].content
        if "page_source JSON" in tail or "当前源文件" in tail:
            doc = PAGE_SOURCE
        elif "整合稿 JSON" in tail:
            doc = FakeProvider.next_docs.pop(0)
        elif "第 2 轮" in tail:
            doc = CLARIFY_2
        else:
            doc = CLARIFY_1
        text = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        return GenerateResult(
            output_text=text, provider_request_id="fake-1", finish_reason="stop",
            raw={}, assistant_message=ConversationMessage(role="assistant", content=text),
            usage={"usage_protocol": "deepseek", "usage_available": True,
                   "input_tokens_total": 10, "output_tokens": 5, "cache_read_tokens": 8,
                   "cache_write_tokens": None, "uncached_input_tokens": 2},
        )


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch, tmp_path):
    FakeProvider.calls = []
    FakeProvider.next_docs = []
    # 等待 runner 的重排队在测试里不该真的睡 2 秒；spool 用临时目录避免互相污染
    monkeypatch.setenv("SHARE_RUNNER_POLL_SECONDS", "0")
    monkeypatch.setenv("SHARE_SPOOL_DIR", str(tmp_path / "spool"))


@pytest.fixture()
def env(session_factory, monkeypatch):
    """一条带正文的条目 + 一份模型配置 + 一把加密凭据。"""
    settings = get_settings()
    store = ObjectStore()
    monkeypatch.setattr(share_worker, "OpenAICompatibleProvider", FakeProvider)
    with session_factory() as db:
        user = User(name="分享测试")
        db.add(user)
        db.flush()
        capture = Capture(user_id=user.id, client_capture_id="c1", request_hash="h", input_json={})
        db.add(capture)
        db.flush()
        item = Item(user_id=user.id, capture_id=capture.id, source_revision=1, bundle_revision=1)
        db.add(item)
        db.flush()
        segments = {"source_revision": 1, "segments": [
            {"segment_id": "s0001", "text": "先分型，再谈用量。"},
            {"segment_id": "s0002", "text": "剂量随证候浮动。"}]}
        sha, key, size = store.put_bytes(json.dumps(segments, ensure_ascii=False).encode("utf-8"))
        db.add(StoredFile(file_id="f-seg", user_id=user.id, item_id=item.id, role="source_material",
                         relative_path="segments.json", mime="application/json", bytes=size,
                         sha256=sha, storage_key=key))
        db.add(SourceRevision(item_id=item.id, user_id=user.id, revision=1, content_hash="x",
                              metadata_json={"title": "材料一", "coverage": "full_text",
                                             "canonical_url": "https://example.org/a",
                                             "source_label": "网页"}, artifacts_json={}))
        profile = ProviderProfile(user_id=user.id, kind="llm", adapter="openai-compatible",
                                  role="digest", endpoint="https://api.deepseek.com/v1",
                                  model="deepseek-chat",
                                  capabilities_json={"cache_mode": "auto",
                                                     "usage_protocol": "deepseek"},
                                  prices_json={}, meta_json={})
        db.add(profile)
        db.flush()
        envelope = cred_crypto.encrypt_secret("sk-fake", settings.load_master_key(),
                                             user_id=user.id, profile_id=profile.id,
                                             credential_version=1)
        db.add(Credential(user_id=user.id, profile_id=profile.id, master_key_version=1, **envelope))
        db.commit()
        return {"user_id": user.id, "item_id": item.id, "profile_id": profile.id,
                "session_factory": session_factory}


def new_run(env, **kwargs) -> tuple[str, str]:
    with env["session_factory"]() as db:
        work = repo.create_work(db, user_id=env["user_id"], title="")
        run = repo.create_run(
            db, work=work, user_id=env["user_id"],
            request_text=kwargs.get("request_text", "整理出它们的差异，适合初学者阅读"),
            base_revision_id=None, profile_id=env["profile_id"], profile_version=1,
            model_config_json={}, runtime_version="share-runtime-1.0.0",
            prompt_version=sharing.PROMPT_VERSION, recipe_hash="r",
            stage=kwargs.get("stage", "preparing"),
        )
        run.checkpoint_json = {"items": kwargs.get("items", [
            {"item_id": env["item_id"], "source_revision": 1}])}
        db.commit()
        return run.id, work.id


def claim_and_run(env, run_id: str) -> ShareRun:
    """按 id 领取（测试里不靠队列轮转，避免用例之间互相抢任务）。"""
    session_factory = env["session_factory"]
    with session_factory() as db:
        run = db.get(ShareRun, run_id)
        assert run.state in ("queued", "retry_wait"), f"任务不在可领取状态：{run.state}/{run.stage}"
        run.state = "running"
        run.lease_token = new_id()
        run.lease_until = utcnow() + timedelta(seconds=120)
        run.attempt = (run.attempt or 0) + 1
        token = run.lease_token
        db.commit()
    share_worker.execute(session_factory, run_id, token)
    with session_factory() as db:
        return db.get(ShareRun, run_id)


def pack_of(run: ShareRun) -> dict:
    return json.loads(ObjectStore().read_object(
        (run.checkpoint_json or {})["pack_storage_key"]).decode("utf-8"))


def write_runner_result(run: ShareRun, *, ok=True, diagnostics=None) -> Path:
    settings = get_settings()
    task_id = (run.checkpoint_json or {})["runner_task"]
    target = Path(settings.share_spool_dir) / ("done" if ok else "failed") / task_id
    shutil.rmtree(target, ignore_errors=True)
    (target / "out" / "screenshots").mkdir(parents=True, exist_ok=True)
    html = b'<!doctype html><main><iframe id="kb-frame" sandbox="allow-scripts"></iframe></main>'
    (target / "out" / "index.html").write_bytes(html)
    import hashlib

    (target / "result.json").write_text(json.dumps({
        "schema_version": "1.0", "task_id": task_id,
        "lease_id": share_worker.runner_lease_id(run.id, task_id),
        "ok": ok, "html_sha256": hashlib.sha256(html).hexdigest(), "html_bytes": len(html),
        "runtime_version": "share-runtime-1.0.0",
        "diagnostics": diagnostics or [], "checks": [], "screenshots": [], "build": {},
    }), encoding="utf-8")
    return target


def answer_round(env, run_id: str, work_id: str, *, stage: str = "clarifying") -> None:
    """把一轮结构化回答与自由补充规范化成一条稳定 user 消息，然后排队下一次澄清。"""
    store = ObjectStore()
    with env["session_factory"]() as db:
        run = db.get(ShareRun, run_id)
        conv = db.query(ShareConversation).filter_by(work_id=work_id, purpose="content").one()
        round_doc = store.read_object(run.pending_round_key).decode("utf-8") if run.pending_round_key else "{}"
        text = share_conversations.normalize_answer_text(
            json.loads(round_doc),
            [{"question_id": "q1", "option_ids": ["beginner"], "text": ""}],
            "主要在手机上阅读，也请保留不同解释的出处。")
        repo.append_message(db, store, conversation=conv, run_id=run.id, user_id=env["user_id"],
                            work_id=work_id, role="user", content=text.encode("utf-8"),
                            reply_to_round_id=run.pending_round_id)
        repo.reschedule(db, run, stage=stage, seconds=0)
        db.commit()


def confirm(env, run_id: str) -> None:
    with env["session_factory"]() as db:
        run = db.get(ShareRun, run_id)
        run.confirmed_brief_version = run.brief_version
        run.confirmation_kind = "confirm"
        repo.reschedule(db, run, stage="synthesizing", seconds=0)
        db.commit()


def test_full_flow_from_selection_to_ready_draft(env):
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)  # preparing → clarifying
    assert run.state == "waiting_user", run.error_detail
    assert run.stage == "clarifying"
    assert run.lease_token is None and run.lease_until is None, "等待用户时必须释放执行资源"
    with env["session_factory"]() as db:
        conv = db.query(ShareConversation).filter_by(work_id=work_id, purpose="content").one()
        assert conv.prefix_hash and conv.prefix_artifact_key
        messages = repo.list_messages(db, env["user_id"], conv.id, context_epoch=1)
        assert [m.role for m in messages] == ["assistant"]
    FakeProvider.next_docs = [synthesis_for(pack_of(run))]

    answer_round(env, run_id, work_id)
    calls_before = len(FakeProvider.calls)
    run = claim_and_run(env, run_id)
    assert run.state == "awaiting_confirmation", run.error_detail
    assert len(FakeProvider.calls) == calls_before + 1, "一次回答只触发一次澄清调用"
    brief = json.loads(ObjectStore().read_object(run.brief_key).decode("utf-8"))
    assert brief["brief"]["audience"] == "没有基础的读者"
    assert brief["provenance"]["audience"]["by"] == "ai" or brief["provenance"]["audience"]["by"] == "user"
    # 第二轮请求保留了第一轮的 assistant 原文，只在尾部追加本轮问题
    second = FakeProvider.calls[-1]
    assert [m["role"] for m in second[:6]] == [
        "system", "user", "user", "assistant", "user", "user"]
    assert json.loads(second[3]["content"])["questions"][0]["id"] == "q1"
    assert second[4]["content"].startswith("关于「读者完全没有基础")

    confirm(env, run_id)
    run = claim_and_run(env, run_id)
    assert run.stage == "awaiting_runner", run.error_detail
    assert run.state == "queued"
    task_dir = Path(get_settings().share_spool_dir) / "ready" / run.checkpoint_json["runner_task"]
    envelope = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert envelope["references"][0]["title"] == "材料一"
    assert envelope["limitations"] == ["没有取得原始统计数据"]
    assert not (task_dir / "input" / "page_source.json").read_bytes().startswith(b"{\"sources\"")

    write_runner_result(run)
    run = claim_and_run(env, run_id)
    assert run.state == "succeeded", run.error_detail
    with env["session_factory"]() as db:
        work = db.get(ShareWork, work_id)
        revision = db.get(ShareRevision, work.latest_ready_revision_id)
        assert revision.revision == 1 and revision.html_bytes > 0
        assert ObjectStore().read_object(revision.html_key).startswith(b"<!doctype html")
        assert work.active_run_id is None
        assert work.title == "两篇材料的对照"
        ops = repo.operations_for_run(db, run_id)
        assert {o.step_key for o in ops} >= {"clarify-1", "clarify-2", "synthesis", "page_source"}
        assert all(o.state == "succeeded" for o in ops)
        assert ops[0].usage_json["cache_read_tokens"] == 8
        assert db.query(ShareArtifact).filter_by(work_id=work_id, role="html").count() == 1
        refs = [a for a in db.query(ShareArtifact).filter_by(work_id=work_id,
                                                            role="public_references").all()]
        assert refs[0].visibility == "public"


def test_second_round_uses_same_prefix_bytes(env):
    """稳定前缀：两轮请求的开头三条消息完全一致（§6.5.2）。"""
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)
    first = FakeProvider.calls[0][:3]
    answer_round(env, run_id, work_id)
    claim_and_run(env, run_id)
    assert FakeProvider.calls[-1][:3] == first


def test_check_failure_retries_then_fails_without_touching_old_draft(env):
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)
    FakeProvider.next_docs = [synthesis_for(pack_of(run))]
    confirm(env, run_id)
    run = claim_and_run(env, run_id)
    write_runner_result(run, ok=False, diagnostics=[
        {"code": "JS_PARSE", "message": "JavaScript 语法错误", "severity": "error"}])
    run = claim_and_run(env, run_id)
    assert run.state == "queued" and run.stage == "awaiting_runner"
    assert run.repair_count == 1
    with env["session_factory"]() as db:
        assert db.query(ShareRevision).filter_by(work_id=work_id).count() == 0
        assert db.get(ShareWork, work_id).latest_ready_revision_id is None
    # 修复上限用完后如实失败：不标「可以分享」，旧版本仍在
    write_runner_result(run, ok=False, diagnostics=[
        {"code": "JS_PARSE", "message": "还是错", "severity": "error"}])
    with env["session_factory"]() as db:
        db.get(ShareRun, run_id).repair_count = get_settings().share_max_repairs
        db.commit()
    run = claim_and_run(env, run_id)
    assert run.state == "failed" and run.reason_code == "check_failed"
    with env["session_factory"]() as db:
        assert db.query(ShareRevision).filter_by(work_id=work_id).count() == 0
        assert db.query(ShareArtifact).filter_by(work_id=work_id, role="check_report").count() >= 2


def test_non_repairable_check_failure_does_not_call_model_again(env):
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)
    FakeProvider.next_docs = [synthesis_for(pack_of(run))]
    confirm(env, run_id)
    run = claim_and_run(env, run_id)
    write_runner_result(run, ok=False, diagnostics=[
        {"code": "BROWSER_UNAVAILABLE", "message": "检查环境无法启动", "severity": "error"}])
    calls = len(FakeProvider.calls)
    run = claim_and_run(env, run_id)
    assert run.state == "failed" and run.reason_code == "check_failed"
    assert len(FakeProvider.calls) == calls, "环境问题不该让 AI 改代码重试"


def test_sent_operation_is_not_resent(env, monkeypatch):
    """A20：请求已发出但没有响应 → unknown_outcome，不自动再发。"""
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)          # 第一轮澄清完成，等待用户
    answer_round(env, run_id, work_id)  # 用户回答后排队第二轮
    calls_before = len(FakeProvider.calls)

    def boom(self, request):
        raise AssertionError("已发出的请求不得自动重发")

    monkeypatch.setattr(FakeProvider, "generate_conversation", boom)
    with env["session_factory"]() as db:
        run = db.get(ShareRun, run_id)
        op = repo.create_operation(db, user_id=env["user_id"], run_id=run.id, step_key="clarify-2",
                                   profile_id=run.profile_id, request_fingerprint="x" * 32)
        provider_ops.mark_sent(op)
        db.commit()
    run = claim_and_run(env, run_id)
    assert run.state == "unknown_outcome", run.error_detail
    assert run.reason_code == "outcome_unknown"
    assert len(FakeProvider.calls) == calls_before


def test_unreadable_material_reports_specific_item(env):
    with env["session_factory"]() as db:
        f = db.query(StoredFile).filter_by(item_id=env["item_id"],
                                           relative_path="segments.json").one()
        db.delete(f)
        db.commit()
        run_id, _ = new_run(env)
    run = claim_and_run(env, run_id)
    assert run.state == "failed" and run.reason_code == "material_unreadable"
    missing = (run.checkpoint_json or {})["missing_items"]
    assert missing and missing[0]["item_id"] == env["item_id"]


def test_source_version_update_does_not_change_this_run(env):
    """A08：本轮固定旧快照；来源后来更新不混用新原文，也不自动改已有作品。"""
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)
    with env["session_factory"]() as db:
        fixed_pack = pack_of(run)
        db.get(Item, env["item_id"]).source_revision = 2  # 用户在别处补充了材料
        db.commit()
    FakeProvider.next_docs = [synthesis_for(fixed_pack)]
    confirm(env, run_id)
    run = claim_and_run(env, run_id)
    assert run.stage == "awaiting_runner", run.error_detail
    # 交给 runner 的仍是本轮固定的版本（r1），没有偷偷读「最新版」
    task_dir = Path(get_settings().share_spool_dir) / "ready" / run.checkpoint_json["runner_task"]
    envelope = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert envelope["references"][0]["revision"] == 1
    with env["session_factory"]() as db:
        stored = json.loads(ObjectStore().read_object(
            (run.checkpoint_json or {})["manifest_key"]).decode("utf-8"))
        assert stored["sources"][0]["source_revision"] == 1


def test_expired_lease_returns_to_queued_without_losing_stage(env):
    run_id, _ = new_run(env)
    claim_and_run(env, run_id)
    with env["session_factory"]() as db:
        run = db.get(ShareRun, run_id)
        run.state = "running"
        run.lease_token = "stale"
        run.lease_until = utcnow() - timedelta(seconds=5)
        db.commit()
    assert repo.recover_expired_leases(env["session_factory"]) == 1
    with env["session_factory"]() as db:
        run = db.get(ShareRun, run_id)
        assert run.state == "queued" and run.lease_token is None
        assert run.stage == "clarifying"


def _add_image_asset(env) -> str:
    """给材料挂一张正文图片：素材目录才会真的带着 storage_key 进入装配。"""
    store = ObjectStore()
    sha, key, size = store.put_bytes(b"\x89PNG\r\n\x1a\n fake asset bytes")
    with env["session_factory"]() as db:
        db.add(StoredFile(file_id="f-img", user_id=env["user_id"], item_id=env["item_id"],
                          role="source_material", relative_path="images/插图一.png",
                          mime="image/png", bytes=size, sha256=sha, storage_key=key))
        db.commit()
    return key


def test_model_requests_never_carry_private_object_keys(env):
    """C-04：每一段发给模型的文本都不带私有对象 key（也不带它的样子）。

    盯的是真实请求文本而不是构造处：稳定前缀早就剥掉 storage_key，尾部目录曾经
    没剥——同一个函数里两套做法就是这条用例要钉住的自相矛盾。
    """
    asset_key = _add_image_asset(env)
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)
    FakeProvider.next_docs = [synthesis_for(pack_of(run))]
    confirm(env, run_id)
    run = claim_and_run(env, run_id)          # synthesizing → generating → packaging
    assert run.stage == "awaiting_runner", run.error_detail
    assert FakeProvider.calls, "应抓到发给模型的请求"

    sent = "\n".join(m["content"] for call in FakeProvider.calls for m in call)
    assert "storage_key" not in sent
    assert asset_key not in sent
    assert not re.search(r"[0-9a-f]{2}/[0-9a-f]{64}", sent), "内部对象 key 的形态外发了"
    # 只剥 key，不剥模型要用到的逻辑标识
    assert '"asset_id"' in sent and '"a1"' in sent
    # 断言本身有牙：同一份目录不剥 key 时，确实会被这段检查抓到
    leaky = share_prompts.code_tail(synthesis={}, asset_catalog=[{"asset_id": "a1",
                                                                  "storage_key": asset_key}],
                                    reference_catalog=[], runbook="", instructions="")
    assert asset_key in leaky and "storage_key" in leaky

    # 剥的位置不能过头：编排器自己的清单与交接仍要按 key 读对象
    with env["session_factory"]() as db:
        stored = json.loads(ObjectStore().read_object(
            db.get(ShareRun, run_id).input_manifest_key).decode("utf-8"))
        assert {a["storage_key"] for s in stored["sources"] for a in s["assets"]} == {asset_key}
        catalog = (db.get(ShareRun, run_id).checkpoint_json or {})["asset_catalog"]
        assert [a["storage_key"] for a in catalog] == [asset_key]
    task_dir = Path(get_settings().share_spool_dir) / "ready" / run.checkpoint_json["runner_task"]
    assert (task_dir / "assets" / "a1.png").read_bytes().startswith(b"\x89PNG")
    envelope = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert all("storage_key" not in a for a in envelope["assets"])


def test_runner_envelope_carries_body_floor_and_wall_clock(env):
    """交接信封给 runner 的两项自查边界：正文下限按材料规模、墙钟按渲染超时倍数。"""
    run_id, work_id = new_run(env)
    run = claim_and_run(env, run_id)
    FakeProvider.next_docs = [synthesis_for(pack_of(run))]
    confirm(env, run_id)
    run = claim_and_run(env, run_id)
    settings = get_settings()
    task_dir = Path(settings.share_spool_dir) / "ready" / run.checkpoint_json["runner_task"]
    check = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))["check"]
    # 材料 21 字符 → 落在 200 下限（min(800, max(200, chars // 25))）
    with env["session_factory"]() as db:
        source_chars = (db.get(ShareRun, run_id).checkpoint_json or {})["source_chars"]
    assert source_chars == sum(len(t) for t in ("先分型，再谈用量。", "剂量随证候浮动。"))
    assert check["min_body_chars"] == min(800, max(200, source_chars // 25)) == 200
    assert check["task_timeout_ms"] == settings.share_render_timeout_seconds * 2 * 1000
    assert 60_000 <= check["task_timeout_ms"] <= 180_000
    # 与「服务端等 runner 的耐心」不是一回事：后者大得多
    assert settings.share_runner_max_wait_seconds * 1000 > check["task_timeout_ms"]
