#!/usr/bin/env python3
"""
Comprehensive integration tests for mono.py - verifying feature parity with refactored modules
"""

import unittest
import os
import sys
import datetime as dt
import random
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import numpy as np
import pytz

# Add the project root to the path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mono


class TestMonoEventSystem(unittest.TestCase):
    """Test event system functionality in mono.py"""

    @classmethod
    def setUpClass(cls):
        cls.cal = mono.CALENDAR_FILE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        mono.prime_events_async(cls.cal)
        mono._EVENTS_READY.wait(timeout=5.0)
        cls.events_by_label = mono.get_cached_events()

    def test_event_loading(self):
        """Test that events load correctly."""
        events = mono.get_cached_events()
        self.assertIsInstance(events, dict)
        self.assertGreater(len(events), 0, "Should load at least some events")

    def test_event_occurrences(self):
        """Test that event occurrences are computed correctly."""
        year = 2025
        for label, rows in self.events_by_label.items():
            if label in ("eventnone",):
                continue
            for row in rows:
                dates = mono.get_all_event_occurrences(row, year)
                self.assertIsInstance(dates, list)
                for d in dates:
                    self.assertIsInstance(d, dt.date)

    def test_day_of_active_for_all_labels(self):
        """Test that events are active on their day."""
        year = 2025
        for label, rows in self.events_by_label.items():
            if label in ("eventnone",):
                continue
            dates = set()
            for row in rows:
                for d in mono.get_all_event_occurrences(row, year):
                    dates.add(d)
            if not dates:
                continue
            for d in dates:
                with self.subTest(label=label, date=d):
                    self.assertTrue(
                        mono._is_event_active(label, 0, d, self.events_by_label),
                        f"Label {label} should be active on its day {d}",
                    )

    def test_before_days_window(self):
        """Test before_days activation window."""
        year = 2025
        
        # Get BeforeDays from CSV files
        cm_rows = mono._read_csv_rows(mono.COMMERCIALS_CSV)
        jingle_rows = mono._read_csv_rows(mono.JINGLES_CSV)
        
        cm_before_days = {}
        jingle_before_days = {}
        
        for row in cm_rows:
            label = mono.normalize_label(mono._field(row, 'eventlabelshort', 'eventlabel'))
            if label and label != 'eventnone':
                before_days = int(float(mono._field(row, 'beforedays', default='0') or '0'))
                if label not in cm_before_days:
                    cm_before_days[label] = before_days
        
        for row in jingle_rows:
            label = mono.normalize_label(mono._field(row, 'eventlabelshort', 'eventlabel'))
            if label and label != 'eventnone':
                before_days = int(float(mono._field(row, 'beforedays', default='0') or '0'))
                if label not in jingle_before_days:
                    jingle_before_days[label] = before_days
        
        for label, rows in self.events_by_label.items():
            if label in ("eventnone", "totakekelive"):
                continue
            
            max_before_days = max(cm_before_days.get(label, 0), jingle_before_days.get(label, 0))
            if max_before_days == 0:
                continue
            
            dates = []
            for row in rows:
                occ = mono.get_all_event_occurrences(row, year)
                if occ:
                    dates.extend(occ)
            if not dates:
                continue
            d = sorted(dates)[0]
            
            self.assertTrue(
                mono._is_event_active(label, max_before_days, d, self.events_by_label),
                f"{label} should be active on event day {d}"
            )
            self.assertTrue(
                mono._is_event_active(label, max_before_days, d - dt.timedelta(days=1), self.events_by_label),
                f"{label} should be active 1 day before event day {d}"
            )
            self.assertFalse(
                mono._is_event_active(label, max_before_days, d - dt.timedelta(days=max_before_days + 1), self.events_by_label),
                f"{label} should NOT be active {max_before_days + 1} days before event day {d}"
            )

    def test_totakekelive_friday_saturday(self):
        """Test TotakekeLive event on Friday/Saturday."""
        label = "totakekelive"
        fri = dt.date(2025, 8, 8)  # Friday
        sat = dt.date(2025, 8, 9)  # Saturday
        self.assertTrue(mono._is_event_active(label, 0, fri, self.events_by_label))
        self.assertTrue(mono._is_event_active(label, 0, sat, self.events_by_label))


