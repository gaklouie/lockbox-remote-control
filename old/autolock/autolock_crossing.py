"""
Error-signal (PDH-style) autolock, triggered by the slow/fast output
channels leaving a safe voltage range.

Physical channel conversions:
    The four ADC channels feed through a resistor network before
    reaching the ADS1115, so raw ADC readings are converted into
    real-world ("true") voltages before being logged or compared against
    any safe range:

        Channel 0 ("slow_output"): 56 * (-0.227273 + 0.118867 * adc_reading)
        Channel 1 ("fast_output"): 56 * (-0.227273 + 0.118867 * adc_reading)
        Channel 2 ("error"):       18 * (-0.185185 + 0.0925927 * adc_reading)
        Channel 3 ("dc_err"):      adc_reading (no scaling)

Monitoring/trigger:
    - slow_output and fast_output are continuously read, converted, and
      logged.
    - If either leaves its configured safe range (SLOW_OUTPUT_SAFE_MIN/MAX,
      FAST_OUTPUT_SAFE_MIN/MAX below), autolock() is triggered.

Autolock:
    1. short_caps_and_block_pid(): set DAC channels B, C, D HIGH.
    2. Coarse pass: sweep DAC channel A across its full range, recording
       the "error" signal (channel 2, converted) as a bipolar error
       signal (e.g. Pound-Drever-Hall).
    3. Find every zero crossing in the error signal, pick the one with
       the steepest slope ("the largest zero crossing").
    4. Fine pass: zoom into a window around that crossing and re-scan at
       fine resolution to refine its exact location.
    5. Set channel A to the refined crossing voltage, call restart_pid()
       (B, C, D LOW).
    6. If no zero crossing is found at all, channel A resets to
       RESET_VOLTAGE_ON_FAILURE, B/C/D stay HIGH, and the autolock is
       retried after a short delay.
    7. Resume monitoring slow_output/fast_output for the next trigger.

Noise / jitter handling during the scan:
    The error signal can be jittery, which risks spurious zero crossings
    (or missed real ones). Two independent mitigations are applied:
        - Oversampling: each scan point averages NUM_SAMPLES_PER_POINT
          raw ADC readings before converting to a physical voltage,
          reducing sample-to-sample measurement noise.
        - Smoothing: the full scanned error trace is then passed through
          a moving-average filter (SMOOTHING_WINDOW samples wide) before
          zero crossings are searched for, reducing the chance that a
          single noisy sample creates a false crossing. Set
          SMOOTHING_WINDOW = 1 to disable this step.
    Both are tunable below; increase them if crossings still look noisy,
    decrease them (or disable smoothing) if real closely-spaced crossings
    seem to be getting blurred together.

Scan plots:
    Set SAVE_SCAN_PLOTS = True to save one PNG per autolock attempt to
    SCAN_PLOT_DIR, with the coarse scan and fine scan side by side as
    two subplots (raw + smoothed error signal, all detected zero
    crossings, and the one chosen), so you can see exactly what each
    autolock attempt saw and decided. Uses matplotlib's non-interactive
    "Agg" backend, since the Pi is assumed to be running headless; if
    you want a live window instead, change the matplotlib.use() call
    near the top of the file.

Install dependencies:
    pip install adafruit-blinka adafruit-circuitpython-mcp4728 \
                adafruit-circuitpython-ads1x15 paho-mqtt python-dotenv \
                numpy matplotlib
"""

import os
import json
import time

import numpy as np
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

import matplotlib
matplotlib.use("Agg")  # non-interactive backend for headless operation
import matplotlib.pyplot as plt

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

# --------------------------------------------------------------------------
# System configuration - adjust as needed
# --------------------------------------------------------------------------
VDD_VOLTAGE = 5.0            # Supply voltage feeding the MCP4728

# Safe ranges for slow_output / fast_output (true, post-conversion volts)
# - the trigger condition for re-running autolock.
SLOW_OUTPUT_SAFE_MIN, SLOW_OUTPUT_SAFE_MAX = -5.0, 5.0
FAST_OUTPUT_SAFE_MIN, FAST_OUTPUT_SAFE_MAX = -5.0, 5.0

SCAN_MIN_VOLTAGE = 0.0       # Start of DAC channel A scan
SCAN_MAX_VOLTAGE = 4.0       # End of DAC channel A scan
NUM_COARSE_POINTS = 51       # Coarse pass resolution (~80 mV/step over 4V)
NUM_FINE_POINTS = 41         # Fine pass resolution, within one coarse step
SCAN_SETTLE_TIME = 0.01      # Seconds to wait after each DAC step before reading

