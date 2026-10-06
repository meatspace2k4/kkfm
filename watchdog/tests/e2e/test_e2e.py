"""
End-to-End Tests for KKFM Watchdog System

These tests verify the complete anomaly detection pipeline:
1. Event injection -> Detector -> Reporter -> JSONL + Flags
2. Dashboard consumption of status files
3. Auto-restart logic with mock systemctl
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from watchdog.anomalies import AnomalyCode, Severity
from watchdog.detector import Detector
from watchdog.events import Event, EventType
from watchdog.tests.e2e.harness import E2ETestHarness, MockSerial, MockSystemctl
from watchdog.tests.e2e.injectors import (
    AnomalyTrigger,
    EventInjector,
    format_log_line,
)


class TestE2EAnomalyDetection(unittest.TestCase):
    """Test that anomaly triggers produce expected anomalies end-to-end."""

    def test_all_triggers_produce_expected_anomalies(self) -> None:
        """Verify each trigger produces its expected anomaly code."""
        # Skip timeout-based triggers that need special handling
        skip_triggers = {
            AnomalyTrigger.JINGLE_START_NO_END,
            AnomalyTrigger.COMM_START_NO_END,
            AnomalyTrigger.RADIO_DEAD_AIR,
            AnomalyTrigger.TX_HEARTBEAT_MISSING,
            AnomalyTrigger.TX_SKETCH_RESTART,
            AnomalyTrigger.TX_RECOVERY,
            AnomalyTrigger.TX_SERIAL_ERROR,
        }

        with E2ETestHarness() as harness:
            injector = EventInjector()

            for trigger in AnomalyTrigger:
                if trigger in skip_triggers:
                    continue

                with self.subTest(trigger=trigger.name):
                    harness.clear_anomalies()
                    harness.detector = Detector()  # Fresh detector

                    result = injector.trigger(trigger)

                    for event in result.events_injected:
                        harness.inject_event(event)

                    self.assertTrue(
                        harness.has_anomaly(result.expected_code),
                        f"Trigger {trigger.name} should produce {result.expected_code.name}",
                    )

    def test_jingle_lifecycle_complete_no_anomaly(self) -> None:
        """A complete jingle lifecycle should not produce anomalies."""
        with E2ETestHarness() as harness:
            ts = time.time()
            harness.inject_event(Event(EventType.JINGLE_PICKED, ts=ts, raw="pick", name="TestJingle"))
            harness.inject_event(Event(EventType.JINGLE_START, ts=ts + 0.1, raw="start", name="TestJingle"))
            harness.inject_event(Event(EventType.JINGLE_END, ts=ts + 5.0, raw="end", name="TestJingle"))

            self.assertEqual(len(harness.captured_anomalies), 0)

    def test_commercial_lifecycle_complete_no_anomaly(self) -> None:
        """A complete commercial lifecycle should not produce anomalies."""
        with E2ETestHarness() as harness:
            ts = time.time()
            harness.inject_event(Event(EventType.COMM_PICKED, ts=ts, raw="pick", name="TestComm"))
            harness.inject_event(Event(EventType.COMM_START, ts=ts + 0.1, raw="start", name="TestComm"))
            harness.inject_event(Event(EventType.COMM_END, ts=ts + 30.0, raw="end", name="TestComm"))

            self.assertEqual(len(harness.captured_anomalies), 0)


class TestE2EReporting(unittest.TestCase):
    """Test that anomalies are properly written to JSONL and flag files."""

    def test_anomaly_written_to_jsonl(self) -> None:
        """Verify anomalies are persisted to JSONL file."""
        with E2ETestHarness() as harness:
            injector = EventInjector()
            result = injector.trigger(AnomalyTrigger.DAC_RING_OVERFLOW)

            for event in result.events_injected:
                harness.inject_event(event)

            # Read back from JSONL
            records = harness.read_anomalies_jsonl()
            self.assertGreaterEqual(len(records), 1)

            codes = {r.get("code") for r in records}
            self.assertIn("DAC_RING_OVERFLOW", codes)

    def test_flag_file_created_for_anomaly(self) -> None:
        """Verify flag files are created for anomalies."""
        with E2ETestHarness() as harness:
            injector = EventInjector()
            result = injector.trigger(AnomalyTrigger.DAC_RING_OVERFLOW)

            for event in result.events_injected:
                harness.inject_event(event)

            flag_files = harness.get_flag_files()
            self.assertGreaterEqual(len(flag_files), 1)

    def test_multiple_anomalies_accumulated(self) -> None:
        """Verify multiple anomalies are accumulated in JSONL."""
        with E2ETestHarness() as harness:
            injector = EventInjector()

            # Inject multiple different anomalies
            triggers = [
                AnomalyTrigger.DAC_RING_OVERFLOW,
                AnomalyTrigger.DAC_RING_THROTTLE,
                AnomalyTrigger.DAC_RING_UNDERRUN,
            ]

            for trigger in triggers:
                result = injector.trigger(trigger)
                for event in result.events_injected:
                    harness.inject_event(event)

            records = harness.read_anomalies_jsonl()
            codes = {r.get("code") for r in records}

            self.assertIn("DAC_RING_OVERFLOW", codes)
            self.assertIn("DAC_RING_THROTTLE", codes)
            self.assertIn("DAC_RING_UNDERRUN", codes)


class TestE2EAutoRestart(unittest.TestCase):
    """Test auto-restart logic with mock systemctl."""

    def test_mock_systemctl_tracks_restarts(self) -> None:
        """Verify MockSystemctl tracks restart calls."""
        mock = MockSystemctl()
        rc = mock.restart("acnh-aircheck.service")
        self.assertEqual(rc, 0)
        self.assertEqual(len(mock.restart_calls), 1)
        self.assertEqual(mock.restart_calls[0][0], "acnh-aircheck.service")
        self.assertEqual(mock.services["acnh-aircheck.service"].restart_count, 1)

    def test_mock_systemctl_fail_restart(self) -> None:
        """Verify MockSystemctl can simulate restart failure."""
        mock = MockSystemctl()
        mock.set_fail_restart("acnh-aircheck.service", return_code=1)
        rc = mock.restart("acnh-aircheck.service")
        self.assertEqual(rc, 1)

    def test_mock_systemctl_show_output(self) -> None:
        """Verify MockSystemctl generates proper show output."""
        mock = MockSystemctl()
        output = mock.show("acnh-aircheck.service")
        self.assertIn("ActiveState=active", output)
        self.assertIn("SubState=running", output)


class TestE2EMockSerial(unittest.TestCase):
    """Test MockSerial for TX monitor testing."""

    def test_mock_serial_inject_telemetry(self) -> None:
        """Verify MockSerial can inject telemetry lines."""
        mock = MockSerial()
        mock.open()

        mock.inject_telemetry(ms=5000, freq=87.7, noise=-100, inlevel=15, over=0)
        line = mock.readline()
        self.assertEqual(line.decode("utf-8").strip(), "5000 87.7 -100 15 0")

    def test_mock_serial_inject_banner(self) -> None:
        """Verify MockSerial can inject TX_FREQ banner."""
        mock = MockSerial()
        mock.open()

        mock.inject_banner(freq=88.1)
        line = mock.readline()
        self.assertEqual(line.decode("utf-8").strip(), "TX_FREQ=88.1")

    def test_mock_serial_inject_recovery(self) -> None:
        """Verify MockSerial can inject recovery events."""
        mock = MockSerial()
        mock.open()

        mock.inject_recovery("I2C timeout")
        line = mock.readline()
        self.assertIn("RECOVER: I2C timeout", line.decode("utf-8"))

    def test_mock_serial_open_fail(self) -> None:
        """Verify MockSerial can simulate open failure."""
        mock = MockSerial()
        mock.set_open_fail(IOError("Device not found"))

        with self.assertRaises(IOError):
            mock.open()


class TestE2ELogLineInjection(unittest.TestCase):
    """Test log line injection through the full pipeline."""

    def test_log_line_parsed_and_detected(self) -> None:
        """Verify log lines are parsed and fed to detector."""
        with E2ETestHarness() as harness:
            # Inject a DAC throttle log line
            line = format_log_line("dac_throttle")
            harness.inject_log_line(f"2025-01-01T00:00:00Z host acnh-aircheck[123]: {line}")

            self.assertTrue(harness.has_anomaly(AnomalyCode.DAC_RING_THROTTLE))

    def test_jingle_mismatch_via_log_lines(self) -> None:
        """Verify jingle name mismatch detected from log lines."""
        with E2ETestHarness() as harness:
            ts = "2025-01-01T00:00:0{}Z"
            lines = [
                f"{ts.format(0)} host acnh-aircheck[123]: {format_log_line('jingle_picked', name='JingleA')}",
                f"{ts.format(1)} host acnh-aircheck[123]: [RADIO] Jingle start: JingleB.bwav",
            ]

            for line in lines:
                harness.inject_log_line(line)

            self.assertTrue(harness.has_anomaly(AnomalyCode.JINGLE_NAME_MISMATCH))


class TestE2EStatusFiles(unittest.TestCase):
    """Test status file generation and consumption."""

    def test_log_status_file_written(self) -> None:
        """Verify log_status.json is written with expected fields."""
        with E2ETestHarness() as harness:
            status = {
                "ts": time.time(),
                "epoch": "test-epoch",
                "last_bwav": {"name": "Test.bwav", "src": "FILE"},
                "last_event": {"etype": "now_playing", "level": "INFO"},
            }
            path = harness.write_log_status(status)

            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["epoch"], "test-epoch")
            self.assertEqual(loaded["last_bwav"]["name"], "Test.bwav")

    def test_tx_status_file_written(self) -> None:
        """Verify tx_status.json is written with expected fields."""
        with E2ETestHarness() as harness:
            status = {
                "ts": time.time(),
                "epoch": "tx-test-epoch",
                "port": "/dev/ttyUSB0",
                "freq_mhz": 87.7,
                "audio_present": True,
            }
            path = harness.write_tx_status(status)

            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["port"], "/dev/ttyUSB0")
            self.assertEqual(loaded["freq_mhz"], 87.7)


class TestE2ETimeoutAnomalies(unittest.TestCase):
    """Test timeout-based anomalies that require polling."""

    def test_jingle_start_no_end_timeout(self) -> None:
        """Verify JINGLE_START_NO_END detected after timeout."""
        with E2ETestHarness() as harness:
            # Use a detector with short timeout for testing
            harness.detector = Detector(end_timeout_s=0.1)

            ts = time.time()
            harness.inject_event(Event(EventType.JINGLE_PICKED, ts=ts, raw="pick", name="StuckJingle"))
            harness.inject_event(Event(EventType.JINGLE_START, ts=ts, raw="start", name="StuckJingle"))

            # Wait for timeout
            time.sleep(0.15)

            # Poll should detect the timeout
            harness.poll_detector()

            self.assertTrue(harness.has_anomaly(AnomalyCode.JINGLE_START_NO_END))

    def test_unit_restart_unexpected(self) -> None:
        """Verify unexpected unit restart is detected."""
        with E2ETestHarness() as harness:
            ts = time.time()

            # First start is baseline
            anomalies1 = harness.inject_event(Event(EventType.UNIT_STARTED, ts=ts, raw="Started"))
            self.assertFalse(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in anomalies1))

            # Second start without safe shutdown is unexpected
            anomalies2 = harness.inject_event(Event(EventType.UNIT_STARTED, ts=ts + 1, raw="Started"))
            self.assertTrue(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in anomalies2))

    def test_safe_shutdown_allows_restart(self) -> None:
        """Verify safe shutdown prevents restart anomaly."""
        with E2ETestHarness() as harness:
            ts = time.time()

            # First start
            harness.inject_event(Event(EventType.UNIT_STARTED, ts=ts, raw="Started"))

            # Safe shutdown
            harness.inject_event(Event(EventType.SAFE_SHUTDOWN, ts=ts + 1, raw="shutdown", name="maintenance"))

            # Restart after safe shutdown should not fault
            anomalies = harness.inject_event(Event(EventType.UNIT_STARTED, ts=ts + 2, raw="Started"))
            self.assertFalse(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in anomalies))


if __name__ == "__main__":
    unittest.main()
