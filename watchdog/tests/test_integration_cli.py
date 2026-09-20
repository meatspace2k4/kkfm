from __future__ import annotations

import json
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path

from watchdog.run import cmd_monitor_logs, cmd_scan_assets


class TestIntegrationCLI(unittest.TestCase):
    def test_scan_assets_end_to_end_emits_anomaly_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            out_dir = td_path / "out"
            stream_dir = td_path / "stream"
            stream_dir.mkdir(parents=True, exist_ok=True)

            # Create an intentionally broken BWAV file (<96 bytes) to force an anomaly.
            (stream_dir / "bad_small.bwav").write_bytes(b"BWAV" + b"\x00" * 10)

            # Minimal assets.h + assets blob so range checks run.
            assets_h = td_path / "assets.h"
            assets_h.write_text('{ 0, 0, 0.0f, 0, "Dummy" },\n', encoding="utf-8")
            assets_blob = td_path / "assets_blob.bin"
            assets_blob.write_bytes(b"\x00" * 1)

            args = Namespace(
                out_dir=str(out_dir),
                stream_dir=str(stream_dir),
                assets_h=str(assets_h),
                assets_blob=str(assets_blob),
            )
            rc = cmd_scan_assets(args)
            self.assertEqual(rc, 0)

            jsonl = out_dir / "anomalies.jsonl"
            flags_dir = out_dir / "flags"
            self.assertTrue(jsonl.exists())
            self.assertTrue(flags_dir.exists())

            # Should have at least one anomaly about the too-small BWAV.
            lines = jsonl.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(lines), 1)
            recs = [json.loads(l) for l in lines]
            codes = {r.get("code") for r in recs}
            self.assertIn("FILE_TOO_SMALL", codes)

    def test_monitor_logs_end_to_end_from_file_emits_anomaly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            out_dir = td_path / "out"
            log_file = td_path / "service.log"

            # A single DAC throttle line should produce a WARN anomaly.
            log_file.write_text(
                "\n".join(
                    [
                        "2025-01-01T00:00:00Z host acnh-aircheck[123]: ERROR: [DAC] Ring buffer full; throttling producer to avoid dropping samples.",
                        "2025-01-01T00:00:01Z host acnh-aircheck[123]: [FILE] BGM_Audio_001_Sanbika_Cheap.bwav (id=1, size=12345)",
                        "2025-01-01T00:00:02Z host acnh-aircheck[123]: [CACHE] BGM_Audio_001_Sanbika_Cheap.bwav (id=1, size=12345)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            args = Namespace(
                out_dir=str(out_dir),
                unit="acnh-aircheck.service",
                since="now",
                start_timeout=0.1,
                end_timeout=0.1,
                status_path=str(td_path / "log_status.json"),
                stdin=False,
                input_file=str(log_file),
            )
            rc = cmd_monitor_logs(args)
            self.assertEqual(rc, 0)

            jsonl = out_dir / "anomalies.jsonl"
            self.assertTrue(jsonl.exists())
            lines = jsonl.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(lines), 1)
            recs = [json.loads(l) for l in lines]
            codes = {r.get("code") for r in recs}
            self.assertIn("DAC_RING_THROTTLE", codes)

            # Also expect the log status snapshot to persist last BWAV seen.
            status_p = td_path / "log_status.json"
            self.assertTrue(status_p.exists())
            status = json.loads(status_p.read_text(encoding="utf-8"))
            last_bwav = status.get("last_bwav") or {}
            self.assertEqual(last_bwav.get("name"), "BGM_Audio_001_Sanbika_Cheap.bwav")
            self.assertEqual(last_bwav.get("src"), "CACHE")

            # Also expect the log status snapshot to include a level for the last event.
            last_event = status.get("last_event") or {}
            self.assertEqual(last_event.get("etype"), "dac_throttle")
            self.assertEqual(last_event.get("level"), "WARN")
