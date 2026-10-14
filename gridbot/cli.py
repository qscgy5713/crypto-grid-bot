from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from .backtest import run_backtest
from .data import (ALIVE, WS_CONNECT_TIMEOUT, Tick, fetch_klines, fetch_price, load_csv, poll_prices,
                   stream_prices, synthetic)
from .engine import GridConfig, GridEngine, breakeven_spacing, min_spacing
from .state import acquire_lock, load_state, save_state
from .sweep import run_sweep, run_walk_forward, validate_params, validate_walk_forward
from .watchdog import Watchdog
from .ws import WebSocketError

MIN_POLL_INTERVAL = 0.5  # 輪詢下限(秒),避免狂打 Binance REST 被限流 / 封 IP
WS_RETRY_SECS = 60.0  # WebSocket 放棄退回輪詢後,每隔這麼久再試著回到 WebSocket


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
    sp, be = min_spacing(cfg), breakeven_spacing(cfg.fee_rate)
    print(f"最小格距 {sp * 100:.3f}% (來回手續費打平需 {be * 100:.3f}%)")
    if sp <= be:
        print("⚠ 格距不足以覆蓋來回手續費,每次套利都會虧損,請減少格數或放大區間", file=sys.stderr)


def _check_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("--limit 必須 >= 1")


def cmd_backtest(a) -> int:
    cfg = _cfg(a)
    cfg.validate()
    _check_limit(a.limit)
    if a.csv:
        candles = load_csv(a.csv)
    elif a.symbol:
        candles = fetch_klines(a.symbol, a.interval, a.limit)
    else:
        mid = (cfg.lower + cfg.upper) / 2
        candles = synthetic(a.limit, mid, a.vol, a.seed)
        print(f"使用合成資料:{a.limit} 根,波動 {a.vol},seed={a.seed}")
    if not candles:
        raise ValueError("沒有可用的 K 線")
    _warn_spacing(cfg)
    r = run_backtest(cfg, candles)
    print(f"價格 {r.start_price:.4f} -> {r.end_price:.4f}  ({r.bars} 根 K 線)")
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
    validate_params(grids, pcts, modes, a.split, a.stop_loss_pct, a.invest, a.fee)  # 先驗參數,再打網路
    _check_limit(a.limit)
    if a.top < 1:
        raise ValueError("--top 必須 >= 1")
    candles = _load_candles(a)
    rows = run_sweep(candles, grids, pcts, modes, a.invest, a.fee, a.split, a.stop_loss_pct)
    if not rows:
        raise ValueError("所有組合的格距都不足以覆蓋手續費")
    has_oos = rows[0].oos is not None
    rank_out = a.rank_by == "out" and has_oos
    rows.sort(key=(lambda r: r.oos.pnl_pct) if rank_out else (lambda r: r.ins.pnl_pct), reverse=True)
    label = "樣本外" if rank_out else "樣本內"
    n_out = rows[0].oos.bars if has_oos else 0
    print(f"{len(candles)} 根 K 線,樣本內 {rows[0].ins.bars} / 樣本外 {n_out}  (依{label}損益排序,前 {a.top} 名)")
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
    validate_params(grids, pcts, modes, 1.0, a.stop_loss_pct, a.invest, a.fee)  # 先驗參數,再打網路
    validate_walk_forward(a.train, a.test, a.step)
    _check_limit(a.limit)
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


# =============================== paper ===============================

_CLR = "\r\033[K" if sys.stdout.isatty() else ""  # 非終端機(重導向)時不輸出控制碼


