"""
I2C access to the MCP4728 DAC and ADS1115 ADC.

Call init_hardware() once at startup before using anything else here.
Every I2C transaction goes through i2c_lock, so the monitor thread, scan
threads, and GUI button handlers can all call these functions safely.

DAC channel roles:
    A        "control out" - set in PHYSICAL volts via set_control_out()
    B, C, D  digital-style lines that short the caps / block the PID
             (HIGH) or restart it (LOW); fixed raw DAC volts.
"""

import threading
import time

import board
import busio
import adafruit_mcp4728
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

from . import state
from .channels import control_out_to_dac_volts, raw_to_physical
from .config import ADS_GAIN, DAC_HIGH_VOLTAGE, DAC_LOW_VOLTAGE, VDD_VOLTAGE, settings

i2c_lock = threading.Lock()  # serializes raw I2C transactions across threads

dac = None
adc_channels = None


def init_hardware():
    global dac, adc_channels
    i2c = busio.I2C(board.SCL, board.SDA)

    dac = adafruit_mcp4728.MCP4728(i2c)
    for ch in (dac.channel_a, dac.channel_b, dac.channel_c, dac.channel_d):
        ch.vref = adafruit_mcp4728.Vref.VDD
        ch.gain = 1

    ads = ADS.ADS1115(i2c)
    ads.gain = ADS_GAIN
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


def read_physical(num_samples=1):
    """Read all four ADC channels, returning {name: physical volts}."""
    return raw_to_physical(read_raw_averaged(num_samples))
