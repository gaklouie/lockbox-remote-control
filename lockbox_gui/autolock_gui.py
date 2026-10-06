"""
Autolock control GUI.

Run this directly on the Raspberry Pi over an X11-forwarded SSH session
(`ssh -X pi@<address>`, then `uv run python3 autolock_gui.py`) so the
Tkinter window appears on your local machine while the DAC/ADC hardware
access happens on the Pi.

Channel A ("control out") physical voltage:
    Channel A drives a shift/amplify circuit, so every channel-A voltage
    in this program (scan range, reset-on-failure voltage, plot axes,
    lock points) is specified in PHYSICAL volts, not raw DAC volts:

        V_physical = V_DAC * 3.826 - 9.78   (DAC_SHIFT_GAIN, DAC_SHIFT_OFFSET)

    set_channel_a_physical_voltage() converts and clips to the DAC's
    achievable raw range, warning (via the live log) if clipping occurs.
    Channels B, C, D are NOT run through this conversion - they're just
    digital-style HIGH/LOW drive lines, fixed at DAC_HIGH_VOLTAGE (4.0V)
    and DAC_LOW_VOLTAGE (0.0V), raw DAC volts, not adjustable from the GUI.

Autolock modes (selectable both per manual scan and for the automatic/
background autolock):
    - "zero_crossing": scan channel A, find zero crossings in the
      "error" channel (optionally filtered to only positive-going or
      only negative-going crossings via CROSSING_SIGN), lock to the
      steepest one. Background trigger: slow_output/fast_output leaving
      their safe range.
    - "dc_err_range": scan channel A, find every contiguous region where
      "dc_err" is inside its safe range, lock to the midpoint of the
      WIDEST such region (most robust to noise). Background trigger:
      dc_err itself leaving its safe range.
    Both use the same coarse-then-fine two-pass scan structure.

GUI layout (three tabs):
    - "Scan & Lock": configure and run a manual scan, choose the search
      mode and crossing sign, see error signals and outputs plotted
      against control out with safe ranges shaded and every lock
      candidate numbered on the plot, pick one from the list, lock to it.
    - "Autolock Settings": settings grouped into Safe Range Parameters,
      Scan Parameters, and General Program Parameters (including the
      autolock mode and crossing sign), plus Engage/Disengage and
      Run-Now controls.
    - "Live Monitor": full-size separate Outputs / Errors strip-charts
      plus a shared scrolling log, always running from GUI startup
      regardless of autolock state, sampled at MONITOR_INTERVAL
      independently of the (usually slower) MQTT/Influx LOG_INTERVAL.

Install dependencies (see pyproject.toml / uv.lock):
    uv sync
Tkinter ships with most Python installs; if missing on Raspberry Pi OS:
    sudo apt install python3-tk
"""

import os
import json
import time
import threading
import queue
from collections import deque
from dataclasses import dataclass, fields

import numpy as np
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

import tkinter as tk
from tkinter import ttk, messagebox

import board
import busio
import adafruit_mcp4728
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# --------------------------------------------------------------------------
# Plot color cycle (applies to every figure created after this point)
# --------------------------------------------------------------------------
COLOR_CYCLE = ['#882255', '#0F7D33', '#332288', '#DDCC77',
               '#C7112F', '#4AAF9E', '#AA4499', '#C1DA49']
plt.rcParams['axes.prop_cycle'] = plt.cycler(color=COLOR_CYCLE)


def place_legend_outside(ax, fontsize=8):
    """Put a legend to the right of the axes instead of overlapping the data."""
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), fontsize=fontsize, borderaxespad=0.0)


# --------------------------------------------------------------------------
# MQTT configuration (matches existing project pattern)
# --------------------------------------------------------------------------
load_dotenv(".env")

mqttTopic = "experiment/sensor/ncc-1701/adc"
mqttBrokerAddress = os.environ.get("ADDRESS")
mqttPort = int(os.environ.get("MQTT_PORT", 1883))
credentials = {
    "username": os.environ.get("MQTT_USERNAME"),
    "password": os.environ.get("PASSWORD"),
}

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(**credentials)
mqtt_client.connect(mqttBrokerAddress, mqttPort)
mqtt_client.loop_start()

# --------------------------------------------------------------------------
# Hardware setup
# --------------------------------------------------------------------------
VDD_VOLTAGE = 5.0   # Supply voltage feeding the MCP4728 (raw DAC full scale)
ADS_GAIN = 1         # 1 -> +/-4.096V input range; use 2/3 for +/-6.144V

# Channel A shift/amplify circuit: V_physical = V_DAC * GAIN + OFFSET
DAC_SHIFT_GAIN = 3.826
DAC_SHIFT_OFFSET = -9.78

# Channels B, C, D: fixed raw DAC volts, not physical-converted, not GUI-adjustable
DAC_HIGH_VOLTAGE = 4.0
DAC_LOW_VOLTAGE = 0.0

i2c = busio.I2C(board.SCL, board.SDA)
i2c_lock = threading.Lock()  # serializes raw I2C transactions across threads

dac = adafruit_mcp4728.MCP4728(i2c)
for _ch in (dac.channel_a, dac.channel_b, dac.channel_c, dac.channel_d):
    _ch.vref = adafruit_mcp4728.Vref.VDD
    _ch.gain = 1

ads = ADS.ADS1115(i2c)
ads.gain = ADS_GAIN
adc_channels = [AnalogIn(ads, i) for i in range(4)]  # channels 0-3


# --------------------------------------------------------------------------
# Channel conversions (raw ADC volts -> true, physical volts)
# --------------------------------------------------------------------------
def to_slow_output(adc_voltage):
    return 56 * (-0.227273 + 0.118867 * adc_voltage)


