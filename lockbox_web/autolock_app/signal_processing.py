"""Smoothing, zero-crossing search, and safe-range segment search on scan traces."""

from typing import NamedTuple

import numpy as np


class Crossing(NamedTuple):
    """One zero crossing of a scan trace."""
    voltage: float      # control out (physical V) where the signal crosses zero
    slope: float        # signal change per volt of control out, across the crossing
    symmetry: float     # smaller lobe / larger lobe, 0-1 (1 = equal lobes)
    lobe: float         # height of the smaller lobe (V of the signal)
    left_peak: float    # control out at the peak of the left lobe
    right_peak: float   # control out at the peak of the right lobe


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
    """
    Every zero crossing of `signal`, unfiltered, as Crossing records.

    symmetry (0-1) compares the heights of the crossing's own two lobes as
    smaller / larger (see _lobe_peak). A clean dispersive crossing (e.g. the
    PDH carrier) has equal, opposite lobes and scores near 1; a crossing
    between a big lobe and a small one (e.g. between the carrier and a
    sideband) scores low. lobe is the smaller lobe's height.
    """
    signal = np.asarray(signal, dtype=float)
    indices = []      # i such that the crossing lies between samples i and i+1
    crossings = []
    for i in range(len(signal) - 1):
        y0, y1 = signal[i], signal[i + 1]
        if y0 == 0.0:
            continue                       # handled as the y1 == 0 case of the previous step
        if y0 * y1 < 0.0:
            x0, x1 = voltages[i], voltages[i + 1]
            frac = -y0 / (y1 - y0)
            x_cross = x0 + frac * (x1 - x0)
            slope = (y1 - y0) / (x1 - x0)
            indices.append(i)
            crossings.append((float(x_cross), float(slope)))
        elif y1 == 0.0 and i + 2 < len(signal) and y0 * signal[i + 2] < 0.0:
            # Lands exactly on a sample (the signal passes through zero at i+1).
            slope = (signal[i + 2] - y0) / (voltages[i + 2] - voltages[i])
            indices.append(i)
            crossings.append((float(voltages[i + 1]), float(slope)))

    # A lobe can extend at most to the neighbouring crossing (or the end of the scan).
    bounds = [-1] + indices + [len(signal) - 1]
    magnitude = np.abs(signal)
    result = []
    for k, (x_cross, slope) in enumerate(crossings):
        start, i, end = bounds[k] + 1, bounds[k + 1], bounds[k + 2]
        left, left_offset = _lobe_peak(magnitude[start:i + 1][::-1])     # walking left from sample i
        right, right_offset = _lobe_peak(magnitude[i + 1:end + 1])        # walking right from sample i+1
        larger = max(left, right)
        symmetry = float(min(left, right) / larger) if larger > 0 else 0.0
        result.append(Crossing(x_cross, slope, symmetry, min(left, right),
                               float(voltages[i - left_offset]), float(voltages[i + 1 + right_offset])))
    return result


def _lobe_peak(magnitudes):
    """
    (height, offset) of a crossing's lobe, from |signal| listed walking away
    from the crossing: follow it up to its peak, and stop once it falls below
    half that peak - the lobe has ended. This keeps a further feature of the
    same sign (with no zero crossing in between, e.g. a neighbouring PDH
    sideband) from being counted as part of the lobe, while small noise
    wiggles near the peak don't end it early. offset is the peak's position in
    `magnitudes`.
    """
    peak, offset = 0.0, 0
    for n, value in enumerate(magnitudes):
        if value > peak:
            peak, offset = value, n
        elif value < 0.5 * peak:
            break
    return float(peak), offset


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
    threshold = min_fraction * max(abs(c[1]) for c in crossings)
    return [c for c in crossings if abs(c[1]) >= threshold]


def find_crossing_candidates(voltages, smoothed, sign, min_slope_fraction,
                             min_symmetry=0.0, min_lobe_fraction=0.0):
    """
    Zero crossings of `smoothed` (Crossing records) that pass, in order:
      - the wanted sign,
      - symmetry >= min_symmetry,
      - lobe height >= min_lobe_fraction x the tallest crossing still left
        (drops noise wiggles, which are symmetric but small),
      - slope >= min_slope_fraction x the steepest crossing still left.
    Lopsided crossings are removed first, so a tall or steep one (e.g. next to
    a PDH sideband) can't set the height or slope bar.
    """
    crossings = filter_crossings_by_sign(find_zero_crossings(voltages, smoothed), sign)
    crossings = [c for c in crossings if c.symmetry >= min_symmetry]
    if crossings:
        tallest = max(c.lobe for c in crossings)
        crossings = [c for c in crossings if c.lobe >= min_lobe_fraction * tallest]
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
