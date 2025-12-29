import os
import sys
import datetime as dt
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import events  # type: ignore
from radio import config  # type: ignore


class TestAllEventsActivation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = config.CALENDAR_TABLE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        cls.events_by_label = events._load_events_by_label(cls.cal)

    def test_day_of_active_for_all_labels_with_dates(self):
        year = 2025
        for label, rows in self.events_by_label.items():
            if label in ("eventnone",):
                continue
            # Compute all precomputed occurrences for this label in the year
            dates = set()
            for row in rows:
                for d in events.get_all_event_occurrences(row, year):
                    dates.add(d)
            if not dates:
                # no precomputed dates for this year; skip
                continue
            for d in dates:
                with self.subTest(label=label, date=d):
                    self.assertTrue(
                        events._is_event_active(label, 0, d, self.events_by_label),
                        f"Label {label} should be active on its day {d}",
                    )

    def test_before_days_window(self):
        year = 2025

        # Get actual BeforeDays from CSV files
        cm_rows = events._read_csv_rows(config.COMMERCIALS_CSV)
        jingle_rows = events._read_csv_rows(config.JINGLES_CSV)

        cm_before_days = {}
        jingle_before_days = {}

        # Collect commercial BeforeDays
        for row in cm_rows:
            label = events.normalize_label(events._field(row, 'eventlabelshort', 'eventlabel'))
            if label and label != 'eventnone':
                before_days = int(float(events._field(row, 'beforedays', default='0') or '0'))
                if label not in cm_before_days:
                    cm_before_days[label] = before_days

        # Collect jingle BeforeDays
        for row in jingle_rows:
            label = events.normalize_label(events._field(row, 'eventlabelshort', 'eventlabel'))
            if label and label != 'eventnone':
                before_days = int(float(events._field(row, 'beforedays', default='0') or '0'))
                if label not in jingle_before_days:
                    jingle_before_days[label] = before_days

        for label, rows in self.events_by_label.items():
            if label in ("eventnone", "totakekelive"):
                continue

            # Get the maximum BeforeDays for this event (from commercials or jingles)
            max_before_days = max(cm_before_days.get(label, 0), jingle_before_days.get(label, 0))

            # Skip events with no before-days window
            if max_before_days == 0:
                continue

            dates = []
            for row in rows:
                occ = events.get_all_event_occurrences(row, year)
                if occ:
                    dates.extend(occ)
            if not dates:
                continue
            d = sorted(dates)[0]

            # Check activation window using the correct BeforeDays value
            self.assertTrue(events._is_event_active(label, max_before_days, d, self.events_by_label),
                           f"{label} should be active on event day {d}")
            self.assertTrue(events._is_event_active(label, max_before_days, d - dt.timedelta(days=1), self.events_by_label),
                           f"{label} should be active 1 day before event day {d}")
            # Outside the window should be inactive
            self.assertFalse(events._is_event_active(label, max_before_days, d - dt.timedelta(days=max_before_days + 1), self.events_by_label),
                            f"{label} should NOT be active {max_before_days + 1} days before event day {d}")

    def test_totakekelive_friday_saturday(self):
        label = "TotakekeLive"
        # Pick known Friday and Saturday
        fri = dt.date(2025, 8, 8)  # Friday
        sat = dt.date(2025, 8, 9)  # Saturday
        self.assertTrue(events._is_event_active(label, 0, fri, self.events_by_label))
        self.assertTrue(events._is_event_active(label, 0, sat, self.events_by_label))


if __name__ == "__main__":
    unittest.main(verbosity=2)


