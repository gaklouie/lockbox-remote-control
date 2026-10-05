import serial
import time
import json
import paho.mqtt.publish as publish
from dotenv import load_dotenv
import os
import numpy as np

# -----------------------
# MQTT setup
# -----------------------
load_dotenv('.env')
mqttTopic = "experiment/sensor/ncc-1701"
mqttBrokerAddress = os.environ.get("ADDRESS")
credentials = {'username': os.environ.get("MQTT_USERNAME"), 'password': os.environ.get("PASSWORD")}

while True:
    try:
        dummy1 = np.random.random()*10
        dummy2 = np.random.random()+1
        payload = { 
                    "dummy1" : dummy1,
                    "dummy2" : dummy2
                    }
        payloadJSON = json.dumps(payload)

        # Send JSON Payload to MQTT Broker
        publish.single(mqttTopic, payloadJSON, hostname = mqttBrokerAddress, auth = credentials)

        # Print the readings
        print("Dummy 1: {:.2f}".format(dummy1))
        print("Dummy 2: {:.2f}".format(dummy2))

        # Wait for a few seconds before the next reading
        time.sleep(2)

    except KeyboardInterrupt:
        print('Program stopped')
        break
    except Exception as e:
        print('An unexpected error occurred:', str(e))
        break


