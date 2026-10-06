"""编排服务自己的状态库（容器内 `agent.db`，独立小卷）。

**绝不碰 A 机的 SQLite**：那是单写端，跨容器写会破坏既有正确性前提（docs/01 ADR-001）。
这里只存编排层需要的东西：会话、在途轮次、事件序列与「已回传到哪里」。

事件先落表再发 SSE，不用内存队列：断线可以按 seq 续传，容器重启不丢，
回传失败可以按 mirrored_seq 重发 —— 这三件事内存队列都做不到。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

EVENT_KINDS = ("user_message", "assistant_message", "tool_call", "tool_result",
               "status", "error", "turn_end")

_SESSION_COLUMNS = ("site", "session_id", "user_id", "dsh_session_id", "title", "state",
                    "last_seq", "mirrored_seq", "created_at", "updated_at")


class Store:
    """单连接 + 一把锁。

    容器里并发就是个位数，`check_same_thread=False` 加显式锁比每个线程一条连接
    更好推理，也天然把 `last_seq` 的分配串行化了。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._create()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _create(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                  site TEXT NOT NULL, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
                  dsh_session_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL DEFAULT 'open', last_seq INTEGER NOT NULL DEFAULT 0,
                  mirrored_seq INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                  PRIMARY KEY (site, session_id)
                );
                CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions (site, user_id, updated_at);

                CREATE TABLE IF NOT EXISTS events (
                  site TEXT NOT NULL, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                  kind TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
                  PRIMARY KEY (session_id, seq)
                );

                CREATE TABLE IF NOT EXISTS turns (
                  turn_id TEXT NOT NULL, site TEXT NOT NULL, session_id TEXT NOT NULL,
                  text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                  created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER,
                  PRIMARY KEY (turn_id)
                );
                CREATE INDEX IF NOT EXISTS ix_turns_session ON turns (site, session_id, created_at);

                CREATE TABLE IF NOT EXISTS tokens (
                  site TEXT NOT NULL, user_id TEXT NOT NULL, token TEXT NOT NULL,
                  expires_at INTEGER NOT NULL, profile_id TEXT NOT NULL DEFAULT '',
                  model TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
                  mcp_token TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY (site, user_id)
                );
                """
            )

    # ---- 会话 ----

    def create_session(self, *, site: str, user_id: str, title: str) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        dsh_session_id = f"kb-{site}-{session_id[:16]}"
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 'open', 0, 0, ?, ?)",
                (site, session_id, user_id, dsh_session_id, title[:200], now, now),
            )
        return {"session_id": session_id, "title": title[:200], "last_seq": 0,
                "state": "open", "created_at": now}

    def get_session(self, *, site: str, session_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE site = ? AND session_id = ?", (site, session_id)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, *, site: str, user_id: str) -> list[dict]:
        """强制带 site + user_id：隔离是结构性的，不是可选过滤。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE site = ? AND user_id = ? "
                "AND state != 'deleted' ORDER BY updated_at DESC",
                (site, user_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def rename_session(self, *, site: str, user_id: str, session_id: str, title: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE site = ? AND session_id = ? "
                "AND user_id = ?",
                (title[:200], int(time.time()), site, session_id, user_id),
            )
        return cur.rowcount > 0

    def set_session_state(self, *, site: str, session_id: str, state: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE site = ? AND session_id = ?",
                (state, int(time.time()), site, session_id),
            )

    def sessions_to_reap(self, idle_seconds: int) -> list[dict]:
        """候选回收会话（运行中且已空闲到位）。真正的回收还要看有没有在途轮次。"""
        cutoff = int(time.time()) - idle_seconds
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE state = 'running' AND updated_at < ?", (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 事件 ----

    def append_event(self, *, site: str, session_id: str, kind: str,
                     payload: dict[str, Any]) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知事件类型：{kind}")
        with self._lock:
            row = self._db.execute(
                "UPDATE sessions SET last_seq = last_seq + 1, updated_at = ? "
                "WHERE site = ? AND session_id = ? RETURNING last_seq",
                (int(time.time()), site, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"会话不存在：{session_id}")
            seq = int(row["last_seq"])
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (site, session_id, seq, kind, json.dumps(payload, ensure_ascii=False),
                 int(time.time())),
            )
        return seq

    def events_after(self, *, session_id: str, after_seq: int, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, kind, payload, created_at FROM events "
                "WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (session_id, after_seq, limit),
            ).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    def pending_mirror(self, *, limit: int = 200) -> list[dict]:
        """还没回传给 A 机的事件，按会话分组返回。"""
        out: list[dict] = []
        with self._lock:
            rows = self._db.execute(
                "SELECT s.site, s.session_id, s.user_id, s.mirrored_seq, s.last_seq "
                "FROM sessions s WHERE s.mirrored_seq < s.last_seq ORDER BY s.updated_at"
            ).fetchall()
            for r in rows[:limit]:
                events = self._db.execute(
                    "SELECT seq, kind, payload FROM events WHERE session_id = ? AND seq > ? "
                    "ORDER BY seq LIMIT 200",
                    (r["session_id"], r["mirrored_seq"]),
                ).fetchall()
                if events:
                    out.append({"site": r["site"], "session_id": r["session_id"],
                                "user_id": r["user_id"],
                                "events": [{"seq": e["seq"], "kind": e["kind"],
                                            "payload": json.loads(e["payload"])} for e in events]})
        return out

    def mark_mirrored(self, *, session_id: str, through_seq: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET mirrored_seq = MAX(mirrored_seq, ?) "
                "WHERE session_id = ? AND mirrored_seq < ?",
                (through_seq, session_id, through_seq),
            )

    # ---- 轮次 ----

    def add_turn(self, *, site: str, session_id: str, text: str) -> str:
        turn_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL)",
                             (turn_id, site, session_id, text, int(time.time())))
        return turn_id

    def claim_turn(self, *, session_id: str) -> dict | None:
        """取该会话最早的一条排队轮次并标记为 running（一个会话一次只跑一轮）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM turns WHERE session_id = ? AND state = 'queued' "
                "ORDER BY created_at LIMIT 1", (session_id,)
            ).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE turns SET state = 'running', started_at = ? WHERE turn_id = ?",
                             (int(time.time()), row["turn_id"]))
        return dict(row)

    def finish_turn(self, *, turn_id: str, state: str) -> None:
        with self._lock:
            self._db.execute("UPDATE turns SET state = ?, finished_at = ? WHERE turn_id = ?",
                             (state, int(time.time()), turn_id))

    def has_finished_turn(self, session_id: str) -> bool:
        """以前有没有跑完过一轮：决定重启进程时要不要试 session/resume。"""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM turns WHERE session_id = ? AND state = 'succeeded' LIMIT 1",
                (session_id,),
            ).fetchone()
        return row is not None

    def has_running_turn(self, session_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM turns WHERE session_id = ? AND state = 'running' LIMIT 1",
                (session_id,),
            ).fetchone()
        return row is not None

    def queued_turns(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.*, s.user_id FROM turns t JOIN sessions s "
                "ON s.session_id = t.session_id WHERE t.state = 'queued' ORDER BY t.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def interrupted_running_turns(self) -> list[dict]:
        """容器重启：上一份进程留下的 running 轮次一律如实标 interrupted。"""
        with self._lock:
            rows = self._db.execute("SELECT * FROM turns WHERE state = 'running'").fetchall()
            for r in rows:
                self._db.execute("UPDATE turns SET state = 'interrupted', finished_at = ? "
                                 "WHERE turn_id = ?", (int(time.time()), r["turn_id"]))
        return [dict(r) for r in rows]

    # ---- 会话 token 缓存 ----

    def put_token(self, *, site: str, user_id: str, token: str, expires_at: int,
                  profile_id: str, model: str, base_url: str, mcp_token: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(site, user_id) DO UPDATE SET token = excluded.token, "
                "expires_at = excluded.expires_at, profile_id = excluded.profile_id, "
                "model = excluded.model, base_url = excluded.base_url, "
                "mcp_token = excluded.mcp_token",
                (site, user_id, token, expires_at, profile_id, model, base_url, mcp_token),
            )

    def get_token(self, *, site: str, user_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tokens WHERE site = ? AND user_id = ?",
                                   (site, user_id)).fetchone()
        if row is None:
            return None
        return dict(row)
