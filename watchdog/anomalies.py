from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class AnomalyCode(str, Enum):
    # Jingle
    JINGLE_END_WITHOUT_START = "JINGLE_END_WITHOUT_START"
    JINGLE_START_WITHOUT_PICK = "JINGLE_START_WITHOUT_PICK"
    JINGLE_NAME_MISMATCH = "JINGLE_NAME_MISMATCH"
    JINGLE_FAILED_TO_LOAD = "JINGLE_FAILED_TO_LOAD"
    JINGLE_PICK_NO_START = "JINGLE_PICK_NO_START"
    JINGLE_START_NO_END = "JINGLE_START_NO_END"

    # Commercial
    COMM_END_WITHOUT_START = "COMM_END_WITHOUT_START"
    COMM_START_WITHOUT_PICK = "COMM_START_WITHOUT_PICK"
    COMM_NAME_MISMATCH = "COMM_NAME_MISMATCH"
    COMM_FAILED_TO_LOAD = "COMM_FAILED_TO_LOAD"
    COMM_PICK_NO_START = "COMM_PICK_NO_START"
    COMM_START_NO_END = "COMM_START_NO_END"

    # Playback pipeline
    DAC_RING_OVERFLOW = "DAC_RING_OVERFLOW"
    DAC_RING_THROTTLE = "DAC_RING_THROTTLE"
    DAC_RING_UNDERRUN = "DAC_RING_UNDERRUN"
    RADIO_DEAD_AIR = "RADIO_DEAD_AIR"

    # Extended playback anomalies (for comprehensive testing)
    PLAYBACK_STALL = "PLAYBACK_STALL"  # Audio position not advancing
    VOLUME_SILENT = "VOLUME_SILENT"  # Output samples all zero
    OVERLAY_TIMING_ERROR = "OVERLAY_TIMING_ERROR"  # Intro/song timing drift

    # Asset integrity
    FILE_ZERO_BYTES = "FILE_ZERO_BYTES"
    FILE_TOO_SMALL = "FILE_TOO_SMALL"
    FILE_BAD_HEADER = "FILE_BAD_HEADER"
    ASSETS_H_HAS_ZERO_SIZE = "ASSETS_H_HAS_ZERO_SIZE"
    ASSETS_BLOB_RANGE_ERROR = "ASSETS_BLOB_RANGE_ERROR"

    # Transmitter (Arduino/Si4713)
    TX_HEARTBEAT_MISSING = "TX_HEARTBEAT_MISSING"
    TX_SKETCH_RESTART = "TX_SKETCH_RESTART"
    TX_RECOVERY = "TX_RECOVERY"
    TX_SERIAL_ERROR = "TX_SERIAL_ERROR"
    TX_OVERMODULATION = "TX_OVERMODULATION"  # Audio level too high

    # Service log passthrough
    AIRCHECK_LOG_ERROR = "AIRCHECK_LOG_ERROR"

    # Monitored unit lifecycle
    AIRCHECK_UNIT_STARTED_UNEXPECTED = "AIRCHECK_UNIT_STARTED_UNEXPECTED"

    # Watchdog recovery actions
    AIRCHECK_AUTO_RESTARTED = "AIRCHECK_AUTO_RESTARTED"
    AIRCHECK_AUTO_RESTART_FAILED = "AIRCHECK_AUTO_RESTART_FAILED"
    AIRCHECK_AUTO_RESTART_SUPPRESSED = "AIRCHECK_AUTO_RESTART_SUPPRESSED"

    # Test-injected anomalies (for E2E testing)
    TEST_INJECTED = "TEST_INJECTED"  # Generic test anomaly


@dataclass(frozen=True)
class Anomaly:
    code: AnomalyCode
    severity: Severity
    ts: float
    message: str
    name: Optional[str] = None
    raw: Optional[str] = None


