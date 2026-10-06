import unittest

from watchdog.events import EventType
from watchdog.log_parser import parse_event


class TestLogParser(unittest.TestCase):
    def test_parse_short_iso_prefix(self):
        line = "2025-12-11T21:35:25-0700 host svc[1]: [RADIO] Jingle picked: X.bwav (size=123)"
        ev = parse_event(line, default_ts=0.0)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.etype, EventType.JINGLE_PICKED)
        self.assertEqual(ev.name, "X.bwav")

    def test_parse_short_iso_prefix_with_fractional_seconds(self):
        line = "2025-12-11T21:35:25.123-0700 host svc[1]: [RADIO] Jingle picked: X.bwav (size=123)"
        ev = parse_event(line, default_ts=0.0)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.etype, EventType.JINGLE_PICKED)
        self.assertGreater(ev.ts, 0.0)

    def test_parse_error_prefix_failed(self):
        line = "ERROR: [RADIO] Jingle failed to load: BGM_Radio_Jingle_XmasEve_00.bwav"
        ev = parse_event(line, default_ts=1.0)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.etype, EventType.JINGLE_FAILED)
        self.assertEqual(ev.name, "BGM_Radio_Jingle_XmasEve_00.bwav")


