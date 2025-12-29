import os
import sys
import unittest
import datetime as dt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import config  # type: ignore
from radio import events  # type: ignore


class TestTotakekeAndWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = config.CALENDAR_TABLE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        cls.events_by_label = events._load_events_by_label(cls.cal)
        cls.current_year = dt.date.today().year

    def _first_future_occurrence(self, label: str, year: int) -> dt.date | None:
        rows = self.events_by_label.get(label, [])
        today = dt.date.today()
        candidates = []
        for row in rows:
            for d in events.get_all_event_occurrences(row, year):
                if d >= today:
                    candidates.append(d)
        return min(candidates) if candidates else None

    def test_insectconvention_before_days_window(self):
        # musicno 209 → Normal_09 should be active within 7 days before
        label = "insectconvention"
        target = self._first_future_occurrence(label, self.current_year)
        if not target:
            self.skipTest("No upcoming InsectConvention date in current year")
        # Exactly 7 days before at midday local
        now = dt.datetime(target.year, target.month, target.day, 12, 0, 0) - dt.timedelta(days=7)
        w = events.get_commercial_weights_cached(now, self.events_by_label, config.COMMERCIALS_CSV)
        self.assertGreater(int(w.get("209", 0)), 0, f"Expected positive weight for 209 seven days before {target}")

    def test_totakeke_postponed_sunday(self):
        # Find a Saturday with commercial events that should postpone KK Slider; then Sunday should include 207
        commercial_event_labels = {
            "fishingconvention", "insectconvention", "countdown", "easter",
            "junebride", "internationalmuseum", "fireworks", "halloween",
            "harvestfestival", "xmaseve", "carnival"
        }

        saturday_with_commercial_event = None
        for label in commercial_event_labels:
            rows = self.events_by_label.get(label, [])
            for row in rows:
                for d in events.get_all_event_occurrences(row, self.current_year):
                    if d.weekday() == 5:  # Saturday
                        saturday_with_commercial_event = d
                        break
                if saturday_with_commercial_event:
                    break
            if saturday_with_commercial_event:
                break

        if not saturday_with_commercial_event:
            self.skipTest("No Saturday with commercial events found this year to validate postponed Sunday")

        sunday = saturday_with_commercial_event + dt.timedelta(days=1)
        now = dt.datetime(sunday.year, sunday.month, sunday.day, 12, 0, 0)
        w = events.get_commercial_weights_cached(now, self.events_by_label, config.COMMERCIALS_CSV)
        self.assertGreater(int(w.get("207", 0)), 0,
            f"Expected positive weight for KK Slider (207) on postponed Sunday {sunday} "
            f"due to commercial event on Saturday {saturday_with_commercial_event}")

    def test_totakeke_weekend_logic(self):
        """Test KK Slider weekend postponement logic."""
        # Test cases for weekend logic - Saturday Jan 4 has no commercial events
        test_cases = [
            # (test_date, expected_result, description)
            (dt.datetime(2025, 1, 3, 12, 0, 0), True, "Friday should always play"),
            (dt.datetime(2025, 1, 4, 12, 0, 0), True, "Saturday should always play"),
            (dt.datetime(2025, 1, 5, 12, 0, 0), False, "Sunday should not play since Saturday had no commercial events"),
            (dt.datetime(2025, 1, 6, 12, 0, 0), False, "Monday should not play since Saturday had no commercial events"),
        ]

        for test_date, expected_active, description in test_cases:
            with self.subTest(date=test_date.strftime('%A %Y-%m-%d'), description=description):
                w = events.get_commercial_weights_cached(test_date, self.events_by_label, config.COMMERCIALS_CSV)
                totakeke_weight = int(w.get("207", 0))
                if expected_active:
                    self.assertGreater(totakeke_weight, 0, f"Expected KK Slider to play: {description}")
                else:
                    self.assertEqual(totakeke_weight, 0, f"Expected KK Slider not to play: {description}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


