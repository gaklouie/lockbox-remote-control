"""
Background threads: the always-on monitor loop, and the manual scan.

Neither touches Tk; results go to the GUI through state.gui_queue.
"""

import time

from . import state
from .config import MODE_DISPLAY_NAMES, settings
from .hardware import read_physical, short_caps_and_block_pid
from .locking import autolock_once
from .scanning import sweep
from .signal_processing import (find_crossing_candidates, find_safe_range_segments,
                                is_within, smooth_signal)
from .telemetry import publish_values


def monitor_loop():
    """
    Read all channels every MONITOR_INTERVAL for the live display, publish
    to MQTT every LOG_INTERVAL, and - if autolock is engaged - run an
    autolock whenever the trigger condition for the current mode fires.
    Pauses while a manual scan or an on-demand autolock owns control out.
    """
    last_log_time = 0.0
    while not state.stop_event.is_set():
        if state.manual_scan_active.is_set() or state.autolock_running.is_set():
            time.sleep(0.2)
            continue

        try:
            values = read_physical(num_samples=1)
        except Exception as exc:
            state.log(f"Monitor read failed: {exc!r}")
            time.sleep(1.0)
            continue
        state.gui_queue.put(("reading", time.time(), values))  # always feeds the fast live display

        now = time.time()
        if now - last_log_time >= settings.LOG_INTERVAL:
            publish_values(values, "monitoring")
            last_log_time = now

        if state.autolock_engaged.is_set():
            if settings.AUTOLOCK_MODE == "dc_err_range":
                triggered = not is_within(values["dc_err"], settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX)
                trigger_msg = (f"dc_err={values['dc_err']:.4f}V outside "
                               f"[{settings.DC_ERR_SAFE_MIN}, {settings.DC_ERR_SAFE_MAX}]")
            else:
                slow_ok = is_within(values["slow_output"], settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX)
                fast_ok = is_within(values["fast_output"], settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX)
                triggered = not (slow_ok and fast_ok)
                trigger_msg = f"slow_ok={slow_ok} fast_ok={fast_ok}"

            if triggered:
                state.log(f"Trigger: {trigger_msg}")
                if not autolock_once():
                    time.sleep(settings.LOCK_RETRY_DELAY)
                continue

        time.sleep(settings.MONITOR_INTERVAL)


def run_manual_scan(v_min, v_max, num_points, mode, crossing_sign, adaptive):
    """
    Sweep control out from v_min to v_max (physical volts), then find lock
    candidates for `mode` and hand everything to the Scan tab via "scan_done".
    num_points is only used for a uniform (non-adaptive) sweep; an adaptive
    sweep follows the signal that `mode` searches.
    """
    state.manual_scan_active.set()
    try:
        state.log(f"Starting manual scan ({MODE_DISPLAY_NAMES[mode]}, sign={crossing_sign}, "
                  f"{'adaptive' if adaptive else f'{num_points} uniform'} points)...")
        short_caps_and_block_pid()

        track_signal = "error" if mode == "zero_crossing" else "dc_err"
        voltages, trace = sweep(
            v_min, v_max, num_points, track_signal, "manual", adaptive=adaptive,
            on_point=lambda _values, fraction: state.gui_queue.put(("scan_progress", fraction)),
        )

        if mode == "zero_crossing":
            signal_smoothed = smooth_signal(voltages, trace["error"], settings.SMOOTHING_WINDOW)
            crossings = find_crossing_candidates(voltages, signal_smoothed, crossing_sign,
                                                 settings.MIN_CROSSING_SLOPE_FRACTION)
            candidates = [
                {"lock_voltage": v, "label": f"V={v:.4f} V   slope={slope:.4f}",
                 "start": None, "end": None}
                for v, slope in crossings
            ]
        else:
            signal_smoothed = smooth_signal(voltages, trace["dc_err"], settings.SMOOTHING_WINDOW)
            segments = find_safe_range_segments(voltages, signal_smoothed,
                                                settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX)
            candidates = [
                {"lock_voltage": mid, "label": f"V={mid:.4f} V   range=[{s:.4f}, {e:.4f}]  width={w:.4f}",
                 "start": s, "end": e}
                for s, e, mid, w in segments
            ]

        state.gui_queue.put(("scan_done", {
            "voltages": voltages, "trace": trace, "mode": mode,
            "signal_smoothed": signal_smoothed, "candidates": candidates,
        }))
        state.log(f"Manual scan complete - {len(voltages)} points, {len(candidates)} candidate(s) found.")
    except Exception as exc:
        state.log(f"Manual scan failed: {exc!r}")
        state.gui_queue.put(("scan_done", None))
    finally:
        state.manual_scan_active.clear()
