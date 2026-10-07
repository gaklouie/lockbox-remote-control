import time
import board
import busio
# import Adafruit_ADS1x15.ADS1115 as ADS
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn


i2c = busio.I2C(board.SCL, board.SDA)

# Create the ADS object and specify the gain
ads = ADS.ADS1115(i2c)
# ads.gain = 1 
chan = AnalogIn(ads, 1)

# Continuously print the values
while True:
    print(f"ADC Voltage: {chan.voltage}V")
    time.sleep(0.5)
