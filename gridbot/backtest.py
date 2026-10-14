from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .data import Candle
from .engine import GridConfig, GridEngine


@dataclass
class Result:
    start_price: float
    end_price: float
    equity: float
    pnl: float
    pnl_pct: float
    hold_pnl_pct: float  # 同期買入持有
    max_drawdown_pct: float
    buys: int
    sells: int
    cycles: int
    grid_profit: float
    base: float
    quote: float
    halted: bool
    bars: int  # K 線根數


def candle_path(o: float, h: float, l: float, c: float) -> List[float]:
    """K 線內部路徑未知,採保守假設:陽線先探低再拉高,陰線先拉高再探低。"""
    return [o, l, h, c] if c >= o else [o, h, l, c]


def run_backtest(cfg: GridConfig, candles: List[Candle]) -> Result:
    first = candles[0]
    eng = GridEngine(cfg, first[1], first[0])
    peak = cfg.invest
    max_dd = 0.0
    prev_close = None
    for ts, o, h, l, c in candles:
        if prev_close is not None and o != prev_close:
            eng.on_price(o, ts, gap=True)  # 上一根收盤到這根開盤之間沒有成交(缺資料 / 跳空)
        for p in candle_path(o, h, l, c)[1:]:
            eng.on_price(p, ts)
            eq = eng.equity(p)
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak)
        prev_close = c
    end = candles[-1][4]
    eq = eng.equity(end)
    return Result(
        start_price=first[1],
        end_price=end,
        equity=eq,
        pnl=eq - cfg.invest,
        pnl_pct=(eq / cfg.invest - 1) * 100,
        hold_pnl_pct=(end / first[1] - 1) * 100,
        max_drawdown_pct=max_dd * 100,
        buys=eng.buy_count,
        sells=eng.cycles,
        cycles=eng.cycles,
        grid_profit=eng.grid_profit,
        base=eng.base,
        quote=eng.quote,
        halted=eng.halted,
        bars=len(candles),
    )
