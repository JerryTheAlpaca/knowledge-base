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


# 审查 C-14：读不到宿主机指标时默认保守不启动（生产安全默认）。
# 本地开发（Windows 无 /proc）可显式设 ASR_IGNORE_IDLE_GATE=1 跳过门禁，
# 让转写链路可以端到端调试；生产环境不要设置。
IGNORE_GATE_ENV = "ASR_IGNORE_IDLE_GATE"


@dataclass
class HostSample:
    t: float
    idle: float                  # 累计 idle jiffies（不含 iowait；iowait/steal 算忙碌）
    total: float                 # 累计总 jiffies
    mem_available_mib: float | None
    own_usec: float | None = None  # 本容器累计 CPU 用量（µs）；读不到为 None


def _own_cpu_usec() -> float | None:
    """本容器自己用掉的 CPU（累计 µs）。

    cgroup v2 在 /sys/fs/cgroup 根上就是本容器的私有视图；v1 回退到 cpuacct。
    """
    try:
        with open("/sys/fs/cgroup/cpu.stat", "r", encoding="ascii") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] == "usage_usec":
                    return float(parts[1])
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage", "r", encoding="ascii") as f:
            return float(f.read()) / 1000.0  # v1 是 ns
    except (OSError, ValueError):
        return None
    return None


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
        return HostSample(t=t, idle=idle, total=total, mem_available_mib=mem_mib,
                          own_usec=_own_cpu_usec())
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

    def _span(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        return self._samples[-1].t - self._samples[0].t

    def _own_ratio(self, span_s: float) -> float:
        """本容器这段时间占掉的总算力比例（1.0 = 吃满整机）。

        读不到自己的 cgroup 时返回 0，即什么都不扣、退回旧的整机口径：宁可保守
        也不要在无法自证「忙的不是我」的时候抢跑。
        """
        if span_s <= 0 or len(self._samples) < 2:
            return 0.0
        first, last = self._samples[0], self._samples[-1]
        if first.own_usec is None or last.own_usec is None:
            return 0.0
        cores = os.cpu_count() or 1
        used = (last.own_usec - first.own_usec) / 1_000_000.0
        return max(0.0, min(1.0, used / (span_s * cores)))

    def _foreign_busy_ratio(self, window_s: float) -> float | None:
        """除本容器以外的整机忙碌比例。

        原先门禁拿整机忙碌比例直接和阈值比，而 Worker 容器自己有 0.75 核配额
        （2 核机上正好是 37.5%）：ASR 只要跑过一段，60 秒滑动平均就被自己顶过
        开工线，等于自己把下一段的许可破坏掉。自己的用量已经由 cgroup 配额硬
        封顶，门禁真正该问的是「邻居有多忙」。
        """
        ratio = self._busy_ratio(window_s)
        if ratio is None:
            return None
        return max(0.0, ratio - self._own_ratio(self._span()))

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
            if os.environ.get(IGNORE_GATE_ENV) == "1":
                return True, "gate_ignored"  # 显式跳过空闲门禁（仅限本地开发）
            return False, "metrics_unavailable"
        ratio = self._foreign_busy_ratio(settings.asr_idle_hold_seconds)
        if ratio is None:
            return False, "idle_window_filling"  # 空闲窗口尚未积累满
        if ratio >= settings.asr_idle_cpu_start:
            return False, "cpu_busy"
        mem = sample.mem_available_mib
        if mem is not None and mem < settings.asr_idle_min_available_mib:
            return False, "memory_low"
        return True, ""

    def can_start_io(self, settings, *, normal_busy: bool) -> tuple[bool, str]:
        """I/O 密集 ASR 阶段（asr_prepare）的准入：不要求 CPU 空闲窗口。

        prepare 是一条连接 + 一个解码线程，本就不是抢 CPU 的那一步；让它等满
        连续空闲窗口，只是把远程音频的读取再往后推 ≥60s。保留的两项都有实义：
        normal_busy 是普通任务的唯一屏障（Worker 单进程同步执行，领取后要跑到
        结束），内存下限避免在低可用内存时再堆一份解码产物。忙碌让出的冷却期不
        检查——本阶段不因 CPU 让出（见 check_running_io）。
        """
        if normal_busy:
            return False, "normal_jobs_active"
        sample = self._take_sample()
        if sample is None:
            if os.environ.get(IGNORE_GATE_ENV) == "1":
                return True, "gate_ignored"
            return False, "metrics_unavailable"
        mem = sample.mem_available_mib
        if mem is not None and mem < settings.asr_idle_min_available_mib:
            return False, "memory_low"
        return True, ""

    # ---- 运行中检查 ----

    def _mem_abort(self, sample: HostSample, settings) -> bool:
        mem = sample.mem_available_mib
        if mem is not None and mem < settings.asr_busy_min_available_mib:
            self._mem_low_streak += 1
            return self._mem_low_streak >= 2  # 连续两次 5s 采样过低
        self._mem_low_streak = 0
        return False

    def check_running(self, settings) -> bool:
        """运行中约每 5s 调用一次；返回 False 表示应终止当前段让出资源。

        看的是邻居负载（见 _foreign_busy_ratio）：本容器的用量由 cpus 配额封顶，
        不再参与「是不是该让」的判断，否则 ASR 自己就能把自己掐停。停止阈值仍
        高于启动阈值，留出差回（docs/11 §6.2）。
        """
        sample = self._take_sample()
        if sample is None:
            return True  # 指标暂时读不到：不打断，保持保守
        abort = False
        ratio = self._foreign_busy_ratio(20.0)
        if ratio is not None and ratio > settings.asr_idle_cpu_stop:
            self._cpu_busy_streak += 1
            if self._cpu_busy_streak >= 4:  # ~20s 连续忙碌
                abort = True
        else:
            self._cpu_busy_streak = 0
        if self._mem_abort(sample, settings):
            abort = True
        return not abort

    def check_running_io(self, settings) -> bool:
        """准备阶段的运行中检查：只看内存，不看 CPU。

        远程输入不做断点续传，作废一次就是从零重下（docs/11 §5.3）；一次 CPU
        抖动作废一条已在飞的下载，代价是整条重下 + 冷却 + 重新过门禁，比它省下
        的 CPU 大得多。内存仍要让，因为解码产物落在盘上、页缓存走同一份预算。
        """
        sample = self._take_sample()
        if sample is None:
            return True
        return not self._mem_abort(sample, settings)

    def note_busy(self) -> None:
        """因资源忙碌让出：进入冷却期，之后需重新满足连续空闲条件。"""
        self._cooldown_until = time.time() + 120.0
        self._cpu_busy_streak = 0
        self._mem_low_streak = 0


# 测试与部署校验用：当前平台是否具备整机指标
def metrics_available() -> bool:
    return os.path.exists("/proc/stat")