NUM_SAMPLES_PER_POINT = 5    # Raw ADC samples averaged together at each scan point
SMOOTHING_WINDOW = 3         # Moving-average window (in scan points) applied to the
                             # error trace before zero-crossing search; 1 = disabled

SAVE_SCAN_PLOTS = True       # Toggle saving a PNG of each coarse/fine scan
SCAN_PLOT_DIR = "scan_plots" # Directory scan plots are saved into

RESET_VOLTAGE_ON_FAILURE = 2.0  # Channel A voltage if no zero crossing is found

DAC_HIGH_VOLTAGE = 4.0          # "high" output level for channels B, C, D
DAC_LOW_VOLTAGE = 0.0           # "low" output level for channels B, C, D
PRIME_SETTLE_TIME = 0.1         # Seconds to wait after setting B, C, D high

MONITOR_INTERVAL = 1.0       # Seconds between normal monitoring samples
LOCK_RETRY_DELAY = 1.0       # Seconds to wait before retrying a failed autolock

ADS_GAIN = 1                 # 1 -> +/-4.096V input range; use 2/3 for +/-6.144V

# --------------------------------------------------------------------------
# Hardware setup
# --------------------------------------------------------------------------
i2c = busio.I2C(board.SCL, board.SDA)

dac = adafruit_mcp4728.MCP4728(i2c)
for ch in (dac.channel_a, dac.channel_b, dac.channel_c, dac.channel_d):
    ch.vref = adafruit_mcp4728.Vref.VDD
    ch.gain = 1

ads = ADS.ADS1115(i2c)
ads.gain = ADS_GAIN
adc_channels = [AnalogIn(ads, i) for i in range(4)]  # channels 0-3

# --------------------------------------------------------------------------
# MQTT setup
# --------------------------------------------------------------------------
client = mqtt.Client()
client.username_pw_set(**credentials)
client.connect(mqttBrokerAddress, mqttPort)
client.loop_start()


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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def voltage_to_dac_value(voltage, vdd=VDD_VOLTAGE):
    """Convert a target voltage into a 16-bit scaled MCP4728 channel value."""
    value = int(round((voltage / vdd) * 65535))
    return max(0, min(65535, value))


def set_dac_voltage(channel, voltage):
    channel.value = voltage_to_dac_value(voltage)


def is_within(value, lo, hi):
    return lo <= value <= hi


def read_raw_averaged(num_samples=1):
    """Read all 4 ADC channels, averaging num_samples raw readings each."""
    sums = [0.0, 0.0, 0.0, 0.0]
    for _ in range(num_samples):
        for i, chan in enumerate(adc_channels):
            sums[i] += chan.voltage
    return [s / num_samples for s in sums]


def read_physical(num_samples=1):
    """Read all 4 channels and convert to true/physical volts."""
    raw = read_raw_averaged(num_samples)
    return {
        "slow_output": to_slow_output(raw[0]),
        "fast_output": to_fast_output(raw[1]),
        "error": to_error(raw[2]),
        "dc_err": to_dc_err(raw[3]),
    }


def publish_reading(state, extra_fields=None, num_samples=1):
    """Read, convert, and publish all 4 physical channel values over MQTT."""
    values = read_physical(num_samples=num_samples)
    lock_status = (
        is_within(values["slow_output"], SLOW_OUTPUT_SAFE_MIN, SLOW_OUTPUT_SAFE_MAX)
        and is_within(values["fast_output"], FAST_OUTPUT_SAFE_MIN, FAST_OUTPUT_SAFE_MAX)
    )

    payload = {
        "timestamp": time.time(),
        "slow_output": values["slow_output"],
        "fast_output": values["fast_output"],
        "error": values["error"],
        "dc_err": values["dc_err"],
        "lock_status": lock_status,  # False if slow/fast_output is outside its safe range
        "state": state,              # "monitoring" or "scanning"
    }
    if extra_fields:
        payload.update(extra_fields)

    client.publish(mqttTopic, json.dumps(payload))
    print(f"[{state}] slow_output={values['slow_output']:.4f}V "
          f"fast_output={values['fast_output']:.4f}V error={values['error']:.4f}V "
          f"dc_err={values['dc_err']:.4f}V lock_status={lock_status}")
    return values


