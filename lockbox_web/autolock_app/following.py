"""
Follow a chosen zero crossing onto lock - an alternative to the fine scan.

The fine scan sweeps a narrow window around where the coarse scan saw the
crossing. If the error signal has moved since, the window can miss it. Here
control out is instead steered onto the crossing as it is NOW:

    1. Servo: read the error, step control out towards zero (a PI step scaled
       by the slope measured in the scan, capped so one step can't jump out of
       the crossing's linear region), until |error| stays within tolerance.
    2. Identity check: step out to where the two lobe peaks were in the scan
       and confirm they're still there, with the right signs and at least half
       their scanned height. This catches parking on a flat stretch (error ~ 0
       but no crossing) or on a different crossing. Then settle again.
    3. If the servo runs away or doesn't settle, or the lobes don't match:
       sweep a small window around the expected position, take the nearest
       crossing of the same sign, enough symmetry and similar lobe height, and
       try again (up to MAX_ATTEMPTS).

Runs with the PID blocked (B/C/D high), so control out alone sets the
actuator; the caller hands over to the PID (restart_pid) afterwards. The ADC
runs in its fast single-input mode on the error signal throughout.
"""

import time

import numpy as np

from . import state
from .config import DAC_SHIFT_GAIN, VDD_VOLTAGE, settings
from .hardware import fast_adc, read_signal_fast, set_control_out
from .scanning import ScanStopped, uniform_sweep
from .signal_processing import find_crossing_candidates, smooth_signal

SIGNAL = "error"
MAX_ATTEMPTS = 3
SETTLED_READINGS = 3          # consecutive in-tolerance readings needed to call it settled
RESETTLE_ITERATIONS = 50      # after the identity check moves control out away and back
CONTROL_OUT_STEP = abs(DAC_SHIFT_GAIN) * VDD_VOLTAGE / 4096   # one 12-bit DAC code, physical V


class FollowFailed(Exception):
    """The crossing couldn't be followed onto lock (lost, or not found again)."""


def _read_at(v):
    """Move control out to v, settle, and read the error (inside fast_adc)."""
    if state.stop_event.is_set():
        raise ScanStopped("the program is shutting down")
    if state.stop_scan_requested.is_set():
        raise ScanStopped("stopped by request")
    set_control_out(v)
    time.sleep(settings.SCAN_SETTLE_MS / 1000.0)
    return read_signal_fast(SIGNAL, settings.NUM_SAMPLES_PER_POINT)


def _servo(crossing, start, max_iterations):
    """
    Steer control out from `start` onto the crossing. Returns (voltage or
    None, steps taken, last error). None if it didn't settle or wandered more
    than two lobe-spans from the start (lost, or a different feature).
    """
    half_width = min(crossing.voltage - crossing.left_peak, crossing.right_peak - crossing.voltage)
    max_step = max(half_width / 2, CONTROL_OUT_STEP)
    give_up_at = 2 * max(crossing.right_peak - crossing.left_peak, CONTROL_OUT_STEP)
    # Never ask for better than one DAC code's worth of error: it can't be reached.
    tolerance = max(settings.SERVO_TOLERANCE * crossing.lobe, abs(crossing.slope) * CONTROL_OUT_STEP)

    v, integral, settled, error = start, 0.0, 0, float("nan")
    for step in range(1, max_iterations + 1):
        error = _read_at(v)
        settled = settled + 1 if abs(error) <= tolerance else 0
        if settled >= SETTLED_READINGS:
            return v, step, error
        integral += error
        dv = -(settings.SERVO_P_GAIN * error + settings.SERVO_I_GAIN * integral) / crossing.slope
        v += float(np.clip(dv, -max_step, max_step))
        if abs(v - start) > give_up_at:
            return None, step, error
    return None, max_iterations, error


def _lobes_match(v, crossing):
    """Read the error where the lobe peaks should be; True if both are still there."""
    direction = 1.0 if crossing.slope > 0 else -1.0      # rising crossing: left lobe negative
    left = _read_at(v - (crossing.voltage - crossing.left_peak))
    right = _read_at(v + (crossing.right_peak - crossing.voltage))
    ok = -direction * left >= 0.5 * crossing.lobe and direction * right >= 0.5 * crossing.lobe
    return ok, left, right


def _find_again(crossing):
    """Sweep around where the crossing was expected; return the matching crossing nearest to it, or None."""
    span = max(crossing.right_peak - crossing.left_peak, CONTROL_OUT_STEP)
    reach = max(5 * span, 3 * settings.SCAN_MAX_STEP)     # well beyond the fine scan's +/- 1 step
    lo = max(settings.SCAN_MIN_VOLTAGE, crossing.voltage - reach)
    hi = min(settings.SCAN_MAX_VOLTAGE, crossing.voltage + reach)
    points = int(np.clip((hi - lo) / max(span / 10, CONTROL_OUT_STEP), 20, 400)) + 1
    voltages, trace = uniform_sweep(lo, hi, points, SIGNAL, "follow")
    smoothed = smooth_signal(voltages, trace[SIGNAL], settings.SMOOTHING_WINDOW)
    sign = "positive" if crossing.slope > 0 else "negative"
    found = [c for c in find_crossing_candidates(voltages, smoothed, sign, 0.0, settings.MIN_CROSSING_SYMMETRY)
             if c.lobe >= 0.5 * crossing.lobe]
    return min(found, key=lambda c: abs(c.voltage - crossing.voltage)) if found else None


def _mv(volts):
    return f"{1000 * volts:+.2f} mV"


def follow_crossing(crossing, log_fn=state.log):
    """
    Steer control out onto `crossing` (a signal_processing.Crossing from a
    scan) as it is now, confirming it's the same crossing. Returns the control
    out voltage of the lock point. Raises FollowFailed, or ScanStopped if
    "Stop Scan" is pressed. The PID must be blocked (B/C/D high) by the caller.
    """
    scanned_at = crossing.voltage
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with fast_adc(SIGNAL):
            v, steps, error = _servo(crossing, crossing.voltage, settings.SERVO_MAX_ITERATIONS)
            if v is None:
                why = f"the servo didn't settle ({steps} steps, last error {_mv(error)})"
            else:
                same, left, right = _lobes_match(v, crossing)
                if not same:
                    why = (f"the lobes don't match (left {_mv(left)}, right {_mv(right)}; "
                           f"expected about {_mv(crossing.lobe)} each, opposite signs)")
                else:
                    v2, steps2, error = _servo(crossing, v, RESETTLE_ITERATIONS)
                    if v2 is not None:
                        log_fn(f"Following: on the crossing at {v2:.4f} V (moved {v2 - scanned_at:+.4f} V "
                               f"since the scan; {steps + steps2} steps; error {_mv(error)}; "
                               f"lobes {_mv(left)} / {_mv(right)}).")
                        return v2
                    why = f"it didn't settle again after checking the lobes (last error {_mv(error)})"
        log_fn(f"Following: attempt {attempt}: {why}. Looking for the crossing nearby...")
        found = _find_again(crossing)
        if found is None:
            raise FollowFailed(f"couldn't find the crossing again near {crossing.voltage:.4f} V")
        log_fn(f"Following: found it at {found.voltage:.4f} V (expected {crossing.voltage:.4f} V).")
        crossing = found
    raise FollowFailed(f"no stable lock after {MAX_ATTEMPTS} attempts")
