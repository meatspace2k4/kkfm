from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .anomalies import Anomaly, Severity
from .paths import get_runtime_status_dir

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _rotate_bounded(path: Path, *, max_bytes: int, max_backups: int) -> None:
    """Rotate `path` when it grows beyond `max_bytes`.

    Rotation scheme:
      - path      -> path.1
      - path.1    -> path.2
      - ... up to max_backups

    This bounds total disk/tmpfs usage to roughly:
      (max_backups + 1) * max_bytes
    """
    if max_bytes <= 0 or max_backups <= 0:
        return

    try:
        st = path.stat()
    except FileNotFoundError:
        return
    except Exception:
        return

    if st.st_size < max_bytes:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    # Shift older backups.
    for i in range(max_backups, 1, -1):
        dst = path.with_name(path.name + f".{i}")
        src = path.with_name(path.name + f".{i - 1}")
        try:
            if src.exists():
                src.replace(dst)
        except Exception:
            pass

    # Move current file into .1.
    try:
        path.replace(path.with_name(path.name + ".1"))
    except Exception:
        pass


class Reporter:
    def __init__(
        self,
        out_dir: str,
        flags_dir: Optional[str] = None,
        jsonl_path: Optional[str] = None,
    ) -> None:
        self.out_dir = Path(out_dir)

        # Tests typically write into a TemporaryDirectory under the OS temp dir.
        # In that scenario, do NOT use the shared runtime dirs (which would pollute
        # live dashboards/alerts). Instead, default to writing outputs under out_dir.
        is_temp_out_dir = False
        try:
            tmp_root = Path(tempfile.gettempdir()).resolve()
            is_temp_out_dir = tmp_root in self.out_dir.resolve().parents or self.out_dir.resolve() == tmp_root
        except Exception:
            is_temp_out_dir = False

        # Defaults: avoid SD/USB wear by writing volatile outputs to tmpfs runtime dirs.
        # Opt back into disk writing with:
        # - WATCHDOG_ANOMALIES_TO_DISK=1
        # - WATCHDOG_FLAGS_TO_DISK=1
        anomalies_env = os.environ.get("WATCHDOG_ANOMALIES_TO_DISK")
        flags_env = os.environ.get("WATCHDOG_FLAGS_TO_DISK")

        self._anomalies_to_disk = (anomalies_env == "1") if anomalies_env is not None else is_temp_out_dir
        self._flags_to_disk = (flags_env == "1") if flags_env is not None else is_temp_out_dir

        # Stdout printing is opt-in to keep journald clean.
        # Values: none | errors | all
        self._stdout_mode = (os.environ.get("WATCHDOG_STDOUT_MODE") or "none").strip().lower()
        if self._stdout_mode not in ("none", "errors", "all"):
            self._stdout_mode = "none"

        # Flag write rate limiting to prevent IO flooding on repeated triggers.
        try:
            self._flag_write_min_interval_s = float(os.environ.get("WATCHDOG_FLAG_WRITE_MIN_INTERVAL", "30"))
        except Exception:
            self._flag_write_min_interval_s = 30.0
        self._flag_write_min_interval_s = max(0.0, self._flag_write_min_interval_s)
        self._last_flag_write_mono_by_code: dict[str, float] = {}

        # Bound anomalies growth (critical on tmpfs/ramdisk).
        # Set WATCHDOG_ANOMALIES_MAX_BYTES=0 to disable.
        default_max_bytes = 2 * 1024 * 1024
        try:
            self._anomalies_max_bytes = int(os.environ.get("WATCHDOG_ANOMALIES_MAX_BYTES", str(default_max_bytes)))
        except Exception:
            self._anomalies_max_bytes = default_max_bytes
        self._anomalies_max_bytes = max(0, self._anomalies_max_bytes)

        # Bound anomalies JSONL growth (critical when runtime dir is tmpfs).
        # Env overrides:
        # - WATCHDOG_ANOMALIES_MAX_BYTES (0 disables rotation)
        # - WATCHDOG_ANOMALIES_MAX_BACKUPS
        # Defaults are intentionally small to avoid OOM on low-RAM devices.
        self._anomalies_max_backups = max(0, _parse_int_env("WATCHDOG_ANOMALIES_MAX_BACKUPS", 3))
        self._anomalies_max_bytes = max(0, _parse_int_env("WATCHDOG_ANOMALIES_MAX_BYTES", 2 * 1024 * 1024))

        runtime_dir = get_runtime_status_dir()
        runtime_flags = runtime_dir / "flags"
        runtime_jsonl = runtime_dir / "anomalies.jsonl"

        disk_flags = self.out_dir / "flags"
        disk_jsonl = self.out_dir / "anomalies.jsonl"

        if flags_dir is not None:
            self.flags_dir = Path(flags_dir)
        else:
            self.flags_dir = disk_flags if self._flags_to_disk else runtime_flags

        if jsonl_path is not None:
            self.jsonl_path = Path(jsonl_path)
        else:
            self.jsonl_path = disk_jsonl if self._anomalies_to_disk else runtime_jsonl

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.flags_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        # Best-effort compatibility link: out_dir/anomalies.jsonl -> runtime jsonl
        # This keeps tooling/tests that expect anomalies under `out_dir` working,
        # while still defaulting to tmpfs writes to avoid SD/USB wear.
        if (not self._anomalies_to_disk) and (self.jsonl_path != (self.out_dir / "anomalies.jsonl")):
            try:
                link = self.out_dir / "anomalies.jsonl"
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(self.jsonl_path)
            except Exception:
                pass

    def _stamp(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S")

    def _maybe_truncate_jsonl(self, f) -> None:
        if self._anomalies_max_bytes <= 0:
            return
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size > self._anomalies_max_bytes:
                f.seek(0)
                f.truncate(0)
        except Exception:
            # Never let log bounding break the reporter.
            return

    def emit(self, a: Anomaly) -> None:
        # Optional stdout marker (disabled by default).
        do_print = False
        if self._stdout_mode == "all":
            do_print = True
        elif self._stdout_mode == "errors":
            do_print = a.severity in (Severity.ERROR, Severity.CRITICAL)
        if do_print:
            try:
                parts = ["[WATCHDOG]", a.severity.value.upper(), a.code.value + ":", a.message]
                if a.name:
                    parts.append(f"name={a.name}")
                print(" ".join(parts), flush=True)
            except Exception:
                # Never let logging break the reporter.
                pass

        # JSONL
        # Prevent unbounded growth: rotate when the file gets too large.
        # Best-effort lock to avoid rotate races across processes.
        if self._anomalies_max_bytes > 0 and self._anomalies_max_backups > 0:
            if fcntl is not None:
                try:
                    lock_path = self.jsonl_path.with_name(self.jsonl_path.name + ".lock")
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    with lock_path.open("a", encoding="utf-8") as lock_f:
                        try:
                            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                        except Exception:
                            pass
                        try:
                            _rotate_bounded(
                                self.jsonl_path,
                                max_bytes=self._anomalies_max_bytes,
                                max_backups=self._anomalies_max_backups,
                            )
                        finally:
                            try:
                                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                            except Exception:
                                pass
                except Exception:
                    _rotate_bounded(
                        self.jsonl_path,
                        max_bytes=self._anomalies_max_bytes,
                        max_backups=self._anomalies_max_backups,
                    )
            else:
                _rotate_bounded(
                    self.jsonl_path,
                    max_bytes=self._anomalies_max_bytes,
                    max_backups=self._anomalies_max_backups,
                )

        rec = asdict(a)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.jsonl_path.open("a+", encoding="utf-8") as f:
            # Best-effort inter-process lock to avoid JSONL corruption when multiple
            # watchdog processes append concurrently.
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
            try:
                self._maybe_truncate_jsonl(f)
                f.write(line)
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass

        # Flag file: stable one-per-code (prevents filesystem churn).
        code = a.code.value
        now_mono = time.monotonic()
        last = self._last_flag_write_mono_by_code.get(code, 0.0)
        if self._flag_write_min_interval_s > 0 and (now_mono - last) < self._flag_write_min_interval_s:
            return
        self._last_flag_write_mono_by_code[code] = now_mono

        fname = f"{code}.flag"
        fpath = self.flags_dir / fname
        self.flags_dir.mkdir(parents=True, exist_ok=True)

        # Best-effort compatibility link: out_dir/flags/<code>.flag -> runtime flag
        if (not self._flags_to_disk) and (self.flags_dir != (self.out_dir / "flags")):
            try:
                disk_flags_dir = self.out_dir / "flags"
                disk_flags_dir.mkdir(parents=True, exist_ok=True)
                link = disk_flags_dir / fname
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(fpath)
            except Exception:
                pass

        tmp = fpath.with_suffix(fpath.suffix + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(a.message + "\n")
            if a.name:
                f.write(f"name={a.name}\n")
            if a.raw:
                f.write(f"raw={a.raw}\n")
        try:
            tmp.replace(fpath)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


