"""编排服务的状态层与调度（docs/27 §Phase 3b）。

不启 dsh 子进程：把 `DshSessionProcess` 换成一个脚本化的替身，测的是编排层
自己那套必须成立的性质 —— 序号单调、事件白名单、准入排队不硬起、回传幂等、
回收只在途轮次为零时发生、跨用户跨站点看不见彼此。

上游真实行为由 Phase 0b 的实测记录（docs/27），不靠这里替它担保。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "agent"))

from orchestrator import admission  # noqa: E402
from orchestrator.config import OrchestratorSettings  # noqa: E402
from orchestrator.sessions import STATUS_QUEUED, SessionManager  # noqa: E402
from orchestrator.store import Store  # noqa: E402


class FakeKb:
    """A 机客户端替身：只记下容器打算回传什么。"""

    def __init__(self) -> None:
        self.declared: list[dict] = []
        self.mirrored: list[dict] = []
        self.llm_calls = 0

    def declare_session(self, *, user_id: str, session_id: str, title: str) -> bool:
        self.declared.append({"user_id": user_id, "session_id": session_id, "title": title})
        return True

    def mirror(self, *, user_id: str, session_id: str, events: list[dict]) -> bool:
        self.mirrored.append({"user_id": user_id, "session_id": session_id, "events": events})
        return True

    def llm_token(self, user_id: str, *, profile_id: str | None = None) -> dict:
        self.llm_calls += 1
        return {"token": f"sess-token-{self.llm_calls}", "expires_at": int(time.time()) + 900,
                "profile_id": profile_id or "p1", "model": "deepseek-chat",
                "base_url": "http://api:8000/v1/llm", "mcp_token": f"mcp-cap-{self.llm_calls}"}


class FakeProcess:
    """替身运行时：把一轮拆成「一条工具事件 + 一条回复」，并记下收到的输入。"""

    instances: list["FakeProcess"] = []

    def __init__(self, settings, **kwargs) -> None:
        self.kwargs = kwargs
        # 复用判断真读这两个字段（换 model 或换 base_url 就该换进程），替身得提供
        self.llm_base_url = kwargs.get("llm_base_url")
        self.model = kwargs.get("model")
        self.turns: list[str] = []
        self.stopped = False
        self.alive_flag = True
        FakeProcess.instances.append(self)

    @property
    def alive(self) -> bool:
        return self.alive_flag

    def holds_tokens(self, token: dict) -> bool:
        # 真实进程有这个判断（换凭据等于换进程），替身必须一起提供，否则复用路径测不到
        return (self.kwargs.get("llm_token") == token.get("token")
                and self.kwargs.get("mcp_token") == (token.get("mcp_token") or ""))

    def ensure_started(self) -> None:
        pass

    def run_turn(self, text, on_event) -> None:
        self.turns.append(text)
        on_event("tool_call", {"name": "mcp__kb__kb_list_items", "arguments": "{}"})
        on_event("assistant_message", {"text": f"收到：{text}"})
        on_event("turn_end", {"reason": "completed"})

    def stop(self) -> None:
        self.stopped = True
        self.alive_flag = False


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    """不调 mgr.start()：测试里手工推 _schedule_once / mirror_once / _reap_pass，
    后台线程跑起来会让断言变成等时间，而这里要验的是「状态层对不对」。"""
    FakeProcess.instances.clear()
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))
    settings = OrchestratorSettings(
        site="kb", kb_base_url="http://api.test",
        db_path=tmp_path / "agent.db", homes_root=tmp_path / "homes",
        relay_key_file=tmp_path / "relay.key", drop_privileges=False,
        admission_retry_seconds=1, reap_scan_seconds=1, idle_reap_seconds=1,
        mirror_interval_seconds=0.2,
    )
    store = Store(settings.db_path)
    kb = FakeKb()
    mgr = SessionManager(settings, store, _StubHomes(), kb)
    monkeypatch.setattr("orchestrator.sessions.DshSessionProcess", FakeProcess)
    yield {"mgr": mgr, "store": store, "kb": kb, "settings": settings}
    mgr.stop()
    store.close()


class _StubHomes:
    """替身家目录：`used_mib` 就是要喂给每人限额闸门的那个读数。"""

    def __init__(self, used_mib: float = 0.0) -> None:
        self.used_mib = used_mib
        self.purged: list[str] = []

    def ensure(self, user_id: str):
        from pathlib import Path as P

        return P(f"/tmp/{user_id}"), P(f"/tmp/{user_id}.work"), 9001

    def disk_usage_mib(self, user_id: str) -> float:
        return self.used_mib

    def purge_if_absent(self, user_id: str) -> None:
        # 限额路径只能拒绝起轮次，绝不能顺手删用户的家 —— 这条被调用就该看见
        self.purged.append(user_id)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_seq_is_monotonic_and_only_user_visible_kinds_land(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        session = store.create_session(site="kb", user_id="u", title="t")
        sid = session["session_id"]
        seqs = [store.append_event(site="kb", session_id=sid, kind=k, payload={"text": k})
                for k in ("user_message", "assistant_message", "tool_call",
                          "tool_result", "status", "error", "turn_end")]
        assert seqs == list(range(1, 8)), "seq 必须由本服务单调分配"
        assert store.events_after(session_id=sid, after_seq=3)[0]["seq"] == 4
        with pytest.raises(ValueError):
            store.append_event(site="kb", session_id=sid, kind="step/start", payload={})
    finally:
        store.close()


def test_turn_runs_and_mirrors_to_a_machine(manager):
    mgr, store, kb = manager["mgr"], manager["store"], manager["kb"]
    session = mgr.create_session(user_id="alice", title="第一个对话")
    sid = session["session_id"]
    assert kb.declared[-1]["session_id"] == sid, "新会话要立刻在 A 机登记，否则历史无处可写"

    mgr.submit(user_id="alice", session_id=sid, text="帮我看看今天的条目")
    mgr._schedule_once()
    assert wait_until(lambda: any("收到" in (e["payload"].get("text") or "")
                                  for e in store.events_after(session_id=sid, after_seq=0)))

    events = store.events_after(session_id=sid, after_seq=0)
    kinds = [e["kind"] for e in events]
    assert kinds == ["user_message", "tool_call", "assistant_message", "turn_end"],         "过程信息只能是真实事件，内部记账类（step/start 等）不进界面"
    assert [e["seq"] for e in events] == [1, 2, 3, 4]

    mgr.mirror_once()
    row = store.get_session(site="kb", session_id=sid)
    assert row["mirrored_seq"] == row["last_seq"], "回传确认后该能续上，不重复也不漏"
    assert kb.mirrored[-1]["user_id"] == "alice"


def test_admission_denial_queues_instead_of_starting(manager, monkeypatch):
    mgr, store = manager["mgr"], manager["store"]
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (False, "memory_low"))
    session = mgr.create_session(user_id="bob", title="忙时提交")
    sid = session["session_id"]
    mgr.submit(user_id="bob", session_id=sid, text="先排着")

    mgr._schedule_once()
    kinds = [e["kind"] for e in store.events_after(session_id=sid, after_seq=0)]
    assert "status" in kinds, "排队要有明确事件，不是静默失败也不是硬起进程"
    assert store.events_after(session_id=sid, after_seq=0)[1]["payload"]["text"] == STATUS_QUEUED
    assert not FakeProcess.instances, "准入不通过时不能起运行时进程"

    # 内存回来了，同一枚排队轮次照样跑掉，不需要用户重发
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))
    mgr._schedule_once()
    assert wait_until(lambda: any(e["kind"] == "assistant_message"
                                  for e in store.events_after(session_id=sid, after_seq=0)))


def test_over_home_quota_refuses_the_turn_without_deleting_history(manager, monkeypatch):
    """每人 `$DSH_HOME` 限额在这台机上是软件闸门：超了如实报错、这一轮不开始。

    同时钉住一条正确性要求：超限额**不是**清理时机，服务不能替用户删历史。
    """
    mgr, store = manager["mgr"], manager["store"]
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))
    homes = _StubHomes(used_mib=mgr.settings.home_quota_mib + 1)
    mgr.homes = homes
    session = mgr.create_session(user_id="gina", title="超限额")
    sid = session["session_id"]

    mgr.submit(user_id="gina", session_id=sid, text="这一轮不该开始")
    mgr._schedule_once()
    assert wait_until(lambda: any(e["kind"] == "error"
                                  for e in store.events_after(session_id=sid, after_seq=0)))
    assert not FakeProcess.instances, "超限额时不能起运行时"
    assert homes.purged == [], "超限额不是删用户的家"
    messages = [e["payload"].get("message", "") for e in
                store.events_after(session_id=sid, after_seq=0) if e["kind"] == "error"]
    assert any("限额" in m for m in messages), f"要如实说明为什么没开始：{messages}"


def test_disk_floor_queues_by_free_space(monkeypatch, tmp_path):
    """整机可用空间是这台机上真正防写满盘的那道闸：不够就排队（可重试，不丢消息）。"""
    monkeypatch.setattr(admission, "host_available_mib", lambda: 1024.0)
    monkeypatch.setattr(admission, "cgroup_remaining_mib", lambda: None)
    monkeypatch.setattr(admission, "disk_free_mib", lambda path: 500.0)
    assert admission.may_start(384, min_free_disk_mib=1024,
                               disk_path=tmp_path) == (False, "disk_low")
    monkeypatch.setattr(admission, "disk_free_mib", lambda path: 4096.0)
    assert admission.may_start(384, min_free_disk_mib=1024, disk_path=tmp_path) == (True, "")
    # 不设这一项时仍是原来的纯内存准入（别的机器/本地开发不必配磁盘闸门）
    assert admission.may_start(384) == (True, "")


def test_disk_free_mib_falls_back_to_existing_ancestor(tmp_path):
    """家目录还没建出来时也要量得到：退到最近的已存在祖先，而不是当成零放行。"""
    assert admission.disk_free_mib(tmp_path / "homes" / "kb" / "u") > 0


def test_one_process_per_user_and_no_auto_retry_of_a_turn(manager, monkeypatch):
    mgr, store = manager["mgr"], manager["store"]
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))
    session = mgr.create_session(user_id="carol", title="两轮")
    sid = session["session_id"]
    mgr.submit(user_id="carol", session_id=sid, text="第一轮")
    mgr._schedule_once()
    assert wait_until(lambda: len(FakeProcess.instances) == 1)
    assert wait_until(lambda: FakeProcess.instances[0].turns == ["第一轮"])
    mgr.submit(user_id="carol", session_id=sid, text="第二轮")
    mgr._schedule_once()
    assert wait_until(lambda: FakeProcess.instances[0].turns == ["第一轮", "第二轮"])
    assert len(FakeProcess.instances) == 1, "同一用户同一会话该复用一个长驻进程，不是每轮起一个"

    # 一轮跑完不重放：替身里没有第三次调用，token 也不会被反复要
    assert FakeProcess.instances[0].turns == ["第一轮", "第二轮"]


def test_token_change_recycles_the_process(manager, monkeypatch):
    """能力凭据换新等于换进程：环境变量是启动时读一次的静态值，
    拿旧凭据撑着的进程下一轮调工具就会失败（dsh 侧实测撤销即启动失败）。"""
    mgr, store = manager["mgr"], manager["store"]
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))
    session = mgr.create_session(user_id="frank", title="换凭据")
    sid = session["session_id"]
    mgr.submit(user_id="frank", session_id=sid, text="第一轮")
    mgr._schedule_once()
    assert wait_until(lambda: FakeProcess.instances and FakeProcess.instances[0].turns)
    first = FakeProcess.instances[0]
    assert first.kwargs["mcp_token"].startswith("mcp-cap-")

    # 让缓存的凭据过期，逼调度重新去 A 机要一份
    with store._lock:
        store._db.execute("UPDATE tokens SET expires_at = 0 WHERE user_id = ?", ("frank",))
    mgr.submit(user_id="frank", session_id=sid, text="第二轮")
    mgr._schedule_once()
    assert wait_until(lambda: len(FakeProcess.instances) >= 2)
    assert first.stopped, "旧凭据的进程该被换掉，而不是继续撑着"
    assert FakeProcess.instances[-1].kwargs["mcp_token"] != first.kwargs["mcp_token"]


def test_failing_turn_is_reported_and_not_retried(manager, monkeypatch):
    mgr, store = manager["mgr"], manager["store"]
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))

    class Boom(FakeProcess):
        def run_turn(self, text, on_event) -> None:
            self.turns.append(text)
            from orchestrator.dsh_runtime import DshTurnError

            raise DshTurnError("运行时中断了，这一轮的结果不确定；下一轮会自动重开")

    monkeypatch.setattr("orchestrator.sessions.DshSessionProcess", Boom)
    session = mgr.create_session(user_id="dave", title="失败轮")
    sid = session["session_id"]
    mgr.submit(user_id="dave", session_id=sid, text="这一轮会断")
    mgr._schedule_once()
    assert wait_until(lambda: any(e["kind"] == "error"
                                  for e in store.events_after(session_id=sid, after_seq=0)))
    kinds = [e["kind"] for e in store.events_after(session_id=sid, after_seq=0)]
    # 中断后如实收尾，不自动重发在途轮次（与 A 机 ProviderOutcomeUnknown 同一原则）
    assert kinds.count("user_message") == 1
    assert kinds[-2:] == ["error", "turn_end"]


def test_reaper_only_reaps_when_no_turn_in_flight(manager, monkeypatch):
    mgr, store = manager["mgr"], manager["store"]
    monkeypatch.setattr(admission, "may_start", lambda min_mib, **kw: (True, ""))
    session = mgr.create_session(user_id="erin", title="回收")
    sid = session["session_id"]
    mgr.submit(user_id="erin", session_id=sid, text="跑一轮")
    mgr._schedule_once()
    assert wait_until(lambda: FakeProcess.instances and FakeProcess.instances[0].turns)

    # 再排一条并让它处于在途：这时不能回收，否则会把正在跑的那轮掐掉
    in_flight = store.add_turn(site="kb", session_id=sid, text="在途的一条")
    store.claim_turn(session_id=sid)
    assert store.has_running_turn(sid)
    mgr._reap_pass(idle_seconds=-1)
    assert not FakeProcess.instances[0].stopped, "还有在途轮次时不能回收进程"

    store.finish_turn(turn_id=in_flight, state="succeeded")
    mgr._reap_pass(idle_seconds=-1)
    assert wait_until(lambda: FakeProcess.instances[0].stopped)
    assert store.get_session(site="kb", session_id=sid)["state"] == "open", "回收进程不关会话"


def test_sessions_are_isolated_by_user_and_site(tmp_path):
    """跨用户与跨站点都看不见彼此：这是结构性隔离，不是查询时顺手过滤一下。"""
    store = Store(tmp_path / "iso.db")
    try:
        kb_session = store.create_session(site="kb", user_id="u", title="知识库的")
        ledger = store.create_session(site="ledger", user_id="u", title="记账的")
        assert [s["session_id"] for s in store.list_sessions(site="kb", user_id="u")] == \
            [kb_session["session_id"]]
        assert [s["session_id"] for s in store.list_sessions(site="ledger", user_id="u")] == \
            [ledger["session_id"]]
        assert store.list_sessions(site="kb", user_id="other") == []
    finally:
        store.close()


def test_restart_marks_inflight_turns_interrupted(tmp_path):
    """容器重启：running 轮次如实标 interrupted 并写一条 error 事件，不假装成功。"""
    store = Store(tmp_path / "restart.db")
    try:
        sid = store.create_session(site="kb", user_id="u", title="t")["session_id"]
        turn = store.add_turn(site="kb", session_id=sid, text="在途")
        store.claim_turn(session_id=sid)
        interrupted = store.interrupted_running_turns()
        assert [t["turn_id"] for t in interrupted] == [turn]
        assert store.claim_turn(session_id=sid) is None
    finally:
        store.close()


def test_container_credentials_are_site_scoped(tmp_path):
    """容器只认**本站点**签的中继凭据：同一把派生密钥签出来的别站凭据也要 401。

    2026-10-06 第一次在生产上实测时这条是漏的——原先只在调用方带了 `X-Agent-Site`
    请求头时才比对，而没有任何调用方带这个头，用 ledger 站点签一枚打过来就是 200。
    """
    from fastapi.testclient import TestClient

    from orchestrator.main import create_app
    from orchestrator.relaykey import bearer, encode_key, issue

    signing = bytes(range(32))
    key_file = tmp_path / "relay.key"
    key_file.write_text(encode_key(signing), encoding="ascii")
    settings = OrchestratorSettings(site="kb", kb_base_url="http://api.test",
                                    db_path=tmp_path / "agent.db",
                                    homes_root=tmp_path / "homes",
                                    relay_key_file=key_file, drop_privileges=False)
    app, _mgr = create_app(settings)
    with TestClient(app) as client:
        mine, _ = issue(signing, site="kb", user_id="u1", subject="user", ttl_seconds=60)
        assert client.get("/agent/sessions", headers=bearer(mine)).status_code == 200
        other, _ = issue(signing, site="ledger", user_id="u1", subject="user", ttl_seconds=60)
        assert client.get("/agent/sessions", headers=bearer(other)).status_code == 401, \
            "跨站点凭据必须拒——站点隔离不能只写在文档里"
        ingest, _ = issue(signing, site="kb", user_id="u1", subject="ingest", ttl_seconds=60)
        assert client.get("/agent/sessions", headers=bearer(ingest)).status_code == 401, \
            "回传凭据不能当用户凭据用"
        assert client.get("/agent/sessions").status_code == 401


def test_manager_start_survives_restart(tmp_path, monkeypatch):
    """启动时把上一份进程留下的在途轮次收尾，并补一条用户看得见的事件。"""
    store = Store(tmp_path / "boot.db")
    sid = store.create_session(site="kb", user_id="u", title="t")["session_id"]
    store.add_turn(site="kb", session_id=sid, text="在途")
    store.claim_turn(session_id=sid)
    settings = OrchestratorSettings(site="kb", kb_base_url="http://api.test",
                                    db_path=tmp_path / "boot.db",
                                    homes_root=tmp_path / "homes",
                                    relay_key_file=tmp_path / "relay.key",
                                    drop_privileges=False)
    mgr = SessionManager(settings, store, _StubHomes(), FakeKb())
    monkeypatch.setattr("orchestrator.sessions.DshSessionProcess", FakeProcess)
    try:
        mgr.start()
        assert wait_until(lambda: any(e["kind"] == "error"
                                      for e in store.events_after(session_id=sid, after_seq=0)))
        thread_count = threading.active_count()
        assert thread_count > 1
    finally:
        mgr.stop()
        store.close()