def short_caps_and_block_pid():
    """Set DAC channels B, C, D HIGH."""
    set_dac_voltage(dac.channel_b, DAC_HIGH_VOLTAGE)
    set_dac_voltage(dac.channel_c, DAC_HIGH_VOLTAGE)
    set_dac_voltage(dac.channel_d, DAC_HIGH_VOLTAGE)
    time.sleep(PRIME_SETTLE_TIME)


def restart_pid():
    """Set DAC channels B, C, D LOW."""
    set_dac_voltage(dac.channel_b, DAC_LOW_VOLTAGE)
    set_dac_voltage(dac.channel_c, DAC_LOW_VOLTAGE)
    set_dac_voltage(dac.channel_d, DAC_LOW_VOLTAGE)


def smooth_signal(signal, window):
    """Moving-average smoothing; window <= 1 returns the signal unchanged."""
    if window <= 1:
        return signal
    kernel = np.ones(window) / window
    return np.convolve(signal, kernel, mode="same")


def _plot_pass_on_axis(ax, data, title):
    """Draw one scan pass's raw/smoothed error signal, crossings, and
    chosen lock point (if any) onto a single matplotlib axis."""
    ax.plot(data["voltages"], data["raw"], ".", alpha=0.4, label="raw error")
    ax.plot(data["voltages"], data["smoothed"], "-", label="smoothed error")
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--")

    for v, _ in data["crossings"]:
        ax.axvline(v, color="gray", linestyle=":", alpha=0.6)

    if data["chosen"] is not None:
        chosen_v, chosen_slope = data["chosen"]
        ax.plot(chosen_v, 0, "r*", markersize=16,
                 label=f"chosen ({chosen_v:.4f} V, slope {chosen_slope:.3f})")

    ax.set_xlabel("DAC channel A voltage (V)")
    ax.set_ylabel("error signal (V)")
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)


def save_scan_plot(session_id, coarse_data, fine_data):
    """
    Save a single PNG with the coarse scan and fine scan side by side as
    two subplots. fine_data may be None if the coarse pass found no zero
    crossings (fine pass never ran). No-op if SAVE_SCAN_PLOTS is False.
    """
    if not SAVE_SCAN_PLOTS:
        return

    os.makedirs(SCAN_PLOT_DIR, exist_ok=True)

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

    filename = os.path.join(SCAN_PLOT_DIR, f"{session_id}_scan.png")
    fig.savefig(filename, dpi=150)
    plt.close(fig)
    print(f"Saved scan plot to {filename}")


def scan_pass(v_min, v_max, num_points, pass_tag):
    """
    Sweep DAC channel A from v_min to v_max over num_points steps,
    recording and publishing the "error" signal at every step (each point
    averaged over NUM_SAMPLES_PER_POINT raw reads to reduce jitter).

    Returns (voltages, error_signal) as numpy arrays - the full trace,
    since we need the whole curve to find every zero crossing, not just
    the first hit.
    """
    voltages = np.linspace(v_min, v_max, num_points)
    error_signal = np.empty(num_points)

    for i, v in enumerate(voltages):
        set_dac_voltage(dac.channel_a, v)
        time.sleep(SCAN_SETTLE_TIME)

        values = publish_reading(
            "scanning",
            {"scan_voltage": float(v), "scan_pass": pass_tag},
            num_samples=NUM_SAMPLES_PER_POINT,
        )
        error_signal[i] = values["error"]

    return voltages, error_signal


def find_zero_crossings(voltages, signal):
    """
    Find every zero crossing in `signal` (sign change between adjacent
    samples), refining each crossing's location by linear interpolation
    and estimating the local slope (signal/voltage) there.

    Returns a list of (crossing_voltage, slope) tuples.
    """
    crossings = []
    for i in range(len(signal) - 1):
        y0, y1 = signal[i], signal[i + 1]
        if y0 == 0.0:
            continue  # exact zero sample; the neighboring sign-change check covers it
        if y0 * y1 < 0.0:
            x0, x1 = voltages[i], voltages[i + 1]
            frac = -y0 / (y1 - y0)
            x_cross = x0 + frac * (x1 - x0)
            slope = (y1 - y0) / (x1 - x0)
            crossings.append((float(x_cross), float(slope)))
    return crossings


