"""參數掃描:格數 × 區間寬度 × 模式,資料切成樣本內 / 樣本外兩段分別回測。

區間以「該段起始價 ±pct%」定義,讓同一組參數能套用到不同時段而不洩漏未來資訊。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from .backtest import Result, run_backtest
from .data import Candle
from .engine import GridConfig, build_levels


@dataclass
class Row:
    grids: int
    range_pct: float
    mode: str
    min_spacing_pct: float
    ins: Result  # 樣本內
    oos: Optional[Result]  # 樣本外(split=1 時為 None)


def _cfg(start: float, grids: int, pct: float, mode: str, invest: float, fee: float,
         stop_loss_pct: Optional[float]) -> GridConfig:
    lower = start * (1 - pct / 100)
    sl = lower * (1 - stop_loss_pct / 100) if stop_loss_pct is not None else None
    return GridConfig(lower, start * (1 + pct / 100), grids, invest, mode, fee, sl)


def split_candles(candles: List[Candle], split: float):
    """切成 (樣本內, 樣本外);split >= 1 時樣本外為空。"""
    cut = int(len(candles) * split) if split < 1 else len(candles)
    if cut < 2 or (split < 1 and len(candles) - cut < 2):
        raise ValueError("資料太少,無法依 split 切段")
    return candles[:cut], candles[cut:]


def validate_params(grids_list: Sequence[int], pcts: Sequence[float], modes: Sequence[str],
                    split: float, stop_loss_pct: Optional[float]) -> None:
    if not grids_list or not pcts or not modes:
        raise ValueError("格數、區間、模式清單都不可為空")
    if any(g < 2 for g in grids_list):
        raise ValueError("格數至少為 2")
    if any(not 0 < p < 100 for p in pcts):
        raise ValueError("區間寬度需在 (0, 100) %")
    if any(m not in ("arithmetic", "geometric") for m in modes):
        raise ValueError("模式只能是 arithmetic / geometric")
    if not 0 < split <= 1:
        raise ValueError("split 需在 (0, 1] 之間")
    if stop_loss_pct is not None and stop_loss_pct <= 0:
        raise ValueError("stop-loss-pct 必須 > 0")


def run_sweep(candles: List[Candle], grids_list: Sequence[int], pcts: Sequence[float],
              modes: Sequence[str], invest: float, fee: float, split: float = 0.7,
              stop_loss_pct: Optional[float] = None) -> List[Row]:
    validate_params(grids_list, pcts, modes, split, stop_loss_pct)
    seg_in, seg_out = split_candles(candles, split)
    rows: List[Row] = []
    for mode in modes:
        for pct in pcts:
            for g in grids_list:
                cfg_in = _cfg(seg_in[0][1], g, pct, mode, invest, fee, stop_loss_pct)
                lv = build_levels(cfg_in)
                spacing = min(lv[i + 1] / lv[i] - 1 for i in range(len(lv) - 1))
                if spacing <= fee * 2:
                    continue  # 格距吃不過來回手續費,直接略過(價位線隨起始價等比縮放,樣本外格距%相同)
                oos = None
                if seg_out:
                    cfg_out = _cfg(seg_out[0][1], g, pct, mode, invest, fee, stop_loss_pct)
                    oos = run_backtest(cfg_out, seg_out)
                rows.append(Row(g, pct, mode, spacing * 100, run_backtest(cfg_in, seg_in), oos))
    return rows


@dataclass
class Fold:
    index: int
    train_start: float  # ts
    test_start: float
    test_end: float
    grids: int
    range_pct: float
    mode: str
    train: Result
    test: Result


def run_walk_forward(candles: List[Candle], grids_list: Sequence[int], pcts: Sequence[float],
                     modes: Sequence[str], invest: float, fee: float, train: int, test: int,
                     step: Optional[int] = None, stop_loss_pct: Optional[float] = None) -> List[Fold]:
    """滾動視窗:每折在訓練段挑樣本內損益最佳的參數,套到緊接的測試段(從未被用來挑參數)。

    視窗每次前進 step 根(預設 = test,測試段彼此不重疊)。
    """
    validate_params(grids_list, pcts, modes, 1.0, stop_loss_pct)
    step = test if step is None else step
    if train < 2 or test < 2 or step < 1:
        raise ValueError("train / test 至少 2 根,step 至少 1 根")
    if len(candles) < train + test:
        raise ValueError(f"資料 {len(candles)} 根不足以湊出一折(需 {train + test} 根)")
    folds: List[Fold] = []
    s = 0
    while s + train + test <= len(candles):
        seg_tr = candles[s:s + train]
        seg_te = candles[s + train:s + train + test]
        best = None
        for mode in modes:
            for pct in pcts:
                for g in grids_list:
                    cfg = _cfg(seg_tr[0][1], g, pct, mode, invest, fee, stop_loss_pct)
                    lv = build_levels(cfg)
                    if min(lv[i + 1] / lv[i] - 1 for i in range(len(lv) - 1)) <= fee * 2:
                        continue
                    r = run_backtest(cfg, seg_tr)
                    if best is None or r.pnl_pct > best[0].pnl_pct:
                        best = (r, g, pct, mode)
        if best is None:
            raise ValueError("所有組合的格距都不足以覆蓋手續費")
        r_tr, g, pct, mode = best
        r_te = run_backtest(_cfg(seg_te[0][1], g, pct, mode, invest, fee, stop_loss_pct), seg_te)
        folds.append(Fold(len(folds) + 1, seg_tr[0][0], seg_te[0][0], seg_te[-1][0],
                          g, pct, mode, r_tr, r_te))
        s += step
    return folds
