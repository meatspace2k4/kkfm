import json
import unittest

from watchdog.weather import RAINY, SNOWY, SUNNY, WeatherReading, WeatherSource
from watchdog.weather_bridge import Bridge, Radar

HOUR = 3600.0


class FakeSource(WeatherSource):
    name = "fake"

    def __init__(self):
        self.next = None
        self.moved = True

    def changed(self):
        moved, self.moved = self.moved, False
        return moved

    def read(self):
        return self.next

    def give(self, reading):
        self.next, self.moved = reading, True


def reading(as_of, hours, minutes=(), minutes_end=0.0, condition="Clear"):
    return WeatherReading(condition=condition, as_of=as_of, source="fake", minutes=minutes,
                          minutes_end=minutes_end, hours=hours)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.src, self.sent, self.said, self.answer = FakeSource(), [], [], (True, "ok")

        def push(w):
            self.sent.append(w)
            return self.answer
        self.bridge = Bridge(self.src, push, self.said.append)

    def test_no_data_sends_nothing(self):
        self.bridge.tick(1000.0)
        self.assertEqual(self.sent, [])
        self.assertIn("fake: no data", self.said)

    def test_state_is_sent_once_and_again_only_when_it_changes(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Clear"), (t0 + HOUR, "Rain"))))
        self.bridge.tick(t0 + 60)
        self.bridge.tick(t0 + 120)                     # the same state: not sent again
        self.assertEqual(self.sent, [SUNNY])
        self.bridge.tick(t0 + HOUR + 1)                # the next hour's forecast, with no new data
        self.assertEqual(self.sent, [SUNNY, RAINY])

    def test_precipitation_chart_drives_the_state_minute_by_minute(self):
        t0 = 10 * HOUR
        chart = ((t0, 0.0), (t0 + 300, 2.0), (t0 + 600, 0.0))     # rain from minute 5 to minute 10
        self.src.give(reading(t0, ((t0, "Cloudy"),), chart, t0 + 3600, condition="Rain"))
        self.bridge.tick(t0 + 60)
        self.bridge.tick(t0 + 360)
        self.bridge.tick(t0 + 660)
        self.assertEqual(self.sent, [SUNNY, RAINY, SUNNY])

    def test_a_push_the_player_did_not_acknowledge_is_tried_again(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Rain"),)))
        self.answer = (False, "failed (refused)")
        self.bridge.tick(t0 + 1)
        self.bridge.tick(t0 + 2)                       # inside the retry wait
        self.assertEqual(self.sent, [RAINY])
        self.answer = (True, "ok")
        self.bridge.tick(t0 + 10)
        self.bridge.tick(t0 + 20)                      # acknowledged: not sent again
        self.assertEqual(self.sent, [RAINY, RAINY])
        self.assertEqual(sum("not sent" in s for s in self.said), 1)

    def test_the_charts_snow_type_wins_over_a_rain_condition(self):
        t0 = 10 * HOUR
        chart = ((t0, 2.0),)
        r = WeatherReading(condition="Rain", as_of=t0, source="fake", minutes=chart, minutes_end=t0 + 3600,
                           hours=((t0, "Rain"),), kinds=((t0, t0 + 3600, 2),))
        self.src.give(r)
        self.bridge.tick(t0 + 60)
        self.assertEqual(self.sent, [SNOWY])

    def test_a_rain_type_chart_stays_rainy(self):
        t0 = 10 * HOUR
        r = WeatherReading(condition="Drizzle", as_of=t0, source="fake", minutes=((t0, 0.45),), minutes_end=t0 + 3600,
                           hours=((t0, "Snow"),), kinds=((t0, t0 + 3600, 1),))
        self.src.give(r)
        self.bridge.tick(t0 + 60)
        self.assertEqual(self.sent, [RAINY])

    def test_data_that_does_not_cover_the_hour_sends_nothing(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Rain"),)))
        self.bridge.tick(t0 + 30 * HOUR)               # a stale mirror: no row for this hour
        self.assertEqual(self.sent, [])


class RadarBridgeTest(unittest.TestCase):
    """MRMS radar (Radar): snow supersedes the app; with no current app reading it decides."""

    def setUp(self):
        self.src, self.sent, self.said = FakeSource(), [], []
        self.radar_answer = {"time": 0.0, "flag": 0, "kinds": [0], "precip": "none"}

        def push(w):
            self.sent.append(w)
            return True, "ok"

        def run(args):
            return json.dumps(self.radar_answer)
        self.radar = Radar("40.782/-111.872", say=self.said.append, run=run, every=60.0)
        self.bridge = Bridge(self.src, push, self.said.append, radar=self.radar)

    def radar_says(self, precip, at):
        self.radar_answer = {"time": at, "flag": 3 if precip == "snow" else 10, "kinds": [], "precip": precip}
        self.radar.next = 0.0

    def test_radar_snow_supersedes_the_apps_rain(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Rain"),), condition="Rain"))
        self.radar_says("snow", t0)
        self.bridge.tick(t0 + 60)
        self.assertEqual(self.sent, [SNOWY])

    def test_radar_rain_leaves_the_app_in_charge(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Clear"),)))
        self.radar_says("rain", t0)
        self.bridge.tick(t0 + 60)
        self.assertEqual(self.sent, [SUNNY])

    def test_radar_decides_when_the_apps_reading_is_stale(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, tuple((t0 + h * HOUR, "Clear") for h in range(12))))
        self.radar_says("rain", t0 + 9 * HOUR)
        self.bridge.tick(t0 + 9 * HOUR)                # the app's data is nine hours old
        self.assertEqual(self.sent, [RAINY])

    def test_radar_decides_with_no_app_data(self):
        t0 = 10 * HOUR
        self.radar_says("none", t0)
        self.bridge.tick(t0 + 60)
        self.assertEqual(self.sent, [SUNNY])

    def test_a_stale_radar_reading_does_not_count(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Rain"),), condition="Rain"))
        self.radar_says("snow", t0 - HOUR)              # an hour old
        self.bridge.tick(t0 + 60)
        self.assertEqual(self.sent, [RAINY])


if __name__ == "__main__":
    unittest.main()
