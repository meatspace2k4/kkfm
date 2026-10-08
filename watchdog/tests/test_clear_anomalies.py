from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from watchdog import run


class TestClearAnomalies(unittest.TestCase):
    def test_hourly_targets_profile_file(self) -> None:
        targets = run._anomalies_targets("hourly", remote=True, out_dir="/x")
        self.assertEqual(targets, ["/home/jpark/Aircheck/kkfm/watchdog/out-hourly/anomalies.jsonl"])

    def test_radio_remote_targets_pi_out_dir(self) -> None:
        targets = run._anomalies_targets("radio", remote=True, out_dir="/x")
        self.assertIn("~/Aircheck/kkfm/watchdog/out/anomalies.jsonl", targets)

    def test_local_truncates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "anomalies.jsonl"
            p.write_text('{"code":"X"}\n')
            with mock.patch.object(run, "_anomalies_targets", return_value=[str(p)]):
                rc = run.cmd_clear_anomalies(argparse.Namespace(ssh="", out_dir=d, profile="radio"))
            self.assertEqual(rc, 0)
            self.assertTrue(p.exists())
            self.assertEqual(p.read_text(), "")

    def test_local_missing_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(run, "_anomalies_targets", return_value=[str(Path(d) / "none.jsonl")]):
                rc = run.cmd_clear_anomalies(argparse.Namespace(ssh="", out_dir=d, profile="radio"))
            self.assertEqual(rc, 1)

    def test_remote_script_truncates_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "anomalies.jsonl"
            p.write_text('{"code":"X"}\n')
            r = subprocess.run([sys.executable, "-", str(p), str(Path(d) / "absent.jsonl")],
                               input=run._CLEAR_REMOTE_SCRIPT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertEqual(p.read_text(), "")
            r = subprocess.run([sys.executable, "-", str(Path(d) / "absent.jsonl")],
                               input=run._CLEAR_REMOTE_SCRIPT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)

    def test_remote_ssh_failure_is_nonzero(self) -> None:
        done = subprocess.CompletedProcess([], 255, stdout="", stderr="ssh: connect failed")
        with mock.patch.object(run.subprocess, "run", return_value=done) as m:
            rc = run.cmd_clear_anomalies(argparse.Namespace(ssh="pi@host", out_dir="/x", profile="hourly"))
        self.assertEqual(rc, 1)
        cmd = m.call_args.args[0]
        self.assertIn("pi@host", cmd)
        self.assertEqual(cmd[-1], "/home/jpark/Aircheck/kkfm/watchdog/out-hourly/anomalies.jsonl")


if __name__ == "__main__":
    unittest.main()
