"""
The two automatic autolock procedures (triggered by the monitor loop, or run
on demand from the Settings tab).

Both use the same coarse-then-fine structure: a coarse sweep of control out
over the full scan range picks a candidate (adaptive point spacing if
ADAPTIVE_SCAN is on - see scanning.py), then a uniform fine sweep of +/- one
coarse step around it refines the lock point.

    - "zero_crossing": lock to the steepest zero crossing of the "error"
      signal, keeping only crossings of the sign set by CROSSING_SIGN.
    - "dc_err_range": lock to the midpoint of the widest region where
      "dc_err" sits inside its safe range.

On success B/C/D go LOW (PID restarted). On failure control out resets to
RESET_VOLTAGE_ON_FAILURE and B/C/D stay HIGH.
"""

import time

from . import state
from .config import settings
from .hardware import restart_pid, set_control_out, short_caps_and_block_pid
from .plotting import save_autolock_scan_plot
from .scanning import sweep, uniform_sweep
from .signal_processing import find_crossing_candidates, find_safe_range_segments, smooth_signal


def _send_reading(values, _fraction_done):
    state.gui_queue.put(("reading", time.time(), values))


def _coarse_pass(track_signal):
    """Full-range sweep: adaptive or uniform per the ADAPTIVE_SCAN setting."""
    return sweep(settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE, settings.NUM_COARSE_POINTS,
                 track_signal, "coarse", on_point=_send_reading)


def _fine_pass(center, coarse_step):
    """Uniform sweep of +/- one coarse step around `center`, clipped to the scan range."""
    fine_min = max(settings.SCAN_MIN_VOLTAGE, center - coarse_step)
    fine_max = min(settings.SCAN_MAX_VOLTAGE, center + coarse_step)
    return uniform_sweep(fine_min, fine_max, settings.NUM_FINE_POINTS, "fine", on_point=_send_reading)


def _coarse_step():
    """The largest gap between coarse points - so +/- this around a candidate brackets it."""
    if settings.ADAPTIVE_SCAN:
        return settings.SCAN_MAX_STEP
    return (settings.SCAN_MAX_VOLTAGE - settings.SCAN_MIN_VOLTAGE) / (settings.NUM_COARSE_POINTS - 1)


def _autolock_zero_crossing(log_fn):
    log_fn(f"Starting zero-crossing autolock on the error signal (sign={settings.CROSSING_SIGN})...")
    short_caps_and_block_pid()
    session_id = time.strftime("%Y%m%d_%H%M%S")

    coarse_step = _coarse_step()
    coarse_v, coarse_trace = _coarse_pass("error")
    coarse_smoothed = smooth_signal(coarse_v, coarse_trace["error"], settings.SMOOTHING_WINDOW)
    coarse_crossings = find_crossing_candidates(coarse_v, coarse_smoothed, settings.CROSSING_SIGN,
                                                settings.MIN_CROSSING_SLOPE_FRACTION)
    coarse_data = {"mode": "zero_crossing", "signal_name": "error", "voltages": coarse_v,
                   "raw": coarse_trace["error"], "smoothed": coarse_smoothed,
                   "crossings": coarse_crossings, "chosen": None}

    if not coarse_crossings:
        log_fn("No matching zero crossings found - autolock failed.")
        save_autolock_scan_plot(session_id, coarse_data, None)
        set_control_out(settings.RESET_VOLTAGE_ON_FAILURE)
        return False

    coarse_best_v, coarse_best_slope = max(coarse_crossings, key=lambda c: abs(c[1]))
    coarse_data["chosen"] = (coarse_best_v, coarse_best_slope)
    log_fn(f"Coarse pass found {len(coarse_crossings)} crossing(s); "
           f"largest near {coarse_best_v:.4f} V. Refining...")

    fine_v, fine_trace = _fine_pass(coarse_best_v, coarse_step)
    fine_smoothed = smooth_signal(fine_v, fine_trace["error"], settings.SMOOTHING_WINDOW)
    fine_crossings = find_crossing_candidates(fine_v, fine_smoothed, settings.CROSSING_SIGN,
                                              settings.MIN_CROSSING_SLOPE_FRACTION)

    if fine_crossings:
        lock_v, lock_slope = max(fine_crossings, key=lambda c: abs(c[1]))
    else:
        lock_v, lock_slope = coarse_best_v, coarse_best_slope

    fine_data = {"mode": "zero_crossing", "signal_name": "error", "voltages": fine_v,
                 "raw": fine_trace["error"], "smoothed": fine_smoothed,
                 "crossings": fine_crossings, "chosen": (lock_v, lock_slope)}
    save_autolock_scan_plot(session_id, coarse_data, fine_data)

    set_control_out(lock_v)
    log_fn(f"Locked: control out = {lock_v:.4f} V physical (slope {lock_slope:.4f})")
    restart_pid()
    return True