def autolock():
    """
    short_caps_and_block_pid(), scan channel A while reading the "error"
    signal, lock onto the steepest zero crossing, then restart_pid().

    Returns True if a zero crossing was found and locked, False otherwise.
    """
    print("Starting PDH-style autolock scan on the error signal...")
    short_caps_and_block_pid()
    session_id = time.strftime("%Y%m%d_%H%M%S")

    coarse_step = (SCAN_MAX_VOLTAGE - SCAN_MIN_VOLTAGE) / (NUM_COARSE_POINTS - 1)
    coarse_voltages, coarse_signal = scan_pass(
        SCAN_MIN_VOLTAGE, SCAN_MAX_VOLTAGE, NUM_COARSE_POINTS, "coarse"
    )
    coarse_signal_smoothed = smooth_signal(coarse_signal, SMOOTHING_WINDOW)
    coarse_crossings = find_zero_crossings(coarse_voltages, coarse_signal_smoothed)
    coarse_data = {
        "voltages": coarse_voltages,
        "raw": coarse_signal,
        "smoothed": coarse_signal_smoothed,
        "crossings": coarse_crossings,
        "chosen": None,
    }

    if not coarse_crossings:
        print("No zero crossings found on the error signal - autolock failed.")
        save_scan_plot(session_id, coarse_data, None)
        set_dac_voltage(dac.channel_a, RESET_VOLTAGE_ON_FAILURE)
        # Leave B, C, D held HIGH so it's visible the system is not locked.
        return False

    coarse_best_v, coarse_best_slope = max(coarse_crossings, key=lambda c: abs(c[1]))
    coarse_data["chosen"] = (coarse_best_v, coarse_best_slope)
    print(f"Coarse pass found {len(coarse_crossings)} zero crossing(s); "
          f"largest is near {coarse_best_v:.4f} V (slope {coarse_best_slope:.4f} V/V). "
          f"Refining...")

    fine_min = max(SCAN_MIN_VOLTAGE, coarse_best_v - coarse_step)
    fine_max = min(SCAN_MAX_VOLTAGE, coarse_best_v + coarse_step)
    fine_voltages, fine_signal = scan_pass(fine_min, fine_max, NUM_FINE_POINTS, "fine")
    fine_signal_smoothed = smooth_signal(fine_signal, SMOOTHING_WINDOW)
    fine_crossings = find_zero_crossings(fine_voltages, fine_signal_smoothed)

    if fine_crossings:
        lock_v, lock_slope = max(fine_crossings, key=lambda c: abs(c[1]))
    else:
        # Fine pass didn't reconfirm a crossing (edge/noise case) - fall
        # back to the coarse estimate rather than leaving channel A
        # somewhere unintended.
        lock_v, lock_slope = coarse_best_v, coarse_best_slope

    fine_data = {
        "voltages": fine_voltages,
        "raw": fine_signal,
        "smoothed": fine_signal_smoothed,
        "crossings": fine_crossings,
        "chosen": (lock_v, lock_slope),
    }
    save_scan_plot(session_id, coarse_data, fine_data)

    set_dac_voltage(dac.channel_a, lock_v)
    print(f"Locked: channel A = {lock_v:.4f} V (slope {lock_slope:.4f} V/V)")
    restart_pid()
    return True


def main():
    print("Starting monitoring/autolock loop "
          "(trigger: slow_output/fast_output safe range). Press Ctrl+C to stop.")
    try:
        while True:
            values = publish_reading("monitoring")

            slow_ok = is_within(values["slow_output"], SLOW_OUTPUT_SAFE_MIN, SLOW_OUTPUT_SAFE_MAX)
            fast_ok = is_within(values["fast_output"], FAST_OUTPUT_SAFE_MIN, FAST_OUTPUT_SAFE_MAX)

            if not (slow_ok and fast_ok):
                print(f"Trigger: slow_output ok={slow_ok} ({values['slow_output']:.4f}V), "
                      f"fast_output ok={fast_ok} ({values['fast_output']:.4f}V)")
                locked = autolock()
                if not locked:
                    print(f"Retrying autolock in {LOCK_RETRY_DELAY}s...")
                    time.sleep(LOCK_RETRY_DELAY)
                continue  # re-check immediately after an autolock attempt

            time.sleep(MONITOR_INTERVAL)
    except KeyboardInterrupt:
        print("\nUnshorting caps...")
        restart_pid()
        set_dac_voltage(dac.channel_a, 2.4)
        print("\nStopping.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()