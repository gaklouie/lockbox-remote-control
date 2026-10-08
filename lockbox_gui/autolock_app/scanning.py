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
"""

import time

import numpy as np

from .channels import CHANNEL_CONVERTERS
from .config import settings
from .hardware import read_physical, set_control_out
from .telemetry import publish_values

# After a sharp feature the step may grow by at most this factor per point,
# so the feature's trailing edge stays densely sampled. It may shrink at once.
MAX_STEP_GROWTH = 2.0


def measure_point(v, pass_tag):
    """Set control out to v (physical volts), let it settle, read and publish all channels."""
    set_control_out(v)
    time.sleep(settings.SCAN_SETTLE_TIME)
    values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
    publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": pass_tag})
    return values


def _to_arrays(voltages, readings):
    trace = {name: np.array([r[name] for r in readings]) for name in CHANNEL_CONVERTERS}
    return np.array(voltages), trace


def uniform_sweep(v_min, v_max, num_points, pass_tag, on_point=None):
    """
    Sweep num_points evenly spaced voltages. on_point(values, fraction_done)
    is called after each point. Returns (voltages, {name: readings array}).
    """
    voltages = np.linspace(v_min, v_max, num_points)
    readings = []
    for i, v in enumerate(voltages):
        values = measure_point(v, pass_tag)
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
    max_step = settings.SCAN_MAX_STEP
    min_step = min(settings.SCAN_MIN_STEP, max_step)
    span = v_max - v_min

    v = v_min
    values = measure_point(v, pass_tag)
    voltages, readings = [v], [values]
    if on_point:
        on_point(values, 0.0 if span > 0 else 1.0)

    step = max_step
    while v < v_max:
        v_next = v + step
        if v_next > v_max - min_step / 2:  # land exactly on v_max, without a sliver of a last step
            v_next = v_max
        values = measure_point(v_next, pass_tag)
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
    return uniform_sweep(v_min, v_max, num_points, pass_tag, on_point)
