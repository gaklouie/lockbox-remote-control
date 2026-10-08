"""
Background threads: the always-on monitor loop, and the manual scan.

Neither touches Tk; results go to the GUI through state.gui_queue.
"""

import time

import numpy as np

from . import state
from .channels import CHANNEL_CONVERTERS
from .config import settings
from .hardware import read_physical, set_control_out, short_caps_and_block_pid
from .locking import autolock_once
from .signal_processing import (filter_crossings_by_sign, find_safe_range_segments,
                                find_zero_crossings, is_within, smooth_signal)
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


def run_manual_scan(v_min, v_max, num_points, mode, crossing_sign):
    """
    Sweep control out from v_min to v_max (physical volts), then find lock
    candidates for `mode` and hand everything to the Scan tab via "scan_done".
    """
    state.manual_scan_active.set()
    try:
        state.log(f"Starting manual scan (mode={mode}, sign={crossing_sign})...")
        short_caps_and_block_pid()

        voltages = np.linspace(v_min, v_max, num_points)
        trace = {name: np.empty(num_points) for name in CHANNEL_CONVERTERS}

        for i, v in enumerate(voltages):
            set_control_out(v)
            time.sleep(settings.SCAN_SETTLE_TIME)
            values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
            publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": "manual"})
            for name in CHANNEL_CONVERTERS:
                trace[name][i] = values[name]
            state.gui_queue.put(("scan_progress", i + 1, num_points))

        if mode == "zero_crossing":
            signal_smoothed = smooth_signal(trace["error"], settings.SMOOTHING_WINDOW)
            crossings = filter_crossings_by_sign(find_zero_crossings(voltages, signal_smoothed), crossing_sign)
            candidates = [
                {"lock_voltage": v, "label": f"V={v:.4f} V   slope={slope:.4f}",
                 "start": None, "end": None}
                for v, slope in crossings
            ]
        else:
            signal_smoothed = smooth_signal(trace["dc_err"], settings.SMOOTHING_WINDOW)
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
        state.log(f"Manual scan complete - {len(candidates)} candidate(s) found.")
    except Exception as exc:
        state.log(f"Manual scan failed: {exc!r}")
        state.gui_queue.put(("scan_done", None))
    finally:
        state.manual_scan_active.clear()
