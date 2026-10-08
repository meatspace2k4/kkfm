"""
Event Injectors and Anomaly Triggers for E2E Testing

Provides pre-built triggers for each anomaly type that can be injected
into the test harness to verify detection and reporting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

from watchdog.anomalies import AnomalyCode
from watchdog.events import Event, EventType


class AnomalyTrigger(Enum):
    """
    Pre-defined triggers for each anomaly type.

    Each trigger represents a sequence of events or conditions that
    should produce a specific anomaly.
    """
    # Jingle anomalies
    JINGLE_END_WITHOUT_START = auto()
    JINGLE_NAME_MISMATCH = auto()
    JINGLE_FAILED_TO_LOAD = auto()
    JINGLE_START_NO_END = auto()

    # Commercial anomalies
    COMM_END_WITHOUT_START = auto()
    COMM_NAME_MISMATCH = auto()
    COMM_FAILED_TO_LOAD = auto()
    COMM_START_NO_END = auto()

    # DAC/Audio anomalies
    DAC_RING_OVERFLOW = auto()
    DAC_RING_THROTTLE = auto()
    DAC_RING_UNDERRUN = auto()
    RADIO_DEAD_AIR = auto()

    # TX anomalies
    TX_HEARTBEAT_MISSING = auto()
    TX_SKETCH_RESTART = auto()
    TX_RECOVERY = auto()
    TX_SERIAL_ERROR = auto()

    # Service lifecycle
    UNIT_RESTART_UNEXPECTED = auto()


@dataclass
class TriggerResult:
    """Result of executing an anomaly trigger."""
    trigger: AnomalyTrigger
    expected_code: AnomalyCode
    events_injected: list[Event] = field(default_factory=list)
    log_lines_injected: list[str] = field(default_factory=list)
    poll_required: bool = False


class EventInjector:
    """
    Injects events to trigger specific anomalies for testing.

    Usage:
        injector = EventInjector()
        result = injector.trigger(AnomalyTrigger.DAC_RING_OVERFLOW)
        for event in result.events_injected:
            harness.inject_event(event)
        if result.poll_required:
            harness.poll_detector()
    """

    def __init__(self, base_ts: Optional[float] = None) -> None:
        self.base_ts = base_ts or time.time()
        self._ts_offset = 0.0

    def _next_ts(self, delta: float = 0.1) -> float:
        """Get next timestamp, advancing by delta."""
        self._ts_offset += delta
        return self.base_ts + self._ts_offset

    def trigger(self, trigger: AnomalyTrigger) -> TriggerResult:
        """Execute a trigger and return the events to inject."""
        method_name = f"_trigger_{trigger.name.lower()}"
        method = getattr(self, method_name, None)
        if method is None:
            raise NotImplementedError(f"Trigger {trigger.name} not implemented")
        return method()

    # -------------------------------------------------------------------------
    # Jingle Triggers
    # -------------------------------------------------------------------------

    def _trigger_jingle_end_without_start(self) -> TriggerResult:
        """Trigger JINGLE_END_WITHOUT_START anomaly.
        
        Note: This requires the detector to be past its orphan_end_grace_s window.
        We inject a baseline event first to set _startup_ts, then wait past the grace period.
        """
        base_ts = self._next_ts()
        events = [
            # Baseline event to set startup_ts
            Event(EventType.NOW_PLAYING, ts=base_ts, raw="Now Playing: X", name="X"),
            # End without start, well past the grace period (default 10s)
            Event(EventType.JINGLE_END, ts=base_ts + 15.0, raw="end", name="TestJingle"),
        ]
        # Update internal offset
        self._ts_offset += 15.0
        return TriggerResult(
            trigger=AnomalyTrigger.JINGLE_END_WITHOUT_START,
            expected_code=AnomalyCode.JINGLE_END_WITHOUT_START,
            events_injected=events,
        )

    def _trigger_jingle_name_mismatch(self) -> TriggerResult:
        """Trigger JINGLE_NAME_MISMATCH anomaly."""
        events = [
            Event(EventType.JINGLE_PICKED, ts=self._next_ts(), raw="pick", name="JingleA"),
            Event(EventType.JINGLE_START, ts=self._next_ts(), raw="start", name="JingleB"),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.JINGLE_NAME_MISMATCH,
            expected_code=AnomalyCode.JINGLE_NAME_MISMATCH,
            events_injected=events,
        )

    def _trigger_jingle_failed_to_load(self) -> TriggerResult:
        """Trigger JINGLE_FAILED_TO_LOAD anomaly."""
        events = [
            Event(EventType.JINGLE_FAILED, ts=self._next_ts(), raw="ERROR: Jingle failed to load", name="BadJingle"),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.JINGLE_FAILED_TO_LOAD,
            expected_code=AnomalyCode.JINGLE_FAILED_TO_LOAD,
            events_injected=events,
        )

    def _trigger_jingle_start_no_end(self) -> TriggerResult:
        """Trigger JINGLE_START_NO_END anomaly (requires poll with timeout)."""
        events = [
            Event(EventType.JINGLE_PICKED, ts=self._next_ts(), raw="pick", name="StuckJingle"),
            Event(EventType.JINGLE_START, ts=self._next_ts(), raw="start", name="StuckJingle"),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.JINGLE_START_NO_END,
            expected_code=AnomalyCode.JINGLE_START_NO_END,
            events_injected=events,
            poll_required=True,
        )

    # -------------------------------------------------------------------------
    # Commercial Triggers
    # -------------------------------------------------------------------------

    def _trigger_comm_end_without_start(self) -> TriggerResult:
        """Trigger COMM_END_WITHOUT_START anomaly.
        
        Note: This requires the detector to be past its orphan_end_grace_s window.
        """
        base_ts = self._next_ts()
        events = [
            # Baseline event to set startup_ts
            Event(EventType.NOW_PLAYING, ts=base_ts, raw="Now Playing: X", name="X"),
            # End without start, well past the grace period (default 10s)
            Event(EventType.COMM_END, ts=base_ts + 15.0, raw="end", name="TestComm"),
        ]
        # Update internal offset
        self._ts_offset += 15.0
        return TriggerResult(
            trigger=AnomalyTrigger.COMM_END_WITHOUT_START,
            expected_code=AnomalyCode.COMM_END_WITHOUT_START,
            events_injected=events,
        )

    def _trigger_comm_name_mismatch(self) -> TriggerResult:
        """Trigger COMM_NAME_MISMATCH anomaly."""
        events = [
            Event(EventType.COMM_PICKED, ts=self._next_ts(), raw="pick", name="CommA"),
            Event(EventType.COMM_START, ts=self._next_ts(), raw="start", name="CommB"),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.COMM_NAME_MISMATCH,
            expected_code=AnomalyCode.COMM_NAME_MISMATCH,
            events_injected=events,
        )

    def _trigger_comm_failed_to_load(self) -> TriggerResult:
        """Trigger COMM_FAILED_TO_LOAD anomaly."""
        events = [
            Event(EventType.COMM_FAILED, ts=self._next_ts(), raw="ERROR: Commercial failed to load", name="BadComm"),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.COMM_FAILED_TO_LOAD,
            expected_code=AnomalyCode.COMM_FAILED_TO_LOAD,
            events_injected=events,
        )

    def _trigger_comm_start_no_end(self) -> TriggerResult:
        """Trigger COMM_START_NO_END anomaly (requires poll with timeout)."""
        events = [
            Event(EventType.COMM_PICKED, ts=self._next_ts(), raw="pick", name="StuckComm"),
            Event(EventType.COMM_START, ts=self._next_ts(), raw="start", name="StuckComm"),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.COMM_START_NO_END,
            expected_code=AnomalyCode.COMM_START_NO_END,
            events_injected=events,
            poll_required=True,
        )

    # -------------------------------------------------------------------------
    # DAC/Audio Triggers
    # -------------------------------------------------------------------------

    def _trigger_dac_ring_overflow(self) -> TriggerResult:
        """Trigger DAC_RING_OVERFLOW anomaly."""
        events = [
            Event(EventType.DAC_OVERFLOW, ts=self._next_ts(), raw="[DAC] Ring buffer overflow", name=None),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.DAC_RING_OVERFLOW,
            expected_code=AnomalyCode.DAC_RING_OVERFLOW,
            events_injected=events,
        )

    def _trigger_dac_ring_throttle(self) -> TriggerResult:
        """Trigger DAC_RING_THROTTLE anomaly."""
        events = [
            Event(EventType.DAC_THROTTLE, ts=self._next_ts(), raw="[DAC] Ring buffer full; throttling", name=None),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.DAC_RING_THROTTLE,
            expected_code=AnomalyCode.DAC_RING_THROTTLE,
            events_injected=events,
        )

    def _trigger_dac_ring_underrun(self) -> TriggerResult:
        """Trigger DAC_RING_UNDERRUN anomaly (the detector needs 3 underruns within 10 s)."""
        events = [
            Event(EventType.DAC_UNDERRUN, ts=self._next_ts(), raw="[DAC] Ring buffer underrun; outputting silence", name=None)
            for _ in range(3)
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.DAC_RING_UNDERRUN,
            expected_code=AnomalyCode.DAC_RING_UNDERRUN,
            events_injected=events,
        )

    def _trigger_radio_dead_air(self) -> TriggerResult:
        """Trigger RADIO_DEAD_AIR anomaly (requires poll with timeout)."""
        # Dead air is triggered by lack of events, so we just return empty
        # and expect the detector to be polled after timeout
        return TriggerResult(
            trigger=AnomalyTrigger.RADIO_DEAD_AIR,
            expected_code=AnomalyCode.RADIO_DEAD_AIR,
            events_injected=[],
            poll_required=True,
        )

    # -------------------------------------------------------------------------
    # TX Triggers (these generate log lines for serial monitor)
    # -------------------------------------------------------------------------

    def _trigger_tx_heartbeat_missing(self) -> TriggerResult:
        """Trigger TX_HEARTBEAT_MISSING anomaly (requires timeout)."""
        return TriggerResult(
            trigger=AnomalyTrigger.TX_HEARTBEAT_MISSING,
            expected_code=AnomalyCode.TX_HEARTBEAT_MISSING,
            log_lines_injected=[],  # No lines = missing heartbeat
            poll_required=True,
        )

    def _trigger_tx_sketch_restart(self) -> TriggerResult:
        """Trigger TX_SKETCH_RESTART anomaly."""
        return TriggerResult(
            trigger=AnomalyTrigger.TX_SKETCH_RESTART,
            expected_code=AnomalyCode.TX_SKETCH_RESTART,
            log_lines_injected=[
                "TX_FREQ=87.7",  # First banner (baseline)
                "1000 87.7 -100 10 0",  # Normal telemetry
                "TX_FREQ=87.7",  # Second banner (restart)
            ],
        )

    def _trigger_tx_recovery(self) -> TriggerResult:
        """Trigger TX_RECOVERY anomaly."""
        return TriggerResult(
            trigger=AnomalyTrigger.TX_RECOVERY,
            expected_code=AnomalyCode.TX_RECOVERY,
            log_lines_injected=[
                "RECOVER: I2C timeout, re-initializing",
            ],
        )

    def _trigger_tx_serial_error(self) -> TriggerResult:
        """Trigger TX_SERIAL_ERROR anomaly."""
        return TriggerResult(
            trigger=AnomalyTrigger.TX_SERIAL_ERROR,
            expected_code=AnomalyCode.TX_SERIAL_ERROR,
            log_lines_injected=[],  # Serial error is triggered by exception
        )

    # -------------------------------------------------------------------------
    # Service Lifecycle Triggers
    # -------------------------------------------------------------------------

    def _trigger_unit_restart_unexpected(self) -> TriggerResult:
        """Trigger AIRCHECK_UNIT_STARTED_UNEXPECTED anomaly."""
        events = [
            # First start is baseline
            Event(EventType.UNIT_STARTED, ts=self._next_ts(), raw="Started KKFM Aircheck."),
            # Second start without safe shutdown is unexpected
            Event(EventType.UNIT_STARTED, ts=self._next_ts(), raw="Started KKFM Aircheck."),
        ]
        return TriggerResult(
            trigger=AnomalyTrigger.UNIT_RESTART_UNEXPECTED,
            expected_code=AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED,
            events_injected=events,
        )


# -------------------------------------------------------------------------
# Log Line Templates for Injection
# -------------------------------------------------------------------------

LOG_TEMPLATES = {
    "jingle_picked": "[RADIO] Jingle picked: {name}.bwav (size={size})",
    "jingle_start": "[RADIO] Jingle start: {name}.bwav",
    "jingle_end": "[RADIO] Jingle end: {name}.bwav",
    "jingle_failed": "ERROR: [RADIO] Jingle failed to load: {name}.bwav",
    "comm_picked": "[RADIO] Commercial picked: {name}.bwav (size={size})",
    "comm_start": "[RADIO] Commercial start: {name}.bwav",
    "comm_end": "[RADIO] Commercial end: {name}.bwav",
    "comm_failed": "ERROR: [RADIO] Commercial failed to load: {name}.bwav",
    "dac_overflow": "[DAC] Ring buffer overflow; dropping samples.",
    "dac_throttle": "ERROR: [DAC] Ring buffer full; throttling producer to avoid dropping samples.",
    "dac_underrun": "[DAC] Ring buffer underrun; outputting silence (stutter).",
    "now_playing": "[RADIO] Now Playing: {name}",
    "unit_started": "Started KKFM Aircheck Service.",
    "safe_shutdown": "[SYSTEM] Received shutdown signal, stopping gracefully...",
}


def format_log_line(template_name: str, **kwargs) -> str:
    """Format a log line from a template."""
    template = LOG_TEMPLATES.get(template_name)
    if template is None:
        raise ValueError(f"Unknown template: {template_name}")
    # Add defaults for common fields
    kwargs.setdefault("size", 12345)
    return template.format(**kwargs)
