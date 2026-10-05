"""
Autolock control GUI.

Run this directly on the Raspberry Pi over an X11-forwarded SSH session
(`ssh -X pi@<address>`, then `python3 autolock_gui.py`) so the Tkinter
window appears on your local machine while the DAC/ADC hardware access
happens on the Pi.

Two main tabs:
    - "Scan & Lock": sweep DAC channel A ("control out") across a
      configurable range, plot the error signals (channel 2 "error" and
      channel 3 "dc_err") and the outputs (channel 0 "slow_output" and
      channel 1 "fast_output") against it, list every zero crossing
      found in whichever error signal you choose to search, and lock to
      any crossing you select.
    - "Autolock Settings": edit safe voltage ranges, scan parameters,
      and lock behavior, then engage/disengage the automatic autolock
      (same trigger-on-out-of-range, steepest-zero-crossing logic as the
      headless pdh_autolock.py script).

A persistent panel at the bottom continuously shows a live strip-chart
and scrolling text log of every reading, whether or not autolock is
engaged - this runs from the moment the GUI starts.

Install dependencies:
    pip install adafruit-blinka adafruit-circuitpython-mcp4728 \
                adafruit-circuitpython-ads1x15 paho-mqtt python-dotenv \
                numpy matplotlib

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
VDD_VOLTAGE = 5.0  # Supply voltage feeding the MCP4728
ADS_GAIN = 1        # 1 -> +/-4.096V input range; use 2/3 for +/-6.144V

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
ERROR_SIGNAL_NAMES = ["error", "dc_err"]      # selectable for zero-crossing search
OUTPUT_SIGNAL_NAMES = ["slow_output", "fast_output"]


# --------------------------------------------------------------------------
# Settings (editable live from the GUI; plain attributes, no extra lock -
# the GIL makes simple read/write of these safe enough between threads)
# --------------------------------------------------------------------------
@dataclass
class Settings:
    SLOW_OUTPUT_SAFE_MIN: float = 1.0
    SLOW_OUTPUT_SAFE_MAX: float = 3.0
    FAST_OUTPUT_SAFE_MIN: float = 1.0
    FAST_OUTPUT_SAFE_MAX: float = 3.0
    # dc_err has no safe range used by the autolock trigger today (that's
    # still just slow_output/fast_output) - this one is purely for the
    # visual band on the plots. Adjust it, or tell me if you want dc_err
    # folded into the actual trigger condition too.
    DC_ERR_SAFE_MIN: float = 1.0
    DC_ERR_SAFE_MAX: float = 3.0

    SCAN_MIN_VOLTAGE: float = 0.0
    SCAN_MAX_VOLTAGE: float = 4.0
    NUM_COARSE_POINTS: int = 51
    NUM_FINE_POINTS: int = 41
    MANUAL_SCAN_POINTS: int = 101
    SCAN_SETTLE_TIME: float = 0.01
    NUM_SAMPLES_PER_POINT: int = 5
    SMOOTHING_WINDOW: int = 3

    RESET_VOLTAGE_ON_FAILURE: float = 2.0
    DAC_HIGH_VOLTAGE: float = 4.0
    DAC_LOW_VOLTAGE: float = 0.0
    PRIME_SETTLE_TIME: float = 0.1

    MONITOR_INTERVAL: float = 1.0
    LOCK_RETRY_DELAY: float = 1.0

    SAVE_SCAN_PLOTS: bool = True
    SCAN_PLOT_DIR: str = "scan_plots"


settings = Settings()

# --------------------------------------------------------------------------
# Shared threading state
# --------------------------------------------------------------------------
stop_event = threading.Event()          # set on GUI close, stops the monitor thread
autolock_engaged = threading.Event()    # set while automatic autolock is active
manual_scan_active = threading.Event()  # set while a manual scan is running
gui_queue = queue.Queue()               # background threads -> GUI thread messages


# --------------------------------------------------------------------------
# Low-level hardware helpers
# --------------------------------------------------------------------------
def voltage_to_dac_value(voltage, vdd=VDD_VOLTAGE):
    value = int(round((voltage / vdd) * 65535))
    return max(0, min(65535, value))


def set_dac_voltage(channel, voltage):
    with i2c_lock:
        channel.value = voltage_to_dac_value(voltage)


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
    set_dac_voltage(dac.channel_b, settings.DAC_HIGH_VOLTAGE)
    set_dac_voltage(dac.channel_c, settings.DAC_HIGH_VOLTAGE)
    set_dac_voltage(dac.channel_d, settings.DAC_HIGH_VOLTAGE)
    time.sleep(settings.PRIME_SETTLE_TIME)


def restart_pid():
    set_dac_voltage(dac.channel_b, settings.DAC_LOW_VOLTAGE)
    set_dac_voltage(dac.channel_c, settings.DAC_LOW_VOLTAGE)
    set_dac_voltage(dac.channel_d, settings.DAC_LOW_VOLTAGE)


# --------------------------------------------------------------------------
# Signal processing helpers
# --------------------------------------------------------------------------
def smooth_signal(signal, window):
    if window <= 1:
        return np.asarray(signal, dtype=float)
    kernel = np.ones(window) / window
    return np.convolve(signal, kernel, mode="same")


def find_zero_crossings(voltages, signal):
    """Returns a list of (crossing_voltage, slope) tuples."""
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


# --------------------------------------------------------------------------
# Scan-plot saving (used by the automatic autolock, same format as the
# headless pdh_autolock.py script)
# --------------------------------------------------------------------------
def _plot_pass_on_axis(ax, data, title):
    ax.plot(data["voltages"], data["raw"], ".", alpha=0.4, label="raw error")
    ax.plot(data["voltages"], data["smoothed"], "-", label="smoothed error")
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
    for v, _ in data["crossings"]:
        ax.axvline(v, color="gray", linestyle=":", alpha=0.6)
    if data["chosen"] is not None:
        chosen_v, chosen_slope = data["chosen"]
        ax.plot(chosen_v, 0, "r*", markersize=16,
                 label=f"chosen ({chosen_v:.4f} V, slope {chosen_slope:.3f})")
    ax.set_xlabel("control out (V)")
    ax.set_ylabel("error signal (V)")
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)


def save_autolock_scan_plot(session_id, coarse_data, fine_data):
    if not settings.SAVE_SCAN_PLOTS:
        return
    os.makedirs(settings.SCAN_PLOT_DIR, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    _plot_pass_on_axis(axes[0], coarse_data, "Coarse scan")
    if fine_data is not None:
        _plot_pass_on_axis(axes[1], fine_data, "Fine scan")
    else:
        axes[1].text(0.5, 0.5, "No zero crossing found -\nfine pass skipped",
                      ha="center", va="center", transform=axes[1].transAxes)
        axes[1].set_title("Fine scan")
    fig.suptitle(f"Autolock scan - {session_id}")
    fig.tight_layout()
    filename = os.path.join(settings.SCAN_PLOT_DIR, f"{session_id}_scan.png")
    fig.savefig(filename, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------
# Automatic autolock (triggered by monitor loop, or run on-demand)
# --------------------------------------------------------------------------
def autolock_scan_pass(v_min, v_max, num_points, pass_tag, log_fn):
    voltages = np.linspace(v_min, v_max, num_points)
    error_signal = np.empty(num_points)
    for i, v in enumerate(voltages):
        set_dac_voltage(dac.channel_a, v)
        time.sleep(settings.SCAN_SETTLE_TIME)
        values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
        publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": pass_tag})
        gui_queue.put(("reading", time.time(), values))
        error_signal[i] = values["error"]
    return voltages, error_signal


def autolock_once(log_fn=print):
    """Coarse+fine scan on the 'error' channel, lock to steepest crossing."""
    log_fn("Starting PDH-style autolock scan on the error signal...")
    short_caps_and_block_pid()
    session_id = time.strftime("%Y%m%d_%H%M%S")

    coarse_step = (settings.SCAN_MAX_VOLTAGE - settings.SCAN_MIN_VOLTAGE) / (settings.NUM_COARSE_POINTS - 1)
    coarse_voltages, coarse_signal = autolock_scan_pass(
        settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE, settings.NUM_COARSE_POINTS, "coarse", log_fn
    )
    coarse_smoothed = smooth_signal(coarse_signal, settings.SMOOTHING_WINDOW)
    coarse_crossings = find_zero_crossings(coarse_voltages, coarse_smoothed)
    coarse_data = {"voltages": coarse_voltages, "raw": coarse_signal,
                   "smoothed": coarse_smoothed, "crossings": coarse_crossings, "chosen": None}

    if not coarse_crossings:
        log_fn("No zero crossings found - autolock failed.")
        save_autolock_scan_plot(session_id, coarse_data, None)
        set_dac_voltage(dac.channel_a, settings.RESET_VOLTAGE_ON_FAILURE)
        return False

    coarse_best_v, coarse_best_slope = max(coarse_crossings, key=lambda c: abs(c[1]))
    coarse_data["chosen"] = (coarse_best_v, coarse_best_slope)
    log_fn(f"Coarse pass found {len(coarse_crossings)} crossing(s); "
           f"largest near {coarse_best_v:.4f} V. Refining...")

    fine_min = max(settings.SCAN_MIN_VOLTAGE, coarse_best_v - coarse_step)
    fine_max = min(settings.SCAN_MAX_VOLTAGE, coarse_best_v + coarse_step)
    fine_voltages, fine_signal = autolock_scan_pass(fine_min, fine_max, settings.NUM_FINE_POINTS, "fine", log_fn)
    fine_smoothed = smooth_signal(fine_signal, settings.SMOOTHING_WINDOW)
    fine_crossings = find_zero_crossings(fine_voltages, fine_smoothed)

    if fine_crossings:
        lock_v, lock_slope = max(fine_crossings, key=lambda c: abs(c[1]))
    else:
        lock_v, lock_slope = coarse_best_v, coarse_best_slope

    fine_data = {"voltages": fine_voltages, "raw": fine_signal,
                 "smoothed": fine_smoothed, "crossings": fine_crossings,
                 "chosen": (lock_v, lock_slope)}
    save_autolock_scan_plot(session_id, coarse_data, fine_data)

    set_dac_voltage(dac.channel_a, lock_v)
    log_fn(f"Locked: control out = {lock_v:.4f} V (slope {lock_slope:.4f})")
    restart_pid()
    return True


# --------------------------------------------------------------------------
# Background threads
# --------------------------------------------------------------------------
def monitor_loop():
    while not stop_event.is_set():
        if manual_scan_active.is_set():
            time.sleep(0.2)
            continue

        values = read_physical(num_samples=1)
        publish_values(values, "monitoring")
        gui_queue.put(("reading", time.time(), values))

        if autolock_engaged.is_set():
            slow_ok = is_within(values["slow_output"], settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX)
            fast_ok = is_within(values["fast_output"], settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX)
            if not (slow_ok and fast_ok):
                gui_queue.put(("log", f"Trigger: slow_ok={slow_ok} fast_ok={fast_ok}"))
                locked = autolock_once(log_fn=lambda m: gui_queue.put(("log", m)))
                if not locked:
                    time.sleep(settings.LOCK_RETRY_DELAY)
                continue

        time.sleep(settings.MONITOR_INTERVAL)


def run_manual_scan(error_channel_name):
    manual_scan_active.set()
    try:
        gui_queue.put(("log", f"Starting manual scan (searching '{error_channel_name}' for crossings)..."))
        short_caps_and_block_pid()

        voltages = np.linspace(settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE, settings.MANUAL_SCAN_POINTS)
        trace = {name: np.empty(settings.MANUAL_SCAN_POINTS) for name in CHANNEL_CONVERTERS}

        for i, v in enumerate(voltages):
            set_dac_voltage(dac.channel_a, v)
            time.sleep(settings.SCAN_SETTLE_TIME)
            values = read_physical(num_samples=settings.NUM_SAMPLES_PER_POINT)
            publish_values(values, "scanning", {"scan_voltage": float(v), "scan_pass": "manual"})
            for name in CHANNEL_CONVERTERS:
                trace[name][i] = values[name]
            gui_queue.put(("scan_progress", i + 1, settings.MANUAL_SCAN_POINTS))

        signal_smoothed = smooth_signal(trace[error_channel_name], settings.SMOOTHING_WINDOW)
        crossings = find_zero_crossings(voltages, signal_smoothed)

        gui_queue.put(("scan_done", {
            "voltages": voltages,
            "trace": trace,
            "error_channel_name": error_channel_name,
            "signal_smoothed": signal_smoothed,
            "crossings": crossings,
        }))
        gui_queue.put(("log", f"Manual scan complete - {len(crossings)} crossing(s) found "
                               f"in '{error_channel_name}'."))
    except Exception as exc:  # surface any hardware error to the GUI instead of dying silently
        gui_queue.put(("log", f"Manual scan failed: {exc}"))
    finally:
        manual_scan_active.clear()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class LiveMonitorPanel(ttk.Frame):
    """Persistent strip-chart + scrolling log, always running."""

    MAX_POINTS = 300
    MAX_LOG_LINES = 500

    def __init__(self, parent):
        super().__init__(parent)

        self.times = deque(maxlen=self.MAX_POINTS)
        self.series = {name: deque(maxlen=self.MAX_POINTS) for name in CHANNEL_CONVERTERS}
        self.t0 = time.time()

        fig = plt.Figure(figsize=(9, 2.6))
        self.ax = fig.add_subplot(111)
        self.lines = {}
        for name in CHANNEL_CONVERTERS:
            (line,) = self.ax.plot([], [], label=name)
            self.lines[name] = line
        self.ax.set_xlabel("time (s)")
        self.ax.set_ylabel("volts")
        self.ax.grid(True, alpha=0.3)

        self.band_patches = []
        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.refresh_safe_bands()  # draws bands + builds the initial legend
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        log_frame = ttk.Frame(self)
        log_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="none")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh_safe_bands(self):
        """(Re)draw the safe-range bands from current settings. Call this
        once at startup and again any time settings are applied, since the
        bands are static per-draw rather than per-reading."""
        for patch in self.band_patches:
            patch.remove()
        self.band_patches = []

        bands = [
            ("dc_err", settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX, "tab:green"),
            ("slow_output", settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX, "tab:blue"),
            ("fast_output", settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX, "tab:orange"),
        ]
        for name, lo, hi, color in bands:
            patch = self.ax.axhspan(lo, hi, color=color, alpha=0.12, label=f"{name} safe range")
            self.band_patches.append(patch)

        self.ax.legend(fontsize=8, loc="upper right")
        self.canvas.draw_idle()

    def add_reading(self, timestamp, values):
        t = timestamp - self.t0
        self.times.append(t)
        for name, val in values.items():
            self.series[name].append(val)

        for name, line in self.lines.items():
            line.set_data(self.times, self.series[name])
        if self.times:
            self.ax.set_xlim(max(0, self.times[0]), self.times[-1] + 1)
            all_vals = [v for series in self.series.values() for v in series]
            if all_vals:
                margin = 0.5
                self.ax.set_ylim(min(all_vals) - margin, max(all_vals) + margin)
        self.canvas.draw_idle()

        line_str = " ".join(f"{k}={v:.4f}" for k, v in values.items())
        self.log(f"[{time.strftime('%H:%M:%S')}] {line_str}")

    def log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        num_lines = int(self.log_text.index("end-1c").split(".")[0])
        if num_lines > self.MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{num_lines - self.MAX_LOG_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")


class ScanTab(ttk.Frame):
    """Manual scan, crossing selection, and lock controls."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.current_voltages = None
        self.current_trace = None
        self.current_crossings = []

        controls = ttk.Frame(self)
        controls.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        ttk.Label(controls, text="Scan min (V)").grid(row=0, column=0, sticky="w")
        self.min_var = tk.StringVar(value=str(settings.SCAN_MIN_VOLTAGE))
        ttk.Entry(controls, textvariable=self.min_var, width=8).grid(row=0, column=1)

        ttk.Label(controls, text="Scan max (V)").grid(row=0, column=2, sticky="w")
        self.max_var = tk.StringVar(value=str(settings.SCAN_MAX_VOLTAGE))
        ttk.Entry(controls, textvariable=self.max_var, width=8).grid(row=0, column=3)

        ttk.Label(controls, text="Points").grid(row=0, column=4, sticky="w")
        self.points_var = tk.StringVar(value=str(settings.MANUAL_SCAN_POINTS))
        ttk.Entry(controls, textvariable=self.points_var, width=8).grid(row=0, column=5)

        ttk.Label(controls, text="Search crossings in:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.error_channel_var = tk.StringVar(value="error")
        for i, name in enumerate(ERROR_SIGNAL_NAMES):
            ttk.Radiobutton(controls, text=name, value=name,
                             variable=self.error_channel_var).grid(row=1, column=1 + i, sticky="w", pady=(6, 0))

        self.scan_button = ttk.Button(controls, text="Run Scan", command=self.on_run_scan)
        self.scan_button.grid(row=1, column=4, padx=(12, 0), pady=(6, 0))

        self.progress = ttk.Progressbar(controls, orient="horizontal", length=160, mode="determinate")
        self.progress.grid(row=1, column=5, padx=(12, 0), pady=(6, 0))

        # Plot area
        fig = plt.Figure(figsize=(9, 5.5))
        self.ax_err = fig.add_subplot(211)
        self.ax_out = fig.add_subplot(212)
        self.fig = fig

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = FigureCanvasTkAgg(fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, canvas_frame)
        toolbar.update()

        # Crossing list + lock controls
        bottom = ttk.Frame(self)
        bottom.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        ttk.Label(bottom, text="Zero crossings found:").pack(side=tk.LEFT)
        self.crossing_list = tk.Listbox(bottom, height=4, width=60)
        self.crossing_list.pack(side=tk.LEFT, padx=(6, 6))

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

        self.crossing_list.delete(0, tk.END)
        self.progress.configure(maximum=settings.MANUAL_SCAN_POINTS, value=0)
        self.scan_button.configure(state="disabled")

        threading.Thread(
            target=run_manual_scan, args=(self.error_channel_var.get(),), daemon=True
        ).start()

    def on_scan_progress(self, i, total):
        self.progress.configure(value=i)

    def on_scan_done(self, data):
        self.scan_button.configure(state="normal")
        self.current_voltages = data["voltages"]
        self.current_trace = data["trace"]
        self.current_crossings = data["crossings"]

        self.ax_err.clear()
        self.ax_out.clear()

        self.ax_err.axhspan(settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX,
                             color="tab:green", alpha=0.15, label="dc_err safe range")
        for name in ERROR_SIGNAL_NAMES:
            self.ax_err.plot(data["voltages"], data["trace"][name], label=name, alpha=0.6)
        self.ax_err.plot(data["voltages"], data["signal_smoothed"], "k-",
                          label=f"{data['error_channel_name']} (smoothed)")
        self.ax_err.axhline(0, color="black", linewidth=0.8, linestyle="--")
        for v, _ in self.current_crossings:
            self.ax_err.axvline(v, color="gray", linestyle=":", alpha=0.6)
        self.ax_err.set_xlabel("control out (V)")
        self.ax_err.set_ylabel("error signals (V)")
        self.ax_err.legend(fontsize=8)
        self.ax_err.grid(True, alpha=0.3)

        self.ax_out.axhspan(settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX,
                             color="tab:blue", alpha=0.12, label="slow_output safe range")
        self.ax_out.axhspan(settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX,
                             color="tab:orange", alpha=0.12, label="fast_output safe range")
        for name in OUTPUT_SIGNAL_NAMES:
            self.ax_out.plot(data["voltages"], data["trace"][name], label=name)
        self.ax_out.set_xlabel("control out (V)")
        self.ax_out.set_ylabel("outputs (V)")
        self.ax_out.legend(fontsize=8)
        self.ax_out.grid(True, alpha=0.3)

        self.fig.tight_layout()
        self.canvas.draw_idle()

        for v, slope in self.current_crossings:
            self.crossing_list.insert(tk.END, f"V = {v:.4f} V   slope = {slope:.4f}")

    def on_lock_selected(self):
        sel = self.crossing_list.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Select a crossing from the list first.")
            return
        v, slope = self.current_crossings[sel[0]]
        set_dac_voltage(dac.channel_a, v)
        restart_pid()
        self.app.monitor_panel.log(f"[manual] Locked to control out = {v:.4f} V (slope {slope:.4f})")

    def on_release(self):
        restart_pid()
        self.app.monitor_panel.log("[manual] PID restarted (B, C, D set LOW) without changing control out.")


class SettingsTab(ttk.Frame):
    """Autolock settings form + engage/disengage control."""

    FIELD_LABELS = {
        "SLOW_OUTPUT_SAFE_MIN": "slow_output safe min (V)",
        "SLOW_OUTPUT_SAFE_MAX": "slow_output safe max (V)",
        "FAST_OUTPUT_SAFE_MIN": "fast_output safe min (V)",
        "FAST_OUTPUT_SAFE_MAX": "fast_output safe max (V)",
        "DC_ERR_SAFE_MIN": "dc_err safe min (V) [display only]",
        "DC_ERR_SAFE_MAX": "dc_err safe max (V) [display only]",
        "SCAN_MIN_VOLTAGE": "Autolock scan min (V)",
        "SCAN_MAX_VOLTAGE": "Autolock scan max (V)",
        "NUM_COARSE_POINTS": "Coarse pass points",
        "NUM_FINE_POINTS": "Fine pass points",
        "SCAN_SETTLE_TIME": "Settle time (s)",
        "NUM_SAMPLES_PER_POINT": "Samples averaged/point",
        "SMOOTHING_WINDOW": "Smoothing window (1 = off)",
        "RESET_VOLTAGE_ON_FAILURE": "Reset voltage on failure (V)",
        "DAC_HIGH_VOLTAGE": "B/C/D HIGH voltage (V)",
        "DAC_LOW_VOLTAGE": "B/C/D LOW voltage (V)",
        "MONITOR_INTERVAL": "Monitor interval (s)",
        "LOCK_RETRY_DELAY": "Retry delay on failure (s)",
        "SAVE_SCAN_PLOTS": "Save autolock scan plots",
        "SCAN_PLOT_DIR": "Scan plot directory",
    }
    FIELD_TYPES = {f.name: f.type for f in fields(Settings)}

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.vars = {}

        form = ttk.Frame(self)
        form.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        for i, (field_name, label) in enumerate(self.FIELD_LABELS.items()):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=2)
            value = getattr(settings, field_name)
            var = tk.StringVar(value=str(value))
            ttk.Entry(form, textvariable=var, width=20).grid(row=i, column=1, sticky="w", pady=2)
            self.vars[field_name] = var

        button_row = ttk.Frame(self)
        button_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Button(button_row, text="Apply Settings", command=self.on_apply).pack(side=tk.LEFT, padx=4)

        self.engage_var = tk.StringVar(value="Engage Autolock")
        self.engage_button = ttk.Button(button_row, textvariable=self.engage_var, command=self.on_toggle_engage)
        self.engage_button.pack(side=tk.LEFT, padx=4)

        ttk.Button(button_row, text="Run Autolock Now", command=self.on_run_now).pack(side=tk.LEFT, padx=4)

        self.status_label = ttk.Label(self, text="Autolock: disengaged")
        self.status_label.pack(side=tk.TOP, anchor="w", padx=10)

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
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Could not parse a field: {exc}")
            return
        self.app.monitor_panel.refresh_safe_bands()
        self.app.monitor_panel.log("Settings applied.")

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
            self.status_label.configure(text="Autolock: engaged")
            self.app.monitor_panel.log("Autolock engaged.")

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
        super().__init__()
        self.title("Autolock Control GUI")
        self.geometry("1000x900")

        notebook = ttk.Notebook(self)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.scan_tab = ScanTab(notebook, self)
        self.settings_tab = SettingsTab(notebook, self)
        notebook.add(self.scan_tab, text="Scan & Lock")
        notebook.add(self.settings_tab, text="Autolock Settings")

        ttk.Separator(self, orient="horizontal").pack(side=tk.TOP, fill=tk.X)
        ttk.Label(self, text="Live Monitor", font=("TkDefaultFont", 10, "bold")).pack(side=tk.TOP, anchor="w", padx=6)
        self.monitor_panel = LiveMonitorPanel(self)
        self.monitor_panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_queue)

        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()

    def poll_queue(self):
        try:
            while True:
                item = gui_queue.get_nowait()
                kind = item[0]
                if kind == "reading":
                    _, timestamp, values = item
                    self.monitor_panel.add_reading(timestamp, values)
                elif kind == "log":
                    _, message = item
                    self.monitor_panel.log(message)
                elif kind == "scan_progress":
                    _, i, total = item
                    self.scan_tab.on_scan_progress(i, total)
                elif kind == "scan_done":
                    _, data = item
                    self.scan_tab.on_scan_done(data)
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    def on_close(self):
        stop_event.set()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = MainApp()
    app.mainloop()
