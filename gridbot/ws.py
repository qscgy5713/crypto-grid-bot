"""精簡 WebSocket 客戶端(RFC 6455),僅標準庫;支援 ws:// 與 wss://、分片訊息、ping/pong、close。

只實作本專案需要的部分:收文字訊息,對 ping 回 pong。不支援壓縮 extension。
"""
from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import ssl
import struct
from typing import Optional
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_PAYLOAD = 16 * 1024 * 1024


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise ValueError(f"不支援的 WebSocket 網址:{url}")
        self.tls = u.scheme == "wss"
        self.host = u.hostname
        self.port = u.port or (443 if self.tls else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._parts: list = []  # 分片訊息的已收部分;跨 recv 呼叫保留,中途插入 ping 也不會遺失
        self.closed = False

    # ---- 連線 ----
    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
            key = base64.b64encode(os.urandom(16)).decode()
            default = 443 if self.tls else 80
            host = self.host if self.port == default else f"{self.host}:{self.port}"
            sock.sendall(
                (f"GET {self.path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                 f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                 "Sec-WebSocket-Version: 13\r\n\r\n").encode()
            )
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("握手時連線被關閉")
                head += chunk
                if len(head) > 65536:
                    raise WebSocketError("握手回應過大")
            head, self._buf = head.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
                raise WebSocketError(f"握手失敗:{lines[0]}")
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
            want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if hdrs.get("sec-websocket-accept") != want:
                raise WebSocketError("握手驗證失敗(Sec-WebSocket-Accept 不符)")
        except BaseException:
            sock.close()
            self.sock = None
            raise

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            if not self.closed:
                self._send(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        finally:
            self.closed = True
            try:
                self.sock.close()
            finally:
                self.sock = None

    # ---- 收發 ----
    def readable(self, timeout: float) -> bool:
        """最多等 timeout 秒,看有沒有資料可讀(含已緩衝 / TLS 已解密未取走的資料)。"""
        if self._buf:
            return True
        if self.tls and self.sock.pending():
            return True
        return bool(select.select([self.sock], [], [], timeout)[0])

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("連線中斷")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)  # 客戶端送出的 frame 必須遮罩
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def recv(self, idle_return: bool = False) -> Optional[str]:
        """收下一則完整文字訊息;對方正常關閉則回 None。逾時 / 斷線會丟 OSError / WebSocketError。

        idle_return=True 時,處理完 ping/pong 就先回傳空字串,讓呼叫端有機會做閒置檢查。"""
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            if n > MAX_PAYLOAD:
                raise WebSocketError("單一 frame 過大")
            mask = self._read_exact(4) if b2 & 0x80 else None
            data = self._read_exact(n) if n else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:  # close
                self.closed = True
                try:
                    self._send(0x8, data[:2])
                except OSError:
                    pass
                return None
            if opcode == 0x9:  # ping -> pong
                self._send(0xA, data)
                if idle_return:
                    return ""
                continue
            if opcode == 0xA:  # pong
                if idle_return:
                    return ""
                continue
            if opcode in (0x0, 0x1, 0x2):
                self._parts.append(data)
                if fin:
                    msg, self._parts = b"".join(self._parts), []
                    return msg.decode("utf-8", errors="replace")
                continue
            raise WebSocketError(f"未知 opcode {opcode}")
