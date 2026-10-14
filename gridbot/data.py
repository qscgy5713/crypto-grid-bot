"""資料來源:CSV、Binance 公開 API(免金鑰)、合成隨機漫步,以及即時價格來源(WebSocket / 輪詢)。"""
from __future__ import annotations

import csv
import json
import math
import random
import time
import urllib.request
from typing import Callable, Iterator, List, NamedTuple, Optional, Tuple, Union

from .ws import WebSocket, WebSocketError

Candle = Tuple[float, float, float, float, float]  # ts(秒), open, high, low, close

BINANCE = "https://api.binance.com"
WS_CONNECT_TIMEOUT = 5.0  # 連線 / TLS / 握手各階段的逾時;連不上要快點放棄,不能讓迴圈長時間沒心跳


def load_csv(path: str) -> List[Candle]:
    """欄位順序:ts,open,high,low,close。第一個非空列可以是標題列;空白行略過。

    其餘無法解析、欄位不足、NaN/inf、價格不合理或時間倒退的列一律報錯(附列號),不悄悄丟資料。
    時間戳若為毫秒(> 1e11,例如 Binance 匯出的 CSV)自動換成秒。"""
    out: List[Candle] = []
    header_allowed = True
    with open(path, newline="") as fh:
        for lineno, row in enumerate(csv.reader(fh), 1):
            if not row or all(not x.strip() for x in row):
                continue
            try:
                vals = [float(x) for x in row[:5]]
            except ValueError:
                if header_allowed:
                    header_allowed = False
                    continue
                raise ValueError(f"{path} 第 {lineno} 列無法解析:{row}")
            header_allowed = False
            if len(vals) < 5:
                raise ValueError(f"{path} 第 {lineno} 列欄位不足(需要 ts,open,high,low,close)")
            if not all(math.isfinite(v) for v in vals):
                raise ValueError(f"{path} 第 {lineno} 列含 NaN 或 inf")
            ts, o, h, l, c = vals
            if min(o, h, l, c) <= 0 or l > min(o, c) or h < max(o, c):
                raise ValueError(f"{path} 第 {lineno} 列價格不合理(需 > 0 且 low <= open/close <= high)")
            if ts > 1e11:
                ts /= 1000
            if out and ts < out[-1][0]:
                raise ValueError(f"{path} 第 {lineno} 列時間比上一列早,請依時間排序")
            out.append((ts, o, h, l, c))
    if not out:
        raise ValueError(f"{path} 沒有可用資料")
    return out


