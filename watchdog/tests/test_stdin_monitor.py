"""monitor-logs --stdin: status keeps refreshing during silence and a closed log
stream (player exited) is reported as an anomaly."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class TestStdinMonitor(unittest.TestCase):
    def test_player_exit_is_reported_and_status_stays_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            status = Path(td) / "log_status.json"
            env = dict(os.environ)
            env["WATCHDOG_STATUS_TO_DISK"] = "1"
            env["WATCHDOG_CLEAR_RUNTIME_ON_START"] = "0"
            # Run the CLI module from its own parent (package import `watchdog.run`).
            pkg_parent = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "watchdog.run", "monitor-logs", "--stdin",
                    "--out-dir", str(out), "--status-path", str(status),
                    "--status-write-min-interval", "0.5",
                ],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=pkg_parent, env=env,
            )
            assert proc.stdin is not None
            proc.stdin.write(b"[HOURLY] Hour 13 -> picked BGM_24Hour_13_Sunny.bwav (weather=Sunny)\n")
            proc.stdin.write(b"Now Playing: 1 PM (Sunny)\n")
            proc.stdin.flush()

            # Silence: the status file must still be rewritten by the 1 s tick.
            time.sleep(3.0)
            snap = json.loads(status.read_text())
            self.assertLess(time.time() - float(snap["ts"]), 2.5)
            self.assertEqual(snap["last_event"]["etype"], "now_playing")
            self.assertEqual(snap["detector"]["bgm"]["picked_hour"], 13)

            # Player goes away: stdin closes.
            proc.stdin.close()
            time.sleep(2.5)
            recs = [json.loads(l) for l in (out / "anomalies.jsonl").read_text().splitlines() if l.strip()]
            codes = [r["code"] for r in recs]
            self.assertIn("RADIO_OFFLINE", codes)
            snap = json.loads(status.read_text())
            self.assertEqual(snap["last_event"]["etype"], "unit_stopped")
            # The monitor keeps running (and reporting) until it is stopped.
            self.assertIsNone(proc.poll())
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