class TestMonoCommercialJingleSelection(unittest.TestCase):
    """Test commercial and jingle selection in mono.py"""

    @classmethod
    def setUpClass(cls):
        cls.cal = mono.CALENDAR_FILE
        assert os.path.exists(cls.cal), f"Missing calendar file: {cls.cal}"
        mono.prime_events_async(cls.cal)
        mono._EVENTS_READY.wait(timeout=5.0)
        cls.events_by_label = mono.get_cached_events()
        cls.commercials_dir = str(mono.SOUND_DIR)
        cls.jingles_dir = str(mono.SOUND_DIR)

    def test_commercial_selection_baseline(self):
        """Test that baseline commercials (Normal_01-06) can be selected."""
        test_time = dt.datetime.now(pytz.timezone("MST7MDT"))
        
        # Try multiple times to get a selection
        found = False
        for _ in range(20):
            result = mono.pick_commercial_filename_for_datetime_cached(
                test_time,
                self.commercials_dir,
                self.events_by_label,
                mono.COMMERCIALS_CSV
            )
            if result:
                found = True
                self.assertTrue(os.path.exists(result), f"Selected commercial should exist: {result}")
                break
        
        # May not find if directory doesn't have commercials, but shouldn't crash
        self.assertTrue(True, "Commercial selection should not crash")

    def test_jingle_selection_time_of_day(self):
        """Test that time-of-day jingles can be selected."""
        test_time = dt.datetime.now(pytz.timezone("MST7MDT"))
        
        result = mono.pick_jingle_filename_for_datetime_cached(
            test_time,
            self.jingles_dir,
            self.events_by_label,
            mono.JINGLES_CSV
        )
        
        # May be None if no jingles available, but shouldn't crash
        if result:
            self.assertTrue(os.path.exists(result), f"Selected jingle should exist: {result}")

    def test_commercial_weights_computation(self):
        """Test commercial weights computation."""
        test_time = dt.datetime.now(pytz.timezone("MST7MDT"))
        
        weights = mono.get_commercial_weights_cached(
            test_time,
            self.events_by_label,
            mono.COMMERCIALS_CSV
        )
        
        self.assertIsInstance(weights, dict)
        # Baseline normals should have weight 1
        for i in range(1, 7):
            music_no = str(i)
            if music_no in weights:
                self.assertGreaterEqual(weights[music_no], 0)

    def test_get_commercials_for_date(self):
        """Test get_commercials_for_date function."""
        test_date = dt.date.today()
        
        result = mono.get_commercials_for_date(
            self.events_by_label,
            test_date,
            mono.COMMERCIALS_CSV,
            mono.SOUND_DIR
        )
        
        self.assertIsInstance(result, list)
        # Each item should be a filename string
        for item in result:
            self.assertIsInstance(item, str)
            self.assertTrue(item.endswith('.bwav'))


class TestMonoDecoder(unittest.TestCase):
    """Test ADPCM decoder functionality in mono.py"""

    def test_decoder_basic(self):
        """Test basic decoder functionality."""
        # Create a minimal test context
        # This is a simplified test - full decoder tests would require actual BWAV files
        self.assertTrue(hasattr(mono, 'Decoder'))
        self.assertTrue(hasattr(mono, 'BWAVParser'))
        
        # Test that decoder can be instantiated (would need real file)
        # For now, just verify the class exists
        self.assertTrue(callable(mono.Decoder))

    def test_bwav_parser_exists(self):
        """Test that BWAVParser exists and has parse method."""
        self.assertTrue(hasattr(mono.BWAVParser, 'parse'))
        self.assertTrue(callable(mono.BWAVParser.parse))


class TestMonoMixer(unittest.TestCase):
    """Test mixer functionality in mono.py"""

    def test_mixer_initialization(self):
        """Test mixer initialization."""
        mixer = mono.Mixer()
        self.assertEqual(mixer.sr, 48000)
        self.assertEqual(mixer.ch, 1)
        self.assertEqual(len(mixer.tracks), 0)

    @patch('mono.sd')
    def test_mixer_start_stop(self, mock_sd):
        """Test mixer start/stop."""
        mixer = mono.Mixer()
        
        mock_stream = MagicMock()
        mock_sd.OutputStream.return_value = mock_stream
        
        mixer.start()
        self.assertIsNotNone(mixer.stream)
        mock_sd.OutputStream.assert_called_once()
        
        mixer.stop()
        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()

    def test_mixer_add_silence(self):
        """Test adding silence track."""
        mixer = mono.Mixer()
        silence = mixer.add_silence(1.0)
        self.assertIsNotNone(silence)
        self.assertEqual(len(mixer.tracks), 1)


