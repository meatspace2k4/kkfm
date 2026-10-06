"""
E2E Test Harness for KKFM Watchdog System

Provides infrastructure for running end-to-end tests with:
- Mock services (systemctl, serial, etc.)
- Temporary directory management
- Event injection and verification
- Dashboard output capture
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from unittest.mock import MagicMock, patch

from watchdog.anomalies import Anomaly, AnomalyCode, Severity
from watchdog.detector import Detector
from watchdog.events import Event, EventType
from watchdog.log_parser import parse_event
from watchdog.reporting import Reporter


@dataclass
class MockServiceState:
    """Simulated systemd service state."""
    name: str
    active_state: str = "active"
    sub_state: str = "running"
    active_enter_timestamp: str = ""
    restart_count: int = 0


class MockSystemctl:
    """Mock systemctl for testing auto-restart logic without real systemd."""

    def __init__(self) -> None:
        self.services: dict[str, MockServiceState] = {
            "acnh-aircheck.service": MockServiceState("acnh-aircheck.service"),
            "acnh-aircheck-watchdog.service": MockServiceState("acnh-aircheck-watchdog.service"),
            "acnh-aircheck-tx-watchdog.service": MockServiceState("acnh-aircheck-tx-watchdog.service"),
        }
        self.restart_calls: list[tuple[str, float]] = []
        self.restart_results: dict[str, int] = {}  # service -> return code
        self._lock = threading.Lock()

    def restart(self, unit: str) -> int:
        """Simulate systemctl restart."""
        with self._lock:
            self.restart_calls.append((unit, time.time()))
            if unit in self.services:
                self.services[unit].restart_count += 1
                self.services[unit].active_enter_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            return self.restart_results.get(unit, 0)

    def show(self, unit: str) -> str:
        """Simulate systemctl show output."""
        with self._lock:
            svc = self.services.get(unit)
            if not svc:
                return ""
            return (
                f"ActiveState={svc.active_state}\n"
                f"SubState={svc.sub_state}\n"
                f"ActiveEnterTimestamp={svc.active_enter_timestamp}\n"
            )

    def set_fail_restart(self, unit: str, return_code: int = 1) -> None:
        """Configure restart to fail for a unit."""
        self.restart_results[unit] = return_code

    def set_service_state(self, unit: str, active: str, sub: str) -> None:
        """Update service state."""
        with self._lock:
            if unit in self.services:
                self.services[unit].active_state = active
                self.services[unit].sub_state = sub


class MockSerial:
    """Mock serial port for testing TX monitor without real hardware."""

    def __init__(self) -> None:
        self.port: Optional[str] = None
        self.baudrate: int = 115200
        self.timeout: float = 0.25
        self.dtr: bool = False
        self.rts: bool = False
        self._buffer: list[bytes] = []
        self._lock = threading.Lock()
        self._closed: bool = True
        self._open_should_fail: bool = False
        self._open_exception: Optional[Exception] = None

    def open(self) -> None:
        if self._open_should_fail:
            raise self._open_exception or IOError("Mock serial open failed")
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def readline(self) -> bytes:
        with self._lock:
            if self._buffer:
                return self._buffer.pop(0)
        time.sleep(0.01)  # Simulate timeout
        return b""

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._buffer.clear()

    def inject_line(self, line: str) -> None:
        """Inject a line of data as if received from serial."""
        with self._lock:
            self._buffer.append((line + "\n").encode("utf-8"))

    def inject_telemetry(
        self,
        ms: int = 1000,
        freq: float = 87.7,
        noise: int = -100,
        inlevel: int = 10,
        over: int = 0,
    ) -> None:
        """Inject a valid telemetry line."""
        self.inject_line(f"{ms} {freq} {noise} {inlevel} {over}")

    def inject_banner(self, freq: float = 87.7) -> None:
        """Inject TX_FREQ banner (startup)."""
        self.inject_line(f"TX_FREQ={freq}")

    def inject_recovery(self, reason: str = "I2C timeout") -> None:
        """Inject recovery event."""
        self.inject_line(f"RECOVER: {reason}")

    def set_open_fail(self, exc: Optional[Exception] = None) -> None:
        """Configure open() to fail."""
        self._open_should_fail = True
        self._open_exception = exc

    def __enter__(self) -> "MockSerial":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


@dataclass
class CapturedAnomaly:
    """An anomaly captured during test execution."""
    anomaly: Anomaly
    captured_at: float = field(default_factory=time.time)


class E2ETestHarness:
    """
    End-to-end test harness for the KKFM watchdog system.

    Provides:
    - Temporary directory for test artifacts
    - Mock systemctl for auto-restart testing
    - Mock serial for TX monitor testing
    - Event injection for triggering anomalies
    - Anomaly capture and verification
    """

    def __init__(self, *, auto_cleanup: bool = True) -> None:
        self.auto_cleanup = auto_cleanup
        self._temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        self._out_dir: Optional[Path] = None
        self.mock_systemctl = MockSystemctl()
        self.mock_serial = MockSerial()
        self.captured_anomalies: list[CapturedAnomaly] = []
        self.detector = Detector()
        self._reporter: Optional[Reporter] = None
        self._log_lines: list[str] = []
        self._lock = threading.Lock()

    def __enter__(self) -> "E2ETestHarness":
        self.setup()
        return self

    def __exit__(self, *args: Any) -> None:
        self.teardown()

    def setup(self) -> None:
        """Initialize the test harness."""
        self._temp_dir = tempfile.TemporaryDirectory(prefix="kkfm_e2e_")
        self._out_dir = Path(self._temp_dir.name) / "out"
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._reporter = Reporter(
            out_dir=str(self._out_dir),
            flags_dir=str(self._out_dir / "flags"),
        )

    def teardown(self) -> None:
        """Clean up test resources."""
        if self.auto_cleanup and self._temp_dir:
            self._temp_dir.cleanup()
            self._temp_dir = None

    @property
    def out_dir(self) -> Path:
        if self._out_dir is None:
            raise RuntimeError("Harness not set up; call setup() or use as context manager")
        return self._out_dir

    @property
    def anomalies_jsonl(self) -> Path:
        return self.out_dir / "anomalies.jsonl"

    @property
    def flags_dir(self) -> Path:
        return self.out_dir / "flags"

    def inject_log_line(self, line: str, default_ts: Optional[float] = None) -> list[Anomaly]:
        """
        Inject a log line and process it through the detector.

        Returns any anomalies produced.
        """
        with self._lock:
            self._log_lines.append(line)

        ts = default_ts if default_ts is not None else time.time()
        event = parse_event(line, default_ts=ts)
        if event is None:
            return []

        anomalies = self.detector.feed(event)
        for a in anomalies:
            self._capture_anomaly(a)
        return list(anomalies)

    def inject_event(self, event: Event) -> list[Anomaly]:
        """Inject an event directly into the detector."""
        anomalies = self.detector.feed(event)
        for a in anomalies:
            self._capture_anomaly(a)
        return list(anomalies)

    def poll_detector(self, now_ts: Optional[float] = None) -> list[Anomaly]:
        """Poll the detector for timeout-based anomalies."""
        ts = now_ts if now_ts is not None else time.time()
        anomalies = self.detector.poll(ts)
        for a in anomalies:
            self._capture_anomaly(a)
        return list(anomalies)

    def _capture_anomaly(self, anomaly: Anomaly) -> None:
        """Capture an anomaly for later verification."""
        with self._lock:
            self.captured_anomalies.append(CapturedAnomaly(anomaly=anomaly))
        if self._reporter:
            self._reporter.emit(anomaly)

    def get_anomalies_by_code(self, code: AnomalyCode) -> list[Anomaly]:
        """Get all captured anomalies with a specific code."""
        with self._lock:
            return [ca.anomaly for ca in self.captured_anomalies if ca.anomaly.code == code]

    def has_anomaly(self, code: AnomalyCode) -> bool:
        """Check if an anomaly with the given code was captured."""
        return len(self.get_anomalies_by_code(code)) > 0

    def clear_anomalies(self) -> None:
        """Clear all captured anomalies."""
        with self._lock:
            self.captured_anomalies.clear()

    def read_anomalies_jsonl(self) -> list[dict[str, Any]]:
        """Read all anomalies from the JSONL file."""
        if not self.anomalies_jsonl.exists():
            return []
        lines = self.anomalies_jsonl.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def get_flag_files(self) -> list[str]:
        """Get list of flag files created."""
        if not self.flags_dir.exists():
            return []
        return [f.name for f in self.flags_dir.iterdir() if f.is_file()]

    def create_mock_log_file(self, lines: list[str]) -> Path:
        """Create a temporary log file with the given lines."""
        log_file = self.out_dir / "test.log"
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log_file

    def simulate_restart(self, unit: str = "acnh-aircheck.service") -> int:
        """Simulate a service restart via mock systemctl."""
        return self.mock_systemctl.restart(unit)

    @contextmanager
    def patch_subprocess_for_systemctl(self) -> Iterator[None]:
        """
        Context manager that patches subprocess.run to use mock systemctl.

        Use this when testing code that calls systemctl via subprocess.
        """
        original_run = subprocess.run

        def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd and cmd[0] == "systemctl":
                if len(cmd) >= 3 and cmd[1] == "restart":
                    unit = cmd[2]
                    rc = self.mock_systemctl.restart(unit)
                    return subprocess.CompletedProcess(cmd, rc, "", "")
                elif len(cmd) >= 3 and cmd[1] == "show":
                    unit = cmd[2]
                    output = self.mock_systemctl.show(unit)
                    return subprocess.CompletedProcess(cmd, 0, output, "")
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            yield

    def write_log_status(self, status: dict[str, Any]) -> Path:
        """Write a log_status.json file."""
        path = self.out_dir / "log_status.json"
        path.write_text(json.dumps(status), encoding="utf-8")
        return path

    def write_tx_status(self, status: dict[str, Any]) -> Path:
        """Write a tx_status.json file."""
        path = self.out_dir / "tx_status.json"
        path.write_text(json.dumps(status), encoding="utf-8")
        return path
