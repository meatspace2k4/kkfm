"""
Health check for watchdog services (watching the watchers).

This module monitors the status files written by the watchdog services
and detects when they go stale (indicating the watchdog may be hung/dead)
or when their epoch changes (indicating a restart).

Can be run standalone or integrated into the dashboard.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, Dict

from .anomalies import Anomaly, AnomalyCode, Severity
from .paths import get_runtime_status_dir


@dataclass
class WatchdogStatus:
    name: str  # "tx" or "log"
    status_file: Path
    last_ts: float = 0.0
    last_epoch: Optional[str] = None
    stale_emitted: bool = False  # Avoid spam
    restart_epoch: Optional[str] = None  # Track last emitted restart


# Default staleness thresholds
DEFAULT_TX_STALE_S = 30.0  # TX watchdog writes every ~1s
DEFAULT_LOG_STALE_S = 60.0  # Log watchdog may write less frequently


def _mk(
    code: AnomalyCode,
    severity: Severity,
    now_ts: float,
    message: str,
) -> Anomaly:
    return Anomaly(code=code, severity=severity, ts=now_ts, message=message)


def read_status_file(path: Path) -> Optional[dict]:
    """Read a JSON status file, returning None on any error."""
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except Exception:
        return None


class WatchdogHealthMonitor:
    """
    Monitor watchdog status files for staleness and restarts.
    
    Usage:
        monitor = WatchdogHealthMonitor(emit_cb=reporter.emit)
        while True:
            monitor.check()
            time.sleep(5)
    """
    
    def __init__(
        self,
        emit_cb: Callable[[Anomaly], None],
        tx_status_path: Optional[Path] = None,
        log_status_path: Optional[Path] = None,
        tx_stale_threshold_s: float = DEFAULT_TX_STALE_S,
        log_stale_threshold_s: float = DEFAULT_LOG_STALE_S,
    ):
        self.emit_cb = emit_cb
        self.tx_stale_threshold_s = tx_stale_threshold_s
        self.log_stale_threshold_s = log_stale_threshold_s
        
        # Find status files in runtime dir if not specified
        runtime_dir = get_runtime_status_dir()
        
        self.tx_status = WatchdogStatus(
            name="tx-watchdog",
            status_file=tx_status_path or runtime_dir / "tx_status.json",
        )
        self.log_status = WatchdogStatus(
            name="log-watchdog", 
            status_file=log_status_path or runtime_dir / "log_status.json",
        )
        
        # Track epochs for restart detection
        self._initialized = False
    
    def check(self) -> Dict[str, str]:
        """
        Check all watchdog status files.
        
        Returns a dict of watchdog name -> status string for display.
        Emits anomalies for staleness or restarts.
        """
        now_ts = time.time()
        results = {}
        
        for ws, stale_threshold in [
            (self.tx_status, self.tx_stale_threshold_s),
            (self.log_status, self.log_stale_threshold_s),
        ]:
            status = read_status_file(ws.status_file)
            
            if status is None:
                # File doesn't exist or is unreadable
                results[ws.name] = "NO FILE"
                if self._initialized and not ws.stale_emitted:
                    ws.stale_emitted = True
                    self.emit_cb(
                        _mk(
                            AnomalyCode.WATCHDOG_STALE,
                            Severity.ERROR,
                            now_ts,
                            message=(
                                f"{ws.name} status file missing or unreadable: {ws.status_file} "
                                f"(watchdog may not be running)"
                            ),
                        )
                    )
                continue
            
            # Parse timestamp
            try:
                file_ts = float(status.get("ts", 0))
            except Exception:
                file_ts = 0.0
            
            age_s = now_ts - file_ts
            ws.last_ts = file_ts
            
            # Check for staleness
            if age_s > stale_threshold:
                results[ws.name] = f"STALE ({age_s:.0f}s)"
                if not ws.stale_emitted:
                    ws.stale_emitted = True
                    self.emit_cb(
                        _mk(
                            AnomalyCode.WATCHDOG_STALE,
                            Severity.ERROR,
                            now_ts,
                            message=(
                                f"{ws.name} status file is stale: last update {age_s:.1f}s ago "
                                f"(threshold: {stale_threshold:.0f}s). Watchdog may be hung or dead."
                            ),
                        )
                    )
            else:
                results[ws.name] = f"OK ({age_s:.1f}s)"
                ws.stale_emitted = False  # Reset for next staleness event
            
            # Check for epoch change (restart detection)
            epoch = status.get("epoch")
            if isinstance(epoch, str) and epoch:
                if ws.last_epoch is None:
                    # First time seeing this watchdog
                    ws.last_epoch = epoch
                    ws.restart_epoch = epoch
                elif epoch != ws.last_epoch:
                    # Epoch changed = watchdog restarted
                    old_epoch = ws.last_epoch
                    ws.last_epoch = epoch
                    
                    # Only emit if we haven't already emitted for this epoch
                    if ws.restart_epoch != epoch:
                        ws.restart_epoch = epoch
                        self.emit_cb(
                            _mk(
                                AnomalyCode.WATCHDOG_RESTARTED,
                                Severity.WARN,
                                now_ts,
                                message=(
                                    f"{ws.name} restarted (epoch changed from {old_epoch[:8]}... "
                                    f"to {epoch[:8]}...)"
                                ),
                            )
                        )
        
        self._initialized = True
        return results
    
    def get_tx_fault_state(self) -> Optional[bool]:
        """Check if TX watchdog reports being in fault state."""
        status = read_status_file(self.tx_status.status_file)
        if status is None:
            return None
        return status.get("in_fault_state", False)
    
    def get_tx_recovery_in_progress(self) -> bool:
        """Check if TX watchdog is currently attempting recovery."""
        status = read_status_file(self.tx_status.status_file)
        if status is None:
            return False
        return status.get("recovery_in_progress", False)


def check_watchdog_health_once(
    emit_cb: Callable[[Anomaly], None],
    tx_status_path: Optional[Path] = None,
    log_status_path: Optional[Path] = None,
) -> Dict[str, str]:
    """
    One-shot health check for all watchdogs.
    
    Useful for CLI health check commands or periodic cron jobs.
    """
    monitor = WatchdogHealthMonitor(
        emit_cb=emit_cb,
        tx_status_path=tx_status_path,
        log_status_path=log_status_path,
    )
    return monitor.check()


if __name__ == "__main__":
    # Simple CLI for testing
    import sys
    
    def print_anomaly(a: Anomaly) -> None:
        print(f"[{a.severity.value.upper()}] {a.code.value}: {a.message}")
    
    results = check_watchdog_health_once(emit_cb=print_anomaly)
    
    print("\nWatchdog Status:")
    for name, status in results.items():
        print(f"  {name}: {status}")
    
    # Exit with error if any issues
    if any("STALE" in s or "NO FILE" in s for s in results.values()):
        sys.exit(1)
    sys.exit(0)
