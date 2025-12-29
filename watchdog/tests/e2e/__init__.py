"""
E2E Test Suite for KKFM Watchdog System

This package provides end-to-end testing infrastructure that integrates:
- Log monitoring and anomaly detection
- TX monitoring (with mock serial)
- Dashboard TUI (headless mode with file output)
- Auto-restart recovery logic

The test harness uses event injection to trigger anomalies and verify
they flow through the entire system correctly.
"""

from .harness import E2ETestHarness, MockSerial, MockSystemctl
from .injectors import EventInjector, AnomalyTrigger

__all__ = [
    "E2ETestHarness",
    "MockSerial",
    "MockSystemctl",
    "EventInjector",
    "AnomalyTrigger",
]
