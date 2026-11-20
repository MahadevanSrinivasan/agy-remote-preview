"""Headless Chromium CDP capture engine and Antigravity artifact renderer."""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cdp import CDPConnection, fetch_json

_BROWSER_CANDIDATES = ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable")
_FALLBACK_PATHS = (
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_browser() -> str:
    """Locate a Chromium or Chrome executable using env, PATH, and standard paths."""
    env_bin = os.environ.get("CHROMIUM_BIN", "").strip()
    if env_bin:
        resolved = shutil.which(env_bin) or (env_bin if Path(env_bin).exists() else None)
        if resolved:
            return resolved
    for name in _BROWSER_CANDIDATES:
        if found := shutil.which(name):
            return found
    for path_str in _FALLBACK_PATHS:
        if Path(path_str).exists():
            return path_str
    raise FileNotFoundError("No Chromium/Chrome binary found; set CHROMIUM_BIN.")


def _is_snap_chromium(binary: str) -> bool:
    """Return True if binary is a Snap Chromium binary or wrapper script."""
    if "/snap/" in binary or "/snap/" in str(Path(binary).resolve()):
        return True
    p = Path(binary)
    try:
        return p.is_file() and p.stat().st_size <= 8192 and b"/snap/bin/chromium" in p.read_bytes()
    except OSError:
        return False


def _make_profile_dir(binary: str) -> Path:
    """Create an ephemeral user-data-dir accessible to Snap or native Chromium."""
    if _is_snap_chromium(binary):
        base = Path.home() / "snap" / "chromium" / "common"
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="agy_rprev_", dir=str(base)))
    return Path(tempfile.mkdtemp(prefix="agy_rprev_"))


def _free_port() -> int:
    """Allocate an ephemeral free localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_page_ws(port: int, proc: subprocess.Popen[bytes], timeout_s: float) -> str:
    """Poll CDP /json/list until a page target exposes webSocketDebuggerUrl."""
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/json/list"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Chromium exited early with code {proc.returncode}")
        with contextlib.suppress(OSError, ValueError, HTTPException):
            for target in fetch_json(url, timeout=1.0):
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting {timeout_s}s for Chromium CDP on port {port}")


def _terminate_browser(proc: subprocess.Popen[bytes]) -> None:
    """Terminate Chromium and its process group, escalating to kill if needed."""
    if proc.poll() is not None:
        return
    sig_fn = (lambda sig: os.killpg(proc.pid, sig)) if os.name == "posix" else (
        lambda sig: proc.terminate() if sig == signal.SIGTERM else proc.kill()
    )
    try:
        sig_fn(signal.SIGTERM)
        proc.wait(timeout=5.0)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            sig_fn(signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5.0)


def _format_console_args(args: list[dict[str, Any]]) -> str:
    return " ".join(str(a.get("value", a.get("description", a.get("type", "unknown")))) for a in args).strip()


def _collect_telemetry(events: list[dict[str, Any]]) -> tuple[int | None, list[str], list[str]]:
    """Extract main document HTTP status, console/JS errors, and network failures."""
    http_status: int | None = None
    console_errors: list[str] = []
    http_failures: list[str] = []
    for ev in events:
        method, params = ev.get("method"), ev.get("params", {})
        if method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "assert"}:
            console_errors.append(_format_console_args(params.get("args", [])) or f"console.{params.get('type')}")
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text") or "Uncaught JS exception"
            console_errors.append(str(text))
        elif method == "Log.entryAdded" and params.get("entry", {}).get("level") == "error":
            console_errors.append(str(params["entry"].get("text", "Browser log error")))
        elif method == "Network.responseReceived":
            resp = params.get("response", {})
            status = resp.get("status")
            if (
                params.get("type") == "Document"
                and isinstance(status, int)
                and str(resp.get("url", "")).startswith(("http://", "https://"))
            ):
                http_status = status
            if isinstance(status, int) and status >= 400:
                http_failures.append(f"{status} {resp.get('url', '')}".strip())
        elif method == "Network.loadingFailed":
            http_failures.append(f"{params.get('errorText', 'loading failed')} ({params.get('type', 'resource')})")
    return http_status, console_errors, http_failures


def _capture_png(cdp: CDPConnection, path: Path, timeout_s: float) -> int:
    res = cdp.call("Page.captureScreenshot", {"format": "png"}, timeout=timeout_s)
    raw = base64.b64decode(res["data"])
    path.write_bytes(raw)
    return len(raw)


def capture_preview(
    url: str,
    out_dir: str | Path,
    prefix: str = "preview",
    settle_ms: int = 350,
    timeout_s: float = 20.0,
    webgl: bool = False,
) -> dict[str, Any]:
    """Capture desktop + mobile screenshots and console/network telemetry for url."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid http(s) URL: {url!r}")
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    binary = find_browser()
    prof = _make_profile_dir(binary)
    port = _free_port()
    gpu_flags = (
        [
            "--disable-gpu-sandbox", "--use-gl=angle",
            "--use-angle=gl-egl", "--ignore-gpu-blocklist",
        ]
        if webgl
        else [
            "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--disable-background-networking", "--disable-sync",
            "--disable-default-apps", "--mute-audio",
        ]
    )
    cmd = [
        binary, "--headless=new", "--no-sandbox", *gpu_flags,
        f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={prof}", "about:blank",
    ]
    t_start = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"))
    cdp: CDPConnection | None = None
    try:
        cdp = CDPConnection(_wait_for_page_ws(port, proc, timeout_s), timeout=timeout_s)
        for domain in ("Page", "Runtime", "Log", "Network"):
            cdp.call(f"{domain}.enable", timeout=timeout_s)
        ev_start, t_nav = len(cdp.events), time.monotonic()
        cdp.call("Page.navigate", {"url": url}, timeout=timeout_s)
        cdp.wait_for_event("Page.loadEventFired", timeout=timeout_s, start_index=ev_start)
        load_ms = round((time.monotonic() - t_nav) * 1000.0, 2)
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False}, timeout=timeout_s)
        cdp.drain_events(settle_ms / 1000.0)
        title_res = cdp.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, timeout=timeout_s)
        title = str(title_res.get("result", {}).get("value") or "")
        desktop_png, mobile_png = out_path / f"{prefix}_desktop.png", out_path / f"{prefix}_mobile.png"
        desktop_bytes = _capture_png(cdp, desktop_png, timeout_s)
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True}, timeout=timeout_s)
        cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True}, timeout=timeout_s)
        cdp.drain_events(0.15)
        mobile_bytes = _capture_png(cdp, mobile_png, timeout_s)
        http_status, console_errors, http_failures = _collect_telemetry(cdp.events)
        return {
            "url": url, "title": title, "http_status": http_status,
            "load_ms": load_ms, "total_ms": round((time.monotonic() - t_start) * 1000.0, 2),
            "console_errors": console_errors, "http_failures": http_failures,
            "desktop_bytes": desktop_bytes, "mobile_bytes": mobile_bytes,
            "desktop_png": str(desktop_png), "mobile_png": str(mobile_png),
        }
    finally:
        if cdp is not None:
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                cdp.call("Browser.close", timeout=2.0)
            cdp.close()
        _terminate_browser(proc)
        for _ in range(5):
            if not prof.exists():
                break
            shutil.rmtree(prof, ignore_errors=True)
            if prof.exists():
                time.sleep(0.05)


