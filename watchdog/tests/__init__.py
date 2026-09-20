"""Test package initialization.

These tests import modules as `watchdog...`, the package that lives in the
checkout root next to `watchdog_run.py`. When running from inside the
`watchdog` directory, Python's default sys.path does not include the checkout
root, so `import watchdog` fails. We fix that here so `python3 -m unittest`
works from any checkout, whatever the directory is named.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_checkout_root_on_path() -> None:
	try:
		here = Path(__file__).resolve()
		# .../<checkout>/watchdog/tests/__init__.py -> parents[2] == .../<checkout>
		root = str(here.parents[2])
		if root not in sys.path:
			sys.path.insert(0, root)
	except Exception:
		return


_ensure_checkout_root_on_path()
