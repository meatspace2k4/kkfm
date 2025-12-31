from __future__ import annotations

import re
import subprocess
import time
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable
from pathlib import Path

from .anomalies import Anomaly, AnomalyCode, Severity
from .paths import get_runtime_status_dir
from .restart_tx_sketch import _reset_via_dtr, _reset_via_1200_baud_touch

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None


# ---------------------------------------------------------------------------
# Service status check (to avoid recovery attempts during maintenance)
# ---------------------------------------------------------------------------
# The TX watchdog should not attempt Arduino recovery if the main audio service
# is intentionally stopped for maintenance. This prevents false positives.

MAIN_SERVICE_UNIT = os.environ.get("WATCHDOG_MAIN_SERVICE_UNIT", "acnh-aircheck.service")


def _is_main_service_running() -> bool:
    """Check if the main audio service is running (active).
    
    Returns True if the service is running, False if stopped/inactive/failed,
    or True if we can't determine (fail-open to allow recovery).
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", MAIN_SERVICE_UNIT],
            timeout=5.0,
            capture_output=True,
        )
        # Exit code 0 = active, non-zero = inactive/failed/unknown
        return result.returncode == 0
    except Exception:
        # If we can't check, assume running (fail-open)
        return True


def _is_main_service_enabled() -> bool:
    """Check if the main audio service is enabled (will auto-start on boot).
    
    Returns True if enabled, False if disabled/masked.
    If we can't determine, returns True (fail-open).
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", MAIN_SERVICE_UNIT],
            timeout=5.0,
            capture_output=True,
        )
        # Exit code 0 = enabled, non-zero = disabled/masked/etc
        return result.returncode == 0
    except Exception:
        return True


def _start_main_service(out_cb: Callable[[Anomaly], None]) -> bool:
    """Attempt to start the main audio service.
    
    Returns True if the start command succeeded, False otherwise.
    """
    now_ts = time.time()
    
    out_cb(
        Anomaly(
            code=AnomalyCode.SERVICE_RESTART_ATTEMPT,
            severity=Severity.INFO,
            ts=now_ts,
            message=f"Attempting to start {MAIN_SERVICE_UNIT}",
        )
    )
    
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "start", MAIN_SERVICE_UNIT],
            timeout=30.0,
            capture_output=True,
        )
        if result.returncode == 0:
            out_cb(
                Anomaly(
                    code=AnomalyCode.SERVICE_RECOVERY_SUCCESS,
                    severity=Severity.INFO,
                    ts=time.time(),
                    message=f"{MAIN_SERVICE_UNIT} started successfully",
                )
            )
            return True
        else:
            out_cb(
                Anomaly(
                    code=AnomalyCode.SERVICE_RESTART_FAILED,
                    severity=Severity.ERROR,
                    ts=time.time(),
                    message=f"Failed to start {MAIN_SERVICE_UNIT}: exit code {result.returncode}",
                    raw=result.stderr.decode("utf-8", errors="replace") if result.stderr else None,
                )
            )
            return False
    except Exception as e:
        out_cb(
            Anomaly(
                code=AnomalyCode.SERVICE_RESTART_FAILED,
                severity=Severity.ERROR,
                ts=time.time(),
                message=f"Exception starting {MAIN_SERVICE_UNIT}: {e}",
            )
        )
        return False


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
    # Auto-recovery state
    recovery_attempt_count: int = 0
    recovery_in_progress: bool = False
    recovery_started_mono: float = 0.0
    audio_present_streak: int = 0  # Consecutive audio-present telemetry lines
    in_fault_state: bool = False  # True when all recovery attempts exhausted
    last_restart_attempt_mono: float = 0.0
    # Silence tracking (separate from heartbeat missing)
    silence_started_mono: float = 0.0  # When sustained silence began
    last_silence_emit_mono: float = 0.0  # Cooldown for TX_SILENCE_DETECTED
    # Heartbeat recovery tracking
    was_missing_heartbeat: bool = False  # Track if we were in missing-heartbeat state
    # Service-level recovery tracking
    service_recovery_attempt_count: int = 0
    last_service_recovery_mono: float = 0.0


