import unittest

from gridbot import GridConfig, GridEngine, build_levels
from gridbot.backtest import candle_path, run_backtest
from gridbot.data import synthetic


def _patch_price(**kw):
    """同一個 mock 同時取代 cli(起始價)與 data(輪詢)的 fetch_price。"""
    import contextlib
    from unittest import mock
    m = mock.Mock(**kw)
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch("gridbot.cli.fetch_price", m))
    stack.enter_context(mock.patch("gridbot.data.fetch_price", m))
    return stack, m


class FakeTime:
    """可控時鐘:sleep 只推進時間,不真的睡。同時取代 cli 與 data 的 time 模組。"""

    def __init__(self):
        import time as _t
        self._t = _t
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1.7e9 + self.now

    def sleep(self, s):
        self.now += s

    def strftime(self, *a):
        return self._t.strftime(*a)

    def gmtime(self, *a):
        return self._t.gmtime(*a)


def cfg(**kw):
    base = dict(lower=90, upper=110, grids=4, invest=1000, fee_rate=0.0)
    base.update(kw)
    return GridConfig(**base)


class LevelsTest(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(build_levels(cfg()), [90, 95, 100, 105, 110])

    def test_geometric_endpoints(self):
        lv = build_levels(cfg(mode="geometric"))
        self.assertAlmostEqual(lv[0], 90)
        self.assertAlmostEqual(lv[-1], 110)
        self.assertAlmostEqual(lv[1] / lv[0], lv[2] / lv[1])

    def test_validation(self):
        for bad in (dict(lower=0), dict(grids=1), dict(invest=0), dict(stop_loss=95), dict(mode="x")):
            with self.assertRaises(ValueError):
                cfg(**bad).validate()
        with self.assertRaises(ValueError):
            GridEngine(cfg(), 120)


class EngineTest(unittest.TestCase):
    def test_initial_orders_and_budget(self):
        e = GridEngine(cfg(fee_rate=0.001), 100.5)
        self.assertEqual(e.orders, {0: "buy", 1: "buy", 2: "buy", 4: "sell"})
        # 所有買單全成交後現金不會為負
        e.on_price(90)
        self.assertGreaterEqual(e.quote, -1e-9)

    def test_buy_then_sell_makes_profit(self):
        e = GridEngine(cfg(), 100.5)
        e.on_price(94)  # 穿越 level 2(100) 與 level 1(95)
        self.assertEqual([f.side for f in e.fills[1:]], ["buy", "buy"])
        self.assertEqual(e.orders[3], "sell")
        e.on_price(101)  # 賣在 level 2(100)
        self.assertEqual(e.cycles, 1)
        self.assertAlmostEqual(e.grid_profit, e.qty * 5)

    def test_always_grids_orders_and_inventory_matches_sells(self):
        e = GridEngine(cfg(grids=10, lower=90, upper=110, fee_rate=0.001), 101)
        for p in synthetic(500, 101, 0.01, seed=7):
            for x in p[1:]:
                e.on_price(min(max(x, 85), 115), p[0])
                self.assertEqual(len(e.orders), 10)
                sells = sum(1 for s in e.orders.values() if s == "sell")
                self.assertAlmostEqual(e.base, sells * e.qty, places=9)
                self.assertGreaterEqual(e.quote, -1e-9)

    def test_next_orders(self):
        e = GridEngine(cfg(), 100.5)
        self.assertEqual(e.next_orders(), (100, 110))  # 空檔在 105:買 90/95/100、賣 110
        e.on_price(94)  # 100、95 的買單成交,各自補上 105、100 的賣單
        self.assertEqual(e.next_orders(), (90, 100))

    def test_whipsaw_in_one_pass_does_not_double_fill(self):
        e = GridEngine(cfg(), 100.5)
        n = len(e.fills)
        e.on_price(100.2)
        self.assertEqual(len(e.fills), n)

    def test_out_of_range_holds_no_negative_balances(self):
        e = GridEngine(cfg(), 100.5)
        e.on_price(50)
        self.assertGreaterEqual(e.quote, -1e-9)
        e.on_price(500)
        self.assertGreaterEqual(e.base, -1e-9)
        self.assertAlmostEqual(e.base, 0, places=9)

    def test_stop_loss(self):
        e = GridEngine(cfg(stop_loss=80), 100.5)
        e.on_price(70)
        self.assertTrue(e.halted)
        self.assertEqual(e.base, 0)
        self.assertEqual(e.on_price(100), [])
        self.assertLess(e.equity(70), 1000)

    def test_equity_conserved_without_price_change_fees_zero(self):
        e = GridEngine(cfg(), 100.5)
        self.assertAlmostEqual(e.equity(100.5), 1000)


class BacktestTest(unittest.TestCase):
    def test_candle_path(self):
        self.assertEqual(candle_path(1, 3, 0, 2), [1, 0, 3, 2])
        self.assertEqual(candle_path(2, 3, 0, 1), [2, 3, 0, 1])

    def test_oscillation_profits(self):
        candles = []
        for t in range(40):
            o, c = (100.5, 100.5)
            candles.append((t, o, 109, 91, c))
        r = run_backtest(cfg(fee_rate=0.001), candles)
        self.assertGreater(r.cycles, 10)
        self.assertGreater(r.pnl, 0)

    def test_synthetic_deterministic(self):
        a = run_backtest(cfg(lower=50, upper=150), synthetic(300, 100, seed=1))
        b = run_backtest(cfg(lower=50, upper=150), synthetic(300, 100, seed=1))
        self.assertEqual(a, b)


class SweepTest(unittest.TestCase):
    def test_sweep_skips_thin_spacing_and_splits(self):
        from gridbot.sweep import run_sweep
        c = synthetic(400, 100, 0.01, seed=3)
        rows = run_sweep(c, [5, 400], [10], ["arithmetic"], 1000, 0.001, 0.7)
        self.assertEqual([r.grids for r in rows], [5])  # 400 格格距 < 手續費,被略過
        self.assertIsNotNone(rows[0].oos)
        # 樣本外的區間依樣本外起始價計算,不洩漏
        self.assertAlmostEqual(rows[0].oos.start_price, c[280][1])
        self.assertIsNone(run_sweep(c, [5], [10], ["arithmetic"], 1000, 0.001, 1.0)[0].oos)
        for kw in (dict(grids_list=[1]), dict(grids_list=[]), dict(pcts=[100]), dict(modes=["x"]),
                   dict(stop_loss_pct=0), dict(split=2)):
            args = dict(grids_list=[5], pcts=[10], modes=["arithmetic"], split=0.7, stop_loss_pct=None)
            args.update(kw)
            with self.assertRaises(ValueError, msg=str(kw)):
                run_sweep(c, args["grids_list"], args["pcts"], args["modes"], 1000, 0.001,
                          args["split"], args["stop_loss_pct"])
        with self.assertRaises(ValueError):
            run_sweep(c[:3], [5], [10], ["arithmetic"], 1000, 0.001, 0.7)


class WalkForwardTest(unittest.TestCase):
    def test_folds_and_no_leak(self):
        from gridbot.sweep import run_walk_forward
        c = synthetic(1000, 100, 0.01, seed=5)
        folds = run_walk_forward(c, [5, 10], [10, 20], ["arithmetic"], 1000, 0.001, 400, 100)
        self.assertEqual(len(folds), 6)  # (1000-400)/100
        for f in folds:
            s = 400 + (f.index - 1) * 100
            self.assertEqual(f.test_start, c[s][0])  # 測試段緊接訓練段
            self.assertAlmostEqual(f.test.start_price, c[s][1])  # 區間以測試段起始價定義

    def test_train_select_is_best_in_train(self):
        from gridbot.sweep import run_sweep, run_walk_forward
        c = synthetic(500, 100, 0.01, seed=9)
        f = run_walk_forward(c, [5, 10, 20], [10, 20], ["arithmetic", "geometric"], 1000, 0.001, 300, 100)[0]
        rows = run_sweep(c[:300], [5, 10, 20], [10, 20], ["arithmetic", "geometric"], 1000, 0.001, 1.0)
        self.assertAlmostEqual(f.train.pnl_pct, max(r.ins.pnl_pct for r in rows))

    def test_errors(self):
        from gridbot.sweep import run_walk_forward
        c = synthetic(100, 100)
        for kw in (dict(train=90, test=20), dict(train=1, test=10), dict(train=50, test=10, step=0)):
            with self.assertRaises(ValueError, msg=str(kw)):
                run_walk_forward(c, [5], [10], ["arithmetic"], 1000, 0.001, **kw)


class PersistenceTest(unittest.TestCase):
    def _prices(self):
        return [x for c in synthetic(300, 100, 0.01, seed=11) for x in c[1:]]

    def test_roundtrip_resume_equals_uninterrupted(self):
        import json
        prices = [min(max(p, 85), 115) for p in self._prices()]
        full = GridEngine(cfg(grids=10, fee_rate=0.001), 100.5)
        half = GridEngine(cfg(grids=10, fee_rate=0.001), 100.5)
        mid = len(prices) // 2
        for p in prices:
            full.on_price(p)
        for p in prices[:mid]:
            half.on_price(p)
        resumed = GridEngine.from_dict(json.loads(json.dumps(half.to_dict())))  # 經過真正的 JSON
        for p in prices[mid:]:
            resumed.on_price(p)
        self.assertEqual(resumed.orders, full.orders)
        self.assertAlmostEqual(resumed.quote, full.quote)
        self.assertAlmostEqual(resumed.base, full.base)
        self.assertEqual(resumed.cycles, full.cycles)
        self.assertAlmostEqual(resumed.grid_profit, full.grid_profit)

    def test_corrupt_state_rejected(self):
        d = GridEngine(cfg(), 100.5).to_dict()
        import copy
        bad = []
        for mut in (
            lambda x: x.update(version=99),
            lambda x: x.pop("orders"),
            lambda x: x["orders"].update({"99": "buy"}),
            lambda x: x["orders"].update({"0": "hold"}),
            lambda x: x["orders"].pop("0"),  # 掛單數不對
            lambda x: x.update(base=x["base"] + 5),  # 持倉與賣單不符
            lambda x: x.update(quote=-100),
            lambda x: x.update(quote=float("nan")),
            lambda x: x["cfg"].update(lower=-1),
        ):
            x = copy.deepcopy(d)
            mut(x)
            bad.append(x)
        for x in bad:
            with self.assertRaises(ValueError, msg=str(x)[:80]):
                GridEngine.from_dict(x)

    def test_save_load_atomic_and_garbage(self):
        import os, tempfile
        from gridbot.state import load_state, save_state
        with tempfile.TemporaryDirectory() as t:
            path = os.path.join(t, "sub", "s.json")
            e = GridEngine(cfg(), 100.5)
            save_state(e, path, {"symbol": "X"})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["s.json"])  # 無殘留暫存檔
            e2, meta = load_state(path)
            self.assertEqual(meta, {"symbol": "X"})
            self.assertEqual(e2.orders, e.orders)
            with open(path, "w") as fh:
                fh.write("{not json")
            with self.assertRaises(ValueError):
                load_state(path)

    def test_fills_capped_but_counters_kept(self):
        from gridbot import engine as m
        e = GridEngine(cfg(grids=4, fee_rate=0.0), 100.5)
        for _ in range(400):
            e.on_price(91)
            e.on_price(109)
        self.assertGreater(len(e.fills), m.MAX_SAVED_FILLS)
        r = GridEngine.from_dict(e.to_dict())
        self.assertEqual(len(r.fills), m.MAX_SAVED_FILLS)
        self.assertEqual(r.cycles, e.cycles)


