import time
import board
import busio

import adafruit_mcp4728 as mcp4728_mod


i2c = busio.I2C(board.SCL, board.SDA)

# Create the DAC object 
dac = mcp4728_mod.MCP4728(i2c)

dac.channel_b.value = int(65535)
dac.channel_c.value = int(65535)
dac.channel_d.value = int(65535)

# ads.gain = 1 
for i in range(5):
    for j in range(50):
        dac.channel_a.value = int(65535/50 * j)
        time.sleep(0.001)
    for j in range(50):
        dac.channel_a.value = int(65535 - 65535/50 * j)
        time.sleep(0.001)

dac.channel_a.value = int(65535/2)
dac.channel_b.value = int(0)
dac.channel_c.value = int(0)
dac.channel_d.value = int(0)

