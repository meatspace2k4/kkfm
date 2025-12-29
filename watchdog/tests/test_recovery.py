import unittest

from kkfm.watchdog.anomalies import Anomaly, AnomalyCode, Severity
from kkfm.watchdog.recovery import AutoRestartConfig, AutoRestarter


class TestAutoRestarter(unittest.TestCase):
    def test_underrun_threshold_triggers_restart(self):
        calls = []

        def restart_fn(unit: str):
            calls.append(unit)
            return True, "ok"

        cfg = AutoRestartConfig(
            enabled=True,
            unit="acnh-aircheck.service",
            restart_on_dead_air=False,
            restart_on_underrun=True,
            underrun_count=3,
            underrun_window_s=10.0,
            cooldown_s=0.0,
            max_per_hour=0,
        )
        r = AutoRestarter(cfg, restart_fn=restart_fn)

        for t in (1.0, 2.0):
            a = Anomaly(
                code=AnomalyCode.DAC_RING_UNDERRUN,
                severity=Severity.WARN,
                ts=t,
                message="u",
            )
            self.assertIsNone(r.consider(a))

        a3 = Anomaly(
            code=AnomalyCode.DAC_RING_UNDERRUN,
            severity=Severity.WARN,
            ts=3.0,
            message="u",
        )
        out = r.consider(a3)
        self.assertIsNotNone(out)
        self.assertEqual(out.code, AnomalyCode.AIRCHECK_AUTO_RESTARTED)
        self.assertEqual(calls, ["acnh-aircheck.service"])

    def test_cooldown_suppresses_restart(self):
        calls = []

        def restart_fn(unit: str):
            calls.append(unit)
            return True, "ok"

        cfg = AutoRestartConfig(
            enabled=True,
            unit="acnh-aircheck.service",
            restart_on_dead_air=True,
            restart_on_underrun=False,
            underrun_count=10,
            underrun_window_s=60.0,
            cooldown_s=100.0,
            max_per_hour=0,
        )
        r = AutoRestarter(cfg, restart_fn=restart_fn)

        dead1 = Anomaly(
            code=AnomalyCode.RADIO_DEAD_AIR,
            severity=Severity.CRITICAL,
            ts=1000.0,
            message="dead",
        )
        out1 = r.consider(dead1)
        self.assertIsNotNone(out1)
        self.assertEqual(out1.code, AnomalyCode.AIRCHECK_AUTO_RESTARTED)

        dead2 = Anomaly(
            code=AnomalyCode.RADIO_DEAD_AIR,
            severity=Severity.CRITICAL,
            ts=1050.0,
            message="dead",
        )
        out2 = r.consider(dead2)
        self.assertIsNotNone(out2)
        self.assertEqual(out2.code, AnomalyCode.AIRCHECK_AUTO_RESTART_SUPPRESSED)
        self.assertEqual(len(calls), 1)

    def test_restart_failure_emits_error(self):
        def restart_fn(_unit: str):
            return False, "nope"

        cfg = AutoRestartConfig(
            enabled=True,
            unit="acnh-aircheck.service",
            restart_on_dead_air=True,
            restart_on_underrun=False,
            underrun_count=10,
            underrun_window_s=60.0,
            cooldown_s=0.0,
            max_per_hour=0,
        )
        r = AutoRestarter(cfg, restart_fn=restart_fn)

        dead = Anomaly(
            code=AnomalyCode.RADIO_DEAD_AIR,
            severity=Severity.CRITICAL,
            ts=1000.0,
            message="dead",
        )
        out = r.consider(dead)
        self.assertIsNotNone(out)
        self.assertEqual(out.code, AnomalyCode.AIRCHECK_AUTO_RESTART_FAILED)
