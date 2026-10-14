"""模擬盤狀態存檔:JSON,先寫暫存檔再原子替換,避免中途中斷留下半截檔案。"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional, Tuple

from .engine import GridEngine

try:
    import fcntl
except ImportError:  # Windows 沒有 fcntl:不加鎖
    fcntl = None  # type: ignore[assignment]


def save_state(engine: GridEngine, path: str, meta: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    payload = {"meta": meta, "engine": engine.to_dict()}
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_state(path: str) -> Tuple[GridEngine, dict]:
    try:
        with open(path) as fh:
            payload = json.load(fh)
        return GridEngine.from_dict(payload["engine"]), dict(payload["meta"])
    except (json.JSONDecodeError, KeyError, TypeError) as ex:
        raise ValueError(f"狀態檔 {path} 損毀或格式不符:{ex!r}") from ex


def acquire_lock(path: str):
    """對狀態檔加獨占鎖(非阻塞)。回傳的檔案物件要持有到結束,close 即釋放;
    已被另一個行程持有則丟 ValueError,避免兩個 paper 行程互相覆蓋狀態。"""
    if fcntl is None:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fh = open(path + ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise ValueError(f"狀態檔 {path} 正被另一個 paper 行程使用(鎖檔 {path}.lock);請先結束它,或用 --state 指定其他檔案")
    return fh
