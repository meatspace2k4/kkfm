import os
import sys
import datetime as dt
import unittest


# Ensure project root on path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import events  # type: ignore


CAL_CSV = os.path.join(ROOT, "radio", "data", "CalendarEventParam.csv")
CM_CSV = os.path.join(ROOT, "radio", "data", "RadioCM.csv")


class TestFireworksActivation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert os.path.exists(CAL_CSV), f"Missing calendar CSV at {CAL_CSV}"
        assert os.path.exists(CM_CSV), f"Missing commercials CSV at {CM_CSV}"
        cls.events_by_label = events._load_events_by_label(CAL_CSV)

    def test_fireworks_occurs_second_sunday_august_2025(self):
        gday = dt.date(2025, 8, 10)  # Sunday, 2nd week of August 2025
        # Using precomputed calendar windows via helper - Fireworks has BeforeDays=4
        active = events._is_event_active("Fireworks", 4, gday, self.events_by_label)
        self.assertTrue(active, "Fireworks should be active on 2nd Sunday of August 2025")

    def test_commercial_weight_positive_for_fireworks_on_day(self):
        # Fireworks day (2nd Sunday of August 2025)
        fireworks_day = dt.datetime(2025, 8, 10, 12, 0, 0)
        fireworks_weights = events.get_commercial_weights_cached(
            fireworks_day, self.events_by_label, commercials_file=CM_CSV
        )

        # Monday after fireworks (should be inactive for Fireworks)
        normal_day = dt.datetime(2025, 8, 11, 12, 0, 0)
        normal_weights = events.get_commercial_weights_cached(
            normal_day, self.events_by_label, commercials_file=CM_CSV
        )

        # CSV-backed expectation: Fireworks (musicno=214) has weight > 0 on the day, 0 the next day
        fw_weight = int(fireworks_weights.get("214", 0))
        norm_weight = int(normal_weights.get("214", 0))
        self.assertGreater(fw_weight, 0, "Fireworks commercial (214) should be active on fireworks day")
        self.assertGreaterEqual(fw_weight, norm_weight, "Fireworks commercial (214) should not be lower on fireworks day")

        # Optional aggregate sanity: fireworks day should be at least as weighted overall
        fireworks_total = sum(w for w in fireworks_weights.values() if w > 0)
        normal_total = sum(w for w in normal_weights.values() if w > 0)
        self.assertGreaterEqual(fireworks_total, normal_total,
                                "Fireworks day should not reduce total positive weights")


if __name__ == "__main__":
    unittest.main(verbosity=2)


