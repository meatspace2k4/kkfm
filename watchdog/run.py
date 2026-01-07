#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import json
import select
import os
import shutil
import re
import uuid
from dataclasses import replace
from pathlib import Path

from .assets_check import (
    check_assets_blob_ranges,
    check_assets_h_sizes,
    scan_stream_bwav_dir,
)
from .anomalies import Anomaly, AnomalyCode, Severity
from .detector import Detector
from .events import Event, EventType
from .log_parser import parse_event
from .reporting import Reporter
from .tx_monitor import monitor_tx
from .dashboard import run_dashboard
from .ansi import strip_ansi
from .paths import get_runtime_status_dir
from .recovery import AutoRestartConfig, AutoRestarter


def _event_level(ev: Event) -> str:
    """Map internal EventType -> dashboard-friendly log level."""
    try:
        et = ev.etype
    except Exception:
        return "INFO"

    if et == EventType.NOW_PLAYING:
        return "INFO"

    if et in (
        EventType.UNIT_STARTED,
        EventType.JINGLE_PICKED,
        EventType.JINGLE_START,
        EventType.JINGLE_END,
        EventType.COMM_PICKED,
        EventType.COMM_START,
        EventType.COMM_END,
    ):
        return "INFO"

    if et in (EventType.JINGLE_FAILED, EventType.COMM_FAILED):
        return "ERROR"

    if et in (EventType.DAC_THROTTLE,):
        return "WARN"

    if et in (EventType.DAC_OVERFLOW, EventType.DAC_UNDERRUN):
        return "ERROR"

    if et in (EventType.UNIT_STOPPED,):
        return "CRIT"

    if et in (EventType.SAFE_SHUTDOWN,):
        return "WARN"

    return "INFO"


def _kkfm_root() -> Path:
    # kkfm/watchdog/run.py -> kkfm/
    return Path(__file__).resolve().parents[1]


def _env_or_default(env_key: str, default: Path) -> str:
    v = os.environ.get(env_key)
    return v if v else str(default)


def _maybe_redirect_status_path(p: Path) -> Path:
    """Redirect status writes off the SD card by default.

    Set env `WATCHDOG_STATUS_TO_DISK=1` to keep the provided path.
    """
    if os.environ.get("WATCHDOG_STATUS_TO_DISK") == "1":
        return p

    # If it's already in a typical tmpfs area, keep it.
    s = str(p)
    if s.startswith("/run/") or s.startswith("/dev/shm/") or s.startswith("/tmp/"):
        return p

    # Otherwise, push to runtime tmpfs.
    return get_runtime_status_dir() / p.name


def _is_volatile_runtime_dir(p: Path) -> bool:
    s = str(p)
    return s.startswith("/run/") or s.startswith("/dev/shm/") or s.startswith("/tmp/")


def _clear_runtime_state(*, runtime_dir: Path, clear_status: bool, clear_anomalies: bool, clear_flags: bool) -> None:
    """Best-effort cleanup of runtime/tmpfs watchdog state.

    This exists to prevent stale state from persisting across service restarts,
    which makes happy-path testing confusing.

    Safety: only clears when the runtime dir looks like volatile storage.
    """
    if not _is_volatile_runtime_dir(runtime_dir):
        return

    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    if clear_status:
        for name in ("log_status.json", "tx_status.json"):
            try:
                (runtime_dir / name).unlink(missing_ok=True)
            except Exception:
                pass

    if clear_anomalies:
        try:
            (runtime_dir / "anomalies.jsonl").unlink(missing_ok=True)
        except Exception:
            pass

    if clear_flags:
        flags_dir = runtime_dir / "flags"
        try:
            if flags_dir.exists():
                for p in flags_dir.glob("*.flag"):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass


def _clear_all_runtime_state_for_uid(*, clear_status: bool, clear_anomalies: bool, clear_flags: bool) -> None:
    """Clear state across all tmpfs runtime candidates for the current uid.

    Rationale: the dashboard has (and historically needed) fallbacks across multiple
    runtime dirs. If an older instance ever wrote to /run/user/$UID or /dev/shm,
    those can stick around and look like "stale" data even after restarting the
    systemd service that writes to /run/kkfm-watchdog.
    """
    uid = os.getuid()
    candidates = [
        get_runtime_status_dir(),
        Path(f"/run/user/{uid}/kkfm-watchdog"),
        Path(f"/dev/shm/kkfm-watchdog-{uid}"),
    ]
    seen: set[str] = set()
    for d in candidates:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        _clear_runtime_state(
            runtime_dir=d,
            clear_status=clear_status,
            clear_anomalies=clear_anomalies,
            clear_flags=clear_flags,
        )