class PaperCliTest(unittest.TestCase):
    ARGS = ["paper", "--symbol", "TESTUSDT", "--source", "poll", "--lower", "90", "--upper", "110", "--grids", "4",
            "--invest", "1000", "--fee", "0", "--interval", "0.5"]

    def _run(self, prices, path, extra=()):
        from unittest import mock
        from gridbot.cli import main
        stack, _ = _patch_price(side_effect=prices)
        with stack, mock.patch("gridbot.data.time.sleep"):
            return main(self.ARGS + ["--state", path, "--max-polls", str(len(prices) - 1), *extra])

    def test_resume_and_guards(self):
        import contextlib, io, os, tempfile
        from gridbot.state import load_state
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()):
            path = os.path.join(t, "s.json")
            self.assertEqual(self._run([100.5, 94, 96], path), 0)
            e1, _ = load_state(path)
            self.assertEqual(e1.cycles, 0)
            n = len(e1.fills)
            self.assertGreater(n, 1)  # 已有買單成交
            # 重啟:價格 101 -> 沿途賣單成交,證明接續的是存檔的掛單而不是重建
            self.assertEqual(self._run([101, 101], path), 0)
            e2, _ = load_state(path)
            self.assertEqual(e2.cycles, 1)
            self.assertGreater(len(e2.fills), n)
            # 參數不同 -> 拒絕
            from gridbot.cli import main
            from unittest import mock
            with mock.patch("gridbot.cli.fetch_price", return_value=100.5), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = main(self.ARGS[:-6] + ["--grids", "8", "--invest", "1000", "--fee", "0",
                                            "--interval", "0.5", "--state", path, "--max-polls", "1"])
            self.assertEqual(rc, 2)
            # --reset -> 重新開始
            self.assertEqual(self._run([100.5, 100.5], path, ["--reset"]), 0)
            e3, _ = load_state(path)
            self.assertEqual(e3.cycles, 0)


