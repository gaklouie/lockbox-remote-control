"""
MQTT publishing - the path by which readings reach InfluxDB/Grafana.

Broker address and credentials come from a .env file (matching the
project's other logging scripts):

    ADDRESS=<broker host>
    MQTT_USERNAME=<user>
    PASSWORD=<password>
    MQTT_PORT=1883          (optional)

Call init_mqtt() once at startup and close_mqtt() on shutdown.
"""

import json
import os
import re
import socket
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from .config import MQTT_TOPIC_PREFIX, settings
from .hardware import lock_status

mqtt_client = None


def mqtt_topic():
    """
    The topic monitoring readings are published to:
    experiment/sensor/<hostname>/<lockbox name>, e.g. host "NCC-1701" with
    lockbox name "lattice laser" -> experiment/sensor/ncc-1701/lattice_laser.
    The hostname is lowercased and loses any domain (".local"). In the name,
    spaces - and "/", "+", "#", which MQTT treats specially - become "_".
    With no name set, the last part is "unnamed".
    """
    host = socket.gethostname().split(".")[0].lower()
    name = re.sub(r"[\s/+#]+", "_", settings.DEVICE_NAME.strip()) or "unnamed"
    return f"{MQTT_TOPIC_PREFIX}/{host}/{name}"


def init_mqtt():
    global mqtt_client
    # .env in the current directory first, then next to server.py.
    # load_dotenv never overrides a variable that's already set.
    load_dotenv(".env")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    broker_address = os.environ.get("ADDRESS")
    port = int(os.environ.get("MQTT_PORT", 1883))
    credentials = {
        "username": os.environ.get("MQTT_USERNAME"),
        "password": os.environ.get("PASSWORD"),
    }

    mqtt_client = mqtt.Client()
    mqtt_client.username_pw_set(**credentials)
    mqtt_client.connect(broker_address, port)
    mqtt_client.loop_start()


def close_mqtt():
    if mqtt_client is not None:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


def publish_values(values):
    """
    Publish one monitoring reading as JSON: timestamp, device name, state
    (always "monitoring" - kept so existing subscribers don't break),
    lock_status (see hardware.lock_status) and the readings. The device name
    falls back to the hostname if none has been set, so every message says
    which Pi sent it. Scan points are not published.
    """
    payload = {
        "timestamp": time.time(),
        "device_name": settings.DEVICE_NAME or socket.gethostname(),
        "state": "monitoring",
        "lock_status": lock_status(values),
    }
    payload.update(values)
    mqtt_client.publish(mqtt_topic(), json.dumps(payload))
