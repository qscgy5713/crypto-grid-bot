from __future__ import annotations

import argparse
import sys
import time

from .backtest import run_backtest
from .data import fetch_klines, fetch_price, load_csv, synthetic
from .engine import GridConfig, GridEngine, build_levels


def _add_grid_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lower", type=float, required=True, help="區間下緣價")
    p.add_argument("--upper", type=float, required=True, help="區間上緣價")
    p.add_argument("--grids", type=int, default=20, help="格數(預設 20)")
    p.add_argument("--invest", type=float, default=1000, help="本金,計價幣(預設 1000)")
    p.add_argument("--mode", choices=["arithmetic", "geometric"], default="arithmetic")
    p.add_argument("--fee", type=float, default=0.001, help="單邊手續費率(預設 0.001)")
    p.add_argument("--stop-loss", type=float, default=None, help="停損價,須低於 lower")


def _cfg(a) -> GridConfig:
    return GridConfig(a.lower, a.upper, a.grids, a.invest, a.mode, a.fee, a.stop_loss)


def _warn_spacing(cfg: GridConfig) -> None:
    lv = build_levels(cfg)
    min_pct = min((lv[i + 1] / lv[i] - 1) for i in range(len(lv) - 1))
    print(f"最小格距 {min_pct * 100:.3f}% (來回手續費約 {cfg.fee_rate * 2 * 100:.3f}%)")
    if min_pct <= cfg.fee_rate * 2:
        print("⚠ 格距不足以覆蓋來回手續費,每次套利都會虧損,請減少格數或放大區間", file=sys.stderr)


def cmd_backtest(a) -> int:
    cfg = _cfg(a)
    cfg.validate()
    if a.csv:
        candles = load_csv(a.csv)
    elif a.symbol:
        candles = fetch_klines(a.symbol, a.interval, a.limit)
    else:
        mid = (cfg.lower + cfg.upper) / 2
        candles = synthetic(a.limit, mid, a.vol, a.seed)
        print(f"使用合成資料:{a.limit} 根,波動 {a.vol},seed={a.seed}")
    _warn_spacing(cfg)
    r = run_backtest(cfg, candles)
    print(f"價格 {r.start_price:.4f} -> {r.end_price:.4f}  ({len(candles)} 根 K 線)")
    print(f"期末權益 {r.equity:.2f} / 本金 {cfg.invest:.2f}  損益 {r.pnl:+.2f} ({r.pnl_pct:+.2f}%)")
    print(f"同期買入持有 {r.hold_pnl_pct:+.2f}%  最大回撤 {r.max_drawdown_pct:.2f}%")
    print(f"買 {r.buys} 次 / 賣 {r.sells} 次 / 完成套利 {r.cycles} 次  網格利潤(估) {r.grid_profit:+.2f}")
    print(f"持倉 {r.base:.6f} 幣 + 現金 {r.quote:.2f}" + ("  [已觸發停損]" if r.halted else ""))
    return 0


def cmd_paper(a) -> int:
    """模擬盤:輪詢 Binance 公開現價,只在本地模擬成交,不下任何真實訂單。"""
    cfg = _cfg(a)
    cfg.validate()
    _warn_spacing(cfg)
    price = fetch_price(a.symbol)
    eng = GridEngine(cfg, price, time.time())
    print(f"[paper] {a.symbol} 起始價 {price}  每格 {eng.qty:.6f}  Ctrl+C 結束")
    try:
        while not eng.halted:
            time.sleep(a.interval)
            try:
                price = fetch_price(a.symbol)
            except Exception as e:  # 網路瞬斷不中止
                print(f"[paper] 取價失敗:{e}", file=sys.stderr)
                continue
            for f in eng.on_price(price, time.time()):
                print(f"[paper] {f.side:4} @ {f.price:.4f} x {f.qty:.6f} {f.note}")
            print(f"[paper] 現價 {price}  權益 {eng.equity(price):.2f}  套利 {eng.cycles} 次", end="\r")
    except KeyboardInterrupt:
        print(f"\n[paper] 結束:權益 {eng.equity(price):.2f}  損益 {eng.equity(price) - cfg.invest:+.2f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gridbot", description="現貨網格交易(回測 / 模擬盤)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="回測")
    _add_grid_args(bt)
    src = bt.add_mutually_exclusive_group()
    src.add_argument("--csv", help="CSV 路徑(ts,open,high,low,close)")
    src.add_argument("--symbol", help="Binance 交易對,如 BTCUSDT")
    bt.add_argument("--interval", default="1h", help="K 線週期(搭配 --symbol)")
    bt.add_argument("--limit", type=int, default=500, help="K 線根數(<=1000)")
    bt.add_argument("--vol", type=float, default=0.01, help="合成資料每根波動")
    bt.add_argument("--seed", type=int, default=42)
    bt.set_defaults(fn=cmd_backtest)

    pp = sub.add_parser("paper", help="模擬盤(即時現價,不下真單)")
    _add_grid_args(pp)
    pp.add_argument("--symbol", required=True)
    pp.add_argument("--interval", type=float, default=5, help="輪詢秒數")
    pp.set_defaults(fn=cmd_paper)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, OSError) as e:  # 參數錯誤 / 檔案或網路錯誤
        print(f"錯誤:{e}", file=sys.stderr)
        return 2
