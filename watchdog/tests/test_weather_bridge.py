import unittest

from watchdog.weather import RAINY, SUNNY, WeatherReading, WeatherSource
from watchdog.weather_bridge import Bridge

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

    def test_data_that_does_not_cover_the_hour_sends_nothing(self):
        t0 = 10 * HOUR
        self.src.give(reading(t0, ((t0, "Rain"),)))
        self.bridge.tick(t0 + 30 * HOUR)               # a stale mirror: no row for this hour
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
