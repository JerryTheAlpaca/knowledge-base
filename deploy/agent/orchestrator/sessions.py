"""会话调度：准入、进程、轮次队列、回收与回传。

一个后台调度线程按 `AGENT_ADMISSION_RETRY_SECONDS` 的节奏看一遍排队轮次：
内存够就起进程跑，不够就发一条 `status` 事件说「服务器忙，已排队」——
不是静默失败，也不是硬起把同机的 api / worker / share_runner 挤死。

每个会话一次只跑一轮（`claim_turn` 保证），一个会话一个工作线程；跨会话才并行，
而跨会话的并发数由上面那道内存准入决定，不是一个写死的常量。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import admission
from .config import OrchestratorSettings
from .dsh_runtime import DshSessionProcess, DshTurnError
from .homes import Homes
from .kbclient import KbClient
from .store import Store

# token 剩下不到这个时间就先换一枚，避免在一轮跑到一半时过期
TOKEN_REFRESH_MARGIN_SECONDS = 120
STATUS_QUEUED = "服务器忙，已排队"


class SessionManager:
    def __init__(self, settings: OrchestratorSettings, store: Store, homes: Homes,
                 kb: KbClient) -> None:
        self.settings = settings
        self.store = store
        self.homes = homes
        self.kb = kb
        self._procs: dict[str, DshSessionProcess] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()

    # ---- 对外动作 ----

    def create_session(self, *, user_id: str, title: str) -> dict[str, Any]:
        session = self.store.create_session(site=self.settings.site, user_id=user_id,
                                            title=title or "")
        self.kb.declare_session(user_id=user_id, session_id=session["session_id"],
                                title=session["title"])
        return session

    def list_sessions(self, *, user_id: str) -> list[dict[str, Any]]:
        return self.store.list_sessions(site=self.settings.site, user_id=user_id)

    def rename(self, *, user_id: str, session_id: str, title: str) -> bool:
        return self.store.rename_session(site=self.settings.site, user_id=user_id,
                                         session_id=session_id, title=title)

    def submit(self, *, user_id: str, session_id: str, text: str) -> dict[str, Any]:
        """收下这一轮：先落一条 `user_message` 事件，再交给调度线程。"""
        session = self.store.get_session(site=self.settings.site, session_id=session_id)
        if session is None or session["user_id"] != user_id:
            raise KeyError(session_id)
        self.store.append_event(site=self.settings.site, session_id=session_id,
                                kind="user_message", payload={"text": text})
        turn_id = self.store.add_turn(site=self.settings.site, session_id=session_id, text=text)
        self._wake.set()
        return {"turn_id": turn_id, "accepted": True}

    def close(self, *, user_id: str, session_id: str) -> bool:
        session = self.store.get_session(site=self.settings.site, session_id=session_id)
        if session is None or session["user_id"] != user_id:
            return False
        self.store.set_session_state(site=self.settings.site, session_id=session_id,
                                     state="closed")
        # 只结束进程：$DSH_HOME 留着，用户以后还能从历史里接着开
        self._stop_process(session_id)
        return True

    # ---- 后台循环 ----

    def start(self) -> None:
        # 容器重启：上一份进程留下的 running 轮次一律如实标 interrupted，不假装成功
        for turn in self.store.interrupted_running_turns():
            self.store.append_event(site=self.settings.site, session_id=turn["session_id"],
                                    kind="error",
                                    payload={"message": "服务重启打断了这一轮，服务器没有替你重试"})
        for target, name in ((self._schedule_loop, "scheduler"), (self._mirror_loop, "mirror"),
                             (self._reap_loop, "reaper")):
            thread = threading.Thread(target=target, name=f"agent-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        for session_id in list(self._procs):
            self._stop_process(session_id)

    def _schedule_loop(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(timeout=self.settings.admission_retry_seconds)
            self._wake.clear()
            if self._stopping.is_set():
                return
            self._schedule_once()

    def _schedule_once(self) -> None:
        allowed, reason = admission.may_start(self.settings.admission_min_available_mib)
        for turn in self.store.queued_turns():
            session_id = turn["session_id"]
            with self._lock:
                if session_id in self._busy:
                    continue  # 这个会话已经有一个工作线程在跑它的队列了
                self._busy.add(session_id)
            if not allowed:
                # 如实说在排队，而不是转圈或报错；原因留在日志里给运维看
                self.store.append_event(site=self.settings.site, session_id=session_id,
                                        kind="status",
                                        payload={"text": STATUS_QUEUED, "reason": reason})
                with self._lock:
                    self._busy.discard(session_id)
                continue
            threading.Thread(target=self._drain_session, args=(session_id,),
                             name=f"agent-turn-{session_id[:8]}", daemon=True).start()

    def _drain_session(self, session_id: str) -> None:
        try:
            while True:
                turn = self.store.claim_turn(session_id=session_id)
                if turn is None:
                    return
                self._run_turn(session_id, turn)
        finally:
            with self._lock:
                self._busy.discard(session_id)

    def _run_turn(self, session_id: str, turn: dict[str, Any]) -> None:
        session = self.store.get_session(site=self.settings.site, session_id=session_id)
        if session is None or session["state"] == "closed":
            self.store.finish_turn(turn_id=turn["turn_id"], state="cancelled")
            return
        user_id = session["user_id"]
        token = self._token_for(user_id)
        if token is None:
            self._fail_turn(session_id, turn, "还没有可用的模型配置，或 A 机暂时联系不上")
            return
        try:
            proc = self._process_for(session=session, token=token)
        except Exception as exc:  # noqa: BLE001 —— 起进程失败要如实落到这一轮上
            self._fail_turn(session_id, turn, f"运行时启动失败（{type(exc).__name__}）")
            print(f"[agent] 启动失败 session={session_id[:8]} {type(exc).__name__}: {exc}")
            return

        def on_event(kind: str, payload: dict) -> None:
            self.store.append_event(site=self.settings.site, session_id=session_id,
                                    kind=kind, payload=payload)

        try:
            proc.run_turn(turn["text"], on_event)
        except DshTurnError as exc:
            self._fail_turn(session_id, turn, str(exc))
            return
        self.store.finish_turn(turn_id=turn["turn_id"], state="succeeded")

    def _fail_turn(self, session_id: str, turn: dict[str, Any], message: str) -> None:
        self.store.append_event(site=self.settings.site, session_id=session_id, kind="error",
                               payload={"message": message})
        self.store.append_event(site=self.settings.site, session_id=session_id, kind="turn_end",
                               payload={"reason": "error"})
        self.store.finish_turn(turn_id=turn["turn_id"], state="failed")

    # ---- 进程与 token ----

    def _token_for(self, user_id: str) -> dict[str, Any] | None:
        cached = self.store.get_token(site=self.settings.site, user_id=user_id)
        if cached and cached["expires_at"] - int(time.time()) > TOKEN_REFRESH_MARGIN_SECONDS:
            return cached
        fresh = self.kb.llm_token(user_id)
        if fresh is None:
            return cached  # A 机暂时联系不上：能续用就续用，不能再由调用方判失败
        self.store.put_token(site=self.settings.site, user_id=user_id, token=fresh["token"],
                             expires_at=int(fresh["expires_at"]), profile_id=fresh["profile_id"],
                             model=fresh["model"], base_url=fresh["base_url"],
                             mcp_token=fresh.get("mcp_token") or "")
        return fresh

    def _process_for(self, *, session: dict[str, Any], token: dict[str, Any]) -> DshSessionProcess:
        session_id = session["session_id"]
        with self._lock:
            proc = self._procs.get(session_id)
        base_url = token.get("base_url") or self.settings.llm_base_url
        if proc is not None and proc.alive and proc.llm_base_url == base_url \
                and proc.model == token["model"] and proc.holds_tokens(token):
            return proc
        if proc is not None:
            self._stop_process(session_id)
        home, workspace, uid = self.homes.ensure(session["user_id"])
        new = DshSessionProcess(self.settings, home=home, workspace=workspace, uid=uid,
                                mcp_token=token.get("mcp_token") or "",
                                llm_token=token["token"], model=token["model"],
                                llm_base_url=base_url, dsh_session_id=session["dsh_session_id"],
                                resumable=self.store.has_finished_turn(session_id))
        with self._lock:
            self._procs[session_id] = new
        new.ensure_started()
        self.store.set_session_state(site=self.settings.site, session_id=session_id,
                                     state="running")
        return new

    def _stop_process(self, session_id: str) -> None:
        with self._lock:
            proc = self._procs.pop(session_id, None)
        if proc is not None:
            proc.stop()

    # ---- 回收与回传 ----

    def _reap_loop(self) -> None:
        while not self._stopping.wait(self.settings.reap_scan_seconds):
            self._reap_pass()

    def _reap_pass(self, *, idle_seconds: int | None = None) -> int:
        """回收空闲进程。**只有在途轮次为零时才回收**，否则会把正在跑的那轮掐掉。

        `idle_seconds` 默认取配置值；显式传值是给测试和运维手工触发用的
        （不然要等一个真实的空闲周期才能验收到回收路径）。
        """
        limit = self.settings.idle_reap_seconds if idle_seconds is None else idle_seconds
        reaped = 0
        for session in self.store.sessions_to_reap(limit):
            if self.store.has_running_turn(session["session_id"]):
                continue
            self._stop_process(session["session_id"])
            # 进程回收不等于会话关闭：状态回到 open，用户下一条消息会重新拉起
            self.store.set_session_state(site=self.settings.site,
                                         session_id=session["session_id"], state="open")
            reaped += 1
        return reaped

    def _mirror_loop(self) -> None:
        while not self._stopping.wait(self.settings.mirror_interval_seconds):
            self.mirror_once()

    def mirror_once(self) -> int:
        sent = 0
        for batch in self.store.pending_mirror(limit=self.settings.mirror_batch_events):
            if self.kb.mirror(user_id=batch["user_id"], session_id=batch["session_id"],
                              events=batch["events"]):
                self.store.mark_mirrored(session_id=batch["session_id"],
                                         through_seq=batch["events"][-1]["seq"])
                sent += len(batch["events"])
        return sent