def synthetic(n: int, start: float, vol: float = 0.01, seed: int = 42) -> List[Candle]:
    """幾何隨機漫步 K 線,vol 為每根 K 線的報酬標準差。"""
    rng = random.Random(seed)
    out: List[Candle] = []
    price = start
    for t in range(n):
        o = price
        c = o * (1 + rng.gauss(0, vol))
        wick = abs(rng.gauss(0, vol / 2))
        h = max(o, c) * (1 + wick)
        l = min(o, c) * (1 - wick)
        out.append((float(t), o, h, l, c))
        price = c
    return out


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 500) -> List[Candle]:
    """取最近 limit 根「已收盤」K 線;limit > 1000 時往回分頁(Binance 單次上限 1000)。

    不帶 endTime 的第一頁,最後一根一定是還在形成中的 K 線(高低收還會變),直接丟掉;
    第一頁多要一根補回,常見的 limit < 1000 只需一次請求。不依賴本機時鐘。"""
    if limit < 1:
        raise ValueError("limit 必須 >= 1")
    out: List[Candle] = []
    end = ""
    first = True
    while len(out) < limit:
        need = limit - len(out)
        n = min(1000, need + 1 if first else need)
        data = _get(f"{BINANCE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={n}{end}")
        if not data:
            break
        rows = data[:-1] if first else data
        out = [(k[0] / 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in rows] + out
        end = f"&endTime={data[0][0] - 1}"
        if len(data) < n:
            break
        first = False
    return out[-limit:]


def fetch_price(symbol: str) -> float:
    return float(_get(f"{BINANCE}/api/v3/ticker/price?symbol={symbol}")["price"])


# ---------------------------------------------------------------------------
# 即時價格來源。兩個來源都 yield 以下三種東西之一:
#   Tick(price, gap)  新價格;gap=True 表示與上一筆之間有沒看到的走勢(輪詢間隔、重連)
#   ALIVE             連線活著但沒有新成交(例如冷門交易對、收到 ping)
#   None              沒有任何資料的心跳(每 tick 秒一次),讓呼叫端能做斷線監看
# ---------------------------------------------------------------------------


class Tick(NamedTuple):
    price: float
    gap: bool = False


class _Alive:
    def __repr__(self) -> str:
        return "ALIVE"


ALIVE = _Alive()
Event = Union[Tick, _Alive, None]


def heartbeat_sleep(seconds: float, tick: float = 1.0) -> Iterator[None]:
    """等待 seconds 秒,期間每 tick 秒 yield 一次 None 心跳。"""
    waited = 0.0
    while waited < seconds:
        step = min(tick, seconds - waited)
        yield None
        time.sleep(step)
        waited += step


def poll_prices(symbol: str, interval: float, log: Callable[[str], None] = lambda m: None) -> Iterator[Event]:
    """輪詢價格來源:每 interval 秒讀一次 REST 現價。兩次輪詢之間的走勢看不到,所以每筆都標 gap。"""
    while True:
        yield from heartbeat_sleep(interval)
        try:
            price = fetch_price(symbol)
        except Exception as e:  # 網路瞬斷不中止
            log(f"取價失敗:{e}")
            yield None
            continue
        yield Tick(price, gap=True)


def stream_url(symbol: str) -> str:
    return f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"


def stream_prices(symbol: str, url: Optional[str] = None, give_up_after: Optional[int] = None,
                  max_backoff: float = 30.0, idle_timeout: float = 30.0, tick: float = 1.0,
                  log: Callable[[str], None] = lambda m: None) -> Iterator[Event]:
    """Binance 逐筆成交價串流;斷線自動指數退避重連。

    idle_timeout 秒內完全沒收到任何資料(連 Binance 的 ping 也沒有)視為連線已死,主動重連。
    give_up_after:連續失敗達此次數就丟 WebSocketError,交由呼叫端決定退路;None 表示永不放棄。
    """
    url = url or stream_url(symbol)
    delay, failures = 1.0, 0
    while True:
        ws = None
        try:
            ws = WebSocket(url, timeout=WS_CONNECT_TIMEOUT)
            ws.connect()
            log("WebSocket 已連線")
            first = True  # 連線(含重連)後第一筆價格與之前之間有看不到的走勢
            last_rx = time.monotonic()
            while True:
                if not ws.readable(tick):
                    if time.monotonic() - last_rx > idle_timeout:
                        raise WebSocketError(f"{idle_timeout:g} 秒沒有任何資料")
                    yield None
                    continue
                last_rx = time.monotonic()
                msg = ws.recv(idle_return=True)
                if msg is None:
                    raise WebSocketError("伺服器關閉連線")
                try:
                    price = float(json.loads(msg)["p"])
                except (ValueError, KeyError, TypeError):
                    yield ALIVE  # ping / 非成交訊息:連線是活的
                    continue
                failures, delay = 0, 1.0
                yield Tick(price, gap=first)
                first = False
        except (OSError, WebSocketError) as e:
            failures += 1
            if give_up_after is not None and failures >= give_up_after:
                raise WebSocketError(f"WebSocket 連續失敗 {failures} 次:{e}") from e
            log(f"WebSocket 中斷({e}),{delay:.0f} 秒後重連")
            if ws is not None:
                ws.close()
            yield from heartbeat_sleep(delay, tick)
            delay = min(delay * 2, max_backoff)
        finally:
            if ws is not None:
                ws.close()
