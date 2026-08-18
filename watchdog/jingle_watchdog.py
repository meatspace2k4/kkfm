#!/usr/bin/env python3
"""
DEPRECATED: legacy watchdog entrypoint.

Use:
  python3 watchdog_run.py monitor-logs
  python3 watchdog_run.py scan-assets
  python3 watchdog_run.py selftest
"""

import os
import sys


def _ensure_repo_root_on_path() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_repo_root_on_path()

from watchdog.run import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())