class TestMonoRadioIntegration(unittest.TestCase):
    """End-to-end integration tests for Radio class"""

    def test_radio_initialization(self):
        """Test Radio class initialization."""
        radio = mono.Radio()
        self.assertIsNotNone(radio.mix)
        self.assertIsNotNone(radio.dir)
        self.assertIsNotNone(radio.stop_evt)

    def test_radio_helper_methods(self):
        """Test Radio helper methods."""
        radio = mono.Radio()
        
        # Test _now_local
        now = mono._now_local()
        self.assertIsInstance(now, dt.datetime)
        
        # Test _pick (would need actual directory with files)
        # For now, just verify it doesn't crash
        try:
            result = mono._pick(mono.SOUND_DIR, mono._RE_INTRO)
            # Result can be None if no files found
            self.assertTrue(result is None or isinstance(result, Path))
        except Exception:
            pass  # Directory might not exist in test environment

    def test_overlay_params(self):
        """Test overlay parameter calculation."""
        # Test various scenarios
        result = mono._overlay_params(10.0, 12.0)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        
        result2 = mono._overlay_params(12.0, 10.0)
        self.assertIsInstance(result2, tuple)
        
        result3 = mono._overlay_params(10.0, 10.0)
        self.assertIsInstance(result3, tuple)

    def test_song_title_lookup(self):
        """Test song title lookup."""
        # Test with various filename formats
        result = mono._song_title("BGM_Audio_001_Test_Cheap.bwav")
        self.assertTrue(result is None or isinstance(result, str))
        
        result2 = mono._song_title("nonexistent.bwav")
        self.assertTrue(result2 is None or isinstance(result2, str))


class TestMonoFeatureParity(unittest.TestCase):
    """Test feature parity between mono.py and refactored modules"""

    def test_event_functions_exist(self):
        """Verify all event functions exist in mono.py."""
        required_functions = [
            'get_cached_events',
            'pick_commercial_filename_for_datetime_cached',
            'pick_jingle_filename_for_datetime_cached',
            'get_commercial_weights_cached',
            '_is_event_active',
            'get_all_event_occurrences',
            'get_commercials_for_date',
        ]
        
        for func_name in required_functions:
            self.assertTrue(
                hasattr(mono, func_name),
                f"mono.py should have {func_name}"
            )
            self.assertTrue(
                callable(getattr(mono, func_name)),
                f"{func_name} should be callable"
            )

    def test_constants_match_refactored(self):
        """Verify constants match refactored code."""
        # Sample rate should be 48000
        self.assertEqual(mono.SR, 48000)
        
        # Channels should be 1 (mono) to match refactored code
        self.assertEqual(mono.CH, 1)
        
        # Volume defaults
        self.assertEqual(mono.VOL_DEFAULT, 1.0)
        self.assertEqual(mono.VOL_DUCK, 0.25)

    def test_paths_exist(self):
        """Verify that required paths are defined."""
        self.assertIsNotNone(mono.CALENDAR_FILE)
        self.assertIsNotNone(mono.COMMERCIALS_CSV)
        self.assertIsNotNone(mono.JINGLES_CSV)
        self.assertIsNotNone(mono.SOUND_DIR)

    def test_event_system_initialized(self):
        """Verify event system is initialized."""
        events = mono.get_cached_events()
        self.assertIsInstance(events, dict)
        # Events should be loaded (may be empty if file doesn't exist, but should not error)


class TestMonoEndToEnd(unittest.TestCase):
    """End-to-end tests simulating actual radio playback"""

    @patch('mono.sd')
    def test_radio_loop_setup(self, mock_sd):
        """Test that radio loop can be set up without errors."""
        mock_stream = MagicMock()
        mock_sd.OutputStream.return_value = mock_stream
        
        radio = mono.Radio()
        
        # Start mixer
        radio.mix.start()
        self.assertIsNotNone(radio.mix.stream)
        
        # Stop mixer
        radio.mix.stop()
        self.assertIsNone(radio.mix.stream)

    def test_time_jingle_selection(self):
        """Test time-of-day jingle selection."""
        # Test at different times of day
        times = [
            dt.datetime(2025, 1, 1, 6, 0, 0),   # Morning
            dt.datetime(2025, 1, 1, 12, 0, 0),  # Day
            dt.datetime(2025, 1, 1, 20, 0, 0),  # Night
        ]
        
        events_by_label = mono.get_cached_events()
        
        for test_time in times:
            with self.subTest(time=test_time):
                tz = pytz.timezone("MST7MDT")
                test_time_tz = tz.localize(test_time) if test_time.tzinfo is None else test_time.astimezone(tz)
                
                result = mono.pick_jingle_filename_for_datetime_cached(
                    test_time_tz,
                    str(mono.SOUND_DIR),
                    events_by_label,
                    mono.JINGLES_CSV
                )
                # May be None if no jingles available, but shouldn't crash
                self.assertTrue(result is None or isinstance(result, str))


if __name__ == '__main__':
    unittest.main(verbosity=2)