# Maximum service recovery attempts before giving up
try:
    SERVICE_MAX_RESTART_ATTEMPTS = int(os.environ.get("WATCHDOG_SERVICE_MAX_RESTART_ATTEMPTS", "3"))
except Exception:
    SERVICE_MAX_RESTART_ATTEMPTS = 3

# Minimum interval between service restart attempts
try:
    SERVICE_RESTART_INTERVAL_S = float(os.environ.get("WATCHDOG_SERVICE_RESTART_INTERVAL_S", "60.0"))
except Exception:
    SERVICE_RESTART_INTERVAL_S = 60.0


def _mk(
    code: AnomalyCode,
    severity: Severity,
    now_ts: float,
    message: str,
    raw: Optional[str] = None,
) -> Anomaly:
    return Anomaly(code=code, severity=severity, ts=now_ts, message=message, raw=raw)


# ---------------------------------------------------------------------------
# Auto-recovery configuration
# ---------------------------------------------------------------------------
# After 30s of missing heartbeat (dead air threshold), attempt DTR reset.
# Each reset attempt gets a grace window to confirm audio is flowing again.
# We require 2 consecutive audio-present telemetry samples (~1-2s) to confirm.
# After 3 failed attempts, enter fault state (emit TX_RECOVERY_FAILED CRITICAL).

try:
    TX_DEAD_AIR_THRESHOLD_S = float(os.environ.get("WATCHDOG_TX_DEAD_AIR_S", "30.0"))
except Exception:
    TX_DEAD_AIR_THRESHOLD_S = 30.0

try:
    TX_MAX_RESTART_ATTEMPTS = int(os.environ.get("WATCHDOG_TX_MAX_RESTART_ATTEMPTS", "3"))
except Exception:
    TX_MAX_RESTART_ATTEMPTS = 3

try:
    TX_RECOVERY_CONFIRM_WINDOW_S = float(os.environ.get("WATCHDOG_TX_RECOVERY_CONFIRM_S", "30.0"))
except Exception:
    TX_RECOVERY_CONFIRM_WINDOW_S = 30.0

try:
    TX_AUDIO_PRESENT_STREAK_REQUIRED = int(os.environ.get("WATCHDOG_TX_AUDIO_STREAK", "2"))
except Exception:
    TX_AUDIO_PRESENT_STREAK_REQUIRED = 2