def _autolock_dc_err_range(log_fn):
    log_fn("Starting DC error lock-range autolock...")
    short_caps_and_block_pid()
    session_id = time.strftime("%Y%m%d_%H%M%S")

    coarse_step = _coarse_step()
    coarse_v, coarse_trace = _coarse_pass("dc_err")
    coarse_smoothed = smooth_signal(coarse_v, coarse_trace["dc_err"], settings.SMOOTHING_WINDOW)
    coarse_segments = find_safe_range_segments(coarse_v, coarse_smoothed,
                                               settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX)
    coarse_data = {"mode": "dc_err_range", "signal_name": "dc_err", "voltages": coarse_v,
                   "raw": coarse_trace["dc_err"], "smoothed": coarse_smoothed,
                   "segments": coarse_segments, "chosen": None,
                   "safe_min": settings.DC_ERR_SAFE_MIN, "safe_max": settings.DC_ERR_SAFE_MAX}

    if not coarse_segments:
        log_fn("No region with DC error inside its lock range found - autolock failed.")
        save_autolock_scan_plot(session_id, coarse_data, None)
        set_control_out(settings.RESET_VOLTAGE_ON_FAILURE)
        return False

    widest = max(coarse_segments, key=lambda s: s[3])
    coarse_mid = widest[2]
    coarse_data["chosen"] = (coarse_mid, None)
    log_fn(f"Coarse pass found {len(coarse_segments)} lock-range region(s); "
           f"widest centered near {coarse_mid:.4f} V. Refining...")

    fine_v, fine_trace = _fine_pass(coarse_mid, coarse_step)
    fine_smoothed = smooth_signal(fine_v, fine_trace["dc_err"], settings.SMOOTHING_WINDOW)
    fine_segments = find_safe_range_segments(fine_v, fine_smoothed,
                                             settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX)

    if fine_segments:
        fine_widest = max(fine_segments, key=lambda s: s[3])
        lock_v = fine_widest[2]
    else:
        lock_v = coarse_mid

    fine_data = {"mode": "dc_err_range", "signal_name": "dc_err", "voltages": fine_v,
                 "raw": fine_trace["dc_err"], "smoothed": fine_smoothed,
                 "segments": fine_segments, "chosen": (lock_v, None),
                 "safe_min": settings.DC_ERR_SAFE_MIN, "safe_max": settings.DC_ERR_SAFE_MAX}
    save_autolock_scan_plot(session_id, coarse_data, fine_data)

    set_control_out(lock_v)
    log_fn(f"Locked: control out = {lock_v:.4f} V physical (DC error lock-range midpoint)")
    restart_pid()
    return True


def autolock_once(log_fn=state.log):
    """
    Run one autolock attempt in the current AUTOLOCK_MODE. Returns True if
    locked. Sets state.autolock_running for the duration so the GUI and the
    monitor loop leave control out alone; an unexpected error is logged and
    counted as a failed attempt rather than killing the calling thread.
    """
    state.autolock_running.set()
    try:
        if settings.AUTOLOCK_MODE == "dc_err_range":
            return _autolock_dc_err_range(log_fn)
        return _autolock_zero_crossing(log_fn)
    except Exception as exc:
        log_fn(f"Autolock failed with an error: {exc!r}")
        return False
    finally:
        state.autolock_running.clear()
