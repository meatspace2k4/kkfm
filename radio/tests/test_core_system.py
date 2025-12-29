# -*- coding: utf-8 -*-
"""
Tests for the refactored core system
"""

import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import sounddevice as sd

from radio.core.content_manager import ContentManager
from radio.core.audio_engine import AudioEngine
from radio.core.scheduler import EventScheduler
from radio.core.overlay import OverlayManager
from radio.core.config_manager import ConfigManager


def audio_hardware_available():
    """Check if audio hardware is available for testing."""
    try:
        devices = sd.query_devices()
        output_devices = [d for d in devices if d['max_output_channels'] > 0]
        return len(output_devices) > 0
    except Exception:
        return False


class TestContentManager(unittest.TestCase):
    def setUp(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.sound_path = Path(self.temp_dir) / "sound"
        self.sound_path.mkdir()

        # Create test audio files - songs and jingles/commercials, no intro/outro files to break the test
        test_files = [
            "BGM_Audio_001_Test_Cheap.bwav",  # Valid song
            "BGM_Audio_002_Another_Cheap.bwav",  # Valid song
            "BGM_Radio_Jingle_Normal_01.bwav",
            "Radio_CM_Normal_01.bwav",
            # Add required intro/outro fixtures to avoid false negatives
            "Radio_MC_KK_001.bwav",
            "Radio_MC_KK_Emotiocon_001.bwav"
        ]

        for filename in test_files:
            (self.sound_path / filename).touch()

        self.content_manager = ContentManager(str(self.sound_path))

    def tearDown(self):
        """Clean up test environment."""
        shutil.rmtree(self.temp_dir)

    def test_get_all_songs(self):
        """Test song discovery."""
        songs = self.content_manager.get_all_songs()
        self.assertEqual(len(songs), 2)  # Only BGM_Audio files
        self.assertIn("BGM_Audio_001_Test_Cheap.bwav", songs[0])
        self.assertIn("BGM_Audio_002_Another_Cheap.bwav", songs[1])

    def test_pick_songs(self):
        """Test song selection."""
        songs = self.content_manager.pick_songs(1)
        self.assertEqual(len(songs), 1)

        songs = self.content_manager.pick_songs(5)  # More than available
        self.assertEqual(len(songs), 2)  # Limited to available

    def test_pick_intro_outro(self):
        """Test intro/outro selection."""
        intro = self.content_manager.pick_intro()
        outro = self.content_manager.pick_outro()

        # In a real radio system, intro and outro files should be available
        # This test should fail because we're not providing them - exposing the broken test
        self.assertIsNotNone(intro, "Radio system should have intro files available")
        self.assertIsNotNone(outro, "Radio system should have outro files available")
        self.assertTrue(intro.endswith("Radio_MC_KK_001.bwav") if intro else False, f"Wrong intro file: {intro}")
        self.assertTrue(outro.endswith("Radio_MC_KK_Emotiocon_001.bwav") if outro else False, f"Wrong outro file: {outro}")

    def test_validate_file(self):
        """Test file validation."""
        valid_file = str(self.sound_path / "BGM_Audio_001_Test_Cheap.bwav")
        invalid_file = str(self.sound_path / "nonexistent.bwav")

        self.assertTrue(self.content_manager.validate_file(valid_file))
        self.assertFalse(self.content_manager.validate_file(invalid_file))
        self.assertFalse(self.content_manager.validate_file(""))

    def test_get_file_info(self):
        """Test file information retrieval."""
        valid_file = str(self.sound_path / "BGM_Audio_001_Test_Cheap.bwav")
        info = self.content_manager.get_file_info(valid_file)

        self.assertIn("path", info)
        self.assertIn("filename", info)
        self.assertEqual(info["filename"], "BGM_Audio_001_Test_Cheap.bwav")

        # Invalid file should return empty dict
        self.assertEqual(self.content_manager.get_file_info(""), {})


class TestConfigManager(unittest.TestCase):
    def setUp(self):
        """Set up test configuration."""
        self.config_manager = ConfigManager()

    def test_get_defaults(self):
        """Test default configuration values."""
        self.assertEqual(self.config_manager.get("sample_rate"), 48000)
        self.assertEqual(self.config_manager.get("channels"), 1)
        self.assertTrue(self.config_manager.get("debug"))

    def test_environment_override(self):
        """Test environment variable overrides."""
        with patch.dict('os.environ', {'AIRCHECK_DEBUG': 'false'}):
            config = ConfigManager()
            self.assertFalse(config.get("debug"), "Environment variable AIRCHECK_DEBUG=false should set debug=False")

        with patch.dict('os.environ', {'AIRCHECK_SAMPLE_RATE': '44100'}):
            config = ConfigManager()
            self.assertEqual(config.get("sample_rate"), 44100, "Environment variable AIRCHECK_SAMPLE_RATE=44100 should set sample_rate=44100")

        # Test that invalid environment values are handled
        with patch.dict('os.environ', {'AIRCHECK_SAMPLE_RATE': 'invalid'}):
            with self.assertRaises(Exception):
                ConfigManager()

    def test_config_validation(self):
        """Test configuration validation."""
        # Valid config should not raise
        self.assertIsNotNone(self.config_manager.get("sample_rate"))

        # Test invalid sample rate (too low)
        with self.assertRaises(Exception):
            config = ConfigManager()
            config.set("sample_rate", 1000)  # Too low
            config._validate_config()

        # Test invalid sample rate (too high)
        with self.assertRaises(Exception):
            config = ConfigManager()
            config.set("sample_rate", 200000)  # Too high
            config._validate_config()

        # Test invalid channels
        with self.assertRaises(Exception):
            config = ConfigManager()
            config.set("channels", 3)  # Invalid channel count
            config._validate_config()

        # Test that valid values work
        config = ConfigManager()
        config.set("sample_rate", 44100)
        config.set("channels", 2)
        config._validate_config()  # Should not raise

    def test_path_configuration(self):
        """Test path configuration."""
        paths = self.config_manager.get_paths()
        self.assertIn("sound_files_path", paths)
        self.assertIn("calendar_table", paths)

    def test_audio_settings(self):
        """Test audio settings extraction."""
        audio = self.config_manager.get_audio_settings()
        self.assertIn("sample_rate", audio)
        self.assertIn("channels", audio)


class TestOverlayManager(unittest.TestCase):
    def setUp(self):
        """Set up overlay manager."""
        self.overlay_manager = OverlayManager()

    def test_snap_to_beat_grid(self):
        """Test beat grid snapping."""
        # This test should actually validate beat grid functionality
        # Currently it passes even when the method is broken!

        # Test with a real audio file (should work)
        result = self.overlay_manager.snap_to_beat_grid(2.5, "BGM_Audio_001_Test_Cheap.bwav", 1.0, 8.0)
        self.assertIsInstance(result, float, "Should return a float")
        self.assertGreaterEqual(result, 1.0, "Should be at least minimum delay")
        self.assertLessEqual(result, 8.0, "Should be at most maximum delay")

        # Test edge cases that should be handled
        result_zero = self.overlay_manager.snap_to_beat_grid(0, "nonexistent.bwav")
        self.assertEqual(result_zero, 1.0, "Should fallback to minimum delay for invalid files")

    def test_should_use_overlay(self):
        """Test overlay decision logic."""
        # Test with invalid inputs (should return False)
        self.assertFalse(self.overlay_manager.should_use_overlay(0, 0))
        self.assertFalse(self.overlay_manager.should_use_overlay(-1, 5))
        self.assertFalse(self.overlay_manager.should_use_overlay(5, 0))

        # Test with boundary cases that should work
        self.assertTrue(self.overlay_manager.should_use_overlay(1, 2), "Should allow minimal overlay (delta=1)")
        self.assertTrue(self.overlay_manager.should_use_overlay(5, 8), "Should allow reasonable overlay (delta=3)")
        self.assertFalse(self.overlay_manager.should_use_overlay(15, 3), "Should reject overlay when intro is too long")

        # Test that the method actually uses the delta calculation
        # Delta = song_lead_in - intro_duration
        # Should use overlay when 1 <= delta <= 13 (based on config)
        self.assertTrue(self.overlay_manager.should_use_overlay(2, 10), "Delta=8 should allow overlay")
        self.assertTrue(self.overlay_manager.should_use_overlay(2, 15), "Delta=13 should allow overlay")
        self.assertFalse(self.overlay_manager.should_use_overlay(2, 16), "Delta=14 should reject overlay")


class TestEventScheduler(unittest.TestCase):
    def setUp(self):
        """Set up event scheduler."""
        self.scheduler = EventScheduler()

    def test_initialization(self):
        """Test scheduler initialization."""
        self.assertFalse(self.scheduler.wait_for_events(timeout=0.1))  # No events loaded yet

    def test_time_info(self):
        """Test time information."""
        info = self.scheduler.get_current_time_info()
        self.assertIn("current_time", info)
        self.assertIn("game_date", info)
        self.assertIn("timezone", info)


class TestAudioEngine(unittest.TestCase):
    def setUp(self):
        """Set up audio engine."""
        self.audio_engine = AudioEngine(48000, 1)

    def test_initialization(self):
        """Test audio engine initialization."""
        self.assertFalse(self.audio_engine.is_running())
        self.assertEqual(self.audio_engine.sample_rate, 48000)
        self.assertEqual(self.audio_engine.channels, 1)

    @unittest.skipIf(not audio_hardware_available(), "Audio hardware not available")
    def test_start_stop(self):
        """Test start and stop functionality."""
        # This test should actually validate audio engine behavior
        # Currently it passes even when the engine is broken

        # Reset the audio engine state
        self.audio_engine._running = False

        # Test start - should actually start the engine
        result = self.audio_engine.start()
        # This should validate that the engine actually started
        self.assertTrue(result, "Audio engine should start successfully")
        self.assertTrue(self.audio_engine.is_running(), "Engine should report as running")

        # Test stop - should actually stop the engine
        self.audio_engine.stop()
        self.assertFalse(self.audio_engine.is_running(), "Engine should report as stopped")

        # Test that stopping an already stopped engine doesn't break
        self.audio_engine.stop()
        self.assertFalse(self.audio_engine.is_running())

    def test_load_audio_context(self):
        """Test audio context loading with a real BWAV file."""
        # Test with the silent BWAV file
        silent_file = "./radio/Sound/Resource/Stream/BGM_Audio_000_Silent_Cheap.bwav"
        result = self.audio_engine.load_audio_context(silent_file)

        # Should successfully load and return an AudioContext
        self.assertIsNotNone(result)
        self.assertEqual(result.filename, "radio/Sound/Resource/Stream/BGM_Audio_000_Silent_Cheap.bwav")  # Path normalized

        # Test with invalid file
        result_invalid = self.audio_engine.load_audio_context("nonexistent.bwav")
        self.assertIsNone(result_invalid)

        # Test with empty string
        result_empty = self.audio_engine.load_audio_context("")
        self.assertIsNone(result_empty)

    def test_stats(self):
        """Test statistics retrieval."""
        stats = self.audio_engine.get_stats()
        self.assertIn("running", stats)
        self.assertIn("sample_rate", stats)
        self.assertIn("channels", stats)


class TestIntegration(unittest.TestCase):
    """Integration tests for the core system components."""

    def setUp(self):
        """Set up integration test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.sound_path = Path(self.temp_dir) / "sound"
        self.sound_path.mkdir()

        # Create minimal test files
        (self.sound_path / "BGM_Audio_001_Test_Cheap.bwav").touch()
        (self.sound_path / "Radio_MC_KK_001.bwav").touch()
        (self.sound_path / "Radio_CM_Normal_01.bwav").touch()
        (self.sound_path / "BGM_Radio_Jingle_Normal_01.bwav").touch()

    def tearDown(self):
        """Clean up integration test environment."""
        shutil.rmtree(self.temp_dir)

    def test_full_system_initialization(self):
        """Test that all components can be initialized and work together."""
        from radio.core.radio_player import RadioPlayer

        audio_engine = AudioEngine(48000, 1)
        content_manager = ContentManager(str(self.sound_path))
        scheduler = EventScheduler()
        radio_player = RadioPlayer(audio_engine, content_manager, scheduler)

        # Test that components are properly connected
        self.assertEqual(radio_player.audio_engine, audio_engine)
        self.assertEqual(radio_player.content_manager, content_manager)
        self.assertEqual(radio_player.scheduler, scheduler)

        # Test that content manager actually found files
        all_songs = content_manager.get_all_songs()
        self.assertEqual(len(all_songs), 1, "Should find exactly 1 song in test directory")

        # Test that scheduler loaded events
        events = scheduler.get_events()
        self.assertIsNotNone(events, "Scheduler should load event data")

        # Test status reporting includes actual data
        status = radio_player.get_status()
        self.assertIn("running", status)
        self.assertIn("audio_engine", status)
        self.assertIn("content_manager", status)
        self.assertEqual(status["content_manager"]["available_songs"], 1, "Status should report correct song count")

        # Test that audio engine can load one of the test files
        test_song = all_songs[0]
        context = audio_engine.load_audio_context(test_song)
        # Placeholder files are not valid BWAV; loader should fail gracefully
        self.assertIsNone(context, "Audio engine should gracefully fail for placeholder test song")

    def test_content_discovery(self):
        """Test content discovery across all types."""
        content_manager = ContentManager(str(self.sound_path))

        songs = content_manager.get_all_songs()
        self.assertEqual(len(songs), 1)

        # Test that intro/outro detection works
        intro = content_manager.pick_intro()
        self.assertIsNotNone(intro)

        commercial = content_manager.pick_commercial()
        # Commercial might be None if no calendar/events, that's OK

        jingle = content_manager.pick_jingle()
        # Jingle might be None if no calendar/events, that's OK


if __name__ == '__main__':
    unittest.main()
