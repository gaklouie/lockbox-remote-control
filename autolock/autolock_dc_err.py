"""
Autolocking DAC/ADC control loop with MQTT logging.

Combines:
    - MCP4728 quad DAC: channel A is the scanned control voltage;
      channels B, C, D are digital-style lines that short the caps and
      block the PID (HIGH) or release it (LOW).
    - ADS1115 quad ADC: channel 3 is monitored as the lock-status signal,
      all four channels are read and logged every cycle.
    - MQTT publishing of ADC readings + lock state (same env/credential
      pattern as the rest of the project), for logging to InfluxDB/Grafana.

Autolock behavior:
    1. Continuously monitor ADC channel 3.
    2. If it leaves the safe voltage range (SAFE_VOLTAGE_MIN/MAX below):
        a. short_caps_and_block_pid(): set DAC channels B, C, D HIGH.
        b. Coarse pass: sweep DAC channel A across the full range in big
           steps, reading + logging ADC channel 3 (and 0-2) at every step.
        c. Fine pass: once the coarse pass finds a safe point, zoom into
           the neighborhood around it (+/- one coarse step) and sweep
           again at fine resolution to refine the lock voltage.
        d. As soon as a safe point is confirmed, channel A is left at
           that voltage and restart_pid() sets B, C, D LOW.
        e. If the coarse pass finds no safe point at all, channel A is
           reset to RESET_VOLTAGE_ON_FAILURE, B/C/D are left HIGH (so
           it's visibly unlocked), and the autolock is retried after a
           short delay.
    3. Resume normal monitoring/logging until channel 3 next leaves the
       safe range.

Every published MQTT message includes a "lock_status" boolean: False
when ADC channel 3 is currently outside the safe range, True when it's
inside it.

Install dependencies:
    pip install adafruit-blinka adafruit-circuitpython-mcp4728 \
                adafruit-circuitpython-ads1x15 paho-mqtt python-dotenv numpy
"""

import os
import json
import time

import numpy as np
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

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

SAFE_VOLTAGE_MIN = 1.0       # Lower bound of ADC channel 3 "safe"/locked range
SAFE_VOLTAGE_MAX = 2.0       # Upper bound of ADC channel 3 "safe"/locked range

SCAN_MIN_VOLTAGE = 0.0       # Start of DAC channel A autolock sweep
SCAN_MAX_VOLTAGE = 4.0       # End of DAC channel A autolock sweep
NUM_COARSE_POINTS = 51       # Coarse pass resolution (~80 mV/step over 4V)
NUM_FINE_POINTS = 41         # Fine pass resolution, within one coarse step
SCAN_SETTLE_TIME = 0.01      # Seconds to wait after each DAC step before reading

RESET_VOLTAGE_ON_FAILURE = 2.0  # Channel A voltage if no safe point is found

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
# Helpers
# --------------------------------------------------------------------------
def voltage_to_dac_value(voltage, vdd=VDD_VOLTAGE):
    """Convert a target voltage into a 16-bit scaled MCP4728 channel value."""
    value = int(round((voltage / vdd) * 65535))
    return max(0, min(65535, value))


def set_dac_voltage(channel, voltage):
    channel.value = voltage_to_dac_value(voltage)


def is_safe(voltage):
    return SAFE_VOLTAGE_MIN <= voltage <= SAFE_VOLTAGE_MAX


def publish_reading(state, extra_fields=None):
    """Read all 4 ADC channels and publish them (+ state info) over MQTT."""
    readings = [c.voltage for c in adc_channels]
    lock_status = is_safe(readings[3])
    payload = {
        "timestamp": time.time(),
        "ch0_voltage": readings[0],
        "ch1_voltage": readings[1],
        "ch2_voltage": readings[2],
        "ch3_voltage": readings[3],
        "lock_status": lock_status,  # False if CH3 outside safe range, True if inside
        "state": state,              # "monitoring" or "locking"
    }
    if extra_fields:
        payload.update(extra_fields)

    client.publish(mqttTopic, json.dumps(payload))
    print(f"[{state}] CH0={readings[0]:.4f}V CH1={readings[1]:.4f}V "
          f"CH2={readings[2]:.4f}V CH3={readings[3]:.4f}V lock_status={lock_status}")
    return readings


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


def scan_pass(v_min, v_max, num_points, pass_tag):
    """
    Sweep DAC channel A from v_min to v_max over num_points steps, reading
    and publishing ADC channel 3 at each step. Stops and returns the
    voltage as soon as channel 3 is back in the safe range.

    Returns the lock voltage (float) if found, otherwise None.
    """
    voltages = np.linspace(v_min, v_max, num_points)
    for v in voltages:
        set_dac_voltage(dac.channel_a, v)
        time.sleep(SCAN_SETTLE_TIME)

        readings = publish_reading(
            "locking", {"scan_voltage": float(v), "scan_pass": pass_tag}
        )

        if is_safe(readings[3]):
            return float(v)

    return None


def autolock():
    """
    short_caps_and_block_pid(), then run a coarse pass followed by a fine
    pass over the neighborhood of the coarse hit to refine the lock
    voltage on channel A. Calls restart_pid() once locked.

    Returns True if a lock point was found, False otherwise.
    """
    print(f"Channel 3 out of range - starting autolock "
          f"(safe range {SAFE_VOLTAGE_MIN}-{SAFE_VOLTAGE_MAX} V)")
    short_caps_and_block_pid()

    coarse_step = (SCAN_MAX_VOLTAGE - SCAN_MIN_VOLTAGE) / (NUM_COARSE_POINTS - 1)
    coarse_v = scan_pass(SCAN_MIN_VOLTAGE, SCAN_MAX_VOLTAGE, NUM_COARSE_POINTS, "coarse")

    if coarse_v is None:
        print("Autolock scan complete - no safe point found.")
        set_dac_voltage(dac.channel_a, RESET_VOLTAGE_ON_FAILURE)
        # Leave B, C, D held HIGH so it's visible the system is not locked.
        return False

    print(f"Coarse pass found a safe point near {coarse_v:.4f} V - refining...")
    fine_min = max(SCAN_MIN_VOLTAGE, coarse_v - coarse_step)
    fine_max = min(SCAN_MAX_VOLTAGE, coarse_v + coarse_step)
    fine_v = scan_pass(fine_min, fine_max, NUM_FINE_POINTS, "fine")

    lock_v = fine_v if fine_v is not None else coarse_v
    set_dac_voltage(dac.channel_a, lock_v)  # ensure channel A ends exactly here
    print(f"Locked: channel A = {lock_v:.4f} V")
    restart_pid()
    return True


def main():
    print("Starting monitoring/autolock loop. Press Ctrl+C to stop.")
    try:
        while True:
            readings = publish_reading("monitoring")

            if not is_safe(readings[3]):
                locked = autolock()
                if not locked:
                    print(f"Retrying autolock in {LOCK_RETRY_DELAY}s...")
                    time.sleep(LOCK_RETRY_DELAY)
                continue  # re-check immediately after an autolock attempt

            time.sleep(MONITOR_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()