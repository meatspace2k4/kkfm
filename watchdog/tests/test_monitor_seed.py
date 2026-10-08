from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from watchdog import run


class _DoneProc:
    """A journalctl -f that ends at once."""

    def __init__(self) -> None:
        r, w = os.pipe()
        os.close(w)
        self.stdout = os.fdopen(r, "rb", buffering=0)

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


class TestMonitorSeed(unittest.TestCase):
    def test_restart_restores_now_playing_without_rereporting(self) -> None:
        t0 = time.time() - 1800
        history = [
            ("Started KKFM hourly BGM.", t0),
            ("[HOURLY] Hour 12 -> picked BGM_24Hour_12_Sunny.bwav (weather=Sunny)", t0 + 1),
            ("[FILE] BGM_24Hour_12_Sunny.bwav (id=1, size=123)", t0 + 1),
            ("ERROR: [DAC] Ring buffer overflow", t0 + 2),
            ("Now Playing: 12 PM (Sunny)", t0 + 240),
        ]
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            status_p = Path(td) / "log_status.json"
            args = Namespace(
                out_dir=str(out_dir), unit="acnh-hourly.service", since="now",
                start_timeout=300.0, end_timeout=45.0, dead_air_timeout=300.0,
                status_path=str(status_p), stdin=False, input_file=None,
            )
            with mock.patch.object(run, "_unit_journal_since_start", return_value=history), \
                 mock.patch.object(run.subprocess, "Popen", return_value=_DoneProc()), \
                 mock.patch.object(run.select, "select", return_value=([], [], [])):
                run.cmd_monitor_logs(args)

            status = json.loads(status_p.read_text())
            self.assertEqual(status["last_event"]["etype"], "now_playing")
            self.assertEqual(status["last_event"]["name"], "12 PM (Sunny)")
            self.assertEqual(status["last_bwav"]["name"], "BGM_24Hour_12_Sunny.bwav")
            self.assertEqual(status["detector"]["bgm"]["picked_name"], "BGM_24Hour_12_Sunny.bwav")
            codes = [json.loads(l)["code"] for l in (out_dir / "anomalies.jsonl").read_text().splitlines()]
            self.assertNotIn("DAC_RING_OVERFLOW", codes)


if __name__ == "__main__":
    unittest.main()
