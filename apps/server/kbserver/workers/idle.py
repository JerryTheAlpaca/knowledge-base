"""ASR 服务器空闲准入（docs/11 §6.2）。

"空闲"必须看宿主机整体负载（/proc/stat、/proc/meminfo），不是只看知识库
有没有任务。容器内 CPU 百分比不能代表宿主机；指标读不到时保持排队并显示
原因，不猜测服务器空闲。

- can_start：开始下一段的条件——连续空闲窗口内整机忙碌比例低于阈值、
  MemAvailable 足够、没有到期的普通任务。
- check_running：运行中变忙的判定（CPU 高于停止阈值持续一段时间、或内存
  逼近下限），由执行方终止当前识别子进程并保留已完成段。
- note_busy：因忙碌让出后进入冷却，冷却结束且重新满足空闲条件才继续。

全部阈值为工程初值（docs/11 §9.3），部署前以基线校准。
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class HostSample:
    t: float
    idle: float                  # 累计 idle jiffies（不含 iowait；iowait/steal 算忙碌）
    total: float                 # 累计总 jiffies
    mem_available_mib: float | None


def sample_host(now: float | None = None) -> HostSample | None:
    """读一次宿主机指标；非 Linux 或读取失败返回 None（指标不可用）。"""
    t = time.time() if now is None else now
    try:
        stat_line = ""
        with open("/proc/stat", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("cpu "):
                    stat_line = line
                    break
        if not stat_line:
            return None
        parts = [int(x) for x in stat_line.split()[1:]]
        # user nice system idle iowait irq softirq steal guest guest_nice
        if len(parts) < 5:
            return None
        idle = float(parts[3])  # iowait(parts[4]) 不算空闲：高 iowait 不视为空闲
        total = float(sum(parts[:8]))  # guest 已含在 user 中，不加
        mem_mib = None
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_mib = float(line.split()[1]) / 1024.0
                    break
        return HostSample(t=t, idle=idle, total=total, mem_available_mib=mem_mib)
    except (OSError, ValueError):
        return None


class AsrGate:
    """空闲状态机：按调用节奏采样并维护滑窗；Windows（无 /proc）恒不空闲。"""

    WINDOW_SECONDS = 60.0

    def __init__(self, sampler=sample_host):
        self._sampler = sampler
        self._samples: deque[HostSample] = deque()
        self._cooldown_until = 0.0
        self._cpu_busy_streak = 0      # 运行中 CPU 连续超阈值的检查次数（每 ~5s 一次）
        self._mem_low_streak = 0       # 运行中内存连续过低的检查次数

    # ---- 采样 ----

    def _take_sample(self) -> HostSample | None:
        s = self._sampler()
        if s is not None:
            self._samples.append(s)
            cutoff = s.t - (self.WINDOW_SECONDS + 10.0)
            while self._samples and self._samples[0].t < cutoff:
                self._samples.popleft()
        return s

    def _busy_ratio(self, window_s: float) -> float | None:
        """窗口内整机忙碌比例；样本覆盖不足窗口时返回 None。"""
        if len(self._samples) < 2:
            return None
        first = self._samples[0]
        last = self._samples[-1]
        if last.t - first.t < window_s * 0.9:
            return None
        total = last.total - first.total
        idle = last.idle - first.idle
        if total <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - idle / total))

    # ---- 启动判定 ----

    def can_start(self, settings, *, normal_busy: bool) -> tuple[bool, str]:
        """是否允许开始下一段 ASR 工作；返回 (允许, 不允许原因)。"""
        now = time.time()
        if now < self._cooldown_until:
            return False, "resource_busy"  # 忙碌让出后的冷却期
        if normal_busy:
            return False, "normal_jobs_active"
        sample = self._take_sample()
        if sample is None:
            return False, "metrics_unavailable"
        ratio = self._busy_ratio(settings.asr_idle_hold_seconds)
        if ratio is None:
            return False, "idle_window_filling"  # 空闲窗口尚未积累满
        if ratio >= settings.asr_idle_cpu_start:
            return False, "cpu_busy"
        mem = sample.mem_available_mib
        if mem is not None and mem < settings.asr_idle_min_available_mib:
            return False, "memory_low"
        return True, ""

    # ---- 运行中检查 ----

    def check_running(self, settings) -> bool:
        """运行中约每 5s 调用一次；返回 False 表示应终止当前段让出资源。

        CPU 忙碌阈值包含 ASR 自身占用，因此停止阈值高于启动阈值，
        避免自己在无其他负载时触发自己（docs/11 §6.2）。
        """
        sample = self._take_sample()
        if sample is None:
            return True  # 指标暂时读不到：不打断，保持保守
        abort = False
        ratio = self._busy_ratio(20.0)
        if ratio is not None and ratio > settings.asr_idle_cpu_stop:
            self._cpu_busy_streak += 1
            if self._cpu_busy_streak >= 4:  # ~20s 连续忙碌
                abort = True
        else:
            self._cpu_busy_streak = 0
        mem = sample.mem_available_mib
        if mem is not None and mem < settings.asr_busy_min_available_mib:
            self._mem_low_streak += 1
            if self._mem_low_streak >= 2:  # 连续两次 5s 采样过低
                abort = True
        else:
            self._mem_low_streak = 0
        return not abort

    def note_busy(self) -> None:
        """因资源忙碌让出：进入冷却期，之后需重新满足连续空闲条件。"""
        self._cooldown_until = time.time() + 120.0
        self._cpu_busy_streak = 0
        self._mem_low_streak = 0


# 测试与部署校验用：当前平台是否具备整机指标
def metrics_available() -> bool:
    return os.path.exists("/proc/stat")
