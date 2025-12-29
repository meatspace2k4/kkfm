from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    UNIT_STARTED = "unit_started"
    UNIT_STOPPED = "unit_stopped"

    SAFE_SHUTDOWN = "safe_shutdown"

    NOW_PLAYING = "now_playing"

    JINGLE_PICKED = "jingle_picked"
    JINGLE_START = "jingle_start"
    JINGLE_END = "jingle_end"
    JINGLE_FAILED = "jingle_failed"

    COMM_PICKED = "commercial_picked"
    COMM_START = "commercial_start"
    COMM_END = "commercial_end"
    COMM_FAILED = "commercial_failed"

    DAC_OVERFLOW = "dac_overflow"
    DAC_THROTTLE = "dac_throttle"
    DAC_UNDERRUN = "dac_underrun"


@dataclass(frozen=True)
class Event:
    etype: EventType
    ts: float  # seconds since epoch OR monotonic seconds (caller-defined)
    raw: str
    name: Optional[str] = None


