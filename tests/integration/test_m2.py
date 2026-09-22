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
    """按提示词构造一份能组装成 v3 文档的输出（兼容提炼/分块/合并/修复调用）。

    docs/24 §3：模型只写内容与选本次给定的 R 编号，程序字段一概不回填。
    """
    payload = json.loads(user_prompt)
    candidates = payload.get("candidates")
    if candidates:  # 合并阶段：只沿用候选里已校验的块与引用
        blocks = [b for c in candidates for s in c["sections"] for b in s["blocks"]]
        return {"title": "演示标题", "summary": "跨段汇总。",
                "sections": [{"heading": "主要判断", "blocks": blocks}], "limitations": []}
    material = payload.get("material") or []
    if not material:
        return {}  # 纠错与分段等其它调用：没有内容主体，上层按「无结果」回退
    first = material[0]
    return {
        "title": "演示标题",
        "summary": "先保存，再提炼。",
        "sections": [{"heading": "主要判断", "blocks": [
            {"kind": "claim", "text": "采集与总结应分开处理。", "refs": [first["ref"]]},
            {"kind": "quote", "text": first["text"], "refs": [first["ref"]]},
            {"kind": "suggestion", "text": "候选启发。", "refs": []},
        ]}],
        "limitations": [],
    }


def chunk_aware_behavior(request):
    """分段提取 + 合并 + 主流程通吃的行为（都按各自提示词里的 material/candidates 作答）。"""
    return llm_result(doc_from_prompt(request.user))


# ---- 测试辅助 ----

def _drain(session_factory, max_rounds=20):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _create_profile(client, token, *, capabilities=None, secret="sk-test-1234567890",
                    role=None, model="deepseek-chat"):
    body = {
        "kind": "llm",
        "adapter": "openai-compatible",
        "endpoint": ALLOWED_ENDPOINT,
        "model": model,
        "capabilities": capabilities or {},
        "secret": secret,
    }
    if role is not None:
        body["role"] = role
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
    assert client.delete(f"/v1/provider-profiles/{out['id']}", headers=b).status_code == 404
    assert client.post(f"/v1/provider-profiles/{out['id']}/test", headers=b).status_code == 404


# ---- 模型角色（整理文本 digest / 优化文本 optimize）----

def test_profile_role_create_patch_and_validation(client, user_a):
    token = user_a["desktop"]["token"]

    # 缺省归为整理（digest）；显式 optimize 原样保存
    legacy = _create_profile(client, token).json()
    assert legacy["role"] == "digest"
    opt = _create_profile(client, token, role="optimize", model="deepseek-flash").json()
    assert opt["role"] == "optimize"

    # PATCH 可以改角色；非法角色与非 llm 配置的角色都拒绝
    patched = client.patch(f"/v1/provider-profiles/{opt['id']}",
                           json={"role": "digest"}, headers=auth(token))
    assert patched.status_code == 200 and patched.json()["role"] == "digest"
    bad = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "role": "workflow",
        "endpoint": ALLOWED_ENDPOINT, "model": "m", "secret": "sk-test-1234567890",
    }, headers=auth(token))
    assert bad.status_code == 422
    bad2 = client.post("/v1/provider-profiles", json={
        "kind": "vision_ocr", "adapter": "openai-compatible", "role": "optimize",
        "endpoint": ALLOWED_ENDPOINT, "model": "m", "secret": "sk-test-1234567890",
    }, headers=auth(token))
    assert bad2.status_code == 422
    bad3 = client.patch(f"/v1/provider-profiles/{legacy['id']}",
                        json={"role": "workflow"}, headers=auth(token))
    assert bad3.status_code == 422


