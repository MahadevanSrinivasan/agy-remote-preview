"""Deterministic unit and headless integration tests for agy-remote-preview."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import socket
import struct
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agy_remote_preview import capture_preview, render_artifact_markdown
from agy_remote_preview.capture import _is_snap_chromium, _make_profile_dir
from agy_remote_preview.cdp import CDPConnection
from agy_remote_preview.cli import main

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_pack(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    b0, n = (0x80 if fin else 0) | opcode, len(payload)
    if n < 126:
        return struct.pack("!BB", b0, n) + payload
    if n < 65536:
        return struct.pack("!BBH", b0, 126, n) + payload
    return struct.pack("!BBQ", b0, 127, n) + payload


def _ws_read_masked(conn: socket.socket) -> tuple[int, bytes]:
    b0, b1 = conn.recv(2)
    length = b1 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", conn.recv(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", conn.recv(8))
    mask = conn.recv(4) if (b1 & 0x80) else b"\x00\x00\x00\x00"
    data = bytearray()
    while len(data) < length:
        data.extend(conn.recv(length - len(data)))
    return b0 & 0x0F, bytes(b ^ mask[i & 3] for i, b in enumerate(data))


def _accept_ws_handshake(conn: socket.socket) -> str:
    raw = conn.recv(4096).decode("latin-1")
    hdrs = {k.lower(): v.strip() for ln in raw.split("\r\n")[1:] if ":" in ln for k, v in [ln.split(":", 1)]}
    accept = base64.b64encode(hashlib.sha1((hdrs["sec-websocket-key"] + _WS_GUID).encode("ascii")).digest()).decode("ascii")
    return f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"


class TestRemotePreview(unittest.TestCase):
    """Unit and end-to-end tests for agy-remote-preview."""

    def test_cdp_websocket_protocol_features(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        observed: dict[str, object] = {}

        def _server() -> None:
            conn, _ = srv.accept()
            with conn:
                hs = _accept_ws_handshake(conn).encode("ascii")
                ev = json.dumps({"method": "Mock.ready", "params": {"pad": "x" * 140}}).encode("utf-8")
                conn.sendall(hs + _ws_pack(0x1, ev))
                op, req_raw = _ws_read_masked(conn)
                req = json.loads(req_raw.decode("utf-8"))
                observed["op"], observed["method"] = op, req["method"]
                conn.sendall(_ws_pack(0x9, b"hb"))
                observed["pong"] = _ws_read_masked(conn)
                resp = json.dumps({"id": req["id"], "result": {"ok": True, "blob": "y" * 200}}).encode("utf-8")
                mid = len(resp) // 2
                conn.sendall(_ws_pack(0x1, resp[:mid], fin=False) + _ws_pack(0x0, resp[mid:], fin=True))

        th = threading.Thread(target=_server, daemon=True)
        th.start()
        try:
            cdp = CDPConnection(f"ws://127.0.0.1:{port}/devtools/page/1", timeout=5.0)
            self.assertTrue(cdp.call("Page.enable", timeout=5.0)["ok"])
            ev = cdp.wait_for_event("Mock.ready", timeout=2.0)
            self.assertEqual(len(ev["params"]["pad"]), 140)
            cdp.close()
        finally:
            th.join(timeout=3.0)
            srv.close()
        self.assertEqual(observed["op"], 0x1)
        self.assertEqual(observed["method"], "Page.enable")
        self.assertEqual(observed["pong"], (0xA, b"hb"))

    def test_render_artifact_markdown_tip_and_warning(self) -> None:
        base = {"url": "http://127.0.0.1:8780", "title": "App", "http_status": 200, "load_ms": 12.3, "total_ms": 45.6, "desktop_bytes": 1024, "mobile_bytes": 2048}
        tip_md = render_artifact_markdown({**base, "console_errors": [], "http_failures": []}, "/tmp/d.png", "/tmp/m.png")
        self.assertIn("> [!TIP]", tip_md)
        self.assertIn("````carousel", tip_md)
        self.assertIn("![Desktop 1280x800](/tmp/d.png)", tip_md)
        self.assertIn("![Mobile 390x844](/tmp/m.png)", tip_md)

        warn_md = render_artifact_markdown({**base, "console_errors": ["boom"], "http_failures": ["500 /api"]}, "/tmp/d.png", "/tmp/m.png")
        self.assertIn("> [!WARNING]", warn_md)
        self.assertIn("`boom`", warn_md)
        self.assertIn("`500 /api`", warn_md)

    def test_invalid_url_scheme_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("ftp://127.0.0.1/file", "not-a-url", "file:///etc/passwd"):
                with self.assertRaises(ValueError):
                    capture_preview(bad, tmp)

    def test_cli_help_and_missing_args(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
            self.assertEqual(cm.exception.code, 0)
        self.assertIn("--check-daemon", buf.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main([])
            self.assertEqual(cm.exception.code, 2)

    def test_check_daemon_warns_when_linger_no(self) -> None:
        def _fake_which(cmd: str) -> str | None:
            return f"/usr/bin/{cmd}" if cmd in {"agy", "loginctl"} else None

        def _fake_run(argv: list[str], **_kw: object) -> mock.MagicMock:
            proc = mock.MagicMock(returncode=0, stderr="")
            proc.stdout = "Daemon status: inactive\n" if "remote-control" in argv else "Linger=no\n"
            return proc

        out = io.StringIO()
        with (
            mock.patch("agy_remote_preview.cli.shutil.which", side_effect=_fake_which),
            mock.patch("agy_remote_preview.cli.subprocess.run", side_effect=_fake_run),
            contextlib.redirect_stdout(out),
        ):
            code = main(["--check-daemon"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("WARNING:", text)
        self.assertIn("Linger=no", text)
        self.assertIn("sudo loginctl enable-linger", text)

    def test_snap_chromium_detection_and_profile_dir(self) -> None:
        self.assertTrue(_is_snap_chromium("/snap/bin/chromium"))
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "chromium-browser"
            wrapper.write_text("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n", encoding="utf-8")
            self.assertTrue(_is_snap_chromium(str(wrapper)))
            native = Path(tmp) / "google-chrome"
            native.write_text("#!/bin/sh\necho chrome\n", encoding="utf-8")
            self.assertFalse(_is_snap_chromium(str(native)))
            snap_prof, native_prof = _make_profile_dir(str(wrapper)), _make_profile_dir(str(native))
            try:
                self.assertEqual(snap_prof.parent, Path.home() / "snap" / "chromium" / "common")
                self.assertNotEqual(native_prof.parent, Path.home() / "snap" / "chromium" / "common")
            finally:
                snap_prof.rmdir()
                native_prof.rmdir()

    def test_cdp_drain_events_ignores_idle_timeout(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _server() -> None:
            conn, _ = srv.accept()
            with conn:
                conn.sendall(_accept_ws_handshake(conn).encode("ascii"))
                threading.Event().wait(0.2)

        th = threading.Thread(target=_server, daemon=True)
        th.start()
        try:
            cdp = CDPConnection(f"ws://127.0.0.1:{port}/devtools/page/1", timeout=2.0)
            cdp.drain_events(0.05)
            cdp.close()
        finally:
            th.join(timeout=2.0)
            srv.close()

    def test_end_to_end_headless_chromium_capture(self) -> None:
        html = (
            b"<!doctype html><html><head><title>Preview E2E</title>"
            b"<style>body{margin:0;background:#111;color:#fff}@media(max-width:500px){body{background:#123}}</style></head>"
            b"<body><h1>Responsive</h1><script>console.error('TEST_CONSOLE_ERR');</script></body></html>"
        )

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        snap_common = Path.home() / "snap" / "chromium" / "common"
        before_tmp = set(Path("/tmp").glob("agy_rprev_*"))
        before_snap = set(snap_common.glob("agy_rprev_*")) if snap_common.exists() else set()
        try:
            with tempfile.TemporaryDirectory() as out_dir:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main([f"http://127.0.0.1:{server.server_port}/", "--out-dir", out_dir, "--timeout", "180", "--json"])
                self.assertEqual(code, 0)
                report = json.loads(buf.getvalue())
                self.assertEqual(report["http_status"], 200)
                self.assertEqual(report["title"], "Preview E2E")
                self.assertTrue(any("TEST_CONSOLE_ERR" in e for e in report["console_errors"]))
                desk = (Path(out_dir) / "preview_desktop.png").read_bytes()
                mob = (Path(out_dir) / "preview_mobile.png").read_bytes()
                self.assertEqual(struct.unpack(">II", desk[16:24]), (1280, 800))
                self.assertEqual(struct.unpack(">II", mob[16:24]), (780, 1688))
                md_path = Path(out_dir) / "remote-preview.md"
                self.assertTrue(md_path.is_file())
                self.assertIn("TEST_CONSOLE_ERR", md_path.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(set(Path("/tmp").glob("agy_rprev_*")) - before_tmp, set())
        after_snap = set(snap_common.glob("agy_rprev_*")) if snap_common.exists() else set()
        self.assertEqual(after_snap - before_snap, set())

    def test_webgl_flag_selection_and_cli_forwarding(self) -> None:
        launched: list[list[str]] = []

        class _FakeProc:
            returncode = None
            pid = 99999

            def poll(self) -> int | None:
                return 0

        def _fake_popen(cmd: list[str], **_kw: object) -> _FakeProc:
            launched.append(cmd)
            return _FakeProc()

        fake_cdp = mock.MagicMock()
        fake_cdp.events = []
        fake_cdp.call.side_effect = [
            {}, {}, {}, {}, {}, {},
            {"result": {"value": "Mock"}},
            {"data": base64.b64encode(b"png1").decode("ascii")},
            {}, {},
            {"data": base64.b64encode(b"png2").decode("ascii")},
            {},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("agy_remote_preview.capture.find_browser", return_value="/usr/bin/chromium"), \
                 mock.patch("agy_remote_preview.capture.subprocess.Popen", side_effect=_fake_popen), \
                 mock.patch("agy_remote_preview.capture._wait_for_page_ws", return_value="ws://127.0.0.1:1/p"), \
                 mock.patch("agy_remote_preview.capture.CDPConnection", return_value=fake_cdp):
                capture_preview("http://127.0.0.1:8780", tmp)
            self.assertIn("--disable-gpu", launched[-1])
            self.assertNotIn("--use-angle=gl-egl", launched[-1])

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agy_remote_preview.cli.capture_preview", return_value={"url": "http://127.0.0.1:8780", "http_status": 200, "load_ms": 1.0, "total_ms": 2.0, "console_errors": [], "http_failures": []}) as cap_mock, \
             contextlib.redirect_stdout(io.StringIO()):
            main(["http://127.0.0.1:8780", "--out-dir", tmp, "--webgl", "--json"])
            self.assertTrue(cap_mock.call_args.kwargs["webgl"])


if __name__ == "__main__":
    unittest.main()
