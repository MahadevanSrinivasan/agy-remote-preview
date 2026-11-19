"""Minimal stdlib-only RFC 6455 WebSocket client for Chrome DevTools Protocol."""

import base64
import hashlib
import json
import os
import socket
import struct
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def fetch_json(url: str, timeout: float = 5.0) -> Any:
    """Fetch and parse a JSON payload from a local HTTP endpoint."""
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CDPConnection:
    """Lightweight RFC 6455 WebSocket client with CDP JSON-RPC & event buffering."""

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        self._buf = bytearray()
        self._next_id = 0
        self._responses: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._sock = self._connect(ws_url, timeout)

    def _connect(self, ws_url: str, timeout: float) -> socket.socket:
        parsed = urlparse(ws_url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise ValueError(f"Unsupported WebSocket URL: {ws_url}")
        port = parsed.port or 80
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise ConnectionError("WebSocket handshake closed before completion")
            raw.extend(chunk)
        head, rest = raw.split(b"\r\n\r\n", 1)
        self._buf = bytearray(rest)
        lines = head.decode("latin-1").split("\r\n")
        if not lines or "101" not in lines[0]:
            sock.close()
            raise ConnectionError(f"WebSocket upgrade rejected: {lines[0] if lines else ''}")
        hdrs = {k.strip().lower(): v.strip() for ln in lines[1:] if ":" in ln for k, v in [ln.split(":", 1)]}
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if hdrs.get("sec-websocket-accept") != expected:
            sock.close()
            raise ConnectionError("Invalid Sec-WebSocket-Accept header")
        return sock

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("WebSocket EOF")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        b0 = 0x80 | (opcode & 0x0F)
        if n < 126:
            header = struct.pack("!BB", b0, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", b0, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", b0, 0x80 | 127, n)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._recv_exact(2)
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        masked, length = bool(b1 & 0x80), b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        mask = self._recv_exact(4) if masked else b""
        data = self._recv_exact(length)
        if masked:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        return fin, opcode, data

    def _read_message(self, timeout: float) -> dict[str, Any]:
        self._sock.settimeout(max(0.01, timeout))
        chunks: list[bytes] = []
        while True:
            fin, opcode, data = self._read_frame()
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by remote")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            chunks.append(data)
            if fin:
                break
        msg = json.loads(b"".join(chunks).decode("utf-8"))
        if "id" in msg:
            self._responses[msg["id"]] = msg
        else:
            self.events.append(msg)
        return msg

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        """Send a CDP command and wait for its JSON-RPC response."""
        self._next_id += 1
        req_id = self._next_id
        self._send_frame(0x1, json.dumps({"id": req_id, "method": method, "params": params or {}}).encode("utf-8"))
        deadline = time.monotonic() + timeout
        while req_id not in self._responses:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"CDP timeout waiting for {method}")
            self._read_message(rem)
        resp = self._responses.pop(req_id)
        if "error" in resp:
            raise RuntimeError(f"CDP {method} failed: {resp['error']}")
        return resp.get("result", {})

    def wait_for_event(self, event_name: str, timeout: float = 10.0, start_index: int = 0) -> dict[str, Any]:
        """Wait until a CDP event with matching method name is observed."""
        idx, deadline = start_index, time.monotonic() + timeout
        while True:
            for ev in self.events[idx:]:
                idx += 1
                if ev.get("method") == event_name:
                    return ev
            if (rem := deadline - time.monotonic()) <= 0:
                raise TimeoutError(f"CDP timeout waiting for event {event_name}")
            self._read_message(rem)

    def drain_events(self, duration_s: float) -> None:
        """Read incoming CDP events until duration_s elapses without failing on idle timeout."""
        deadline = time.monotonic() + max(0.0, duration_s)
        while (rem := deadline - time.monotonic()) > 0:
            try:
                self._read_message(rem)
            except (TimeoutError, socket.timeout, ConnectionError):
                break

    def close(self) -> None:
        """Close the underlying WebSocket connection."""
        for fn in (lambda: self._send_frame(0x8, b""), self._sock.close):
            try:
                fn()
            except Exception:
                pass