def _frame(opcode, payload=b"", fin=True):
    """伺服器端 frame(不遮罩)。"""
    import struct
    n = len(payload)
    h = bytes([(0x80 if fin else 0) | opcode])
    if n < 126:
        h += bytes([n])
    elif n < 65536:
        h += bytes([126]) + struct.pack("!H", n)
    else:
        h += bytes([127]) + struct.pack("!Q", n)
    return h + payload


class FakeWsServer:
    """本機單執行緒假 WS 伺服器:依序處理多個連線,每個連線執行一個 script(conn)。"""

    def __init__(self, scripts, bad_accept=False):
        import socket, threading
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.scripts, self.bad_accept, self.received = scripts, bad_accept, []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def url(self):
        return f"ws://127.0.0.1:{self.port}/ws/test"

    def _run(self):
        import base64, hashlib
        from gridbot.ws import GUID
        for script in self.scripts:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # 伺服器已關閉
            conn.settimeout(5)
            req = b""
            try:
                while b"\r\n\r\n" not in req:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise OSError("客戶端在握手中途斷線")
                    req += chunk
            except OSError:
                conn.close()
                continue
            key = [l.split(b":")[1].strip() for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest()).decode()
            if self.bad_accept:
                acc = "wrong"
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n").encode())
            try:
                script(conn, self)
            finally:
                conn.close()

    def close(self):
        self.sock.close()


def _read_client_frame(conn):
    """讀一個客戶端 frame,回傳 (opcode, 解遮罩後 payload);同時驗證它有遮罩。"""
    import struct
    b1, b2 = conn.recv(2)
    assert b2 & 0x80, "客戶端 frame 必須遮罩"
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", conn.recv(2))[0]
    mask = conn.recv(4)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


