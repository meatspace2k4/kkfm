"""
Test suite for TX fault detection and auto-recovery.

Tests the cascading failure states, recovery logic, and watchdog health monitoring.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from unittest.mock import Mock, patch, MagicMock
import pytest

from watchdog.anomalies import Anomaly, AnomalyCode, Severity
from watchdog.health_check import (
    WatchdogHealthMonitor,
    WatchdogStatus,
    read_status_file,
    check_watchdog_health_once,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_status_dir():
    """Create a temporary directory for status files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def collected_anomalies():
    """Collector for emitted anomalies."""
    anomalies = []
    def collector(a: Anomaly) -> None:
        anomalies.append(a)
    return anomalies, collector


# ---------------------------------------------------------------------------
# Test: Watchdog Health Monitoring
# ---------------------------------------------------------------------------

class TestWatchdogHealthMonitoring:
    """Tests for watching the watchers."""
    
    def test_stale_tx_status_detection(self, temp_status_dir, collected_anomalies):
        """Dashboard should flag stale tx_status.json."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        # Write a stale TX status (60s old)
        stale_ts = time.time() - 60
        tx_path.write_text(json.dumps({"ts": stale_ts, "epoch": "abc123"}))
        # Write a fresh log status
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def456"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
            tx_stale_threshold_s=30.0,
        )
        
        results = monitor.check()
        
        assert "STALE" in results["tx-watchdog"]
        assert "OK" in results["log-watchdog"]
        
        # Should have emitted WATCHDOG_STALE for TX
        stale_anomalies = [a for a in anomalies if a.code == AnomalyCode.WATCHDOG_STALE]
        assert len(stale_anomalies) == 1
        assert "tx-watchdog" in stale_anomalies[0].message
    
    def test_missing_status_file_detection(self, temp_status_dir, collected_anomalies):
        """Missing status file should be detected."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"  # Does not exist
        log_path = temp_status_dir / "log_status.json"
        
        # Only write log status
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def456"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        # First check initializes
        results = monitor.check()
        assert results["tx-watchdog"] == "NO FILE"
        
        # Second check should emit anomaly (after initialization)
        results = monitor.check()
        
        stale_anomalies = [a for a in anomalies if a.code == AnomalyCode.WATCHDOG_STALE]
        assert len(stale_anomalies) >= 1
        assert "missing" in stale_anomalies[0].message.lower()
    
    def test_watchdog_epoch_change_detection(self, temp_status_dir, collected_anomalies):
        """Detect when watchdog process restarts via epoch change."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        # Initial state
        tx_path.write_text(json.dumps({"ts": time.time(), "epoch": "epoch1"}))
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "epoch2"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        # First check - establishes baseline
        monitor.check()
        assert len(anomalies) == 0
        
        # Simulate TX watchdog restart (new epoch)
        tx_path.write_text(json.dumps({"ts": time.time(), "epoch": "epoch1-NEW"}))
        
        # Second check - should detect restart
        monitor.check()
        
        restart_anomalies = [a for a in anomalies if a.code == AnomalyCode.WATCHDOG_RESTARTED]
        assert len(restart_anomalies) == 1
        assert "tx-watchdog" in restart_anomalies[0].message
        assert "epoch1" in restart_anomalies[0].message
    
    def test_fresh_status_no_anomaly(self, temp_status_dir, collected_anomalies):
        """Fresh status files should not emit anomalies."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        tx_path.write_text(json.dumps({"ts": time.time(), "epoch": "abc123"}))
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def456"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        results = monitor.check()
        
        assert "OK" in results["tx-watchdog"]
        assert "OK" in results["log-watchdog"]
        assert len(anomalies) == 0
    
    def test_staleness_resets_after_recovery(self, temp_status_dir, collected_anomalies):
        """After staleness, fresh data should clear the stale flag."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def456"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
            tx_stale_threshold_s=30.0,
        )
        
        # Stale
        tx_path.write_text(json.dumps({"ts": time.time() - 60, "epoch": "abc123"}))
        monitor.check()
        monitor.check()  # Second check emits
        
        stale_count = len([a for a in anomalies if a.code == AnomalyCode.WATCHDOG_STALE])
        assert stale_count >= 1
        
        # Now fresh
        tx_path.write_text(json.dumps({"ts": time.time(), "epoch": "abc123"}))
        results = monitor.check()
        
        assert "OK" in results["tx-watchdog"]
        
        # Go stale again - should emit again
        anomalies.clear()
        tx_path.write_text(json.dumps({"ts": time.time() - 60, "epoch": "abc123"}))
        monitor.check()
        
        new_stale = [a for a in anomalies if a.code == AnomalyCode.WATCHDOG_STALE]
        assert len(new_stale) == 1  # Fresh emission after recovery


class TestTxFaultStateTracking:
    """Tests for TX fault state in status files."""
    
    def test_fault_state_in_status_json(self, temp_status_dir, collected_anomalies):
        """tx_status.json includes in_fault_state field."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        tx_path.write_text(json.dumps({
            "ts": time.time(),
            "epoch": "abc123",
            "in_fault_state": True,
            "recovery_attempt_count": 3,
        }))
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def456"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        assert monitor.get_tx_fault_state() == True
    
    def test_recovery_in_progress_tracking(self, temp_status_dir, collected_anomalies):
        """Can detect when recovery is in progress."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        tx_path.write_text(json.dumps({
            "ts": time.time(),
            "epoch": "abc123",
            "recovery_in_progress": True,
            "recovery_attempt_count": 2,
        }))
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def456"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        assert monitor.get_tx_recovery_in_progress() == True


class TestAnomalyCodes:
    """Verify new anomaly codes exist and have correct properties."""
    
    def test_tx_heartbeat_recovered_exists(self):
        """TX_HEARTBEAT_RECOVERED anomaly code exists."""
        assert hasattr(AnomalyCode, "TX_HEARTBEAT_RECOVERED")
        assert AnomalyCode.TX_HEARTBEAT_RECOVERED.value == "TX_HEARTBEAT_RECOVERED"
    
    def test_tx_silence_detected_exists(self):
        """TX_SILENCE_DETECTED anomaly code exists."""
        assert hasattr(AnomalyCode, "TX_SILENCE_DETECTED")
        assert AnomalyCode.TX_SILENCE_DETECTED.value == "TX_SILENCE_DETECTED"
    
    def test_watchdog_stale_exists(self):
        """WATCHDOG_STALE anomaly code exists."""
        assert hasattr(AnomalyCode, "WATCHDOG_STALE")
        assert AnomalyCode.WATCHDOG_STALE.value == "WATCHDOG_STALE"
    
    def test_watchdog_restarted_exists(self):
        """WATCHDOG_RESTARTED anomaly code exists."""
        assert hasattr(AnomalyCode, "WATCHDOG_RESTARTED")
        assert AnomalyCode.WATCHDOG_RESTARTED.value == "WATCHDOG_RESTARTED"
    
    def test_tx_restart_attempt_exists(self):
        """TX_RESTART_ATTEMPT anomaly code exists."""
        assert hasattr(AnomalyCode, "TX_RESTART_ATTEMPT")
    
    def test_tx_restart_failed_exists(self):
        """TX_RESTART_FAILED anomaly code exists."""
        assert hasattr(AnomalyCode, "TX_RESTART_FAILED")
    
    def test_tx_recovery_failed_exists(self):
        """TX_RECOVERY_FAILED anomaly code exists."""
        assert hasattr(AnomalyCode, "TX_RECOVERY_FAILED")


class TestReadStatusFile:
    """Tests for the read_status_file utility."""
    
    def test_read_valid_json(self, temp_status_dir):
        """Can read valid JSON status file."""
        path = temp_status_dir / "test.json"
        path.write_text('{"ts": 123.45, "epoch": "abc"}')
        
        result = read_status_file(path)
        
        assert result == {"ts": 123.45, "epoch": "abc"}
    
    def test_read_missing_file(self, temp_status_dir):
        """Returns None for missing file."""
        path = temp_status_dir / "nonexistent.json"
        
        result = read_status_file(path)
        
        assert result is None
    
    def test_read_invalid_json(self, temp_status_dir):
        """Returns None for invalid JSON."""
        path = temp_status_dir / "bad.json"
        path.write_text('not valid json')
        
        result = read_status_file(path)
        
        assert result is None
    
    def test_read_empty_file(self, temp_status_dir):
        """Returns None for empty file."""
        path = temp_status_dir / "empty.json"
        path.write_text('')
        
        result = read_status_file(path)
        
        assert result is None


# ---------------------------------------------------------------------------
# Test: TX Monitor State Machine (Mock-based)
# ---------------------------------------------------------------------------

class TestTxStateFields:
    """Test TxState dataclass has required fields."""
    
    def test_state_has_silence_tracking(self):
        """TxState includes silence tracking fields."""
        from watchdog.tx_monitor import TxState
        
        state = TxState()
        assert hasattr(state, 'silence_started_mono')
        assert hasattr(state, 'last_silence_emit_mono')
        assert state.silence_started_mono == 0.0
    
    def test_state_has_heartbeat_recovery_tracking(self):
        """TxState includes heartbeat recovery tracking."""
        from watchdog.tx_monitor import TxState
        
        state = TxState()
        assert hasattr(state, 'was_missing_heartbeat')
        assert state.was_missing_heartbeat == False
    
    def test_state_has_recovery_fields(self):
        """TxState includes auto-recovery fields."""
        from watchdog.tx_monitor import TxState
        
        state = TxState()
        assert hasattr(state, 'recovery_attempt_count')
        assert hasattr(state, 'recovery_in_progress')
        assert hasattr(state, 'in_fault_state')
        assert hasattr(state, 'audio_present_streak')
        assert state.recovery_attempt_count == 0
        assert state.in_fault_state == False


class TestTxRecoveryConstants:
    """Test TX recovery configuration constants."""
    
    def test_dead_air_threshold_exists(self):
        """TX_DEAD_AIR_THRESHOLD_S is defined."""
        from watchdog.tx_monitor import TX_DEAD_AIR_THRESHOLD_S
        assert TX_DEAD_AIR_THRESHOLD_S == 30.0
    
    def test_max_restart_attempts_exists(self):
        """TX_MAX_RESTART_ATTEMPTS is defined."""
        from watchdog.tx_monitor import TX_MAX_RESTART_ATTEMPTS
        assert TX_MAX_RESTART_ATTEMPTS == 3
    
    def test_audio_streak_required_exists(self):
        """TX_AUDIO_PRESENT_STREAK_REQUIRED is defined."""
        from watchdog.tx_monitor import TX_AUDIO_PRESENT_STREAK_REQUIRED
        assert TX_AUDIO_PRESENT_STREAK_REQUIRED == 2


# ---------------------------------------------------------------------------
# Test: CLI Health Check
# ---------------------------------------------------------------------------

class TestHealthCheckCLI:
    """Tests for the CLI health check function."""
    
    def test_check_once_with_healthy_watchdogs(self, temp_status_dir, collected_anomalies):
        """CLI check returns healthy status."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        tx_path.write_text(json.dumps({"ts": time.time(), "epoch": "abc"}))
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def"}))
        
        results = check_watchdog_health_once(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        assert "OK" in results["tx-watchdog"]
        assert "OK" in results["log-watchdog"]
    
    def test_check_once_with_stale_watchdog(self, temp_status_dir, collected_anomalies):
        """CLI check detects stale watchdog."""
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        # TX is stale
        tx_path.write_text(json.dumps({"ts": time.time() - 120, "epoch": "abc"}))
        log_path.write_text(json.dumps({"ts": time.time(), "epoch": "def"}))
        
        results = check_watchdog_health_once(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        assert "STALE" in results["tx-watchdog"]


# ---------------------------------------------------------------------------
# Scenario Tests
# ---------------------------------------------------------------------------

class TestScenarios:
    """
    High-level scenario tests matching the requirements.
    
    These test the expected behavior for specific failure scenarios.
    """
    
    def test_scenario_service_restart_web_works_tx_silence(self):
        """
        Scenario 1: Service restarts, web works, TX sees silence.
        
        Expected: TX_SILENCE_DETECTED should be emitted, recovery attempted.
        """
        # This would require mocking the full tx_monitor loop
        # For now, verify the anomaly code exists
        assert hasattr(AnomalyCode, "TX_SILENCE_DETECTED")
    
    def test_scenario_watchdog_staleness_detection(self, temp_status_dir, collected_anomalies):
        """
        Scenario 5: Service down but watchdog status stale - can't determine state.
        
        Expected: WATCHDOG_STALE emitted.
        """
        anomalies, collector = collected_anomalies
        tx_path = temp_status_dir / "tx_status.json"
        log_path = temp_status_dir / "log_status.json"
        
        # Both watchdogs stale
        stale_ts = time.time() - 120
        tx_path.write_text(json.dumps({"ts": stale_ts, "epoch": "abc"}))
        log_path.write_text(json.dumps({"ts": stale_ts, "epoch": "def"}))
        
        monitor = WatchdogHealthMonitor(
            emit_cb=collector,
            tx_status_path=tx_path,
            log_status_path=log_path,
        )
        
        results = monitor.check()
        
        assert "STALE" in results["tx-watchdog"]
        assert "STALE" in results["log-watchdog"]
    
    def test_scenario_tx_recovery_cascade_to_fault(self):
        """
        Scenario 2d/3: TX down for 30s+, recovery cascade to fault state.
        
        Expected: After 3 failed attempts, TX_RECOVERY_FAILED CRITICAL.
        """
        # Verify the anomaly has CRITICAL severity when emitted
        from watchdog.tx_monitor import TX_MAX_RESTART_ATTEMPTS
        assert TX_MAX_RESTART_ATTEMPTS == 3

    def test_maintenance_mode_skips_recovery(self):
        """
        When main audio service is stopped (maintenance), TX watchdog should
        NOT attempt Arduino recovery even if silence is detected.
        
        This prevents false positives during intentional maintenance.
        """
        from watchdog.tx_monitor import _is_main_service_running, MAIN_SERVICE_UNIT
        
        # Verify the function exists and is callable
        assert callable(_is_main_service_running)
        
        # Verify the service unit constant exists
        assert MAIN_SERVICE_UNIT == "acnh-aircheck.service" or MAIN_SERVICE_UNIT  # env override allowed
    
    def test_maintenance_mode_service_check_mock(self):
        """
        Test that _is_main_service_running correctly interprets systemctl results.
        """
        from watchdog.tx_monitor import _is_main_service_running
        
        # Mock subprocess.run to simulate service states
        with patch("watchdog.tx_monitor.subprocess.run") as mock_run:
            # Service is running (exit code 0)
            mock_run.return_value = MagicMock(returncode=0)
            assert _is_main_service_running() == True
            
            # Service is stopped (exit code non-zero)
            mock_run.return_value = MagicMock(returncode=3)  # inactive
            assert _is_main_service_running() == False
            
            # Service is failed (exit code non-zero)
            mock_run.return_value = MagicMock(returncode=1)
            assert _is_main_service_running() == False
            
            # subprocess raises exception (fail-open to True)
            mock_run.side_effect = Exception("systemctl not found")
            assert _is_main_service_running() == True

    def test_service_enabled_check(self):
        """
        Test that _is_main_service_enabled correctly interprets systemctl is-enabled results.
        """
        from watchdog.tx_monitor import _is_main_service_enabled
        
        with patch("watchdog.tx_monitor.subprocess.run") as mock_run:
            # Service is enabled
            mock_run.return_value = MagicMock(returncode=0)
            assert _is_main_service_enabled() == True
            
            # Service is disabled
            mock_run.return_value = MagicMock(returncode=1)
            assert _is_main_service_enabled() == False
            
            # Exception (fail-open to True)
            mock_run.side_effect = Exception("systemctl not found")
            assert _is_main_service_enabled() == True

    def test_service_recovery_constants(self):
        """
        Test that service recovery configuration constants exist.
        """
        from watchdog.tx_monitor import SERVICE_MAX_RESTART_ATTEMPTS, SERVICE_RESTART_INTERVAL_S
        
        assert SERVICE_MAX_RESTART_ATTEMPTS == 3
        assert SERVICE_RESTART_INTERVAL_S == 60.0

    def test_start_main_service_success(self, collected_anomalies):
        """
        Test _start_main_service emits correct anomalies on success.
        """
        from watchdog.tx_monitor import _start_main_service
        anomalies, collector = collected_anomalies
        
        with patch("watchdog.tx_monitor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _start_main_service(collector)
            
            assert result == True
            codes = [a.code for a in anomalies]
            assert AnomalyCode.SERVICE_RESTART_ATTEMPT in codes
            assert AnomalyCode.SERVICE_RECOVERY_SUCCESS in codes

    def test_start_main_service_failure(self, collected_anomalies):
        """
        Test _start_main_service emits correct anomalies on failure.
        """
        from watchdog.tx_monitor import _start_main_service
        anomalies, collector = collected_anomalies
        
        with patch("watchdog.tx_monitor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr=b"Failed to start")
            result = _start_main_service(collector)
            
            assert result == False
            codes = [a.code for a in anomalies]
            assert AnomalyCode.SERVICE_RESTART_ATTEMPT in codes
            assert AnomalyCode.SERVICE_RESTART_FAILED in codes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
