"""
Headless Dashboard for Testing

Provides a file-based output mode for the dashboard that can be used
in automated tests without a real terminal.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class DashboardSnapshot:
    """A snapshot of dashboard state at a point in time."""
    ts: float
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    tx_status: Optional[dict[str, Any]] = None
    log_status: Optional[dict[str, Any]] = None
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    ssh_state: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "services": self.services,
            "tx_status": self.tx_status,
            "log_status": self.log_status,
            "anomalies": self.anomalies,
            "ssh_state": self.ssh_state,
            "errors": self.errors,
        }


class HeadlessDashboard:
    """
    Headless dashboard that reads status files and outputs to a file.

    This can be used for:
    1. Testing dashboard logic without a terminal
    2. Running as a background service that writes JSON snapshots
    3. Feeding dashboard data to external monitoring systems
    """

    def __init__(
        self,
        out_dir: Path,
        output_file: Path,
        *,
        refresh_s: float = 1.0,
        max_anomalies: int = 10,
    ) -> None:
        self.out_dir = out_dir
        self.output_file = output_file
        self.refresh_s = refresh_s
        self.max_anomalies = max_anomalies
        self._running = False

    def take_snapshot(self) -> DashboardSnapshot:
        """Take a snapshot of current dashboard state."""
        snap = DashboardSnapshot(ts=time.time())

        # Read TX status
        tx_path = self.out_dir / "tx_status.json"
        if tx_path.exists():
            try:
                snap.tx_status = json.loads(tx_path.read_text())
            except Exception as e:
                snap.errors.append(f"tx_status: {e}")

        # Read log status
        log_path = self.out_dir / "log_status.json"
        if log_path.exists():
            try:
                snap.log_status = json.loads(log_path.read_text())
            except Exception as e:
                snap.errors.append(f"log_status: {e}")

        # Read recent anomalies
        anomalies_path = self.out_dir / "anomalies.jsonl"
        if anomalies_path.exists():
            try:
                lines = anomalies_path.read_text().splitlines()[-self.max_anomalies:]
                for line in lines:
                    if line.strip():
                        snap.anomalies.append(json.loads(line))
            except Exception as e:
                snap.errors.append(f"anomalies: {e}")

        return snap

    def write_snapshot(self, snapshot: Optional[DashboardSnapshot] = None) -> None:
        """Write a snapshot to the output file."""
        if snapshot is None:
            snapshot = self.take_snapshot()

        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.output_file.write_text(
            json.dumps(snapshot.to_dict(), indent=2),
            encoding="utf-8",
        )

    def run_once(self) -> DashboardSnapshot:
        """Take a snapshot and write it to file."""
        snap = self.take_snapshot()
        self.write_snapshot(snap)
        return snap


def create_headless_dashboard(
    out_dir: str,
    output_file: str,
    *,
    refresh_s: float = 1.0,
) -> HeadlessDashboard:
    """Factory function to create a headless dashboard."""
    return HeadlessDashboard(
        out_dir=Path(out_dir),
        output_file=Path(output_file),
        refresh_s=refresh_s,
    )