class WebSocketTest(unittest.TestCase):
    def test_frames_ping_fragments_large_and_close(self):
        from gridbot.ws import WebSocket

        big = "x" * 70000  # 走 8 byte 長度欄位

        def script(conn, srv):
            conn.sendall(_frame(0x9, b"hi"))  # ping
            conn.sendall(_frame(0x1, "你好".encode(), fin=False)[:1] + bytes([6]) + "你好".encode())  # 第一片
            conn.sendall(_frame(0x0, b"-ok", fin=True))  # 續片
            conn.sendall(_frame(0x1, b"m" * 200))  # 126 長度欄位
            conn.sendall(_frame(0x1, big.encode()))
            srv.received.append(_read_client_frame(conn))  # 應收到 pong
            conn.sendall(_frame(0x8, b"\x03\xe8"))  # close
            srv.received.append(_read_client_frame(conn))  # 應回 close

        srv = FakeWsServer([script])

        self.addCleanup(srv.close)
        ws = WebSocket(srv.url(), timeout=5)
        ws.connect()
        self.assertEqual(ws.recv(), "你好-ok")
        self.assertEqual(ws.recv(), "m" * 200)
        self.assertEqual(ws.recv(), big)
        self.assertIsNone(ws.recv())
        ws.close()
        srv.thread.join(5)
        self.assertEqual(srv.received[0], (0xA, b"hi"))
        self.assertEqual(srv.received[1][0], 0x8)
        srv.close()

    def test_bad_handshake_rejected(self):
        from gridbot.ws import WebSocket, WebSocketError
        srv = FakeWsServer([lambda c, s: None], bad_accept=True)
        self.addCleanup(srv.close)
        with self.assertRaises(WebSocketError):
            WebSocket(srv.url(), timeout=5).connect()
        srv.close()

    def test_bad_url(self):
        from gridbot.ws import WebSocket
        with self.assertRaises(ValueError):
            WebSocket("http://x")


def nxt(g):
    """取下一個 Tick(價格),略過心跳 None 與 ALIVE。"""
    from gridbot.data import Tick
    while True:
        v = next(g)
        if isinstance(v, Tick):
            return v


class StreamPricesTest(unittest.TestCase):
    def test_parse_skip_and_reconnect(self):
        from unittest import mock
        from gridbot.data import stream_prices

        def first(conn, srv):  # 一筆非成交訊息 + 一筆成交,然後直接掛斷
            conn.sendall(_frame(0x1, b'{"result":null}'))
            conn.sendall(_frame(0x1, b'not json'))
            conn.sendall(_frame(0x1, b'{"e":"trade","p":"100.5"}'))

        def second(conn, srv):
            conn.sendall(_frame(0x1, b'{"e":"trade","p":"101.25"}'))
            conn.recv(1)  # 等到客戶端關閉

        srv = FakeWsServer([first, second])

        self.addCleanup(srv.close)
        logs = []
        with mock.patch("gridbot.data.time.sleep") as sl:
            g = stream_prices("X", url=srv.url(), log=logs.append)
            alive = []
            from gridbot.data import ALIVE, Tick
            while True:  # 非成交訊息應以 ALIVE 回報(連線活著),不是被吞掉
                v = next(g)
                if v is ALIVE:
                    alive.append(v)
                if isinstance(v, Tick):
                    break
            self.assertEqual(len(alive), 2)
            self.assertEqual(v, Tick(100.5, gap=True))  # 連線後第一筆:跳空
            self.assertEqual(nxt(g), Tick(101.25, gap=True))  # 重連後第一筆:跳空
            g.close()
        sl.assert_called_once_with(1.0)
        self.assertTrue(any("重連" in m for m in logs))
        srv.close()

    def test_give_up_after(self):
        from unittest import mock
        from gridbot.data import stream_prices
        from gridbot.ws import WebSocketError
        srv = FakeWsServer([])
        self.addCleanup(srv.close)
        port = srv.port
        srv.close()  # 沒人監聽 -> 連線被拒
        with mock.patch("gridbot.data.time.sleep"):
            with self.assertRaises(WebSocketError):
                nxt(stream_prices("X", url=f"ws://127.0.0.1:{port}/", give_up_after=3))


class WatchdogTest(unittest.TestCase):
    def test_warn_repeat_halt_recover(self):
        from gridbot.watchdog import Watchdog
        w = Watchdog(10, 100, now=0, repeat=30)
        self.assertIsNone(w.on_idle(5))
        self.assertEqual(w.on_idle(10), ("warn", 10))
        self.assertIsNone(w.on_idle(20))  # 重複提醒要等 repeat
        self.assertEqual(w.on_idle(40), ("warn", 40))
        self.assertEqual(w.on_idle(100), ("halt", 100))
        self.assertEqual(w.on_price(50), ("recover", 50))
        self.assertIsNone(w.on_price(51))  # 沒警告過就不報恢復
        self.assertIsNone(w.on_idle(55))  # 重新計時

    def test_no_halt_when_zero_and_validation(self):
        from gridbot.watchdog import Watchdog
        w = Watchdog(1, 0, now=0)
        self.assertEqual(w.on_idle(10**6)[0], "warn")
        for bad in ((0, 0), (10, 5), (10, 10), (-1, 0)):
            with self.assertRaises(ValueError):
                Watchdog(bad[0], bad[1], 0)


