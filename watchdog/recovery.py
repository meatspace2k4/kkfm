from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Tuple

from .anomalies import Anomaly, AnomalyCode, Severity


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = str(raw).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except Exception:
        return default


@dataclass(frozen=True)
class AutoRestartConfig:
    enabled: bool
    unit: str
    restart_on_dead_air: bool
    restart_on_underrun: bool
    underrun_count: int
    underrun_window_s: float
    cooldown_s: float
    max_per_hour: int

    @staticmethod
    def from_env(*, unit: str) -> "AutoRestartConfig":
        return AutoRestartConfig(
            enabled=_env_bool("WATCHDOG_AUTO_RESTART", False),
            unit=unit,
            restart_on_dead_air=_env_bool("WATCHDOG_AUTO_RESTART_DEAD_AIR", True),
            restart_on_underrun=_env_bool("WATCHDOG_AUTO_RESTART_UNDERRUN", True),
            underrun_count=max(1, _env_int("WATCHDOG_AUTO_RESTART_UNDERRUN_COUNT", 10)),
            underrun_window_s=max(1.0, _env_float("WATCHDOG_AUTO_RESTART_UNDERRUN_WINDOW_S", 120.0)),
            cooldown_s=max(0.0, _env_float("WATCHDOG_AUTO_RESTART_COOLDOWN_S", 600.0)),
            max_per_hour=max(0, _env_int("WATCHDOG_AUTO_RESTART_MAX_PER_HOUR", 3)),
        )


RestartFn = Callable[[str], Tuple[bool, str]]


def _systemctl_restart(unit: str) -> Tuple[bool, str]:
    try:
        p = subprocess.run(
            ["systemctl", "restart", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20.0,
        )
        out = (p.stdout or "").strip()
        return (p.returncode == 0), out
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class AutoRestarter:
    """Restarts a systemd unit when watchdog detects persistent failures.

    This is intentionally conservative: it rate-limits restarts to avoid thrashing.

    Triggers:
    - RADIO_DEAD_AIR (critical): restart immediately (subject to cooldown/limits)
    - DAC_RING_UNDERRUN (warn): restart only after N occurrences in a window
    """

    def __init__(self, cfg: AutoRestartConfig, *, restart_fn: Optional[RestartFn] = None) -> None:
        self.cfg = cfg
        self._restart_fn: RestartFn = restart_fn or _systemctl_restart
        self._underrun_ts: Deque[float] = deque()
        self._restart_ts: Deque[float] = deque()
        self._last_restart_ts: float = 0.0

    def _prune(self, now_ts: float) -> None:
        # Underrun window
        w = self.cfg.underrun_window_s
        while self._underrun_ts and (now_ts - self._underrun_ts[0]) > w:
            self._underrun_ts.popleft()

        # Restart rate window (1h)
        while self._restart_ts and (now_ts - self._restart_ts[0]) > 3600.0:
            self._restart_ts.popleft()

    def _can_restart(self, now_ts: float) -> bool:
        if not self.cfg.enabled:
            return False

        self._prune(now_ts)

        if self.cfg.cooldown_s > 0 and (now_ts - self._last_restart_ts) < self.cfg.cooldown_s:
            return False

        if self.cfg.max_per_hour > 0 and len(self._restart_ts) >= self.cfg.max_per_hour:
            return False

        return True

    def _do_restart(self, *, now_ts: float, trigger: Anomaly) -> tuple[bool, str]:
        ok, out = self._restart_fn(self.cfg.unit)
        self._last_restart_ts = now_ts
        self._restart_ts.append(now_ts)
        return ok, out

    def consider(self, a: Anomaly) -> Optional[Anomaly]:
        """Observe an anomaly and maybe trigger a restart.

        Returns an Anomaly describing the restart action (or None).
        """
        if not self.cfg.enabled:
            return None

        now_ts = float(getattr(a, "ts", time.time()))
        self._prune(now_ts)

        trigger_reason = None

        if self.cfg.restart_on_dead_air and a.code == AnomalyCode.RADIO_DEAD_AIR:
            trigger_reason = "dead_air"

        if self.cfg.restart_on_underrun and a.code == AnomalyCode.DAC_RING_UNDERRUN:
            self._underrun_ts.append(now_ts)
            self._prune(now_ts)
            if len(self._underrun_ts) >= self.cfg.underrun_count:
                trigger_reason = "underrun"

        if not trigger_reason:
            return None

        if not self._can_restart(now_ts):
            return Anomaly(
                code=AnomalyCode.AIRCHECK_AUTO_RESTART_SUPPRESSED,
                severity=Severity.WARN,
                ts=now_ts,
                message=(
                    f"Auto-restart suppressed (cooldown/limit). trigger={trigger_reason} "
                    f"unit={self.cfg.unit}"
                ),
                raw=a.raw,
            )

        ok, out = self._do_restart(now_ts=now_ts, trigger=a)
        if ok:
            # Clear underrun window after a successful recovery to avoid immediate re-trigger.
            self._underrun_ts.clear()
            return Anomaly(
                code=AnomalyCode.AIRCHECK_AUTO_RESTARTED,
                severity=Severity.CRITICAL,
                ts=now_ts,
                message=(
                    f"Auto-restarted {self.cfg.unit} due to {trigger_reason} "
                    f"(cooldown={self.cfg.cooldown_s:.0f}s, max_per_hour={self.cfg.max_per_hour})"
                ),
                raw=out or a.raw,
            )

        return Anomaly(
            code=AnomalyCode.AIRCHECK_AUTO_RESTART_FAILED,
            severity=Severity.ERROR,
            ts=now_ts,
            message=f"Auto-restart FAILED for {self.cfg.unit} due to {trigger_reason}: {out}",
            raw=a.raw,
        )
