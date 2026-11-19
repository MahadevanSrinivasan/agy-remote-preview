# agy-remote-preview

Zero-dependency headless Chromium CDP dual-viewport preview (`1280x800` desktop + `390x844` mobile), console/JS/HTTP error collector, and Antigravity artifact carousel generator.

## Problem & Thesis

When running Antigravity over SSH or `agy remote-control`, agents often lack a lightweight, deterministic way to render a local dev server across desktop and mobile viewports, capture runtime console/network failures, and produce an embeddable visual artifact without pulling in Node, Playwright, or Puppeteer.

`agy-remote-preview` implements an RFC 6455 WebSocket client and Chrome DevTools Protocol (CDP) driver in pure Python 3.12 stdlib:
1. **Zero Runtime Dependencies**: Pure Python stdlib (`socket`, `struct`, `hashlib`, `urllib`, `json`).
2. **Dual-Viewport + Telemetry**: Captures `1280x800` (1x DPR) desktop and `390x844` (2x DPR, touch-enabled) mobile PNGs alongside `Runtime.consoleAPICalled`, `Runtime.exceptionThrown`, `Log.entryAdded`, and `Network` 4xx/5xx failures.
3. **Snap-Safe Ephemeral Profiles**: Detects Ubuntu Snap Chromium wrappers (`/usr/bin/chromium-browser` -> `/snap/bin/chromium`) and allocates temporary profiles under `~/snap/chromium/common` with guaranteed cleanup.
4. **Antigravity-Native Output**: Emits `remote-preview.md` with a `carousel` block, `[!TIP]`/`[!WARNING]` status callout, and compact metrics table.

---

## 60-Second Quickstart

### 1. Run Directly From Checkout (Zero Install)

```bash
PYTHONPATH=src python3 -m agy_remote_preview.cli http://127.0.0.1:8780 --out-dir ./preview-out --json
```

### 2. Editable Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

agy-remote-preview http://127.0.0.1:8780 --out-dir ./preview-out --json
```

For Three.js, WebGL, or hardware-accelerated canvas apps, opt into ANGLE/EGL GPU flags with `--webgl` (by default, `agy-remote-preview` passes `--disable-gpu` to avoid spawning an extra GPU process on headless Linux hosts):

```bash
agy-remote-preview http://127.0.0.1:8780 --out-dir ./preview-out --webgl --json
```

### 3. Check `agy remote-control` & Linux Linger Status

```bash
agy-remote-preview --check-daemon
```

If `Linger=no` is reported on systemd-based Linux hosts, enable linger so the daemon survives SSH disconnect:

```bash
sudo loginctl enable-linger "$USER"
agy remote-control start --name "$(hostname)"
```

---

## Architecture Overview

```
agy-remote-preview/
├── .antigravityignore
├── .gitignore
├── LICENSE
├── pyproject.toml
├── README.md
├── skills/
│   └── remote-preview/
│       ├── SKILL.md
│       └── scripts/
│           └── run_preview.py
├── src/
│   └── agy_remote_preview/
│       ├── __init__.py       # Public exports & version
│       ├── cdp.py            # Stdlib RFC 6455 WebSocket & CDP JSON-RPC client
│       ├── capture.py        # Headless Chromium launcher, CDP capture, artifact renderer
│       └── cli.py            # CLI flags, daemon check, artifact writer
└── tests/
    └── test_preview.py       # Deterministic unit & headless integration tests
```

---

## Capabilities & Architecture Comparison

| Capability | 2-Process `chromium --screenshot` | `agy-remote-preview` (Single-Process CDP) |
| :--- | :--- | :--- |
| **Chromium launches** | `2` (separate desktop & mobile spawns) | `1` (in-place viewport & touch emulation switch) |
| **Desktop + mobile viewports** | Separate cold runs | `1280×800` + `390×844 @ 2x` touch reflow in one instance |
| **JS console / uncaught exception telemetry** | No | Yes (`Runtime.consoleAPICalled`, `Runtime.exceptionThrown`, `Log.entryAdded`) |
| **Network `4xx`/`5xx` audit** | No | Yes (`Network.responseReceived`, `Network.loadingFailed`) |
| **Snap Chromium `/tmp` confinement** | Fails without manual profile path | Auto-detects wrapper and isolates profiles in `~/snap/chromium/common` |
| **Antigravity `carousel` artifact generation** | No | Yes (`remote-preview.md` + `[!TIP]`/`[!WARNING]` + metrics table) |
| **GPU mode** | Manual flags per invocation | `--disable-gpu` default + opt-in `--webgl` (ANGLE/EGL) |

---

## Install as an Antigravity Skill

Copy the bundled skill into your workspace `.agents/skills/` or global `~/.gemini/antigravity/skills/` directory:

```bash
mkdir -p .agents/skills/remote-preview
cp -r skills/remote-preview/* .agents/skills/remote-preview/
```

Invoke the skill script from any directory inside the repo:

```bash
python3 skills/remote-preview/scripts/run_preview.py http://127.0.0.1:8780 --out-dir ./preview-out
```

---

## Reproducible Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## License

MIT License — see [LICENSE](./LICENSE).