def to_fast_output(adc_voltage):
    return 56 * (-0.227273 + 0.118867 * adc_voltage)


def to_error(adc_voltage):
    return 18 * (-0.185185 + 0.0925927 * adc_voltage)


def to_dc_err(adc_voltage):
    return adc_voltage


CHANNEL_CONVERTERS = {
    "slow_output": to_slow_output,
    "fast_output": to_fast_output,
    "error": to_error,
    "dc_err": to_dc_err,
}
ERROR_SIGNAL_NAMES = ["error", "dc_err"]
OUTPUT_SIGNAL_NAMES = ["slow_output", "fast_output"]


# --------------------------------------------------------------------------
# Settings (editable live from the GUI; plain attributes, no extra lock -
# the GIL makes simple read/write of these safe enough between threads)
# --------------------------------------------------------------------------
@dataclass
class Settings:
    # --- Safe range parameters ---
    SLOW_OUTPUT_SAFE_MIN: float = 1.0
    SLOW_OUTPUT_SAFE_MAX: float = 3.0
    FAST_OUTPUT_SAFE_MIN: float = 1.0
    FAST_OUTPUT_SAFE_MAX: float = 3.0
    DC_ERR_SAFE_MIN: float = 1.0
    DC_ERR_SAFE_MAX: float = 3.0

    # --- Scan parameters ---
    SCAN_MIN_VOLTAGE: float = 0.0    # control out, PHYSICAL volts
    SCAN_MAX_VOLTAGE: float = 4.0    # control out, PHYSICAL volts
    NUM_COARSE_POINTS: int = 51
    NUM_FINE_POINTS: int = 41
    MANUAL_SCAN_POINTS: int = 101
    SCAN_SETTLE_TIME: float = 0.01
    NUM_SAMPLES_PER_POINT: int = 5
    SMOOTHING_WINDOW: int = 3

    # --- General program parameters ---
    AUTOLOCK_MODE: str = "zero_crossing"   # "zero_crossing" or "dc_err_range"
    CROSSING_SIGN: str = "positive"        # "positive", "negative", or "both"
    RESET_VOLTAGE_ON_FAILURE: float = 2.0  # control out, PHYSICAL volts
    PRIME_SETTLE_TIME: float = 0.1
    MONITOR_INTERVAL: float = 0.2          # fast: GUI display sampling rate (s)
    LOG_INTERVAL: float = 1.0              # slow: MQTT/Influx logging rate (s)
    LOCK_RETRY_DELAY: float = 1.0
    SAVE_SCAN_PLOTS: bool = True
    SCAN_PLOT_DIR: str = "scan_plots"


settings = Settings()

# --------------------------------------------------------------------------
# Shared threading state
# --------------------------------------------------------------------------
stop_event = threading.Event()
autolock_engaged = threading.Event()
manual_scan_active = threading.Event()
gui_queue = queue.Queue()


# --------------------------------------------------------------------------
# Low-level hardware helpers
# --------------------------------------------------------------------------
def voltage_to_dac_value(voltage, vdd=VDD_VOLTAGE):
    value = int(round((voltage / vdd) * 65535))
    return max(0, min(65535, value))


def set_dac_voltage(channel, voltage):
    """Set a DAC channel to a raw DAC voltage (0-VDD_VOLTAGE)."""
    with i2c_lock:
        channel.value = voltage_to_dac_value(voltage)


def set_channel_a_physical_voltage(v_physical):
    """
    Set channel A ("control out") to a target PHYSICAL voltage, converting
    through the shift/amplify circuit and clipping to the DAC's achievable
    raw range (warning via the live log if clipping was needed).
    """
    v_dac = (v_physical - DAC_SHIFT_OFFSET) / DAC_SHIFT_GAIN
    clipped = max(0.0, min(VDD_VOLTAGE, v_dac))
    if abs(clipped - v_dac) > 1e-9:
        gui_queue.put(("log", f"WARNING: requested control out {v_physical:.4f} V physical "
                               f"needs {v_dac:.4f} V at the DAC, outside [0, {VDD_VOLTAGE}] V - "
                               f"clipped to {clipped:.4f} V raw."))
    set_dac_voltage(dac.channel_a, clipped)


def read_raw_averaged(num_samples=1):
    sums = [0.0, 0.0, 0.0, 0.0]
    with i2c_lock:
        for _ in range(num_samples):
            for i, chan in enumerate(adc_channels):
                sums[i] += chan.voltage
    return [s / num_samples for s in sums]


def read_physical(num_samples=1):
    raw = read_raw_averaged(num_samples)
    return {
        "slow_output": to_slow_output(raw[0]),
        "fast_output": to_fast_output(raw[1]),
        "error": to_error(raw[2]),
        "dc_err": to_dc_err(raw[3]),
    }


def is_within(value, lo, hi):
    return lo <= value <= hi


def publish_values(values, state, extra_fields=None):
    payload = {"timestamp": time.time(), "state": state}
    payload.update(values)
    if extra_fields:
        payload.update(extra_fields)
    mqtt_client.publish(mqttTopic, json.dumps(payload))


def short_caps_and_block_pid():
    """Set DAC channels B, C, D HIGH (fixed, raw DAC volts)."""
    set_dac_voltage(dac.channel_b, DAC_HIGH_VOLTAGE)
    set_dac_voltage(dac.channel_c, DAC_HIGH_VOLTAGE)
    set_dac_voltage(dac.channel_d, DAC_HIGH_VOLTAGE)
    time.sleep(settings.PRIME_SETTLE_TIME)