def test_profile_copy_from_reuses_credential(client, user_a, user_b, session_factory):
    """优化文本默认复用整理配置：copy_from 创建新配置并复制密钥（凭据仍只进不出）。"""
    from kbserver.config import get_settings
    from kbserver.models import Credential, ProviderProfile
    from kbserver.security import credentials as cred_crypto

    token = user_a["desktop"]["token"]
    digest = _create_profile(client, token, capabilities={"thinking_mode": True}).json()

    # 复用创建：不带 secret，密钥从来源配置复制；configured=True 且不回读明文
    r = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "role": "optimize",
        "endpoint": ALLOWED_ENDPOINT, "model": "deepseek-chat",
        "capabilities": {"thinking_mode": False},
        "copy_from": digest["id"],
    }, headers=auth(token))
    assert r.status_code == 201
    opt = r.json()
    assert opt["role"] == "optimize"
    assert opt["configured"] is True and opt["credential_version"] == 1
    assert "sk-test" not in json.dumps(opt)

    # 复制的密钥按新配置重新加密，能以新 profile 绑定解密回原文
    with session_factory() as db:
        row = db.query(ProviderProfile).filter(ProviderProfile.id == opt["id"]).one()
        cred = db.query(Credential).filter(
            Credential.profile_id == opt["id"], Credential.revoked_at.is_(None)).one()
        plain = cred_crypto.decrypt_secret(
            cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
            get_settings().load_master_key(),
            user_id=row.user_id, profile_id=row.id, credential_version=cred.version,
        )
        assert plain == "sk-test-1234567890"

    # 来源没有可用密钥时拒绝复制
    digest2 = _create_profile(client, token, model="deepseek-v2").json()
    client.delete(f"/v1/provider-profiles/{digest2['id']}/credential", headers=auth(token))
    r2 = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "role": "optimize",
        "endpoint": ALLOWED_ENDPOINT, "model": "m", "copy_from": digest2["id"],
    }, headers=auth(token))
    assert r2.status_code == 422

    # 不能复制他人配置（统一 404）；普通创建仍要求密钥
    r3 = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "role": "optimize",
        "endpoint": ALLOWED_ENDPOINT, "model": "m", "copy_from": digest["id"],
    }, headers=auth(user_b["desktop"]["token"]))
    assert r3.status_code == 404
    r4 = client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "role": "digest",
        "endpoint": ALLOWED_ENDPOINT, "model": "m",
    }, headers=auth(token))
    assert r4.status_code == 422