def _log(msg: str) -> None:
    print(f"{_CLR}[paper] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"{_CLR}[paper] {msg}", file=sys.stderr, flush=True)


def _print_fills(fills) -> None:
    for f in fills:
        _log(f"{f.side:4} @ {f.price:.4f} x {f.qty:.6f} {f.note}")


def _price_source(a):
    """ws:WebSocket;連續失敗就退回輪詢,每 WS_RETRY_SECS 秒再試回 WebSocket。

    第一次連線容許 3 次失敗;之後從輪詢回頭試 WebSocket 只探測 1 次(每次最多約 WS_CONNECT_TIMEOUT 秒),
    避免長時間拿不到價格而觸發斷線警報。真的連上並收到價格後,恢復為 3 次。"""
    if a.source != "ws":
        yield from poll_prices(a.symbol, a.interval, log=_err)
        return
    give_up = 3
    while True:
        got_price = False
        try:
            for ev in stream_prices(a.symbol, give_up_after=give_up, log=_log):
                if isinstance(ev, Tick):
                    got_price = True
                yield ev
            return
        except WebSocketError as e:
            _err(f"{e};改用輪詢(每 {a.interval:g} 秒),{WS_RETRY_SECS:g} 秒後再試 WebSocket")
        give_up = 3 if got_price else 1
        poll = poll_prices(a.symbol, a.interval, log=_err)
        t_end = time.monotonic() + WS_RETRY_SECS
        try:
            for ev in poll:
                yield ev
                if time.monotonic() >= t_end:
                    break
        finally:
            poll.close()


def _status(eng: GridEngine) -> str:
    price = eng.last_price
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
    now = time.monotonic()
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
    end = time.monotonic() + timeout
    for p, _ in _ALERTS:
        try:
            p.wait(max(0.05, end - time.monotonic()))
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
            _err(f"警報指令失敗:{e}")


class _StopFlag:
    """SIGINT / SIGTERM handler:第一次只設旗標,由迴圈在安全點(不在修改狀態中途)存檔結束;
    第二次才丟 KeyboardInterrupt 強制中斷(此時不存檔,保留上一份一致的存檔)。"""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum, frame) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        _log("收到停止訊號,處理完目前這一步就存檔結束(再按一次 Ctrl+C 強制結束,不存檔)")


def _install_signals(handler) -> dict:
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old[sig] = signal.signal(sig, handler)
        except ValueError:  # 非主執行緒不能設 signal handler
            pass
    return old


def cmd_paper(a) -> int:
    """模擬盤:只讀 Binance 公開現價(WebSocket 即時 / 輪詢),在本地模擬成交,不下任何真實訂單。

    狀態定期存檔;再次啟動會自動接續(參數須一致,否則請加 --reset)。
    """
    cfg = _cfg(a)
    cfg.validate()
    Watchdog(a.stale_warn, a.stale_halt, 0.0)  # 先驗參數,再打網路
    if not MIN_POLL_INTERVAL <= a.interval < a.stale_warn:
        raise ValueError(f"--interval ({a.interval:g}) 需介於 {MIN_POLL_INTERVAL:g} 秒與 --stale-warn ({a.stale_warn:g}) 之間;"
                         "太小會狂打 API 被限流,太大則輪詢間隔本身就會觸發斷線警報")
    if a.source == "ws" and a.stale_warn <= 2 * WS_CONNECT_TIMEOUT:
        raise ValueError(f"WebSocket 模式下 --stale-warn 需大於 {2 * WS_CONNECT_TIMEOUT:g} 秒,"
                         "否則一次重連就會觸發斷線警報")
    if a.max_polls < 0:
        raise ValueError("--max-polls 必須 >= 0")
    _warn_spacing(cfg)
    path = a.state or os.path.join("state", f"paper_{a.symbol}.json")
    lock = acquire_lock(path)  # 同一狀態檔同時只允許一個行程
    stop = _StopFlag()
    old_handlers = _install_signals(stop)
    try:
        return _run_paper(a, cfg, path, stop)
    finally:
        for sig, h in old_handlers.items():
            signal.signal(sig, h)
        if lock:
            lock.close()