def _journal_json_to_message_and_ts(raw: str) -> tuple[str | None, float | None]:
    """Parse a single `journalctl -o json` line.

    Returns (message, ts_epoch_seconds). If parsing fails, returns (None, None).
    """
    try:
        obj = json.loads(raw)
    except Exception:
        return None, None

    msg = obj.get("MESSAGE")
    if isinstance(msg, list):
        # Sometimes MESSAGE is serialized as a list of byte values.
        try:
            msg = bytes(int(x) & 0xFF for x in msg).decode("utf-8", errors="replace")
        except Exception:
            msg = None
    if not isinstance(msg, str):
        msg = None

    ts = None
    rt = obj.get("__REALTIME_TIMESTAMP")
    if isinstance(rt, str) and rt.isdigit():
        try:
            ts = int(rt) / 1_000_000.0
        except Exception:
            ts = None
    return msg, ts


def cmd_monitor_logs(args: argparse.Namespace) -> int:
    runtime_dir = get_runtime_status_dir()
    # Default behavior for systemd services: clear stale status/flags on start,
    # but PRESERVE anomaly history so admins can see what happened before restart.
    # Opt out with WATCHDOG_CLEAR_RUNTIME_ON_START=0.
    clear_on_start_default = "1" if os.environ.get("RUNTIME_DIRECTORY") else "0"
    if os.environ.get("WATCHDOG_CLEAR_RUNTIME_ON_START", clear_on_start_default) == "1":
        _clear_all_runtime_state_for_uid(clear_status=True, clear_anomalies=False, clear_flags=True)

    rep = Reporter(out_dir=args.out_dir)
    
    # Emit startup anomaly so the admin can see when the watchdog (re)started
    rep.emit(Anomaly(
        code=AnomalyCode.WATCHDOG_STARTED,
        severity=Severity.INFO,
        ts=time.time(),
        message=f"Log watchdog started (PID {os.getpid()}). Monitoring {args.unit}.",
    ))
    det = Detector(
        start_timeout_s=float(getattr(args, "start_timeout", 60.0)),
        end_timeout_s=float(getattr(args, "end_timeout", 45.0)),
        dead_air_timeout_s=float(getattr(args, "dead_air_timeout", 300.0)),
    )

    cfg = AutoRestartConfig.from_env(unit=str(args.unit))
    if bool(getattr(args, "auto_restart", False)):
        cfg = replace(cfg, enabled=True)
    auto_restarter = AutoRestarter(cfg)

    # Epoch/session id for this monitor process. Persisted with every status snapshot so
    # consumers (dashboard) can detect restarts and drop sticky state.
    epoch = uuid.uuid4().hex
    proc_start_ts = time.time()
    proc_pid = os.getpid()
    status_arg = getattr(args, "status_path", None)
    status_p: Path | None
    if not status_arg:
        # Default to runtime tmpfs to avoid SD wear.
        status_p = runtime_dir / "log_status.json"
    else:
        status_p = _maybe_redirect_status_path(Path(status_arg))
    status_write_min_interval_s = float(getattr(args, "status_write_min_interval", 1.0))
    last_status_write_mono = 0.0
    last_ev = None
    last_bwav: dict | None = None

    # Match the log formats emitted by the player:
    # - [CACHE] <name>.bwav (id=..., size=...)
    # - [FILE]  <name>.bwav (id=..., size=...)
    # - [BWAV]  <name>.bwav | Len: ...   (legacy/first-load parser log)
    bwav_cache_re = re.compile(r"\[CACHE\]\s+(?P<name>[^()|]+?)\s*\(", re.IGNORECASE)
    bwav_file_re = re.compile(r"\[(?:FILE|BWAV)\]\s+(?P<name>[^()|]+?)\s*(?:\(|\|)", re.IGNORECASE)

    def maybe_note_bwav(raw_line: str, ts: float) -> bool:
        nonlocal last_bwav
        s = strip_ansi(raw_line).strip()
        src = None
        name = ""

        m = bwav_cache_re.search(s)
        if m:
            src = "CACHE"
            name = (m.group("name") or "").strip()
        else:
            m2 = bwav_file_re.search(s)
            if m2:
                src = "FILE"
                name = (m2.group("name") or "").strip()

        if not src:
            return False
        if not name:
            return False
        last_bwav = {"ts": ts, "name": name, "raw": s, "src": src}
        return True

    def maybe_write_status(now_ts: float, *, force: bool = False) -> None:
        nonlocal last_status_write_mono
        if status_p is None:
            return
        now_mono = time.monotonic()
        if (not force) and (now_mono - last_status_write_mono) < status_write_min_interval_s:
            return
        last_status_write_mono = now_mono

        snap = {
            "ts": now_ts,
            "epoch": epoch,
            "proc_start_ts": proc_start_ts,
            "pid": proc_pid,
            "unit": args.unit,
            "since": args.since,
            "last_event": (
                {
                    "ts": getattr(last_ev, "ts", None),
                    "etype": getattr(getattr(last_ev, "etype", None), "value", None),
                    "level": _event_level(last_ev) if last_ev is not None else None,
                    "name": getattr(last_ev, "name", None),
                    "raw": getattr(last_ev, "raw", None),
                }
                if last_ev is not None
                else None
            ),
            "last_bwav": last_bwav,
            "detector": det.snapshot(),
        }

        status_p.parent.mkdir(parents=True, exist_ok=True)
        # Use a unique tmp name to avoid collisions when multiple instances overlap
        # (e.g. systemd restart loops). Shared `.tmp` can be renamed away by another process.
        tmp = status_p.with_suffix(status_p.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(status_p)

    if args.input_file:
        src = Path(args.input_file).read_text(errors="ignore").splitlines()
        base_ts = time.time()
        for i, line in enumerate(src):
            # If the log line lacks an embedded timestamp, give each line a distinct,
            # monotonic default timestamp so timing-based detectors behave sensibly.
            now = base_ts + (i * 0.001)
            if maybe_note_bwav(line, now):
                maybe_write_status(now, force=True)
            ev = parse_event(line, default_ts=now)
            if not ev:
                continue
            last_ev = ev
            for a in det.feed(ev):
                rep.emit(a)
                action = auto_restarter.consider(a)
                if action is not None:
                    rep.emit(action)
            maybe_write_status(ev.ts)
        maybe_write_status(base_ts + (len(src) * 0.001))
        return 0

    if args.stdin:
        for raw in sys.stdin:
            now = time.time()
            if maybe_note_bwav(raw, now):
                maybe_write_status(now, force=True)
            ev = parse_event(raw, default_ts=now)
            if not ev:
                maybe_write_status(now)
                continue
            last_ev = ev
            for a in det.feed(ev):
                rep.emit(a)
            maybe_write_status(now)
        return 0

    # Live journalctl
    # Use JSON output so we never get hard-wrapped lines and we can use the
    # journal's own timestamp for consistent timeout logic.
    journal_cmd: list[str] = [
        "journalctl",
        "--no-pager",
        "-u",
        args.unit,
        "-f",
        "--since",
        args.since,
        "-o",
        "json",
        "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
    ]

    # IMPORTANT: when journalctl's stdout is a pipe (as it is here), it may
    # buffer its output. That causes events like "Now Playing: ..." to appear
    # "stuck" until some later log line arrives and triggers a flush.
    # Force line-buffering when possible.
    if shutil.which("stdbuf"):
        journal_cmd = ["stdbuf", "-oL", "-eL", *journal_cmd]
    proc = subprocess.Popen(
        journal_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        bufsize=0,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    buf = b""
    try:
        while True:
            # Wait for data with a 1s timeout so we can run detector poll()
            r, _, _ = select.select([fd], [], [], 1.0)
            now = time.time()
            
            if r:
                while True:
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        # EOF
                        break
                    buf += chunk
                if not buf and proc.poll() is not None:
                    break

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    raw = line.decode("utf-8", errors="replace")

                    msg, msg_ts = _journal_json_to_message_and_ts(raw)
                    if msg is not None:
                        if maybe_note_bwav(msg, msg_ts or now):
                            maybe_write_status(msg_ts or now, force=True)
                        ev = parse_event(msg, default_ts=msg_ts or now)
                        if ev:
                            last_ev = ev
                            for a in det.feed(ev):
                                rep.emit(a)
                                action = auto_restarter.consider(a)
                                if action is not None:
                                    rep.emit(action)
                            # Keep periodic checks aligned to the journal timeline.
                            now = ev.ts
            
            # Periodic checks (dead air, etc)
            for a in det.poll(now):
                rep.emit(a)
                action = auto_restarter.consider(a)
                if action is not None:
                    rep.emit(action)
            
            maybe_write_status(now)
            
            if proc.poll() is not None:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    return 0


def cmd_monitor_tx(args: argparse.Namespace) -> int:
    runtime_dir = get_runtime_status_dir()
    # Preserve anomaly history across reboots - only clear stale status/flags
    clear_on_start_default = "1" if os.environ.get("RUNTIME_DIRECTORY") else "0"
    if os.environ.get("WATCHDOG_CLEAR_RUNTIME_ON_START", clear_on_start_default) == "1":
        _clear_all_runtime_state_for_uid(clear_status=True, clear_anomalies=False, clear_flags=True)

    rep = Reporter(out_dir=args.out_dir)

    # Emit startup anomaly so the admin can see when the TX watchdog (re)started
    rep.emit(Anomaly(
        code=AnomalyCode.WATCHDOG_STARTED,
        severity=Severity.INFO,
        ts=time.time(),
        message=f"TX watchdog started (PID {os.getpid()}). Monitoring {args.port}.",
    ))

    def emit(a) -> None:
        rep.emit(a)

    monitor_tx(
        port=args.port,
        baudrate=int(args.baudrate),
        heartbeat_timeout_s=float(args.heartbeat_timeout),
        start_grace_s=float(args.start_grace),
        emit_cooldown_s=float(args.emit_cooldown),
        status_path=(
            str(runtime_dir / "tx_status.json")
            if not getattr(args, "status_path", None)
            else args.status_path
        ),
        status_write_min_interval_s=float(args.status_write_min_interval),
        out_cb=emit,
    )
    return 0


def cmd_scan_assets(args: argparse.Namespace) -> int:
    rep = Reporter(out_dir=args.out_dir)
    now = time.time()

    for a in scan_stream_bwav_dir(args.stream_dir, ts=now):
        rep.emit(a)
    for a in check_assets_h_sizes(args.assets_h, ts=now):
        rep.emit(a)
    for a in check_assets_blob_ranges(args.assets_h, args.assets_blob, ts=now):
        rep.emit(a)
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    # Run stdlib unittest suite.
    import unittest

    from .test_runner import run_unittest_suite

    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent / "tests"))
    return run_unittest_suite(suite, verbose=bool(getattr(_, "verbose", False)))


