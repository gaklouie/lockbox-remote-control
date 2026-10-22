"""
I2C access to the MCP4728 DAC and ADS1115 ADC.

Call init_hardware() once at startup before using anything else here.
Every I2C transaction goes through i2c_lock, so the monitor thread, scan
threads, and command handlers can all call these functions safely.

DAC channel roles:
    A        "control out" - set in PHYSICAL volts via set_control_out()
    B, C, D  digital-style lines that short the caps / block the PID
             (HIGH) or restart it (LOW); fixed raw DAC volts.

ADC modes:
    normal  single-shot conversions at ADS_NORMAL_DATA_RATE, one per read;
            used for the monitor loop and normal sweeps (all four channels).
    fast    continuous conversions of ONE channel at ADS_FAST_DATA_RATE;
            used by fast sweeps (see fast_adc() and read_signal_fast()).
"""

import threading
import time
from contextlib import contextmanager

import board
import busio
import adafruit_mcp4728
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.ads1x15 import Mode
from adafruit_ads1x15.analog_in import AnalogIn

from . import state
from .channels import CHANNEL_CONVERTERS, control_out_to_dac_volts, raw_to_physical
from .config import ADS_GAIN, DAC_HIGH_VOLTAGE, DAC_LOW_VOLTAGE, VDD_VOLTAGE, settings

ADS_NORMAL_DATA_RATE = 128   # conversions/s: the driver's default, lower noise
ADS_FAST_DATA_RATE = 860     # conversions/s: the ADS1115's maximum

i2c_lock = threading.Lock()  # serializes raw I2C transactions across threads

dac = None
ads = None
adc_channels = None


def init_hardware():
    global dac, adc_channels
    i2c = busio.I2C(board.SCL, board.SDA)

    dac = adafruit_mcp4728.MCP4728(i2c)
    for ch in (dac.channel_a, dac.channel_b, dac.channel_c, dac.channel_d):
        ch.vref = adafruit_mcp4728.Vref.VDD
        ch.gain = 1

    global ads
    ads = ADS.ADS1115(i2c)
    ads.gain = ADS_GAIN
    ads.data_rate = ADS_NORMAL_DATA_RATE
    adc_channels = [AnalogIn(ads, i) for i in range(4)]  # channels 0-3


# --------------------------------------------------------------------------
# DAC
# --------------------------------------------------------------------------
def voltage_to_dac_value(voltage, vdd=VDD_VOLTAGE):
    value = int(round((voltage / vdd) * 65535))
    return max(0, min(65535, value))


def set_dac_voltage(channel, voltage):
    """Set a DAC channel to a raw DAC voltage (0-VDD_VOLTAGE)."""
    with i2c_lock:
        channel.value = voltage_to_dac_value(voltage)


def set_control_out(v_physical):
    """
    Set channel A ("control out") to a target PHYSICAL voltage, converting
    through the shift/amplify circuit and clipping to the DAC's achievable
    raw range (warning via the live log if clipping was needed).
    """
    v_dac = control_out_to_dac_volts(v_physical)
    clipped = max(0.0, min(VDD_VOLTAGE, v_dac))
    if abs(clipped - v_dac) > 1e-9:
        state.log(f"WARNING: requested control out {v_physical:.4f} V physical "
                  f"needs {v_dac:.4f} V at the DAC, outside [0, {VDD_VOLTAGE}] V - "
                  f"clipped to {clipped:.4f} V raw.")
    set_dac_voltage(dac.channel_a, clipped)


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


def pid_running():
    """
    True if B, C, D are all LOW (PID running), False if all HIGH (caps
    shorted, PID blocked), None if they disagree or the DAC isn't set up yet.

    Uses the values the DAC driver keeps: it reads the chip's real outputs
    when the service starts and updates them on every write, so this is
    right even for values left over from before a restart, and needs no
    I2C traffic.
    """
    if dac is None:
        return None
    threshold = (DAC_HIGH_VOLTAGE + DAC_LOW_VOLTAGE) / 2
    high = [ch.value / 65535 * VDD_VOLTAGE > threshold
            for ch in (dac.channel_b, dac.channel_c, dac.channel_d)]
    if not any(high):
        return True
    if all(high):
        return False
    return None


