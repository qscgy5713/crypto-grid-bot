"""資料來源:CSV、Binance 公開 API(免金鑰)、合成隨機漫步。"""
from __future__ import annotations

import csv
import json
import random
import sys
import time
import urllib.request
from typing import Callable, Iterator, List, Optional, Tuple

from .ws import WebSocket, WebSocketError

Candle = Tuple[float, float, float, float, float]  # ts, open, high, low, close

BINANCE = "https://api.binance.com"


def load_csv(path: str) -> List[Candle]:
    """欄位順序:ts,open,high,low,close(可有標題列)。"""
    out: List[Candle] = []
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            try:
                out.append(tuple(float(x) for x in row[:5]))  # type: ignore[arg-type]
            except ValueError:
                continue  # 標題列
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
    """取最近 limit 根 K 線;limit > 1000 時往回分頁(Binance 單次上限 1000)。"""
    if limit < 1:
        raise ValueError("limit 必須 >= 1")
    out: List[Candle] = []
    end = ""
    while len(out) < limit:
        n = min(1000, limit - len(out))
        data = _get(f"{BINANCE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={n}{end}")
        if not data:
            break
        chunk = [(k[0] / 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in data]
        out = chunk + out
        end = f"&endTime={data[0][0] - 1}"
        if len(data) < n:
            break
    return out


def fetch_price(symbol: str) -> float:
    return float(_get(f"{BINANCE}/api/v3/ticker/price?symbol={symbol}")["price"])


def stream_url(symbol: str) -> str:
    return f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"


def stream_prices(symbol: str, url: Optional[str] = None, give_up_after: Optional[int] = None,
                  max_backoff: float = 30.0, idle_timeout: float = 30.0, tick: float = 1.0,
                  log: Callable[[str], None] = lambda m: None) -> Iterator[Optional[float]]:
    """Binance 逐筆成交價串流;斷線自動指數退避重連。

    沒有新價格時(靜默、重連等待中)每 tick 秒 yield 一次 None 當心跳,讓呼叫端能做斷線監看。
    idle_timeout 秒內完全沒收到任何資料(連 Binance 的 ping 也沒有)視為連線已死,主動重連。
    give_up_after:連續失敗達此次數就丟 WebSocketError,交由呼叫端決定退路;None 表示永不放棄。
    """
    url = url or stream_url(symbol)
    delay, failures = 1.0, 0
    while True:
        ws = None
        try:
            ws = WebSocket(url, timeout=30.0)
            ws.connect()
            log("WebSocket 已連線")
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
                    continue  # 非成交訊息 / ping,略過
                failures, delay = 0, 1.0
                yield price
        except (OSError, WebSocketError) as e:
            failures += 1
            if give_up_after is not None and failures >= give_up_after:
                raise WebSocketError(f"WebSocket 連續失敗 {failures} 次:{e}") from e
            log(f"WebSocket 中斷({e}),{delay:.0f} 秒後重連")
            if ws is not None:
                ws.close()
            waited = 0.0
            while waited < delay:  # 分段等待,期間持續送心跳
                step = min(tick, delay - waited)
                yield None
                time.sleep(step)
                waited += step
            delay = min(delay * 2, max_backoff)
        finally:
            if ws is not None:
                ws.close()
