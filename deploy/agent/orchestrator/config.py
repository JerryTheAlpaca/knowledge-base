"""编排服务配置（docs/27 §Phase 3）。

只读环境变量，不引 pydantic-settings：这个容器里配置项一只手数得过来，
而且都必须在部署清单里看得见。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "yes")


@dataclass(frozen=True)
class OrchestratorSettings:
    site: str = os.environ.get("AGENT_SITE", "kb")
    # A 机的对外地址：MCP 与 LLM 代理都从这里拼
    kb_base_url: str = os.environ.get("KB_BASE_URL", "https://kb.jerrythealpaca.cn").rstrip("/")
    db_path: Path = Path(os.environ.get("AGENT_DB", "/srv/agent-state/agent.db"))
    homes_root: Path = Path(os.environ.get("AGENT_HOMES_ROOT", "/srv/agent-homes"))
    relay_key_file: Path = Path(os.environ.get("AGENT_RELAY_KEY_FILE", "/run/secrets/agent_relay_key"))
    # 每用户一个长驻 dsh 子进程；空闲到这个点才回收（只在没有在途轮次时执行）
    idle_reap_seconds: int = int(os.environ.get("AGENT_IDLE_REAP_SECONDS", "600"))
    reap_scan_seconds: int = int(os.environ.get("AGENT_REAP_SCAN_SECONDS", "60"))
    # 内存准入：低于这个可用内存就排队，不是硬起、也不是静默失败
    admission_min_available_mib: int = int(os.environ.get("AGENT_ADMISSION_MIN_AVAILABLE_MIB", "384"))
    # 排队时多久再试一次准入
    admission_retry_seconds: int = int(os.environ.get("AGENT_ADMISSION_RETRY_SECONDS", "10"))
    turn_timeout_seconds: int = int(os.environ.get("AGENT_TURN_TIMEOUT_SECONDS", "900"))
    mirror_batch_events: int = int(os.environ.get("AGENT_MIRROR_BATCH_EVENTS", "20"))
    mirror_interval_seconds: float = float(os.environ.get("AGENT_MIRROR_INTERVAL_SECONDS", "2"))
    home_quota_mib: int = int(os.environ.get("AGENT_HOME_QUOTA_MIB", "512"))
    # 回传凭据的有效期：容器自己签，A 机用同一把派生密钥验
    relay_ttl_seconds: int = int(os.environ.get("AGENT_RELAY_TTL_SECONDS", "900"))
    # 每用户不同 uid 的降权运行（结构性隔离的一层）；本地开发可关
    drop_privileges: bool = _flag("AGENT_DROP_PRIVILEGES", "true")
    uid_base: int = int(os.environ.get("AGENT_UID_BASE", "9000"))

    @property
    def mcp_url(self) -> str:
        return f"{self.kb_base_url}/mcp"

    @property
    def llm_base_url(self) -> str:
        return f"{self.kb_base_url}/v1/llm"

    def relay_key(self) -> bytes:
        from .relaykey import decode_key

        return decode_key(self.relay_key_file.read_text(encoding="ascii"))
