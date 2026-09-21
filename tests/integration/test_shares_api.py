"""/v1/shares 私有接口（docs/20 §12；验收 A18/A19/A22/A26/A27 的接口部分）。

用模拟模型响应与模拟 runner 结果走通接口，不产生真实模型调用。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from kbserver.config import get_settings
from kbserver.domain import provider_ops
from kbserver.models import (
    Capture,
    Credential,
    Item,
    ProviderProfile,
    ShareRun,
    SourceRevision,
    StoredFile,
    User,
    new_id,
    utcnow,
)
from kbserver.repositories import shares as repo
from kbserver.security import credentials as cred_crypto
from kbserver.security.tokens import issue_token
from kbserver.storage.objects import ObjectStore
from kbserver.workers import share as share_worker

from tests.conftest import auth  # noqa: F401  复用统一鉴权头写法
from tests.integration.test_share_worker import (  # 复用同一套替身与样例输出
    CLARIFY_1, CLARIFY_2, FakeProvider, PAGE_SOURCE, synthesis_for,
)

SHARE_SCOPES = ["shares:read", "shares:write", "items:read"]


@pytest.fixture(autouse=True)
def _fake(monkeypatch, tmp_path):
    FakeProvider.calls = []
    FakeProvider.next_docs = []
    monkeypatch.setattr(share_worker, "OpenAICompatibleProvider", FakeProvider)
    monkeypatch.setenv("SHARE_ENABLED", "true")
    monkeypatch.setenv("SHARE_RUNNER_POLL_SECONDS", "0")
    monkeypatch.setenv("SHARE_SPOOL_DIR", str(tmp_path / "spool"))


@pytest.fixture()
def share_env(db, session_factory):
    """两个用户，各自一条有正文的材料与一份模型配置。"""
    store = ObjectStore()
    settings = get_settings()
    users = {}
    for label in ("甲", "乙"):
        user = User(name=f"分享{label}")
        db.add(user)
        db.flush()
        raw, token = issue_token(user.id, _device(db, user.id).id, SHARE_SCOPES)
        db.add(token)
        capture = Capture(user_id=user.id, client_capture_id=f"c-{label}", request_hash="h",
                          input_json={})
        db.add(capture)
        db.flush()
        item = Item(user_id=user.id, capture_id=capture.id, source_revision=1, bundle_revision=1)
        db.add(item)
        db.flush()
        segments = {"source_revision": 1, "segments": [
            {"segment_id": "s0001", "text": "先分型，再谈用量。"},
            {"segment_id": "s0002", "text": "剂量随证候浮动。"}]}
        sha, key, size = store.put_bytes(json.dumps(segments, ensure_ascii=False).encode("utf-8"))
        db.add(StoredFile(file_id=f"f-{label}", user_id=user.id, item_id=item.id,
                         role="source_material", relative_path="segments.json",
                         mime="application/json", bytes=size, sha256=sha, storage_key=key))
        db.add(SourceRevision(item_id=item.id, user_id=user.id, revision=1, content_hash="x",
                              metadata_json={"title": f"材料{label}", "coverage": "full_text",
                                             "canonical_url": "https://example.org/a"},
                              artifacts_json={}))
        profile = ProviderProfile(user_id=user.id, kind="llm", adapter="openai-compatible",
                                  role="digest", endpoint="https://api.deepseek.com/v1",
                                  model="deepseek-chat", capabilities_json={}, prices_json={},
                                  meta_json={})
        db.add(profile)
        db.flush()
        envelope = cred_crypto.encrypt_secret("sk-fake", settings.load_master_key(),
                                             user_id=user.id, profile_id=profile.id,
                                             credential_version=1)
        db.add(Credential(user_id=user.id, profile_id=profile.id, master_key_version=1, **envelope))
        users[label] = {"user_id": user.id, "item_id": item.id, "profile_id": profile.id,
                        "headers": auth(raw)}
    db.commit()
    for entry in users.values():
        entry["session_factory"] = session_factory
    return {"a": users["甲"], "b": users["乙"], "session_factory": session_factory}


def _device(db, user_id: str):
    from kbserver.models import Device

    device = Device(user_id=user_id, kind="web", name="分享测试")
    db.add(device)
    db.flush()
    return device


def create(client, env, **body) -> tuple[str, str]:
    resp = client.post("/v1/shares", json={"item_ids": [env["item_id"]],
                                          "instructions": "整理出差异，适合初学者", **body},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 202, resp.text
    data = resp.json()
    return data["share_id"], data["run_id"]


def run_stage(env, run_id: str) -> ShareRun:
    """按 id 领取并执行一次（测试里不靠队列轮转）。"""
    sf = env["session_factory"]
    with sf() as db:
        run = db.get(ShareRun, run_id)
        run.state = "running"
        run.lease_token = new_id()
        run.lease_until = utcnow() + timedelta(seconds=120)
        token = run.lease_token
        db.commit()
    share_worker.execute(sf, run_id, token)
    with sf() as db:
        return db.get(ShareRun, run_id)


def write_result(env, run: ShareRun, *, ok=True) -> None:
    settings = get_settings()
    task_id = (run.checkpoint_json or {})["runner_task"]
    target = Path(settings.share_spool_dir) / ("done" if ok else "failed") / task_id
    import shutil

    shutil.rmtree(target, ignore_errors=True)
    (target / "out").mkdir(parents=True, exist_ok=True)
    html = b'<!doctype html><main><iframe id="kb-frame" sandbox="allow-scripts" srcdoc=""></iframe></main>'
    (target / "out" / "index.html").write_bytes(html)
    import hashlib

    (target / "result.json").write_text(json.dumps({
        "task_id": task_id, "lease_id": share_worker.runner_lease_id(run.id, task_id),
        "ok": ok, "html_sha256": hashlib.sha256(html).hexdigest(), "html_bytes": len(html),
        "runtime_version": "share-runtime-1.0.0", "diagnostics": [], "checks": [],
        "screenshots": [], "build": {},
    }), encoding="utf-8")


def to_ready(client, env, share_id: str, run_id: str) -> ShareRun:
    """走一遍：澄清 → 回答 → 确认 → 生成 → runner → 可用草稿。"""
    run = run_stage(env, run_id)
    assert run.state == "waiting_user"
    conv = client.get(f"/v1/shares/{share_id}/runs/{run_id}/conversation", headers=env["headers"])
    assert conv.status_code == 200
    questions = conv.json()["round"]["questions"]
    FakeProvider.next_docs = [synthesis_for(_pack(run))]
    resp = client.post(f"/v1/shares/{share_id}/runs/{run_id}/messages",
                       json={"expected_conversation_version": conv.json()["conversation_version"],
                             "round_id": conv.json()["round"]["round_id"],
                             "answers": [{"question_id": questions[0]["id"],
                                          "option_ids": [questions[0]["options"][0]["id"]],
                                          "text": ""}],
                             "message": "主要在手机上阅读"},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 202, resp.text
    run = run_stage(env, run_id)
    assert run.state == "awaiting_confirmation", run.error_detail
    started = client.post(f"/v1/shares/{share_id}/runs/{run_id}/start",
                          json={"expected_brief_version": run.brief_version, "mode": "confirm"},
                          headers={**env["headers"], "Idempotency-Key": new_id()})
    assert started.status_code == 202, started.text
    run = run_stage(env, run_id)
    assert run.stage == "awaiting_runner", run.error_detail
    write_result(env, run)
    return run_stage(env, run_id)


def _pack(run: ShareRun) -> dict:
    return json.loads(ObjectStore().read_object(
        (run.checkpoint_json or {})["pack_storage_key"]).decode("utf-8"))


# ---- 创建与幂等 ----


def test_create_requires_idempotency_key(client, share_env):
    env = share_env["a"]
    resp = client.post("/v1/shares", json={"item_ids": [env["item_id"]], "instructions": "做对照页"},
                       headers=env["headers"])
    assert resp.status_code == 422


def test_double_click_returns_same_run_without_second_task(client, share_env):
    """A19：同 key 同请求返回原响应，不重复建任务、不重复调用模型。"""
    env = share_env["a"]
    body = {"item_ids": [env["item_id"]], "instructions": "做对照页"}
    key = new_id()
    first = client.post("/v1/shares", json=body, headers={**env["headers"], "Idempotency-Key": key})
    second = client.post("/v1/shares", json=body, headers={**env["headers"], "Idempotency-Key": key})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    listing = client.get("/v1/shares", headers=env["headers"]).json()
    assert listing["total"] == 1


def test_same_key_different_body_conflicts(client, share_env):
    env = share_env["a"]
    key = new_id()
    a = client.post("/v1/shares", json={"item_ids": [env["item_id"]], "instructions": "甲"},
                    headers={**env["headers"], "Idempotency-Key": key})
    b = client.post("/v1/shares", json={"item_ids": [env["item_id"]], "instructions": "乙"},
                    headers={**env["headers"], "Idempotency-Key": key})
    assert a.status_code == 202 and b.status_code == 409


def test_unreadable_material_names_the_item(client, share_env):
    """A07：只有链接/标题时指出具体条目，不用模型猜测补全。"""
    env = share_env["a"]
    with share_env["session_factory"]() as db:
        f = db.query(StoredFile).filter_by(item_id=env["item_id"],
                                           relative_path="segments.json").one()
        db.delete(f)
        db.commit()
    resp = client.post("/v1/shares", json={"item_ids": [env["item_id"]], "instructions": "做页"},
                       headers=env["headers"])
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert details["unreadable_items"][0]["item_id"] == env["item_id"]


# ---- 用户隔离 ----


def test_other_user_cannot_touch_work(client, share_env):
    """A18：不能读取、修改、预览、下载或发布他人私有数据。"""
    a, b = share_env["a"], share_env["b"]
    share_id, run_id = create(client, a)
    for method, path in (
        ("get", f"/v1/shares/{share_id}"),
        ("get", f"/v1/shares/{share_id}/runs/{run_id}/conversation"),
        ("get", f"/v1/shares/{share_id}/revisions/1/preview-content"),
        ("get", f"/v1/shares/{share_id}/revisions/1/download"),
        ("get", f"/v1/shares/{share_id}/link"),
        ("delete", f"/v1/shares/{share_id}"),
    ):
        resp = getattr(client, method)(path, headers=b["headers"])
        assert resp.status_code == 404, (method, path, resp.status_code)
    resp = client.post(f"/v1/shares/{share_id}/revoke", json={}, headers=b["headers"])
    assert resp.status_code == 404
    with share_env["session_factory"]() as db:
        assert db.get(ShareRun, run_id).state == "queued"


# ---- 对话与生成 ----


def test_answer_requires_current_round(client, share_env):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    run_stage(env, run_id)
    resp = client.post(f"/v1/shares/{share_id}/runs/{run_id}/messages",
                       json={"expected_conversation_version": 1, "round_id": "stale",
                             "answers": [], "message": "随便"},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"


def test_answer_rejects_option_from_other_question(client, share_env):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    run_stage(env, run_id)
    round_id = client.get(f"/v1/shares/{share_id}/runs/{run_id}/conversation",
                          headers=env["headers"]).json()["round"]["round_id"]
    resp = client.post(f"/v1/shares/{share_id}/runs/{run_id}/messages",
                       json={"expected_conversation_version": 1, "round_id": round_id,
                             "answers": [{"question_id": "nope", "option_ids": [], "text": "x"}],
                             "message": ""},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 422


def test_start_before_brief_is_ready_is_actionable_409(client, share_env):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    resp = client.post(f"/v1/shares/{share_id}/runs/{run_id}/start",
                       json={"mode": "confirm"},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["state"] == "queued"


def test_full_flow_produces_private_draft_with_no_store(client, share_env):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    run = to_ready(client, env, share_id, run_id)
    assert run.state == "succeeded", run.error_detail

    detail = client.get(f"/v1/shares/{share_id}", headers=env["headers"]).json()
    assert detail["revisions"][0]["revision"] == 1
    assert detail["actions"]["can_publish"] is True
    assert detail["share"]["status"] == "private"

    preview = client.get(f"/v1/shares/{share_id}/revisions/1/preview-content", headers=env["headers"])
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert "iframe" in preview.json()["document"]

    token = client.post(f"/v1/shares/{share_id}/revisions/1/preview-token", headers=env["headers"])
    assert token.status_code == 200
    assert token.json()["preview_path"].endswith("preview-content")

    download = client.get(f"/v1/shares/{share_id}/revisions/1/download", headers=env["headers"])
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")


def test_public_link_requires_isolated_share_site(client, share_env, monkeypatch):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id)
    with share_env["session_factory"]() as db:
        from kbserver.models import ShareWork
        work = db.query(ShareWork).filter_by(id=share_id).one()
        revision_id = work.latest_ready_revision_id
    monkeypatch.delenv("SHARE_PUBLIC_BASE_URL", raising=False)
    resp = client.post(f"/v1/shares/{share_id}/publish",
                       json={"revision_id": revision_id},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "share_site_not_configured"

    monkeypatch.setenv("SHARE_PUBLIC_BASE_URL", "https://share.example")
    published = client.post(f"/v1/shares/{share_id}/publish", json={"revision_id": revision_id},
                            headers={**env["headers"], "Idempotency-Key": new_id()})
    assert published.status_code == 200, published.text
    url = published.json()["url"]
    assert url.startswith("https://share.example/s/")
    link = client.get(f"/v1/shares/{share_id}/link", headers=env["headers"])
    assert link.json()["url"] == url
    assert link.headers["cache-control"] == "no-store"

    revoked = client.post(f"/v1/shares/{share_id}/revoke", json={}, headers=env["headers"])
    assert revoked.json()["status"] == "revoked"
    assert client.get(f"/v1/shares/{share_id}/link", headers=env["headers"]).json()["url"] is None
    again = client.post(f"/v1/shares/{share_id}/publish", json={"revision_id": revision_id},
                        headers={**env["headers"], "Idempotency-Key": new_id()})
    assert again.json()["url"] != url, "撤销后重新发布必须换新链接"


def test_stale_work_version_conflicts_on_modify(client, share_env):
    """A22：版本基线变了返回 409 与当前版本，不覆盖较新作品。"""
    env = share_env["a"]
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id)
    with share_env["session_factory"]() as db:
        from kbserver.models import ShareRevision, ShareWork
        work = db.query(ShareWork).filter_by(id=share_id).one()
        revision = db.get(ShareRevision, work.latest_ready_revision_id)
        revision_id = revision.id
        current_version = work.version
    resp = client.post(f"/v1/shares/{share_id}/runs",
                       json={"base_revision_id": revision_id, "instructions": "缩短一点",
                             "expected_work_version": current_version + 5},
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["current_version"] == current_version


def test_cancel_stops_further_model_calls(client, share_env):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    run_stage(env, run_id)
    resp = client.post(f"/v1/shares/{share_id}/runs/{run_id}/cancel", headers=env["headers"])
    assert resp.status_code == 200
    calls = len(FakeProvider.calls)
    again = run_stage(env, run_id)
    assert again.state == "cancelled"
    assert len(FakeProvider.calls) == calls


def test_retry_reuses_fixed_input_as_new_run(client, share_env):
    env = share_env["a"]
    share_id, run_id = create(client, env)
    run_stage(env, run_id)
    client.post(f"/v1/shares/{share_id}/runs/{run_id}/cancel", headers=env["headers"])
    resp = client.post(f"/v1/shares/{share_id}/runs/{run_id}/retry",
                       headers={**env["headers"], "Idempotency-Key": new_id()})
    assert resp.status_code == 202, resp.text
    new_run_id = resp.json()["run_id"]
    assert new_run_id != run_id
    with share_env["session_factory"]() as db:
        old, new = db.get(ShareRun, run_id), db.get(ShareRun, new_run_id)
        assert new.checkpoint_json["items"] == old.checkpoint_json["items"]
        assert new.state == "queued"


def test_share_html_does_not_leak_private_material(client, share_env):
    """A27 的接口侧：预览数据里不出现原文包、完整要求、Key 或诊断。"""
    env = share_env["a"]
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id)
    doc = client.get(f"/v1/shares/{share_id}/revisions/1/preview-content",
                     headers=env["headers"]).json()["document"]
    for secret in ("sk-fake", "storage_key", "source_pack", "先分型，再谈用量。"):
        assert secret not in doc, secret
