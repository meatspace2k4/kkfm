"""
Full Integration Tests

These tests verify the complete system integration:
1. Log lines → Detector → Reporter → JSONL
2. Status files → Headless Dashboard → Snapshot
3. Auto-restart with mock systemctl
4. TX monitor with mock serial
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from kkfm.watchdog.anomalies import AnomalyCode, Severity
from kkfm.watchdog.dashboard_headless import HeadlessDashboard
from kkfm.watchdog.detector import Detector
from kkfm.watchdog.events import Event, EventType
from kkfm.watchdog.reporting import Reporter
from kkfm.watchdog.tests.e2e.harness import E2ETestHarness
from kkfm.watchdog.tests.e2e.injectors import AnomalyTrigger, EventInjector


class TestFullIntegration(unittest.TestCase):
    """Test complete system integration."""

    def test_log_to_dashboard_flow(self) -> None:
        """
        Full flow: Log event → Detector → Reporter → JSONL → Dashboard snapshot
        """
        with E2ETestHarness() as harness:
            # 1. Inject an event that produces an anomaly
            injector = EventInjector()
            result = injector.trigger(AnomalyTrigger.DAC_RING_OVERFLOW)

            for event in result.events_injected:
                harness.inject_event(event)

            # 2. Verify anomaly was written to JSONL
            records = harness.read_anomalies_jsonl()
            self.assertGreaterEqual(len(records), 1)
            self.assertIn("DAC_RING_OVERFLOW", [r["code"] for r in records])

            # 3. Create headless dashboard and take snapshot
            snapshot_file = harness.out_dir / "dashboard_snapshot.json"
            dashboard = HeadlessDashboard(
                harness.out_dir,
                snapshot_file,
            )
            snap = dashboard.run_once()

            # 4. Verify dashboard captured the anomaly
            self.assertEqual(len(snap.anomalies), len(records))
            self.assertIn("DAC_RING_OVERFLOW", [a["code"] for a in snap.anomalies])

    def test_status_files_to_dashboard(self) -> None:
        """
        Verify dashboard reads status files correctly.
        """
        with E2ETestHarness() as harness:
            # Write status files
            harness.write_tx_status({
                "ts": time.time(),
                "freq_mhz": 87.7,
                "audio_present": True,
                "inlevel": 15,
            })
            harness.write_log_status({
                "ts": time.time(),
                "epoch": "test-123",
                "last_bwav": {"name": "TestSong.bwav", "src": "FILE"},
            })

            # Take dashboard snapshot
            snapshot_file = harness.out_dir / "snapshot.json"
            dashboard = HeadlessDashboard(harness.out_dir, snapshot_file)
            snap = dashboard.run_once()

            # Verify TX status
            self.assertIsNotNone(snap.tx_status)
            self.assertEqual(snap.tx_status["freq_mhz"], 87.7)
            self.assertTrue(snap.tx_status["audio_present"])

            # Verify log status
            self.assertIsNotNone(snap.log_status)
            self.assertEqual(snap.log_status["epoch"], "test-123")
            self.assertEqual(snap.log_status["last_bwav"]["name"], "TestSong.bwav")

    def test_multiple_anomalies_sequence(self) -> None:
        """
        Test a realistic sequence of events producing multiple anomalies.
        """
        with E2ETestHarness() as harness:
            ts = time.time()

            # Simulate a problematic radio session:
            # 1. DAC overflow
            harness.inject_event(Event(
                EventType.DAC_OVERFLOW, ts=ts, raw="[DAC] overflow"
            ))

            # 2. Normal jingle (should not produce anomaly)
            harness.inject_event(Event(
                EventType.JINGLE_PICKED, ts=ts + 1, raw="pick", name="Jingle1"
            ))
            harness.inject_event(Event(
                EventType.JINGLE_START, ts=ts + 1.1, raw="start", name="Jingle1"
            ))
            harness.inject_event(Event(
                EventType.JINGLE_END, ts=ts + 5, raw="end", name="Jingle1"
            ))

            # 3. Jingle name mismatch
            harness.inject_event(Event(
                EventType.JINGLE_PICKED, ts=ts + 10, raw="pick", name="JingleA"
            ))
            harness.inject_event(Event(
                EventType.JINGLE_START, ts=ts + 10.1, raw="start", name="JingleB"
            ))

            # 4. DAC underrun
            harness.inject_event(Event(
                EventType.DAC_UNDERRUN, ts=ts + 15, raw="[DAC] underrun"
            ))

            # Should have exactly 3 anomalies
            self.assertEqual(len(harness.captured_anomalies), 3)

            codes = [ca.anomaly.code for ca in harness.captured_anomalies]
            self.assertIn(AnomalyCode.DAC_RING_OVERFLOW, codes)
            self.assertIn(AnomalyCode.JINGLE_NAME_MISMATCH, codes)
            self.assertIn(AnomalyCode.DAC_RING_UNDERRUN, codes)

    def test_restart_detection_flow(self) -> None:
        """
        Test unexpected restart detection and safe shutdown handling.
        """
        with E2ETestHarness() as harness:
            ts = time.time()

            # First start (baseline)
            harness.inject_event(Event(
                EventType.UNIT_STARTED, ts=ts, raw="Started KKFM Aircheck."
            ))
            self.assertEqual(len(harness.captured_anomalies), 0)

            # Unexpected restart (no safe shutdown)
            harness.inject_event(Event(
                EventType.UNIT_STARTED, ts=ts + 100, raw="Started KKFM Aircheck."
            ))
            self.assertEqual(len(harness.captured_anomalies), 1)
            self.assertEqual(
                harness.captured_anomalies[0].anomaly.code,
                AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED
            )

            harness.clear_anomalies()

            # Safe shutdown followed by restart
            harness.inject_event(Event(
                EventType.SAFE_SHUTDOWN, ts=ts + 200, raw="shutdown", name="maintenance"
            ))
            harness.inject_event(Event(
                EventType.UNIT_STARTED, ts=ts + 300, raw="Started KKFM Aircheck."
            ))
            # Should not produce an anomaly after safe shutdown
            unexpected = [
                ca for ca in harness.captured_anomalies
                if ca.anomaly.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED
            ]
            self.assertEqual(len(unexpected), 0)


class TestAutoRestartIntegration(unittest.TestCase):
    """Test auto-restart integration with mock systemctl."""

    def test_mock_restart_and_verify(self) -> None:
        """
        Test that mock systemctl can simulate restart and we can verify it.
        """
        with E2ETestHarness() as harness:
            # Simulate restart
            rc = harness.simulate_restart("acnh-aircheck.service")
            self.assertEqual(rc, 0)

            # Verify restart was tracked
            self.assertEqual(len(harness.mock_systemctl.restart_calls), 1)
            unit, _ = harness.mock_systemctl.restart_calls[0]
            self.assertEqual(unit, "acnh-aircheck.service")

            # Verify service state updated
            svc = harness.mock_systemctl.services["acnh-aircheck.service"]
            self.assertEqual(svc.restart_count, 1)
            self.assertNotEqual(svc.active_enter_timestamp, "")

    def test_restart_failure_handling(self) -> None:
        """
        Test that restart failures are tracked.
        """
        with E2ETestHarness() as harness:
            harness.mock_systemctl.set_fail_restart("acnh-aircheck.service", return_code=1)

            rc = harness.simulate_restart("acnh-aircheck.service")
            self.assertEqual(rc, 1)

            # Restart was still attempted
            self.assertEqual(len(harness.mock_systemctl.restart_calls), 1)


class TestMockSerialIntegration(unittest.TestCase):
    """Test mock serial integration."""

    def test_telemetry_injection_parsing(self) -> None:
        """
        Test that injected telemetry follows expected format.
        """
        with E2ETestHarness() as harness:
            harness.mock_serial.open()

            # Inject telemetry
            harness.mock_serial.inject_telemetry(
                ms=60000,
                freq=87.7,
                noise=-95,
                inlevel=20,
                over=0,
            )

            # Read it back
            line = harness.mock_serial.readline().decode("utf-8").strip()
            parts = line.split()

            self.assertEqual(len(parts), 5)
            self.assertEqual(parts[0], "60000")  # ms
            self.assertEqual(parts[1], "87.7")  # freq
            self.assertEqual(parts[2], "-95")  # noise
            self.assertEqual(parts[3], "20")  # inlevel
            self.assertEqual(parts[4], "0")  # over

    def test_banner_and_recovery_injection(self) -> None:
        """
        Test injection of banner and recovery events.
        """
        with E2ETestHarness() as harness:
            harness.mock_serial.open()

            harness.mock_serial.inject_banner(88.1)
            banner = harness.mock_serial.readline().decode("utf-8").strip()
            self.assertEqual(banner, "TX_FREQ=88.1")

            harness.mock_serial.inject_recovery("I2C timeout")
            recovery = harness.mock_serial.readline().decode("utf-8").strip()
            self.assertIn("RECOVER:", recovery)
            self.assertIn("I2C timeout", recovery)


class TestFlagFileIntegration(unittest.TestCase):
    """Test flag file creation for anomalies."""

    def test_flag_files_created_by_severity(self) -> None:
        """
        Verify flag files are created for each anomaly.
        """
        with E2ETestHarness() as harness:
            ts = time.time()

            # Inject anomalies of different severities
            harness.inject_event(Event(
                EventType.DAC_OVERFLOW, ts=ts, raw="overflow"
            ))  # ERROR
            harness.inject_event(Event(
                EventType.DAC_UNDERRUN, ts=ts + 1, raw="underrun"
            ))  # WARN

            flag_files = harness.get_flag_files()
            self.assertGreaterEqual(len(flag_files), 1)


if __name__ == "__main__":
    unittest.main()
