"""MCP server 验收（docs/27 Phase 0a + Phase 1）。

覆盖三件必须成立的事：
1. 用户隔离：A 的 token 拿不到 B 的任何条目；
2. 撤销即失效：停用后下一个请求就 401，不留「等 TTL 自然过期」的半失效状态；
3. **每个被禁用的能力一条负向测试**：改原文、删除、refetch、模型 Key、admin、
   同步回执都不在工具清单里，读类工具也做不出写以外的效果。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.domain import pipeline
from kbserver.models import Device, Item, Token, User, utcnow
from kbserver.config import get_settings
from kbserver.security.tokens import (
    AGENT_DEVICE_KIND,
    MCP_READ_SCOPE,
    MCP_WRITE_SCOPE,
    issue_agent_token,
)


def get_master_key() -> bytes:
    return get_settings().load_master_key()

MCP_PATH = "/mcp"
PROTOCOL_VERSION = "2025-11-25"  # Phase 0b 实测 dsh 协商到的版本


# ---- 传输层：把 MCP 的 JSON-RPC 形态包成几行可读的断言 ----

def _rpc(client: TestClient, token: str, method: str, params: dict | None = None,
         *, request_id: int | None = 1) -> dict:
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if method != "initialize":
        # 无状态模式下每个 POST 是独立请求，协议版本靠这个头带（dsh 实测就是这样）
        headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
    body: dict[str, Any] = {"jsonrpc": "2.0"}
    if request_id is not None:
        body["id"] = request_id
    body["method"] = method
    if params is not None:
        body["params"] = params
    return client.post(MCP_PATH, headers=headers, json=body)


def handshake(client: TestClient, token: str) -> str:
    """initialize -> initialized，返回协商到的协议版本。"""
    resp = _rpc(client, token, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "kb-test", "version": "0"},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    version = data["result"]["protocolVersion"]
    note = _rpc(client, token, "notifications/initialized", {}, request_id=None)
    assert note.status_code in (200, 202), note.text
    return version


def tool_names(client: TestClient, token: str) -> list[str]:
    resp = _rpc(client, token, "tools/list", {})
    assert resp.status_code == 200, resp.text
    return [t["name"] for t in resp.json()["result"]["tools"]]


def call_tool(client: TestClient, token: str, name: str, arguments: dict) -> dict:
    """返回 (是否报错, 解析后的 JSON, 原始文本)。

    工具报错走 MCP 的 isError + 文本（`_surface_errors` 把错误码写在文本开头），
    所以断言既看 payload 也看 text。
    """
    resp = _rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    parsed: dict = {}
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {"raw": text}
    return {"is_error": bool(result.get("isError")), "payload": parsed, "text": text}


def extract_now(factory, item_id: str) -> None:
    """把这篇条目的提取阶段就地跑完：正文要经过 extract 才成为归档的 normalized.md。

    测试里不预先塞一个「看起来像原文」的文件——那样测不到 read_source 真正读的产物。
    """
    from kbserver.storage.objects import ObjectStore
    from kbserver.workers import worker as worker_mod
    from kbserver.models import Job

    with factory() as db:
        item = db.get(Item, item_id)
        job = db.query(Job).filter(Job.item_id == item_id, Job.stage == "extract").first()
        assert item is not None and job is not None
        job.state = "running"
        job.lease_token = "test-lease"
        db.flush()
        worker_mod.run_extract(db, ObjectStore(), job, item)
        job.state = "succeeded"
        db.commit()


# ---- 夹具 ----

@pytest.fixture()
def agent_env(session_factory, monkeypatch):
    """开一个只服务于本模块的应用实例：AGENT_ENABLED 决定 /mcp 挂不挂。"""
    monkeypatch.setenv("AGENT_ENABLED", "1")
    factory = session_factory

    def make_user(label: str, scopes: list[str]) -> dict:
        with factory() as db:
            user = User(name=f"MCP {label}")
            db.add(user)
            db.flush()
            device = Device(user_id=user.id, kind=AGENT_DEVICE_KIND, name=f"{label}-agent")
            db.add(device)
            db.flush()
            raw, token = issue_agent_token(user.id, device.id, scopes)
            db.add(token)
            item_id = None
            if label != "只读":
                payload = {
                    "schema_version": "1.0", "client_capture_id": f"mcp-fixture-{label}",
                    "input_kind": "text", "text": f"{label} 的私有正文", "user_note": label,
                    "upload_ids": [], "capture_channel": "web_inbox",
                    "processing_intent": "default", "archive_policy": "source_materials",
                    "content_scope": "unknown",
                }
                from kbserver.storage.objects import ObjectStore
                _, item = pipeline.create_capture(db, ObjectStore(), user_id=user.id,
                                                  payload=payload, uploads={})
                item_id = item.id
            db.commit()
            return {"token": raw, "user_id": user.id, "item_id": item_id,
                    "device_id": device.id, "token_row_id": token.id}

    with factory() as db:
        other = User(name="MCP 隔壁用户")
        db.add(other)
        db.flush()
        payload = {
            "schema_version": "1.0", "client_capture_id": "mcp-fixture-other",
            "input_kind": "text", "text": "隔壁用户的正文，谁都不该看到",
            "user_note": "other", "upload_ids": [], "capture_channel": "web_inbox",
            "processing_intent": "default", "archive_policy": "source_materials",
            "content_scope": "unknown",
        }
        from kbserver.storage.objects import ObjectStore
        _, other_item = pipeline.create_capture(db, ObjectStore(), user_id=other.id,
                                                payload=payload, uploads={})
        db.commit()
        other_item_id = other_item.id

    with TestClient(create_app()) as client:
        yield {
            "client": client,
            "a": make_user("A", [MCP_READ_SCOPE, MCP_WRITE_SCOPE]),
            "ro": make_user("只读", [MCP_READ_SCOPE]),
            "other_item_id": other_item_id,
            "factory": factory,
        }


def test_mcp_handshake_and_tools_list(agent_env):
    client, token = agent_env["client"], agent_env["a"]["token"]
    assert handshake(client, token) == PROTOCOL_VERSION
    names = tool_names(client, token)
    assert names == [
        "kb_list_items", "kb_search_items", "kb_get_item", "kb_read_source", "kb_read_digest",
        "kb_capture_note", "kb_draft_note", "kb_submit_job", "kb_get_job",
    ]


def test_mcp_disabled_capabilities_are_not_registered(agent_env):
    """负向：被禁的能力连名字都不该出现在工具清单里（不注册比运行时判断更硬）。"""
    names = " ".join(tool_names(agent_env["client"], agent_env["a"]["token"]))
    for forbidden in ("source_text", "source-text", "delete", "remove", "refetch",
                      "profile", "credential", "api_key", "admin", "receipt", "sync",
                      "publish", "kb_edit"):
        assert forbidden not in names, f"工具清单里出现了被禁的能力：{forbidden}"


def test_mcp_user_isolation(agent_env):
    client = agent_env["client"]
    token = agent_env["a"]["token"]
    handshake(client, token)

    listed = call_tool(client, token, "kb_list_items", {"limit": 50})
    ids = {i["item_id"] for i in listed["payload"]["items"]}
    assert agent_env["other_item_id"] not in ids
    assert ids == {agent_env["a"]["item_id"]}

    # 别人的 item_id 一律 404，且不暴露「存在但不属于你」这种差异
    peek = call_tool(client, token, "kb_get_item", {"item_id": agent_env["other_item_id"]})
    assert peek["is_error"] is True
    assert "NOT_FOUND" in peek["text"]

    read = call_tool(client, token, "kb_read_source", {"item_id": agent_env["other_item_id"]})
    assert read["is_error"] is True


def test_mcp_bad_token_is_401(agent_env):
    resp = _rpc(agent_env["client"], "kbi_" + "z" * 43, "initialize", {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "x", "version": "0"}})
    assert resp.status_code == 401


def test_mcp_revocation_takes_effect_on_next_request(agent_env):
    client, a = agent_env["client"], agent_env["a"]
    handshake(client, a["token"])
    assert not call_tool(client, a["token"], "kb_list_items", {})["is_error"]

    with agent_env["factory"]() as db:
        row = db.get(Token, a["token_row_id"])
        row.revoked_at = utcnow()
        db.commit()

    resp = _rpc(client, a["token"], "tools/list", {})
    assert resp.status_code == 401


def test_mcp_read_only_token_cannot_write(agent_env):
    client, ro = agent_env["client"], agent_env["ro"]
    handshake(client, ro["token"])
    # 读可以
    assert not call_tool(client, ro["token"], "kb_list_items", {})["is_error"]
    # 写一律 403
    for name, args in (("kb_capture_note", {"text": "投一条笔记", "client_capture_id": "mcp-ro-0001"}),
                       ("kb_draft_note", {"title": "草稿", "markdown": "内容"}),
                       ("kb_submit_job", {"kind": "optimize_text", "item_id": ro["item_id"]})):
        result = call_tool(client, ro["token"], name, args)
        assert result["is_error"] is True, f"{name} 不该让只有 mcp:read 的 token 写成"
        assert "FORBIDDEN" in result["text"]


def test_mcp_capture_note_is_idempotent_and_lands_in_inbox(agent_env):
    client, a = agent_env["client"], agent_env["a"]
    handshake(client, a["token"])
    args = {"text": "agent 帮我把这段存下来", "title": "随手记",
            "client_capture_id": "mcp-note-0001", "idempotency_key": "mcp-note-0001"}
    first = call_tool(client, a["token"], "kb_capture_note", args)
    assert first["is_error"] is False, first["text"]
    second = call_tool(client, a["token"], "kb_capture_note", args)
    assert second["payload"]["item_id"] == first["payload"]["item_id"], "同一幂等键重复调用不该建两条"

    with agent_env["factory"]() as db:
        item = db.get(Item, first["payload"]["item_id"])
        assert item is not None and item.user_id == a["user_id"]
        rows = db.query(Item).filter(Item.user_id == a["user_id"]).count()
    listed = call_tool(client, a["token"], "kb_list_items", {"limit": 50})
    assert {i["item_id"] for i in listed["payload"]["items"]} - {first["payload"]["item_id"],
                                                                 a["item_id"]} == set()
    assert rows == listed["payload"]["total"]


def test_mcp_read_source_reports_truncation_and_versions(agent_env):
    client, a = agent_env["client"], agent_env["a"]
    handshake(client, a["token"])
    extract_now(agent_env["factory"], a["item_id"])
    full = call_tool(client, a["token"], "kb_read_source",
                     {"item_id": a["item_id"], "kind": "normalized"})
    payload = full["payload"]
    assert payload["available"] is True
    assert {"source_revision", "bundle_revision", "truncated"} <= set(payload)
    assert payload["truncated"] is False

    tiny = call_tool(client, a["token"], "kb_read_source",
                     {"item_id": a["item_id"], "kind": "normalized", "max_chars": 2})
    assert tiny["payload"]["truncated"] is True
    assert len(tiny["payload"]["text"]) <= 2

    # max_chars 的上限由服务端夹住，不给模型一次读穿上下文的空间
    huge = call_tool(client, a["token"], "kb_read_source",
                     {"item_id": a["item_id"], "kind": "normalized", "max_chars": 10_000_000})
    assert huge["payload"]["chars_returned"] <= 60_000


def test_mcp_draft_note_keeps_only_real_sources(agent_env):
    client, a = agent_env["client"], agent_env["a"]
    handshake(client, a["token"])
    result = call_tool(client, a["token"], "kb_draft_note", {
        "title": "两篇材料的小结",
        "markdown": "## 要点\n\n- 第一条",
        "source_item_ids": [a["item_id"], agent_env["other_item_id"], "does-not-exist"],
        "idempotency_key": "mcp-draft-0001",
    })
    assert result["is_error"] is False, result["text"]
    sources = result["payload"]["sources"]
    assert [s["item_id"] for s in sources] == [a["item_id"]], "别人的条目必须丢掉，不引进草稿"
    extract_now(agent_env["factory"], result["payload"]["item_id"])
    body = call_tool(client, a["token"], "kb_read_source",
                     {"item_id": result["payload"]["item_id"], "kind": "normalized"})
    assert a["item_id"] in body["payload"]["text"]


def test_mcp_digest_reports_state_honestly(agent_env):
    client, a = agent_env["client"], agent_env["a"]
    handshake(client, a["token"])
    result = call_tool(client, a["token"], "kb_read_digest", {"item_id": a["item_id"]})
    payload = result["payload"]
    assert payload["state"] in ("pending", "ready", "failed", "expired", "unknown_format")
    assert payload["state"] == "pending"
    assert payload["text"] == ""
    # 没有整理稿时必须给一句人能看懂的说明，不能只留一个空对象
    assert payload["state_detail"]


def test_mcp_submit_and_poll_job_handle(agent_env):
    client, a = agent_env["client"], agent_env["a"]
    handshake(client, a["token"])
    submitted = call_tool(client, a["token"], "kb_submit_job",
                          {"kind": "reprocess_item", "item_id": a["item_id"],
                           "idempotency_key": "mcp-job-0001"})
    assert submitted["is_error"] is False, submitted["text"]
    job_id = submitted["payload"]["job_id"]

    again = call_tool(client, a["token"], "kb_submit_job",
                      {"kind": "reprocess_item", "item_id": a["item_id"],
                       "idempotency_key": "mcp-job-0001"})
    assert again["payload"]["job_id"] == job_id, "同一幂等键重复提交要回到同一个句柄"

    unknown = call_tool(client, a["token"], "kb_submit_job",
                        {"kind": "delete_item", "item_id": a["item_id"]})
    assert unknown["is_error"] is True

    # 别人的任务句柄读不到（与不存在的句柄同一个答案）
    other = call_tool(client, a["token"], "kb_get_job", {"job_id": "0" * 32})
    assert other["is_error"] is True

    from kbserver.models import AgentTask, Job
    with agent_env["factory"]() as db:
        task = db.get(AgentTask, job_id)
        assert task is not None and task.job_id is not None
        job = db.get(Job, task.job_id)
        assert job is not None and job.item_id == a["item_id"]
        # 底层加工完成 → 句柄如实变 succeeded，并带上整理稿所在版本
        job.state = "succeeded"
        db.commit()

    from kbserver.domain.agent_tasks import execute_agent_task
    with agent_env["factory"]() as db:
        from kbserver.models import utcnow
        task = db.get(AgentTask, job_id)
        task.state = "running"
        task.lease_token = "lease-for-test"
        task.lease_until = utcnow()
        db.commit()
        lease = task.lease_token
    execute_agent_task(agent_env["factory"], job_id, lease)

    polled = call_tool(client, a["token"], "kb_get_job", {"job_id": job_id})
    assert polled["payload"]["state"] == "succeeded"
    assert polled["payload"]["result"]["item_id"] == a["item_id"]


def test_mcp_accepts_first_party_capability_token(agent_env):
    """网页面板是登录态，容器替用户读他自己的库用 A 机现签的短时能力凭据。"""
    from kbserver.security.agent_llm_tokens import (
        issue_llm_token, issue_mcp_capability,
    )
    from kbserver.security.tokens import MCP_READ_SCOPE, MCP_WRITE_SCOPE

    master = get_master_key()
    a = agent_env["a"]
    cap, _ = issue_mcp_capability(master, site="kb", user_id=a["user_id"],
                                  scopes=[MCP_READ_SCOPE, MCP_WRITE_SCOPE])
    client = agent_env["client"]
    handshake(client, cap)
    listed = call_tool(client, cap, "kb_list_items", {"limit": 10})
    assert listed["is_error"] is False
    assert {i["item_id"] for i in listed["payload"]["items"]} == {a["item_id"]}
    # 写也可以：面板允许 agent 往用户自己的收件箱投草稿
    draft = call_tool(client, cap, "kb_capture_note",
                      {"text": "面板里让 agent 存的", "client_capture_id": "mcp-cap-0001"})
    assert draft["is_error"] is False

    # 别人的条目照样读不到
    other = call_tool(client, cap, "kb_get_item", {"item_id": agent_env["other_item_id"]})
    assert other["is_error"] is True and "NOT_FOUND" in other["text"]


def test_mcp_rejects_wrong_purpose_and_expired_capability(agent_env):
    from kbserver.security.agent_llm_tokens import issue_llm_token, issue_mcp_capability
    from kbserver.security.tokens import MCP_READ_SCOPE

    master = get_master_key()
    a = agent_env["a"]
    client = agent_env["client"]
    # LLM 会话 token 与 MCP 能力 token 是两把用途密钥，互相顶替过不了
    llm_token, _ = issue_llm_token(master, site="kb", user_id=a["user_id"], profile_id="p1")
    assert _rpc(client, llm_token, "initialize", {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "x", "version": "0"}}).status_code == 401

    expired, _ = issue_mcp_capability(master, site="kb", user_id=a["user_id"],
                                      scopes=[MCP_READ_SCOPE], ttl_seconds=-1)
    assert _rpc(client, expired, "tools/list", {}).status_code == 401

    # 别站点的能力凭据不认（结构性隔离，不是先信再过滤）
    other_site, _ = issue_mcp_capability(master, site="ledger", user_id=a["user_id"],
                                        scopes=[MCP_READ_SCOPE])
    assert _rpc(client, other_site, "tools/list", {}).status_code == 401

    # 空 scope 的能力凭据也不该过
    empty, _ = issue_mcp_capability(master, site="kb", user_id=a["user_id"], scopes=[])
    assert _rpc(client, empty, "tools/list", {}).status_code == 401


def test_mcp_disabled_flag_hides_the_surface(session_factory, monkeypatch):
    """AGENT_ENABLED=false 时 /mcp 根本不存在，不是「进门再拦」。"""
    monkeypatch.setenv("AGENT_ENABLED", "0")
    with TestClient(create_app()) as client:
        resp = client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {}},
                           headers={"Authorization": "Bearer kbi_" + "q" * 40})
        assert resp.status_code == 404
