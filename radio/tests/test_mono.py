#!/usr/bin/env python3
"""
Unit tests for mono.py - the monolith implementation
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import datetime as dt
import pytz

# Add the project root to the path so we can import mono
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import mono


class TestMonoBasics(unittest.TestCase):
    """Test basic functionality of mono.py"""

    def test_imports(self):
        """Test that mono.py imports successfully."""
        self.assertTrue(hasattr(mono, 'Radio'))
        self.assertTrue(hasattr(mono, 'BWAVParser'))
        self.assertTrue(hasattr(mono, 'Mixer'))
        self.assertTrue(hasattr(mono, 'Decoder'))

    def test_constants(self):
        """Test that constants are defined correctly."""
        self.assertEqual(mono.SR, 48000)
        self.assertEqual(mono.CH, 1)  # Updated to match refactored code
        self.assertEqual(mono.VOL_DEFAULT, 1.0)
        self.assertEqual(mono.VOL_DUCK, 0.25)

    def test_radio_initialization(self):
        """Test that Radio class can be instantiated."""
        radio = mono.Radio()
        self.assertIsNotNone(radio.mix)
        self.assertIsNotNone(radio.dir)
        self.assertIsNotNone(radio.stop_evt)

    def test_event_system_initialization(self):
        """Test that event system functions are available."""
        self.assertTrue(callable(mono.get_cached_events))
        self.assertTrue(callable(mono.pick_commercial_filename_for_datetime_cached))
        self.assertTrue(callable(mono.pick_jingle_filename_for_datetime_cached))
        self.assertTrue(callable(mono._is_event_active))

    def test_event_loading(self):
        """Test that events can be loaded."""
        events = mono.get_cached_events()
        self.assertIsInstance(events, dict)
        # Events should be loaded (may be empty if file doesn't exist, but should not error)

    def test_commercial_selection(self):
        """Test commercial selection logic."""
        events_by_label = mono.get_cached_events()
        test_time = dt.datetime.now(pytz.timezone("MST7MDT"))
        
        # Should not crash even if no commercials directory exists
        result = mono.pick_commercial_filename_for_datetime_cached(
            test_time,
            str(mono.SOUND_DIR),
            events_by_label,
            mono.COMMERCIALS_CSV
        )
        # Result can be None if no commercials found, but should not error
        self.assertTrue(result is None or isinstance(result, str))

    def test_jingle_selection(self):
        """Test jingle selection logic."""
        events_by_label = mono.get_cached_events()
        test_time = dt.datetime.now(pytz.timezone("MST7MDT"))
        
        # Should not crash even if no jingles directory exists
        result = mono.pick_jingle_filename_for_datetime_cached(
            test_time,
            str(mono.SOUND_DIR),
            events_by_label,
            mono.JINGLES_CSV
        )
        # Result can be None if no jingles found, but should not error
        self.assertTrue(result is None or isinstance(result, str))

    def test_mixer_initialization(self):
        """Test that Mixer can be initialized."""
        mixer = mono.Mixer()
        self.assertEqual(mixer.sr, 48000)
        self.assertEqual(mixer.ch, 1)
        self.assertEqual(len(mixer.tracks), 0)

    def test_song_title_lookup(self):
        """Test song title lookup."""
        # Test with a non-existent file (should return None)
        result = mono._song_title("nonexistent.bwav")
        self.assertTrue(result is None or isinstance(result, str))

    def test_now_local(self):
        """Test timezone conversion."""
        now = mono._now_local()
        self.assertIsInstance(now, dt.datetime)
        self.assertIsNotNone(now.tzinfo)

    def test_clamp16(self):
        """Test 16-bit clamping."""
        self.assertEqual(mono._clamp16(32767), 32767)
        self.assertEqual(mono._clamp16(-32768), -32768)
        self.assertEqual(mono._clamp16(50000), 32767)
        self.assertEqual(mono._clamp16(-50000), -32768)

    def test_overlay_params(self):
        """Test overlay parameter calculation."""
        # Test various scenarios
        result = mono._overlay_params(10.0, 12.0)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    def test_get_commercials_for_date(self):
        """Test get_commercials_for_date function."""
        events_by_label = mono.get_cached_events()
        test_date = dt.date.today()
        
        result = mono.get_commercials_for_date(
            events_by_label,
            test_date,
            mono.COMMERCIALS_CSV,
            mono.SOUND_DIR
        )
        self.assertIsInstance(result, list)
        # Should not crash even if no commercials found

    @patch('mono.sd')
    def test_mixer_start_stop(self, mock_sd):
        """Test mixer start/stop without actually starting audio."""
        mixer = mono.Mixer()
        
        # Mock the OutputStream
        mock_stream = MagicMock()
        mock_sd.OutputStream.return_value = mock_stream
        
        mixer.start()
        self.assertIsNotNone(mixer.stream)
        mock_sd.OutputStream.assert_called_once()
        
        mixer.stop()
        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

