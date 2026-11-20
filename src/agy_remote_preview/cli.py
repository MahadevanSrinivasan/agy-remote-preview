"""CLI entrypoint and remote-control daemon diagnostics for agy-remote-preview."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from .capture import capture_preview, render_artifact_markdown


def check_daemon() -> dict[str, Any]:
    """Inspect agy remote-control daemon status and systemd Linger state."""
    user = os.environ.get("USER") or getpass.getuser()
    host = socket.gethostname()
    result: dict[str, Any] = {
        "user": user,
        "host": host,
        "agy_status": "not-installed",
        "linger": "unknown",
        "warnings": [],
        "suggestions": [],
    }
    agy_bin = shutil.which("agy")
    if agy_bin:
        try:
            proc = subprocess.run([agy_bin, "remote-control", "status"], check=False, capture_output=True, text=True, timeout=15)
            out = (proc.stdout or proc.stderr or "").strip()
            result["agy_status"] = out or f"exit={proc.returncode}"
            if "inactive" in out.lower() or proc.returncode != 0:
                result["warnings"].append("agy remote-control daemon is not active.")
                result["suggestions"].append(f"agy remote-control start --name {host}")
        except (OSError, subprocess.SubprocessError) as exc:
            result["agy_status"] = f"error: {exc}"
    else:
        result["warnings"].append("agy CLI not found on PATH.")

    loginctl = shutil.which("loginctl")
    if loginctl:
        try:
            proc = subprocess.run(
                [loginctl, "show-user", user, "-p", "Linger"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            linger_out = (proc.stdout or proc.stderr or "").strip()
            result["linger"] = linger_out
            if "Linger=no" in linger_out:
                result["warnings"].append(f"systemd user Linger=no for {user}; daemon will stop on logout.")
                result["suggestions"].insert(0, f"sudo loginctl enable-linger {user}")
                if not any("remote-control start" in s for s in result["suggestions"]):
                    result["suggestions"].append(f"agy remote-control start --name {host}")
        except (OSError, subprocess.SubprocessError) as exc:
            result["linger"] = f"error: {exc}"
    return result


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for agy-remote-preview."""
    parser = argparse.ArgumentParser(
        prog="agy-remote-preview",
        description="Capture dual-viewport headless CDP previews and Antigravity artifacts.",
    )
    parser.add_argument("url", nargs="?", help="Target http(s) URL to preview")
    parser.add_argument("--out-dir", default=".", help="Output directory (default: .)")
    parser.add_argument("--prefix", default="preview", help="Output PNG filename prefix (default: preview)")
    parser.add_argument("--settle-ms", type=int, default=350, help="Post-load settle delay in ms (default: 350)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Overall timeout in seconds (default: 20.0)")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.add_argument("--webgl", action="store_true", help="Enable ANGLE/EGL GPU flags for WebGL/canvas apps")
    parser.add_argument("--check-daemon", action="store_true", help="Check agy remote-control and loginctl Linger")
    return parser


def _print_daemon_report(info: dict[str, Any]) -> None:
    print(f"agy remote-control: {info['agy_status']}")
    print(f"loginctl ({info['user']}): {info['linger']}")
    for warn in info["warnings"]:
        print(f"WARNING: {warn}")
    for cmd in info["suggestions"]:
        print(f"  Suggested: {cmd}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.check_daemon and not args.url:
        parser.error("url is required unless --check-daemon is specified")

    daemon_info = check_daemon() if args.check_daemon else None
    if daemon_info is not None and not args.url:
        if args.json:
            print(json.dumps(daemon_info, indent=2))
        else:
            _print_daemon_report(daemon_info)
        return 0

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = capture_preview(
        args.url,
        out_dir,
        prefix=args.prefix,
        settle_ms=args.settle_ms,
        timeout_s=args.timeout,
        webgl=args.webgl,
    )
    desktop_abs = str((out_dir / f"{args.prefix}_desktop.png").resolve())
    mobile_abs = str((out_dir / f"{args.prefix}_mobile.png").resolve())
    artifact_path = out_dir / "remote-preview.md"
    artifact_path.write_text(render_artifact_markdown(report, desktop_abs, mobile_abs), encoding="utf-8")
    report["artifact_md"] = str(artifact_path)
    if daemon_info is not None:
        report["daemon"] = daemon_info

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if daemon_info is not None:
            _print_daemon_report(daemon_info)
        print(
            f"Preview captured: {report['url']} (HTTP {report['http_status']}, "
            f"load={report['load_ms']}ms, total={report['total_ms']}ms, "
            f"console_errors={len(report['console_errors'])}, http_failures={len(report['http_failures'])})"
        )
        print(f"Artifact: {artifact_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
