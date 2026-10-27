# Lockbox web service

The lockbox controller: an always-on service on the Pi plus a web page to
watch and control it. It replaced the Tk GUI, which is archived in
`../old/lockbox_gui` and no longer maintained.

## The idea

In the old Tk GUI, the program that drew the window was also the program that
read the ADC, logged to MQTT and relocked. Closing the window stopped it all,
and only one person could use it at a time.

Here those jobs are split:

- **The service** (`server.py`) runs on the Pi all the time. It is the only
  program that touches the DAC/ADC. It does the monitoring, MQTT logging and
  autolock whether or not anyone is looking.
- **The page** (`static/`) is a web page the service hosts. Any computer on
  the network opens `http://<pi-address>:8000` in a browser. Several people
  can have it open at once; they all see the same live data, and a change
  made by one shows up for everyone.

Nothing needs installing on the computers that view it, and there is no SSH
window forwarding.

## Running it

For step-by-step setup on a new Pi, running it in the background, stopping
it and troubleshooting, see [SETUP.md](SETUP.md). In short, on the Pi, from
this folder:

    uv sync
    uv run python server.py

Then browse to `http://<pi-address>:8000`. Options: `--host`, `--port`.

The MQTT broker login goes in `.env` in this folder. Settings are saved in
`~/.config/autolock_gui/settings.json` (the same file the old Tk GUI used, so
existing settings carry over). If autolock was engaged when the service last
ran, it re-engages on start.

## How it is built

    server.py                 the service: web server, shared state, commands
    autolock_app/             hardware, logging, scans and lock logic
        config.py             constants, the Settings dataclass, save/load of settings
        state.py              cross-thread events and the worker -> service event queue
        channels.py           ADC/DAC conversions to physical volts, signal names
        hardware.py           I2C access to the MCP4728 DAC and ADS1115 ADC
        telemetry.py          MQTT publishing (the InfluxDB/Grafana logging path)
        signal_processing.py  smoothing, zero crossings, lock-range segments
        scanning.py           sweeps of control out (uniform and adaptive spacing)
        plotting.py           signal colors, saved autolock PNGs
        locking.py            the two automatic autolock procedures
        workers.py            background threads: monitor loop, manual scan
    static/index.html         the page
    static/app.js             page logic: WebSocket, tabs, forms
    static/plot.js            small dependency-free canvas line plot
    static/style.css
    static/icon.png, icon_32.png   browser-tab icon

Flow of data:

    worker threads -> state.event_queue -> pump thread -> Hub (asyncio)
        -> JSON over one WebSocket (/ws) to every connected browser

The Hub keeps a rolling history (recent readings, log tail, last scan), so a
browser that connects later sees the current picture straight away.

### Messages from the service

| type            | contents                                                     |
|-----------------|--------------------------------------------------------------|
| `hello`         | sent once on connect: page description, status, history      |
| `status`        | hostname, name, settings, engaged, busy reason               |
| `reading`       | one set of readings: `t`, `values`                           |
| `log`           | one log line: `t`, `message`                                 |
| `scan_progress` | `fraction` (0-1) of the manual scan                          |
| `scan`          | finished manual scan: voltages, traces, smoothed, candidates |
| `scan_failed`   | the manual scan stopped with an error (details in the log)   |
| `ok` / `error`  | reply to the browser that sent a command                     |

### Commands from a browser

`{"cmd": ...}` with: `run_scan`, `lock` (candidate index), `release`,
`set_output`, `apply_settings`, `engage` (on/off; on also releases the PID),
`autolock_now` ("Run Autolock Once"), `test_sound`,
`set_name`. The service checks every command itself (valid values, nothing
else sweeping control out), so two browsers cannot start conflicting actions.

## Not done yet

- **No login.** Anyone who can reach the Pi on the network can control the
  lockbox. Fine on a closed lab network; needs a password or token before
  going anywhere else.
- **Start at boot.** Needs a systemd unit so the service starts with the Pi
  and restarts if it crashes.
- **Saved autolock images** aren't shown in the page.
- **Plots are basic**: no zoom or pan. `plot.js` could be swapped for a
  plotting library.
- **Who did what.** Log lines for manual actions show the browser's address,
  but there are no user names.
