"""
Sweeps of control out, shared by the manual scan and the automatic autolock.

Every sweep is a single increasing pass from v_min to v_max - never stepping
backwards and never merging separate sweeps - because the actuator driven by
control out may have hysteresis, so the signal at a voltage can depend on the
direction it was approached from.

    uniform_sweep    evenly spaced points
    adaptive_sweep   step size shrinks where the tracked signal changes quickly
                     and grows (gradually) where it's flat, so sharp features
                     separated by long flat stretches get dense sampling
                     without wasting points on the flat parts

With FAST_SWEEP on, a sweep reads only the signal it searches (`track_signal`:
error, or DC error) with the ADC in its fast continuous mode - a few ms per
point instead of ~0.2 s - so slow drift has little time to distort a narrow
feature within one sweep. The other channels are then not measured, and the
returned trace only contains the tracked signal.
"""

import time

import numpy as np

from . import state
from .channels import CHANNEL_CONVERTERS
from .config import settings
from .hardware import fast_adc, read_physical, read_signal_fast, set_control_out
from .telemetry import publish_values

class ScanStopped(Exception):
    """A sweep was stopped part-way: "Stop Scan" was pressed, or the program is shutting down."""


# After a sharp feature the step may grow by at most this factor per point,
# so the feature's trailing edge stays densely sampled. It may shrink at once.
MAX_STEP_GROWTH = 2.0


def measure_point(v, pass_tag, fast_signal=None):
    """
    Set control out to v (physical volts), let it settle, read and publish.
    Reads all four channels, or - in a fast sweep - only `fast_signal`.
    """
    # Checked before every point, so a stop takes effect within one point.
    if state.stop_event.is_set():
        raise ScanStopped("the program is shutting down")
    if state.stop_scan_requested.is_set():
        raise ScanStopped(f"stopped by request before moving control out to {v:.4f} V physical "
                          f"(it stays at the previous point)")
    set_control_out(v)
    time.sleep(settings.SCAN_SETTLE_MS / 1000.0)
    if fast_signal:
        values = {fast_signal: read_signal_fast(fast_signal, settings.NUM_SAMPLES_PER_POINT)}
    else:
        values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
    publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": pass_tag})
    return values


def _to_arrays(voltages, readings):
    """(voltages, {name: array}) for the signals that were actually measured."""
    measured = [name for name in CHANNEL_CONVERTERS if name in readings[0]]
    trace = {name: np.array([r[name] for r in readings]) for name in measured}
    return np.array(voltages), trace


def _fast_signal(track_signal):
    """The one signal a fast sweep reads, or None for a normal (all-channel) sweep."""
    return track_signal if settings.FAST_SWEEP else None


def uniform_sweep(v_min, v_max, num_points, track_signal, pass_tag, on_point=None):
    """
    Sweep num_points evenly spaced voltages. on_point(values, fraction_done)
    is called after each point. Returns (voltages, {name: readings array}).
    track_signal is the signal being searched (read alone in a fast sweep).
    """
    fast_signal = _fast_signal(track_signal)
    voltages = np.linspace(v_min, v_max, num_points)
    readings = []
    with fast_adc(fast_signal):
        for i, v in enumerate(voltages):
            values = measure_point(v, pass_tag, fast_signal)
            readings.append(values)
            if on_point:
                on_point(values, (i + 1) / num_points)
    return _to_arrays(voltages, readings)


def adaptive_sweep(v_min, v_max, track_signal, pass_tag, on_point=None):
    """
    Sweep from v_min to v_max, choosing each step from the slope of
    `track_signal` over the previous step so the signal changes by about
    ADAPTIVE_TARGET_CHANGE per step, within [SCAN_MIN_STEP, SCAN_MAX_STEP].
    Same callback and return value as uniform_sweep().
    """
    with fast_adc(_fast_signal(track_signal)):
        return _adaptive_sweep(v_min, v_max, track_signal, pass_tag, on_point)


def _adaptive_sweep(v_min, v_max, track_signal, pass_tag, on_point):
    fast_signal = _fast_signal(track_signal)
    max_step = settings.SCAN_MAX_STEP
    min_step = min(settings.SCAN_MIN_STEP, max_step)
    span = v_max - v_min

    v = v_min
    values = measure_point(v, pass_tag, fast_signal)
    voltages, readings = [v], [values]
    if on_point:
        on_point(values, 0.0 if span > 0 else 1.0)

    step = max_step
    while v < v_max:
        v_next = v + step
        if v_next > v_max - min_step / 2:  # land exactly on v_max, without a sliver of a last step
            v_next = v_max
        values = measure_point(v_next, pass_tag, fast_signal)
        voltages.append(v_next)
        readings.append(values)
        if on_point:
            on_point(values, (v_next - v_min) / span)

        slope = abs(values[track_signal] - readings[-2][track_signal]) / (v_next - v)
        wanted = settings.ADAPTIVE_TARGET_CHANGE / slope if slope > 0 else max_step
        step = max(min_step, min(max_step, wanted, step * MAX_STEP_GROWTH))
        v = v_next

    return _to_arrays(voltages, readings)


def sweep(v_min, v_max, num_points, track_signal, pass_tag, on_point=None, adaptive=None):
    """Adaptive or uniform sweep depending on `adaptive` (default: the ADAPTIVE_SCAN setting)."""
    if adaptive is None:
        adaptive = settings.ADAPTIVE_SCAN
    if adaptive:
        return adaptive_sweep(v_min, v_max, track_signal, pass_tag, on_point)
    return uniform_sweep(v_min, v_max, num_points, track_signal, pass_tag, on_point)