def test_provider_profile_insert_on_production_like_schema():
    """生产库仍保留去计费时代的 prices_json JSON NOT NULL（无默认）列（docs/05 §5.3）。

    模型必须带 Python 默认值随 INSERT 写入：否则任何新建配置（含优化配置复用
    整理配置的 copy_from）在带该列的库上都会 IntegrityError（接口表现为 500）。
    用真实模型建表后把该列重建为生产同款（NOT NULL 无默认）再插入验证。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from kbserver.models import Base, ProviderProfile

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.execute(text("ALTER TABLE provider_profiles DROP COLUMN prices_json"))
        db.execute(text("ALTER TABLE provider_profiles ADD COLUMN prices_json JSON NOT NULL"))
        db.add(ProviderProfile(
            id="p1", user_id="u1", kind="llm", adapter="openai-compatible",
            role="optimize", endpoint="https://api.example.com/v1", model="m", version=1,
        ))
        db.commit()
        stored = db.execute(text(
            "SELECT prices_json FROM provider_profiles WHERE id='p1'")).scalar_one()
        assert stored == "{}"


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
    assert m["processing"]["format_version"] == "3.0"
    assert m["processing"]["completeness"] == "complete"
    assert m["processing"]["recipe_version"] == "content-v3-1"
    paths = {f["relative_path"] for f in m["files"]}
    assert "content.json" in paths and "preview.md" in paths
    assert "analysis.json" not in paths, "旧输出协议不再写出（docs/23 §5.1）"
    content_file = next(f for f in m["files"] if f["relative_path"] == "content.json")
    assert m["processing"]["result_file_id"] == content_file["file_id"]

    doc = json.loads(
        client.get(f"/v1/items/{item_id}/bundles/{rev}/files/{content_file['file_id']}", headers=auth(token)).content
    )
    assert doc["format_version"] == "3.0" and doc["kind"] == "digest"
    assert doc["summary"] == "先保存，再提炼。"
    blocks = doc["sections"][0]["blocks"]
    assert {b["kind"] for b in blocks} >= {"claim", "quote"}
    assert blocks[0]["refs"] and all(r in doc["references"] for r in blocks[0]["refs"])

    # 去计费后无 /v1/usage 接口
    assert client.get("/v1/usage", headers=auth(token)).status_code == 404


def test_enrich_applies_ai_semantic_paragraphs(client, user_a, session_factory, fake_llm):
    """语义分段：模型返回段首句 → readable/segments.json 按 AI 分组发布并带标记。"""

    def behavior(request):
        prompt = request.user
        if '"task": "这份文本是语音识别的原始输出' in prompt:
            payload = json.loads(prompt)
            segs = json.loads(payload["segments"])
            return llm_result({
                "paragraph_starts": [segs[0]["segment_id"]],
                "corrections": [{"segment_id": segs[0]["segment_id"],
                                 "text": "第一段：可靠保存材料已修正。"}],
            })
        return llm_result(doc_from_prompt(prompt))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2parai",
                      "第一段：可靠保存材料。\n第二段：加工不丢原文。\n第三段：都属同一话题。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
                   headers=auth(token)).json()
    seg_file = next(f for f in m["files"] if f["relative_path"] == "segments.json")
    seg_doc = json.loads(client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{seg_file['file_id']}",
        headers=auth(token)).content)
    assert seg_doc["paragraph_source"] == "ai"
    assert len(seg_doc["paragraphs"]) == 1  # 模型判定整篇同一个话题
    assert seg_doc["segments"][0]["text"] == "第一段：可靠保存材料已修正。"
    assert seg_doc["ai_corrections"][0]["original"] == "第一段：可靠保存材料。"

    readable_file = next(f for f in m["files"] if f["relative_path"] == "readable.md")
    readable = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{readable_file['file_id']}",
        headers=auth(token)).content.decode("utf-8")
    assert readable.count(" ^p") == 1
    assert "已修正" in readable


def test_enrich_routes_paragraphing_to_optimize_profile(client, user_a, session_factory, fake_llm, monkeypatch):
    """分段/纠错走设置里选择的「优化文本」配置，提炼走「整理文本」配置。"""
    calls: list[tuple[str, str]] = []

    def behavior(request):
        prompt = request.user
        if '"task": "这份文本是语音识别的原始输出' in prompt:
            payload = json.loads(prompt)
            segs = json.loads(payload["segments"])
            return llm_result({"paragraph_starts": [segs[0]["segment_id"]], "corrections": []})
        return llm_result(doc_from_prompt(prompt))

    fake_llm.behavior = behavior

    def generate(self, request):
        calls.append((self.kwargs.get("model"), request.user))
        return type(self).behavior(request)

    monkeypatch.setattr(fake_llm, "generate", generate)

    token = user_a["desktop"]["token"]
    # 配置自身能力与挡位设置相反，证明按用途的挡位设置优先生效
    digest = _create_profile(client, token, capabilities={"thinking_mode": False}).json()
    opt = _create_profile(client, token, model="deepseek-flash",
                          capabilities={"thinking_mode": True}).json()
    # 整理/优化档都在设置里显式选择（配置池任一配置均可，不再按角色自动取）；
    # 思考挡位按用途设置：整理 high、优化 off
    patched = client.patch("/v1/settings", json={
        "default_profile_id": digest["id"], "optimize_profile_id": opt["id"],
        "digest_thinking": "high", "optimize_thinking": "off",
    }, headers=auth(token))
    assert patched.status_code == 200
    assert patched.json()["optimize_profile_id"] == opt["id"]
    assert patched.json()["digest_thinking"] == "high"
    assert patched.json()["optimize_thinking"] == "off"
    # 非法思考挡位拒绝
    bad_level = client.patch("/v1/settings", json={"digest_thinking": "ultra"}, headers=auth(token))
    assert bad_level.status_code == 422
    c = _capture_text(client, user_a["phone"]["token"], "m2optroute",
                      "第一段：可靠保存材料。\n第二段：加工不丢原文。\n第三段：都属同一话题。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it

    para_models = {m for m, p in calls if '"task": "这份文本是语音识别的原始输出' in p}
    digest_models = {m for m, p in calls if '"task": "这份文本是语音识别的原始输出' not in p}
    assert para_models == {"deepseek-flash"}
    assert digest_models == {"deepseek-chat"}
    # 挡位按设置覆盖配置自身能力：整理 high（配置 False）、优化 off（配置 True）
    caps_by_model = {i.kwargs["model"]: i.kwargs["capabilities"] for i in fake_llm.instances}
    assert caps_by_model["deepseek-chat"] == {"thinking_mode": True, "thinking_effort": "high"}
    assert caps_by_model["deepseek-flash"] == {"thinking_mode": False}
    # 显式提交 null = 清除该档选择（下拉选回「未设置」）；只提交一档不影响另一档
    cleared = client.patch("/v1/settings", json={"optimize_profile_id": None}, headers=auth(token))
    assert cleared.status_code == 200
    assert cleared.json()["optimize_profile_id"] is None
    assert cleared.json()["default_profile_id"] == digest["id"]


def test_enrich_without_optimize_profile_uses_digest_for_paragraphing(
        client, user_a, session_factory, fake_llm, monkeypatch):
    """设置里未选择优化配置：分段/纠错兜底用整理配置（同一模型），
    思考挡位按用途默认独立生效（整理 high、优化关）。"""
    calls: list[tuple[str, str]] = []

    def behavior(request):
        prompt = request.user
        if '"task": "这份文本是语音识别的原始输出' in prompt:
            payload = json.loads(prompt)
            segs = json.loads(payload["segments"])
            return llm_result({"paragraph_starts": [segs[0]["segment_id"]], "corrections": []})
        return llm_result(doc_from_prompt(prompt))

    fake_llm.behavior = behavior

    def generate(self, request):
        calls.append((self.kwargs.get("model"), request.user))
        return type(self).behavior(request)

    monkeypatch.setattr(fake_llm, "generate", generate)

    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2optfall",
                      "第一段：可靠保存材料。\n第二段：加工不丢原文。\n第三段：都属同一话题。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready", it
    assert {m for m, _ in calls} == {"deepseek-chat"}
    # 跟随时也独立构造优化 provider（同一配置、不同思考挡位）：
    # 整理默认 high，优化默认 off——同一份配置无需配两遍
    assert len(fake_llm.instances) == 2
    caps_list = [i.kwargs["capabilities"] for i in fake_llm.instances]
    assert caps_list[0] == {"thinking_mode": True, "thinking_effort": "high"}
    assert caps_list[1] == {"thinking_mode": False}


def test_enrich_paragraphing_failure_falls_back(client, user_a, session_factory, fake_llm):
    """语义分段调用失败：enrich 照常完成，阅读层保持本地规则分段。"""

    def behavior(request):
        if '"task": "这份文本是语音识别的原始输出' in request.user:
            raise RuntimeError("段落模型不可用")
        return llm_result(doc_from_prompt(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    c = _capture_text(client, user_a["phone"]["token"], "m2parafail",
                      "第一段：可靠保存材料。\n第二段：加工不丢原文。\n第三段：都属同一话题。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
                   headers=auth(token)).json()
    seg_file = next(f for f in m["files"] if f["relative_path"] == "segments.json")
    seg_doc = json.loads(client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{seg_file['file_id']}",
        headers=auth(token)).content)
    assert "paragraph_source" not in seg_doc


def test_enrich_paragraphing_disabled_skips_llm_call(client, user_a, session_factory, fake_llm):
    """关闭「AI 语义分段」：加工不发出分段调用，阅读层保持本地规则分段。"""
    calls = {"n": 0}

    def behavior(request):
        if '"task": "这份文本是语音识别的原始输出' in request.user:
            calls["n"] += 1
            return llm_result({"paragraph_starts": ["s0001"]})
        return llm_result(doc_from_prompt(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    client.patch("/v1/settings", json={"ai_paragraphing": False},
                 headers=auth(token))
    c = _capture_text(client, user_a["phone"]["token"], "m2paraoff",
                      "第一段：可靠保存材料。\n第二段：加工不丢原文。\n第三段：都属同一话题。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    assert calls["n"] == 0  # 分段调用一次都没有发
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
                   headers=auth(token)).json()
    seg_file = next(f for f in m["files"] if f["relative_path"] == "segments.json")
    seg_doc = json.loads(client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{seg_file['file_id']}",
        headers=auth(token)).content)
    assert "paragraph_source" not in seg_doc


def test_paragraphing_runs_even_when_auto_enrich_off(
    client, user_a, session_factory, fake_llm,
):
    """两个开关各自独立：关掉「AI 自动整理」不能把文字优化一起掐掉。

    线上出现过这个形状——用户开着「AI 语义分段与纠错」放了一段录音，转写完成，
    但整理档关着导致 enrich 从不入队，分段/纠错一次也没跑（模型后台 0 调用）。
    """
    calls = {"paragraphing": 0, "digest": 0}

    def behavior(request):
        prompt = request.user
        if '"task": "这份文本是语音识别的原始输出' in prompt:
            calls["paragraphing"] += 1
            payload = json.loads(prompt)
            segs = json.loads(payload["segments"])
            return llm_result({"paragraph_starts": [segs[0]["segment_id"]], "corrections": []})
        calls["digest"] += 1
        return llm_result(doc_from_prompt(prompt))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    client.patch("/v1/settings", json={"auto_enrich": False}, headers=auth(token))
    c = _capture_text(client, user_a["phone"]["token"], "m2paralone",
                      "第一段：可靠保存材料。\n第二段：加工不丢原文。\n第三段：都属同一话题。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert calls["paragraphing"] >= 1  # 分段/纠错照跑
    assert calls["digest"] == 0        # 整理档关着，一次提炼也不发
    # 停在已提取：条目没有长出用户没要的笔记
    assert it["pipeline_state"] == "extracted"
    assert "文字优化" in it["state_detail"]

    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
                   headers=auth(token)).json()
    paths = {f["relative_path"] for f in m["files"]}
    assert "analysis.json" not in paths and "preview.md" not in paths
    seg_file = next(f for f in m["files"] if f["relative_path"] == "segments.json")
    seg_doc = json.loads(client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{seg_file['file_id']}",
        headers=auth(token)).content)
    assert seg_doc["paragraph_source"] == "ai"  # 阅读层确实是 AI 分段的结果


def test_manual_organize_overrides_auto_enrich_off(
    client, user_a, session_factory, fake_llm,
):
    """自动整理关着时，手动「开始整理」仍然要出成品笔记。

    手动入队与自动入队复用同一个 jobs 行，所以「用户点名要整理」必须记在行上，
    否则任务内部只读当前开关的话，这个按钮在自动开关关着时是空点。
    """
    def behavior(request):
        if '"task": "这份文本是语音识别的原始输出' in request.user:
            return llm_result({"paragraph_starts": [], "corrections": []})
        return llm_result(doc_from_prompt(request.user))

    fake_llm.behavior = behavior
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    client.patch("/v1/settings", json={"auto_enrich": False}, headers=auth(token))
    c = _capture_text(client, user_a["phone"]["token"], "m2manual",
                      "第一段：手动整理的原文。\n第二段：先只做文字优化。")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    assert _get_item(client, token, item_id)["pipeline_state"] == "extracted"

    r = client.post(f"/v1/items/{item_id}/reprocess", json={}, headers=auth(token))
    assert r.status_code == 202
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "ready"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
                   headers=auth(token)).json()
    assert "content.json" in {f["relative_path"] for f in m["files"]}


def test_both_auto_switches_off_leaves_item_extracted(
    client, user_a, session_factory, fake_llm,
):
    """两个开关都关：提取完成后不入队，条目停在已提取等手动加工。"""
    fake_llm.behavior = lambda request: llm_result(doc_from_prompt(request.user))
    token = user_a["desktop"]["token"]
    _create_profile(client, token)
    client.patch("/v1/settings", json={"auto_enrich": False, "ai_paragraphing": False},
                 headers=auth(token))
    c = _capture_text(client, user_a["phone"]["token"], "m2bothoff",
                      "第一段：都不开的情况。\n第二段：不该有任何模型调用。")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    assert fake_llm.instances == []  # 一次模型调用都没有发生
    it = _get_item(client, token, item_id)
    assert it["pipeline_state"] == "extracted"
    with session_factory() as db:
        from kbserver.workers import worker

        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "enrich").count() == 0


def test_paragraphing_prompt_includes_subtitle_refs():
    """有平台字幕参考时，分段提示词携带按句对齐的字幕文本；无参考则不带。"""
    from kbserver.domain import templates

    prompt = templates.build_paragraphing_prompt(
        segments=[{"segment_id": "s0001", "text": "语音识别输出。"}],
        subtitle_refs={"s0001": "平台字幕参考文本"})
    assert "subtitle_refs" in prompt
    assert "平台字幕参考文本" in prompt
    plain = templates.build_paragraphing_prompt(
        segments=[{"segment_id": "s0001", "text": "x"}])
    assert "subtitle_refs" in plain and "平台字幕参考文本" not in plain


def test_align_subtitle_refs_by_time_overlap():
    """字幕参考按时间重叠并到 ASR 句段。"""
    from kbserver.workers.enrich import _align_subtitle_refs

    segments = [
        {"segment_id": "s0001", "start_ms": 0, "end_ms": 10000, "text": "x"},
        {"segment_id": "s0002", "start_ms": 10000, "end_ms": 20000, "text": "y"},
    ]
    records = [
        {"start_ms": 500, "end_ms": 6000, "text": "甲"},
        {"start_ms": 12000, "end_ms": 15000, "text": "乙"},
    ]
    assert _align_subtitle_refs(segments, records) == {"s0001": "甲", "s0002": "乙"}
    assert _align_subtitle_refs(segments, None) == {}


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
    assert calls["n"] == 3  # 提取坏输出 + 修复 + 语义分段调用


def test_enrich_validation_failure_keeps_diagnostic(client, user_a, session_factory, fake_llm):
    def behavior(request):
        doc = doc_from_prompt(request.user)
        for section in doc.get("sections") or []:
            for block in section["blocks"]:
                block["refs"] = ["R9999"]  # 永远引用不存在的阅读单元
                if block["kind"] == "quote":
                    block["text"] = "凭空拼出来的摘录"
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
    assert "content.error.json" in {f["relative_path"] for f in m["files"]}
    # 不覆盖成品：没有 content.json 的 ready 声明
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
        if "请合并成一篇提炼结果" in prompt:
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


def test_conversation_mode_prompts_for_decision_context(client, user_a, session_factory, fake_llm):
    """对话类材料：提示词要求保留提出/接受/否定/未知的决策上下文（docs/23 §3.3、§8.1）。

    v3 不再有 workflow 输出结构，决策状态作为自然章节由模型写在 sections 里。
    """
    prompts: dict[str, str] = {}

    def behavior(request):
        payload = json.loads(request.user)
        if payload.get("material"):
            prompts[payload.get("task") or "digest"] = request.user
        return llm_result(doc_from_prompt(request.user))

    fake_llm.behavior = behavior
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
    assert it["pipeline_state"] == "ready", it
    assert "决策与结果" in next(iter(prompts.values())), "对话材料要带决策状态说明"
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest", headers=auth(token)).json()
    content_file = next(f for f in m["files"] if f["relative_path"] == "content.json")
    doc = json.loads(
        client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{content_file['file_id']}",
                   headers=auth(token)).content)
    assert "workflow" not in doc, "对话状态不再走独立的 workflow 结构"
