from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from .backtest import run_backtest
from .data import fetch_klines, fetch_price, load_csv, stream_prices, synthetic
from .ws import WebSocketError
from .engine import GridConfig, GridEngine, build_levels
from .state import acquire_lock, load_state, save_state
from .watchdog import Watchdog
from .sweep import run_sweep, run_walk_forward, split_candles, validate_params


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


def _load_candles(a):
    """sweep / walkforward 共用:--csv 或 --symbol(兩者都沒給預設 BTCUSDT)。"""
    if a.csv and a.symbol:
        raise ValueError("--csv 與 --symbol 只能擇一")
    if a.csv:
        return load_csv(a.csv)
    return fetch_klines(a.symbol or "BTCUSDT", a.interval, a.limit)


def _ints(s: str):
    return [int(x) for x in s.split(",") if x]


def _floats(s: str):
    return [float(x) for x in s.split(",") if x]


def cmd_sweep(a) -> int:
    grids, pcts = _ints(a.grids_list), _floats(a.range_pcts)
    modes = [m for m in a.modes.split(",") if m]
    validate_params(grids, pcts, modes, a.split, a.stop_loss_pct)  # 先驗參數,再打網路
    if a.top < 1:
        raise ValueError("--top 必須 >= 1")
    candles = _load_candles(a)
    rows = run_sweep(candles, grids, pcts, modes, a.invest, a.fee, a.split, a.stop_loss_pct)
    if not rows:
        raise ValueError("所有組合的格距都不足以覆蓋手續費")
    has_oos = rows[0].oos is not None
    rank_out = a.rank_by == "out" and has_oos
    rows.sort(key=(lambda r: r.oos.pnl_pct) if rank_out else (lambda r: r.ins.pnl_pct), reverse=True)
    seg_in, seg_out = split_candles(candles, a.split)
    label = "樣本外" if rank_out else "樣本內"
    print(f"{len(candles)} 根 K 線,樣本內 {len(seg_in)} / 樣本外 {len(seg_out)}  (依{label}損益排序,前 {a.top} 名)")
    if rank_out:
        print("⚠ 以樣本外排序等於用樣本外挑參數,樣本外數字會偏樂觀,僅供參考")
    print(f"買入持有:樣本內 {rows[0].ins.hold_pnl_pct:+.2f}%" + (f"  樣本外 {rows[0].oos.hold_pnl_pct:+.2f}%" if has_oos else ""))
    print(f"{'模式':<10}{'格數':>4}{'±區間%':>8}{'格距%':>7} | {'內:損益%':>9}{'回撤%':>7}{'套利':>5} | {'外:損益%':>9}{'回撤%':>7}{'套利':>5}")
    for r in rows[: a.top]:
        o = r.oos
        right = f"{o.pnl_pct:>+9.2f}{o.max_drawdown_pct:>7.2f}{o.cycles:>5}" if o else ""
        print(f"{r.mode:<10}{r.grids:>4}{r.range_pct:>8.1f}{r.min_spacing_pct:>7.2f} | "
              f"{r.ins.pnl_pct:>+9.2f}{r.ins.max_drawdown_pct:>7.2f}{r.ins.cycles:>5} | {right}")
    if has_oos:
        both = sum(1 for r in rows if r.ins.pnl_pct > 0 and r.oos.pnl_pct > 0)
        print(f"共 {len(rows)} 組,兩段皆為正報酬的有 {both} 組")
    return 0


def _d(ts: float) -> str:
    return time.strftime("%m-%d", time.gmtime(ts)) if ts > 1e9 else f"#{int(ts)}"


