from __future__ import annotations

import atexit
import json
import os
import signal
import struct
import subprocess
import threading
import time
import curses
import select
import shutil
import sys
import tempfile
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import re
import errno

from .paths import get_runtime_status_dir
from .health_check import WatchdogHealthMonitor, read_status_file
from . import sonos


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
        sys.stdout.write("\033[?1004l")  # Disable focus reporting
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


@dataclass(frozen=True)
class DashboardProfile:
    """Which service set the dashboard watches."""
    name: str
    units: tuple[str, ...]
    watchdog_unit: str           # unit whose log monitor feeds RADIO LOGIC MONITOR
    user_units: bool             # systemctl --user instead of the system manager
    stream_port: int
    status_dir: Optional[str]    # remote dir holding log_status.json / tx_status.json (None: radio defaults)
    anomalies: Optional[str]     # remote anomalies.jsonl (None: radio defaults)
    title: str = "KKFM AIRCHECK"  # header title
    transmitter: bool = True      # has the Arduino/Si4713 TX telemetry (tx_status.json)


PROFILES: dict[str, DashboardProfile] = {
    "radio": DashboardProfile(
        name="radio",
        units=("acnh-aircheck.service", "acnh-aircheck-watchdog.service", "acnh-aircheck-tx-watchdog.service"),
        watchdog_unit="acnh-aircheck-watchdog.service",
        user_units=False,
        stream_port=9000,
        status_dir=None,
        anomalies=None,
    ),
    # Hourly BGM on the Pi: system units running as svc-kkfm, packed build on the USB.
    "hourly": DashboardProfile(
        name="hourly",
        units=("acnh-hourly.service", "acnh-hourly-watchdog.service"),
        watchdog_unit="acnh-hourly-watchdog.service",
        user_units=False,
        stream_port=8791,
        status_dir="/run/kkfm-hourly",
        anomalies="/mnt/kkfm-usb/kkfm/hourly-build/watchdog/out/anomalies.jsonl",
        title="HOURLY BGM",
        transmitter=False,
    ),
}


