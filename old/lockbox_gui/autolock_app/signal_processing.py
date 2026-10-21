"""Smoothing, zero-crossing search, and safe-range segment search on scan traces."""

import numpy as np


def is_within(value, lo, hi):
    return lo <= value <= hi


def smooth_signal(voltages, signal, window):
    """
    Moving average of `signal`, same length as the input, over a fixed
    VOLTAGE span: each point averages every sample within
    +/- (window - 1) / 2 * (smallest point spacing) of it.

    For a uniform scan that is exactly a `window`-sample moving average. For
    an adaptive scan it averages only close neighbours: dense points on a
    sharp feature are smoothed together, but a sparse point in a flat region
    is never averaged with a feature value 0.04 V away (which would smear the
    feature out and could create fake zero crossings).

    Near the ends of the trace, each point averages only the samples that
    actually exist, rather than treating missing ones as 0 V - so the end
    points aren't dragged toward zero (fake crossings / safe-range edges at
    the scan limits).
    """
    signal = np.asarray(signal, dtype=float)
    voltages = np.asarray(voltages, dtype=float)
    if int(window) <= 1 or len(signal) < 2:
        return signal

    order = np.argsort(voltages, kind="stable")   # sweeps may run downward
    v, y = voltages[order], signal[order]
    min_spacing = np.min(np.diff(v))
    half_width = (int(window) - 1) / 2 * min_spacing * (1 + 1e-6)  # tolerance for float spacing
    first = np.searchsorted(v, v - half_width, side="left")
    last = np.searchsorted(v, v + half_width, side="right")
    cumsum = np.concatenate(([0.0], np.cumsum(y)))
    smoothed = (cumsum[last] - cumsum[first]) / (last - first)

    result = np.empty_like(smoothed)
    result[order] = smoothed
    return result


def find_zero_crossings(voltages, signal):
    """Returns a list of (crossing_voltage, slope) tuples, unfiltered by sign."""
    crossings = []
    for i in range(len(signal) - 1):
        y0, y1 = signal[i], signal[i + 1]
        if y0 == 0.0:
            continue
        if y0 * y1 < 0.0:
            x0, x1 = voltages[i], voltages[i + 1]
            frac = -y0 / (y1 - y0)
            x_cross = x0 + frac * (x1 - x0)
            slope = (y1 - y0) / (x1 - x0)
            crossings.append((float(x_cross), float(slope)))
    return crossings


def filter_crossings_by_sign(crossings, sign):
    """Keep only crossings whose slope matches the requested sign."""
    if sign == "positive":
        return [c for c in crossings if c[1] > 0]
    if sign == "negative":
        return [c for c in crossings if c[1] < 0]
    return list(crossings)


def filter_crossings_by_slope(crossings, min_fraction):
    """
    Keep only crossings at least min_fraction times as steep as the steepest
    one in `crossings`. Drops the shallow crossings that noise produces in
    flat stretches of the error signal. Apply after the sign filter, so
    "steepest" means the steepest crossing of the wanted sign.
    """
    if not crossings:
        return []
    threshold = min_fraction * max(abs(slope) for _, slope in crossings)
    return [c for c in crossings if abs(c[1]) >= threshold]


def find_crossing_candidates(voltages, smoothed, sign, min_slope_fraction):
    """Zero crossings of `smoothed`, filtered by sign, then by slope relative to the steepest."""
    crossings = filter_crossings_by_sign(find_zero_crossings(voltages, smoothed), sign)
    return filter_crossings_by_slope(crossings, min_slope_fraction)


def find_safe_range_segments(voltages, signal, lo, hi):
    """
    Find every contiguous run of samples where lo <= signal <= hi.
    Returns a list of (start_v, end_v, mid_v, width) tuples.
    """
    segments = []
    in_segment = False
    start_idx = None
    n = len(signal)
    for i in range(n):
        within = lo <= signal[i] <= hi
        if within and not in_segment:
            in_segment = True
            start_idx = i
        elif not within and in_segment:
            in_segment = False
            start_v, end_v = voltages[start_idx], voltages[i - 1]
            segments.append((float(start_v), float(end_v), float((start_v + end_v) / 2), float(abs(end_v - start_v))))
    if in_segment:
        start_v, end_v = voltages[start_idx], voltages[-1]
        segments.append((float(start_v), float(end_v), float((start_v + end_v) / 2), float(abs(end_v - start_v))))
    return segments
