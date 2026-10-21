# Setting up and running the lockbox web service

This is the step-by-step guide for putting the web version of the lockbox
controller on a Raspberry Pi and running it. Once it's running, anyone on the
same network can open the lockbox page in a web browser. See
[README.md](README.md) for how it works inside.

> **Prototype.** The page has no login: anyone who can reach the Pi on the
> network can control the lockbox. Only run it on a trusted lab network.

Commands marked **on the Pi** are typed into a terminal on the Pi, usually over
SSH from your own computer:

```bash
ssh <user>@<pi-address>
```

---

## 1. One-time setup on a new Pi

### 1.1 Turn on I2C

The DAC and ADC talk to the Pi over I2C, which is off by default.

**On the Pi:**

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

SSH back in after the reboot.

### 1.2 Install system packages

**On the Pi:**

```bash
sudo apt update
sudo apt install -y git i2c-tools
```

### 1.3 Check the DAC and ADC are wired up

**On the Pi:**

```bash
i2cdetect -y 1
```

The grid should show **`60`** (the MCP4728 DAC) and **`48`** (the ADS1115
ADC). If either is missing, check the wiring and power before going further;
the service can't start without both.

### 1.4 Install uv

uv installs Python and the project's packages. Every Pi gets exactly the
versions listed in `uv.lock`.

**On the Pi:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

(Or log out and back in instead of the `source` line.) Check it worked:

```bash
uv --version
```

### 1.5 Download the code

**On the Pi**, in your home folder:

```bash
cd ~
git clone https://github.com/gaklouie/lockbox-remote-control.git
```

If the repository is private, GitHub will ask you to log in. Use a personal
access token as the password, or set up an SSH key on the Pi.

### 1.6 Add the MQTT login (`.env` file)

The service sends its readings to the MQTT broker, which feeds InfluxDB and
Grafana. It needs the broker address and login in a file named `.env` in
the `lockbox_gui` folder. This file is never committed to Git.

**On the Pi:**

```bash
nano ~/lockbox-remote-control/lockbox_gui/.env
```

Put in (with your real values):

```
ADDRESS=<broker address>
MQTT_USERNAME=<username>
PASSWORD=<password>
MQTT_PORT=1883
```

`MQTT_PORT` is optional (1883 is the default). Save with `Ctrl-O`, `Enter`,
then exit with `Ctrl-X`.

If this Pi already runs the Tk GUI, the `.env` file is probably already there,
and the web service uses the same one.

### 1.7 Install the service's packages

**On the Pi:**

```bash
cd ~/lockbox-remote-control/lockbox_web
uv sync
```

The first run downloads Python packages and can take several minutes on a Pi.

If `uv sync` fails while building **RPi.GPIO**, install the build tools and
try again:

```bash
sudo apt install -y build-essential python3-dev
uv sync
```

Setup is done. Go to "Running the service" below.

---

## 2. Running the service

All of these are run **on the Pi**, from the `lockbox_web` folder:

```bash
cd ~/lockbox-remote-control/lockbox_web
```

### Before starting: close the Tk GUI

Only one program can drive the DAC/ADC at a time. If the Tk GUI is open,
close it. If its **"Keep running after GUI closes (headless)"** box is
ticked, untick it first, so its background process stops too. The service
refuses to start while that background process is running.

### Start it (stops when you close SSH)

```bash
uv run python server.py
```

You'll see:

```
Starting the lockbox web service (loading libraries, then connecting to MQTT and the hardware)...
Lockbox web service is running. Open http://192.168.1.42:8000  or  http://<pi-name>.local:8000 in a browser.
Stop it with Ctrl-C (or, if started in the background: pkill -f "python server.py").
```

The second line takes a few seconds to appear. Once it does, open the address
it shows in a browser on any computer on the same network. If the `.local`
name doesn't load, use the number.

Stop it with **`Ctrl-C`**. Closing the terminal or losing the SSH connection
also stops it.

### Start it so it keeps running after you close SSH

```bash
nohup uv run python server.py > server.log 2>&1 &
```

Nothing is printed to the terminal; the messages go to `server.log`. After a
few seconds, check it's up and find the address to open:

```bash
cat server.log
```

You can now close the SSH session; the service keeps running.

### Check whether it's running

```bash
pgrep -af "python server.py"
```

If it prints one or two lines (the `uv run` launcher and the Python process it
started), it's running. No output means it isn't.

To see what it has printed (background mode):

```bash
cat server.log
```

To watch new messages as they arrive (stop watching with `Ctrl-C`; that
doesn't stop the service):

```bash
tail -f server.log
```

The lockbox's own activity (scans, locks, setting changes) appears in the log
panel at the bottom of the web page.

### Stop it

If it's running in the foreground, press **`Ctrl-C`** in its terminal.

If it's running in the background, from any SSH session:

```bash
pkill -f "python server.py"
```

Then check it has stopped (should print nothing):

```bash
pgrep -af "python server.py"
```

If it's still listed after a few seconds (it may be finishing a scan), force
it. This skips the clean shutdown, so use it only as a last resort:

```bash
pkill -9 -f "python server.py"
```

When the service stops, the DAC holds its last values: control out and the
B/C/D lines stay where they were. Nothing is logged and nothing relocks until
you start the service (or the Tk GUI) again.

### Use a different port

The page is on port 8000 by default. To use another port, e.g. 8080:

```bash
uv run python server.py --port 8080
```

### Update to the latest code

```bash
cd ~/lockbox-remote-control
git pull
cd lockbox_web
uv sync
```

Then stop the service and start it again so the new code is used. After an
update, also refresh the page in each open browser.

---

## 3. Things to know

- **Settings** are saved on the Pi in `~/.config/autolock_gui/settings.json`
  and shared with the Tk GUI, so ranges, scan parameters and the lockbox name
  carry over between the two.
- **Autolock after a restart:** if autolock was engaged when the service
  stopped, it is engaged again when the service starts.
- **Saved autolock plots** go in `lockbox_web/scan_plots/` (when "Save
  autolock scan plots" is ticked in Settings).
- **Several people can have the page open at once.** Everyone sees the same
  live data, and a change made by one shows up for all.
- **Doesn't start by itself after the Pi reboots.** Start it again with one of
  the commands above.

---

## 4. Troubleshooting

| What you see | What to do |
|---|---|
| `The Tk GUI's headless process is running and owns the hardware` | Open the Tk GUI, untick "Keep running after GUI closes", close it, then start the service. |
| `address already in use` | The service (or something else) is already using that port. Check with `pgrep -af "python server.py"`, stop the old one, or use `--port`. |
| An error mentioning MQTT, `connect`, or `ConnectionRefusedError` at startup | The `.env` file is missing or wrong, or the broker can't be reached from the Pi. Check `lockbox_gui/.env` (step 1.6). |
| An error mentioning I2C, `No I2C device`, or `ValueError` at startup | I2C is off or a chip isn't detected. Repeat steps 1.1 and 1.3. |
| The page doesn't load in the browser | Make sure the "running" line has appeared. Use the numeric address rather than `.local`, and check your computer is on the same network as the Pi. |
| The page says **"Disconnected - retrying"** | The service stopped or restarted. Check it with `pgrep -af "python server.py"` and look at `server.log`. The page reconnects by itself once the service is back. |
| A button is greyed out | Hover over it: a scan is running, or autolock is engaged (disengage it first). |