class StreamIdleTest(unittest.TestCase):
    def test_silent_connection_heartbeats_then_reconnects(self):
        import time as t
        from unittest import mock
        from gridbot.data import stream_prices

        def silent(conn, srv):  # 連上後完全不出聲,直到客戶端自己關閉(不用 sleep,因為測試會 mock time.sleep)
            conn.recv(1)

        def talks(conn, srv):
            conn.sendall(_frame(0x1, b'{"p":"7"}'))
            conn.recv(1)

        srv = FakeWsServer([silent, talks])

        self.addCleanup(srv.close)
        logs = []
        g = stream_prices("X", url=srv.url(), idle_timeout=0.3, tick=0.05, log=logs.append)
        beats = 0
        from gridbot.data import Tick
        with mock.patch("gridbot.data.time.sleep"):
            while True:
                v = next(g)
                if v is None:
                    beats += 1
                elif isinstance(v, Tick):
                    break
        g.close()
        self.assertEqual(v.price, 7.0)
        self.assertGreater(beats, 3)  # 靜默期間持續送心跳
        self.assertTrue(any("沒有任何資料" in m for m in logs))
        srv.close()

    def test_ping_only_does_not_block_heartbeat(self):
        import time as t
        from gridbot.data import stream_prices

        def pings(conn, srv):
            try:
                for _ in range(3):
                    conn.sendall(_frame(0x9, b"p"))
                    t.sleep(0.1)
                    _read_client_frame(conn)  # pong
            except OSError:
                pass  # 客戶端提早關閉

        srv = FakeWsServer([pings])

        self.addCleanup(srv.close)
        g = stream_prices("X", url=srv.url(), idle_timeout=5, tick=0.05)
        from gridbot.data import ALIVE
        self.assertIn(next(g), (None, ALIVE))  # ping 不該卡住迴圈:回報「連線活著」或心跳
        g.close()
        srv.close()


class PaperStaleTest(unittest.TestCase):
    def test_outage_warns_alerts_and_halts_with_state_saved(self):
        import contextlib, io, os, tempfile
        from unittest import mock
        from gridbot.cli import main
        from gridbot.state import load_state
        calls = {"n": 0}

        def price(_):
            calls["n"] += 1
            if calls["n"] == 1:
                return 100.5
            raise OSError("down")

        ft = FakeTime()
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            log = os.path.join(t, "alerts.txt")
            stack, _ = _patch_price(side_effect=price)
            with stack, mock.patch("gridbot.cli.time", ft), mock.patch("gridbot.data.time", ft):
                rc = main(PaperCliTest.ARGS + ["--state", os.path.join(t, "s.json"),
                                               "--stale-warn", "2", "--stale-halt", "4",
                                               "--alert-cmd", f'echo "$GRIDBOT_ALERT_LEVEL" >> {log}'])
            self.assertEqual(rc, 3)
            with open(log) as fh:
                self.assertCountEqual(fh.read().split(), ["warn", "halt"])  # 背景並行執行,順序不保證
            e, _ = load_state(os.path.join(t, "s.json"))
            self.assertFalse(e.halted)  # 只是停機,不是停損,重啟可接續
            self.assertIn("自動停機", out.getvalue())

    def test_bad_thresholds_rejected(self):
        import contextlib, io
        from gridbot.cli import main
        with contextlib.redirect_stderr(io.StringIO()):
            rc = main(PaperCliTest.ARGS + ["--stale-warn", "30", "--stale-halt", "10"])
        self.assertEqual(rc, 2)


