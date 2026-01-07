import unittest

from kkfm.watchdog.detector import Detector
from kkfm.watchdog.events import Event, EventType
from kkfm.watchdog.anomalies import AnomalyCode


class TestDetector(unittest.TestCase):
    def test_jingle_picked_no_start_timeout(self):
        d = Detector(start_timeout_s=1.0, end_timeout_s=10.0)
        d.feed(Event(EventType.JINGLE_PICKED, ts=0.0, raw="pick", name="A"))
        out = d.feed(Event(EventType.NOW_PLAYING, ts=2.0, raw="Now Playing: X", name="X"))
        # "Picked" can mean preloaded/cached and may not be followed by an immediate START.
        # We silently drop stale PICK state to avoid false positives.
        self.assertFalse(any(a.code == AnomalyCode.JINGLE_PICK_NO_START for a in out))

    def test_jingle_start_without_pick(self):
        d = Detector()
        out = d.feed(Event(EventType.JINGLE_START, ts=0.0, raw="start", name="A"))
        # We resync state and tolerate missing/out-of-order PICK without emitting noise.
        self.assertFalse(any(a.code == AnomalyCode.JINGLE_START_WITHOUT_PICK for a in out))
        self.assertEqual(d.jingle.started_name, "A")
        self.assertEqual(d.jingle.picked_name, "A")

    def test_jingle_start_then_pick_out_of_order_does_not_timeout(self):
        d = Detector(start_timeout_s=1.0, end_timeout_s=10.0)
        d.feed(Event(EventType.JINGLE_START, ts=0.0, raw="start", name="A"))
        d.feed(Event(EventType.JINGLE_PICKED, ts=0.1, raw="pick", name="A"))
        out = d.feed(Event(EventType.NOW_PLAYING, ts=2.0, raw="Now Playing: X", name="X"))
        self.assertFalse(any(a.code == AnomalyCode.JINGLE_PICK_NO_START for a in out))

    def test_commercial_name_mismatch(self):
        d = Detector()
        d.feed(Event(EventType.COMM_PICKED, ts=0.0, raw="pick", name="A"))
        out = d.feed(Event(EventType.COMM_START, ts=0.1, raw="start", name="B"))
        self.assertTrue(any(a.code == AnomalyCode.COMM_NAME_MISMATCH for a in out))

    def test_dac_overflow(self):
        d = Detector()
        out = d.feed(Event(EventType.DAC_OVERFLOW, ts=0.0, raw="[DAC] Ring buffer overflow", name=None))
        self.assertTrue(any(a.code == AnomalyCode.DAC_RING_OVERFLOW for a in out))

    def test_unit_started_baseline_then_restart_emits_error(self):
        d = Detector()
        # First start is baseline.
        out1 = d.feed(Event(EventType.UNIT_STARTED, ts=1.0, raw="Started KKFM Aircheck."))
        self.assertFalse(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in out1))

        # Second start (restart) should fault.
        out2 = d.feed(Event(EventType.UNIT_STARTED, ts=2.0, raw="Started KKFM Aircheck."))
        self.assertTrue(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in out2))

    def test_unit_started_after_safe_shutdown_does_not_fault(self):
        d = Detector()
        d.feed(Event(EventType.UNIT_STARTED, ts=1.0, raw="Started KKFM Aircheck."))
        d.feed(Event(EventType.SAFE_SHUTDOWN, ts=2.0, raw="shutdown reason: maintenance", name="maintenance"))
        out = d.feed(Event(EventType.UNIT_STARTED, ts=3.0, raw="Started KKFM Aircheck."))
        self.assertFalse(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in out))

    def test_unit_started_after_long_stop_gap_not_unexpected_restart(self):
        d = Detector(expected_start_after_stop_gap_s=10.0)
        d.feed(Event(EventType.UNIT_STARTED, ts=1.0, raw="Started KKFM Aircheck."))
        d.feed(Event(EventType.UNIT_STOPPED, ts=2.0, raw="Stopped KKFM Aircheck."))
        out = d.feed(Event(EventType.UNIT_STARTED, ts=20.0, raw="Started KKFM Aircheck."))
        self.assertFalse(any(a.code == AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED for a in out))

    def test_underrun_suppressed_during_startup_grace(self):
        d = Detector(underrun_startup_grace_s=10.0)
        d.feed(Event(EventType.UNIT_STARTED, ts=0.0, raw="Started KKFM Aircheck."))
        # Even if multiple underruns happen immediately after start, don't flag.
        out1 = d.feed(Event(EventType.DAC_UNDERRUN, ts=1.0, raw="[DAC] underrun", name=None))
        out2 = d.feed(Event(EventType.DAC_UNDERRUN, ts=2.0, raw="[DAC] underrun", name=None))
        out3 = d.feed(Event(EventType.DAC_UNDERRUN, ts=3.0, raw="[DAC] underrun", name=None))
        self.assertFalse(any(a.code == AnomalyCode.DAC_RING_UNDERRUN for a in (out1 + out2 + out3)))

        # After grace period, normal debounce should apply.
        a1 = d.feed(Event(EventType.DAC_UNDERRUN, ts=12.0, raw="[DAC] underrun", name=None))
        a2 = d.feed(Event(EventType.DAC_UNDERRUN, ts=13.0, raw="[DAC] underrun", name=None))
        a3 = d.feed(Event(EventType.DAC_UNDERRUN, ts=14.0, raw="[DAC] underrun", name=None))
        self.assertTrue(any(a.code == AnomalyCode.DAC_RING_UNDERRUN for a in (a1 + a2 + a3)))

    def test_dead_air_recovered_emits_info(self):
        d = Detector(dead_air_timeout_s=5.0)
        d.feed(Event(EventType.NOW_PLAYING, ts=0.0, raw="Now Playing: X", name="X"))
        # Trigger dead-air state.
        out1 = d.poll(10.0)
        self.assertTrue(any(a.code == AnomalyCode.RADIO_DEAD_AIR for a in out1))
        # Activity resumes after being dead.
        out2 = d.feed(Event(EventType.NOW_PLAYING, ts=11.0, raw="Now Playing: Y", name="Y"))
        self.assertTrue(any(a.code == AnomalyCode.RADIO_DEAD_AIR_RECOVERED for a in out2))


