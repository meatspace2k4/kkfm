#!/usr/bin/env python3
"""
Repo entrypoint for the KKFM watchdog.

Use:
    python3 kkfm/watchdog_run.py monitor-logs --since now
    python3 kkfm/watchdog_run.py scan-assets
    python3 kkfm/watchdog_run.py selftest

Notes:
    - For the TUI dashboard, use `dashboard --ssh pi@host` to read Pi data remotely.
      The SSH stream is resilient to laptop sleep / Wi-Fi drops and will auto-reconnect.
"""

from __future__ import annotations

import os
import sys


def _ensure_repo_root_on_path() -> None:
    # The watchdog package lives next to this file; the checkout directory can
    # have any name (worktrees, copies), so import it from here, not by name.
    repo_root = os.path.abspath(os.path.dirname(__file__))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def main() -> int:
    _ensure_repo_root_on_path()

    # macOS laptop ergonomics: when using the dashboard with `--ssh`, default to
    # a quicker stale-stream restart after sleep/Wi-Fi drops unless overridden.
    try:
        if sys.platform == "darwin":
            argv = sys.argv[1:]
            if "dashboard" in argv and "--ssh" in argv:
                os.environ.setdefault("WATCHDOG_SSH_STALE_TIMEOUT_S", "8")
                os.environ.setdefault("WATCHDOG_SSH_CONNECT_TIMEOUT_S", "3")
    except Exception:
        pass

    # Single source of truth:
    # Always delegate to the package CLI (which includes the real dashboard).
    try:
        from watchdog.run import main as pkg_main
    except Exception as e:
        raise SystemExit(
            "watchdog package is not importable from this checkout "
            f"({os.path.dirname(os.path.abspath(__file__))}).\n\n"
            f"Original error: {e}"
        )

    return int(pkg_main())


if __name__ == "__main__":
    raise SystemExit(main())


