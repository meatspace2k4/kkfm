#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None


@dataclass(frozen=True)
class Result:
    ok: bool
    detail: str


def _reset_via_dtr(*, port: str, baudrate: int, pulse_s: float) -> Result:
    if serial is None:
        return Result(False, "pyserial not available (install python3-serial / pip install pyserial)")

    try:
        ser = serial.Serial()  # type: ignore[call-arg]
        ser.port = port
        ser.baudrate = int(baudrate)
        ser.timeout = 0.25

        # Open and *intentionally* toggle control lines to reset the MCU.
        ser.open()
        try:
            # Ensure stable known state first.
            try:
                ser.dtr = True
            except Exception:
                pass
            try:
                ser.rts = True
            except Exception:
                pass
            time.sleep(0.05)

            # Pulse DTR low then high (classic Arduino auto-reset).
            try:
                ser.dtr = False
            except Exception:
                pass
            time.sleep(max(0.01, float(pulse_s)))
            try:
                ser.dtr = True
            except Exception:
                pass
            time.sleep(0.05)

            # Clear any boot noise so subsequent reads see fresh banner.
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
        finally:
            try:
                ser.close()
            except Exception:
                pass

        return Result(True, f"toggled DTR on {port}")
    except Exception as e:
        return Result(False, f"DTR reset failed on {port}: {type(e).__name__}: {e}")


def _reset_via_1200_baud_touch(*, port: str, pulse_s: float) -> Result:
    """1200-baud touch reset.

    Works for many ATmega32u4-based boards (Leonardo/Micro) and some other
    bootloaders. For an Uno, DTR toggling is usually the correct method.
    """
    if serial is None:
        return Result(False, "pyserial not available (install python3-serial / pip install pyserial)")

    try:
        ser = serial.Serial(port, 1200, timeout=0.25)  # type: ignore[call-arg]
        try:
            # Many bootloaders trigger on open/close at 1200.
            try:
                ser.dtr = False
            except Exception:
                pass
            time.sleep(max(0.05, float(pulse_s)))
        finally:
            try:
                ser.close()
            except Exception:
                pass
        return Result(True, f"1200-baud touch on {port}")
    except Exception as e:
        return Result(False, f"1200-baud touch failed on {port}: {type(e).__name__}: {e}")


def _wait_for_banner(*, port: str, baudrate: int, timeout_s: float) -> Result:
    if serial is None:
        return Result(False, "pyserial not available")

    deadline = time.time() + max(0.1, float(timeout_s))
    last_err: str | None = None
    # After toggling DTR, many Arduino boards briefly disconnect and re-enumerate
    # (ttyACM device node can flap). Also, another process (tx watchdog) may already
    # have the port open. Treat open/read errors as transient and retry until timeout.
    while time.time() < deadline:
        try:
            with serial.Serial(port, int(baudrate), timeout=0.25) as ser:  # type: ignore[call-arg]
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass

                while time.time() < deadline:
                    try:
                        raw = ser.readline()
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {e}"
                        break
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    if line.startswith("TX_FREQ="):
                        return Result(True, f"saw banner: {line}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        time.sleep(0.25)

    if last_err:
        return Result(False, f"did not see TX_FREQ banner within {timeout_s:.1f}s (last error: {last_err})")
    return Result(False, f"did not see TX_FREQ banner within {timeout_s:.1f}s")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="restart_tx_sketch.py")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baudrate", type=int, default=115200)
    ap.add_argument(
        "--method",
        default="auto",
        choices=["auto", "dtr", "1200"],
        help="Reset method. auto prefers DTR then falls back to 1200-baud touch.",
    )
    ap.add_argument("--pulse-s", type=float, default=0.25, help="Reset pulse duration")
    ap.add_argument(
        "--wait-banner-s",
        type=float,
        default=6.0,
        help="After reset, wait up to this long for 'TX_FREQ=' banner. 0 disables.",
    )
    ap.add_argument(
        "--require-banner",
        action="store_true",
        help="Exit non-zero if the TX_FREQ banner is not observed.",
    )

    args = ap.parse_args(argv)

    if serial is None:
        print("ERROR: pyserial not available (install python3-serial)", file=sys.stderr)
        return 2

    if args.method == "dtr":
        res = _reset_via_dtr(port=args.port, baudrate=args.baudrate, pulse_s=args.pulse_s)
    elif args.method == "1200":
        res = _reset_via_1200_baud_touch(port=args.port, pulse_s=args.pulse_s)
    else:
        res = _reset_via_dtr(port=args.port, baudrate=args.baudrate, pulse_s=args.pulse_s)
        if not res.ok:
            res2 = _reset_via_1200_baud_touch(port=args.port, pulse_s=args.pulse_s)
            if res2.ok:
                res = res2

    if not res.ok:
        print(f"ERROR: {res.detail}", file=sys.stderr)
        return 1

    print(f"OK: {res.detail}")

    if float(args.wait_banner_s) > 0:
        time.sleep(0.25)
        w = _wait_for_banner(port=args.port, baudrate=args.baudrate, timeout_s=args.wait_banner_s)
        if w.ok:
            print(f"OK: {w.detail}")
            return 0
        print(f"WARN: {w.detail}", file=sys.stderr)
        # Default: don't fail the whole command just because we didn't catch the
        # banner (it can be missed if another process is holding the port or if the
        # device re-enumerates). Use --require-banner to enforce non-zero.
        return 3 if bool(args.require_banner) else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
