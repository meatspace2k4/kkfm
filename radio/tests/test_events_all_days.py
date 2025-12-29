import os
import sys
import datetime as dt
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import events  # type: ignore
from radio import config  # type: ignore
from radio.utils import _read_csv_rows, normalize_label  # type: ignore


class TestAllEventDays(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = config.CALENDAR_TABLE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        cls.events_by_label = events._load_events_by_label(cls.cal)
        # Build CM rows by label (lowercased short label)
        cls.cm_rows = _read_csv_rows(config.COMMERCIALS_CSV)
        cls.cm_by_label = {}
        for r in cls.cm_rows:
            lab = normalize_label(r.get("eventlabelshort") or r.get("eventlabel") or "")
            if lab:
                cls.cm_by_label.setdefault(lab, []).append(r)
        cls.current_year = dt.date.today().year

    def test_all_precomputed_event_days_are_active(self):
        year = self.current_year
        for label, rows in self.events_by_label.items():
            if label in ("eventnone",):
                continue
            dates = []
            for row in rows:
                dates.extend(events.get_all_event_occurrences(row, year))
            for d in dates:
                with self.subTest(label=label, date=d):
                    self.assertTrue(
                        events._is_event_active(label, 0, d, self.events_by_label),
                        f"Label {label} should be active on {d}",
                    )

    def test_commercials_positive_weight_on_event_days(self):
        # For any label with commercials, day-of should yield positive weight for at least one CM row
        year = self.current_year
        for label, rows in self.events_by_label.items():
            cm_rows = self.cm_by_label.get(label, [])
            if not cm_rows:
                continue
            dates = []
            for row in rows:
                dates.extend(events.get_all_event_occurrences(row, year))
            for d in dates:
                now = dt.datetime(d.year, d.month, d.day, 12, 0, 0)
                weights = events.get_commercial_weights_cached(now, self.events_by_label, config.COMMERCIALS_CSV)
                any_pos = False
                for r in cm_rows:
                    music_no_s = r.get("musicno") or r.get("id") or r.get("uniqueid")
                    if not music_no_s:
                        continue
                    mn = str(int(float(music_no_s)))
                    if int(weights.get(mn, 0)) > 0:
                        any_pos = True
                        break
                with self.subTest(label=label, date=d):
                    self.assertTrue(any_pos, f"Expected positive CM weight for {label} on {d}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


