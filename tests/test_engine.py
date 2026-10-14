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


if __name__ == "__main__":
    unittest.main()
