"""内存准入与空闲回收（docs/27 §容量预算）。

**不写死并发数字**：2GB 的 A 机上真实可行并发是 1，偶尔 2，而这个数字取决于当下
share_runner 有没有在渲染、ASR 有没有在跑。写死一个常量要么把机器压垮，要么白白
限制 B 机。所以按可用内存准入（照仓库既有的 `ASR_IDLE_MIN_AVAILABLE_MIB` 先例）：
够就起，不够就排队并如实告诉前端「服务器忙，已排队」。

容器另外带 `mem_limit`：超了是容器内部 OOM，伤害局限在 agent 容器里，不会打死
同机的 api / worker / share_runner。这两件事是配套的，不是一个替代另一个。

磁盘同理：这台机没有可分配配额的独立挂载点，所以按 `$DSH_HOME` 所在文件系统的
**可用空间**准入（`AGENT_MIN_FREE_DISK_MIB`），低于这个点就排队而不是继续写。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def host_available_mib() -> float | None:
    """宿主机 MemAvailable（MiB）。读不到返回 None —— 指标缺失时保守不启动，
    而不是猜一个空闲值（与 `workers/idle.py` 同一立场）。"""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        return None
    return None


def cgroup_remaining_mib() -> float | None:
    """本容器还能用多少内存：mem_limit - 当前用量。

    v2 在 /sys/fs/cgroup，v1 回退 memory.cgroup。任一条读不到就返回 None，
    由调用方按「只信宿主机」处理。
    """
    candidates_v2 = (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current"))
    try:
        limit_raw = candidates_v2[0].read_text(encoding="ascii").strip()
        current = float(candidates_v2[1].read_text(encoding="ascii").strip())
        if limit_raw == "max":
            return None
        return max(0.0, (float(limit_raw) - current) / 1048576.0)
    except (OSError, ValueError):
        pass
    try:
        limit = float(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
                      .read_text(encoding="ascii").strip())
        usage = float(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
                      .read_text(encoding="ascii").strip())
        # v1 里「没有限制」会读到一个接近 int64 上限的巨值，别把它当成限额
        if limit > 2 ** 62:
            return None
        return max(0.0, (limit - usage) / 1048576.0)
    except (OSError, ValueError):
        return None


def disk_free_mib(path: Path) -> float | None:
    """这个路径所在文件系统的可用空间（MiB）。

    容器里的 `/srv/agent-homes` 落在宿主那块盘上，所以这个数就是「还敢写多少」——
    没有 XFS 配额可用的机器上，防止写满盘的闸门只能建在这里。路径还没建出来时
    退到最近的已存在祖先（同一个文件系统），一路到根都不存在才算量不到。
    """
    target = path
    while not target.exists():
        parent = target.parent
        if parent == target:
            return None
        target = parent
    try:
        return shutil.disk_usage(target).free / 1048576.0
    except OSError:
        return None


def may_start(min_available_mib: int, *, min_free_disk_mib: int = 0,
              disk_path: Path | None = None) -> tuple[bool, str]:
    """能不能起一个新的 dsh 子进程；返回 (可以, 原因)。"""
    host = host_available_mib()
    if host is None:
        # 本地开发（Windows/macOS）没有 /proc：显式跳过才放行，生产不设这个变量
        if os.environ.get("AGENT_IGNORE_ADMISSION") == "1":
            return True, "gate_ignored"
        return False, "metrics_unavailable"
    if host < min_available_mib:
        return False, "memory_low"
    remaining = cgroup_remaining_mib()
    if remaining is not None and remaining < min_available_mib:
        return False, "container_memory_low"
    if min_free_disk_mib > 0 and disk_path is not None:
        free = disk_free_mib(disk_path)
        # 与内存指标同一立场：量不到就保守不启动（排队会如实显示，不是静默失败）
        if free is None:
            return False, "disk_metrics_unavailable"
        if free < min_free_disk_mib:
            return False, "disk_low"
    return True, ""