def lock_status(values):
    """
    True when the lockbox is locked and nothing is disturbing it: no manual
    scan, autolock scan or follow-mode locking is running, the PID is on
    (B/C/D low), and slow and fast output are both within their safe ranges
    in `values` (physical volts). False otherwise - including for a reading
    that lacks the outputs (e.g. a fast-sweep scan point).
    """
    if state.manual_scan_active.is_set() or state.autolock_running.is_set() or state.lock_acquiring.is_set():
        return False
    if pid_running() is not True:
        return False
    if "slow_output" not in values or "fast_output" not in values:
        return False
    return (settings.SLOW_OUTPUT_SAFE_MIN <= values["slow_output"] <= settings.SLOW_OUTPUT_SAFE_MAX
            and settings.FAST_OUTPUT_SAFE_MIN <= values["fast_output"] <= settings.FAST_OUTPUT_SAFE_MAX)


# --------------------------------------------------------------------------
# ADC
# --------------------------------------------------------------------------
def read_raw_averaged(num_samples=1):
    sums = [0.0, 0.0, 0.0, 0.0]
    with i2c_lock:
        for _ in range(num_samples):
            for i, chan in enumerate(adc_channels):
                sums[i] += chan.voltage
    return [s / num_samples for s in sums]


def _channel_for(signal_name):
    return adc_channels[list(CHANNEL_CONVERTERS).index(signal_name)]


@contextmanager
def fast_adc(signal_name):
    """
    For the duration of a sweep, run the ADC in continuous mode on the one
    channel carrying `signal_name`, at the maximum rate. Each read is then a
    single short I2C transfer instead of a start-wait-read exchange. Always
    restores the normal mode afterwards, even if the sweep is stopped.
    Does nothing if signal_name is None.

    The channel is selected with an ordinary single-shot read BEFORE switching
    to continuous mode. The driver (adafruit-circuitpython-ads1x15 3.0.5)
    cannot select a channel in continuous mode: it silently keeps whichever
    channel the chip was last set to. (Relying on it read the DC error input -
    whatever the monitor read last - instead of the error input.) Switching
    mode keeps the selected channel, and read_signal_fast() never asks the
    driver to select one.
    """
    if signal_name is None:
        yield
        return
    with i2c_lock:
        ads.data_rate = ADS_FAST_DATA_RATE
        _channel_for(signal_name).voltage   # single-shot read: points the chip at this channel
        ads.mode = Mode.CONTINUOUS          # starts converting that channel continuously
    try:
        yield
    finally:
        with i2c_lock:
            ads.mode = Mode.SINGLE          # single-shot reads select their own channel again
            ads.data_rate = ADS_NORMAL_DATA_RATE


def read_signal_fast(signal_name, num_samples=1):
    """
    Read one signal (physical volts) in fast mode, averaging num_samples
    separate conversions. Must be called inside fast_adc(signal_name).

    In continuous mode the chip converts on its own schedule; this reads its
    latest finished result directly (get_last_result), bypassing the
    driver's channel handling. Waiting two conversion periods first
    guarantees that result was taken entirely after control out moved and
    settled; waiting one period between samples guarantees each is a new
    conversion rather than the same one read twice.
    """
    period = 1.0 / ADS_FAST_DATA_RATE
    channel = _channel_for(signal_name)
    time.sleep(2 * period)
    total = 0.0
    for i in range(num_samples):
        if i:
            time.sleep(period)
        with i2c_lock:
            raw = ads.get_last_result()      # 16-bit two's complement conversion result
        signed = raw - 0x10000 if raw & 0x8000 else raw
        total += channel.convert_to_voltage(signed)
    return CHANNEL_CONVERTERS[signal_name](total / num_samples)


def read_physical(num_samples=1):
    """Read all four ADC channels, returning {name: physical volts}."""
    return raw_to_physical(read_raw_averaged(num_samples))