def _systemctl_state(unit: str, *, ssh_target: Optional[str] = None, user: bool = False) -> ServiceState:
    cmd = [
        "systemctl",
        *(["--user"] if user else []),
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


def _stream_host(ssh_target: Optional[str]) -> str:
    if ssh_target and "@" in ssh_target:
        return ssh_target.split("@", 1)[1]
    return ssh_target or "127.0.0.1"


# The players bind loopback only (ACNH_LOCALHOST_ONLY=1), so a remote dashboard reaches
# their HTTP port through an SSH local forward instead of the LAN.
class _SshTunnel:
    def __init__(self, ssh_target: str, remote_port: int) -> None:
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.local_port = sock.getsockname()[1]
        self.proc = subprocess.Popen(
            ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
             "-L", f"127.0.0.1:{self.local_port}:localhost:{remote_port}", ssh_target],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 8.0
        while time.time() < deadline and self.proc.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", self.local_port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except Exception:
                self.proc.kill()


# Sonos can't reach the loopback-only players either; it plays the LAN MP3 relay that the
# Sonos SMAPI service (sonos/smapi_server.py) runs on the Pi, which carries live ICY titles.
SONOS_RELAY_PORT = 8777
SONOS_RELAY_PATH = {"radio": "/kkfm.mp3", "hourly": "/hourly.mp3"}


# SAFETY - HARD LOCK, matching the jukebox's BwavPlayer (jukebox-web/lib/
# bwavPlayer.js): corrupt or distorted audio must never reach the speaker.
# That player owns its whole pipeline (fetch -> decode -> Web Audio graph)
# and layers three defenses: a source integrity gate before decode, a
# post-decode PCM sanity check, and a live output watchdog that hard-locks
# on a sustained rail-level run (the signature of decode corruption).
#
# This player is a subprocess (ffplay/mpv) we don't own the decode of, so we
# take over exactly the part we can: WE fetch /stream ourselves instead of
# letting the player hit the URL, validate its WAV header once (the source
# integrity gate — bad magic/format/rate/channels means the server or the
# link is broken, refuse to play garbage), then scan every PCM sample as it
# passes through (the output watchdog, moved upstream of the player instead
# of downstream of it) before forwarding raw bytes into the player's stdin.
# Any failure calls _hard_lock(): the player is killed immediately and
# playback refuses to restart until a deliberate 'r' reset, same as the
# jukebox's latch — fail closed, silent, and stay there.
class StreamIntegrityError(Exception):
    pass


_WAV_HEADER_STRUCT = struct.Struct("<4sI4s4sIHHIIHH4sI")

# Rail-run watchdog: mirrors bwavPlayer.js's RAIL_LEVEL=0.999 / RAIL_RUN_LIMIT
# =4096 (a sustained near-full-scale run == square-wave/DC-rail == the
# signature of corruption, not real audio, which never sits at rail for
# long). Same order of magnitude here: 4096 interleaved int16 samples of a
# 48kHz stereo stream is ~43ms, comparable to the web player's ~85ms of mono
# samples.
_RAIL_LEVEL_INT16 = 32735  # ~0.999 * 32767
_RAIL_RUN_LIMIT = 4096


def _validate_wav_header(header_bytes: bytes) -> None:
    if len(header_bytes) != _WAV_HEADER_STRUCT.size:
        raise StreamIntegrityError(f"short WAV header ({len(header_bytes)} bytes)")
    (riff, _file_size, wave, fmt, fmt_size, fmt_tag, channels,
     sample_rate, _byte_rate, _block_align, bits, data, _data_size) = _WAV_HEADER_STRUCT.unpack(header_bytes)
    if riff != b"RIFF" or wave != b"WAVE" or fmt != b"fmt " or data != b"data":
        raise StreamIntegrityError("bad WAV magic")
    if fmt_size != 16 or fmt_tag != 1:
        raise StreamIntegrityError(f"unsupported format tag {fmt_tag}")
    if channels != 2 or sample_rate != 48000 or bits != 16:
        raise StreamIntegrityError(f"unexpected format {channels}ch/{sample_rate}Hz/{bits}bit")


def _stream_relay(url: str, stdin_pipe: Any, stop_flag: dict, lock_holder: dict) -> None:
    """Runs on a background thread. Owns the network fetch so the player
    subprocess never talks to the server directly; forwards validated raw
    PCM to its stdin. Any integrity-gate failure sets lock_holder and stops
    forwarding; a plain network hiccup just ends the thread (the player then
    exits on stdin EOF and the main loop reports that the normal way)."""
    rail_run = 0
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            _validate_wav_header(resp.read(_WAV_HEADER_STRUCT.size))
            while not stop_flag["stop"]:
                chunk = resp.read(8192)
                if not chunk:
                    break
                n = len(chunk) - (len(chunk) % 2)
                if n:
                    for s in struct.unpack(f"<{n // 2}h", chunk[:n]):
                        if abs(s) >= _RAIL_LEVEL_INT16:
                            rail_run += 1
                            if rail_run >= _RAIL_RUN_LIMIT:
                                raise StreamIntegrityError(
                                    f"sustained rail-level run ({_RAIL_RUN_LIMIT} samples): "
                                    "corrupt/distorted stream"
                                )
                        else:
                            rail_run = 0
                try:
                    stdin_pipe.write(chunk)
                    stdin_pipe.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break
    except StreamIntegrityError as e:
        lock_holder["locked"] = True
        lock_holder["reason"] = str(e)
    except Exception as e:
        stop_flag["error"] = f"{type(e).__name__}: {e}"
    finally:
        # Tell the main loop the feed is gone (server exited, connection dropped).
        # ffplay/mpv do not exit on stdin EOF by themselves, so without this the
        # dashboard would keep showing PLAYING with nothing coming out.
        stop_flag["ended"] = True
        try:
            stdin_pipe.close()
        except Exception:
            pass


def _player_cmd(bin_path: str) -> list[str]:
    """Raw s16le PCM from stdin: matches the web player, which pushes raw
    PCM straight into an AudioWorklet with no buffering, no format probing,
    and (since the player never opens the URL) none of the seek/Range
    handling that /stream's infinite-length WAV framing can't support."""
    name = os.path.basename(bin_path)
    if name.startswith("mpv"):
        return [
            bin_path, "--no-video", "--really-quiet", "--no-terminal",
            "--demuxer=rawaudio", "--demuxer-rawaudio-format=s16le",
            "--demuxer-rawaudio-rate=48000", "--demuxer-rawaudio-channels=2",
            "-",
        ]
    return [
        # -sample_rate/-ch_layout, not -ar/-ac: those are demuxer-private
        # AVOptions on the s16le demuxer (`ffmpeg -h demuxer=s16le`), the
        # -ar/-ac CLI aliases don't route to it as an input option here.
        bin_path, "-nodisp", "-loglevel", "error",
        "-f", "s16le", "-sample_rate", "48000", "-ch_layout", "stereo",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-i", "-",
    ]


def run_dashboard(
    *,
    out_dir: str,
    refresh_s: float = 1.0,
    ssh_target: Optional[str] = None,
    stream_port: Optional[int] = None,
    profile: str = "radio",
) -> int:
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
            stream_port=stream_port,
            profile=profile,
        )
    finally:
        _restore_terminal()
        # Restore original signal handlers
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)


def _run_dashboard_impl(
    *, out_dir: str, refresh_s: float, ssh_target: Optional[str], stream_port: Optional[int] = None,
    profile: str = "radio",
) -> int:
    prof = PROFILES[profile]
    if stream_port is None:
        stream_port = prof.stream_port
    out_path = Path(out_dir)
    tunnel = _SshTunnel(ssh_target, stream_port) if ssh_target else None
    player_base = tunnel.base if tunnel else f"http://127.0.0.1:{stream_port}"
    stream_url = f"{player_base}/stream"
    player_bin = shutil.which("ffplay") or shutil.which("mpv")
    player_proc_holder: dict[str, Any] = {"proc": None, "stop_flag": None}
    # Set by _stream_relay's integrity gate; a background thread writes this,
    # the main loop reads it once per tick (plain dict assignment is atomic
    # enough under the GIL for this - worst case a one-tick-stale read).
    player_lock_holder: dict[str, Any] = {"locked": False, "reason": None}
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

    units = list(prof.units)

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
            self.profile = prof
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
                "--units",
                ",".join(self.profile.units),
                *(["--user-units"] if self.profile.user_units else []),
                *(["--status-dir", self.profile.status_dir] if self.profile.status_dir else []),
                *(["--anomalies", self.profile.anomalies] if self.profile.anomalies else []),
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
        "systemctl",*(["--user"] if USER_UNITS else []),"show",unit,
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
ap.add_argument("--units", default="acnh-aircheck.service,acnh-aircheck-watchdog.service,acnh-aircheck-tx-watchdog.service")
ap.add_argument("--user-units", action="store_true")
ap.add_argument("--status-dir", default="")
ap.add_argument("--anomalies", default="")
args = ap.parse_args()

