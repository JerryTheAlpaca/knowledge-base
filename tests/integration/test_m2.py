"""M2 云端加工集成测试。

覆盖：凭据托管 API（明文不回读）、enrich 成功发布、JSON 修复、校验失败诊断、
401 waiting_key 与凭据更新重排队、超时 unknown_outcome、
旧来源版本不覆盖新内容（A13）、重新加工幂等、
不提供价格/供应商不返回 usage 也能完成（docs/05 §5）、
长文本分块、租户隔离。
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import auth

ALLOWED_ENDPOINT = "https://api.deepseek.com/v1"


# ---- 假 LLM 供应商：替换 enrich 内构造的 Provider ----

from kbserver.providers.llm import GenerateResult  # noqa: E402


class FakeProvider:
    behavior = None  # callable(request) -> GenerateResult，由各测试注入
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    def generate(self, request):
        return type(self).behavior(request)


@pytest.fixture()
def fake_llm(monkeypatch):
    FakeProvider.behavior = None
    FakeProvider.instances = []
    monkeypatch.setattr("kbserver.workers.enrich.OpenAICompatibleProvider", FakeProvider)
    return FakeProvider


def llm_result(payload) -> GenerateResult:
    text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
    return GenerateResult(
        output_text=text,
        provider_request_id="req-fake",
        finish_reason="stop",
        raw={},
    )


def doc_from_prompt(user_prompt: str) -> dict:
    """按提示词构造一份必然通过校验的输出（兼容主流程/合并/修复调用提示词）。

    docs/08 §3.2：云端单篇提炼 Schema 2.0 —— claim_id、适用条件、逐字摘录，
    不输出主题/标签/知识关联/晋升/双链。
    """
    payload = json.loads(user_prompt)
    schema = payload.get("output_schema") or payload.get("original_task") or {}
    source_data = payload.get("source_data") or {}
    segments = json.loads(source_data["segments"]) if source_data.get("segments") else []
    if segments:
        seg_id = segments[0]["segment_id"]
        seg_text = segments[0]["text"]
    else:
        # 合并阶段：只允许引用候选要点携带的片段与摘录（docs/08 §7.1）
        cands = payload.get("candidates") or {}
        kps = cands.get("key_points") or []
        exs = cands.get("excerpts") or []
        seg_id = (kps[0]["evidence_ids"][0] if kps else
                  (exs[0]["evidence_ids"][0] if exs else "s0001"))
        seg_text = exs[0]["text"] if exs else "示例原文"
    doc = {
        "schema_version": "2.0",
        "source_revision": schema["source_revision"],
        "summary": "演示摘要。",
        "key_points": [{"claim_id": "c0001", "text": "采集与总结应分开处理。",
                        "conditions": None, "evidence_ids": [seg_id]}],
        "excerpts": [{"claim_id": "c0001", "text": seg_text, "evidence_ids": [seg_id]}],
        "methods": [],
        "insights": [{"text": "候选启发。", "kind": "ai_suggestion", "basis_ids": [seg_id]}],
        "limitations": [],
    }
    if "workflow" in schema:
        doc["workflow"] = None
    return doc


def chunk_aware_behavior(request):
    """分段提取 + 合并 + 主流程通吃的行为。"""
    prompt = request.user
    if '"task": "这是长材料的分段提取' in prompt or '"task":"这是长材料的分段提取' in prompt:
        payload = json.loads(prompt)
        segments = json.loads(payload["source_data"]["segments"])
        seg_id = segments[0]["segment_id"]
        seg_text = segments[0]["text"]
        return llm_result({
            "key_points": [{"text": "分段要点。", "conditions": None, "evidence_ids": [seg_id]}],
            "excerpts": [{"text": seg_text, "evidence_ids": [seg_id]}],
            "methods": [],
            "insights": [],
        })
    if "以下是分段提取的候选要点" in prompt:
        return llm_result(doc_from_prompt(prompt))
    return llm_result(doc_from_prompt(prompt))


# ---- 测试辅助 ----

def _drain(session_factory, max_rounds=20):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _create_profile(client, token, *, capabilities=None, secret="sk-test-1234567890"):
    body = {
        "kind": "llm",
        "adapter": "openai-compatible",
        "endpoint": ALLOWED_ENDPOINT,
        "model": "deepseek-chat",
        "capabilities": capabilities or {},
        "secret": secret,
    }
    return client.post("/v1/provider-profiles", json=body, headers=auth(token))


def _capture_text(client, token, key: str, text: str, note: str | None = None):
    body = {
        "client_capture_id": f"{key}-1111-2222-3333-444444444444",
        "input_kind": "text",
        "text": text,
    }
    if note:
        body["user_note"] = note
    return client.post("/v1/captures", json=body, headers={**auth(token), "Idempotency-Key": key})


def _get_item(client, token, item_id):
    return client.get(f"/v1/items/{item_id}", headers=auth(token)).json()


# ---- 凭据托管 API ----

def test_profile_secret_never_returned_and_validation(client, user_a):
    token = user_a["desktop"]["token"]

    # HTTP endpoint / 未批准主机 / 未知能力字段都拒绝
    bad = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "endpoint": "http://api.deepseek.com/v1",
        "model": "m", "secret": "sk-test-1234567890",
    }, headers=auth(token))
    assert bad.status_code == 422
    bad2 = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "endpoint": "https://evil.example.com/v1",
        "model": "m", "secret": "sk-test-1234567890",
    }, headers=auth(token))
    assert bad2.status_code == 422
    bad3 = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "endpoint": ALLOWED_ENDPOINT,
        "model": "m", "capabilities": {"tools": True}, "secret": "sk-test-1234567890",
    }, headers=auth(token))
    assert bad3.status_code == 422

    r = _create_profile(client, token)
    assert r.status_code == 201
    out = r.json()
    assert out["configured"] is True
    assert out["credential_version"] == 1
    serialized = json.dumps(out)
    assert "sk-test" not in serialized, "任何接口不得回读明文 Key"

    # 版本冲突
    patch = client.patch(f"/v1/provider-profiles/{out['id']}", json={
        "model": "deepseek-reasoner", "expected_version": 99,
    }, headers=auth(token))
    assert patch.status_code == 409

    # 正常更新：版本递增、凭据轮换到 v2
    patch2 = client.patch(f"/v1/provider-profiles/{out['id']}", json={
        "model": "deepseek-reasoner", "expected_version": out["version"],
        "secret": "sk-rotated-9876543210",
    }, headers=auth(token))
    assert patch2.status_code == 200
    out2 = patch2.json()
    assert out2["version"] == out["version"] + 1
    assert out2["credential_version"] == 2
    assert "sk-rotated" not in json.dumps(out2)

    # 撤销凭据：configured 变 false，不返回历史
    rev = client.delete(f"/v1/provider-profiles/{out['id']}/credential", headers=auth(token))
    assert rev.status_code == 200
    lst = client.get("/v1/provider-profiles", headers=auth(token)).json()
    assert lst[0]["configured"] is False

    # 租户隔离在 test_profile_tenant_isolation 中覆盖


def test_profile_tenant_isolation(client, user_a, user_b):
    token = user_a["desktop"]["token"]
    out = _create_profile(client, token).json()
    b = auth(user_b["desktop"]["token"])
    # 未知对象与他人对象统一 404（docs/02 §10.2）
    assert client.patch(f"/v1/provider-profiles/{out['id']}", json={"model": "x"}, headers=b).status_code == 404
    assert client.delete(f"/v1/provider-profiles/{out['id']}/credential", headers=b).status_code == 404
    assert client.post(f"/v1/provider-profiles/{out['id']}/test", headers=b).status_code == 404


# ---- enrich 成功路径 ----

def test_enrich_success_publishes_ready_bundle(client, user_a, session_factory, fake_llm):
    fake_llm.behavior = chunk_aware_behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)

    c = _capture_text(client, user_a["phone"]["token"], "m2cap1", "第一段：可靠保存材料。\n第二段：加工不丢原文。", note="我的备注")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it
    rev = it["bundle_revision"]

    m = client.get(f"/v1/items/{item_id}/bundles/{rev}/manifest", headers=auth(token)).json()
    assert m["processing"]["state"] == "ready"
    paths = {f["relative_path"] for f in m["files"]}
    assert "analysis.json" in paths and "preview.md" in paths
    analysis_file = next(f for f in m["files"] if f["relative_path"] == "analysis.json")
    assert m["processing"]["result_file_id"] == analysis_file["file_id"]

    analysis_doc = json.loads(
        client.get(f"/v1/items/{item_id}/bundles/{rev}/files/{analysis_file['file_id']}", headers=auth(token)).content
    )
    assert analysis_doc["summary"] == "演示摘要。"
    assert analysis_doc["key_points"][0]["evidence_ids"]

    # 去计费后无 /v1/usage 接口
    assert client.get("/v1/usage", headers=auth(token)).status_code == 404


def test_enrich_succeeds_without_usage_in_response(client, user_a, session_factory, fake_llm):
    """供应商响应不返回 usage、配置无价格：加工照常完成（docs/05 §5）。"""
    fake_llm.behavior = chunk_aware_behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2usage", "无价格配置的加工。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest", headers=auth(token)).json()
    assert m["processing"]["state"] == "ready"


# ---- 校验与修复 ----

def test_enrich_repairs_invalid_json(client, user_a, session_factory, fake_llm):
    calls = {"n": 0}

    def behavior(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return llm_result("这不是 JSON")  # 首次输出坏 → 触发修复
        return llm_result(doc_from_prompt(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2repair", "修复调用测试。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    assert _get_item(client, token, item_id)["pipeline_state"] == "ready"
    assert calls["n"] == 2


def test_enrich_validation_failure_keeps_diagnostic(client, user_a, session_factory, fake_llm):
    def behavior(request):
        doc = doc_from_prompt(request.user)
        doc["key_points"][0]["evidence_ids"] = ["s9999"]  # 永远引用不存在的片段
        return llm_result(doc)

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2badev", "校验失败诊断测试。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "failed"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest", headers=auth(token)).json()
    assert m["processing"]["state"] == "failed"
    assert "analysis.error.json" in {f["relative_path"] for f in m["files"]}
    # 不覆盖成品：没有 analysis.json 的 ready 声明
    assert m["processing"]["result_file_id"] is None


# ---- 凭据失效与恢复 ----

def test_auth_failed_then_credential_update_requeues(client, user_a, session_factory, fake_llm):
    from kbserver.providers.llm import ProviderAuthFailed

    fake_llm.behavior = lambda request: (_ for _ in ()).throw(ProviderAuthFailed("HTTP 401"))
    token = user_a["desktop"]["token"]
    c = _capture_text(client, user_a["phone"]["token"], "m2auth", "凭据失效测试。")
    item_id = c.json()["item_id"]

    # 先跑到 waiting_key（无凭据）
    _drain(session_factory)
    assert _get_item(client, token, item_id)["pipeline_state"] == "waiting_key"

    # 配置了坏 Key 的凭据 → enrich 失败 → 仍 waiting_key
    _create_profile(client, token)
    _drain(session_factory)
    assert _get_item(client, token, item_id)["pipeline_state"] == "waiting_key"

    # 更新凭据（轮换）→ waiting_key 条目自动重新排队并成功
    fake_llm.behavior = chunk_aware_behavior
    profile = client.get("/v1/provider-profiles", headers=auth(token)).json()[0]
    patch = client.patch(f"/v1/provider-profiles/{profile['id']}", json={"secret": "sk-newkey-1234567890"}, headers=auth(token))
    assert patch.status_code == 200
    _drain(session_factory)
    assert _get_item(client, token, item_id)["pipeline_state"] == "ready"


# ---- 未知结果 ----

def test_timeout_unknown_outcome_then_reprocess(client, user_a, session_factory, fake_llm):
    from kbserver.providers.llm import ProviderOutcomeUnknown

    fake_llm.behavior = lambda request: (_ for _ in ()).throw(ProviderOutcomeUnknown("ReadTimeout"))
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2unk", "超时未知结果测试。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "unknown_outcome"

    # 不自动重发：允许显式重新加工后成功
    fake_llm.behavior = chunk_aware_behavior
    r = client.post(f"/v1/items/{item_id}/reprocess", json={"reason": "对账完成"}, headers=auth(token))
    assert r.status_code == 202
    _drain(session_factory)
    assert _get_item(client, token, item_id)["pipeline_state"] == "ready"


# ---- 版本与幂等 ----

def test_stale_enrich_cancelled_a13(client, user_a, session_factory, fake_llm):
    """r1 的 enrich 在来源进入 r2 后不得发布旧结果（docs/02 §6.1、A13）。"""
    fake_llm.behavior = chunk_aware_behavior
    token = user_a["phone"]["token"]
    _create_profile(client, user_a["desktop"]["token"])
    c = _capture_text(client, token, "m2stale", "第一版正文。")
    item_id = c.json()["item_id"]

    # 只跑 extract：r1 的 enrich 任务已生成但未执行
    from kbserver.workers import worker

    for _ in range(5):
        if not worker.run_once(session_factory):
            break

    # 补充材料 → r2
    sup = client.post(f"/v1/items/{item_id}/supplements", json={
        "expected_source_revision": 1, "text": "补充的第二版正文。",
    }, headers=auth(user_a["desktop"]["token"]))
    assert sup.status_code == 202

    _drain(session_factory)
    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "ready"
    assert it["source_revision"] == 2
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest", headers=auth(user_a["desktop"]["token"])).json()
    assert m["source_revision"] == 2
    assert m["processing"]["state"] == "ready"


def test_reprocess_ready_item_idempotent(client, user_a, session_factory, fake_llm):
    """ready 条目重新加工不得撞任务唯一约束，且新结果是新 Bundle。"""
    fake_llm.behavior = chunk_aware_behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2reproc", "重新加工测试。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    rev1 = _get_item(client, token, item_id)["bundle_revision"]

    r = client.post(f"/v1/items/{item_id}/reprocess", json={"reason": "换模型"}, headers=auth(token))
    assert r.status_code == 202
    _drain(session_factory)
    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    assert it["bundle_revision"] > rev1


def test_long_text_chunked_processing(client, user_a, session_factory, fake_llm):
    """超过上下文预算的材料按片段分块：先分段提取，再合并。"""
    calls = {"chunk": 0, "merge": 0}

    def behavior(request):
        prompt = request.user
        if "这是长材料的分段提取" in prompt:
            calls["chunk"] += 1
        if "以下是分段提取的候选要点" in prompt:
            calls["merge"] += 1
        return chunk_aware_behavior(request)

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    # context_tokens=600 → 强制分块
    _create_profile(client, token, capabilities={"context_tokens": 600})
    long_text = "\n".join(f"第{i}段：这里是一些需要加工的中文内容，用于撑起分块逻辑的测试。" for i in range(40))
    c = _capture_text(client, user_a["phone"]["token"], "m2chunk", long_text)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it
    assert calls["chunk"] >= 2 and calls["merge"] == 1


def test_conversation_mode_enables_workflow(client, user_a, session_factory, fake_llm):
    """对话类输入启用 workflow 输出字段。"""
    fake_llm.behavior = chunk_aware_behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = client.post("/v1/captures", json={
        "client_capture_id": "convo-1111-2222-3333-444444444444",
        "input_kind": "conversation",
        "text": "用户：怎么选方案？\n助手：建议 A，因为成本低。",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "m2convo"})
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest", headers=auth(token)).json()
    analysis_file = next(f for f in m["files"] if f["relative_path"] == "analysis.json")
    doc = json.loads(
        client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{analysis_file['file_id']}", headers=auth(token)).content
    )
    assert "workflow" in doc
