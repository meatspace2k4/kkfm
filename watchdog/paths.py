import os
import sys
import tempfile
from pathlib import Path

def get_runtime_status_dir() -> Path:
    """Return a runtime directory suitable for watchdog status files.

    Priority:
    - If systemd sets `RUNTIME_DIRECTORY`, use that (best for services).
    - Otherwise, on Linux prefer:
        - `/run/user/$UID/...` when available (tmpfs, per-user)
        - `/dev/shm/...` as a tmpfs fallback (still RAM)
    - On non-Linux (e.g. macOS laptop dashboard), fall back to the OS temp dir.
    """
    # 1. Prefer systemd-managed RuntimeDirectory (provided via env var)
    #    This is the most robust method for services.
    #    Systemd sets RUNTIME_DIRECTORY to something like /run/kkfm-watchdog
    env_runtime = os.environ.get("RUNTIME_DIRECTORY")
    if env_runtime:
        p = Path(env_runtime)
        # Systemd creates the directory, but we ensure it exists just in case
        p.mkdir(parents=True, exist_ok=True)
        return p

    uid = os.getuid()

    # Special-case root: services often run as uid 0 without a user runtime dir.
    # Prefer a stable, dashboard-friendly location.
    if sys.platform.startswith("linux") and uid == 0:
        p = Path("/run/kkfm-watchdog")
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass

    # 2. Non-Linux: don't try Linux-specific runtime dirs.
    if not sys.platform.startswith("linux"):
        p = Path(tempfile.gettempdir()) / f"kkfm-watchdog-{uid}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # 3. Linux user runtime dir (e.g. /run/user/1000) when available.
    xdg_base = Path(f"/run/user/{uid}")
    if xdg_base.exists():
        p = xdg_base / "kkfm-watchdog"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # 4. Linux tmpfs fallback.
    shm = Path("/dev/shm")
    if shm.exists() and os.access(str(shm), os.W_OK):
        p = shm / f"kkfm-watchdog-{uid}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # 5. Last resort: OS temp dir (may be disk on some systems).
    p = Path(tempfile.gettempdir()) / f"kkfm-watchdog-{uid}"
    p.mkdir(parents=True, exist_ok=True)
    return p