def cmd_walkforward(a) -> int:
    grids, pcts = _ints(a.grids_list), _floats(a.range_pcts)
    modes = [m for m in a.modes.split(",") if m]
    validate_params(grids, pcts, modes, 1.0, a.stop_loss_pct)
    candles = _load_candles(a)
    folds = run_walk_forward(candles, grids, pcts, modes, a.invest, a.fee,
                             a.train, a.test, a.step, a.stop_loss_pct)
    print(f"{len(candles)} 根 K 線 ({_d(candles[0][0])}~{_d(candles[-1][0])}),訓練 {a.train} / 測試 {a.test} / 步長 {a.test if a.step is None else a.step},共 {len(folds)} 折")
    print(f"{'折':>3} {'測試期間':<13}{'選中參數':<20}{'訓練%':>8} | {'測試%':>8}{'持有%':>8}{'回撤%':>7}{'套利':>5}")
    for f in folds:
        t = f.test
        span = f"{_d(f.test_start)}~{_d(f.test_end)}"
        param = f"{f.mode[:3]}/{f.grids}格/±{f.range_pct:g}%"
        print(f"{f.index:>3} {span:<13}{param:<{20 - param.count('格')}}"
              f"{f.train.pnl_pct:>+8.2f} | {t.pnl_pct:>+8.2f}{t.hold_pnl_pct:>+8.2f}{t.max_drawdown_pct:>7.2f}{t.cycles:>5}")
    pnl = sorted(f.test.pnl_pct for f in folds)
    n = len(pnl)
    median = pnl[n // 2] if n % 2 else (pnl[n // 2 - 1] + pnl[n // 2]) / 2
    comp = comp_h = 1.0
    for f in folds:
        comp *= 1 + f.test.pnl_pct / 100
        comp_h *= 1 + f.test.hold_pnl_pct / 100
    chained = (a.test if a.step is None else a.step) >= a.test  # 測試段不重疊時,複利串接才有意義
    print("-" * 70)
    print(f"測試段損益:平均 {sum(pnl) / n:+.2f}%  中位數 {median:+.2f}%  最差 {pnl[0]:+.2f}%  最好 {pnl[-1]:+.2f}%")
    print(f"賺錢 {sum(1 for x in pnl if x > 0)}/{n} 折  勝過買入持有 {sum(1 for f in folds if f.test.pnl_pct > f.test.hold_pnl_pct)}/{n} 折"
          f"  訓練平均 {sum(f.train.pnl_pct for f in folds) / n:+.2f}%(與測試平均的差距即過擬合程度)")
    if chained:
        print(f"測試段串接複利:網格 {(comp - 1) * 100:+.2f}%  買入持有 {(comp_h - 1) * 100:+.2f}%")
    return 0


def _print_fills(fills) -> None:
    for f in fills:
        print(f"{_CLR}[paper] {f.side:4} @ {f.price:.4f} x {f.qty:.6f} {f.note}", flush=True)


_CLR = "\r\033[K" if sys.stdout.isatty() else ""  # 非終端機(重導向)時不輸出控制碼


def poll_prices(symbol: str, interval: float):
    """輪詢價格來源:每 interval 秒讀一次 REST 現價;網路瞬斷只警告。

    等待與失敗時每秒 yield 一次 None 當心跳,讓呼叫端能做斷線監看。"""
    while True:
        waited = 0.0
        while waited < interval:
            step = min(1.0, interval - waited)
            time.sleep(step)
            waited += step
            yield None
        try:
            yield fetch_price(symbol)
        except Exception as e:
            print(f"{_CLR}[paper] 取價失敗:{e}", file=sys.stderr, flush=True)
            yield None


WS_RETRY_SECS = 60.0  # WebSocket 放棄退回輪詢後,每隔這麼久再試著回到 WebSocket


def _price_source(a):
    if a.source != "ws":
        yield from poll_prices(a.symbol, a.interval)
        return
    while True:
        try:
            yield from stream_prices(a.symbol, give_up_after=3,
                                     log=lambda m: print(f"{_CLR}[paper] {m}", flush=True))
            return
        except WebSocketError as e:
            print(f"{_CLR}[paper] {e};改用輪詢(每 {a.interval:g} 秒),{WS_RETRY_SECS:g} 秒後再試 WebSocket",
                  file=sys.stderr, flush=True)
        poll = poll_prices(a.symbol, a.interval)
        t_end = time.time() + WS_RETRY_SECS
        try:
            for p in poll:
                yield p
                if time.time() >= t_end:
                    break
        finally:
            poll.close()


def _status(eng: GridEngine, price: float) -> str:
    buy, sell = eng.next_orders()
    nb = f"{buy:.2f}({(buy / price - 1) * 100:+.2f}%)" if buy else "-"
    ns = f"{sell:.2f}({(sell / price - 1) * 100:+.2f}%)" if sell else "-"
    return (f"[paper] {time.strftime('%H:%M:%S')} 現價 {price}  權益 {eng.equity(price):.2f}  "
            f"套利 {eng.cycles} 次  下一買 {nb}  下一賣 {ns}")


_ALERTS: list = []  # (Popen, 啟動時間):進行中的警報指令


def _kill(p) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)  # shell 與其子行程一起收掉
    except (OSError, AttributeError):
        p.kill()
    p.wait()