def render_artifact_markdown(report: dict[str, Any], desktop_abs: str, mobile_abs: str) -> str:
    """Render Antigravity-compatible carousel markdown with status callout and metrics."""
    console_errs, http_fails = report.get("console_errors", []), report.get("http_failures", [])
    if not console_errs and not http_fails:
        callout = "> [!TIP]\n> Zero console/JS errors and zero HTTP 4xx/5xx failures detected."
    else:
        lines = ["> [!WARNING]", "> Runtime or network issues detected during preview capture:"]
        lines.extend(f"> - Console/JS: `{err}`" for err in console_errs)
        lines.extend(f"> - HTTP: `{err}`" for err in http_fails)
        callout = "\n".join(lines)
    status = report.get("http_status")
    sizes = f"{report.get('desktop_bytes', 0)} B / {report.get('mobile_bytes', 0)} B"
    return (
        f"# Remote Preview: {report.get('url', '')}\n\n{callout}\n\n"
        f"````carousel\n![Desktop 1280x800]({desktop_abs})\n<!-- slide -->\n![Mobile 390x844]({mobile_abs})\n````\n\n"
        "| Metric | Value |\n| :--- | :--- |\n"
        f"| URL | `{report.get('url', '')}` |\n| Title | {report.get('title') or '(untitled)'} |\n"
        f"| HTTP Status | {status if status is not None else 'n/a'} |\n"
        f"| Page Load ms | {report.get('load_ms', 0)} |\n| Total Capture ms | {report.get('total_ms', 0)} |\n"
        f"| Console/JS Errors | {len(console_errs)} |\n| Network 4xx/5xx | {len(http_fails)} |\n"
        f"| Desktop/Mobile PNG Sizes | {sizes} |\n"
    )
