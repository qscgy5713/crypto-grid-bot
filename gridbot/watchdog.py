"""斷線監看:追蹤「多久沒收到價格」,產生警告 / 停機 / 恢復事件。純邏輯,時間由呼叫端傳入,方便測試。"""
from __future__ import annotations

from typing import Optional, Tuple

Event = Tuple[str, float]  # ("warn" | "halt" | "recover", 秒數)


class Watchdog:
    def __init__(self, warn_after: float, halt_after: float, now: float, repeat: float = 60.0):
        if warn_after <= 0:
            raise ValueError("stale-warn 必須 > 0")
        if halt_after and halt_after <= warn_after:
            raise ValueError("stale-halt 必須大於 stale-warn(0 表示不自動停機)")
        self.warn_after, self.halt_after, self.repeat = warn_after, halt_after, repeat
        self.last_price_at = now
        self._warned_at: Optional[float] = None

    def on_price(self, now: float) -> Optional[Event]:
        """收到價格。若先前已警告過,回報 recover 與這次中斷的秒數。"""
        gap = now - self.last_price_at
        was_warned = self._warned_at is not None
        self.last_price_at, self._warned_at = now, None
        return ("recover", gap) if was_warned else None

    def on_idle(self, now: float) -> Optional[Event]:
        """沒收到價格的心跳。達停機門檻回 halt;達警告門檻回 warn(之後每 repeat 秒再提醒一次)。"""
        stale = now - self.last_price_at
        if self.halt_after and stale >= self.halt_after:
            return ("halt", stale)
        if stale >= self.warn_after and (self._warned_at is None or now - self._warned_at >= self.repeat):
            self._warned_at = now
            return ("warn", stale)
        return None
