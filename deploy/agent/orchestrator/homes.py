"""每用户一份 `$DSH_HOME`（docs/27 §$DSH_HOME 布局）。

```
/srv/agent-homes/<site>/<user_id>/
  profiles/sdk/cordis.patch.yml   ← 由模板按站点渲染，只含本站点那一个 MCP 条目
  sessions/                       ← 唯一会长大的东西（JSONL 历史）
  .credentials.yaml               ← 不放模型 Key（Key 只在子进程环境里，来自 A 机会话 token）
/srv/agent-homes/<site>/<user_id>.workspace/   ← dsh 的 cwd：空的、只读的写入围栏
```

隔离靠三层叠加，不是任何单层：
1. 目录按 `<site>/<user_id>` 分开，权限 700，**每用户不同 uid**（进程读不到别人的家）；
2. 渲染出来的补丁里只有本站点的 MCP 连接（那个进程根本没有别站的通道）；
3. 会话表与回传都带 `site`，值只从 A 机签发的中继凭据取。

配额 `AGENT_HOME_QUOTA_MIB` 要靠 XFS `prjquota` 或独立挂载点**硬限**：周期 `du`
发现时盘已经写满了。设配额的动作在部署脚本里（见 deploy/agent/README.md），
这里只负责如实报告读不到的情况。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

from .config import OrchestratorSettings

TEMPLATE_NAME = "cordis.patch.yml.tmpl"


def user_slot(user_id: str, *, uid_base: int, slots: int = 4096) -> int:
    """把 user_id 稳定映射到一个 uid 槽位。

    取哈希而不是序号：序号会让「先注册的用户」和「后注册的用户」落在相邻槽位上，
    运维按 uid 排查时容易误判归属；哈希槽位配合 700 权限已经足够。
    """
    digest = hashlib.sha256(f"agent-home|{user_id}".encode("utf-8")).hexdigest()
    return uid_base + (int(digest[:8], 16) % slots)


class Homes:
    def __init__(self, settings: OrchestratorSettings, template_dir: Path) -> None:
        self.settings = settings
        self.template = (template_dir / TEMPLATE_NAME).read_text(encoding="utf-8")

    def home_for(self, user_id: str) -> Path:
        return self.settings.homes_root / self.settings.site / user_id

    def workspace_for(self, user_id: str) -> Path:
        return self.settings.homes_root / self.settings.site / f"{user_id}.workspace"

    def render_patch(self) -> str:
        return self.template.replace("{{KB_MCP_URL}}", self.settings.mcp_url)

    def ensure(self, user_id: str) -> tuple[Path, Path, int]:
        """建好这个用户的家与空工作区，返回 (home, workspace, uid)。"""
        home, work = self.home_for(user_id), self.workspace_for(user_id)
        (home / "profiles" / "sdk").mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        uid = user_slot(user_id, uid_base=self.settings.uid_base)
        patch = home / "profiles" / "sdk" / "cordis.patch.yml"
        patch.write_text(self.render_patch(), encoding="utf-8")
        self._own(home, uid)
        self._own(work, uid)
        # 700 而不是 755：同容器里别的用户进程不该看到这本家的目录结构
        for path in (home, work):
            os.chmod(path, stat.S_IRWXU)
        return home, work, uid

    def _own(self, path: Path, uid: int) -> None:
        if not self.settings.drop_privileges:
            return
        # 只在目录属主不是目标 uid 时才 chown -R：每轮都递归改一遍家目录的属主，
        # 会话历史一大就是纯浪费
        try:
            info = path.stat()
        except OSError:
            return
        if info.st_uid == uid:
            return
        os.chown(path, uid, uid)
        for child in path.rglob("*"):
            try:
                os.chown(child, uid, uid)
            except OSError:
                # 已经存在的别人拥有的文件不该由这个容器决定怎么处理：停在这里
                break

    def purge_if_absent(self, user_id: str) -> None:
        """删除整个家目录：只在「A 机确认这个用户不再启用 agent」时由运维调用。

        正常关闭会话**不删**家目录（历史要能续），超配额时前端提示「导出后清理」，
        不静默删用户的东西。
        """
        shutil.rmtree(self.home_for(user_id), ignore_errors=True)
        shutil.rmtree(self.workspace_for(user_id), ignore_errors=True)

    def disk_usage_mib(self, user_id: str) -> float:
        total = 0
        for path in (self.home_for(user_id), self.workspace_for(user_id)):
            for child in path.rglob("*"):
                if child.is_file():
                    total += child.stat().st_size
        return total / 1048576.0
