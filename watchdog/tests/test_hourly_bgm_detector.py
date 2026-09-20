"""Hourly BGM fork: log parsing and anomaly detection in the watchdog."""
from __future__ import annotations

import unittest
from datetime import datetime

from watchdog.anomalies import AnomalyCode, Severity
from watchdog.detector import Detector
from watchdog.events import EventType
from watchdog.log_parser import parse_event


def _ts(hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(2026, 9, 20, hour, minute, second).timestamp()


PICK13 = "\x1b[33m[HOURLY] \x1b[0mHour 13 -> picked \x1b[36mBGM_24Hour_13_Sunny.bwav\x1b[0m (weather=Sunny)"
CHG13 = "\x1b[33m[HOURLY] \x1b[0mChangeover 13 -> 14: \x1b[36mBGM_24Hour_13_Sunny.bwav\x1b[0m fades at its next loop end (hour changed)"
END13 = "\x1b[33m[HOURLY] \x1b[0mTrack end: \x1b[36mBGM_24Hour_13_Sunny.bwav\x1b[0m (loops=37)"
PICK14 = "[HOURLY] Hour 14 -> picked BGM_24Hour_14_Sunny.bwav (weather=Sunny)"
WX_OK = "\x1b[33m[HOURLY] \x1b[0mWeather fetched: \x1b[1mSunny\x1b[0m (wmo=0 rain=0.00mm showers=0.00mm snow=0.00cm at 2026-09-20T13:30)"
WX_RAIN = "[HOURLY] Weather fetched: Rainy (wmo=61 rain=0.40mm showers=0.00mm snow=0.00cm at 2026-09-20T13:59)"
WX_FAIL = "\x1b[31mERROR: [HOURLY] Weather fetch failed; keeping previous observation\x1b[0m"
BGM_FAIL = "\x1b[31mERROR: [HOURLY] BGM failed to load: hourly/Stream/BGM_24Hour_14_Snowy.bwav\x1b[0m"


def _codes(anoms) -> list[AnomalyCode]:
    return [a.code for a in anoms]


class TestHourlyParse(unittest.TestCase):
    def test_pick(self) -> None:
        ev = parse_event(PICK13, default_ts=1.0)
        self.assertEqual(ev.etype, EventType.BGM_PICKED)
        self.assertEqual(ev.name, "BGM_24Hour_13_Sunny.bwav")

    def test_changeover(self) -> None:
        ev = parse_event(CHG13, default_ts=1.0)
        self.assertEqual(ev.etype, EventType.BGM_CHANGEOVER)
        self.assertEqual(ev.name, "BGM_24Hour_13_Sunny.bwav")
        self.assertIn("13 -> 14", ev.raw)

    def test_end(self) -> None:
        ev = parse_event(END13, default_ts=1.0)
        self.assertEqual(ev.etype, EventType.BGM_END)
        self.assertEqual(ev.name, "BGM_24Hour_13_Sunny.bwav")

    def test_failed(self) -> None:
        ev = parse_event(BGM_FAIL, default_ts=1.0)
        self.assertEqual(ev.etype, EventType.BGM_FAILED)
        self.assertEqual(ev.name, "hourly/Stream/BGM_24Hour_14_Snowy.bwav")

    def test_weather(self) -> None:
        ev = parse_event(WX_OK, default_ts=1.0)
        self.assertEqual(ev.etype, EventType.WEATHER_FETCHED)
        self.assertEqual(ev.name, "Sunny")
        ev = parse_event(WX_FAIL, default_ts=1.0)
        self.assertEqual(ev.etype, EventType.WEATHER_FAILED)

    def test_short_iso_prefix(self) -> None:
        ev = parse_event("2026-09-20T13:00:01-0600 " + PICK14.replace("14", "13", 1), default_ts=0.0)
        self.assertEqual(ev.etype, EventType.BGM_PICKED)
        self.assertNotEqual(ev.ts, 0.0)


class TestHourlyDetector(unittest.TestCase):
    def feed(self, det: Detector, line: str, ts: float):
        ev = parse_event(line, default_ts=ts)
        self.assertIsNotNone(ev, line)
        return det.feed(ev)

    def test_happy_hour_cycle_emits_nothing(self) -> None:
        det = Detector()
        out = []
        out += self.feed(det, WX_OK, _ts(13, 30))
        out += self.feed(det, PICK13, _ts(13, 30, 5))
        out += self.feed(det, WX_OK, _ts(13, 44))
        out += self.feed(det, WX_OK, _ts(13, 59))
        out += self.feed(det, CHG13, _ts(14, 0, 0))
        out += det.poll(_ts(14, 1))
        out += self.feed(det, END13, _ts(14, 1, 45))
        out += self.feed(det, PICK14, _ts(14, 1, 46))
        out += det.poll(_ts(14, 5))
        self.assertEqual(_codes(out), [])
        snap = det.snapshot()["bgm"]
        self.assertEqual(snap["picked_name"], "BGM_24Hour_14_Sunny.bwav")
        self.assertEqual(snap["picked_hour"], 14)
        self.assertEqual(snap["picked_weather"], "Sunny")
        self.assertEqual(snap["last_weather"], "Sunny")

    def test_hour_mismatch(self) -> None:
        det = Detector()
        out = self.feed(det, PICK14, _ts(13, 10))
        self.assertIn(AnomalyCode.BGM_HOUR_MISMATCH, _codes(out))
        self.assertEqual(out[0].severity, Severity.ERROR)

    def test_weather_mismatch(self) -> None:
        det = Detector()
        self.feed(det, WX_RAIN, _ts(13, 59))
        out = self.feed(det, PICK14, _ts(14, 0, 30))   # picked Sunny, last fetched Rainy
        self.assertIn(AnomalyCode.BGM_WEATHER_MISMATCH, _codes(out))

    def test_pick_while_playing_is_a_cut(self) -> None:
        det = Detector()
        self.feed(det, PICK13, _ts(13, 5))
        out = self.feed(det, PICK13, _ts(13, 20))   # no changeover, no end in between
        self.assertIn(AnomalyCode.BGM_PICK_WHILE_PLAYING, _codes(out))

    def test_end_without_changeover(self) -> None:
        det = Detector()
        self.feed(det, PICK13, _ts(13, 5))
        out = self.feed(det, END13, _ts(13, 20))
        self.assertIn(AnomalyCode.BGM_END_WITHOUT_CHANGEOVER, _codes(out))

    def test_name_mismatch(self) -> None:
        det = Detector()
        self.feed(det, PICK13, _ts(13, 5))
        out = self.feed(det, CHG13.replace("_13_Sunny", "_13_Rainy"), _ts(14, 0))
        self.assertIn(AnomalyCode.BGM_NAME_MISMATCH, _codes(out))
        out = self.feed(det, END13.replace("_13_Sunny", "_13_Rainy"), _ts(14, 1))
        self.assertIn(AnomalyCode.BGM_NAME_MISMATCH, _codes(out))

    def test_changeover_without_end_times_out(self) -> None:
        det = Detector(bgm_changeover_timeout_s=300)
        self.feed(det, PICK13, _ts(13, 5))
        self.feed(det, CHG13, _ts(14, 0))
        self.assertEqual(_codes(det.poll(_ts(14, 4))), [])
        out = det.poll(_ts(14, 5, 1))
        self.assertIn(AnomalyCode.BGM_CHANGEOVER_NO_END, _codes(out))
        self.assertEqual(_codes(det.poll(_ts(14, 6))), [])   # emitted once

    def test_end_without_next_pick_times_out(self) -> None:
        det = Detector(bgm_pick_timeout_s=30)
        self.feed(det, PICK13, _ts(13, 5))
        self.feed(det, CHG13, _ts(14, 0))
        self.feed(det, END13, _ts(14, 1))
        self.assertEqual(_codes(det.poll(_ts(14, 1, 20))), [])
        out = det.poll(_ts(14, 1, 31))
        self.assertIn(AnomalyCode.BGM_END_NO_PICK, _codes(out))

    def test_failed_load_and_weather_failure(self) -> None:
        det = Detector()
        out = self.feed(det, BGM_FAIL, _ts(14, 0))
        self.assertEqual(_codes(out), [AnomalyCode.BGM_FAILED_TO_LOAD])
        self.assertEqual(out[0].severity, Severity.ERROR)
        out = self.feed(det, WX_FAIL, _ts(14, 14))
        self.assertEqual(_codes(out), [AnomalyCode.WEATHER_FETCH_FAILED])
        self.assertEqual(out[0].severity, Severity.WARN)

    def test_weather_stale(self) -> None:
        det = Detector(weather_stale_s=3600)
        self.feed(det, WX_OK, _ts(13, 0))
        # Keep the log alive with the fork's heartbeat so dead-air does not fire.
        for m in range(4, 60, 4):
            self.feed(det, "Now Playing: 1 PM (Sunny)", _ts(13, m))
        self.assertNotIn(AnomalyCode.WEATHER_STALE, _codes(det.poll(_ts(13, 59))))
        out = det.poll(_ts(14, 0, 1))
        self.assertIn(AnomalyCode.WEATHER_STALE, _codes(out))
        self.assertNotIn(AnomalyCode.WEATHER_STALE, _codes(det.poll(_ts(14, 0, 30))))   # once until the next fetch
        self.feed(det, WX_OK, _ts(14, 14))
        self.assertNotIn(AnomalyCode.WEATHER_STALE, _codes(det.poll(_ts(14, 15))))

    def test_heartbeat_keeps_dead_air_quiet(self) -> None:
        det = Detector(dead_air_timeout_s=300)
        self.feed(det, PICK13, _ts(13, 0))
        for m in range(4, 60, 4):
            self.feed(det, "Now Playing: 1 PM (Sunny)", _ts(13, m))
            self.assertEqual(_codes(det.poll(_ts(13, m, 30))), [])

    def test_radio_state_untouched_by_bgm_events(self) -> None:
        det = Detector()
        self.feed(det, PICK13, _ts(13, 0))
        self.feed(det, CHG13, _ts(14, 0))
        self.feed(det, END13, _ts(14, 1))
        snap = det.snapshot()
        self.assertIsNone(snap["jingle"]["picked_name"])
        self.assertIsNone(snap["commercial"]["picked_name"])


if __name__ == "__main__":
    unittest.main()
