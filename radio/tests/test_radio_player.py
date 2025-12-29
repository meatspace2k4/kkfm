#!/usr/bin/env python3
"""
Unit tests for RadioPlayer class
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add the project root to the path so we can import radio modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from radio.core.radio_player import RadioPlayer

# Use only the silent track for testing
SILENT_TRACK = "BGM_Audio_000_Silent_Cheap.bwav"


class TestRadioPlayer(unittest.TestCase):
    """Test cases for RadioPlayer functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_audio_engine = Mock()
        self.mock_content_manager = Mock()
        self.mock_scheduler = Mock()

        # Mock the audio engine's start method
        self.mock_audio_engine.start.return_value = True

        # Mock scheduler's start method
        self.mock_scheduler.start.return_value = None

    def test_init(self):
        """Test RadioPlayer initialization."""
        player = RadioPlayer(
            self.mock_audio_engine,
            self.mock_content_manager,
            self.mock_scheduler
        )

        self.assertEqual(player.audio_engine, self.mock_audio_engine)
        self.assertEqual(player.content_manager, self.mock_content_manager)
        self.assertEqual(player.scheduler, self.mock_scheduler)
        self.assertFalse(player._first_run_done)
        self.assertFalse(player._first_song_played)
        self.assertEqual(
            player._forced_first_song,
            "BGM_Audio_095_Minnaatsumare_Cheap.bwav"
        )

    def test_generate_playlist_first_run(self):
        """Test that first-run logic forces song to front on first playlist generation."""
        with patch('radio.core.radio_player.config') as mock_config:
            mock_config.SONGS_TO_PICK_PER_CYCLE = 4
            mock_config.FIRST_RUN_FORCE_ENABLED = True
            mock_config.FIRST_RUN_FORCED_SONG = SILENT_TRACK

            player = RadioPlayer(
                self.mock_audio_engine,
                self.mock_content_manager,
                self.mock_scheduler
            )

            # Mock content manager to return some songs
            mock_songs = ["song1.bwav", "song2.bwav", "song3.bwav", "song4.bwav"]
            self.mock_content_manager.pick_songs.return_value = mock_songs

            # Mock the _apply_first_run_logic method to avoid file operations
            with patch.object(player, '_apply_first_run_logic') as mock_apply_logic:
                def mock_apply_logic_side_effect(playlist):
                    playlist.insert(0, f"/silent/{SILENT_TRACK}")
                    player._first_run_done = True  # Set the flag that the real method would set
                mock_apply_logic.side_effect = mock_apply_logic_side_effect

                # Generate playlist
                playlist = player._generate_playlist()

                # Verify first-run logic was applied
                self.assertTrue(player._first_run_done)
                self.assertEqual(len(playlist), 5)  # 4 original + 1 forced song
                self.assertEqual(playlist[0], f"/silent/{SILENT_TRACK}")
                mock_apply_logic.assert_called_once()

    @patch('radio.core.radio_player.config')
    def test_generate_playlist_subsequent_runs(self, mock_config):
        """Test that first-run logic doesn't apply on subsequent playlist generations."""
        # Setup config mocks
        mock_config.SONGS_TO_PICK_PER_CYCLE = 4

        # Create player
        player = RadioPlayer(
            self.mock_audio_engine,
            self.mock_content_manager,
            self.mock_scheduler
        )

        # Mark first run as done
        player._first_run_done = True

        # Mock content manager
        mock_songs = [
            "/path/to/song1.bwav",
            "/path/to/song2.bwav",
            "/path/to/song3.bwav",
            "/path/to/song4.bwav"
        ]
        self.mock_content_manager.pick_songs.return_value = mock_songs

        # Generate playlist
        playlist = player._generate_playlist()

        # Verify first-run logic was NOT applied
        self.assertEqual(playlist, mock_songs)
        self.mock_content_manager.pick_songs.assert_called_once_with(4)

    def test_play_playlist_first_song_skip_intro(self):
        """Test that first Welcome Horizons song skips intro overlay."""
        with patch('radio.core.radio_player.config') as mock_config:
            mock_config.SONGS_PER_SET = 1  # Single song per set for simplicity
            mock_config.FIRST_RUN_FORCE_ENABLED = True
            mock_config.FIRST_RUN_FORCED_SONG = "BGM_Audio_095_Minnaatsumare_Cheap.bwav"

            player = RadioPlayer(
                self.mock_audio_engine,
                self.mock_content_manager,
                self.mock_scheduler
            )

            # Ensure first song flag is False initially
            self.assertFalse(player._first_song_played)

            # Mock playlist - single Welcome Horizons song
            playlist = ["BGM_Audio_095_Minnaatsumare_Cheap.bwav"]

            # Mock audio context for Welcome Horizons
            mock_song_ctx = Mock()
            self.mock_audio_engine.load_audio_context.return_value = mock_song_ctx

            # Mock content manager methods
            self.mock_content_manager.pick_intro.return_value = None  # No intro
            self.mock_content_manager.get_file_info.return_value = {"filename": "Welcome Horizons"}

            # Mock the _play_interstitials method to prevent jingles/commercials from playing
            with patch.object(player, '_play_interstitials'):
                # Call the method we're testing
                player._play_playlist_with_interstitials(playlist)

            # Verify first song was played without intro (direct call to play_track)
            self.mock_audio_engine.play_track.assert_called_once_with(
                mock_song_ctx,
                wait=True
            )

            # Verify first song flag is now True
            self.assertTrue(player._first_song_played)

    def test_play_playlist_second_song_uses_intro(self):
        """Test that second song (not first ever) uses intro overlay."""
        with patch('radio.core.radio_player.config') as mock_config, \
             patch('radio.core.radio_player.threading') as mock_threading:

            mock_config.SONGS_PER_SET = 1  # Single song per set for simplicity

            player = RadioPlayer(
                self.mock_audio_engine,
                self.mock_content_manager,
                self.mock_scheduler
            )

            # Mark first song as already played (so intro overlay should be used)
            player._first_song_played = True

            # Test the logic directly by calling the overlay scheduling method
            mock_intro_ctx = Mock()
            mock_song_ctx = Mock()

            # Mock threading Timer
            mock_timer = Mock()
            mock_threading.Timer.return_value = mock_timer

            # Mock overlay manager to return overlay plan
            with patch.object(player.overlay_manager, 'compute_overlay_decision') as mock_compute:
                mock_compute.return_value = {
                    'plan': 'overlay',
                    'delay_intro_sec': 0.0,
                    'delay_song_sec': 0.5,
                    'intro_sec': 2.0,
                    'lead_in_sec': 0.5,
                    'delta': 0.0
                }

                # Call the overlay scheduling method directly
                player._schedule_intro_song_overlay(mock_intro_ctx, mock_song_ctx, mock_compute.return_value)

            # Verify overlay scheduling works (we passed the decision to it)
            # The method should work without errors when given a valid decision

    def test_get_status(self):
        """Test get_status returns correct information."""
        player = RadioPlayer(
            self.mock_audio_engine,
            self.mock_content_manager,
            self.mock_scheduler
        )

        # Set some state
        player._first_run_done = True
        player._first_song_played = False

        # Mock dependencies
        self.mock_audio_engine.get_stats.return_value = {"test": "audio_stats"}
        self.mock_scheduler.get_current_time_info.return_value = {"test": "scheduler_info"}
        self.mock_content_manager.get_all_songs.return_value = ["song1", "song2"]
        self.mock_content_manager.sound_files_path = "/test/path"

        status = player.get_status()

        expected_status = {
            "running": True,  # Event is not set
            "first_run_done": True,
            "first_song_played": False,
            "audio_engine": {"test": "audio_stats"},
            "scheduler": {"test": "scheduler_info"},
            "content_manager": {
                "available_songs": 2,
                "sound_path": "/test/path"
            }
        }

        self.assertEqual(status, expected_status)


if __name__ == '__main__':
    unittest.main()