class ReviewFixesTest(unittest.TestCase):
    # #6 分片中途插入 ping,已收的分片不能遺失
    def test_ws_ping_between_fragments_keeps_parts(self):
        from gridbot.ws import WebSocket

        def script(conn, srv):
            conn.sendall(_frame(0x1, b"hello ", fin=False))
            conn.sendall(_frame(0x9, b"x"))
            _read_client_frame(conn)  # pong
            conn.sendall(_frame(0x0, b"world", fin=True))
            conn.recv(1)

        srv = FakeWsServer([script])

        self.addCleanup(srv.close)
        ws = WebSocket(srv.url(), timeout=5)
        ws.connect()
        got = [ws.recv(idle_return=True) for _ in range(2)]
        self.assertEqual(got, ["", "hello world"])
        ws.close()
        srv.close()

    # #8 記憶體中的 fills 有上限,但買賣計數正確、且會存檔還原
    def test_fills_trimmed_in_memory_counters_exact(self):
        from gridbot import engine as m
        e = GridEngine(cfg(grids=4, fee_rate=0.0), 100.5)
        for _ in range(600):
            e.on_price(91)
            e.on_price(109)
        self.assertLessEqual(len(e.fills), 2 * m.MAX_SAVED_FILLS)
        r = GridEngine.from_dict(e.to_dict())
        self.assertEqual((r.buy_count, r.cycles), (e.buy_count, e.cycles))
        self.assertGreater(e.buy_count, m.MAX_SAVED_FILLS)  # 計數不受修剪影響

    # #3 狀態檔鎖
    def test_state_lock_exclusive(self):
        import os, tempfile
        from gridbot.state import acquire_lock
        with tempfile.TemporaryDirectory() as t:
            path = os.path.join(t, "s.json")
            first = acquire_lock(path)
            with self.assertRaises(ValueError):
                acquire_lock(path)
            first.close()
            acquire_lock(path).close()  # 釋放後可再取得

    # #1 輪詢間隔不得 >= stale-warn
    def test_interval_must_be_below_stale_warn(self):
        import contextlib, io
        from gridbot.cli import main
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = main(PaperCliTest.ARGS[:-2] + ["--interval", "40"])  # >= 預設 stale-warn 30
        self.assertEqual(rc, 2)
        self.assertIn("stale-warn", err.getvalue())
        for bad in ("0", "0.1"):  # 低於下限:會狂打 API
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(PaperCliTest.ARGS[:-2] + ["--interval", bad]), 2)

    # #2 接續時價格已跌破停損:清倉後直接結束,不進價格迴圈
    def test_resume_below_stop_loss_exits_immediately(self):
        import contextlib, io, os, tempfile
        from unittest import mock
        from gridbot.cli import main
        from gridbot.state import load_state
        args = PaperCliTest.ARGS + ["--stop-loss", "80"]
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            path = os.path.join(t, "s.json")
            stack, _ = _patch_price(side_effect=[100.5, 100.5])
            with stack, mock.patch("gridbot.data.time.sleep"):
                self.assertEqual(main(args + ["--state", path, "--max-polls", "1"]), 0)
            stack, fp = _patch_price(side_effect=[70.0])  # 只允許一次取價
            with stack:
                self.assertEqual(main(args + ["--state", path]), 0)
            self.assertEqual(fp.call_count, 1)
            e, _ = load_state(path)
            self.assertTrue(e.halted)
            self.assertEqual(e.base, 0)
            self.assertEqual(e.fills[-1].price, 70.0)  # 停機期間跌破:以現價而非停損價出場
            self.assertIn("停損", out.getvalue())

    # #4 退回輪詢後會再試 WebSocket
    def test_ws_retried_after_fallback(self):
        import argparse
        from unittest import mock
        from gridbot import cli
        from gridbot.ws import WebSocketError
        calls = {"ws": 0}

        def fake_stream(symbol, **kw):
            calls["ws"] += 1
            if calls["ws"] == 1:
                raise WebSocketError("down")
            yield 5.0

        def fake_poll(*a, **kw):
            yield 1.0
            yield 2.0

        a = argparse.Namespace(source="ws", symbol="X", interval=1)
        with mock.patch.object(cli, "stream_prices", fake_stream), mock.patch.object(cli, "poll_prices", fake_poll), \
             mock.patch.object(cli, "WS_RETRY_SECS", 0), mock.patch("sys.stderr", new=__import__("io").StringIO()):
            self.assertEqual(list(cli._price_source(a)), [1.0, 5.0])

    # #5 SIGTERM 要存檔並正常結束,且還原原本的 handler
    def test_sigterm_saves_state_and_restores_handler(self):
        import contextlib, io, os, signal, tempfile
        from unittest import mock
        from gridbot.cli import main
        from gridbot.state import load_state
        n = {"i": 0}

        def price(_):
            n["i"] += 1
            if n["i"] == 1:
                return 100.5
            if n["i"] == 2:
                return 100.4  # 不跨價位線:不會觸發成交存檔
            os.kill(os.getpid(), signal.SIGTERM)
            return 100.3

        before = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()):
            path = os.path.join(t, "s.json")
            stack, _ = _patch_price(side_effect=price)
            with stack, mock.patch("gridbot.data.time.sleep"):
                self.assertEqual(main(PaperCliTest.ARGS + ["--state", path]), 0)
            e, _ = load_state(path)
            # 訊號只設旗標:迴圈在安全點結束,收到訊號後送來的 100.3 不再處理;收尾存檔寫入 100.4
            self.assertAlmostEqual(e.last_price, 100.4)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual(signal.getsignal(signal.SIGINT), before_int)

    # #7 警報指令不阻塞;結束時逾時會被砍掉
    def test_alert_nonblocking_and_flush_kills(self):
        import time as t
        from gridbot import cli
        t0 = t.time()
        cli._spawn_alert("sleep 5", {})
        self.assertLess(t.time() - t0, 0.5)
        t1 = t.time()
        cli._flush_alerts(timeout=0.3)
        self.assertLess(t.time() - t1, 2)
        self.assertEqual(cli._ALERTS, [])

    # #9 --csv 與 --symbol 互斥
    def test_csv_and_symbol_mutually_exclusive(self):
        import contextlib, io
        from gridbot.cli import main
        for cmd in ("sweep", "walkforward"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                main([cmd, "--csv", "a.csv", "--symbol", "ETHUSDT"])
            self.assertEqual(cm.exception.code, 2)


class ExecutionModelTest(unittest.TestCase):
    def test_touch_does_not_fill_cross_does(self):
        e = GridEngine(cfg(), 100.5)  # 買 90/95/100、賣 110
        self.assertEqual(e.on_price(100), [])  # 剛好碰到 100:不成交
        self.assertEqual([f.price for f in e.on_price(99.9)], [100])  # 穿過才成交
        self.assertEqual(e.on_price(105), [])  # 剛好碰到補上的 105 賣單:不成交
        self.assertEqual([f.price for f in e.on_price(105.1)], [105])

    def test_stop_loss_gap_fills_at_market_continuous_at_stop(self):
        for gap, want in ((False, 80), (True, 70)):
            e = GridEngine(cfg(stop_loss=80), 100.5)
            fills = e.on_price(70, gap=gap)
            self.assertEqual(fills[-1].note, "停損清倉")
            self.assertEqual(fills[-1].price, want)
        # 跳空出場的權益一定不優於連續出場
        a, b = GridEngine(cfg(stop_loss=80), 100.5), GridEngine(cfg(stop_loss=80), 100.5)
        a.on_price(70)
        b.on_price(70, gap=True)
        self.assertLess(b.equity(70), a.equity(70))

    def test_fetch_klines_drops_forming_candle_and_backfills(self):
        from unittest import mock
        from urllib.parse import parse_qs, urlsplit
        from gridbot import data
        H = 3600_000
        NOW = 10_000 * H  # 第 10000 根是形成中的那根
        calls = []

        def fake_get(url):  # 依 limit / endTime 回傳「到 endTime 為止」的最近 limit 根
            q = parse_qs(urlsplit(url).query)
            n = int(q["limit"][0])
            end_idx = (int(q["endTime"][0]) // H) if "endTime" in q else NOW // H
            calls.append(n)
            return [[i * H, "1", "2", "0.5", str(i), "0", (i + 1) * H - 1] for i in range(end_idx - n + 1, end_idx + 1)]

        with mock.patch.object(data, "_get", side_effect=fake_get):
            k = data.fetch_klines("X", "1h", 3)
        self.assertEqual([c[4] for c in k], [9997.0, 9998.0, 9999.0])  # 不含形成中的 10000
        self.assertEqual(calls, [4])  # 多要一根,一次請求就夠

        calls.clear()
        with mock.patch.object(data, "_get", side_effect=fake_get):
            k = data.fetch_klines("X", "1h", 1001)
        self.assertEqual(len(k), 1001)
        self.assertEqual(k[-1][4], 9999.0)
        self.assertEqual([c[4] for c in k], [float(i) for i in range(8999, 10000)])  # 連續、無重複
        self.assertEqual(calls, [1000, 2])


class HelpTest(unittest.TestCase):
    def test_every_subcommand_help_renders(self):
        import contextlib, io
        from gridbot.cli import main
        for cmd in ("backtest", "sweep", "walkforward", "paper"):
            with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
                main([cmd, "-h"])
            self.assertEqual(cm.exception.code, 0, cmd)
            self.assertIn("--", out.getvalue())


class DeepReviewTest(unittest.TestCase):
    # #1 金額大 / 價格極低時,最後一張單不能因浮點誤差被跳過
    def test_large_invest_and_tiny_price_fill_every_order(self):
        cases = [
            (dict(lower=75000, upper=92000, grids=g, invest=inv, fee_rate=0.001, mode=m), start)
            for g in (7, 20, 33) for inv in (1e4, 1e5, 1e6) for m in ("arithmetic", "geometric")
            for start in (75001.3, 83443.8, 91999.9)
        ] + [
            (dict(lower=1e-5, upper=2e-5, grids=g, invest=inv, fee_rate=0.001, mode=m), 1.37e-5)
            for g in (7, 20, 33) for inv in (1e3, 1e5) for m in ("arithmetic", "geometric")
        ]
        for kw, start in cases:
            c = GridConfig(**kw)
            e = GridEngine(c, start)
            e.on_price(c.lower * 0.5)  # 跌穿下緣:所有買單都該成交
            self.assertEqual(set(e.orders.values()), {"sell"}, (kw, start))
            self.assertGreaterEqual(e.quote, 0)
            e.on_price(c.upper * 2)  # 漲穿上緣:所有賣單都該成交
            self.assertEqual(set(e.orders.values()), {"buy"}, (kw, start))
            self.assertAlmostEqual(e.base, 0, delta=e.qty * 1e-6)
            self.assertEqual(len(e.orders), c.grids)

    # #16 端點精確,起始價貼著上緣也不會 StopIteration
    def test_levels_endpoints_exact(self):
        import math
        for m in ("arithmetic", "geometric"):
            for lo, hi, g in ((75000, 92000, 7), (0.1, 0.3, 33), (1e-5, 3e-5, 11)):
                c = GridConfig(lo, hi, g, 1000, m)
                lv = build_levels(c)
                self.assertEqual((lv[0], lv[-1]), (lo, hi))
                GridEngine(c, math.nextafter(hi, 0) if hasattr(math, "nextafter") else hi * (1 - 1e-15))

    # #15 打平格距是 2f/(1-f),不是 2f
    def test_breakeven_spacing(self):
        from gridbot.engine import breakeven_spacing, spacing_covers_fees
        f = 0.001
        self.assertAlmostEqual(breakeven_spacing(f), 2 * f / (1 - f))
        # 格距 0.2001%:大於 2f 但小於真正打平點 -> 不該視為足夠
        c = GridConfig(100, 100 * 1.002001 ** 2, 2, 1000, "geometric", f)
        self.assertFalse(spacing_covers_fees(c))
        # 實際來回一次確實虧
        e = GridEngine(c, 100.1)
        e.on_price(99.99)
        e.on_price(c.upper + 1)
        self.assertLess(e.grid_profit, 0)

    # #17 撮合中途有一張被跳過,不能覆蓋到它
    def test_skipped_fill_aborts_round(self):
        e = GridEngine(cfg(), 100.5)  # 買 90/95/100
        orig = e._fill_buy
        e._fill_buy = lambda i, ts: None if i == 2 else orig(i, ts)
        self.assertEqual(e.on_price(94), [])
        self.assertEqual(e.orders, {0: "buy", 1: "buy", 2: "buy", 4: "sell"})

    # #11 回測:K 線之間跳空跌破停損,以開盤價出場而不是停損價
    def test_backtest_gap_between_candles(self):
        from gridbot.backtest import run_backtest
        c = cfg(stop_loss=80)
        gapped = [(0, 100.5, 101, 100.2, 100.5), (1, 70, 71, 69, 70)]
        continuous = [(0, 100.5, 101, 100.2, 100.5), (1, 100.5, 101, 69, 70)]
        g, k = run_backtest(c, gapped), run_backtest(c, continuous)
        self.assertTrue(g.halted and k.halted)
        self.assertLess(g.equity, k.equity)

    # #3 / #13 CSV
    def test_load_csv_strict(self):
        import os, tempfile
        from gridbot.data import load_csv

        def load(text):
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
                fh.write(text)
            self.addCleanup(os.unlink, fh.name)
            return load_csv(fh.name)

        ok = load("ts,open,high,low,close\n\n1700000000000,1,2,0.5,1.5\n\n1700000060000,1.5,2,1,1.2\n")
        self.assertEqual([r[0] for r in ok], [1700000000.0, 1700000060.0])  # 空白行略過、毫秒轉秒
        for bad in (
            "ts,o,h,l,c\n1,1,2,0.5,1.5\nx,1,2,0.5,1.5\n",  # 中間的壞列不能被當標題略過
            "1,1,2,0.5\n",  # 欄位不足
            "1,1,2,nan,1.5\n",
            "1,1,2,0.5,3\n",  # close > high
            "2,1,2,0.5,1.5\n1,1,2,0.5,1.5\n",  # 時間倒退
            "\n\n",
        ):
            with self.assertRaises(ValueError, msg=bad):
                load(bad)

    # #12 / #14 參數錯誤要在打網路之前就擋下
    def test_validation_before_network(self):
        import contextlib, io
        from unittest import mock
        from gridbot.cli import main
        boom = mock.Mock(side_effect=AssertionError("不該打網路"))
        with mock.patch("gridbot.cli.fetch_klines", boom), contextlib.redirect_stderr(io.StringIO()):
            for argv in (["sweep", "--stop-loss-pct", "100"], ["sweep", "--fee", "0.5"], ["sweep", "--limit", "0"],
                         ["walkforward", "--step", "0"], ["walkforward", "--train", "1"],
                         ["backtest", "--symbol", "X", "--lower", "1", "--upper", "2", "--limit", "0"]):
                self.assertEqual(main(argv), 2, argv)
            self.assertEqual(main(["backtest", "--lower", "90", "--upper", "110", "--limit", "0"]), 2)
        boom.assert_not_called()

    # #4 第一次訊號只設旗標,第二次才強制中斷
    def test_stop_flag(self):
        import contextlib, io
        from gridbot.cli import _StopFlag
        f = _StopFlag()
        with contextlib.redirect_stdout(io.StringIO()):
            f(2, None)
        self.assertTrue(f.requested)
        with self.assertRaises(KeyboardInterrupt):
            f(2, None)

    # #7 輪詢的每一筆都標記跳空
    def test_poll_ticks_marked_gap(self):
        from unittest import mock
        from gridbot.data import Tick, poll_prices
        with mock.patch("gridbot.data.fetch_price", side_effect=[1.0, 2.0]), mock.patch("gridbot.data.time.sleep"):
            g = poll_prices("X", 0.5)
            got = [v for v in (next(g) for _ in range(4)) if v is not None]
        self.assertEqual(got, [Tick(1.0, True), Tick(2.0, True)])

    # #6 / #8 連線活著(ALIVE)但沒成交不算斷線;計時用 monotonic,牆上時鐘亂跳不影響
    def test_alive_without_trades_is_not_outage(self):
        import contextlib, io, os, tempfile
        from unittest import mock
        from gridbot import cli
        from gridbot.data import ALIVE, Tick
        ft = FakeTime()

        def source(a):
            for _ in range(40):  # 400 秒只有 ping,沒有成交(超過 stale-warn 30 與 stale-halt 300)
                ft.now += 10
                yield ALIVE
            yield Tick(100.6, False)

        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(cli, "_price_source", source), mock.patch("gridbot.cli.time", ft):
            stack, _ = _patch_price(return_value=100.5)
            with stack:
                rc = cli.main(PaperCliTest.ARGS + ["--state", os.path.join(t, "s.json"), "--max-polls", "1"])
        self.assertEqual(rc, 0)
        self.assertNotIn("沒有收到", out.getvalue())


if __name__ == "__main__":
    unittest.main()