out_path = Path(args.out_dir)
USER_UNITS = bool(args.user_units)
STATUS_DIR = Path(args.status_dir.format(uid=os.getuid())) if args.status_dir else None
ANOMALIES = Path(args.anomalies.format(uid=os.getuid())) if args.anomalies else None

def get_status_path(filename):
    if STATUS_DIR is not None:
        return STATUS_DIR / filename
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
    if ANOMALIES is not None:
        return ANOMALIES
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

units = [u for u in args.units.split(",") if u]

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
        curses.init_pair(13, curses.COLOR_MAGENTA, -1)  # Purple for CRITICAL
        curses.curs_set(0)
        stdscr.timeout(max(100, int(refresh_s * 1000)))

        # Enable terminal focus reporting so we get ESC[I / ESC[O on
        # tab focus-in / focus-out.  This lets us force a full repaint the
        # instant the user switches back to this tab.
        try:
            sys.stdout.write("\033[?1004h")
            sys.stdout.flush()
        except Exception:
            pass

        # Also handle SIGCONT for terminals that send it on resume.
        _need_full_repaint = False
        def _on_sigcont(signum: int, frame: Any) -> None:
            nonlocal _need_full_repaint
            _need_full_repaint = True
        signal.signal(signal.SIGCONT, _on_sigcont)

        last_tx_label: str = "WAIT"
        last_tx_color = curses.color_pair(3)
        tx_silence_since_ts: Optional[float] = None
        tx_silence_grace_s: float = 5.0

        last_jingle_name: Optional[str] = None
        last_comm_name: Optional[str] = None
        last_jingle_state: Optional[str] = None
        last_comm_state: Optional[str] = None
        last_bwav_name: Optional[str] = None
        last_bwav_src: Optional[str] = None
        last_song_name: Optional[str] = None
        jingle_playing_now: bool = False
        comm_playing_now: bool = False

        player_state: str = "idle"  # "idle" | "playing" | "locked"
        player_err: Optional[str] = None

        # STREAM chip ('s'): hand the /stream URL to the Sonos zone-group
        # coordinator (SetAVTransportURI + Play) and show its transport state.
        sonos_state: str = "idle"  # "idle" | "streaming"
        sonos_room: Optional[str] = None
        sonos_ip: Optional[str] = None
        sonos_err: Optional[str] = None
        sonos_transport: Optional[str] = None
        sonos_last_poll: float = 0.0

        def _stream_url_for_sonos(coord_ip: str) -> str:
            host = _stream_host(ssh_target)
            if host in ("127.0.0.1", "localhost", "::1"):
                host = sonos.local_ip_towards(coord_ip)
            return f"http://{host}:{SONOS_RELAY_PORT}{SONOS_RELAY_PATH[prof.name]}"

        # Both players go to the Sonos as an internet-radio station through the relay:
        # live ICY titles, no length cap.
        sonos_radio: bool = True

        def _hand_to_sonos(coord_ip: str) -> None:
            station = "Hourly BGM" if prof.name == "hourly" else "KKFM"
            sonos.set_radio_and_play(coord_ip, _stream_url_for_sonos(coord_ip), station=station)

        sonos_title: Optional[str] = None

        def _sonos_track_title() -> str:
            # What the Sonos shows as the track: the BWAV file name the service is
            # playing (from the log monitor's status), until source names exist.
            if isinstance(last_bwav_name, str) and last_bwav_name:
                return _pretty_bwav_label(last_bwav_name)
            return "KKFM Aircheck"

        def _start_sonos() -> None:
            nonlocal sonos_state, sonos_room, sonos_ip, sonos_err, sonos_transport, sonos_last_poll, sonos_title, sonos_handoff_ts, sonos_stopped_polls
            sonos_err = None
            try:
                coord = sonos.find_coordinator()
                url = _stream_url_for_sonos(coord.ip)
                sonos_title = _sonos_track_title()
                _hand_to_sonos(coord.ip)
                sonos_handoff_ts = time.time()
                sonos_room, sonos_ip = coord.room, coord.ip
                sonos_state = "streaming"
                sonos_transport = None
                # First poll after the player has had time to open the stream
                # (it reports TRANSITIONING/STOPPED while connecting).
                sonos_last_poll = time.time() + 3.0
                sonos_stopped_polls = 0
            except sonos.SonosError as e:
                sonos_err = str(e)
                sonos_state = "idle"

        def _adopt_sonos() -> None:
            """On startup: if a Sonos coordinator is already playing this stream
            (radio or WAV URI for this host/port), take it over as STREAMING."""
            nonlocal sonos_state, sonos_room, sonos_ip, sonos_transport, sonos_last_poll, sonos_handoff_ts, sonos_radio, sonos_title
            try:
                coord = sonos.find_coordinator()
                cur = sonos.current_uri(coord.ip) or ""
                radio = sonos.radio_uri(_stream_url_for_sonos(coord.ip))
                if cur != radio:
                    return
                if sonos_state != "idle":
                    return   # 's' was pressed meanwhile
                sonos_room, sonos_ip = coord.room, coord.ip
                sonos_radio = True
                sonos_title = _sonos_track_title()
                sonos_transport = sonos.transport_state(coord.ip)
                sonos_handoff_ts = time.time()
                sonos_last_poll = time.time()
                sonos_state = "streaming"
            except Exception:
                return

        def _stop_sonos() -> None:
            nonlocal sonos_state, sonos_err, sonos_transport
            if sonos_ip:
                try:
                    sonos.stop(sonos_ip)
                except sonos.SonosError as e:
                    sonos_err = str(e)
            sonos_state = "idle"
            sonos_transport = None

        sonos_stopped_polls: int = 0
        sonos_resumes: int = 0
        # The stream declares 0xFFFFFFFF bytes (WAV header + Content-Length): at 48 kHz
        # stereo 16-bit that is 6 h 12 m 49 s, after which the Sonos treats it as ended.
        # Hand it over again a little before that.
        SONOS_REFRESH_S = 6 * 3600 + 10 * 60
        sonos_handoff_ts: float = 0.0

        # Player controls over HTTP (hourly player: /control/weather/<w>, /control/skip).
        control_msg: dict[str, Any] = {"text": None}

        def _send_control(path: str, label: str) -> None:
            url = f"{player_base}{path}"
            control_msg["text"] = f"{label}: sending..."

            def _go() -> None:
                try:
                    with urllib.request.urlopen(url, timeout=4) as resp:
                        body = resp.read().decode("utf-8", errors="replace").strip()
                    control_msg["text"] = f"{label}: {body}"
                except urllib.error.HTTPError as e:
                    control_msg["text"] = f"{label}: not supported by this player (HTTP {e.code})"
                except Exception as e:
                    control_msg["text"] = f"{label}: failed ({e})"

            threading.Thread(target=_go, daemon=True).start()

        def _poll_sonos() -> None:
            nonlocal sonos_transport, sonos_err, sonos_state, sonos_last_poll, sonos_stopped_polls, sonos_title, sonos_resumes, sonos_handoff_ts
            if sonos_state != "streaming" or not sonos_ip:
                return
            if not sonos_radio and sonos_handoff_ts and time.time() - sonos_handoff_ts >= SONOS_REFRESH_S:
                try:
                    _hand_to_sonos(sonos_ip)
                    sonos_handoff_ts = time.time()
                    sonos_resumes += 1
                    sonos_stopped_polls = 0
                    sonos_last_poll = time.time() + 3.0
                except sonos.SonosError as e:
                    sonos_err = str(e)
                return
            if time.time() - sonos_last_poll < 3.0:
                return
            sonos_last_poll = time.time()
            try:
                sonos_transport = sonos.transport_state(sonos_ip)
                if sonos_transport in ("STOPPED", "NO_MEDIA_PRESENT"):
                    sonos_stopped_polls += 1
                    if sonos_stopped_polls >= 2:
                        url = _stream_url_for_sonos(sonos_ip)
                        radio_url = sonos.radio_uri(url)
                        cur = sonos.current_uri(sonos_ip)
                        if cur not in (None, "", url, radio_url):
                            # Someone switched the Sonos to another source: hand it back to them.
                            sonos_err = f"Sonos {sonos_room}: switched to another source; stream released"
                            sonos_state = "idle"
                        else:
                            # The Sonos dropped our stream (it ends it on its own): put it straight
                            # back, same as pressing play in the Sonos app.
                            _hand_to_sonos(sonos_ip)
                            sonos_handoff_ts = time.time()
                            sonos_resumes += 1
                            sonos_err = None
                            sonos_stopped_polls = 0
                            sonos_last_poll = time.time() + 3.0
                else:
                    sonos_stopped_polls = 0
            except sonos.SonosError as e:
                # Keep the intent and retry on the next poll; show the error meanwhile.
                sonos_err = str(e)

        def _stop_player() -> None:
            nonlocal player_state, player_err
            stop_flag = player_proc_holder["stop_flag"]
            if stop_flag is not None:
                stop_flag["stop"] = True
            proc = player_proc_holder["proc"]
            player_proc_holder["proc"] = None
            player_proc_holder["stop_flag"] = None
            player_state = "idle"
            if proc is not None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        proc.kill()
                except Exception:
                    pass

        def _reset_player_lock() -> None:
            nonlocal player_state, player_err
            player_lock_holder["locked"] = False
            player_lock_holder["reason"] = None
            player_state = "idle"
            player_err = None

        def _start_player() -> None:
            nonlocal player_state, player_err
            if not player_bin:
                player_err = "no audio player found (install ffplay or mpv)"
                return
            try:
                proc = subprocess.Popen(
                    _player_cmd(player_bin),
                    stdin=subprocess.PIPE,      # we feed this ourselves (see
                    stdout=subprocess.DEVNULL,  # _stream_relay) - the player
                    stderr=subprocess.PIPE,     # never touches the network
                )
                if proc.stderr is not None:
                    os.set_blocking(proc.stderr.fileno(), False)
                player_proc_holder["proc"] = proc
                stop_flag = {"stop": False}
                player_proc_holder["stop_flag"] = stop_flag
                player_state = "playing"
                player_err = None
                threading.Thread(
                    target=_stream_relay,
                    args=(stream_url, proc.stdin, stop_flag, player_lock_holder),
                    daemon=True,
                ).start()
            except Exception as e:
                player_err = f"player launch failed: {e}"
                player_state = "idle"

        last_log_epoch: Optional[str] = None
        last_tx_epoch: Optional[str] = None

        try:
            log_status_stale_s = float(os.environ.get("WATCHDOG_LOG_STATUS_STALE_S", "30"))
        except Exception:
            log_status_stale_s = 30.0
        log_status_stale_s = max(0.0, log_status_stale_s)

        prev_jingle_playing: bool = False
        prev_comm_playing: bool = False

        # Watchdog health monitoring (watching the watchers)
        # Collects anomalies but doesn't persist them directly - dashboard is read-only
        watchdog_health_anomalies: list = []
        def _collect_health_anomaly(a) -> None:
            watchdog_health_anomalies.append(a)
        
        health_monitor = WatchdogHealthMonitor(
            emit_cb=_collect_health_anomaly,
            tx_status_path=tx_status,
            log_status_path=log_status,
        )
        last_health_check_mono = 0.0
        health_check_interval_s = 10.0  # Check every 10s
        watchdog_status_display: dict = {}

        threading.Thread(target=_adopt_sonos, daemon=True).start()

        ch = -1
        while True:
            # ch was read at the bottom of the previous pass (getch waits up to the
            # refresh interval there and also catches focus-in reports).
            if ch in (ord("q"), ord("Q")):
                return
            if ch in (ord("r"), ord("R")):
                if player_state == "locked":
                    _reset_player_lock()
                else:
                    _send_control("/control/skip", "next track")
            if ch in (ord("1"), ord("2"), ord("3")):
                w = {ord("1"): "sunny", ord("2"): "rainy", ord("3"): "snowy"}[ch]
                _send_control(f"/control/weather/{w}", f"weather {w}")
            if ch in (ord("p"), ord("P")) and player_state != "locked":
                if player_state == "idle":
                    _start_player()
                else:
                    _stop_player()
            if ch in (ord("s"), ord("S")):
                if sonos_state == "idle":
                    _start_sonos()
                else:
                    _stop_sonos()
            _poll_sonos()

            if player_lock_holder["locked"] and player_state != "locked":
                # The relay thread's integrity gate tripped: kill playback
                # outright and latch. Only an explicit 'r' clears this, same
                # as the jukebox's hardLock()/reset() - a bad stream can't
                # quietly "recover" into noise.
                proc = player_proc_holder["proc"]
                player_proc_holder["proc"] = None
                player_proc_holder["stop_flag"] = None
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                player_state = "locked"
                player_err = player_lock_holder["reason"] or "stream-integrity hard-lock"
            elif player_state != "locked":
                proc = player_proc_holder["proc"]
                sf = player_proc_holder.get("stop_flag")
                if proc is not None and proc.poll() is None and isinstance(sf, dict) and sf.get("ended") and not sf.get("stop"):
                    # Stream feed ended while the player is still alive: stop it and say so.
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    player_proc_holder["proc"] = None
                    player_proc_holder["stop_flag"] = None
                    player_state = "idle"
                    player_err = "stream ended: " + (sf.get("error") or "server closed the connection")
                    proc = None
                if proc is not None and proc.poll() is not None:
                    rc = proc.returncode
                    player_proc_holder["proc"] = None
                    player_proc_holder["stop_flag"] = None
                    player_state = "idle"
                    tail = ""
                    if proc.stderr is not None:
                        try:
                            tail = proc.stderr.read().decode("utf-8", errors="ignore").strip()
                        except Exception:
                            tail = ""
                        tail = tail.splitlines()[-1] if tail else ""
                    player_err = tail if tail else f"player exited (code {rc})"
            ch = -1

            try:
                if _need_full_repaint:
                    _need_full_repaint = False
                    curses.wrefresh(curses.curscr)
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

                # Periodic watchdog health check (watching the watchers)
                now_mono = time.monotonic()
                if (now_mono - last_health_check_mono) >= health_check_interval_s:
                    last_health_check_mono = now_mono
                    watchdog_status_display = health_monitor.check()
                    # Note: anomalies collected in watchdog_health_anomalies list
                    # In a full implementation, these would be forwarded to the reporter
                    # For now, the dashboard just displays the status

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

                onair_flag = False
                onair_flag = False
                tx_snap = remote.get("tx") if isinstance(remote, dict) else None
                if prof.transmitter and isinstance(tx_snap, dict):
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
                            last_tx_color = curses.color_pair(3)
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
                        onair_flag = audio_present
                        if audio_present:
                            tx_silence_since_ts = None
                        else:
                            if tx_silence_since_ts is None:
                                tx_silence_since_ts = now_ts
                            if (now_ts - tx_silence_since_ts) >= tx_silence_grace_s:
                                level = max(level, 1)

                if (
                    prof.transmitter
                    and ssh_target
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
                    header = f" {prof.title} DASHBOARD | {ts_str}{suffix}"
                elif max_x > 30:
                    header = f" {prof.title.split()[0]} DASHBOARD{suffix}"
                else:
                    header = " KKFM "
                
                stdscr.attron(header_color | curses.A_BOLD)
                stdscr.addnstr(0, 0, header.center(max_x), max(0, max_x - 1))
                stdscr.attroff(header_color | curses.A_BOLD)

                # PLAY CHIP: station on-air state, plus what's on air (song title,
                # or a bare "Jingle"/"Commercial" hint while those play). Toggled
                # with 'p', which spawns a real player (ffplay/mpv) against the
                # stream endpoint, same on-air source of truth as the header level.
                y = 2
                if y < max_y:
                    locked = player_state == "locked"
                    listening = player_state == "playing"
                    chip_attr = curses.color_pair(8) | curses.A_BOLD
                    if locked:
                        chip = "  ✕ LOCKED  "
                        chip_attr = curses.color_pair(13) | curses.A_BOLD  # magenta, matches CRITICAL
                    elif listening:
                        chip = "  PLAYING...  "
                    elif onair_flag:
                        chip = "  ● LIVE  "
                    else:
                        chip = "  ▶ PLAY  "
                    stdscr.attron(chip_attr)
                    stdscr.addnstr(y, 2, chip, max(0, max_x - 3))
                    stdscr.attroff(chip_attr)
                    hint_col = 2 + len(chip) + 1
                    if hint_col < max_x:
                        if locked:
                            stdscr.attron(curses.color_pair(13) | curses.A_BOLD)
                            hint = f"{player_err or 'stream-integrity hard-lock'} (press 'r' to reset)"
                            stdscr.addnstr(y, hint_col, hint[:70], max(0, max_x - hint_col - 1))
                            stdscr.attroff(curses.color_pair(13) | curses.A_BOLD)
                        elif player_err:
                            stdscr.attron(curses.color_pair(2))
                            stdscr.addnstr(y, hint_col, player_err[:70], max(0, max_x - hint_col - 1))
                            stdscr.attroff(curses.color_pair(2))
                        elif listening:
                            onair_title = (
                                last_song_name
                                if (not jingle_playing_now and not comm_playing_now and last_song_name)
                                else None
                            )
                            if onair_title:
                                stdscr.attron(title_text_attr)
                                stdscr.addnstr(y, hint_col, onair_title, max(0, max_x - hint_col - 1))
                                stdscr.attroff(title_text_attr)
                        else:
                            stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
                            stdscr.addnstr(y, hint_col, "<-- press 'p' to LISTEN!", max(0, max_x - hint_col - 1))
                            stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

                # CONTROLS: 1/2/3 set the player's weather, 'r' queues the next track.
                y = 4
                if y < max_y:
                    ctl = "1 Sunny  2 Rainy  3 Snowy  r next track"
                    if control_msg["text"]:
                        ctl += "   |  " + str(control_msg["text"])
                    stdscr.attron(curses.color_pair(3))
                    stdscr.addnstr(y, 4, ctl, max(0, max_x - 5))
                    stdscr.attroff(curses.color_pair(3))

                # STREAM CHIP: 's' sends the stream URL to the Sonos group coordinator.
                y = 3
                if y < max_y:
                    streaming = sonos_state == "streaming"
                    chip_attr = curses.color_pair(8) | curses.A_BOLD
                    chip = "  STREAMING...  " if streaming else "  ⇢ STREAM  "
                    stdscr.attron(chip_attr)
                    stdscr.addnstr(y, 2, chip, max(0, max_x - 3))
                    stdscr.attroff(chip_attr)
                    hint_col = 2 + len(chip) + 1
                    if hint_col < max_x:
                        if sonos_err:
                            stdscr.attron(curses.color_pair(2))
                            stdscr.addnstr(y, hint_col, sonos_err[:90], max(0, max_x - hint_col - 1))
                            stdscr.attroff(curses.color_pair(2))
                        elif streaming:
                            stdscr.attron(title_text_attr)
                            stdscr.addnstr(y, hint_col, f"Sonos {sonos_room} ({sonos_ip})  {sonos_transport or 'starting'}" + (f"  (auto-resumed {sonos_resumes}x)" if sonos_resumes else ""), max(0, max_x - hint_col - 1))
                            stdscr.attroff(title_text_attr)
                        else:
                            stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
                            stdscr.addnstr(y, hint_col, "<-- press 's' to STREAM to Sonos", max(0, max_x - hint_col - 1))
                            stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

                y = 6
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
                            st = _systemctl_state(u, ssh_target=ssh_target, user=prof.user_units)
                    
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
                                last_tx_color = curses.color_pair(3)
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
                            # Check for fault state first (highest priority display)
                            # TODO: Maybe add distinct color/style for fault state?
                            if tx.get("in_fault_state"):
                                label = "FAULT"
                                color = curses.color_pair(2)  # Red/error color
                                last_tx_label = label
                                last_tx_color = color
                            elif tx.get("recovery_in_progress"):
                                attempt = tx.get("recovery_attempt_count", 0)
                                label = f"RECOVERING ({attempt})"
                                color = curses.color_pair(3)  # Yellow/warn color
                                last_tx_label = label
                                last_tx_color = color
                            elif audio_present:
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
                                last_song_name = None
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
                            last_song_name = None
                            prev_jingle_playing = False
                            prev_comm_playing = False

                        wd = None
                        if isinstance(remote, dict) and isinstance(remote.get("services"), dict):
                            rst = remote["services"].get(prof.watchdog_unit)
                            if isinstance(rst, dict):
                                wd = ServiceState(
                                    name=prof.watchdog_unit,
                                    active=str(rst.get("active") or "?"),
                                    sub=str(rst.get("sub") or "?"),
                                    since=str(rst.get("since") or ""),
                                )
                        if wd is None:
                            if ssh_target and remote_stream:
                                wd = ServiceState(name=prof.watchdog_unit, active="CONNECT", sub="-", since="")
                            else:
                                wd = _systemctl_state(prof.watchdog_unit, ssh_target=ssh_target, user=prof.user_units)
                        wd_active = wd.active == "active"
                        # No systemd (local run, e.g. macOS): the log monitor is up when its
                        # status file is fresh and the PID it recorded is alive.
                        if not wd_active and not ssh_target and not log_stale:
                            try:
                                mon_pid = int(ls.get("pid") or 0)
                                if mon_pid > 0:
                                    os.kill(mon_pid, 0)
                                    wd = ServiceState(name=wd.name, active="active", sub=f"pid {mon_pid}", since="")
                                    wd_active = True
                            except Exception:
                                pass

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
                            elif etype == "now_playing":
                                last_song_name = ename

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
                            # Type indicator first, then label
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

                            # Fixed column positions for alignment
                            label_col = 4
                            status_col = 11
                            content_col = 19
                            
                            # Print LOGS: label at col 4
                            stdscr.addnstr(y, label_col, "LOGS:", max(0, max_x - label_col - 1))
                            
                            # Print level at fixed col 11
                            if status_col < max_x:
                                if level_attr:
                                    stdscr.attron(level_attr)
                                stdscr.addnstr(y, status_col, f"{level[:7]:<8}", max(0, max_x - status_col - 1))
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
                                    if content_col < max_x:
                                        stdscr.addnstr(y, content_col, group1, max(0, max_x - content_col - 1))
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
                            # Fixed column positions for alignment
                            label_col = 4
                            status_col = 11
                            content_col = 19
                            
                            if isinstance(last_bwav_name, str) and last_bwav_name:
                                src_label = (last_bwav_src or "FILE")[:7]
                                shown = _pretty_bwav_label(last_bwav_name)
                                # Print BWAV: at col 4
                                stdscr.addnstr(y, label_col, "BWAV:", max(0, max_x - label_col - 1))
                                # Print src_label at col 10
                                stdscr.attron(curses.color_pair(4))
                                if status_col < max_x:
                                    stdscr.addnstr(y, status_col, f"{src_label:<8}", max(0, max_x - status_col - 1))
                                stdscr.attroff(curses.color_pair(4))
                                # Print file name at col 18
                                if content_col < max_x:
                                    stdscr.addnstr(y, content_col, f"{shown[:40]}", max(0, max_x - content_col - 1))
                            else:
                                stdscr.addnstr(y, label_col, "BWAV:", max(0, max_x - label_col - 1))
                                if status_col < max_x:
                                    stdscr.addnstr(y, status_col, "IDLE", max(0, max_x - status_col - 1))
                            y += 1
                        
                        for kind, data in [("JING", jing), ("COMM", com)]:
                            if y >= max_y: break
                            # Fixed column positions for alignment
                            label_col = 4
                            status_col = 11
                            content_col = 19
                            
                            if data.get("started_name"):
                                # Print label at col 4
                                stdscr.addnstr(y, label_col, f"{kind}:", max(0, max_x - label_col - 1))
                                # Print PLAYING at col 10
                                stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
                                if status_col < max_x:
                                    stdscr.addnstr(y, status_col, f"{'PLAYING':<8}", max(0, max_x - status_col - 1))
                                stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
                                # Print file name at col 18
                                shown = _pretty_bwav_label(data.get("started_name"))
                                if content_col < max_x:
                                    stdscr.addnstr(y, content_col, f"{shown[:40]}", max(0, max_x - content_col - 1))
                            elif data.get("picked_name"):
                                # Print label at col 4
                                stdscr.addnstr(y, label_col, f"{kind}:", max(0, max_x - label_col - 1))
                                # Print PICKED at col 10
                                stdscr.attron(curses.color_pair(3))
                                if status_col < max_x:
                                    stdscr.addnstr(y, status_col, f"{'PICKED':<8}", max(0, max_x - status_col - 1))
                                stdscr.attroff(curses.color_pair(3))
                                # Print file name at col 18
                                shown = _pretty_bwav_label(data.get("picked_name"))
                                if content_col < max_x:
                                    stdscr.addnstr(y, content_col, f"{shown[:40]}", max(0, max_x - content_col - 1))
                            else:
                                is_jingle = kind == "JING"
                                last_name = last_jingle_name if is_jingle else last_comm_name
                                last_state = last_jingle_state if is_jingle else last_comm_state
                                if isinstance(last_name, str) and last_name:
                                    label_str = (last_state or "PREV")[:7]
                                    # Print label at col 4
                                    stdscr.addnstr(y, label_col, f"{kind}:", max(0, max_x - label_col - 1))
                                    # Print state at col 10
                                    stdscr.attron(curses.color_pair(4))
                                    if status_col < max_x:
                                        stdscr.addnstr(y, status_col, f"{label_str:<8}", max(0, max_x - status_col - 1))
                                    stdscr.attroff(curses.color_pair(4))
                                    # Print file name at col 18
                                    shown = _pretty_bwav_label(last_name)
                                    if content_col < max_x:
                                        stdscr.addnstr(y, content_col, f"{shown[:40]}", max(0, max_x - content_col - 1))
                                else:
                                    # Print label at col 4, IDLE at col 10
                                    stdscr.addnstr(y, label_col, f"{kind}:", max(0, max_x - label_col - 1))
                                    if status_col < max_x:
                                        stdscr.addnstr(y, status_col, "IDLE", max(0, max_x - status_col - 1))
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
                    # Get most recent anomaly for the header
                    most_recent = sorted_anomalies[0] if sorted_anomalies else None
                    most_recent_ts = most_recent.get("ts", 0) if most_recent else 0
                    most_recent_title = most_recent.get("code", "") if most_recent else ""
                    most_recent_sev = most_recent.get("severity", "info").lower() if most_recent else "info"
                    
                    # Determine header color based on most recent severity
                    if most_recent_sev == "critical":
                        header_color = curses.color_pair(13)  # Purple
                    elif most_recent_sev == "error":
                        header_color = curses.color_pair(2)   # Red
                    elif most_recent_sev == "warn":
                        header_color = curses.color_pair(3)
                    else:
                        header_color = curses.color_pair(4)
                    
                    # Header line: DATE + most recent anomaly title (colored by severity)
                    try:
                        date_str = time.strftime("%m/%d", time.localtime(most_recent_ts))
                    except Exception:
                        date_str = "??/??"
                    
                    if y < max_y:
                        stdscr.addnstr(y, 4, f"{date_str}  ", max(0, max_x - 5))
                        cur_x = stdscr.getyx()[1]
                        if cur_x < max_x:
                            stdscr.attron(header_color | curses.A_BOLD)
                            stdscr.addnstr(most_recent_title, max(0, max_x - cur_x - 1))
                            stdscr.attroff(header_color | curses.A_BOLD)
                        y += 1
                    
                    # Track last date shown to only print new dates
                    last_date = date_str
                    
                    # Each anomaly line: SEVERITY | HH:MM:SS | MESSAGE
                    # Severity at col 11 to align with title (header is "MM/DD  TITLE" where title starts at col 11)
                    # Timestamp at col 19, message at col 29
                    sev_col = 11
                    ts_col = 19
                    msg_col = 29
                    
                    for r in sorted_anomalies:
                        if y >= max_y: break
                        sev = r.get("severity", "info").lower()
                        if sev == "critical":
                            color = curses.color_pair(13)  # Purple
                        elif sev == "error":
                            color = curses.color_pair(2)   # Red
                        elif sev == "warn":
                            color = curses.color_pair(3)
                        else:
                            color = curses.color_pair(4)
                        
                        ts = r.get("ts", 0)
                        try:
                            this_date = time.strftime("%m/%d", time.localtime(ts))
                            time_str = time.strftime("%H:%M:%S", time.localtime(ts))
                        except Exception:
                            this_date = "??/??"
                            time_str = "??:??:??"
                        
                        # If date changed, print a new date header line with blank line above
                        if this_date != last_date:
                            if y < max_y:
                                y += 1  # Blank line to separate from previous date's entries
                                title_for_date = r.get("code", "")
                                stdscr.addnstr(y, 4, f"{this_date}  {title_for_date}", max(0, max_x - 5))
                                y += 1
                                last_date = this_date
                        
                        if y >= max_y: break
                        
                        msg = r.get("message", "")
                        
                        # SEVERITY (colored, bold, 6 chars to fit before ts_col 10)
                        stdscr.attron(color | curses.A_BOLD)
                        stdscr.addnstr(y, sev_col, f"{sev.upper():<6}", max(0, max_x - sev_col - 1))
                        stdscr.attroff(color | curses.A_BOLD)
                        
                        # TIMESTAMP (HH:MM:SS, 8 chars)
                        if ts_col < max_x:
                            stdscr.addnstr(y, ts_col, f"{time_str:<8}", max(0, max_x - ts_col - 1))
                        
                        # MESSAGE (just the message, title is in header)
                        if msg_col < max_x:
                            remaining_width = max(0, max_x - msg_col - 1)
                            stdscr.addnstr(y, msg_col, msg[:remaining_width], remaining_width)
                        
                        y += 1

                stdscr.refresh()

                # Read input; getch blocks up to timeout ms.
                # Detect focus-in sequence ESC [ I (27, 91, 73).
                try:
                    ch = stdscr.getch()
                except curses.error:
                    ch = -1
                if ch in (ord("q"), ord("Q")):
                    return
                if ch == 27:  # ESC - might be focus report
                    stdscr.nodelay(True)
                    try:
                        c2 = stdscr.getch()
                        c3 = stdscr.getch()
                    except curses.error:
                        c2 = c3 = -1
                    stdscr.timeout(max(100, int(refresh_s * 1000)))
                    if c2 == ord("[") and c3 == ord("I"):
                        # Focus-in: force full repaint on next iteration
                        _need_full_repaint = True

            except curses.error:
                curses.napms(50)
                continue

        # Disable focus reporting on exit
        try:
            sys.stdout.write("\033[?1004l")
            sys.stdout.flush()
        except Exception:
            pass

    try:
        curses.wrapper(draw)
    finally:
        if tunnel is not None:
            tunnel.close()
        proc = player_proc_holder["proc"]
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    return 0