def _attempt_tx_reset(
    port: str,
    baudrate: int,
    attempt_num: int,
    out_cb: Callable[[Anomaly], None],
) -> bool:
    """Attempt to reset the TX Arduino. Returns True if reset command succeeded."""
    now_ts = time.time()
    
    # Emit restart attempt anomaly
    out_cb(
        _mk(
            AnomalyCode.TX_RESTART_ATTEMPT,
            Severity.INFO,
            now_ts,
            message=f"Attempting TX Arduino reset (attempt {attempt_num}/{TX_MAX_RESTART_ATTEMPTS}) on {port}",
        )
    )
    
    # Try DTR reset first (works for Uno)
    res = _reset_via_dtr(port=port, baudrate=baudrate, pulse_s=0.25)
    if res.ok:
        return True
    
    # Fallback to 1200-baud touch (works for Leonardo/Micro)
    res = _reset_via_1200_baud_touch(port=port, pulse_s=0.25)
    return res.ok


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
            status_p = get_runtime_status_dir() / Path(status_path).name

    # ---------------------------------------------------------------------------
    # Restore fault state from previous tx_status.json (survives watchdog restart)
    # ---------------------------------------------------------------------------
    # If we were in fault state before restart, we should stay in fault state.
    # The admin must either fix the issue or manually clear the state.
    if status_p is not None:
        try:
            if status_p.exists():
                prev_status = json.loads(status_p.read_text(encoding="utf-8").strip())
                if prev_status.get("in_fault_state"):
                    st.in_fault_state = True
                    st.recovery_attempt_count = prev_status.get("recovery_attempt_count", TX_MAX_RESTART_ATTEMPTS)
                    # Emit anomaly so admin sees fault state persisted across restart
                    out_cb(
                        _mk(
                            AnomalyCode.TX_RECOVERY_FAILED,
                            Severity.CRITICAL,
                            time.time(),
                            message=(
                                f"TX fault state restored from previous session on {port}. "
                                f"Previous recovery attempts: {st.recovery_attempt_count}. "
                                f"Administrator intervention still required."
                            ),
                        )
                    )
        except Exception:
            pass  # If we can't read previous state, start fresh
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
                            # Check if we're recovering from missing heartbeat
                            if st.was_missing_heartbeat and st.armed:
                                gap_duration = now_mono - st.last_telem_mono
                                st.was_missing_heartbeat = False
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_HEARTBEAT_RECOVERED,
                                        Severity.INFO,
                                        now_ts,
                                        message=(
                                            f"Arduino telemetry resumed on {port} after {gap_duration:.1f}s gap"
                                        ),
                                    )
                                )
                            st.last_telem_mono = now_mono
                        else:
                            # Ignore non-telemetry chatter.
                            line = None

                    # Arm after grace period (avoid false alarms during UNO auto-reset and restarts).
                    if (not st.armed) and now_mono >= st.arm_after_mono:
                        st.armed = True

                    # Periodic missing-heartbeat check.
                    if st.armed and (now_mono - st.last_telem_mono) > heartbeat_timeout_s:
                        st.was_missing_heartbeat = True  # Track for recovery detection
                        if (now_mono - st.last_missing_mono) >= emit_cooldown_s:
                            st.last_missing_mono = now_mono

                            # Escalate missing-telemetry severity; max severity is ERROR.
                            # CRITICAL is reserved for TX_RECOVERY_FAILED (fault state).
                            missing_dur = now_mono - st.last_telem_mono
                            if missing_dur >= 10.0:
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
                        
                        # ---------------------------------------------------------------------------
                        # Auto-recovery: attempt DTR reset after sustained heartbeat failure
                        # ---------------------------------------------------------------------------
                        # Only attempt recovery if:
                        # 1. Not already in fault state (all attempts exhausted)
                        # 2. Not currently in a recovery confirmation window
                        # 3. Missing heartbeat duration exceeds dead air threshold (30s)
                        # 4. Haven't attempted a restart too recently (avoid rapid-fire resets)
                        # 5. Main audio service is actually running (not in maintenance)
                        missing_dur = now_mono - st.last_telem_mono
                        min_restart_interval = TX_RECOVERY_CONFIRM_WINDOW_S + 5.0  # Don't restart during confirmation
                        
                        # Check if main service is running before attempting recovery
                        main_service_running = _is_main_service_running()
                        main_service_enabled = _is_main_service_enabled()
                        
                        # ---------------------------------------------------------------------------
                        # Service-level recovery: if service is enabled but not running, start it
                        # ---------------------------------------------------------------------------
                        if (
                            not main_service_running
                            and main_service_enabled
                            and missing_dur >= TX_DEAD_AIR_THRESHOLD_S
                            and st.service_recovery_attempt_count < SERVICE_MAX_RESTART_ATTEMPTS
                            and (now_mono - st.last_service_recovery_mono) >= SERVICE_RESTART_INTERVAL_S
                        ):
                            st.service_recovery_attempt_count += 1
                            st.last_service_recovery_mono = now_mono
                            
                            if _start_main_service(out_cb):
                                # Service started, reset TX recovery counters
                                st.recovery_attempt_count = 0
                                st.service_recovery_attempt_count = 0
                                # Give service time to start producing audio
                                time.sleep(5.0)
                                continue
                        
                        # TX-level recovery: only if service is running
                        if (
                            not st.in_fault_state
                            and not st.recovery_in_progress
                            and missing_dur >= TX_DEAD_AIR_THRESHOLD_S
                            and (now_mono - st.last_restart_attempt_mono) >= min_restart_interval
                            and st.recovery_attempt_count < TX_MAX_RESTART_ATTEMPTS
                            and main_service_running  # Skip TX recovery if service is stopped
                        ):
                            st.recovery_attempt_count += 1
                            st.recovery_in_progress = True
                            st.recovery_started_mono = now_mono
                            st.last_restart_attempt_mono = now_mono
                            st.audio_present_streak = 0
                            
                            # Close serial before reset attempt to release the port
                            try:
                                ser.close()
                            except Exception:
                                pass
                            
                            reset_ok = _attempt_tx_reset(port, baudrate, st.recovery_attempt_count, out_cb)
                            
                            if not reset_ok:
                                # Reset command itself failed - log and continue to next attempt
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_RESTART_FAILED,
                                        Severity.ERROR,
                                        time.time(),
                                        message=(
                                            f"TX restart attempt {st.recovery_attempt_count}/{TX_MAX_RESTART_ATTEMPTS} "
                                            f"failed to execute on {port}"
                                        ),
                                    )
                                )
                                st.recovery_in_progress = False
                            
                            # Give sketch time to boot before re-opening serial
                            time.sleep(2.0)
                            
                            # Re-open will happen in the outer exception handler / retry loop
                            raise Exception("Restarting serial connection after TX reset attempt")

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

                    # ---------------------------------------------------------------------------
                    # Silence tracking: detect sustained silence over TX (separate from heartbeat)
                    # ---------------------------------------------------------------------------
                    # This catches the case where telemetry is flowing but no audio is present.
                    if audio_present:
                        # Audio is present, reset silence tracking
                        st.silence_started_mono = 0.0
                    else:
                        # No audio - track how long
                        if st.silence_started_mono == 0.0:
                            st.silence_started_mono = now_mono
                        
                        silence_duration = now_mono - st.silence_started_mono
                        
                        # Emit TX_SILENCE_DETECTED after threshold (with cooldown)
                        if silence_duration >= TX_DEAD_AIR_THRESHOLD_S:
                            if (now_mono - st.last_silence_emit_mono) >= emit_cooldown_s:
                                st.last_silence_emit_mono = now_mono
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_SILENCE_DETECTED,
                                        Severity.ERROR,
                                        now_ts,
                                        message=(
                                            f"Sustained silence on TX for {silence_duration:.1f}s on {port} "
                                            f"(telemetry present but no audio detected)"
                                        ),
                                    )
                                )
                            
                            # Trigger recovery cascade (same as heartbeat missing)
                            # Only if not already in fault state and not in recovery
                            # Also skip if main service is stopped (maintenance mode)
                            min_restart_interval = TX_RECOVERY_CONFIRM_WINDOW_S + 5.0
                            main_service_running = _is_main_service_running()
                            main_service_enabled = _is_main_service_enabled()
                            
                            # ---------------------------------------------------------------------------
                            # Service-level recovery: if service is enabled but not running, start it
                            # ---------------------------------------------------------------------------
                            if (
                                not main_service_running
                                and main_service_enabled
                                and st.service_recovery_attempt_count < SERVICE_MAX_RESTART_ATTEMPTS
                                and (now_mono - st.last_service_recovery_mono) >= SERVICE_RESTART_INTERVAL_S
                            ):
                                st.service_recovery_attempt_count += 1
                                st.last_service_recovery_mono = now_mono
                                
                                if _start_main_service(out_cb):
                                    # Service started, reset TX recovery counters
                                    st.recovery_attempt_count = 0
                                    st.service_recovery_attempt_count = 0
                                    st.silence_started_mono = 0.0
                                    # Give service time to start producing audio
                                    time.sleep(5.0)
                                    continue
                            
                            # TX-level recovery: only if service is running
                            if (
                                not st.in_fault_state
                                and not st.recovery_in_progress
                                and (now_mono - st.last_restart_attempt_mono) >= min_restart_interval
                                and st.recovery_attempt_count < TX_MAX_RESTART_ATTEMPTS
                                and main_service_running  # Skip TX recovery if service is stopped
                            ):
                                st.recovery_attempt_count += 1
                                st.recovery_in_progress = True
                                st.recovery_started_mono = now_mono
                                st.last_restart_attempt_mono = now_mono
                                st.audio_present_streak = 0
                                
                                # Close serial before reset attempt to release the port
                                try:
                                    ser.close()
                                except Exception:
                                    pass
                                
                                reset_ok = _attempt_tx_reset(port, baudrate, st.recovery_attempt_count, out_cb)
                                
                                if not reset_ok:
                                    out_cb(
                                        _mk(
                                            AnomalyCode.TX_RESTART_FAILED,
                                            Severity.ERROR,
                                            time.time(),
                                            message=(
                                                f"TX restart attempt {st.recovery_attempt_count}/{TX_MAX_RESTART_ATTEMPTS} "
                                                f"failed to execute on {port} (silence-triggered)"
                                            ),
                                        )
                                    )
                                    st.recovery_in_progress = False
                                
                                time.sleep(2.0)
                                raise Exception("Restarting serial connection after TX reset attempt (silence)")

                    # ---------------------------------------------------------------------------
                    # Recovery confirmation: track audio presence after restart attempts
                    # ---------------------------------------------------------------------------
                    if st.recovery_in_progress:
                        recovery_elapsed = now_mono - st.recovery_started_mono
                        
                        if audio_present:
                            st.audio_present_streak += 1
                            
                            # Confirmed: N consecutive audio-present samples = recovery success
                            if st.audio_present_streak >= TX_AUDIO_PRESENT_STREAK_REQUIRED:
                                st.recovery_in_progress = False
                                st.recovery_attempt_count = 0  # Reset for future failures
                                st.audio_present_streak = 0
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_RECOVERY,
                                        Severity.INFO,
                                        now_ts,
                                        message=(
                                            f"TX auto-recovery succeeded after DTR reset on {port} "
                                            f"(audio confirmed with {TX_AUDIO_PRESENT_STREAK_REQUIRED} samples)"
                                        ),
                                    )
                                )
                        else:
                            # Reset streak on any non-audio sample
                            st.audio_present_streak = 0
                            
                            # Check if recovery confirmation window expired
                            if recovery_elapsed >= TX_RECOVERY_CONFIRM_WINDOW_S:
                                st.recovery_in_progress = False
                                
                                out_cb(
                                    _mk(
                                        AnomalyCode.TX_RESTART_FAILED,
                                        Severity.ERROR,
                                        now_ts,
                                        message=(
                                            f"TX restart attempt {st.recovery_attempt_count}/{TX_MAX_RESTART_ATTEMPTS} "
                                            f"did not restore audio within {TX_RECOVERY_CONFIRM_WINDOW_S:.0f}s on {port}"
                                        ),
                                    )
                                )
                                
                                # Check if all attempts exhausted -> enter fault state
                                if st.recovery_attempt_count >= TX_MAX_RESTART_ATTEMPTS:
                                    st.in_fault_state = True
                                    out_cb(
                                        _mk(
                                            AnomalyCode.TX_RECOVERY_FAILED,
                                            Severity.CRITICAL,
                                            now_ts,
                                            message=(
                                                f"TX recovery failed after {TX_MAX_RESTART_ATTEMPTS} attempts on {port}. "
                                                f"System in FAULT state. Administrator intervention required. "
                                                f"Web streaming may still be operational."
                                            ),
                                        )
                                    )
                    
                    # If audio naturally recovers while in fault state, clear fault
                    # (allows admin to fix without watchdog restart, or self-healing)
                    # TODO: Maybe require admin acknowledgment before clearing fault state?
                    elif st.in_fault_state and audio_present:
                        st.audio_present_streak += 1
                        if st.audio_present_streak >= TX_AUDIO_PRESENT_STREAK_REQUIRED:
                            st.in_fault_state = False
                            st.recovery_attempt_count = 0
                            st.audio_present_streak = 0
                            out_cb(
                                _mk(
                                    AnomalyCode.TX_RECOVERY,
                                    Severity.INFO,
                                    now_ts,
                                    message=(
                                        f"TX audio naturally recovered on {port} after fault state. "
                                        f"Fault state cleared."
                                    ),
                                )
                            )
                    elif st.in_fault_state:
                        st.audio_present_streak = 0

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
                                # Fault state for dashboard display
                                "in_fault_state": st.in_fault_state,
                                "recovery_in_progress": st.recovery_in_progress,
                                "recovery_attempt_count": st.recovery_attempt_count,
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