def _run_paper(a, cfg: GridConfig, path: str, stop: _StopFlag) -> int:
    meta = {"symbol": a.symbol}
    price = fetch_price(a.symbol)
    if stop.requested:
        return 0
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
        _print_fills(eng.on_price(price, time.time(), gap=True))  # 停機期間的走勢沒看到
        save_state(eng, path, meta)
        if eng.halted:  # 停機期間價格已跌破停損:清倉後不必再進入價格迴圈
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
    ticks, rc, aborted = 0, 0, False
    clock = time.monotonic  # 計時一律用 monotonic,系統校時不會造成誤判
    wd = Watchdog(a.stale_warn, a.stale_halt, clock())
    last_save = last_status = clock()
    status_every = 1.0 if sys.stdout.isatty() else 30.0
    try:
        for ev in source:
            if stop.requested:
                break
            now = clock()
            if ev is None:  # 心跳:什麼都沒收到
                w = wd.on_idle(now)
                if w and w[0] == "warn":
                    _alert(a, "warn", f"⚠ 已 {w[1]:.0f} 秒沒有收到任何行情資料;持倉 {eng.base:.6f} 幣目前無人監控"
                           + (f",超過 {a.stale_halt:g} 秒將自動停機" if a.stale_halt else ""))
                elif w and w[0] == "halt":
                    _alert(a, "halt", f"⛔ 已 {w[1]:.0f} 秒沒有收到任何行情資料,超過上限,自動停機"
                           f"(狀態已存檔,持倉 {eng.base:.6f} 幣;排除問題後重跑同一指令可接續)")
                    rc = 3
                    break
                continue
            w = wd.on_price(now)  # 連線活著(有成交或有 ping)就算恢復
            if w:
                _alert(a, "recover", f"✔ 行情已恢復(中斷約 {w[1]:.0f} 秒);中斷期間的走勢以直線近似")
            if ev is ALIVE:
                continue
            ticks += 1
            fills = eng.on_price(ev.price, time.time(), gap=ev.gap)
            if fills:
                _print_fills(fills)
            if fills or now - last_save >= 5:  # 成交立刻存;否則至少每 5 秒存一次,避免逐筆寫檔
                save_state(eng, path, meta)
                last_save = now
            if now - last_status >= status_every:
                print(_CLR + _status(eng), end="" if _CLR else "\n", flush=True)
                last_status = now
            if eng.halted or (a.max_polls and ticks >= a.max_polls):
                break
    except KeyboardInterrupt:  # 第二次 Ctrl+C:可能打斷在狀態更新中途,不存檔
        aborted = True
    finally:
        source.close()
        if not aborted:
            save_state(eng, path, meta)
        _flush_alerts()
    last = eng.last_price
    note = "(強制結束,未存最後狀態;接續時以上一次存檔為準)" if aborted else ""
    print(f"\n[paper] 結束:權益 {eng.equity(last):.2f}  損益 {eng.equity(last) - cfg.invest:+.2f}{note}")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gridbot", description="現貨網格交易(回測 / 模擬盤)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="回測")
    _add_grid_args(bt)
    src = bt.add_mutually_exclusive_group()
    src.add_argument("--csv", help="CSV 路徑(ts,open,high,low,close)")
    src.add_argument("--symbol", help="Binance 交易對,如 BTCUSDT")
    bt.add_argument("--interval", default="1h", help="K 線週期,如 15m/1h/4h/1d(預設 1h,搭配 --symbol)")
    bt.add_argument("--limit", type=int, default=500, help="K 線根數(預設 500,>1000 自動分頁;合成資料時為產生根數)")
    bt.add_argument("--vol", type=float, default=0.01, help="合成資料每根 K 線報酬標準差(預設 0.01)")
    bt.add_argument("--seed", type=int, default=42, help="合成資料亂數種子(預設 42,同種子結果相同)")
    bt.set_defaults(fn=cmd_backtest)

    sw = sub.add_parser("sweep", help="參數掃描(樣本內 / 樣本外)")
    sw.add_argument("--invest", type=float, default=1000, help="本金(預設 1000)")
    sw.add_argument("--fee", type=float, default=0.001, help="單邊手續費率(預設 0.001)")
    sw_src = sw.add_mutually_exclusive_group()
    sw_src.add_argument("--csv", help="CSV 路徑(ts,open,high,low,close),與 --symbol 擇一")
    sw_src.add_argument("--symbol", help="Binance 交易對;與 --csv 都沒給時預設 BTCUSDT")
    sw.add_argument("--interval", default="1h", help="K 線週期(預設 1h)")
    sw.add_argument("--limit", type=int, default=1000, help="K 線根數(預設 1000,>1000 自動分頁)")
    sw.add_argument("--grids-list", default="10,20,30,40,60", help="格數清單,逗號分隔")
    sw.add_argument("--range-pcts", default="5,10,15,20,30", help="起始價 ±%% 清單")
    sw.add_argument("--modes", default="arithmetic,geometric", help="要掃描的模式,逗號分隔")
    sw.add_argument("--split", type=float, default=0.7, help="樣本內比例,1 表示不切")
    sw.add_argument("--stop-loss-pct", type=float, default=None, help="停損:低於下緣再跌 N%%")
    sw.add_argument("--rank-by", choices=["in", "out"], default="in", help="排序依據(預設樣本內,避免用樣本外挑參數)")
    sw.add_argument("--top", type=int, default=10, help="顯示前幾名(預設 10)")
    sw.set_defaults(fn=cmd_sweep)

    wf = sub.add_parser("walkforward", help="滾動視窗驗證(訓練段挑參數、測試段檢驗)")
    wf.add_argument("--invest", type=float, default=1000, help="本金(預設 1000)")
    wf.add_argument("--fee", type=float, default=0.001, help="單邊手續費率(預設 0.001)")
    wf_src = wf.add_mutually_exclusive_group()
    wf_src.add_argument("--csv", help="CSV 路徑(ts,open,high,low,close),與 --symbol 擇一")
    wf_src.add_argument("--symbol", help="Binance 交易對;與 --csv 都沒給時預設 BTCUSDT")
    wf.add_argument("--interval", default="1h", help="K 線週期(預設 1h)")
    wf.add_argument("--limit", type=int, default=3000, help="K 線根數(>1000 自動分頁)")
    wf.add_argument("--train", type=int, default=720, help="訓練段根數")
    wf.add_argument("--test", type=int, default=240, help="測試段根數")
    wf.add_argument("--step", type=int, default=None, help="視窗前進根數(預設=test)")
    wf.add_argument("--grids-list", default="10,20,30,40", help="格數清單,逗號分隔")
    wf.add_argument("--range-pcts", default="5,10,15,20,30", help="起始價 ±%% 清單")
    wf.add_argument("--modes", default="arithmetic,geometric", help="要掃描的模式,逗號分隔")
    wf.add_argument("--stop-loss-pct", type=float, default=None, help="停損:低於下緣再跌 N%%")
    wf.set_defaults(fn=cmd_walkforward)

    pp = sub.add_parser("paper", help="模擬盤(即時現價,不下真單)")
    _add_grid_args(pp)
    pp.add_argument("--symbol", required=True, help="Binance 交易對,如 BTCUSDT(必填)")
    pp.add_argument("--source", choices=["ws", "poll"], default="ws",
                    help="價格來源:ws=WebSocket 即時成交價(預設,連不上自動改輪詢)/ poll=定時輪詢")
    pp.add_argument("--interval", type=float, default=5,
                    help=f"輪詢間隔秒數(預設 5,下限 {MIN_POLL_INTERVAL:g};ws 退回輪詢時也用它,須小於 --stale-warn)")
    pp.add_argument("--stale-warn", type=float, default=30, help="超過 N 秒沒收到任何行情資料就警告(預設 30)")
    pp.add_argument("--stale-halt", type=float, default=300, help="超過 N 秒沒收到任何行情資料就自動停機,0=不停機(預設 300)")
    pp.add_argument("--alert-cmd", help="警報時執行的 shell 指令;訊息在環境變數 GRIDBOT_ALERT_MSG,等級在 GRIDBOT_ALERT_LEVEL(warn/halt/recover)")
    pp.add_argument("--state", help="狀態檔路徑(預設 state/paper_<SYMBOL>.json)")
    pp.add_argument("--reset", action="store_true", help="忽略並覆蓋既有狀態檔,重新開始")
    pp.add_argument("--max-polls", type=int, default=0,
                    help="處理幾筆成功取得的價格後結束(取價失敗不計),0 = 持續執行")
    pp.set_defaults(fn=cmd_paper)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, OSError) as e:  # 參數錯誤 / 檔案或網路錯誤
        print(f"錯誤:{e}", file=sys.stderr)
        return 2
