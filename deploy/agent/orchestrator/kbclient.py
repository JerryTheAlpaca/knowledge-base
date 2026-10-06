"""容器侧的 A 机客户端：签回传凭据、要会话 token、批量回传事件。

容器**不直连 A 机数据库**，所有写入都走 api 容器的 HTTP 接口，SQLite 单写端这个
既有前提才不被破坏。回传凭据由容器自己用派生后的用途密钥签出（它拿不到 master_key）。

失败处理保持克制：回传失败就留着下次重发（表里按 `mirrored_seq` 记进度），
不去做退避重试框架 —— A 机不可达时用户界面照样能看能问，只是「同步中」。
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import OrchestratorSettings
from .relaykey import bearer, issue

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
SUB_INGEST = "ingest"


class KbClient:
    def __init__(self, settings: OrchestratorSettings) -> None:
        self.settings = settings
        self._key = settings.relay_key()

    def _auth(self, user_id: str) -> dict[str, str]:
        token, _ = issue(self._key, site=self.settings.site, user_id=user_id,
                         subject=SUB_INGEST, ttl_seconds=self.settings.relay_ttl_seconds)
        return bearer(token)

    def llm_token(self, user_id: str, *, profile_id: str | None = None) -> dict[str, Any] | None:
        """要一枚该用户的 LLM 代理会话 token（profile 由 A 机按网页对话同一规则选）。"""
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(f"{self.settings.kb_base_url}/v1/agent/internal/llm-token",
                                   json={"profile_id": profile_id}, headers=self._auth(user_id))
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        return resp.json()

    def mirror(self, *, user_id: str, session_id: str, events: list[dict]) -> bool:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(f"{self.settings.kb_base_url}/v1/agent/internal/events",
                                   json={"session_id": session_id, "events": events},
                                   headers=self._auth(user_id))
        except httpx.HTTPError:
            return False
        return resp.status_code < 400

    def declare_session(self, *, user_id: str, session_id: str, title: str) -> bool:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(f"{self.settings.kb_base_url}/v1/agent/internal/sessions",
                                   json={"session_id": session_id, "title": title},
                                   headers=self._auth(user_id))
        except httpx.HTTPError:
            return False
        return resp.status_code < 400
