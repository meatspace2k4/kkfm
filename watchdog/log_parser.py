from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .ansi import strip_ansi
from .events import Event, EventType


JINGLE_PICK_RE = re.compile(r"\[RADIO\]\s+Jingle picked:\s+(?P<name>.+?)\s+\(size=")
JINGLE_START_RE = re.compile(r"\[RADIO\]\s+Jingle start:\s+(?P<name>.+)$")
JINGLE_END_RE = re.compile(r"\[RADIO\]\s+Jingle end:\s+(?P<name>.+)$")
JINGLE_FAIL_RE = re.compile(r"\[RADIO\]\s+Jingle failed to load:\s+(?P<name>.+)$")

COMM_PICK_RE = re.compile(r"\[RADIO\]\s+Commercial picked:\s+(?P<name>.+?)\s+\(size=")
COMM_START_RE = re.compile(r"\[RADIO\]\s+Commercial start:\s+(?P<name>.+)$")
COMM_END_RE = re.compile(r"\[RADIO\]\s+Commercial end:\s+(?P<name>.+)$")
COMM_FAIL_RE = re.compile(r"\[RADIO\]\s+Commercial failed to load:\s+(?P<name>.+)$")

NOW_PLAYING_RE = re.compile(r"Now Playing:\s+(?P<name>.+)$")

# systemd lifecycle messages (included in `journalctl -u <unit>`) often look like:
#   "Started <Description>."
#   "Stopped <Description>."
SYSTEMD_UNIT_STARTED_RE = re.compile(r"^Started\s+.+", re.IGNORECASE)
SYSTEMD_UNIT_STOPPED_RE = re.compile(r"^Stopped\s+.+", re.IGNORECASE)

# Operator-defined safe shutdown marker. The radio service should log this before
# exiting for maintenance/testing. If we observe a subsequent systemd "Started"
# without seeing this marker first, we treat it as an unexpected/crash restart.
SAFE_SHUTDOWN_RE = re.compile(r"shutdown\s+reason:\s*(?P<reason>.+)", re.IGNORECASE)

DAC_OVERFLOW_RE = re.compile(r"\[DAC\].*Ring buffer overflow")
DAC_THROTTLE_RE = re.compile(r"\[DAC\].*throttling producer", re.IGNORECASE)
DAC_UNDERRUN_RE = re.compile(r"\[DAC\].*Ring buffer underrun", re.IGNORECASE)

SHORT_ISO_PREFIX_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{4}))\s+(?P<rest>.*)$"
)


def _parse_short_iso(ts: str) -> float:
    """Parse `journalctl -o short-iso` timestamps.

    Examples:
      - 2025-12-11T21:35:25-0700
      - 2025-12-11T21:35:25.123456-0700
      - 2025-12-11T21:35:25Z

    `datetime.fromisoformat()` handles fractional seconds, but prefers `Z` -> `+00:00`
    and timezone offsets with a colon (`-07:00`).
    """

    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    else:
        # Normalize trailing offsets like -0700 / +0130 to -07:00 / +01:30
        # (journalctl short-iso format)
        m = re.search(r"([+-])(\d{2})(\d{2})$", s)
        if m:
            s = s[: m.start()] + f"{m.group(1)}{m.group(2)}:{m.group(3)}"

    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Should not happen for the formats we accept, but keep a safe fallback.
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.timestamp()
    except Exception:
        # Fallback for older/odd formats (no fractional seconds).
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
        return dt.timestamp()


def parse_event(line: str, default_ts: float) -> Optional[Event]:
    """Parse a journal line into a typed event.

    - Supports optional `short-iso` timestamp prefix.
    - Strips ANSI sequences.
    """
    s = strip_ansi(line).strip()

    ts = default_ts
    m = SHORT_ISO_PREFIX_RE.match(s)
    if m:
        try:
            ts = _parse_short_iso(m.group("ts"))
        except Exception:
            ts = default_ts
        s = m.group("rest")

    # Normalize "ERROR: " prefix present in some prints.
    if s.startswith("ERROR: "):
        s2 = s[len("ERROR: ") :]
    else:
        s2 = s

    # Service lifecycle (systemd) events.
    if SYSTEMD_UNIT_STARTED_RE.match(s2):
        return Event(EventType.UNIT_STARTED, ts=ts, raw=s, name=None)
    if SYSTEMD_UNIT_STOPPED_RE.match(s2):
        return Event(EventType.UNIT_STOPPED, ts=ts, raw=s, name=None)

    m = SAFE_SHUTDOWN_RE.search(s2)
    if m:
        reason = (m.group("reason") or "").strip() or None
        return Event(EventType.SAFE_SHUTDOWN, ts=ts, raw=s, name=reason)

    m = JINGLE_PICK_RE.search(s2)
    if m:
        return Event(EventType.JINGLE_PICKED, ts=ts, raw=s, name=m.group("name").strip())
    m = JINGLE_START_RE.search(s2)
    if m:
        return Event(EventType.JINGLE_START, ts=ts, raw=s, name=m.group("name").strip())
    m = JINGLE_END_RE.search(s2)
    if m:
        return Event(EventType.JINGLE_END, ts=ts, raw=s, name=m.group("name").strip())
    m = JINGLE_FAIL_RE.search(s2)
    if m:
        return Event(EventType.JINGLE_FAILED, ts=ts, raw=s, name=m.group("name").strip())

    m = COMM_PICK_RE.search(s2)
    if m:
        return Event(EventType.COMM_PICKED, ts=ts, raw=s, name=m.group("name").strip())
    m = COMM_START_RE.search(s2)
    if m:
        return Event(EventType.COMM_START, ts=ts, raw=s, name=m.group("name").strip())
    m = COMM_END_RE.search(s2)
    if m:
        return Event(EventType.COMM_END, ts=ts, raw=s, name=m.group("name").strip())
    m = COMM_FAIL_RE.search(s2)
    if m:
        return Event(EventType.COMM_FAILED, ts=ts, raw=s, name=m.group("name").strip())

    m = NOW_PLAYING_RE.search(s2)
    if m:
        return Event(EventType.NOW_PLAYING, ts=ts, raw=s, name=m.group("name").strip())

    if DAC_OVERFLOW_RE.search(s2):
        return Event(EventType.DAC_OVERFLOW, ts=ts, raw=s, name=None)
    if DAC_THROTTLE_RE.search(s2):
        return Event(EventType.DAC_THROTTLE, ts=ts, raw=s, name=None)
    if DAC_UNDERRUN_RE.search(s2):
        return Event(EventType.DAC_UNDERRUN, ts=ts, raw=s, name=None)

    return None


