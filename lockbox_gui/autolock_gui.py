"""
Autolock control GUI - entry point.

Run on the Raspberry Pi, over an X11-forwarded SSH session so the window
appears on your own computer while the DAC/ADC hardware is driven from the Pi:

    ssh -X pi@<address>
    cd <project folder>
    uv run python3 autolock_gui.py

Install dependencies once per Pi with `uv sync` (see pyproject.toml and
uv.lock). If Tkinter is missing: `sudo apt install python3-tk`.

Project layout (all the real code lives in the autolock_app package):

    autolock_gui.py              this file - startup only
    autolock_app/
        config.py                constants, the Settings dataclass, save/load of settings
        state.py                 cross-thread events and the worker->GUI message queue
        channels.py              ADC-to-physical-volts conversions, channel names
        hardware.py              I2C / MCP4728 DAC / ADS1115 ADC access, B/C/D PID lines
        telemetry.py             MQTT publishing (the InfluxDB/Grafana logging path)
        signal_processing.py     smoothing, zero crossings, safe-range segments
        scanning.py              sweeps of control out (uniform and adaptive spacing)
        plotting.py              matplotlib setup (colors, range bars), saved autolock PNGs
        locking.py               the two automatic autolock procedures
        workers.py               background threads: monitor loop, manual scan
        headless.py              running with no GUI, and the GUI <-> headless hand-off
        gui/
            main_window.py       window, hostname/name header, worker->GUI message handling
            scan_tab.py          "Scan & Lock" tab
            settings_tab.py      "Settings" tab
            monitor_panel.py     "Live Monitor" tab

Settings are saved per machine in ~/.config/autolock_gui/settings.json
(override the location with the AUTOLOCK_SETTINGS_PATH environment variable).

Headless operation: tick "Keep running after GUI closes" in the window header
and monitoring, logging and autolock carry on with no window after the GUI
closes or the SSH session drops (see autolock_app/headless.py). To run with
no GUI at all:

    uv run python3 autolock_gui.py --headless [--engage]
"""

import argparse


def parse_args():
    parser = argparse.ArgumentParser(description="Autolock control GUI")
    parser.add_argument("--headless", action="store_true",
                        help="run monitoring, logging and autolock with no GUI")
    parser.add_argument("--engage", action="store_true",
                        help="with --headless: engage autolock even if it wasn't engaged when last saved")
    parser.add_argument("--wait-for-pid", type=int, default=None, metavar="PID",
                        help="with --headless: start only once this process (the GUI) has ended; "
                             "used by the GUI's own hand-off")
    return parser.parse_args()


def main():
    args = parse_args()
    from autolock_app import headless

    if args.headless:
        headless.run(wait_for_pid=args.wait_for_pid, engage=args.engage)
        return

    from autolock_app import config, hardware, state, telemetry
    from autolock_app.gui.main_window import MainApp

    # Restore this machine's saved settings before any widget reads them.
    state.log(config.load_settings())

    # Only one program may drive the hardware: stop a headless process left
    # running by a previous GUI session before touching the DAC/ADC.
    took_over = headless.stop_running()
    if took_over:
        state.log("Stopped the headless process and took over from it.")

    # Connect to the outside world before the GUI starts its monitor thread.
    telemetry.init_mqtt()
    hardware.init_hardware()

    app = MainApp(took_over_from_headless=took_over)
    app.mainloop()


if __name__ == "__main__":
    main()
