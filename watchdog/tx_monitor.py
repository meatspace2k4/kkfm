from __future__ import annotations

import re
import time
import json
import os
import uuid
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from .anomalies import Anomaly, AnomalyCode, Severity
from .paths import get_runtime_status_dir

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None


TELEMETRY_RE = re.compile(
    r"^(?P<ms>\d+)\s+"
    r"(?P<freq>\d+(?:\.\d+)?)\s+"
    r"(?P<noise>-?\d+)\s+"
    r"(?P<inlevel>-?\d+)\s+"
    r"(?P<over>[01])\s*$"
)


@dataclass
class TxState:
    armed: bool = False
    arm_after_mono: float = 0.0
    last_telem_mono: float = 0.0
    last_ms: Optional[int] = None
    last_restart_mono: float = 0.0
    last_missing_mono: float = 0.0
    last_recover_mono: float = 0.0
    last_status_write_mono: float = 0.0
    last_overmod_mono: float = 0.0


def _mk(
    code: AnomalyCode,
    severity: Severity,
    now_ts: float,
    message: str,
    raw: Optional[str] = None,
) -> Anomaly:
    return Anomaly(code=code, severity=severity, ts=now_ts, message=message, raw=raw)


def monitor_tx(
    *,
    port: str,
    baudrate: int,
    heartbeat_timeout_s: float,
    start_grace_s: float,
    emit_cooldown_s: float,
    status_path: Optional[str],
    status_write_min_interval_s: float,
    out_cb,
) -> None:
    """Monitor Arduino UNO serial logs for TX health and emit anomalies.

        Expects the `radio/si4713.ino` sketch log format:
            - startup banner: "TX_FREQ=..."
            - periodic telemetry: "<ms> <freq> <noise> <currInLevel> <overMod>"
                where `currInLevel` is the Si4713 ASQ-reported input level (NOT Pi dBFS).
            - recovery lines: "RECOVER: ..."

    out_cb: function(Anomaly) -> None
    """
    if serial is None:
        raise RuntimeError("pyserial not available (install python3-serial)")

    # Heuristic (because the sketch reports Si4713 ASQ `currInLevel`, not Pi dBFS):
    # - Some builds report `currInLevel` as a signed dB-like value (e.g. -10).
    # - Others report it as a small non-negative level-ish integer.
    # We support both with separate env-tunable thresholds.
    try:
        audio_inlevel_threshold = int(os.environ.get("WATCHDOG_TX_INLEVEL_AUDIO_THRESHOLD", "2"))
    except Exception:
        audio_inlevel_threshold = 2
    try:
        audio_db_threshold = float(os.environ.get("WATCHDOG_TX_AUDIO_DB_THRESHOLD", "-50"))
    except Exception:
        audio_db_threshold = -50.0

    try:
        restart_grace_s = float(os.environ.get("WATCHDOG_TX_RESTART_GRACE_S", "5.0"))
    except Exception:
        restart_grace_s = 5.0
    restart_grace_s = max(0.0, restart_grace_s)

    # `overmod` can be a brief spike; latch it so the dashboard doesn't miss it
    # when status writes are infrequent.
    try:
        overmod_hold_s = float(os.environ.get("WATCHDOG_TX_OVERMOD_HOLD_S", "10.0"))
    except Exception:
        overmod_hold_s = 10.0
    overmod_hold_s = max(0.0, overmod_hold_s)

    st = TxState()
    epoch = uuid.uuid4().hex
    proc_start_ts = time.time()
    proc_pid = os.getpid()
    start_mono = time.monotonic()
    st.last_telem_mono = start_mono
    st.arm_after_mono = start_mono + float(start_grace_s)
    status_p = Path(status_path) if status_path else None
    if status_p is not None and os.environ.get("WATCHDOG_STATUS_TO_DISK") != "1":
        sp = str(status_p)
        if not (sp.startswith("/run/") or sp.startswith("/dev/shm/") or sp.startswith("/tmp/")):
            status_p = get_runtime_status_dir() / status_p.name

    while True:
        ser = None
        try:
            # Open serial in a way that avoids DTR transitions (which reset many Arduino boards).
            # `serial.Serial(port=...)` may toggle DTR as part of open on some platforms/drivers.
            ser = serial.Serial()  # type: ignore[call-arg]
            ser.port = port
            ser.baudrate = baudrate
            ser.timeout = 0.25
            try:
                ser.dtr = False
            except Exception:
                pass
            try:
                ser.rts = False
            except Exception:
                pass
            ser.open()
            with ser:
                # Drop any boot garbage without resetting the board.
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass

                while True:
                    now_ts = time.time()
                    now_mono = time.monotonic()

                    line: Optional[str] = None
                    raw = ser.readline()
                    if raw:
                        decoded = raw.decode("utf-8", errors="ignore").strip()
                        if decoded:
                            line = decoded

                    if line is not None:
                        # Sketch restart signature: banner appears again after we've already been armed.
                        if line.startswith("TX_FREQ="):
                            # Treat the banner itself as activity and suppress missing-heartbeat
                            # for a short grace window while the sketch finishes booting.
                            was_armed = st.armed
                            st.last_telem_mono = now_mono
                            st.armed = False
                            st.arm_after_mono = max(st.arm_after_mono, now_mono + restart_grace_s)

                            if was_armed and (now_mono - st.last_restart_mono) >= emit_cooldown_s:
                                st.last_restart_mono = now_mono
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_SKETCH_RESTART,
                                        Severity.WARN,
                                        now_ts,
                                        message=f"Arduino/sketch restarted (saw TX_FREQ banner again) on {port}",
                                        raw=line,
                                    )
                                )
                            continue

                        # Recovery events from the hardened sketch.
                        if line.startswith("RECOVER:"):
                            # Recovery logs prove the MCU is alive; treat as activity.
                            st.last_telem_mono = now_mono
                            if (now_mono - st.last_recover_mono) >= emit_cooldown_s:
                                st.last_recover_mono = now_mono
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_RECOVERY,
                                        Severity.WARN,
                                        now_ts,
                                        message="transmitter entered auto-recovery (I2C timeout / reset / re-init)",
                                        raw=line,
                                    )
                                )
                            continue

                        m = TELEMETRY_RE.match(line)
                        if m:
                            st.last_telem_mono = now_mono
                        else:
                            # Ignore non-telemetry chatter.
                            line = None

                    # Arm after grace period (avoid false alarms during UNO auto-reset and restarts).
                    if (not st.armed) and now_mono >= st.arm_after_mono:
                        st.armed = True

                    # Periodic missing-heartbeat check.
                    if st.armed and (now_mono - st.last_telem_mono) > heartbeat_timeout_s:
                        if (now_mono - st.last_missing_mono) >= emit_cooldown_s:
                            st.last_missing_mono = now_mono

                            # Escalate missing-telemetry severity; even short gaps are suspicious.
                            missing_dur = now_mono - st.last_telem_mono
                            if missing_dur >= 30.0:
                                sev = Severity.CRITICAL
                            elif missing_dur >= 10.0:
                                sev = Severity.ERROR
                            else:
                                sev = Severity.WARN

                            out_cb(
                                _mk(
                                    AnomalyCode.TX_HEARTBEAT_MISSING,
                                    sev,
                                    now_ts,
                                    message=(
                                        f"No Arduino telemetry for {missing_dur:.1f}s on {port} "
                                        f"(USB/power reset, port contention, or sketch hung)"
                                    ),
                                )
                            )

                    if line is None:
                        continue

                    ms = int(m.group("ms"))
                    freq = float(m.group("freq"))
                    noise = int(m.group("noise"))
                    inlevel = int(m.group("inlevel"))
                    over = int(m.group("over"))

                    if over:
                        st.last_overmod_mono = now_mono

                    if inlevel < 0:
                        # Treat as dB-like (less negative => louder).
                        audio_present = float(inlevel) > float(audio_db_threshold)
                        audio_present_kind = "db"
                    else:
                        audio_present = inlevel >= audio_inlevel_threshold
                        audio_present_kind = "level"

                    # Heartbeat/status file for external monitoring.
                    # Keep write frequency low (SD wear); prefer a tmpfs path if possible.
                    if status_p is not None:
                        if (now_mono - st.last_status_write_mono) >= status_write_min_interval_s:
                            st.last_status_write_mono = now_mono
                            rec = {
                                "ts": now_ts,
                                "epoch": epoch,
                                "proc_start_ts": proc_start_ts,
                                "pid": proc_pid,
                                "port": port,
                                "baudrate": baudrate,
                                "ms": ms,
                                "freq_mhz": freq,
                                "noise": noise,
                                "inlevel": inlevel,
                                "inlevel_kind": "si4713_currInLevel",
                                "audio_present": audio_present,
                                "audio_inlevel_threshold": audio_inlevel_threshold,
                                "audio_db_threshold": audio_db_threshold,
                                "audio_present_kind": audio_present_kind,
                                # `overmod` is latched to avoid missing spikes between writes.
                                "overmod": 1 if (now_mono - st.last_overmod_mono) <= overmod_hold_s else 0,
                                "overmod_sample": over,
                            }
                            status_p.parent.mkdir(parents=True, exist_ok=True)
                            # Use a unique tmp name to avoid collisions across processes.
                            tmp = status_p.with_suffix(status_p.suffix + f".{os.getpid()}.tmp")
                            tmp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
                            tmp.replace(status_p)

                    # Detect restarts by millis() going backwards significantly.
                    # Guard against small jitter and early-boot behavior.
                    if st.last_ms is not None:
                        reboot_sig = (ms + 5000 < st.last_ms) and (st.last_ms > 60000)
                        if reboot_sig:
                            # Suppress missing-heartbeat briefly after a reboot signature.
                            st.armed = False
                            st.arm_after_mono = max(st.arm_after_mono, now_mono + restart_grace_s)

                            if (now_mono - st.last_restart_mono) >= emit_cooldown_s:
                                st.last_restart_mono = now_mono
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_SKETCH_RESTART,
                                        Severity.WARN,
                                        now_ts,
                                        message=(
                                            f"Arduino reboot/reset detected on {port} "
                                            f"(millis counter reset: prev={st.last_ms} now={ms})"
                                        ),
                                        raw=line,
                                    )
                                )

                    st.last_ms = ms

        except Exception as e:
            # Clean up serial if open() succeeded but with ser: block wasn't entered
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            now_ts = time.time()
            now_mono = time.monotonic()
            if (now_mono - st.last_missing_mono) >= emit_cooldown_s:
                st.last_missing_mono = now_mono
                out_cb(
                    _mk(
                        AnomalyCode.TX_SERIAL_ERROR,
                        Severity.ERROR,
                        now_ts,
                        message=(
                            f"Lost serial connection to Arduino on {port} "
                            f"(device reset/disconnected or another process is using the port)"
                        ),
                        raw=f"{type(e).__name__}: {e}",
                    )
                )
            time.sleep(1.0)