def _spawn_alert(cmd: str, env: dict) -> None:
    """背景執行警報指令,不阻塞價格迴圈;順便回收已結束 / 卡住超過 30 秒的舊指令。"""
    now = time.time()
    alive = []
    for p, t in _ALERTS:
        if p.poll() is None:
            if now - t > 30:
                _kill(p)
            else:
                alive.append((p, t))
    _ALERTS[:] = alive
    _ALERTS.append((subprocess.Popen(cmd, shell=True, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True), now))


def _flush_alerts(timeout: float = 5.0) -> None:
    """結束前最多等 timeout 秒讓警報指令跑完(例如停機通知),逾時就砍掉。"""
    end = time.time() + timeout
    for p, _ in _ALERTS:
        try:
            p.wait(max(0.05, end - time.time()))
        except subprocess.TimeoutExpired:
            _kill(p)
    _ALERTS.clear()


def _alert(a, level: str, msg: str) -> None:
    """終端機顯示(並響鈴);有 --alert-cmd 時另外在背景執行該指令(訊息放在環境變數)。"""
    print(f"{_CLR}[paper] {msg}" + ("\a" if level != "recover" and sys.stdout.isatty() else ""), flush=True)
    if a.alert_cmd:
        env = dict(os.environ, GRIDBOT_ALERT_LEVEL=level, GRIDBOT_ALERT_MSG=msg)
        try:
            _spawn_alert(a.alert_cmd, env)
        except Exception as e:  # 警報指令失敗不能拖垮交易迴圈
            print(f"{_CLR}[paper] 警報指令失敗:{e}", file=sys.stderr, flush=True)


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt  # 走與 Ctrl+C 相同的收尾流程(存檔)


def cmd_paper(a) -> int:
    """模擬盤:只讀 Binance 公開現價(WebSocket 即時 / 輪詢),在本地模擬成交,不下任何真實訂單。

    狀態定期存檔;再次啟動會自動接續(參數須一致,否則請加 --reset)。
    """
    cfg = _cfg(a)
    cfg.validate()
    Watchdog(a.stale_warn, a.stale_halt, 0.0)  # 先驗參數,再打網路
    if a.interval < 0 or a.interval >= a.stale_warn:
        raise ValueError(f"--interval ({a.interval:g}) 必須 >= 0 且小於 --stale-warn ({a.stale_warn:g}),否則輪詢間隔本身就會觸發斷線警報")
    _warn_spacing(cfg)
    path = a.state or os.path.join("state", f"paper_{a.symbol}.json")
    lock = acquire_lock(path)  # 同一狀態檔同時只允許一個行程
    try:
        return _run_paper(a, cfg, path)
    finally:
        if lock:
            lock.close()


