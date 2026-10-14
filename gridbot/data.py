"""資料來源:CSV、Binance 公開 API(免金鑰)、合成隨機漫步。"""
from __future__ import annotations

import csv
import json
import random
import urllib.request
from typing import List, Tuple

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
    data = _get(f"{BINANCE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
    return [(k[0] / 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in data]


def fetch_price(symbol: str) -> float:
    return float(_get(f"{BINANCE}/api/v3/ticker/price?symbol={symbol}")["price"])
