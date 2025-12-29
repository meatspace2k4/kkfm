from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .anomalies import Anomaly, AnomalyCode, Severity
from .events import Event, EventType


@dataclass
class SegmentState:
    kind: str  # "jingle" | "commercial"
    picked_name: Optional[str] = None
    picked_ts: Optional[float] = None
    started_name: Optional[str] = None
    started_ts: Optional[float] = None

    def reset(self) -> None:
        self.picked_name = None
        self.picked_ts = None
        self.started_name = None
        self.started_ts = None


class Detector:
    """Strict anomaly detector over the service event stream.

    Notes:
    - This only uses *observable* log events. If a thing isn't logged, we don't infer it.
        - "Picked" events can be preloads/cache-warm, and watchdog restarts can miss edges.
            For those cases, we resync state instead of emitting false positives.
    """

    def __init__(
        self,
        start_timeout_s: float = 60.0,
        end_timeout_s: float = 45.0,
        dead_air_timeout_s: float = 300.0,
        orphan_end_grace_s: float = 10.0,
    ) -> None:
        self.start_timeout_s = float(start_timeout_s)
        self.end_timeout_s = float(end_timeout_s)
        self.dead_air_timeout_s = float(dead_air_timeout_s)
        self.orphan_end_grace_s = max(0.0, float(orphan_end_grace_s))
        self.jingle = SegmentState(kind="jingle")
        self.comm = SegmentState(kind="commercial")
        self.last_activity_ts: Optional[float] = None

        # Unit lifecycle tracking (from systemd journal lines).
        # We treat the *first* UNIT_STARTED we observe as baseline (attach/startup).
        # Subsequent UNIT_STARTED events indicate a restart.
        self._seen_unit_started = False

        # Optional marker for intentional maintenance/testing shutdowns.
        # If the service does not emit such a marker yet, all restarts are treated
        # as unexpected (which matches the user's request).
        self._saw_safe_shutdown_marker = False

        # Still track whether we've seen normal activity; used for other heuristics.
        self._had_non_lifecycle_activity = False
        self.last_dead_air_emit_ts: float = 0.0

        # Used to suppress restart-induced noise for a short window after startup.
        self._startup_ts: Optional[float] = None

        # `journalctl -o json -f` should preserve ordering, but in practice we still
        # see occasional out-of-order delivery across restarts/rotations. Also, the
        # service may log "picked" during preload/cache warmup, well before start.
        self.reorder_grace_s: float = 2.0

    def snapshot(self) -> dict:
        def seg(st: SegmentState) -> dict:
            return {
                "kind": st.kind,
                "picked_name": st.picked_name,
                "picked_ts": st.picked_ts,
                "started_name": st.started_name,
                "started_ts": st.started_ts,
            }

        return {
            "start_timeout_s": self.start_timeout_s,
            "end_timeout_s": self.end_timeout_s,
            "jingle": seg(self.jingle),
            "commercial": seg(self.comm),
        }

    def _timeout_checks(self, now_ts: float) -> list[Anomaly]:
        out: list[Anomaly] = []
        for st in (self.jingle, self.comm):
            if st.picked_ts is not None and st.started_ts is None:
                # NOTE: In the real system, "picked" can mean "selected/preloaded" well
                # before playback begins (cache warmup). Also, on watchdog restart we can
                # miss the corresponding START. Treat this as non-fatal and resync.
                if (now_ts - st.picked_ts) > self.start_timeout_s:
                    # Do not emit an anomaly here: PICK can legitimately precede START by
                    # a long time (preload), and journald ordering can be imperfect.
                    # Instead, silently drop the stale PICK state so we don't create
                    # downstream false positives.
                    st.reset()
            if st.started_ts is not None:
                if (now_ts - st.started_ts) > self.end_timeout_s:
                    out.append(
                        Anomaly(
                            code=AnomalyCode.JINGLE_START_NO_END
                            if st.kind == "jingle"
                            else AnomalyCode.COMM_START_NO_END,
                            severity=Severity.ERROR,
                            ts=now_ts,
                            name=st.started_name,
                            message=f"{st.kind} started but never ended within {self.end_timeout_s:.1f}s",
                        )
                    )
                    st.reset()
        return out

    def feed(self, ev: Event) -> list[Anomaly]:
        """Feed one event; returns any anomalies triggered by this event."""
        if self._startup_ts is None:
            self._startup_ts = ev.ts
        self.last_activity_ts = ev.ts
        anomalies: list[Anomaly] = []
        anomalies.extend(self._timeout_checks(ev.ts))

        if ev.etype == EventType.SAFE_SHUTDOWN:
            # The service told us it is intentionally shutting down.
            self._saw_safe_shutdown_marker = True
            # This is still "activity" (it indicates the service was alive).
            self._had_non_lifecycle_activity = True
            return anomalies

        # Unit lifecycle handling:
        # - First UNIT_STARTED we observe is baseline.
        # - Subsequent UNIT_STARTED indicates a restart.
        # - If a safe shutdown marker was seen beforehand, suppress the anomaly.
        if ev.etype == EventType.UNIT_STARTED:
            if not self._seen_unit_started:
                self._seen_unit_started = True
                self._saw_safe_shutdown_marker = False
                return anomalies

            if not self._saw_safe_shutdown_marker:
                anomalies.append(
                    Anomaly(
                        code=AnomalyCode.AIRCHECK_UNIT_STARTED_UNEXPECTED,
                        severity=Severity.CRITICAL,
                        ts=ev.ts,
                        raw=ev.raw,
                        message="Monitored service restarted unexpectedly",
                    )
                )

            # Reset marker for the next run.
            self._saw_safe_shutdown_marker = False
            return anomalies
        if ev.etype == EventType.UNIT_STOPPED:
            return anomalies

        if ev.etype == EventType.DAC_OVERFLOW:
            anomalies.append(
                Anomaly(
                    code=AnomalyCode.DAC_RING_OVERFLOW,
                    severity=Severity.ERROR,
                    ts=ev.ts,
                    raw=ev.raw,
                    message="DAC ring buffer overflow observed",
                )
            )
            return anomalies
        if ev.etype == EventType.DAC_UNDERRUN:
            anomalies.append(
                Anomaly(
                    code=AnomalyCode.DAC_RING_UNDERRUN,
                    severity=Severity.WARN,
                    ts=ev.ts,
                    raw=ev.raw,
                    message="DAC ring buffer underrun observed",
                )
            )
            return anomalies
        if ev.etype == EventType.DAC_THROTTLE:
            anomalies.append(
                Anomaly(
                    code=AnomalyCode.DAC_RING_THROTTLE,
                    severity=Severity.WARN,
                    ts=ev.ts,
                    raw=ev.raw,
                    message="DAC ring buffer throttling observed",
                )
            )
            return anomalies

        # Any other event counts as "real" activity.
        self._had_non_lifecycle_activity = True

        # Jingle state machine
        if ev.etype == EventType.JINGLE_PICKED:
            # If we already saw START, a late-arriving PICK should not clear it.
            if (
                self.jingle.started_ts is not None
                and self.jingle.started_name == ev.name
                and abs(self.jingle.started_ts - ev.ts) <= self.reorder_grace_s
            ):
                self.jingle.picked_name = ev.name
                self.jingle.picked_ts = ev.ts
                return anomalies

            self.jingle.picked_name = ev.name
            self.jingle.picked_ts = ev.ts
            self.jingle.started_name = None
            self.jingle.started_ts = None
            return anomalies

        if ev.etype == EventType.JINGLE_START:
            if self.jingle.picked_ts is None:
                # Allow missing PICK entirely (watchdog attach mid-stream) and tolerate
                # out-of-order delivery where START arrives before PICK.
                self.jingle.picked_name = ev.name
                self.jingle.picked_ts = ev.ts
            else:
                # If the PICK is very old, treat it as preload/irrelevant and resync.
                if (ev.ts - self.jingle.picked_ts) > self.start_timeout_s:
                    self.jingle.picked_name = ev.name
                    self.jingle.picked_ts = ev.ts
                elif ev.name != self.jingle.picked_name:
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.JINGLE_NAME_MISMATCH,
                            severity=Severity.ERROR,
                            ts=ev.ts,
                            name=ev.name,
                            raw=ev.raw,
                            message=f"jingle name mismatch: picked={self.jingle.picked_name} start={ev.name}",
                        )
                    )
            self.jingle.started_name = ev.name
            self.jingle.started_ts = ev.ts
            return anomalies

        if ev.etype == EventType.JINGLE_END:
            if self.jingle.started_ts is None:
                # Missing START is common if the watchdog restarts mid-jingle.
                # During the initial attach window, suppress this marker to avoid
                # stale/restart noise; otherwise emit INFO and resync.
                within_grace = (
                    self.orphan_end_grace_s > 0
                    and self._startup_ts is not None
                    and (ev.ts - self._startup_ts) <= self.orphan_end_grace_s
                )
                if not within_grace:
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.JINGLE_END_WITHOUT_START,
                            severity=Severity.INFO,
                            ts=ev.ts,
                            name=ev.name,
                            raw=ev.raw,
                            message="jingle END observed without a preceding START (restart/missed log edge)",
                        )
                    )
                self.jingle.reset()
                return anomalies
            else:
                if ev.name != self.jingle.started_name:
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.JINGLE_NAME_MISMATCH,
                            severity=Severity.ERROR,
                            ts=ev.ts,
                            name=ev.name,
                            raw=ev.raw,
                            message=f"jingle name mismatch: start={self.jingle.started_name} end={ev.name}",
                        )
                    )
            self.jingle.reset()
            return anomalies

        if ev.etype == EventType.JINGLE_FAILED:
            anomalies.append(
                Anomaly(
                    code=AnomalyCode.JINGLE_FAILED_TO_LOAD,
                    severity=Severity.ERROR,
                    ts=ev.ts,
                    name=ev.name,
                    raw=ev.raw,
                    message="jingle failed to load",
                )
            )
            return anomalies

        # Commercial state machine
        if ev.etype == EventType.COMM_PICKED:
            if (
                self.comm.started_ts is not None
                and self.comm.started_name == ev.name
                and abs(self.comm.started_ts - ev.ts) <= self.reorder_grace_s
            ):
                self.comm.picked_name = ev.name
                self.comm.picked_ts = ev.ts
                return anomalies

            self.comm.picked_name = ev.name
            self.comm.picked_ts = ev.ts
            self.comm.started_name = None
            self.comm.started_ts = None
            return anomalies

        if ev.etype == EventType.COMM_START:
            if self.comm.picked_ts is None:
                # Allow missing PICK entirely and tolerate out-of-order delivery.
                self.comm.picked_name = ev.name
                self.comm.picked_ts = ev.ts
            else:
                if (ev.ts - self.comm.picked_ts) > self.start_timeout_s:
                    self.comm.picked_name = ev.name
                    self.comm.picked_ts = ev.ts
                elif ev.name != self.comm.picked_name:
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.COMM_NAME_MISMATCH,
                            severity=Severity.ERROR,
                            ts=ev.ts,
                            name=ev.name,
                            raw=ev.raw,
                            message=f"commercial name mismatch: picked={self.comm.picked_name} start={ev.name}",
                        )
                    )
            self.comm.started_name = ev.name
            self.comm.started_ts = ev.ts
            return anomalies

        if ev.etype == EventType.COMM_END:
            if self.comm.started_ts is None:
                within_grace = (
                    self.orphan_end_grace_s > 0
                    and self._startup_ts is not None
                    and (ev.ts - self._startup_ts) <= self.orphan_end_grace_s
                )
                if not within_grace:
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.COMM_END_WITHOUT_START,
                            severity=Severity.INFO,
                            ts=ev.ts,
                            name=ev.name,
                            raw=ev.raw,
                            message="commercial END observed without a preceding START (restart/missed log edge)",
                        )
                    )
                self.comm.reset()
                return anomalies
            else:
                if ev.name != self.comm.started_name:
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.COMM_NAME_MISMATCH,
                            severity=Severity.ERROR,
                            ts=ev.ts,
                            name=ev.name,
                            raw=ev.raw,
                            message=f"commercial name mismatch: start={self.comm.started_name} end={ev.name}",
                        )
                    )
            self.comm.reset()
            return anomalies

        if ev.etype == EventType.COMM_FAILED:
            anomalies.append(
                Anomaly(
                    code=AnomalyCode.COMM_FAILED_TO_LOAD,
                    severity=Severity.ERROR,
                    ts=ev.ts,
                    name=ev.name,
                    raw=ev.raw,
                    message="commercial failed to load",
                )
            )
            return anomalies

        return anomalies

    def poll(self, now_ts: float) -> list[Anomaly]:
        """Periodic checks (e.g. dead air) when no events are arriving."""
        anomalies: list[Anomaly] = []
        anomalies.extend(self._timeout_checks(now_ts))

        if self.last_activity_ts is not None:
            silence_dur = now_ts - self.last_activity_ts
            if silence_dur > self.dead_air_timeout_s:
                # Emit dead air anomaly every 60s if it persists
                if (now_ts - self.last_dead_air_emit_ts) > 60.0:
                    self.last_dead_air_emit_ts = now_ts
                    anomalies.append(
                        Anomaly(
                            code=AnomalyCode.RADIO_DEAD_AIR,
                            severity=Severity.CRITICAL,
                            ts=now_ts,
                            message=f"RADIO DEAD AIR: no log activity for {silence_dur:.1f}s",
                        )
                    )
        return anomalies


