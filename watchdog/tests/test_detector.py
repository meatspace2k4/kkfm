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


