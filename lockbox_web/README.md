# Lockbox web service (prototype)

A side project for trying the "always-on service" design. The working GUI in
`../lockbox_gui` is not changed by anything here.

## The idea

In `lockbox_gui`, the program that draws the window is also the program that
reads the ADC, logs to MQTT and relocks. Close the window and it all stops
(unless its headless option is on), and only one person can use it at a time.

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

On the Pi, from this folder:

    uv sync
    uv run python server.py

Then browse to `http://<pi-address>:8000`. Options: `--host`, `--port`.

Stop the Tk GUI (and its headless process) first: only one program may drive
the hardware. The service refuses to start while the Tk GUI's headless
process is running.

It uses the same `.env` (MQTT broker login) and the same saved settings file
(`~/.config/autolock_gui/settings.json`) as the Tk GUI. If autolock was
engaged when the service last ran, it re-engages on start.

## How it is built

    server.py            the service: web server, shared state, commands
    static/index.html    the page
    static/app.js        page logic: WebSocket, tabs, forms
    static/plot.js       small dependency-free canvas line plot
    static/style.css

The lock logic is **not** copied. `server.py` imports the `autolock_app`
package from `../lockbox_gui` (config, hardware, telemetry, scanning,
locking, workers - everything except its Tk `gui/` modules), so a fix there
applies to both programs.

Flow of data:

    worker threads -> state.gui_queue -> pump thread -> Hub (asyncio)
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
`set_output`, `apply_settings`, `engage` (on/off), `autolock_now`,
`set_name`. The service checks every command itself (valid values, nothing
else sweeping control out), so two browsers cannot start conflicting actions.

## Not done yet

- **No login.** Anyone who can reach the Pi on the network can control the
  lockbox. Fine on a closed lab network; needs a password or token before
  going anywhere else.
- **Start at boot.** Needs a systemd unit so the service starts with the Pi
  and restarts if it crashes.
- **Hand-over with the Tk GUI.** The two programs don't negotiate; you stop
  one to run the other.
- **Saved autolock images** aren't shown in the page.
- **Plots are basic**: no zoom or pan. `plot.js` could be swapped for a
  plotting library.
- **Settings labels are duplicated** from the Tk GUI's Settings tab
  (`SETTINGS_GROUPS` in `server.py`). They should move to one shared place.
- **Who did what.** Log lines for manual actions show the browser's address,
  but there are no user names.
