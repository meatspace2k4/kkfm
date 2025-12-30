from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import time
import curses
import select
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import re
import errno

from .paths import get_runtime_status_dir


# Track whether terminal needs restoration (for signal handlers)
_terminal_active = False
_original_terminal_settings = None


def _restore_terminal() -> None:
    """Restore terminal to normal state, safe to call multiple times."""
    global _terminal_active, _original_terminal_settings
    if not _terminal_active:
        return
    _terminal_active = False
    try:
        # Restore terminal settings if we saved them
        if _original_terminal_settings is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _original_terminal_settings)
    except Exception:
        pass
    try:
        # Ensure cursor is visible and terminal is in normal mode
        sys.stdout.write("\033[?25h")  # Show cursor
        sys.stdout.write("\033[?1049l")  # Exit alternate screen buffer
        sys.stdout.write("\033[0m")  # Reset attributes
        sys.stdout.flush()
    except Exception:
        pass


def _signal_handler(signum: int, frame: Any) -> None:
    """Handle SIGTERM/SIGINT to restore terminal before exit."""
    _restore_terminal()
    # Re-raise as KeyboardInterrupt for clean exit from curses.wrapper
    raise KeyboardInterrupt(f"Received signal {signum}")


@dataclass
class ServiceState:
    name: str
    active: str = "?"
    sub: str = "?"
    since: str = ""


def _run(cmd: list[str], timeout_s: float = 1.0) -> str:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _ssh_args(*, connect_timeout_s: float, control_persist_s: float = 60.0) -> list[str]:
    ct = max(1, int(connect_timeout_s))
    return [
        "-T",
        "-q",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={ct}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "TCPKeepAlive=yes",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={max(1, int(control_persist_s))}s",
        "-o",
        "ControlPath=/tmp/kkfm-watchdog-%C",
    ]


def _run_ssh(ssh_target: str, cmd: list[str], timeout_s: float = 1.0) -> str:
    return _run(
        [
            "ssh",
            *_ssh_args(connect_timeout_s=timeout_s, control_persist_s=60.0),
            ssh_target,
            "--",
            *cmd,
        ],
        timeout_s=timeout_s,
    )


def _systemctl_state(unit: str, *, ssh_target: Optional[str] = None) -> ServiceState:
    cmd = [
        "systemctl",
        "show",
        unit,
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "ActiveEnterTimestamp",
        "--no-pager",
    ]
    out = _run_ssh(ssh_target, cmd) if ssh_target else _run(cmd)
    st = ServiceState(name=unit)
    for line in out.splitlines():
        if line.startswith("ActiveState="):
            st.active = line.split("=", 1)[1].strip()
        elif line.startswith("SubState="):
            st.sub = line.split("=", 1)[1].strip()
        elif line.startswith("ActiveEnterTimestamp="):
            st.since = line.split("=", 1)[1].strip()
    return st


def _read_json_line(path: Path, *, ssh_target: Optional[str] = None) -> Optional[dict[str, Any]]:
    try:
        if ssh_target:
            txt = _run_ssh(ssh_target, ["cat", str(path)], timeout_s=1.0).strip()
        else:
            if not path.exists():
                return None
            txt = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not txt:
            return None
        return json.loads(txt)
    except Exception:
        return None


