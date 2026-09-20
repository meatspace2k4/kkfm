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

    # Hourly BGM fork ([HOURLY] lines)
    BGM_PICKED = "bgm_picked"            # name = BGM_24Hour_HH_<Weather>.bwav
    BGM_CHANGEOVER = "bgm_changeover"    # name = file being faded; raw carries "HH -> HH"
    BGM_END = "bgm_end"                  # name = file that finished its fade
    BGM_FAILED = "bgm_failed"            # name = path that failed to load
    WEATHER_FETCHED = "weather_fetched"  # name = Sunny | Rainy | Snowy
    WEATHER_FAILED = "weather_failed"


@dataclass(frozen=True)
class Event:
    etype: EventType
    ts: float  # seconds since epoch OR monotonic seconds (caller-defined)
    raw: str
    name: Optional[str] = None