def restart_pid():
    """Set DAC channels B, C, D LOW (fixed, raw DAC volts)."""
    set_dac_voltage(dac.channel_b, DAC_LOW_VOLTAGE)
    set_dac_voltage(dac.channel_c, DAC_LOW_VOLTAGE)
    set_dac_voltage(dac.channel_d, DAC_LOW_VOLTAGE)


# --------------------------------------------------------------------------
# Signal processing helpers
# --------------------------------------------------------------------------
def smooth_signal(signal, window):
    if window <= 1:
        return np.asarray(signal, dtype=float)
    kernel = np.ones(window) / window
    return np.convolve(signal, kernel, mode="same")


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


# --------------------------------------------------------------------------
# Scan-plot saving (used by the automatic autolock)
# --------------------------------------------------------------------------
def _plot_autolock_pass(ax, data, title):
    ax.plot(data["voltages"], data["raw"], ".", alpha=0.4, label=f"raw {data['signal_name']}")
    ax.plot(data["voltages"], data["smoothed"], "-", label=f"smoothed {data['signal_name']}")

    if data["mode"] == "zero_crossing":
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        for v, _ in data["crossings"]:
            ax.axvline(v, color="gray", linestyle=":", alpha=0.6)
    else:
        ax.axhspan(data["safe_min"], data["safe_max"], color="tab:green", alpha=0.15, label="safe range")
        for start, end, _, _ in data["segments"]:
            ax.axvspan(start, end, color="gray", alpha=0.15)

    if data["chosen"] is not None:
        chosen_v = data["chosen"][0]
        y_at_chosen = 0 if data["mode"] == "zero_crossing" else (data["safe_min"] + data["safe_max"]) / 2
        ax.plot(chosen_v, y_at_chosen, "r*", markersize=16, label=f"chosen ({chosen_v:.4f} V)")

    ax.set_xlabel("control out, physical (V)")
    ax.set_ylabel(f"{data['signal_name']} (V)")
    ax.set_title(title)
    place_legend_outside(ax)
    ax.grid(True, alpha=0.3)


