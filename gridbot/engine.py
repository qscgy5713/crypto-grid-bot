"""現貨網格交易引擎(純邏輯,不碰網路、不碰時間,方便回測與測試)。

規則:
- 價格區間 [lower, upper] 切成 grids 格,共 grids+1 條價位線。
- 啟動時:現價下方每條線掛買單,上方每條線掛賣單(現價所在處留一個空檔);賣單所需的幣以現價市價買入。
- 買單在 level i 成交 -> 在 level i+1 掛賣單;賣單在 level i 成交 -> 在 level i-1 掛買單。
- 成交採「穿價」:價格必須越過掛單價才算成交,剛好碰到不算(實際上碰價未必輪得到你的單)。
- 停損:連續行情以停損價出場;跳空(gap=True,例如停機後接續、斷線恢復)則以當下價格出場。
- 每格數量固定,資金預先依「所有買單 + 初始持倉」分配,保證不會透支。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

REL_TOL = 1e-9  # 資金 / 持倉足夠與否的相對容差(絕對容差在金額大時會被浮點誤差吃掉)
STATE_VERSION = 1
MAX_SAVED_FILLS = 1000  # 存檔只保留最近的成交紀錄;cycles / grid_profit 另存,不受影響


@dataclass
class GridConfig:
    lower: float
    upper: float
    grids: int
    invest: float  # 投入本金(計價幣,如 USDT)
    mode: str = "arithmetic"  # arithmetic 等差 / geometric 等比
    fee_rate: float = 0.001  # 單邊手續費率
    stop_loss: Optional[float] = None  # 跌破此價全數賣出並停機

    def validate(self) -> None:
        if not (0 < self.lower < self.upper):
            raise ValueError("需滿足 0 < lower < upper")
        if self.grids < 2:
            raise ValueError("grids 至少為 2")
        if self.invest <= 0:
            raise ValueError("invest 必須 > 0")
        if self.mode not in ("arithmetic", "geometric"):
            raise ValueError("mode 必須是 arithmetic 或 geometric")
        if not (0 <= self.fee_rate < 0.05):
            raise ValueError("fee_rate 需在 [0, 0.05) 之間")
        if self.stop_loss is not None and not (0 < self.stop_loss < self.lower):
            raise ValueError("stop_loss 必須低於 lower")


@dataclass
class Fill:
    ts: float
    side: str  # buy / sell
    level: int
    price: float
    qty: float
    fee: float
    note: str = ""


def build_levels(cfg: GridConfig) -> List[float]:
    n = cfg.grids
    if cfg.mode == "arithmetic":
        step = (cfg.upper - cfg.lower) / n
        lv = [cfg.lower + step * i for i in range(n + 1)]
    else:
        ratio = (cfg.upper / cfg.lower) ** (1.0 / n)
        lv = [cfg.lower * ratio**i for i in range(n + 1)]
    lv[0], lv[-1] = cfg.lower, cfg.upper  # 端點不受浮點誤差影響
    return lv


def min_spacing(cfg: GridConfig) -> float:
    """最小格距(比例,0.01 = 1%)。"""
    lv = build_levels(cfg)
    return min(lv[i + 1] / lv[i] - 1 for i in range(len(lv) - 1))


def breakeven_spacing(fee_rate: float) -> float:
    """一買一賣扣完雙邊手續費剛好打平的格距:p_sell*(1-f) = p_buy*(1+f) -> 2f/(1-f)。"""
    return 2 * fee_rate / (1 - fee_rate)


def spacing_covers_fees(cfg: GridConfig) -> bool:
    return min_spacing(cfg) > breakeven_spacing(cfg.fee_rate)


class GridEngine:
    def __init__(self, cfg: GridConfig, start_price: float, ts: float = 0.0):
        cfg.validate()
        if not (cfg.lower < start_price < cfg.upper):
            raise ValueError("起始價格必須落在 (lower, upper) 區間內")
        self.cfg = cfg
        self.levels = build_levels(cfg)
        self.last_price = start_price
        self.halted = False
        self.fills: List[Fill] = []
        self.cycles = 0  # 已完成的「買->賣」套利次數
        self.buy_count = 0  # 網格買單成交次數(不含初始建倉);賣單成交次數即 cycles
        self.grid_profit = 0.0  # 網格套利利潤(估算,已扣手續費)
        # level index -> "buy" | "sell"
        self.orders: Dict[int, str] = {}

        fee = cfg.fee_rate
        # 現價所在的「空檔」:第一條 >= 現價的線不掛單,其下全掛買、其上全掛賣。
        # 如此任何時刻剛好 grids 張單,買單成交補在空檔的賣單、反之亦然,不會互相覆蓋。
        gap = next((i for i, p in enumerate(self.levels) if p >= start_price), len(self.levels) - 1)
        buys = list(range(gap))
        sells = list(range(gap + 1, len(self.levels)))
        need_per_unit = (1 + fee) * (
            len(sells) * start_price + sum(self.levels[i] for i in buys)
        )
        self.qty = cfg.invest / need_per_unit  # 每格固定數量(幣)

        self.quote = cfg.invest
        self.base = 0.0
        init_qty = self.qty * len(sells)
        if init_qty > 0:
            cost = init_qty * start_price
            f = cost * fee
            self.quote -= cost + f
            self.base += init_qty
            self.fills.append(Fill(ts, "buy", -1, start_price, init_qty, f, "初始建倉"))
        for i in buys:
            self.orders[i] = "buy"
        for i in sells:
            self.orders[i] = "sell"

    def _record(self, f: Fill) -> None:
        """記錄成交;記憶體只保留最近一段(統計另有計數器),避免長時間運行無限成長。"""
        self.fills.append(f)
        if len(self.fills) > 2 * MAX_SAVED_FILLS:
            del self.fills[:-MAX_SAVED_FILLS]

    # ---- 查詢 ----
    def equity(self, price: float) -> float:
        return self.quote + self.base * price

    def next_orders(self):
        """(最近買單價, 最近賣單價);沒有則為 None。"""
        buys = [self.levels[i] for i, s in self.orders.items() if s == "buy"]
        sells = [self.levels[i] for i, s in self.orders.items() if s == "sell"]
        return (max(buys) if buys else None, min(sells) if sells else None)

    # ---- 驅動 ----
    def on_price(self, price: float, ts: float = 0.0, gap: bool = False) -> List[Fill]:
        """價格由 last_price 移動到 price,依序撮合途中被穿越的掛單。

        gap=False:視為連續行情(回測的 K 線路徑、即時串流)。
        gap=True:中間有一段沒看到的走勢(停機、斷線)。網格限價單仍以掛單價成交
        (真實交易所上掛著的限價單會照常撮合),但停損是程式監控的,只能以當下價格出場。
        """
        if self.halted:
            return []
        new: List[Fill] = []
        last = self.last_price
        stop = self.cfg.stop_loss
        if stop is not None and price <= stop:
            new.extend(self._fill_range(last, stop, ts))
            new.append(self._stop_out(price if gap else stop, ts))
            self.last_price = price
            return new
        new.extend(self._fill_range(last, price, ts))
        self.last_price = price
        return new

    def _fill_range(self, last: float, price: float, ts: float) -> List[Fill]:
        out: List[Fill] = []
        if price < last:
            idxs = sorted(
                (i for i, s in self.orders.items() if s == "buy" and price < self.levels[i] <= last),
                reverse=True,
            )
            for i in idxs:
                f = self._fill_buy(i, ts)
                if f is None:
                    break  # 跳過的單仍掛著;再往下撮合會覆蓋它,所以中止這一輪
                out.append(f)
        elif price > last:
            idxs = sorted(
                i for i, s in self.orders.items() if s == "sell" and last <= self.levels[i] < price
            )
            for i in idxs:
                f = self._fill_sell(i, ts)
                if f is None:
                    break
                out.append(f)
        return out

    def _fill_buy(self, i: int, ts: float) -> Optional[Fill]:
        p = self.levels[i]
        cost = self.qty * p
        fee = cost * self.cfg.fee_rate
        need = cost + fee
        if self.quote < need * (1 - REL_TOL):
            return None  # 資金不足(浮點誤差外不應發生)
        del self.orders[i]
        self.quote = max(self.quote - need, 0.0)  # 容差內的微小負值歸零
        self.base += self.qty
        self.orders[i + 1] = "sell"
        f = Fill(ts, "buy", i, p, self.qty, fee)
        self.buy_count += 1
        self._record(f)
        return f

    def _fill_sell(self, i: int, ts: float) -> Optional[Fill]:
        p = self.levels[i]
        if self.base < self.qty * (1 - REL_TOL):
            return None
        proceeds = self.qty * p
        fee = proceeds * self.cfg.fee_rate
        del self.orders[i]
        self.quote += proceeds - fee
        self.base = max(self.base - self.qty, 0.0)
        self.orders[i - 1] = "buy"
        self.cycles += 1
        self.grid_profit += self.qty * (p - self.levels[i - 1]) - fee - self.qty * self.levels[i - 1] * self.cfg.fee_rate
        f = Fill(ts, "sell", i, p, self.qty, fee)
        self._record(f)
        return f

    def _stop_out(self, price: float, ts: float) -> Fill:
        qty = self.base
        proceeds = qty * price
        fee = proceeds * self.cfg.fee_rate
        self.quote += proceeds - fee
        self.base = 0.0
        self.orders.clear()
        self.halted = True
        f = Fill(ts, "sell", -1, price, qty, fee, "停損清倉")
        self._record(f)
        return f

    # ---- 持久化 ----
    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "cfg": asdict(self.cfg),
            "qty": self.qty,
            "quote": self.quote,
            "base": self.base,
            "last_price": self.last_price,
            "halted": self.halted,
            "cycles": self.cycles,
            "grid_profit": self.grid_profit,
            "buy_count": self.buy_count,
            "orders": {str(i): s for i, s in self.orders.items()},
            "fills": [asdict(f) for f in self.fills[-MAX_SAVED_FILLS:]],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GridEngine":
        """還原引擎狀態;資料缺漏或內部不一致(被改壞、版本不符)一律丟 ValueError。"""
        try:
            if d["version"] != STATE_VERSION:
                raise ValueError(f"不支援的狀態版本 {d['version']}")
            cfg = GridConfig(**d["cfg"])
            cfg.validate()
            e = object.__new__(cls)
            e.cfg = cfg
            e.levels = build_levels(cfg)
            e.qty = float(d["qty"])
            e.quote = float(d["quote"])
            e.base = float(d["base"])
            e.last_price = float(d["last_price"])
            e.halted = bool(d["halted"])
            e.cycles = int(d["cycles"])
            e.grid_profit = float(d["grid_profit"])
            e.buy_count = int(d.get("buy_count", 0))
            e.orders = {int(i): s for i, s in d["orders"].items()}
            e.fills = [Fill(**f) for f in d["fills"]]
        except (KeyError, TypeError, AttributeError) as ex:
            raise ValueError(f"狀態檔格式錯誤:{ex!r}") from ex
        nums = (e.qty, e.quote, e.base, e.last_price, e.grid_profit)
        if not all(math.isfinite(x) for x in nums) or e.qty <= 0 or e.last_price <= 0:
            raise ValueError("狀態檔數值異常")
        if e.quote < -1e-6 or e.base < -1e-9:
            raise ValueError("狀態檔餘額為負")
        n = len(e.levels)
        if any(not (0 <= i < n) or s not in ("buy", "sell") for i, s in e.orders.items()):
            raise ValueError("狀態檔掛單異常")
        if not e.halted:
            sells = sum(1 for s in e.orders.values() if s == "sell")
            if len(e.orders) != cfg.grids or abs(e.base - sells * e.qty) > 1e-6 * max(1.0, e.base):
                raise ValueError("狀態檔不一致:掛單數或持倉與賣單不符")
        return e
