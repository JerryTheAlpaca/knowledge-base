"""进程内限流（审查 C-06）：滑动窗口 + 惰性清理。

单进程 SQLite 架构下的已知取舍（AGENTS.md 不引入 Redis）：
- 重启即清零；多进程部署时各进程独立计数，需跨进程一致时落表或换共享存储。
- 键只在窗口内有记录时保留，并由周期清扫摘掉过期键，
  不随历史 IP／条目无限增长（修复原实现键永不删除的慢速内存泄漏）。
"""
from __future__ import annotations

import threading

from ..domain.errors import ApiError


class SlidingWindowLimiter:
    """每键在 window 内最多 limit 次。

    `now` 用 `utcnow()`（datetime）或 `time.monotonic()`（float 秒）均可，
    只要与 window 同类型可比较：datetime 配 timedelta，float 配秒数。

    这些端点都是同步 `def`，跑在 anyio 线程池里，是真并发；窗口字典的
    读改写全部在同一把锁内完成，否则迭代期间别的线程插键会抛
    `dictionary changed size during iteration`，limit=1 的场景还能被并发
    穿透（审查 C-05）。
    """

    def __init__(self, limit: int, window):
        self.limit = limit
        self.window = window
        self._attempts: dict = {}
        self._last_sweep = None
        self._lock = threading.Lock()

    def check(self, key, now, message: str) -> None:
        """超限抛 429；未超限时不记账（由 record() 决定何时记账）。"""
        with self._lock:
            self._check(key, now, message)

    def record(self, key, now) -> None:
        with self._lock:
            self._record(key, now)

    def hit(self, key, now, message: str) -> None:
        """check + record 在同一把锁内：尝试即记账（限流的是尝试次数，而非成功次数）。"""
        with self._lock:
            self._check(key, now, message)
            self._record(key, now)

    def _check(self, key, now, message: str) -> None:
        self._sweep(now)
        ts = self._attempts.get(key)
        if ts is None:
            return
        window = [t for t in ts if now - t < self.window]
        # 修剪结果回写：只读不回写时，持续活跃但没超限的键（企业 NAT 出口 IP
        # 轮询设备授权）时间戳按窗口只增不减，慢速泄漏没被消除
        if window:
            self._attempts[key] = window
        else:
            self._attempts.pop(key, None)
        if len(window) >= self.limit:
            raise ApiError("RATE_LIMITED", message, status_code=429)

    def _record(self, key, now) -> None:
        self._attempts.setdefault(key, []).append(now)

    def _sweep(self, now) -> None:
        """每过一个窗口做一次全量清理：摘掉整个窗口都没再访问的键。"""
        if self._last_sweep is not None and now - self._last_sweep < self.window:
            return
        stale = [k for k, ts in self._attempts.items()
                 if not ts or now - ts[-1] >= self.window]
        for k in stale:
            self._attempts.pop(k, None)
        self._last_sweep = now