def _tail_jsonl(path: Path, n: int = 10, *, ssh_target: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        if ssh_target:
            raw_out = _run_ssh(ssh_target, ["tail", "-n", str(int(n)), str(path)], timeout_s=1.0)
            lines = raw_out.splitlines()[-n:]
        else:
            if not path.exists():
                return []

            def tail_lines(p: Path, count: int) -> list[str]:
                try:
                    with p.open("rb") as f:
                        f.seek(0, 2)
                        pos = f.tell()
                        if pos <= 0:
                            return []
                        buf = b""
                        while pos > 0 and buf.count(b"\n") <= count:
                            read_sz = 8192 if pos >= 8192 else pos
                            pos -= read_sz
                            f.seek(pos)
                            buf = f.read(read_sz) + buf
                        raw_lines = buf.splitlines()[-count:]
                        return [b.decode("utf-8", errors="ignore") for b in raw_lines]
                except Exception:
                    return []

            lines = tail_lines(path, n)
        out: list[dict[str, Any]] = []
        for raw in lines[-n:]:
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _fmt_age(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    try:
        age = time.time() - float(ts)
        if age < 0:
            age = 0
        if age < 60:
            return f"{age:.0f}s"
        if age < 3600:
            return f"{age/60:.1f}m"
        return f"{age/3600:.1f}h"
    except Exception:
        return "-"


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "-"


def _fmt_value(val: Any) -> str:
    """Format a value without padding (padding happens between fields, not inside them)."""
    try:
        if val is None:
            return "-"
        elif isinstance(val, bool):
            return "1" if val else "0"
        elif isinstance(val, int):
            return str(val)
        elif isinstance(val, float):
            return str(int(val)) if val.is_integer() else f"{val:.1f}"
        else:
            return str(val)
    except Exception:
        return "-"


def _fmt_kv(label: str, val: Any, width: int) -> str:
    """Format `label= value` left-padded to a fixed field width (aligns keys at columns)."""
    s = f"{label}= {_fmt_value(val)}"
    return s if width <= 0 else s.ljust(width)


def run_dashboard(*, out_dir: str, refresh_s: float = 1.0, ssh_target: Optional[str] = None) -> int:
    global _terminal_active, _original_terminal_settings

    # Set up signal handlers for graceful terminal restoration
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(_restore_terminal)

    # Save terminal settings before curses takes over
    try:
        import termios
        _original_terminal_settings = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        _original_terminal_settings = None

    _terminal_active = True

    try:
        return _run_dashboard_impl(
            out_dir=out_dir,
            refresh_s=refresh_s,
            ssh_target=ssh_target,
        )
    finally:
        _restore_terminal()
        # Restore original signal handlers
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)


def _run_dashboard_impl(*, out_dir: str, refresh_s: float, ssh_target: Optional[str]) -> int:
    out_path = Path(out_dir)
    tx_status: Path = out_path / "tx_status.json"
    log_status: Path = out_path / "log_status.json"

    if not ssh_target:
        runtime_dir = get_runtime_status_dir()
        systemd_runtime = Path("/run/kkfm-watchdog")
        candidates = [systemd_runtime, runtime_dir, out_path]

        def _pick_first_existing(paths: list[Path]) -> Optional[Path]:
            for p in paths:
                try:
                    if p.exists():
                        return p
                except Exception:
                    continue
            return None

        tx_status_p = _pick_first_existing([d / "tx_status.json" for d in candidates])
        log_status_p = _pick_first_existing([d / "log_status.json" for d in candidates])

        tx_status = tx_status_p or tx_status
        log_status = log_status_p or log_status

    anomalies_jsonl: Path = out_path / "anomalies.jsonl"

    if not ssh_target:
        runtime_dir = get_runtime_status_dir()
        systemd_runtime = Path("/run/kkfm-watchdog")
        candidates = [systemd_runtime, runtime_dir, out_path]
        anoms = _pick_first_existing([d / "anomalies.jsonl" for d in candidates])
        if anoms is not None:
            anomalies_jsonl = anoms

    units = [
        "acnh-aircheck.service",
        "acnh-aircheck-watchdog.service",
        "acnh-aircheck-tx-watchdog.service",
    ]

    if not ssh_target:
        allow_local = os.environ.get("WATCHDOG_ALLOW_LOCAL_DASHBOARD") == "1"

        if sys.platform == "darwin" and not allow_local:
            raise SystemExit(
                "Dashboard is running locally on macOS with no SSH target.\n\n"
                "The KKFM services and telemetry (systemd units + tx/log status files) usually run on the Raspberry Pi,\n"
                "so local macOS mode will not show live data and can look like it's 'not connecting'.\n\n"
                "Use SSH to the Pi:\n"
                "  python3 kkfm/watchdog_run.py dashboard --ssh pi@<host>\n\n"
                "(If you really want local mode anyway, set WATCHDOG_ALLOW_LOCAL_DASHBOARD=1.)"
            )

        has_systemctl = shutil.which("systemctl") is not None
        has_core_status = any(
            p.exists()
            for p in (
                tx_status,
                log_status,
                out_path / "tx_status.json",
                out_path / "log_status.json",
            )
        )

        if (not has_systemctl) and (not has_core_status):
            msg = (
                "No local data source for the dashboard.\n\n"
                "This mode expects either `systemctl` (systemd) or local status files\n"
                "(tx_status.json/log_status.json) written by `monitor-tx`/`monitor-logs`.\n\n"
                "If you meant to monitor the Raspberry Pi, use:\n"
                "  python3 kkfm/watchdog_run.py dashboard --ssh pi@<host>"
            )
            if anomalies_jsonl.exists():
                msg += "\n\n(Note: anomalies.jsonl exists, but that file can be stale and does not provide live telemetry.)"
            raise SystemExit(msg)

    class _RemoteStream:
        def __init__(self, target: str, out_dir_s: str, refresh: float) -> None:
            self.target = target
            self.out_dir_s = out_dir_s
            self.refresh = float(refresh)
            self.proc: subprocess.Popen[str] | None = None
            self.last: dict[str, Any] | None = None
            self.last_rx_ts: float = 0.0
            self.next_start_ts: float = 0.0
            self.failures: int = 0
            self.state: str = "DISCONNECTED"
            self.expected_refresh_s = max(0.1, float(refresh))
            try:
                self.degraded_timeout_s = float(
                    os.environ.get(
                        "WATCHDOG_SSH_DEGRADED_TIMEOUT_S",
                        str(max(5.0, 3.0 * self.expected_refresh_s)),
                    )
                )
            except Exception:
                self.degraded_timeout_s = max(5.0, 3.0 * self.expected_refresh_s)
            self.degraded_timeout_s = max(0.0, self.degraded_timeout_s)

            try:
                self.jitter_warn_s = float(
                    os.environ.get(
                        "WATCHDOG_SSH_JITTER_WARN_S",
                        str(max(2.5, 2.5 * self.expected_refresh_s)),
                    )
                )
            except Exception:
                self.jitter_warn_s = max(2.5, 2.5 * self.expected_refresh_s)
            self.jitter_warn_s = max(0.0, self.jitter_warn_s)

            try:
                self.jitter_crit_s = float(
                    os.environ.get(
                        "WATCHDOG_SSH_JITTER_CRIT_S",
                        str(max(6.0, 6.0 * self.expected_refresh_s)),
                    )
                )
            except Exception:
                self.jitter_crit_s = max(6.0, 6.0 * self.expected_refresh_s)
            self.jitter_crit_s = max(self.jitter_warn_s, self.jitter_crit_s)

            self._prev_rx_ts: float | None = None
            self._rx_intervals: list[float] = []
            self._rx_intervals_max = 20
            self._stdout_fd: int | None = None
            self._buf: bytes = b""

            try:
                self.stale_timeout_s = float(os.environ.get("WATCHDOG_SSH_STALE_TIMEOUT_S", "15"))
            except Exception:
                self.stale_timeout_s = 15.0

            try:
                self.connect_timeout_s = float(os.environ.get("WATCHDOG_SSH_CONNECT_TIMEOUT_S", "3"))
            except Exception:
                self.connect_timeout_s = 3.0

        def _schedule_backoff(self) -> None:
            delay = min(30.0, float(2 ** min(self.failures, 5)))
            self.next_start_ts = time.time() + delay
            self.state = "BACKOFF"

        def _stop(self) -> None:
            p = self.proc
            self.proc = None
            self._stdout_fd = None
            self._buf = b""
            if self.state != "BACKOFF":
                self.state = "DISCONNECTED"
            if p is None:
                return
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=0.5)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass
            except Exception:
                return

        def start(self) -> None:
            now = time.time()
            if now < self.next_start_ts:
                return
            if self.proc is not None and self.proc.poll() is not None:
                self._stop()
            if self.proc is not None and self.proc.poll() is None:
                return

            self.state = "CONNECTING"
            cmd = [
                "ssh",
                *_ssh_args(connect_timeout_s=self.connect_timeout_s, control_persist_s=60.0),
                self.target,
                "--",
                "python3",
                "-u",
                "-",
                "--out-dir",
                self.out_dir_s,
                "--refresh",
                str(self.refresh),
            ]

            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=False,
                    bufsize=0,
                )
            except Exception:
                self.proc = None
                self.failures += 1
                self._schedule_backoff()
                return
            assert self.proc.stdin is not None
            script = r'''
import argparse, json, os, subprocess, time
from pathlib import Path

def run(cmd, timeout_s=1.0):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout_s)
        return (p.stdout or "").strip()
    except Exception:
        return ""

def systemctl_state(unit: str):
    out = run([
        "systemctl","show",unit,
        "-p","ActiveState","-p","SubState","-p","ActiveEnterTimestamp",
        "--no-pager",
    ], timeout_s=1.0)
    st = {"active":"?","sub":"?","since":""}
    for line in out.splitlines():
        if line.startswith("ActiveState="):
            st["active"] = line.split("=",1)[1].strip()
        elif line.startswith("SubState="):
            st["sub"] = line.split("=",1)[1].strip()
        elif line.startswith("ActiveEnterTimestamp="):
            st["since"] = line.split("=",1)[1].strip()
    return st

def read_json_line(p: Path):
    try:
        if not p.exists():
            return None
        txt = p.read_text(encoding="utf-8", errors="ignore").strip()
        if not txt:
            return None
        return json.loads(txt)
    except Exception:
        return None

def tail_jsonl(p: Path, n: int):
    try:
        if not p.exists():
            return []
        out = run(["tail","-n",str(int(n)),str(p)], timeout_s=1.0)
        recs = []
        for raw in out.splitlines()[-n:]:
            try:
                recs.append(json.loads(raw))
            except Exception:
                pass
        return recs
    except Exception:
        return []

ap = argparse.ArgumentParser()
ap.add_argument("--out-dir", required=True)
ap.add_argument("--refresh", type=float, default=1.0)
args = ap.parse_args()

out_path = Path(args.out_dir)

def get_status_path(filename):
    uid = os.getuid()
    cands = [
        Path("/run/kkfm-watchdog") / filename,
        Path(f"/run/user/{uid}/kkfm-watchdog") / filename,
        Path(f"/dev/shm/kkfm-watchdog-{uid}") / filename,
        out_path / filename,
    ]
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return out_path / filename

def get_anomalies_path():
    uid = os.getuid()
    cands = [
        Path("/run/kkfm-watchdog") / "anomalies.jsonl",
        Path(f"/run/user/{uid}/kkfm-watchdog") / "anomalies.jsonl",
        Path(f"/dev/shm/kkfm-watchdog-{uid}") / "anomalies.jsonl",
        out_path / "anomalies.jsonl",
    ]
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return out_path / "anomalies.jsonl"

units = [
    "acnh-aircheck.service",
    "acnh-aircheck-watchdog.service",
    "acnh-aircheck-tx-watchdog.service",
]

while True:
    snap = {
        "ts": time.time(),
        "services": {u: systemctl_state(u) for u in units},
        "tx": read_json_line(get_status_path("tx_status.json")),
        "log": read_json_line(get_status_path("log_status.json")),
        "anomalies": tail_jsonl(get_anomalies_path(), 50),
    }
    print(json.dumps(snap, ensure_ascii=False), flush=True)
    time.sleep(max(0.1, float(args.refresh)))
'''
            try:
                self.proc.stdin.write(script.encode("utf-8", errors="replace"))
            except Exception:
                self.failures += 1
                self._schedule_backoff()
                self._stop()
                return
            self.proc.stdin.close()
            self.last_rx_ts = time.time()
            try:
                assert self.proc.stdout is not None
                self._stdout_fd = self.proc.stdout.fileno()
                os.set_blocking(self._stdout_fd, False)
                self._buf = b""
            except Exception:
                self._stdout_fd = None

        def poll_latest(self) -> dict[str, Any] | None:
            self.start()
            if self.proc is None or self._stdout_fd is None:
                return self.last
            if self.proc.poll() is not None:
                self.failures += 1
                self._schedule_backoff()
                self.last = None
                self.state = "DISCONNECTED"
                return None
            try:
                got_line = False
                fd = self._stdout_fd
                while True:
                    r, _, _ = select.select([fd], [], [], 0.0)
                    if not r:
                        break
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        break
                    except OSError as e:
                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            break
                        raise
                    if not chunk:
                        break
                    self._buf += chunk
                    while b"\n" in self._buf:
                        raw, self._buf = self._buf.split(b"\n", 1)
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw.decode("utf-8", errors="ignore"))
                        except Exception:
                            continue
                        if isinstance(obj, dict):
                            self.last = obj
                            got_line = True
                            now_rx = time.time()
                            self.last_rx_ts = now_rx
                            if self._prev_rx_ts is not None:
                                try:
                                    dt = float(now_rx - self._prev_rx_ts)
                                    if dt >= 0:
                                        self._rx_intervals.append(dt)
                                        if len(self._rx_intervals) > self._rx_intervals_max:
                                            self._rx_intervals = self._rx_intervals[-self._rx_intervals_max :]
                                except Exception:
                                    pass
                            self._prev_rx_ts = now_rx
                            self.failures = 0
                            self.state = "CONNECTED"
                if not got_line and self.last_rx_ts and (time.time() - self.last_rx_ts) > self.stale_timeout_s:
                    self.failures += 1
                    self._schedule_backoff()
                    self.last = None
                    self._stop()
            except Exception:
                return self.last
            return self.last

        def link_level(self) -> str:
            if self.state in ("BACKOFF", "DISCONNECTED"):
                return "err"
            if self.state == "CONNECTING":
                return "warn"
            if not self.last_rx_ts:
                return "warn"
            try:
                age = time.time() - float(self.last_rx_ts)
            except Exception:
                age = 0.0
            if self.degraded_timeout_s > 0 and age > self.degraded_timeout_s:
                return "warn"
            if self._rx_intervals:
                try:
                    worst = max(float(x) for x in self._rx_intervals)
                    if self.jitter_crit_s > 0 and worst >= self.jitter_crit_s:
                        return "warn"
                    if self.jitter_warn_s > 0 and worst >= self.jitter_warn_s:
                        return "warn"
                except Exception:
                    pass
            return "ok"

        def status_line(self) -> str:
            try:
                age = time.time() - float(self.last_rx_ts or 0.0)
            except Exception:
                age = 0.0
            if self.state == "CONNECTED":
                if self.degraded_timeout_s > 0 and age > self.degraded_timeout_s:
                    return f"SSH: DEGRADED (no updates for {age:.0f}s)"
                if self._rx_intervals:
                    try:
                        worst = max(float(x) for x in self._rx_intervals)
                        if self.jitter_warn_s > 0 and worst >= self.jitter_warn_s:
                            return f"SSH: JITTER (max gap {worst:.1f}s)"
                    except Exception:
                        pass
                return "SSH: CONNECTED"
            if self.state == "BACKOFF":
                try:
                    wait = max(0.0, self.next_start_ts - time.time())
                except Exception:
                    wait = 0.0
                return f"SSH: OFFLINE (retry in {wait:.0f}s)"
            if self.state == "CONNECTING":
                return "SSH: CONNECTING"
            return "SSH: DISCONNECTED"

    remote_stream: _RemoteStream | None = _RemoteStream(ssh_target, str(out_path), refresh_s) if ssh_target else None

    def _pretty_bwav_label(v: object) -> str:
        if not isinstance(v, str):
            return ""
        s = v.strip()
        if not s:
            return ""
        base = Path(s).name
        if base.lower().endswith(".bwav"):
            base = base[:-5]
        base = re.sub(r"^(BGM_Radio_Jingle_|Radio_CM_|BGM_Radio_CM_)", "", base)
        return base

    def draw(stdscr) -> None:
        curses.start_color()
        curses.use_default_colors()

        # Default (body) text color: prefer a light gray if the terminal supports it.
        # - 256-color: use a neutral gray (245).
        # - 16-color: use bright black (8), typically rendered as gray.
        # - Fallback: white.
        body_fg = curses.COLOR_WHITE
        title_fg = curses.COLOR_WHITE
        title_dim = False
        try:
            if getattr(curses, "COLORS", 0) >= 256:
                body_fg = 245
                # Slightly lighter than body, but not white.
                title_fg = 250
            elif getattr(curses, "COLORS", 0) >= 16:
                body_fg = 8
                # Use dim white to approximate "light gray".
                title_fg = curses.COLOR_WHITE
                title_dim = True
        except Exception:
            body_fg = curses.COLOR_WHITE
            title_fg = curses.COLOR_WHITE
            title_dim = False

        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_YELLOW)

        curses.init_pair(11, body_fg, -1)
        try:
            stdscr.bkgd(" ", curses.color_pair(11))
        except Exception:
            pass

        curses.init_pair(12, title_fg, -1)
        section_title_attr = curses.color_pair(12) | curses.A_BOLD | curses.A_UNDERLINE
        title_text_attr = curses.color_pair(12)
        if title_dim:
            section_title_attr |= curses.A_DIM
            title_text_attr |= curses.A_DIM
        
        orange = curses.COLOR_YELLOW
        try:
            if getattr(curses, "COLORS", 0) >= 256:
                orange = 208
            elif curses.can_change_color() and getattr(curses, "COLORS", 0) >= 16:
                orange = 8
                try:
                    curses.init_color(orange, 1000, 500, 0)
                except Exception:
                    orange = curses.COLOR_YELLOW
        except Exception:
            orange = curses.COLOR_YELLOW

        curses.init_pair(7, orange, -1)
        curses.curs_set(0)
        stdscr.nodelay(True)

        last_tx_label: str = "WAIT"
        last_tx_color = curses.color_pair(7)
        tx_silence_since_ts: Optional[float] = None
        tx_silence_grace_s: float = 5.0

        last_jingle_name: Optional[str] = None
        last_comm_name: Optional[str] = None
        last_jingle_state: Optional[str] = None
        last_comm_state: Optional[str] = None
        last_bwav_name: Optional[str] = None
        last_bwav_src: Optional[str] = None

        last_log_epoch: Optional[str] = None
        last_tx_epoch: Optional[str] = None

        try:
            log_status_stale_s = float(os.environ.get("WATCHDOG_LOG_STATUS_STALE_S", "30"))
        except Exception:
            log_status_stale_s = 30.0
        log_status_stale_s = max(0.0, log_status_stale_s)

        prev_jingle_playing: bool = False
        prev_comm_playing: bool = False

        while True:
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1
            if ch in (ord("q"), ord("Q")):
                return

            try:
                stdscr.erase()
                max_y, max_x = stdscr.getmaxyx()

                try:
                    if curses.is_term_resized(max_y, max_x):
                        curses.resizeterm(max_y, max_x)
                except Exception:
                    pass

                remote = remote_stream.poll_latest() if remote_stream else None
                ssh_status = ""
                ssh_level: str | None = None
                if ssh_target and remote_stream:
                    ssh_status = remote_stream.status_line()
                    ssh_level = remote_stream.link_level()

                level = 0
                if ssh_target and remote_stream:
                    if ssh_level == "err":
                        level = max(level, 2)
                    elif ssh_level == "warn":
                        level = max(level, 1)

                if isinstance(remote, dict) and isinstance(remote.get("services"), dict):
                    for u in units:
                        rst = remote["services"].get(u)
                        if isinstance(rst, dict):
                            active = str(rst.get("active") or "?")
                            if active != "active":
                                level = max(level, 2)
                                break

                tx_snap = remote.get("tx") if isinstance(remote, dict) else None
                if isinstance(tx_snap, dict):
                    now_ts = time.time()
                    try:
                        age_s = now_ts - float(tx_snap.get("ts", 0))
                    except Exception:
                        age_s = 1e9
                    telemetry_fresh = age_s < 5
                    telemetry_down = age_s > 30

                    if telemetry_down:
                        level = max(level, 2)

                    tx_epoch = tx_snap.get("epoch")
                    if isinstance(tx_epoch, str) and tx_epoch:
                        if last_tx_epoch is None:
                            last_tx_epoch = tx_epoch
                        elif tx_epoch != last_tx_epoch:
                            last_tx_epoch = tx_epoch
                            last_tx_label = "WAIT"
                            last_tx_color = curses.color_pair(7)
                            tx_silence_since_ts = None

                    inlevel = tx_snap.get("inlevel")
                    audio_present = tx_snap.get("audio_present")
                    if not isinstance(audio_present, bool):
                        try:
                            lvl_thr = int(os.environ.get("WATCHDOG_TX_INLEVEL_AUDIO_THRESHOLD", "2"))
                        except Exception:
                            lvl_thr = 2
                        try:
                            db_thr = float(os.environ.get("WATCHDOG_TX_AUDIO_DB_THRESHOLD", "-50"))
                        except Exception:
                            db_thr = -50.0
                        if isinstance(inlevel, (int, float)):
                            if float(inlevel) < 0:
                                audio_present = float(inlevel) > float(db_thr)
                            else:
                                audio_present = float(inlevel) >= float(lvl_thr)
                        else:
                            audio_present = False

                    if telemetry_fresh and isinstance(audio_present, bool):
                        if audio_present:
                            tx_silence_since_ts = None
                        else:
                            if tx_silence_since_ts is None:
                                tx_silence_since_ts = now_ts
                            if (now_ts - tx_silence_since_ts) >= tx_silence_grace_s:
                                level = max(level, 1)

                if (
                    ssh_target
                    and remote_stream
                    and ssh_level == "ok"
                    and isinstance(remote, dict)
                    and (remote.get("tx") is None)
                ):
                    level = max(level, 2)

                header_color = curses.color_pair(8)
                if level >= 2:
                    header_color = curses.color_pair(9)
                elif level == 1:
                    header_color = curses.color_pair(10)

                ts_str = time.strftime('%Y-%m-%d %H:%M:%S')
                suffix = ""
                if ssh_status:
                    suffix = f" | {ssh_status} "
                if max_x > 60:
                    header = f" KKFM AIRCHECK DASHBOARD | {ts_str}{suffix}"
                elif max_x > 30:
                    header = f" KKFM DASHBOARD{suffix}"
                else:
                    header = " KKFM "
                
                stdscr.attron(header_color | curses.A_BOLD)
                stdscr.addnstr(0, 0, header.center(max_x), max(0, max_x - 1))
                stdscr.attroff(header_color | curses.A_BOLD)

                y = 2
                if y < max_y:
                    stdscr.attron(section_title_attr)
                    stdscr.addnstr(y, 2, "SYSTEM SERVICES", max(0, max_x - 3))
                    stdscr.attroff(section_title_attr)
                    y += 1
                
                for u in units:
                    if y >= max_y: break
                    st = None
                    if isinstance(remote, dict) and isinstance(remote.get("services"), dict):
                        rst = remote["services"].get(u)
                        if isinstance(rst, dict):
                            st = ServiceState(
                                name=u,
                                active=str(rst.get("active") or "?"),
                                sub=str(rst.get("sub") or "?"),
                                since=str(rst.get("since") or ""),
                            )
                    if st is None:
                        if ssh_target and remote_stream:
                            if remote is None:
                                st = ServiceState(name=u, active="OFFLINE", sub="-", since="")
                            else:
                                st = ServiceState(name=u, active="CONNECT", sub="-", since="")
                        else:
                            st = _systemctl_state(u, ssh_target=ssh_target)
                    
                    u_disp = u
                    if max_x < 50:
                        u_disp = u.replace(".service", "")
                    
                    stdscr.addnstr(y, 4, f"{u_disp:<35} ", max(0, max_x - 5))
                    
                    if st.active == "active":
                        color = curses.color_pair(1)
                    elif st.active in ("CONNECT", "OFFLINE"):
                        color = curses.color_pair(3)
                    else:
                        color = curses.color_pair(2)
                    
                    stdscr.attron(color | curses.A_BOLD)
                    cur_x = stdscr.getyx()[1]
                    if cur_x < max_x:
                        stdscr.addnstr(f"{st.active}/{st.sub}", max(0, max_x - cur_x - 1))
                    stdscr.attroff(color | curses.A_BOLD)
                    y += 1

                y += 1
                if y < max_y:
                    stdscr.attron(section_title_attr)
                    stdscr.addnstr(y, 2, "TRANSMITTER (Arduino/Si4713)", max(0, max_x - 3))
                    stdscr.attroff(section_title_attr)
                    y += 1
                    tx = None
                    if isinstance(remote, dict) and isinstance(remote.get("tx"), dict):
                        tx = remote.get("tx")
                    if tx is None and not (ssh_target and remote_stream):
                        tx = _read_json_line(tx_status, ssh_target=ssh_target)
                        if tx is None and ssh_target:
                            tx = _read_json_line(out_path / "tx_status.json", ssh_target=ssh_target)
                    if tx:
                        now_ts = time.time()
                        age_s = now_ts - tx.get("ts", 0)
                        telemetry_fresh = age_s < 5
                        telemetry_down = age_s > 30

                        tx_epoch = tx.get("epoch")
                        if isinstance(tx_epoch, str) and tx_epoch:
                            if last_tx_epoch is None:
                                last_tx_epoch = tx_epoch
                            elif tx_epoch != last_tx_epoch:
                                last_tx_epoch = tx_epoch
                                last_tx_label = "WAIT"
                                last_tx_color = curses.color_pair(7)
                                tx_silence_since_ts = None
                        inlevel = tx.get("inlevel")
                        audio_present = tx.get("audio_present")
                        if not isinstance(audio_present, bool):
                            try:
                                lvl_thr = int(os.environ.get("WATCHDOG_TX_INLEVEL_AUDIO_THRESHOLD", "2"))
                            except Exception:
                                lvl_thr = 2
                            try:
                                db_thr = float(os.environ.get("WATCHDOG_TX_AUDIO_DB_THRESHOLD", "-50"))
                            except Exception:
                                db_thr = -50.0
                            if isinstance(inlevel, (int, float)):
                                if float(inlevel) < 0:
                                    audio_present = float(inlevel) > float(db_thr)
                                else:
                                    audio_present = float(inlevel) >= float(lvl_thr)
                            else:
                                audio_present = False

                        if telemetry_fresh:
                            if audio_present:
                                tx_silence_since_ts = None
                                label = "AUDIO"
                                color = curses.color_pair(1)
                                last_tx_label = label
                                last_tx_color = color
                            else:
                                if tx_silence_since_ts is None:
                                    tx_silence_since_ts = now_ts
                                if (now_ts - tx_silence_since_ts) >= tx_silence_grace_s:
                                    label = "SILENCE"
                                    color = curses.color_pair(3)
                                    last_tx_label = label
                                    last_tx_color = color
                                else:
                                    label = last_tx_label
                                    color = last_tx_color
                        else:
                            if telemetry_down:
                                label = "NO TELEMETRY"
                                color = curses.color_pair(2)
                            else:
                                label = last_tx_label
                                color = last_tx_color

                        stdscr.addnstr(y, 4, "TX: ", max(0, max_x - 5))
                        stdscr.attron(color | curses.A_BOLD)
                        cur_x = stdscr.getyx()[1]
                        if cur_x < max_x:
                            stdscr.addnstr(label, max(0, max_x - cur_x - 1))
                        stdscr.attroff(color | curses.A_BOLD)

                        # Frequency
                        cur_x = stdscr.getyx()[1]
                        if cur_x < max_x:
                            stdscr.addnstr("  Freq: ", max(0, max_x - cur_x - 1))
                        
                        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
                        cur_x = stdscr.getyx()[1]
                        if cur_x < max_x:
                            stdscr.addnstr(f"{tx.get('freq_mhz')} MHz", max(0, max_x - cur_x - 1))
                        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
                        
                        # Port
                        cur_x = stdscr.getyx()[1]
                        if cur_x < max_x:
                            stdscr.addnstr("  Port: ", max(0, max_x - cur_x - 1))

                        port_s = str(tx.get("port"))
                        stdscr.attron(title_text_attr)
                        cur_x = stdscr.getyx()[1]
                        if cur_x < max_x:
                            stdscr.addnstr(port_s, max(0, max_x - cur_x - 1))
                        stdscr.attroff(title_text_attr)

                        y += 1
                        if y < max_y:
                            ts_txt = _fmt_ts(tx.get('ts'))
                            stdscr.addnstr(y, 4, "Status: ", max(0, max_x - 5))
                            stdscr.attron(title_text_attr)
                            cur_x = stdscr.getyx()[1]
                            if cur_x < max_x:
                                stdscr.addnstr(ts_txt, max(0, max_x - cur_x - 1))
                            stdscr.attroff(title_text_attr)
                            y += 1
                        if y < max_y:
                            stdscr.attron(title_text_attr)
                            stdscr.addnstr(
                                y,
                                4,
                                f"{_fmt_kv('In', tx.get('inlevel'), 14)}{_fmt_kv('Noise', tx.get('noise'), 14)}{_fmt_kv('Overmod', tx.get('overmod'), 0)}",
                                max(0, max_x - 5),
                            )
                            stdscr.attroff(title_text_attr)
                            y += 1
                    else:
                        stdscr.attron(curses.color_pair(3))
                        stdscr.addnstr(y, 4, "Waiting for transmitter data...", max(0, max_x - 5))
                        stdscr.attroff(curses.color_pair(3))
                        y += 1

                y += 1
                if y < max_y:
                    stdscr.attron(section_title_attr)
                    stdscr.addnstr(y, 2, "RADIO LOGIC MONITOR", max(0, max_x - 3))
                    stdscr.attroff(section_title_attr)
                    y += 1
                    ls = None
                    if isinstance(remote, dict) and isinstance(remote.get("log"), dict):
                        ls = remote.get("log")
                    if ls is None and not (ssh_target and remote_stream):
                        ls = _read_json_line(log_status, ssh_target=ssh_target)
                        if ls is None and ssh_target:
                            ls = _read_json_line(out_path / "log_status.json", ssh_target=ssh_target)
                    if ls:
                        now_ts = time.time()
                        ls_ts = ls.get("ts")
                        try:
                            status_age_s = now_ts - float(ls_ts)
                        except Exception:
                            status_age_s = 1e9

                        log_epoch = ls.get("epoch")
                        if isinstance(log_epoch, str) and log_epoch:
                            if last_log_epoch is None:
                                last_log_epoch = log_epoch
                            elif log_epoch != last_log_epoch:
                                last_log_epoch = log_epoch
                                last_jingle_name = None
                                last_comm_name = None
                                last_jingle_state = None
                                last_comm_state = None
                                last_bwav_name = None
                                last_bwav_src = None
                                prev_jingle_playing = False
                                prev_comm_playing = False

                        log_stale = log_status_stale_s > 0 and status_age_s > log_status_stale_s
                        if log_stale:
                            last_jingle_name = None
                            last_comm_name = None
                            last_jingle_state = None
                            last_comm_state = None
                            last_bwav_name = None
                            last_bwav_src = None
                            prev_jingle_playing = False
                            prev_comm_playing = False

                        wd = None
                        if isinstance(remote, dict) and isinstance(remote.get("services"), dict):
                            rst = remote["services"].get("acnh-aircheck-watchdog.service")
                            if isinstance(rst, dict):
                                wd = ServiceState(
                                    name="acnh-aircheck-watchdog.service",
                                    active=str(rst.get("active") or "?"),
                                    sub=str(rst.get("sub") or "?"),
                                    since=str(rst.get("since") or ""),
                                )
                        if wd is None:
                            if ssh_target and remote_stream:
                                wd = ServiceState(name="acnh-aircheck-watchdog.service", active="CONNECT", sub="-", since="")
                            else:
                                wd = _systemctl_state("acnh-aircheck-watchdog.service", ssh_target=ssh_target)
                        wd_active = wd.active == "active"

                        le = ls.get("last_event") or {}
                        bwav = ls.get("last_bwav") or {}
                        det = ls.get("detector") or {}
                        jing = det.get("jingle") or {}
                        com = det.get("commercial") or {}

                        etype = (le.get("etype") or "").strip().lower()
                        ename = le.get("name")
                        if isinstance(ename, str) and ename:
                            if etype == "jingle_picked":
                                last_jingle_name = ename
                                last_jingle_state = "PICKED"
                            elif etype == "jingle_start":
                                last_jingle_name = ename
                                last_jingle_state = "PLAYING"
                            elif etype == "jingle_end":
                                last_jingle_name = ename
                                last_jingle_state = "ENDED"
                            elif etype == "commercial_picked":
                                last_comm_name = ename
                                last_comm_state = "PICKED"
                            elif etype == "commercial_start":
                                last_comm_name = ename
                                last_comm_state = "PLAYING"
                            elif etype == "commercial_end":
                                last_comm_name = ename
                                last_comm_state = "ENDED"

                        jingle_playing_now = bool(jing.get("started_name"))
                        comm_playing_now = bool(com.get("started_name"))

                        if isinstance(jing.get("started_name"), str) and jing.get("started_name"):
                            last_jingle_name = str(jing.get("started_name"))
                            last_jingle_state = "PLAYING"
                        if isinstance(com.get("started_name"), str) and com.get("started_name"):
                            last_comm_name = str(com.get("started_name"))
                            last_comm_state = "PLAYING"

                        if prev_jingle_playing and not jingle_playing_now and last_jingle_state == "PLAYING":
                            last_jingle_state = "ENDED"
                        if prev_comm_playing and not comm_playing_now and last_comm_state == "PLAYING":
                            last_comm_state = "ENDED"
                        prev_jingle_playing = jingle_playing_now
                        prev_comm_playing = comm_playing_now

                        bwav_name = bwav.get("name")
                        if isinstance(bwav_name, str) and bwav_name:
                            last_bwav_name = bwav_name
                            src = bwav.get("src")
                            if isinstance(src, str) and src.strip():
                                last_bwav_src = src.strip().upper()
                            else:
                                raw = bwav.get("raw")
                                if isinstance(raw, str):
                                    rr = raw.upper()
                                    if "[CACHE]" in rr:
                                        last_bwav_src = "CACHE"
                                    elif "[FILE]" in rr or "[BWAV]" in rr:
                                        last_bwav_src = "FILE"
                        
                        stdscr.addnstr(y, 4, f"{'EVENT':<8}: ", max(0, max_x - 5))

                        event_ts: Optional[float]
                        try:
                            event_ts = float(le.get("ts")) if le.get("ts") is not None else None
                        except Exception:
                            event_ts = None
                        try:
                            event_age_s = (now_ts - float(event_ts)) if event_ts is not None else None
                        except Exception:
                            event_age_s = None

                        if wd_active and not log_stale:
                            # Type indicator (aligned like BWAV/SIGN/COMM)
                            level = "IDLE"
                            if isinstance(le, dict):
                                lvl = le.get("level")
                                if isinstance(lvl, str) and lvl.strip():
                                    level = lvl.strip().upper()
                                else:
                                    et0 = le.get("etype")
                                    if isinstance(et0, str):
                                        et0s = et0.strip().lower()
                                        if et0s == "now_playing":
                                            level = "INFO"
                                        elif et0s.endswith("_failed"):
                                            level = "ERROR"
                                        elif et0s in ("dac_throttle",):
                                            level = "WARN"
                                        elif et0s in ("dac_overflow", "dac_underrun"):
                                            level = "ERROR"
                                        elif et0s == "unit_stopped":
                                            level = "CRIT"

                            level_attr = 0
                            if level == "INFO":
                                level_attr = curses.color_pair(4)
                            elif level == "WARN":
                                level_attr = curses.color_pair(3)
                            elif level == "ERROR":
                                level_attr = curses.color_pair(7) | curses.A_BOLD
                            elif level == "CRIT":
                                level_attr = curses.color_pair(2) | curses.A_BOLD

                            cur_x = stdscr.getyx()[1]
                            if cur_x < max_x:
                                if level_attr:
                                    stdscr.attron(level_attr)
                                stdscr.addnstr(f"{level[:7]:<7} ", max(0, max_x - cur_x - 1))
                                if level_attr:
                                    stdscr.attroff(level_attr)

                            if not isinstance(le, dict):
                                # No event payload.
                                pass
                            else:
                                etype = str(le.get("etype", ""))
                                if not etype and (level == "IDLE"):
                                    # Explicitly show IDLE only.
                                    pass
                                else:
                                    if event_age_s is not None and etype:
                                        # Fixed-width seconds so the arrow doesn't shift as 9s -> 10s -> 100s.
                                        group1 = f"{etype} ({event_age_s:>3.0f}s)"
                                    else:
                                        group1 = etype or "-"

                                    stdscr.attron(curses.color_pair(4))
                                    cur_x = stdscr.getyx()[1]
                                    if cur_x < max_x:
                                        stdscr.addnstr(group1, max(0, max_x - cur_x - 1))
                                    stdscr.attroff(curses.color_pair(4))

                                    le_name = le.get("name")
                                    if le_name:
                                        shown = _pretty_bwav_label(le_name)
                                        shown_s = str(shown)
                                        arrow_prefix = " -> "
                                        arrow_text = f"{arrow_prefix}{shown_s}"
                                        cur_x = stdscr.getyx()[1]
                                        remaining = max_x - cur_x - 1

                                        if len(arrow_text) <= remaining:
                                            stdscr.addnstr(arrow_prefix, max(0, remaining))
                                            cur_x2 = stdscr.getyx()[1]
                                            rem2 = max_x - cur_x2 - 1
                                            if cur_x2 < max_x and rem2 > 0:
                                                stdscr.attron(title_text_attr)
                                                stdscr.addnstr(shown_s, rem2)
                                                stdscr.attroff(title_text_attr)
                                        else:
                                            y += 1
                                            if y < max_y:
                                                stdscr.addnstr(y, 6, arrow_prefix, max(0, max_x - 7))
                                                cur_x2 = stdscr.getyx()[1]
                                                rem2 = max_x - cur_x2 - 1
                                                if cur_x2 < max_x and rem2 > 0:
                                                    stdscr.attron(title_text_attr)
                                                    stdscr.addnstr(shown_s, rem2)
                                                    stdscr.attroff(title_text_attr)
                        else:
                            stdscr.attron(curses.color_pair(2) | curses.A_BOLD)
                            cur_x = stdscr.getyx()[1]
                            if cur_x < max_x:
                                stdscr.addnstr("STALE" if log_stale else "DOWN", max(0, max_x - cur_x - 1))
                            stdscr.attroff(curses.color_pair(2) | curses.A_BOLD)
                            cur_x = stdscr.getyx()[1]
                            if cur_x < max_x:
                                stdscr.addnstr(f"  (watchdog {wd.active}/{wd.sub})", max(0, max_x - cur_x - 1))
                        y += 1

                        if y < max_y:
                            stdscr.addnstr(y, 4, f"{'BWAV':<8}: ", max(0, max_x - 5))
                            if isinstance(last_bwav_name, str) and last_bwav_name:
                                src_label = (last_bwav_src or "FILE")[:7]
                                shown = _pretty_bwav_label(last_bwav_name)
                                stdscr.attron(curses.color_pair(4))
                                cur_x = stdscr.getyx()[1]
                                if cur_x < max_x:
                                    stdscr.addnstr(f"{src_label:<7} {shown[:40]}", max(0, max_x - cur_x - 1))
                                stdscr.attroff(curses.color_pair(4))
                            else:
                                cur_x = stdscr.getyx()[1]
                                if cur_x < max_x:
                                    stdscr.addnstr("IDLE", max(0, max_x - cur_x - 1))
                            y += 1
                        
                        for kind, data in [("SIGN", jing), ("COMM", com)]:
                            if y >= max_y: break
                            stdscr.addnstr(y, 4, f"{kind:<8}: ", max(0, max_x - 5))
                            if data.get("started_name"):
                                stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
                                shown = _pretty_bwav_label(data.get("started_name"))
                                cur_x = stdscr.getyx()[1]
                                if cur_x < max_x:
                                    stdscr.addnstr(f"PLAYING {shown[:40]}", max(0, max_x - cur_x - 1))
                                stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
                            elif data.get("picked_name"):
                                stdscr.attron(curses.color_pair(3))
                                shown = _pretty_bwav_label(data.get("picked_name"))
                                cur_x = stdscr.getyx()[1]
                                if cur_x < max_x:
                                    stdscr.addnstr(f"PICKED  {shown[:40]}", max(0, max_x - cur_x - 1))
                                stdscr.attroff(curses.color_pair(3))
                            else:
                                is_jingle = kind == "SIGN"
                                last_name = last_jingle_name if is_jingle else last_comm_name
                                last_state = last_jingle_state if is_jingle else last_comm_state
                                if isinstance(last_name, str) and last_name:
                                    stdscr.attron(curses.color_pair(4))
                                    shown = _pretty_bwav_label(last_name)
                                    label_str = (last_state or "PREV")[:7]
                                    cur_x = stdscr.getyx()[1]
                                    if cur_x < max_x:
                                        stdscr.addnstr(f"{label_str:<7} {shown[:40]}", max(0, max_x - cur_x - 1))
                                    stdscr.attroff(curses.color_pair(4))
                                else:
                                    cur_x = stdscr.getyx()[1]
                                    if cur_x < max_x:
                                        stdscr.addnstr("IDLE", max(0, max_x - cur_x - 1))
                            y += 1
                    else:
                        stdscr.attron(curses.color_pair(3))
                        stdscr.addnstr(y, 4, "Waiting for log monitor data...", max(0, max_x - 5))
                        stdscr.attroff(curses.color_pair(3))
                        y += 1

                y += 1
                if y < max_y:
                    stdscr.attron(curses.color_pair(6) | curses.A_BOLD)
                    stdscr.addnstr(y, 0, " RECENT ANOMALIES ".center(max_x), max(0, max_x - 1))
                    stdscr.attroff(curses.color_pair(6) | curses.A_BOLD)
                y += 1
                
                if isinstance(remote, dict) and isinstance(remote.get("anomalies"), list):
                    recs = list(remote.get("anomalies") or [])
                else:
                    if ssh_target and remote_stream:
                        recs = []
                    else:
                        # Read more lines to ensure we capture recent repeating errors for aggregation
                        recs = _tail_jsonl(anomalies_jsonl, n=50, ssh_target=ssh_target)

                # Aggregate anomalies by code to show "state" instead of log stream
                grouped_anomalies = {}
                for r in recs:
                    code = r.get("code")
                    if not code: continue
                    # Keep the most recent instance of each code
                    if code not in grouped_anomalies or r.get("ts", 0) > grouped_anomalies[code].get("ts", 0):
                        grouped_anomalies[code] = r
                
                # Sort by timestamp descending (most recent first)
                sorted_anomalies = sorted(grouped_anomalies.values(), key=lambda x: x.get("ts", 0), reverse=True)
                
                if not sorted_anomalies and y < max_y:
                    stdscr.addnstr(y, 4, "No anomalies detected. System healthy.", max(0, max_x - 5))
                else:
                    def word_wrap(text: str, width: int) -> list:
                        """Split text into lines of at most `width` chars, breaking at word boundaries."""
                        if width <= 0:
                            return [text] if text else []
                        lines = []
                        while text:
                            if len(text) <= width:
                                lines.append(text)
                                break
                            # Find last space within width
                            break_at = text.rfind(' ', 0, width + 1)
                            if break_at <= 0:
                                # No space found, hard break
                                break_at = width
                            lines.append(text[:break_at])
                            text = text[break_at:].lstrip()
                        return lines
                    
                    for r in sorted_anomalies:
                        if y >= max_y: break
                        sev = r.get("severity", "info").lower()
                        if sev == "critical":
                            color = curses.color_pair(2)
                        elif sev == "error":
                            color = curses.color_pair(7)
                        elif sev == "warn":
                            color = curses.color_pair(3)
                        else:
                            color = curses.color_pair(4)
                        
                        # Format timestamp as 24h date/time
                        ts = r.get("ts", 0)
                        try:
                            ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
                        except Exception:
                            ts_str = "??:??:??"
                        
                        code = r.get("code", "")
                        msg = r.get("message", "")
                        
                        # Format: "HH:MM:SS SEVERITY CODE: message..."
                        # Aligned like EVENT/BWAV/SIGN/COMM section (starts at col 4)
                        # Timestamp (8 chars) : SEVERITY (8 chars) CODE: message
                        stdscr.addnstr(y, 4, f"{ts_str:<8} ", max(0, max_x - 5))
                        cur_x = stdscr.getyx()[1]
                        
                        stdscr.attron(color | curses.A_BOLD)
                        if cur_x < max_x:
                            stdscr.addnstr(f"{sev.upper():<8} ", max(0, max_x - cur_x - 1))
                        stdscr.attroff(color | curses.A_BOLD)
                        
                        cur_x = stdscr.getyx()[1]
                        # Code + message inline
                        full_text = f"{code}: {msg}" if msg else code
                        remaining_width = max(0, max_x - cur_x - 1)
                        # Continuation lines: align with CODE start + 2 spaces indent
                        cont_col = cur_x + 2
                        cont_width = max(1, max_x - cont_col - 1)
                        
                        # First line uses remaining_width, continuation lines are indented
                        if len(full_text) <= remaining_width:
                            stdscr.addnstr(full_text, remaining_width)
                            y += 1
                        else:
                            # Find word break for first line
                            break_at = full_text.rfind(' ', 0, remaining_width + 1)
                            if break_at <= 0:
                                break_at = remaining_width
                            stdscr.addnstr(full_text[:break_at], remaining_width)
                            y += 1
                            text_left = full_text[break_at:].lstrip()
                            
                            # Wrap remainder, indented from CODE start
                            while text_left and y < max_y:
                                line_to_print = ""
                                if len(text_left) <= cont_width:
                                    line_to_print = text_left
                                    text_left = ""
                                else:
                                    brk = text_left.rfind(' ', 0, cont_width + 1)
                                    if brk <= 0:
                                        brk = cont_width
                                    line_to_print = text_left[:brk]
                                    text_left = text_left[brk:].lstrip()
                                try:
                                    stdscr.addstr(y, cont_col, line_to_print[:cont_width])
                                except curses.error:
                                    pass
                                y += 1

                stdscr.refresh()
                time.sleep(max(0.1, float(refresh_s)))

            except curses.error:
                time.sleep(0.05)
                continue

    curses.wrapper(draw)
    return 0