def _run_paper(a, cfg: GridConfig, path: str) -> int:
    meta = {"symbol": a.symbol}
    price = fetch_price(a.symbol)
    if os.path.exists(path) and not a.reset:
        eng, old = load_state(path)
        if old.get("symbol") != a.symbol or eng.cfg != cfg:
            raise ValueError(
                f"狀態檔 {path} 的交易對/參數與這次不同;請用相同參數接續,或加 --reset 重新開始,或用 --state 指定其他檔案"
            )
        print(f"[paper] 接續 {path}:權益 {eng.equity(price):.2f}  套利 {eng.cycles} 次  掛單 {len(eng.orders)} 張")
        if eng.halted:
            print("[paper] 上次已觸發停損停機,請加 --reset 重新開始")
            return 0
        if not (cfg.lower < price < cfg.upper):
            print(f"[paper] ⚠ 現價 {price} 已在區間外", file=sys.stderr)
        print("[paper] 停機期間的價格變動以直線近似,實際路徑可能不同")
        _print_fills(eng.on_price(price, time.time()))
        if eng.halted:  # 停機期間價格已跌破停損:清倉後不必再進入價格迴圈
            save_state(eng, path, meta)
            print(f"[paper] 現價 {price} 已觸發停損並清倉;結算權益 {eng.equity(price):.2f}"
                  f"  損益 {eng.equity(price) - cfg.invest:+.2f}")
            return 0
    else:
        if not (cfg.lower < price < cfg.upper):
            raise ValueError(
                f"{a.symbol} 現價 {price} 不在區間 ({cfg.lower}, {cfg.upper}) 內,"
                f"請調整 --lower/--upper(例如現價 ±10%:{price * 0.9:.2f} ~ {price * 1.1:.2f})"
            )
        eng = GridEngine(cfg, price, time.time())
        print(f"[paper] {a.symbol} 起始價 {price}  每格 {eng.qty:.6f}  狀態檔 {path}  Ctrl+C 結束", flush=True)
    save_state(eng, path, meta)

    source = _price_source(a)
    ticks, rc = 0, 0
    wd = Watchdog(a.stale_warn, a.stale_halt, time.time())
    last_save = last_status = time.time()
    status_every = 1.0 if sys.stdout.isatty() else 30.0
    old_term = None
    try:
        old_term = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # 非主執行緒不能設 signal handler
        pass
    try:
        for price in source:
            now = time.time()
            if price is None:  # 心跳:沒有新價格
                ev = wd.on_idle(now)
                if ev and ev[0] == "warn":
                    _alert(a, "warn", f"⚠ 已 {ev[1]:.0f} 秒沒有收到價格;持倉 {eng.base:.6f} 幣目前無人監控"
                           + (f",超過 {a.stale_halt:g} 秒將自動停機" if a.stale_halt else ""))
                elif ev and ev[0] == "halt":
                    _alert(a, "halt", f"⛔ 已 {ev[1]:.0f} 秒沒有收到價格,超過上限,自動停機"
                           f"(狀態已存檔,持倉 {eng.base:.6f} 幣;排除問題後重跑同一指令可接續)")
                    rc = 3
                    break
                continue
            ticks += 1
            ev = wd.on_price(now)
            if ev:
                _alert(a, "recover", f"✔ 價格已恢復(中斷約 {ev[1]:.0f} 秒);中斷期間的走勢以直線近似")
            fills = eng.on_price(price, now)
            if fills:
                _print_fills(fills)
            if fills or now - last_save >= 5:  # 成交立刻存;否則至少每 5 秒存一次,避免逐筆寫檔
                save_state(eng, path, meta)
                last_save = now
            if now - last_status >= status_every:
                print(_CLR + _status(eng, price), end="" if _CLR else "\n", flush=True)
                last_status = now
            if eng.halted or (a.max_polls and ticks >= a.max_polls):
                break
    except KeyboardInterrupt:  # Ctrl+C / SIGTERM
        pass
    finally:  # 任何離開方式(含例外)都要存檔、收掉背景警報、還原 signal
        source.close()
        save_state(eng, path, meta)
        _flush_alerts()
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
    last = eng.last_price  # 迴圈變數 price 可能是心跳 None,結算一律用引擎記錄的最後價格
    print(f"\n[paper] 結束:權益 {eng.equity(last):.2f}  損益 {eng.equity(last) - cfg.invest:+.2f}")
    return rc


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

    sw = sub.add_parser("sweep", help="參數掃描(樣本內 / 樣本外)")
    sw.add_argument("--invest", type=float, default=1000)
    sw.add_argument("--fee", type=float, default=0.001)
    sw_src = sw.add_mutually_exclusive_group()
    sw_src.add_argument("--csv")
    sw_src.add_argument("--symbol", help="Binance 交易對;與 --csv 都沒給時預設 BTCUSDT")
    sw.add_argument("--interval", default="1h")
    sw.add_argument("--limit", type=int, default=1000)
    sw.add_argument("--grids-list", default="10,20,30,40,60", help="格數清單,逗號分隔")
    sw.add_argument("--range-pcts", default="5,10,15,20,30", help="起始價 ±% 清單")
    sw.add_argument("--modes", default="arithmetic,geometric")
    sw.add_argument("--split", type=float, default=0.7, help="樣本內比例,1 表示不切")
    sw.add_argument("--stop-loss-pct", type=float, default=None, help="停損:低於下緣再跌 N%")
    sw.add_argument("--rank-by", choices=["in", "out"], default="in", help="排序依據(預設樣本內,避免用樣本外挑參數)")
    sw.add_argument("--top", type=int, default=10)
    sw.set_defaults(fn=cmd_sweep)

    wf = sub.add_parser("walkforward", help="滾動視窗驗證(訓練段挑參數、測試段檢驗)")
    wf.add_argument("--invest", type=float, default=1000)
    wf.add_argument("--fee", type=float, default=0.001)
    wf_src = wf.add_mutually_exclusive_group()
    wf_src.add_argument("--csv")
    wf_src.add_argument("--symbol", help="Binance 交易對;與 --csv 都沒給時預設 BTCUSDT")
    wf.add_argument("--interval", default="1h")
    wf.add_argument("--limit", type=int, default=3000, help="K 線根數(>1000 自動分頁)")
    wf.add_argument("--train", type=int, default=720, help="訓練段根數")
    wf.add_argument("--test", type=int, default=240, help="測試段根數")
    wf.add_argument("--step", type=int, default=None, help="視窗前進根數(預設=test)")
    wf.add_argument("--grids-list", default="10,20,30,40")
    wf.add_argument("--range-pcts", default="5,10,15,20,30")
    wf.add_argument("--modes", default="arithmetic,geometric")
    wf.add_argument("--stop-loss-pct", type=float, default=None)
    wf.set_defaults(fn=cmd_walkforward)

    pp = sub.add_parser("paper", help="模擬盤(即時現價,不下真單)")
    _add_grid_args(pp)
    pp.add_argument("--symbol", required=True)
    pp.add_argument("--source", choices=["ws", "poll"], default="ws",
                    help="價格來源:ws=WebSocket 即時成交價(預設,連不上自動改輪詢)/ poll=定時輪詢")
    pp.add_argument("--interval", type=float, default=5, help="輪詢模式的間隔秒數")
    pp.add_argument("--stale-warn", type=float, default=15, help="超過 N 秒沒收到價格就警告(預設 15)")
    pp.add_argument("--stale-halt", type=float, default=300, help="超過 N 秒沒收到價格就自動停機,0=不停機(預設 300)")
    pp.add_argument("--alert-cmd", help="警報時執行的 shell 指令;訊息在環境變數 GRIDBOT_ALERT_MSG,等級在 GRIDBOT_ALERT_LEVEL(warn/halt/recover)")
    pp.add_argument("--state", help="狀態檔路徑(預設 state/paper_<SYMBOL>.json)")
    pp.add_argument("--reset", action="store_true", help="忽略並覆蓋既有狀態檔,重新開始")
    pp.add_argument("--max-polls", type=int, default=0, help="最多處理幾筆價格(ws 為成交筆數、poll 為輪詢次數),0 = 持續執行")
    pp.set_defaults(fn=cmd_paper)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, OSError) as e:  # 參數錯誤 / 檔案或網路錯誤
        print(f"錯誤:{e}", file=sys.stderr)
        return 2
