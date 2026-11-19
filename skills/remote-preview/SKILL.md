---
name: remote-preview
description: >-
  Headless CDP dual-viewport visual preview (desktop 1280x800 + mobile 390x844),
  console/JS/HTTP error audit, and Antigravity carousel markdown generator for
  local or remote dev servers.
---

# Remote Preview Skill

Use this skill when you need deterministic visual, console, and network verification of a running web app or local dev server (`http://` or `https://`), or when checking `agy remote-control` daemon readiness on Linux.

## Quick Execution

Run the helper script (auto-discovers `agy_remote_preview` from the workspace or installed package):

```bash
python3 skills/remote-preview/scripts/run_preview.py http://127.0.0.1:8780 --out-dir ./preview-out --json
```

If invoked from the repository root with `.agents/skills/`:

```bash
python3 .agents/skills/remote-preview/scripts/run_preview.py http://127.0.0.1:8780 --out-dir ./preview-out --json
```

## Outputs

- `<out-dir>/<prefix>_desktop.png` (`1280x800` at 1x DPR)
- `<out-dir>/<prefix>_mobile.png` (`390x844` at 2x DPR, touch enabled -> `780x1688` PNG)
- `<out-dir>/remote-preview.md` (Antigravity `carousel` markdown + `[!TIP]`/`[!WARNING]` callout + telemetry table)

## Flags

- `url`: Target HTTP/HTTPS URL (required unless `--check-daemon` is passed alone)
- `--out-dir`: Output directory for PNGs and `remote-preview.md` (default: `.`)
- `--prefix`: Filename prefix for PNG outputs (default: `preview`)
- `--settle-ms`: Post-load settle window in milliseconds (default: `350`)
- `--timeout`: Overall CDP capture timeout in seconds (default: `20.0`)
- `--json`: Emit structured JSON telemetry to stdout
- `--webgl`: Enable ANGLE/EGL GPU rendering for Three.js/WebGL/canvas apps (default uses `--disable-gpu` to avoid GPU process overhead on headless Linux hosts)
- `--check-daemon`: Check `agy remote-control status` and Linux `loginctl` linger state

## Workflow Tips

1. When running over SSH or `agy remote-control`, run `--check-daemon` first to confirm `Linger=yes` and an active daemon.
2. Always inspect `console_errors` and `http_failures` in the JSON output before reporting a clean UI build.
3. Copy or embed `<out-dir>/remote-preview.md` into your artifact directory when presenting visual comparisons to the user.
