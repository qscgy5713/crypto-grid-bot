import unittest

from gridbot import GridConfig, GridEngine, build_levels
from gridbot.backtest import candle_path, run_backtest
from gridbot.data import synthetic


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
            "--invest", "1000", "--fee", "0", "--interval", "0"]

    def _run(self, prices, path, extra=()):
        from unittest import mock
        from gridbot.cli import main
        with mock.patch("gridbot.cli.fetch_price", side_effect=prices), \
             mock.patch("gridbot.cli.time.sleep"):
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
                                            "--interval", "0", "--state", path, "--max-polls", "1"])
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
            conn, _ = self.sock.accept()
            conn.settimeout(5)
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
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
        with self.assertRaises(WebSocketError):
            WebSocket(srv.url(), timeout=5).connect()
        srv.close()

    def test_bad_url(self):
        from gridbot.ws import WebSocket
        with self.assertRaises(ValueError):
            WebSocket("http://x")


def nxt(g):
    """取下一個真正的價格,略過心跳 None。"""
    while True:
        v = next(g)
        if v is not None:
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
        logs = []
        with mock.patch("gridbot.data.time.sleep") as sl:
            g = stream_prices("X", url=srv.url(), log=logs.append)
            self.assertEqual(nxt(g), 100.5)
            self.assertEqual(nxt(g), 101.25)  # 第一條連線被關 -> 重連 -> 第二條
            g.close()
        sl.assert_called_once_with(1.0)
        self.assertTrue(any("重連" in m for m in logs))
        srv.close()

    def test_give_up_after(self):
        from unittest import mock
        from gridbot.data import stream_prices
        from gridbot.ws import WebSocketError
        srv = FakeWsServer([])
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
        logs = []
        g = stream_prices("X", url=srv.url(), idle_timeout=0.3, tick=0.05, log=logs.append)
        beats = 0
        with mock.patch("gridbot.data.time.sleep"):
            while True:
                v = next(g)
                if v is None:
                    beats += 1
                else:
                    break
        g.close()
        self.assertEqual(v, 7.0)
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
        g = stream_prices("X", url=srv.url(), idle_timeout=5, tick=0.05)
        self.assertIsNone(next(g))  # ping 不該卡住迴圈,應立刻回到心跳
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

        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            log = os.path.join(t, "alerts.txt")
            with mock.patch("gridbot.cli.fetch_price", side_effect=price), \
                 mock.patch("gridbot.cli.time.sleep"):
                rc = main(PaperCliTest.ARGS + ["--state", os.path.join(t, "s.json"),
                                               "--stale-warn", "0.05", "--stale-halt", "0.2",
                                               "--alert-cmd", f'echo "$GRIDBOT_ALERT_LEVEL" >> {log}'])
            self.assertEqual(rc, 3)
            self.assertEqual(open(log).read().split(), ["warn", "halt"])
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
        self.assertEqual((r.buy_count, r.sell_count), (e.buy_count, e.sell_count))
        self.assertGreater(e.buy_count, m.MAX_SAVED_FILLS)  # 計數不受修剪影響
        self.assertEqual(e.sell_count, e.cycles)

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
            rc = main(PaperCliTest.ARGS[:-2] + ["--interval", "20"])
        self.assertEqual(rc, 2)
        self.assertIn("stale-warn", err.getvalue())

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
            with mock.patch("gridbot.cli.fetch_price", side_effect=[100.5, 100.5]), \
                 mock.patch("gridbot.cli.time.sleep"):
                self.assertEqual(main(args + ["--state", path, "--max-polls", "1"]), 0)
            with mock.patch("gridbot.cli.fetch_price", side_effect=[70.0]) as fp:  # 只允許一次取價
                self.assertEqual(main(args + ["--state", path]), 0)
                self.assertEqual(fp.call_count, 1)
            e, _ = load_state(path)
            self.assertTrue(e.halted)
            self.assertEqual(e.base, 0)
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

        def fake_poll(symbol, interval):
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
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()):
            path = os.path.join(t, "s.json")
            with mock.patch("gridbot.cli.fetch_price", side_effect=price), mock.patch("gridbot.cli.time.sleep"):
                self.assertEqual(main(PaperCliTest.ARGS + ["--state", path]), 0)
            e, _ = load_state(path)
            self.assertAlmostEqual(e.last_price, 100.4)  # 收尾存檔把最後價格寫進去
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

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


if __name__ == "__main__":
    unittest.main()
