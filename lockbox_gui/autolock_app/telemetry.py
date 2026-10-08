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
import socket
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from .config import MQTT_TOPIC, settings

mqtt_client = None


def init_mqtt():
    global mqtt_client
    # .env in the current directory first, then next to autolock_gui.py.
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


def publish_values(values, status, extra_fields=None):
    """
    Publish one JSON payload: timestamp, device name, status
    ("monitoring"/"scanning"), and the readings. The device name falls back
    to the hostname if none has been set, so every message says which Pi sent it.
    """
    payload = {
        "timestamp": time.time(),
        "device_name": settings.DEVICE_NAME or socket.gethostname(),
        "state": status,
    }
    payload.update(values)
    if extra_fields:
        payload.update(extra_fields)
    mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))
