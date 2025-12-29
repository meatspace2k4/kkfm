"""Test package initialization.

These tests import modules as `kkfm.watchdog...`.
When running from inside the `kkfm` directory (CWD=.../kkfm), Python's default
sys.path does not include the parent directory, so `import kkfm` fails.

We fix that here so `python3 -m unittest` works from the repo checkout without
requiring environment variables.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_repo_parent_on_path() -> None:
	try:
		here = Path(__file__).resolve()
		# .../kkfm/watchdog/tests/__init__.py -> parents[3] == .../Aircheck
		repo_parent = str(here.parents[3])
		if repo_parent not in sys.path:
			sys.path.insert(0, repo_parent)
	except Exception:
		return


_ensure_repo_parent_on_path()



