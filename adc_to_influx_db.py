"""
Continuously read all four channels of an ADS1115 ADC and publish the
voltages over MQTT, matching the broker/credential pattern used
elsewhere in this project. A separate MQTT subscriber (e.g. Telegraf's
mqtt_consumer input, or another script) is assumed to pick these
messages up and write them into InfluxDB.

Hardware:
    ADS1115 (4-channel 16-bit ADC) -- I2C

Install dependencies:
    pip install adafruit-blinka adafruit-circuitpython-ads1x15 paho-mqtt python-dotenv

Expects a .env file with (same as your other MQTT programs):
    ADDRESS=<broker address>
    MQTT_USERNAME=<username>
    PASSWORD=<password>
    MQTT_PORT=1883          # optional, defaults to 1883 if not set
"""

import os
import json
import time

from dotenv import load_dotenv
import paho.mqtt.client as mqtt

import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# --------------------------------------------------------------------------
# MQTT configuration (matches existing project pattern)
# --------------------------------------------------------------------------
load_dotenv(".env")

mqttTopic = "experiment/sensor/ncc-1701/lockbox1"
mqttBrokerAddress = os.environ.get("ADDRESS")
mqttPort = int(os.environ.get("MQTT_PORT", 1883))
credentials = {
    "username": os.environ.get("MQTT_USERNAME"),
    "password": os.environ.get("PASSWORD"),
}

# --------------------------------------------------------------------------
# ADC configuration
# --------------------------------------------------------------------------
SAMPLE_INTERVAL = 0.1   # seconds between samples
ADS_GAIN = 1            # 1 -> +/-4.096V input range; use 2/3 for +/-6.144V

# --------------------------------------------------------------------------
# Set up I2C and ADC
# --------------------------------------------------------------------------
i2c = busio.I2C(board.SCL, board.SDA)
ads = ADS.ADS1115(i2c)
ads.gain = ADS_GAIN

channels = [
    AnalogIn(ads, 0),
    AnalogIn(ads, 1),
    AnalogIn(ads, 2),
    AnalogIn(ads, 3),
]

# --------------------------------------------------------------------------
# Set up MQTT client
# --------------------------------------------------------------------------
client = mqtt.Client()
client.username_pw_set(**credentials)
client.connect(mqttBrokerAddress, mqttPort)
client.loop_start()

def adc_vout_to_vin(vout):
    return 56*(-0.227273 + 0.118867*vout)

def adc_verr_to_vin(verr):
    return 18*(-0.185185 + 0.0925927*verr)

def read_and_publish():
    payload = {
        "timestamp": time.time(),
        "slow": adc_vout_to_vin(channels[0].voltage),
        "fast": adc_vout_to_vin(channels[1].voltage),
        "error": adc_verr_to_vin(channels[2].voltage),
        "dc_error": channels[3].voltage,
    }
    client.publish(mqttTopic, json.dumps(payload))

    values_str = ", ".join(
        f"CH{i}={c.voltage:.4f} V" for i, c in enumerate(channels)
    )
    print(f"Published to {mqttTopic}: {values_str}")


def main():
    print(f"Publishing ADS1115 channels 0-3 to '{mqttTopic}' "
          f"every {SAMPLE_INTERVAL}s. Press Ctrl+C to stop.")
    try:
        while True:
            read_and_publish()
            time.sleep(SAMPLE_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()