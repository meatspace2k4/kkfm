import os
import sys
import datetime as dt
import unittest
import random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import events  # type: ignore
from radio import config  # type: ignore


class TestEventJinglesOnEventDays(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = config.CALENDAR_TABLE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        cls.events_by_label = events._load_events_by_label(cls.cal)
        cls.jingles_dir = config.SOUND_FILES_PATH
        assert os.path.isdir(cls.jingles_dir), f"Missing jingles dir: {cls.jingles_dir}"

        # LabelShort -> expected on-disk jingle filename (as used by picker)
        cls.label_to_file = {
            "carnival": "BGM_Radio_Jingle_Carnival_00.bwav",
            "countdown": "BGM_Radio_Jingle_CountDown_00.bwav",
            "halloween": "BGM_Radio_Jingle_Halloween_00.bwav",
            "harvestfestival": "BGM_Radio_Jingle_Harvest_00.bwav",
            "xmaseve": "BGM_Radio_Jingle_Christmas_00.bwav",
            "easter": "BGM_Radio_Jingle_Easter_00.bwav",
        }

        # Only keep labels where the file is present on disk
        cls.present_labels = {
            lab: fn for lab, fn in cls.label_to_file.items()
            if os.path.exists(os.path.join(cls.jingles_dir, fn))
        }
        if not cls.present_labels:
            raise unittest.SkipTest("No event jingle files found on disk; skipping jingle event tests")

    def _first_occurrence(self, label: str, start_year: int = 2020, end_year: int = 2030):
        rows = self.events_by_label.get(label, [])
        for year in range(start_year, end_year + 1):
            for row in rows:
                occ = events.get_all_event_occurrences(row, year)
                if occ:
                    return sorted(occ)[0]
        return None

    def test_event_jingle_can_be_picked_on_event_day(self):
        random.seed(1234)
        for label, fn in self.present_labels.items():
            d = self._first_occurrence(label)
            if not d:
                # No precomputed date in the checked range; skip
                continue
            # Midday to avoid TOD edge cases
            now = dt.datetime(d.year, d.month, d.day, 12, 0, 0)
            picked_once = False
            # Try multiple times because selection is weighted random among eligibles
            for _ in range(60):
                sel = events.pick_jingle_filename_for_datetime_cached(
                    now, self.jingles_dir, self.events_by_label, config.JINGLES_CSV
                )
                if sel and os.path.basename(sel) == fn:
                    picked_once = True
                    break
            with self.subTest(label=label, date=d, expected=fn):
                self.assertTrue(picked_once, f"Expected to pick {fn} at least once on {d}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


