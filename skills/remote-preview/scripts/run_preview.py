#!/usr/bin/env python3
"""Skill entrypoint for agy-remote-preview."""

from pathlib import Path
import sys


def _ensure_importable() -> None:
    try:
        import agy_remote_preview.cli  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for parent in here.parents:
        for rel in ("src", "projects/agy-remote-preview/src"):
            candidate = parent / rel
            if (candidate / "agy_remote_preview").is_dir():
                sys.path.insert(0, str(candidate))
                return


def main() -> int:
    _ensure_importable()
    from agy_remote_preview.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