def save_autolock_scan_plot(session_id, coarse_data, fine_data):
    if not settings.SAVE_SCAN_PLOTS:
        return
    os.makedirs(settings.SCAN_PLOT_DIR, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(15, 5))
    _plot_autolock_pass(axes[0], coarse_data, "Coarse scan")
    if fine_data is not None:
        _plot_autolock_pass(axes[1], fine_data, "Fine scan")
    else:
        axes[1].text(0.5, 0.5, "No lock candidate found -\nfine pass skipped",
                      ha="center", va="center", transform=axes[1].transAxes)
        axes[1].set_title("Fine scan")
    fig.suptitle(f"Autolock scan ({coarse_data['mode']}) - {session_id}")
    fig.tight_layout()
    fig.subplots_adjust(right=0.85)
    filename = os.path.join(settings.SCAN_PLOT_DIR, f"{session_id}_scan.png")
    fig.savefig(filename, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------
# Automatic autolock (triggered by monitor loop, or run on-demand)
# --------------------------------------------------------------------------
def autolock_scan_pass(v_min, v_max, num_points, pass_tag):
    """Sweep channel A (physical volts) recording every channel at each point."""
    voltages = np.linspace(v_min, v_max, num_points)
    trace = {name: np.empty(num_points) for name in CHANNEL_CONVERTERS}
    for i, v in enumerate(voltages):
        set_channel_a_physical_voltage(v)
        time.sleep(settings.SCAN_SETTLE_TIME)
        values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
        publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": pass_tag})
        gui_queue.put(("reading", time.time(), values))
        for name in CHANNEL_CONVERTERS:
            trace[name][i] = values[name]
    return voltages, trace


def _autolock_zero_crossing(log_fn):
    log_fn(f"Starting zero-crossing autolock on the error signal (sign={settings.CROSSING_SIGN})...")
    short_caps_and_block_pid()
    session_id = time.strftime("%Y%m%d_%H%M%S")

    coarse_step = (settings.SCAN_MAX_VOLTAGE - settings.SCAN_MIN_VOLTAGE) / (settings.NUM_COARSE_POINTS - 1)
    coarse_v, coarse_trace = autolock_scan_pass(settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE,
                                                  settings.NUM_COARSE_POINTS, "coarse")
    coarse_smoothed = smooth_signal(coarse_trace["error"], settings.SMOOTHING_WINDOW)
    coarse_crossings = filter_crossings_by_sign(find_zero_crossings(coarse_v, coarse_smoothed), settings.CROSSING_SIGN)
    coarse_data = {"mode": "zero_crossing", "signal_name": "error", "voltages": coarse_v,
                   "raw": coarse_trace["error"], "smoothed": coarse_smoothed,
                   "crossings": coarse_crossings, "chosen": None}

    if not coarse_crossings:
        log_fn("No matching zero crossings found - autolock failed.")
        save_autolock_scan_plot(session_id, coarse_data, None)
        set_channel_a_physical_voltage(settings.RESET_VOLTAGE_ON_FAILURE)
        return False

    coarse_best_v, coarse_best_slope = max(coarse_crossings, key=lambda c: abs(c[1]))
    coarse_data["chosen"] = (coarse_best_v, coarse_best_slope)
    log_fn(f"Coarse pass found {len(coarse_crossings)} crossing(s); "
           f"largest near {coarse_best_v:.4f} V. Refining...")

    fine_min = max(settings.SCAN_MIN_VOLTAGE, coarse_best_v - coarse_step)
    fine_max = min(settings.SCAN_MAX_VOLTAGE, coarse_best_v + coarse_step)
    fine_v, fine_trace = autolock_scan_pass(fine_min, fine_max, settings.NUM_FINE_POINTS, "fine")
    fine_smoothed = smooth_signal(fine_trace["error"], settings.SMOOTHING_WINDOW)
    fine_crossings = filter_crossings_by_sign(find_zero_crossings(fine_v, fine_smoothed), settings.CROSSING_SIGN)

    if fine_crossings:
        lock_v, lock_slope = max(fine_crossings, key=lambda c: abs(c[1]))
    else:
        lock_v, lock_slope = coarse_best_v, coarse_best_slope

    fine_data = {"mode": "zero_crossing", "signal_name": "error", "voltages": fine_v,
                 "raw": fine_trace["error"], "smoothed": fine_smoothed,
                 "crossings": fine_crossings, "chosen": (lock_v, lock_slope)}
    save_autolock_scan_plot(session_id, coarse_data, fine_data)

    set_channel_a_physical_voltage(lock_v)
    log_fn(f"Locked: control out = {lock_v:.4f} V physical (slope {lock_slope:.4f})")
    restart_pid()
    return True


def _autolock_dc_err_range(log_fn):
    log_fn("Starting dc_err safe-range autolock...")
    short_caps_and_block_pid()
    session_id = time.strftime("%Y%m%d_%H%M%S")

    coarse_step = (settings.SCAN_MAX_VOLTAGE - settings.SCAN_MIN_VOLTAGE) / (settings.NUM_COARSE_POINTS - 1)
    coarse_v, coarse_trace = autolock_scan_pass(settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE,
                                                  settings.NUM_COARSE_POINTS, "coarse")
    coarse_smoothed = smooth_signal(coarse_trace["dc_err"], settings.SMOOTHING_WINDOW)
    coarse_segments = find_safe_range_segments(coarse_v, coarse_smoothed,
                                                settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX)
    coarse_data = {"mode": "dc_err_range", "signal_name": "dc_err", "voltages": coarse_v,
                   "raw": coarse_trace["dc_err"], "smoothed": coarse_smoothed,
                   "segments": coarse_segments, "chosen": None,
                   "safe_min": settings.DC_ERR_SAFE_MIN, "safe_max": settings.DC_ERR_SAFE_MAX}

    if not coarse_segments:
        log_fn("No safe dc_err region found - autolock failed.")
        save_autolock_scan_plot(session_id, coarse_data, None)
        set_channel_a_physical_voltage(settings.RESET_VOLTAGE_ON_FAILURE)
        return False

    widest = max(coarse_segments, key=lambda s: s[3])
    coarse_mid = widest[2]
    coarse_data["chosen"] = (coarse_mid, None)
    log_fn(f"Coarse pass found {len(coarse_segments)} safe region(s); "
           f"widest centered near {coarse_mid:.4f} V. Refining...")

    fine_min = max(settings.SCAN_MIN_VOLTAGE, coarse_mid - coarse_step)
    fine_max = min(settings.SCAN_MAX_VOLTAGE, coarse_mid + coarse_step)
    fine_v, fine_trace = autolock_scan_pass(fine_min, fine_max, settings.NUM_FINE_POINTS, "fine")
    fine_smoothed = smooth_signal(fine_trace["dc_err"], settings.SMOOTHING_WINDOW)
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

    set_channel_a_physical_voltage(lock_v)
    log_fn(f"Locked: control out = {lock_v:.4f} V physical (dc_err safe-region midpoint)")
    restart_pid()
    return True


def autolock_once(log_fn=print):
    if settings.AUTOLOCK_MODE == "dc_err_range":
        return _autolock_dc_err_range(log_fn)
    return _autolock_zero_crossing(log_fn)


# --------------------------------------------------------------------------
# Background threads
# --------------------------------------------------------------------------
def monitor_loop():
    last_log_time = 0.0
    while not stop_event.is_set():
        if manual_scan_active.is_set():
            time.sleep(0.2)
            continue

        values = read_physical(num_samples=1)
        gui_queue.put(("reading", time.time(), values))  # always feeds the fast live display

        now = time.time()
        if now - last_log_time >= settings.LOG_INTERVAL:
            publish_values(values, "monitoring")
            last_log_time = now

        if autolock_engaged.is_set():
            if settings.AUTOLOCK_MODE == "dc_err_range":
                triggered = not is_within(values["dc_err"], settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX)
                trigger_msg = f"dc_err={values['dc_err']:.4f}V outside [{settings.DC_ERR_SAFE_MIN}, {settings.DC_ERR_SAFE_MAX}]"
            else:
                slow_ok = is_within(values["slow_output"], settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX)
                fast_ok = is_within(values["fast_output"], settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX)
                triggered = not (slow_ok and fast_ok)
                trigger_msg = f"slow_ok={slow_ok} fast_ok={fast_ok}"

            if triggered:
                gui_queue.put(("log", f"Trigger: {trigger_msg}"))
                locked = autolock_once(log_fn=lambda m: gui_queue.put(("log", m)))
                if not locked:
                    time.sleep(settings.LOCK_RETRY_DELAY)
                continue

        time.sleep(settings.MONITOR_INTERVAL)


def run_manual_scan(mode, crossing_sign):
    manual_scan_active.set()
    try:
        gui_queue.put(("log", f"Starting manual scan (mode={mode}, sign={crossing_sign})..."))
        short_caps_and_block_pid()

        voltages = np.linspace(settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE, settings.MANUAL_SCAN_POINTS)
        trace = {name: np.empty(settings.MANUAL_SCAN_POINTS) for name in CHANNEL_CONVERTERS}

        for i, v in enumerate(voltages):
            set_channel_a_physical_voltage(v)
            time.sleep(settings.SCAN_SETTLE_TIME)
            values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
            publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": "manual"})
            for name in CHANNEL_CONVERTERS:
                trace[name][i] = values[name]
            gui_queue.put(("scan_progress", i + 1, settings.MANUAL_SCAN_POINTS))

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

        gui_queue.put(("scan_done", {
            "voltages": voltages, "trace": trace, "mode": mode,
            "signal_smoothed": signal_smoothed, "candidates": candidates,
        }))
        gui_queue.put(("log", f"Manual scan complete - {len(candidates)} candidate(s) found."))
    except Exception as exc:
        gui_queue.put(("log", f"Manual scan failed: {exc}"))
    finally:
        manual_scan_active.clear()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class LiveMonitorPanel(ttk.Frame):
    """Full-size, separate Outputs / Errors strip-charts + a shared scrolling log."""

    MAX_POINTS = 300
    MAX_LOG_LINES = 500

    def __init__(self, parent):
        super().__init__(parent)

        self.times = deque(maxlen=self.MAX_POINTS)
        self.series = {name: deque(maxlen=self.MAX_POINTS) for name in CHANNEL_CONVERTERS}
        self.t0 = time.time()

        plots_frame = ttk.Frame(self)
        plots_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        out_frame = ttk.LabelFrame(plots_frame, text="Outputs")
        out_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        fig_out = plt.Figure(figsize=(7, 5))
        self.ax_out = fig_out.add_subplot(111)
        self.lines_out = {}
        for name in OUTPUT_SIGNAL_NAMES:
            (line,) = self.ax_out.plot([], [], label=name)
            self.lines_out[name] = line
        self.ax_out.set_xlabel("time (s)")
        self.ax_out.set_ylabel("volts")
        self.ax_out.grid(True, alpha=0.3)
        fig_out.subplots_adjust(right=0.72)
        self.canvas_out = FigureCanvasTkAgg(fig_out, master=out_frame)
        self.canvas_out.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        err_frame = ttk.LabelFrame(plots_frame, text="Errors")
        err_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        fig_err = plt.Figure(figsize=(7, 5))
        self.ax_err = fig_err.add_subplot(111)
        self.lines_err = {}
        for name in ERROR_SIGNAL_NAMES:
            (line,) = self.ax_err.plot([], [], label=name)
            self.lines_err[name] = line
        self.ax_err.set_xlabel("time (s)")
        self.ax_err.set_ylabel("volts")
        self.ax_err.grid(True, alpha=0.3)
        fig_err.subplots_adjust(right=0.72)
        self.canvas_err = FigureCanvasTkAgg(fig_err, master=err_frame)
        self.canvas_err.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.out_band_patches = []
        self.err_band_patches = []
        self.refresh_safe_bands()

        log_frame = ttk.Frame(self)
        log_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="none")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh_safe_bands(self):
        for patch in self.out_band_patches:
            patch.remove()
        self.out_band_patches = []
        for patch in self.err_band_patches:
            patch.remove()
        self.err_band_patches = []

        out_bands = [
            ("slow_output", settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX, "tab:blue"),
            ("fast_output", settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX, "tab:orange"),
        ]
        for name, lo, hi, color in out_bands:
            patch = self.ax_out.axhspan(lo, hi, color=color, alpha=0.12, label=f"{name} safe range")
            self.out_band_patches.append(patch)
        place_legend_outside(self.ax_out)

        patch = self.ax_err.axhspan(settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX,
                                     color="tab:green", alpha=0.12, label="dc_err safe range")
        self.err_band_patches.append(patch)
        place_legend_outside(self.ax_err)

        self.canvas_out.draw_idle()
        self.canvas_err.draw_idle()

    def record_reading(self, timestamp, values, log_buffer=None):
        """
        Cheap, data-only update: append to the history deques (and
        optionally a log-line buffer). Does NOT touch the canvases or the
        log widget - call redraw_plots() / log_many() separately, once per
        batch, rather than once per reading. This is what keeps a fast
        stream of readings (e.g. during a scan) from forcing a full
        matplotlib redraw on every single point.
        """
        t = timestamp - self.t0
        self.times.append(t)
        for name, val in values.items():
            self.series[name].append(val)
        if log_buffer is not None:
            line_str = " ".join(f"{k}={v:.4f}" for k, v in values.items())
            log_buffer.append(f"[{time.strftime('%H:%M:%S')}] {line_str}")

    def redraw_plots(self):
        """Push the current deque contents onto the lines and redraw once."""
        for name, line in self.lines_out.items():
            line.set_data(self.times, self.series[name])
        for name, line in self.lines_err.items():
            line.set_data(self.times, self.series[name])

        if self.times:
            xmin, xmax = max(0, self.times[0]), self.times[-1] + 1
            self.ax_out.set_xlim(xmin, xmax)
            self.ax_err.set_xlim(xmin, xmax)

            out_vals = [v for n in OUTPUT_SIGNAL_NAMES for v in self.series[n]]
            if out_vals:
                m = 0.5
                self.ax_out.set_ylim(min(out_vals) - m, max(out_vals) + m)

            err_vals = [v for n in ERROR_SIGNAL_NAMES for v in self.series[n]]
            if err_vals:
                m = 0.5
                self.ax_err.set_ylim(min(err_vals) - m, max(err_vals) + m)

        self.canvas_out.draw_idle()
        self.canvas_err.draw_idle()

    def log(self, message):
        self.log_many([message])

    def log_many(self, messages):
        """Insert several log lines in one Text widget operation, and do
        the overflow-trim/scroll-to-end just once, instead of per line."""
        if not messages:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, "\n".join(messages) + "\n")
        num_lines = int(self.log_text.index("end-1c").split(".")[0])
        if num_lines > self.MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{num_lines - self.MAX_LOG_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")


class ScanTab(ttk.Frame):
    """Manual scan, mode selection, candidate selection, and lock controls."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.current_voltages = None
        self.current_trace = None
        self.current_candidates = []

        controls = ttk.Frame(self)
        controls.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        ttk.Label(controls, text="Scan min (physical V)").grid(row=0, column=0, sticky="w")
        self.min_var = tk.StringVar(value=str(settings.SCAN_MIN_VOLTAGE))
        ttk.Entry(controls, textvariable=self.min_var, width=8).grid(row=0, column=1)

        ttk.Label(controls, text="Scan max (physical V)").grid(row=0, column=2, sticky="w")
        self.max_var = tk.StringVar(value=str(settings.SCAN_MAX_VOLTAGE))
        ttk.Entry(controls, textvariable=self.max_var, width=8).grid(row=0, column=3)

        ttk.Label(controls, text="Points").grid(row=0, column=4, sticky="w")
        self.points_var = tk.StringVar(value=str(settings.MANUAL_SCAN_POINTS))
        ttk.Entry(controls, textvariable=self.points_var, width=8).grid(row=0, column=5)

        ttk.Label(controls, text="Lock mode:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mode_var = tk.StringVar(value=settings.AUTOLOCK_MODE)
        ttk.Radiobutton(controls, text="Search for crossings in error", value="zero_crossing",
                         variable=self.mode_var).grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Radiobutton(controls, text="Search for safe range in dc_err", value="dc_err_range",
                         variable=self.mode_var).grid(row=1, column=3, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(controls, text="Crossing sign:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.sign_var = tk.StringVar(value=settings.CROSSING_SIGN)
        ttk.Radiobutton(controls, text="Positive", value="positive",
                         variable=self.sign_var).grid(row=2, column=1, sticky="w", pady=(4, 0))
        ttk.Radiobutton(controls, text="Negative", value="negative",
                         variable=self.sign_var).grid(row=2, column=2, sticky="w", pady=(4, 0))
        ttk.Radiobutton(controls, text="Both", value="both",
                         variable=self.sign_var).grid(row=2, column=3, sticky="w", pady=(4, 0))

        self.scan_button = ttk.Button(controls, text="Run Scan", command=self.on_run_scan)
        self.scan_button.grid(row=1, column=5, padx=(12, 0), pady=(6, 0))

        self.progress = ttk.Progressbar(controls, orient="horizontal", length=160, mode="determinate")
        self.progress.grid(row=1, column=6, padx=(8, 0), pady=(6, 0))

        fig = plt.Figure(figsize=(10, 6))
        self.ax_err = fig.add_subplot(211)
        self.ax_out = fig.add_subplot(212)
        self.fig = fig

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = FigureCanvasTkAgg(fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, canvas_frame)
        toolbar.update()

        bottom = ttk.Frame(self)
        bottom.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        ttk.Label(bottom, text="Lock candidates:").pack(side=tk.LEFT)
        self.candidate_list = tk.Listbox(bottom, height=4, width=60)
        self.candidate_list.pack(side=tk.LEFT, padx=(6, 6))

        ttk.Button(bottom, text="Lock to Selected", command=self.on_lock_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(bottom, text="Release (restart PID)", command=self.on_release).pack(side=tk.LEFT, padx=4)

    def on_run_scan(self):
        if autolock_engaged.is_set():
            messagebox.showwarning("Busy", "Disengage autolock before running a manual scan.")
            return
        if manual_scan_active.is_set():
            return
        try:
            settings.SCAN_MIN_VOLTAGE = float(self.min_var.get())
            settings.SCAN_MAX_VOLTAGE = float(self.max_var.get())
            settings.MANUAL_SCAN_POINTS = int(self.points_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Scan min/max/points must be numeric.")
            return

        self.candidate_list.delete(0, tk.END)
        self.progress.configure(maximum=settings.MANUAL_SCAN_POINTS, value=0)
        self.scan_button.configure(state="disabled")

        threading.Thread(
            target=run_manual_scan, args=(self.mode_var.get(), self.sign_var.get()), daemon=True
        ).start()

    def on_scan_progress(self, i, total):
        self.progress.configure(value=i)

    def on_scan_done(self, data):
        self.scan_button.configure(state="normal")
        self.current_voltages = data["voltages"]
        self.current_trace = data["trace"]
        self.current_candidates = data["candidates"]
        mode = data["mode"]

        self.ax_err.clear()
        self.ax_out.clear()

        self.ax_err.axhspan(settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX,
                             color="tab:green", alpha=0.15, label="dc_err safe range")
        for name in ERROR_SIGNAL_NAMES:
            self.ax_err.plot(data["voltages"], data["trace"][name], label=name, alpha=0.6)
        self.ax_err.plot(data["voltages"], data["signal_smoothed"], "k-",
                          label=f"{'error' if mode == 'zero_crossing' else 'dc_err'} (smoothed)")

        if mode == "zero_crossing":
            self.ax_err.axhline(0, color="black", linewidth=0.8, linestyle="--")
            marker_y = 0
        else:
            marker_y = (settings.DC_ERR_SAFE_MIN + settings.DC_ERR_SAFE_MAX) / 2

        # Visually indicate every candidate: shading/lines, plus a numbered marker
        # matching the candidate list order below.
        for idx, c in enumerate(self.current_candidates):
            if mode == "zero_crossing":
                self.ax_err.axvline(c["lock_voltage"], color="gray", linestyle=":", alpha=0.6)
            else:
                self.ax_err.axvspan(c["start"], c["end"], color="gray", alpha=0.2)
            self.ax_err.plot(c["lock_voltage"], marker_y, "o", color="black", markersize=6)
            self.ax_err.annotate(str(idx + 1), (c["lock_voltage"], marker_y),
                                  textcoords="offset points", xytext=(0, 8),
                                  ha="center", fontsize=8, fontweight="bold")

        self.ax_err.set_xlabel("control out, physical (V)")
        self.ax_err.set_ylabel("error signals (V)")
        place_legend_outside(self.ax_err)
        self.ax_err.grid(True, alpha=0.3)

        self.ax_out.axhspan(settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX,
                             color="tab:blue", alpha=0.12, label="slow_output safe range")
        self.ax_out.axhspan(settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX,
                             color="tab:orange", alpha=0.12, label="fast_output safe range")
        for name in OUTPUT_SIGNAL_NAMES:
            self.ax_out.plot(data["voltages"], data["trace"][name], label=name)
        self.ax_out.set_xlabel("control out, physical (V)")
        self.ax_out.set_ylabel("outputs (V)")
        place_legend_outside(self.ax_out)
        self.ax_out.grid(True, alpha=0.3)

        self.fig.tight_layout()
        self.fig.subplots_adjust(right=0.78)
        self.canvas.draw_idle()

        for idx, c in enumerate(self.current_candidates):
            self.candidate_list.insert(tk.END, f"{idx + 1}. {c['label']}")

    def on_lock_selected(self):
        sel = self.candidate_list.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Select a candidate from the list first.")
            return
        v = self.current_candidates[sel[0]]["lock_voltage"]
        set_channel_a_physical_voltage(v)
        restart_pid()
        self.app.monitor_panel.log(f"[manual] Locked to control out = {v:.4f} V physical")

    def on_release(self):
        restart_pid()
        self.app.monitor_panel.log("[manual] PID restarted (B, C, D set LOW) without changing control out.")


class SettingsTab(ttk.Frame):
    """Autolock settings, grouped, + engage/disengage control."""

    SAFE_RANGE_FIELDS = {
        "SLOW_OUTPUT_SAFE_MIN": "slow_output safe min (V)",
        "SLOW_OUTPUT_SAFE_MAX": "slow_output safe max (V)",
        "FAST_OUTPUT_SAFE_MIN": "fast_output safe min (V)",
        "FAST_OUTPUT_SAFE_MAX": "fast_output safe max (V)",
        "DC_ERR_SAFE_MIN": "dc_err safe min (V)",
        "DC_ERR_SAFE_MAX": "dc_err safe max (V)",
    }
    SCAN_FIELDS = {
        "SCAN_MIN_VOLTAGE": "Scan output min (physical V)",
        "SCAN_MAX_VOLTAGE": "Scan output max (physical V)",
        "NUM_COARSE_POINTS": "Coarse pass points",
        "NUM_FINE_POINTS": "Fine pass points",
        "SCAN_SETTLE_TIME": "Settle time (s)",
        "NUM_SAMPLES_PER_POINT": "Samples averaged/point",
        "SMOOTHING_WINDOW": "Smoothing window (1 = off)",
    }
    GENERAL_FIELDS = {
        "RESET_VOLTAGE_ON_FAILURE": "Reset voltage on failure (physical V)",
        "PRIME_SETTLE_TIME": "Prime (short caps) settle time (s)",
        "MONITOR_INTERVAL": "Monitor sample interval (s)",
        "LOG_INTERVAL": "Logging interval (s)",
        "LOCK_RETRY_DELAY": "Retry delay on failure (s)",
        "SAVE_SCAN_PLOTS": "Save autolock scan plots",
        "SCAN_PLOT_DIR": "Scan plot directory",
    }
    FIELD_TYPES = {f.name: f.type for f in fields(Settings)}

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.vars = {}

        groups_frame = ttk.Frame(self)
        groups_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        safe_frame = ttk.LabelFrame(groups_frame, text="Safe Range Parameters")
        safe_frame.grid(row=0, column=0, sticky="n", padx=(0, 8))
        self._build_field_group(safe_frame, self.SAFE_RANGE_FIELDS)

        scan_frame = ttk.LabelFrame(groups_frame, text="Scan Parameters")
        scan_frame.grid(row=0, column=1, sticky="n", padx=8)
        self._build_field_group(scan_frame, self.SCAN_FIELDS)

        general_frame = ttk.LabelFrame(groups_frame, text="General Program Parameters")
        general_frame.grid(row=0, column=2, sticky="n", padx=(8, 0))

        ttk.Label(general_frame, text="Autolock mode:").grid(row=0, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.mode_var = tk.StringVar(value=settings.AUTOLOCK_MODE)
        ttk.Radiobutton(general_frame, text="Zero crossing (error + output range)", value="zero_crossing",
                         variable=self.mode_var).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(general_frame, text="DC err safe range", value="dc_err_range",
                         variable=self.mode_var).grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Label(general_frame, text="Crossing sign:").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.sign_var = tk.StringVar(value=settings.CROSSING_SIGN)
        ttk.Radiobutton(general_frame, text="Positive", value="positive",
                         variable=self.sign_var).grid(row=4, column=0, sticky="w")
        ttk.Radiobutton(general_frame, text="Negative", value="negative",
                         variable=self.sign_var).grid(row=4, column=1, sticky="w")
        ttk.Radiobutton(general_frame, text="Both", value="both",
                         variable=self.sign_var).grid(row=5, column=0, sticky="w")

        self._build_field_group(general_frame, self.GENERAL_FIELDS, start_row=6)

        button_row = ttk.Frame(self)
        button_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Button(button_row, text="Apply Settings", command=self.on_apply).pack(side=tk.LEFT, padx=4)

        self.engage_var = tk.StringVar(value="Engage Autolock")
        self.engage_button = ttk.Button(button_row, textvariable=self.engage_var, command=self.on_toggle_engage)
        self.engage_button.pack(side=tk.LEFT, padx=4)

        ttk.Button(button_row, text="Run Autolock Now", command=self.on_run_now).pack(side=tk.LEFT, padx=4)

        self.status_label = ttk.Label(self, text="Autolock: disengaged")
        self.status_label.pack(side=tk.TOP, anchor="w", padx=10)

    def _build_field_group(self, frame, field_dict, start_row=0):
        for i, (field_name, label) in enumerate(field_dict.items()):
            row = start_row + i
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=4)
            value = getattr(settings, field_name)
            var = tk.StringVar(value=str(value))
            ttk.Entry(frame, textvariable=var, width=18).grid(row=row, column=1, sticky="w", pady=2, padx=4)
            self.vars[field_name] = var

    def on_apply(self):
        try:
            for field_name, var in self.vars.items():
                field_type = self.FIELD_TYPES[field_name]
                raw = var.get()
                if field_type is bool:
                    value = raw.strip().lower() in ("1", "true", "yes", "on")
                elif field_type is int:
                    value = int(raw)
                elif field_type is float:
                    value = float(raw)
                else:
                    value = raw
                setattr(settings, field_name, value)
            settings.AUTOLOCK_MODE = self.mode_var.get()
            settings.CROSSING_SIGN = self.sign_var.get()
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Could not parse a field: {exc}")
            return
        self.app.monitor_panel.refresh_safe_bands()
        self.app.monitor_panel.log(
            f"Settings applied. Autolock mode = {settings.AUTOLOCK_MODE}, crossing sign = {settings.CROSSING_SIGN}")

    def on_toggle_engage(self):
        if manual_scan_active.is_set():
            messagebox.showwarning("Busy", "Wait for the manual scan to finish first.")
            return
        if autolock_engaged.is_set():
            autolock_engaged.clear()
            self.engage_var.set("Engage Autolock")
            self.status_label.configure(text="Autolock: disengaged")
            self.app.monitor_panel.log("Autolock disengaged.")
        else:
            self.on_apply()
            autolock_engaged.set()
            self.engage_var.set("Disengage Autolock")
            self.status_label.configure(text=f"Autolock: engaged ({settings.AUTOLOCK_MODE})")
            self.app.monitor_panel.log(f"Autolock engaged (mode={settings.AUTOLOCK_MODE}).")

    def on_run_now(self):
        if manual_scan_active.is_set() or autolock_engaged.is_set():
            messagebox.showwarning("Busy", "Disengage autolock / wait for the manual scan to finish first.")
            return
        self.on_apply()
        threading.Thread(
            target=lambda: autolock_once(log_fn=lambda m: gui_queue.put(("log", m))),
            daemon=True,
        ).start()


class MainApp(tk.Tk):
    def __init__(self):
        # Plain Tk, no ttk theming at all - temporary diagnostic revert to
        # isolate whether a theme (Clearlooks, or ttk theming in general)
        # was ever actually the cause of the tab-switch/entry-focus lag.
        super().__init__()
        self.title("Autolock Control GUI")
        self.geometry("1150x850")

        self.app_icon = tk.PhotoImage(file="icon.png")
        self.iconphoto(True, self.app_icon)

        notebook = ttk.Notebook(self)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.scan_tab = ScanTab(notebook, self)
        self.settings_tab = SettingsTab(notebook, self)
        self.monitor_panel = LiveMonitorPanel(notebook)
        notebook.add(self.scan_tab, text="Scan & Lock")
        notebook.add(self.settings_tab, text="Autolock Settings")
        notebook.add(self.monitor_panel, text="Live Monitor")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_queue)

        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()

    def poll_queue(self):
        """
        Drain everything currently queued, but only touch the expensive
        stuff (matplotlib redraws, progress bar, log widget) ONCE per
        call - regardless of how many readings arrived this tick. During a
        fast scan, dozens of "reading" messages can pile up between ticks;
        updating the plot/log for each one individually is what made the
        GUI feel slow, especially on Windows where Tk's canvas blitting is
        already the slower path. Batching keeps the redraw rate capped at
        roughly 1 / (after-delay) regardless of data rate.
        """
        had_reading = False
        log_lines = []
        latest_progress = None

        try:
            while True:
                item = gui_queue.get_nowait()
                kind = item[0]
                if kind == "reading":
                    _, timestamp, values = item
                    self.monitor_panel.record_reading(timestamp, values, log_lines)
                    had_reading = True
                elif kind == "log":
                    _, message = item
                    log_lines.append(message)
                elif kind == "scan_progress":
                    _, i, total = item
                    latest_progress = (i, total)
                elif kind == "scan_done":
                    _, data = item
                    self.scan_tab.on_scan_done(data)
        except queue.Empty:
            pass

        if had_reading:
            self.monitor_panel.redraw_plots()
        if log_lines:
            self.monitor_panel.log_many(log_lines)
        if latest_progress is not None:
            self.scan_tab.on_scan_progress(*latest_progress)

        self.after(100, self.poll_queue)

    def on_close(self):
        stop_event.set()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = MainApp()
    app.mainloop()
