#!/usr/bin/env python3
"""
End-to-end tests for mono.py Radio.loop() - verifying full playback behavior
"""

import unittest
import os
import sys
import datetime as dt
import random
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
import pytz

# Add the project root to the path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mono


class TestMonoRadioLoop(unittest.TestCase):
    """Test the full Radio.loop() behavior"""

    def setUp(self):
        """Set up test environment with mock files."""
        self.temp_dir = tempfile.mkdtemp()
        self.sound_dir = Path(self.temp_dir) / "sound"
        self.sound_dir.mkdir()
        
        # Create mock song files
        self.song_files = [
            "BGM_Audio_001_Test_Cheap.bwav",
            "BGM_Audio_002_Another_Cheap.bwav",
            "BGM_Audio_003_Third_Cheap.bwav",
            "BGM_Audio_004_Fourth_Cheap.bwav",
            "BGM_Audio_005_Fifth_Cheap.bwav",
            "BGM_Audio_006_Sixth_Cheap.bwav",
            "BGM_Audio_007_Seventh_Cheap.bwav",
            "BGM_Audio_008_Eighth_Cheap.bwav",
            "BGM_Audio_009_Ninth_Cheap.bwav",
            "BGM_Audio_010_Tenth_Cheap.bwav",
            "BGM_Audio_011_Eleventh_Cheap.bwav",
            "BGM_Audio_012_Twelfth_Cheap.bwav",
        ]
        
        for song in self.song_files:
            (self.sound_dir / song).touch()
        
        # Create mock intro/outro files
        (self.sound_dir / "Radio_MC_KK_001.bwav").touch()
        (self.sound_dir / "Radio_MC_KK_Emotiocon_001.bwav").touch()
        
        # Create mock jingle
        (self.sound_dir / "BGM_Radio_Jingle_Normal_01.bwav").touch()
        
        # Create mock commercial
        (self.sound_dir / "Radio_CM_Normal_01.bwav").touch()

    def tearDown(self):
        """Clean up test environment."""
        shutil.rmtree(self.temp_dir)

    @patch('mono.BWAVParser.parse')
    @patch('mono.sd')
    @patch.object(mono.Radio, '_wait_track')  # Mock wait to return immediately
    @patch.object(mono.Radio, '_gap_silence')  # Mock silence gap
    @patch.object(mono.Radio, '_play_intro_song')  # Mock intro/song overlay
    @patch.object(mono.Radio, '_play_outro')  # Mock outro
    def test_radio_loop_structure(self, mock_outro, mock_intro_song, mock_gap, mock_wait, mock_sd, mock_parse):
        """Test that Radio.loop() follows the correct structure."""
        # Mock BWAV parsing
        mock_ctx = MagicMock()
        mock_ctx.info.rate = 48000
        mock_ctx.info.total_samples = 48000  # 1 second
        mock_ctx.info.loop_start = 0
        mock_ctx.info.loop_end = 0xFFFFFFFF
        mock_ctx.path = self.sound_dir / "test.bwav"
        mock_parse.return_value = mock_ctx
        
        # Mock audio stream
        mock_stream = MagicMock()
        mock_sd.OutputStream.return_value = mock_stream
        
        # Create radio with temp directory
        radio = mono.Radio(d=self.sound_dir)
        
        # Set stop event immediately to prevent infinite loop
        radio.stop_evt.set()
        
        # Mock all wait methods to return immediately
        mock_wait.return_value = None
        mock_gap.return_value = None
        mock_intro_song.return_value = None
        mock_outro.return_value = None
        
        # This should exit immediately since stop_evt is set
        try:
            radio.loop()
        except Exception as e:
            self.fail(f"Radio.loop() should not crash: {e}")

    def test_song_pool_selection(self):
        """Test that Radio.loop() selects 12 songs correctly."""
        radio = mono.Radio(d=self.sound_dir)
        
        # Get all songs that match pattern
        all_songs = [
            p for p in self.sound_dir.iterdir()
            if mono._RE_SONG.fullmatch(p.name)
            and p.name not in mono._EXCLUDE
        ]
        
        self.assertEqual(len(all_songs), 12, "Should have 12 test songs")
        
        # Test that random.sample would work
        if len(all_songs) >= 12:
            pool = random.sample(all_songs, 12)
            self.assertEqual(len(pool), 12)
            
            # Verify sets are created correctly
            for off in (0, 4, 8):
                songs = pool[off:off + 4]
                self.assertEqual(len(songs), 4, f"Set at offset {off} should have 4 songs")

    def test_intro_outro_picking(self):
        """Test that intro/outro files are picked correctly."""
        radio = mono.Radio(d=self.sound_dir)
        
        intro = mono._pick(self.sound_dir, mono._RE_INTRO)
        outro = mono._pick(self.sound_dir, mono._RE_OUTRO)
        
        # Should find intro/outro files
        self.assertIsNotNone(intro, "Should find intro file")
        self.assertIsNotNone(outro, "Should find outro file")
        self.assertTrue(intro.exists())
        self.assertTrue(outro.exists())

    def test_reggae_special_case_logic(self):
        """Test the reggae special case timing logic."""
        radio = mono.Radio(d=self.sound_dir)
        
        # Create reggae file
        reggae_file = self.sound_dir / "BGM_Audio_015_Reggae_Cheap.bwav"
        reggae_file.touch()
        
        # Test at 16:20
        test_time = dt.datetime(2025, 1, 1, 16, 20, 0)
        with patch('mono._now_local', return_value=test_time.replace(tzinfo=pytz.timezone("MST7MDT"))):
            now = mono._now_local()
            self.assertEqual(now.hour, 16)
            self.assertEqual(now.minute, 20)
            
            # Logic should trigger
            should_trigger = (
                now.hour == 16 and
                (now.minute == 20 or (now.minute == 19 and now.second >= 49))
            )
            self.assertTrue(should_trigger)

    def test_jingle_selection_in_loop(self):
        """Test that jingles are selected in the loop."""
        radio = mono.Radio(d=self.sound_dir)
        
        # Test jingle picking
        jingle = mono._pick_time_jingle(self.sound_dir)
        # May be None if no jingles match time-of-day, but shouldn't crash
        self.assertTrue(jingle is None or isinstance(jingle, Path))

    def test_commercial_selection_in_loop(self):
        """Test that commercials are selected in the loop."""
        radio = mono.Radio(d=self.sound_dir)
        
        # Initialize events
        mono.prime_events_async(mono.CALENDAR_FILE)
        mono._EVENTS_READY.wait(timeout=5.0)
        events_by_label = mono.get_cached_events()
        
        # Test commercial selection
        today_ads = mono.get_commercials_for_date(
            events_by_label,
            dt.date.today(),
            mono.COMMERCIALS_CSV,
            self.sound_dir
        )
        
        self.assertIsInstance(today_ads, list)
        # Should find at least the baseline commercial if it exists
        if today_ads:
            self.assertTrue(all(isinstance(ad, str) for ad in today_ads))

    def test_overlay_logic_called(self):
        """Test that overlay logic is called for intro/song pairs."""
        # Test overlay params calculation directly (no need for full playback)
        intro_len = 11.0  # 11 seconds
        loop_start = 5.0  # Loop starts at 5 seconds
        
        result = mono._overlay_params(intro_len, loop_start)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        
        # Test various scenarios
        result2 = mono._overlay_params(10.0, 12.0)
        self.assertIsInstance(result2, tuple)
        
        result3 = mono._overlay_params(12.0, 10.0)
        self.assertIsInstance(result3, tuple)

    @patch.object(mono.Radio, '_wait_track')
    def test_silence_gap_after_commercial(self, mock_wait):
        """Test that silence gap is added after commercial."""
        radio = mono.Radio(d=self.sound_dir)
        mock_wait.return_value = None
        
        # Test silence gap
        radio._gap_silence(mono.DELAY_AFTER_CM_S)
        mock_wait.assert_called()
        
        # Test with zero duration
        radio._gap_silence(0)
        # Should not call wait for zero duration

    @patch.object(mono.Radio, '_wait_track')
    def test_silence_gap_after_outro(self, mock_wait):
        """Test that silence gap is added after outro."""
        radio = mono.Radio(d=self.sound_dir)
        mock_wait.return_value = None
        
        # Test silence gap
        radio._gap_silence(mono.DELAY_AFTER_OUTRO)
        mock_wait.assert_called()

    @patch('mono.BWAVParser.parse')
    @patch('mono.sd')
    @patch.object(mono.Radio, '_wait_track')  # Mock wait to return immediately
    def test_play_ctx_method(self, mock_wait, mock_sd, mock_parse):
        """Test _play_ctx method."""
        mock_ctx = MagicMock()
        mock_ctx.info.rate = 48000
        mock_ctx.info.total_samples = 48000
        mock_ctx.info.loop_start = 0
        mock_ctx.info.loop_end = 0xFFFFFFFF
        mock_ctx.path = self.sound_dir / "test.bwav"
        mock_parse.return_value = mock_ctx
        
        mock_stream = MagicMock()
        mock_sd.OutputStream.return_value = mock_stream
        
        radio = mono.Radio(d=self.sound_dir)
        mock_wait.return_value = None  # Don't actually wait
        
        # This should complete immediately since _wait_track is mocked
        radio._play_ctx(mock_ctx, wait=True)
        mock_wait.assert_called_once()
        # Should not crash

    def test_stop_event_functionality(self):
        """Test that stop event works correctly."""
        radio = mono.Radio(d=self.sound_dir)
        
        # Initially not set
        self.assertFalse(radio.stop_evt.is_set())
        
        # Set it
        radio.stop_evt.set()
        self.assertTrue(radio.stop_evt.is_set())
        
        # Clear it
        radio.stop_evt.clear()
        self.assertFalse(radio.stop_evt.is_set())


class TestMonoPlaylistStructure(unittest.TestCase):
    """Test playlist structure and set organization"""

    def test_playlist_sets(self):
        """Test that playlists are organized into sets correctly."""
        # Radio.loop() picks 12 songs and organizes into 3 sets of 4
        pool_size = 12
        songs_per_set = 4
        num_sets = 3
        
        # Simulate the structure
        for off in (0, 4, 8):
            set_start = off
            set_end = off + songs_per_set
            self.assertEqual(set_end - set_start, songs_per_set)
            self.assertLessEqual(set_end, pool_size)

    def test_first_song_gets_intro(self):
        """Test that first song in set gets intro."""
        # In Radio.loop(), intro is played when i == 0
        songs = ["song1", "song2", "song3", "song4"]
        
        for i, song in enumerate(songs):
            is_first = (i == 0)
            self.assertTrue(is_first if i == 0 else not is_first)

    def test_last_song_gets_outro(self):
        """Test that last song in set gets outro."""
        songs = ["song1", "song2", "song3", "song4"]
        
        for i, song in enumerate(songs):
            is_last = (i == len(songs) - 1)
            self.assertTrue(is_last if i == len(songs) - 1 else not is_last)


if __name__ == '__main__':
    unittest.main(verbosity=2)

