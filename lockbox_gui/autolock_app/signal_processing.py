"""Smoothing, zero-crossing search, and safe-range segment search on scan traces."""

import numpy as np


def is_within(value, lo, hi):
    return lo <= value <= hi


def smooth_signal(signal, window):
    """
    Moving average of `signal` over `window` samples, same length as the input.

    Near the ends of the trace the full window would run off the data. A plain
    np.convolve(..., mode="same") treats the missing samples as zeros, which
    drags the end points toward 0 V - creating fake zero crossings / safe-range
    edges at the scan limits. Instead, each output point averages only the
    samples that actually exist (dividing by the number of real samples in its
    window, not by `window`).
    """
    signal = np.asarray(signal, dtype=float)
    window = min(int(window), len(signal))
    if window <= 1:
        return signal
    kernel = np.ones(window)
    sums = np.convolve(signal, kernel, mode="same")
    counts = np.convolve(np.ones_like(signal), kernel, mode="same")
    return sums / counts


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
