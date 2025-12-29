import os
import sys
import unittest
import datetime as dt
import random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import config  # type: ignore
from radio import events  # type: ignore
from radio.utils import _read_csv_rows, normalize_label  # type: ignore


class TestEventCommercialJinglePairs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = config.CALENDAR_TABLE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        cls.events_by_label = events._load_events_by_label(cls.cal)

        # Load CSV rows
        cls.cm_rows = _read_csv_rows(config.COMMERCIALS_CSV)
        cls.jingle_rows = _read_csv_rows(config.JINGLES_CSV)

        # Build label -> rows maps
        cls.cm_by_label = {}
        for r in cls.cm_rows:
            lab = normalize_label(r.get("eventlabelshort") or r.get("eventlabel") or "")
            if lab:
                cls.cm_by_label.setdefault(lab, []).append(r)
        cls.jingle_by_label = {}
        for r in cls.jingle_rows:
            lab = normalize_label(r.get("eventlabelshort") or r.get("eventlabel") or "")
            if lab:
                cls.jingle_by_label.setdefault(lab, []).append(r)

        # Intersection of labels that have both CM and Jingle rows and exist in calendar
        cls.labels = sorted(
            set(cls.cm_by_label.keys())
            .intersection(cls.jingle_by_label.keys())
            .intersection(cls.events_by_label.keys())
        )

        # Resolve expected jingle filename per label using calendar tokens
        cls.jingles_dir = config.SOUND_FILES_PATH
        assert os.path.isdir(cls.jingles_dir), f"Missing jingles dir: {cls.jingles_dir}"

        # Known overrides to match on-disk filenames
        cls.jingle_token_overrides = {
            "countdown": "CountDown",
            "xmaseve": "Christmas",
            "harvestfestival": "Harvest",
        }

        cls.expected_jingle_file = {}
        for lab in cls.labels:
            token = None
            for ev in (cls.events_by_label.get(lab, []) or []):
                m = (ev.get("MsgFile") or ev.get("msgfile") or "").strip()
                token = m if m else (ev.get("LabelShort") or ev.get("LabelLong") or "").strip()
                if token:
                    break
            if not token:
                # No token -> cannot form expected filename
                continue
            token = cls.jingle_token_overrides.get(lab, token)
            fn = f"BGM_Radio_Jingle_{token}_00.bwav"
            full = os.path.join(cls.jingles_dir, fn)
            if os.path.exists(full):
                cls.expected_jingle_file[lab] = fn
        # Only test labels that have a resolvable on-disk jingle file
        cls.labels = [lab for lab in cls.labels if lab in cls.expected_jingle_file]
        cls.current_year = dt.date.today().year

    def _first_occurrence(self, label: str):
        rows = self.events_by_label.get(label, [])
        year = self.current_year
        for row in rows:
            occ = events.get_all_event_occurrences(row, year)
            if occ:
                return sorted(occ)[0]
        return None

    def test_jingle_and_commercial_present_on_event_day(self):
        if not self.labels:
            self.skipTest("No labels have both CM and Jingle with on-disk files; skipping pair test")
        random.seed(12345)
        for lab in self.labels:
            d = self._first_occurrence(lab)
            if not d:
                # Calendar has no concrete date in checked range
                continue
            now = dt.datetime(d.year, d.month, d.day, 12, 0, 0)

            # Jingle can be picked at least once
            expected_fn = self.expected_jingle_file[lab]
            picked_once = False
            for _ in range(60):
                sel = events.pick_jingle_filename_for_datetime_cached(
                    now, self.jingles_dir, self.events_by_label, config.JINGLES_CSV
                )
                if sel and os.path.basename(sel) == expected_fn:
                    picked_once = True
                    break

            # At least one commercial row for this label has positive weight
            weights = events.get_commercial_weights_cached(now, self.events_by_label, config.COMMERCIALS_CSV)
            any_pos = False
            for r in self.cm_by_label.get(lab, []):
                music_no_s = r.get("musicno") or r.get("id") or r.get("uniqueid")
                if not music_no_s:
                    continue
                mn = str(int(float(music_no_s)))
                if int(weights.get(mn, 0)) > 0:
                    any_pos = True
                    break

            with self.subTest(label=lab, date=d, jingle=expected_fn):
                self.assertTrue(picked_once, f"Expected jingle {expected_fn} to be pickable on {d}")
                self.assertTrue(any_pos, f"Expected positive commercial weight for {lab} on {d}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