def cmd_dashboard(args: argparse.Namespace) -> int:
    ssh_target = getattr(args, "ssh", None) or None
    
    # Clear anomalies first if requested
    if getattr(args, "clear_anomalies", False):
        clear_args = argparse.Namespace(ssh=ssh_target, out_dir=args.out_dir)
        cmd_clear_anomalies(clear_args)
    
    return int(
        run_dashboard(
            out_dir=args.out_dir,
            refresh_s=float(args.refresh),
            ssh_target=ssh_target,
        )
    )


def cmd_clear_anomalies(args: argparse.Namespace) -> int:
    """Clear the anomalies.jsonl log file (local or remote via SSH)."""
    ssh_target = getattr(args, "ssh", None) or None
    
    if ssh_target:
        # Remote clear via SSH - truncate the known location
        cmd = ["ssh", "-o", "ConnectTimeout=5", ssh_target, 
               "truncate", "-s", "0", "/run/kkfm-watchdog/anomalies.jsonl"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print(f"Cleared anomalies on {ssh_target}")
            else:
                print(f"Note: {result.stderr.strip() or 'file may not exist'}")
            return 0
        except subprocess.TimeoutExpired:
            print("SSH command timed out", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"SSH error: {e}", file=sys.stderr)
            return 1
    else:
        # Local clear
        from .paths import get_runtime_status_dir
        
        uid = os.getuid()
        candidates = [
            Path("/run/kkfm-watchdog") / "anomalies.jsonl",
            Path(f"/run/user/{uid}/kkfm-watchdog") / "anomalies.jsonl",
            Path(f"/dev/shm/kkfm-watchdog-{uid}") / "anomalies.jsonl",
            Path(args.out_dir) / "anomalies.jsonl",
        ]
        
        # Also check runtime dir
        try:
            runtime_dir = get_runtime_status_dir()
            candidates.insert(0, runtime_dir / "anomalies.jsonl")
        except Exception:
            pass
        
        cleared = []
        for p in candidates:
            try:
                if p.exists():
                    p.write_text("")
                    cleared.append(str(p))
            except Exception as e:
                print(f"Failed to clear {p}: {e}", file=sys.stderr)
        
        if cleared:
            print(f"Cleared: {', '.join(cleared)}")
        else:
            print("No anomalies.jsonl files found to clear")
        
        return 0


def main() -> int:
    kkfm_root = _kkfm_root()
    watchdog_root = Path(__file__).resolve().parent

    default_out_dir = _env_or_default("WATCHDOG_OUT_DIR", watchdog_root / "out")
    default_stream_dir = _env_or_default(
        "WATCHDOG_STREAM_DIR",
        kkfm_root / "radio" / "Sound" / "Resource" / "Stream",
    )
    default_assets_h = _env_or_default("WATCHDOG_ASSETS_H", kkfm_root / "assets.h")
    default_assets_blob = _env_or_default("WATCHDOG_ASSETS_BLOB", kkfm_root / "assets_blob.bin")

    ap = argparse.ArgumentParser(prog="watchdog/run.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("monitor-logs")
    p.add_argument("--out-dir", default=default_out_dir)
    p.add_argument("--unit", default="acnh-aircheck.service")
    p.add_argument("--since", default="now")
    p.add_argument("--start-timeout", type=float, default=300.0)
    p.add_argument("--end-timeout", type=float, default=45.0)
    p.add_argument("--dead-air-timeout", type=float, default=300.0)
    p.add_argument(
        "--auto-restart",
        action="store_true",
        help="auto-restart the monitored unit on persistent dead-air/underrun (also controlled by WATCHDOG_AUTO_RESTART=1)",
    )
    # Default to runtime tmpfs (status-path is only for the dashboard).
    # Set `WATCHDOG_STATUS_TO_DISK=1` if you explicitly want disk writes.
    p.add_argument("--status-path", default="")
    # Writes a status JSON file for the dashboard; keep this relatively slow to avoid SD wear.
    p.add_argument("--status-write-min-interval", type=float, default=5.0)
    p.add_argument("--stdin", action="store_true", help="read log lines from stdin")
    p.add_argument("--input-file", help="read log lines from file (for debugging)")
    p.set_defaults(func=cmd_monitor_logs)

    p = sub.add_parser("monitor-tx")
    p.add_argument("--out-dir", default=default_out_dir)
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--baudrate", type=int, default=115200)
    p.add_argument("--heartbeat-timeout", type=float, default=2.0)
    p.add_argument("--start-grace", type=float, default=10.0)
    p.add_argument("--emit-cooldown", type=float, default=30.0)
    # Default to runtime tmpfs (status-path is only for the dashboard).
    # Set `WATCHDOG_STATUS_TO_DISK=1` if you explicitly want disk writes.
    p.add_argument("--status-path", default="")
    # Writes a status JSON file for the dashboard; keep this relatively slow to avoid SD wear.
    p.add_argument("--status-write-min-interval", type=float, default=5.0)
    p.set_defaults(func=cmd_monitor_tx)

    p = sub.add_parser("scan-assets")
    p.add_argument("--out-dir", default=default_out_dir)
    p.add_argument("--stream-dir", default=default_stream_dir)
    p.add_argument("--assets-h", default=default_assets_h)
    p.add_argument("--assets-blob", default=default_assets_blob)
    p.set_defaults(func=cmd_scan_assets)

    p = sub.add_parser("selftest")
    p.add_argument("-v", "--verbose", action="store_true", help="print one line per test")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("dashboard")
    p.add_argument("--out-dir", default=default_out_dir)
    p.add_argument("--refresh", type=float, default=1.0)
    p.add_argument(
        "--ssh",
        default="",
        help="Run dashboard locally but read Pi data via ssh (e.g. pi@raspberrypi)",
    )
    p.add_argument(
        "--clear-anomalies", "-c",
        action="store_true",
        dest="clear_anomalies",
        help="Clear the anomalies log before starting the dashboard",
    )
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("clear-anomalies", help="Clear the anomalies log file")
    p.add_argument("--out-dir", default=default_out_dir)
    p.add_argument(
        "--ssh",
        default="",
        help="Clear anomalies on remote host via ssh (e.g. pi@raspberrypi)",
    )
    p.set_defaults(func=cmd_clear_anomalies)

    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())


