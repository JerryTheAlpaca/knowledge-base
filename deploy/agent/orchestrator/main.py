"""agent 编排服务（跑在 agent 容器里，与 A 机同栈不同进程）。

对外只有这五个接口，全部要求 A 机签发的中继凭据；`site` 与 `user_id` **只从凭据取**，
请求体里给了也不算（结构性站点隔离在身份层的落点）。

```
POST   /agent/sessions                      新建会话
GET    /agent/sessions                      本用户本站点的会话列表
PUT    /agent/sessions/{id}                 重命名
DELETE /agent/sessions/{id}                 关闭会话（结束进程，不删 $DSH_HOME）
POST   /agent/sessions/{id}/messages        发一轮，立即返回 turn_id
GET    /agent/sessions/{id}/events?after=N  SSE：从事件表按 seq 追
```

SSE 走表而不是内存队列：断线可以按 seq 续传，容器重启不丢，A 机与前端读到的
都是同一份已落库的事件。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .admission import may_start
from .config import OrchestratorSettings
from .homes import Homes
from .kbclient import KbClient
from .relaykey import SUB_USER, read
from .sessions import SessionManager
from .store import Store

# SSE 轮询事件表的间隔：容器里就几个会话，0.4s 的延迟换「断线可续 + 重启不丢」值得
SSE_POLL_SECONDS = 0.4
SSE_HEARTBEAT_EVENTS = 50


def create_app(settings: OrchestratorSettings | None = None) -> tuple[FastAPI, SessionManager]:
    settings = settings or OrchestratorSettings()
    store = Store(settings.db_path)
    homes = Homes(settings, Path(__file__).resolve().parent.parent)
    manager = SessionManager(settings, store, homes, KbClient(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(title="kb agent orchestrator", lifespan=lifespan)

    def relay_claims(request: Request) -> dict[str, Any]:
        """每个 /agent/* 都要过的认证：site 与 user_id 只从 A 机签发的凭据取。

        请求体或查询串里出现的 site/user_id 一律不看 —— 这是结构性隔离，
        不是「先信再过滤」。
        """
        authorization = request.headers.get("Authorization") or ""
        if not authorization.startswith("Bearer "):
            return _reject("缺少中继凭据")
        claims = read(settings.relay_key(), authorization[7:].strip(), expect_subject=SUB_USER)
        if claims is None:
            return _reject("中继凭据无效或已过期")
        # 容器自己的身份就是 settings.site（A 机侧硬编码签同一个值），所以这里直接比对，
        # 不看请求头：2026-10-06 线上实测，原先只在校验方带了 X-Agent-Site 时才比，
        # 而没有任何调用方带这个头 —— 别的站点签的凭据照样能进来。
        if claims.get("site") != settings.site:
            return _reject("凭据站点不符")
        return claims

    @app.get("/agent/health")
    def health() -> dict[str, Any]:
        allowed, reason = may_start(settings.admission_min_available_mib,
                                    min_free_disk_mib=settings.min_free_disk_mib,
                                    disk_path=settings.homes_root)
        return {"ok": True, "site": settings.site, "admission_allowed": allowed,
                "admission_reason": reason}

    @app.post("/agent/sessions", status_code=201)
    def create_session(body: dict, claims: dict = Depends(relay_claims)) -> dict[str, Any]:
        title = str((body or {}).get("title") or "")[:200]
        return manager.create_session(user_id=claims["user_id"], title=title)

    @app.get("/agent/sessions")
    def list_sessions(claims: dict = Depends(relay_claims)) -> dict[str, Any]:
        return {"sessions": manager.list_sessions(user_id=claims["user_id"])}

    @app.put("/agent/sessions/{session_id}")
    def rename_session(session_id: str, body: dict,
                       claims: dict = Depends(relay_claims)) -> JSONResponse:
        ok = manager.rename(user_id=claims["user_id"], session_id=session_id,
                            title=str((body or {}).get("title") or "")[:200])
        return JSONResponse({"session_id": session_id, "renamed": ok},
                            status_code=200 if ok else 404)

    @app.delete("/agent/sessions/{session_id}")
    def close_session(session_id: str, claims: dict = Depends(relay_claims)) -> JSONResponse:
        ok = manager.close(user_id=claims["user_id"], session_id=session_id)
        return JSONResponse({"session_id": session_id, "closed": ok},
                            status_code=200 if ok else 404)

    @app.post("/agent/sessions/{session_id}/messages", status_code=202)
    def send_message(session_id: str, body: dict,
                     claims: dict = Depends(relay_claims)) -> JSONResponse:
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return JSONResponse({"error": {"code": "SCHEMA_INVALID", "message": "text 不能为空"}},
                                status_code=422)
        try:
            return JSONResponse(manager.submit(user_id=claims["user_id"], session_id=session_id,
                                               text=text), status_code=202)
        except KeyError:
            return JSONResponse({"error": {"code": "NOT_FOUND", "message": "会话不存在"}},
                                status_code=404)

    @app.get("/agent/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, after: int = 0,
                     claims: dict = Depends(relay_claims)) -> StreamingResponse:
        session = store.get_session(site=settings.site, session_id=session_id)
        if session is None or session["user_id"] != claims["user_id"]:
            return JSONResponse({"error": {"code": "NOT_FOUND", "message": "会话不存在"}},
                                status_code=404)

        async def stream():
            cursor = max(0, int(after))
            idle_polls = 0
            while not await request.is_disconnected():
                rows = store.events_after(session_id=session_id, after_seq=cursor)
                if rows:
                    idle_polls = 0
                    for row in rows:
                        cursor = row["seq"]
                        yield _sse(row)
                else:
                    idle_polls += 1
                    # 心跳注释行：让中间代理知道这条流还活着（Caddy 侧要 flush_interval -1）
                    if idle_polls >= SSE_HEARTBEAT_EVENTS:
                        idle_polls = 0
                        yield b": ping\n\n"
                await asyncio.sleep(SSE_POLL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return app, manager


def _reject(message: str):
    from fastapi import HTTPException

    raise HTTPException(status_code=401, detail={"code": "AUTH_EXPIRED", "message": message})


def _sse(row: dict[str, Any]) -> bytes:
    data = json.dumps({"seq": row["seq"], "kind": row["kind"], "payload": row["payload"],
                       "created_at": row["created_at"]}, ensure_ascii=False)
    return f"id: {row['seq']}\nevent: agent\ndata: {data}\n\n".encode("utf-8")


app = None  # uvicorn 入口：`python -m orchestrator.main` 会把它建出来


def run() -> None:
    global app
    import os

    import uvicorn

    app, _manager = create_app()
    # 绑 0.0.0.0 不等于暴露：这个容器只在 internal 网络上（没有网关与 NAT），
    # 也没有发布任何宿主端口。绑 127.0.0.1 反而让同网的 api 连不到它
    # —— 2026-10-06 线上第一次启用就是这么起不来又看不出来的。
    uvicorn.run(app, host=os.environ.get("AGENT_BIND", "0.0.0.0"),
                port=int(os.environ.get("AGENT_PORT", "8100")))


if __name__ == "__main__":
    run()
