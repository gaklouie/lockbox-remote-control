"""
Sweep MCP4728 DAC channel A across a range of voltages, read back the
result on ADS1115 ADC channel 0, and plot set-voltage vs. measured-voltage.

Hardware:
    MCP4728 (quad 12-bit DAC)  -- I2C
    ADS1115 (4-channel 16-bit ADC) -- I2C
    Wire DAC channel A (VA) output directly to ADC channel 0 (A0), with a
    common ground between DAC, ADC, and their supply.

Install dependencies (Raspberry Pi / Linux with I2C enabled):
    pip3 install adafruit-circuitpython-mcp4728 adafruit-circuitpython-ads1x15 \
                 adafruit-blinka matplotlib numpy
"""

import time

import board
import busio
import numpy as np
import matplotlib.pyplot as plt

import adafruit_mcp4728
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# --------------------------------------------------------------------------
# Configuration - adjust these for your setup
# --------------------------------------------------------------------------
VDD_VOLTAGE = 5     # Supply voltage feeding the MCP4728 (its DAC full-scale
                        # reference when vref=VDD). Change to 5.0 if that's
                        # what you're powering it with.
NUM_POINTS = 21         # Number of voltage steps in the sweep
SETTLE_TIME = 0.05      # Seconds to wait after each DAC update before reading
ADS_GAIN = 1            # ADS1115 gain: 1 -> +/-4.096V input range (safe for
                        # 3.3V systems). If VDD_VOLTAGE > 4.0V, use 2/3
                        # (+/-6.144V range) instead.

# --------------------------------------------------------------------------
# Set up I2C bus and devices
# --------------------------------------------------------------------------
i2c = busio.I2C(board.SCL, board.SDA)

dac = adafruit_mcp4728.MCP4728(i2c)          # default address 0x60
dac.channel_a.vref = adafruit_mcp4728.Vref.VDD
dac.channel_a.gain = 1

ads = ADS.ADS1115(i2c)                       # default address 0x48
ads.gain = ADS_GAIN
adc_channel = AnalogIn(ads, 1)

dac.channel_b.value = int(65535)
dac.channel_c.value = int(65535)
dac.channel_d.value = int(65535)

# --------------------------------------------------------------------------
# Sweep DAC channel A, read ADC channel 0
# --------------------------------------------------------------------------
set_voltages = np.linspace(0, 4, NUM_POINTS)
measured_voltages = []

for v in set_voltages:
    # Convert desired voltage to a 16-bit scaled DAC value (0-65535)
    dac_value = int(round((v / VDD_VOLTAGE) * 65535))
    dac_value = max(0, min(65535, dac_value))

    dac.channel_a.value = dac_value
    time.sleep(SETTLE_TIME)  # allow the DAC output / ADC input to settle

    reading = adc_channel.voltage
    measured_voltages.append(reading)

    print(f"Set: {v:6.3f} V  ->  Measured: {reading:6.3f} V")

# Return DAC to 0V when done
dac.channel_a.value = int(round(65535/2))
dac.channel_b.value = 0
dac.channel_c.value = 0
dac.channel_d.value = 0

# --------------------------------------------------------------------------
# Plot results
# --------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 6))

ax.plot(set_voltages, measured_voltages, "o-", label="Measured (ADS1115 CH0)")

ax.set_xlabel("DAC set voltage (V)")
ax.set_ylabel("ADC measured voltage (V)")
ax.set_title("MCP4728 (CH A) output vs. ADS1115 (CH0) measurement")
ax.legend()
ax.grid(True)

plt.tight_layout()
plt.savefig("dac_adc_sweep.png", dpi=150)
plt.show()