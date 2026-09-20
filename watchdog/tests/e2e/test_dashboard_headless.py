"""
Tests for Headless Dashboard

Verifies the headless dashboard can:
- Read status files correctly
- Take snapshots
- Write output to file
- Handle missing/corrupt files gracefully
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from watchdog.dashboard_headless import HeadlessDashboard, DashboardSnapshot


class TestHeadlessDashboard(unittest.TestCase):
    """Test headless dashboard functionality."""

    def test_snapshot_empty_directory(self) -> None:
        """Snapshot of empty directory should return defaults."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            dashboard = HeadlessDashboard(out_dir, output_file)
            snap = dashboard.take_snapshot()

            self.assertIsNone(snap.tx_status)
            self.assertIsNone(snap.log_status)
            self.assertEqual(len(snap.anomalies), 0)
            self.assertEqual(len(snap.errors), 0)

    def test_snapshot_reads_tx_status(self) -> None:
        """Snapshot should read tx_status.json."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            tx_data = {"ts": time.time(), "freq_mhz": 87.7, "audio_present": True}
            (out_dir / "tx_status.json").write_text(json.dumps(tx_data))

            dashboard = HeadlessDashboard(out_dir, output_file)
            snap = dashboard.take_snapshot()

            self.assertIsNotNone(snap.tx_status)
            self.assertEqual(snap.tx_status["freq_mhz"], 87.7)
            self.assertTrue(snap.tx_status["audio_present"])

    def test_snapshot_reads_log_status(self) -> None:
        """Snapshot should read log_status.json."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            log_data = {
                "ts": time.time(),
                "last_bwav": {"name": "Test.bwav", "src": "FILE"},
            }
            (out_dir / "log_status.json").write_text(json.dumps(log_data))

            dashboard = HeadlessDashboard(out_dir, output_file)
            snap = dashboard.take_snapshot()

            self.assertIsNotNone(snap.log_status)
            self.assertEqual(snap.log_status["last_bwav"]["name"], "Test.bwav")

    def test_snapshot_reads_anomalies(self) -> None:
        """Snapshot should read recent anomalies from JSONL."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            anomalies = [
                {"code": "DAC_RING_OVERFLOW", "ts": time.time(), "severity": "error"},
                {"code": "DAC_RING_THROTTLE", "ts": time.time(), "severity": "warn"},
            ]
            (out_dir / "anomalies.jsonl").write_text(
                "\n".join(json.dumps(a) for a in anomalies)
            )

            dashboard = HeadlessDashboard(out_dir, output_file)
            snap = dashboard.take_snapshot()

            self.assertEqual(len(snap.anomalies), 2)
            codes = {a["code"] for a in snap.anomalies}
            self.assertIn("DAC_RING_OVERFLOW", codes)
            self.assertIn("DAC_RING_THROTTLE", codes)

    def test_write_snapshot(self) -> None:
        """write_snapshot should write JSON to file."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            dashboard = HeadlessDashboard(out_dir, output_file)
            dashboard.write_snapshot()

            self.assertTrue(output_file.exists())
            data = json.loads(output_file.read_text())
            self.assertIn("ts", data)
            self.assertIn("services", data)
            self.assertIn("anomalies", data)

    def test_run_once(self) -> None:
        """run_once should take snapshot and write to file."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            tx_data = {"ts": time.time(), "freq_mhz": 88.1}
            (out_dir / "tx_status.json").write_text(json.dumps(tx_data))

            dashboard = HeadlessDashboard(out_dir, output_file)
            snap = dashboard.run_once()

            self.assertIsNotNone(snap.tx_status)
            self.assertTrue(output_file.exists())

    def test_handles_corrupt_json(self) -> None:
        """Snapshot should handle corrupt JSON gracefully."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            (out_dir / "tx_status.json").write_text("not valid json {{{")

            dashboard = HeadlessDashboard(out_dir, output_file)
            snap = dashboard.take_snapshot()

            self.assertIsNone(snap.tx_status)
            self.assertGreaterEqual(len(snap.errors), 1)
            self.assertIn("tx_status", snap.errors[0])

    def test_max_anomalies_limit(self) -> None:
        """Snapshot should respect max_anomalies limit."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            output_file = Path(td) / "snapshot.json"

            # Write 20 anomalies
            anomalies = [
                {"code": f"TEST_{i}", "ts": time.time() + i}
                for i in range(20)
            ]
            (out_dir / "anomalies.jsonl").write_text(
                "\n".join(json.dumps(a) for a in anomalies)
            )

            dashboard = HeadlessDashboard(out_dir, output_file, max_anomalies=5)
            snap = dashboard.take_snapshot()

            self.assertEqual(len(snap.anomalies), 5)
            # Should have the last 5
            codes = [a["code"] for a in snap.anomalies]
            self.assertEqual(codes, ["TEST_15", "TEST_16", "TEST_17", "TEST_18", "TEST_19"])


class TestDashboardSnapshot(unittest.TestCase):
    """Test DashboardSnapshot dataclass."""

    def test_to_dict(self) -> None:
        """to_dict should return serializable dict."""
        snap = DashboardSnapshot(
            ts=time.time(),
            tx_status={"freq_mhz": 87.7},
            anomalies=[{"code": "TEST"}],
        )
        d = snap.to_dict()

        self.assertIn("ts", d)
        self.assertIn("tx_status", d)
        self.assertIn("anomalies", d)
        self.assertEqual(d["tx_status"]["freq_mhz"], 87.7)

        # Should be JSON serializable
        json_str = json.dumps(d)
        self.assertIsInstance(json_str, str)


if __name__ == "__main__":
    unittest.main()
