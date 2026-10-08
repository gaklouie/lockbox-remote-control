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
        plotting.py              matplotlib setup (backend, colors), saved autolock PNGs
        locking.py               the two automatic autolock procedures
        workers.py               background threads: monitor loop, manual scan
        gui/
            main_window.py       window, hostname/name header, worker->GUI message handling
            scan_tab.py          "Scan & Lock" tab
            settings_tab.py      "Settings" tab
            monitor_panel.py     "Live Monitor" tab

Settings are saved per machine in ~/.config/autolock_gui/settings.json
(override the location with the AUTOLOCK_SETTINGS_PATH environment variable).
"""


def main():
    # Import order matters: plotting sets the matplotlib backend and color
    # cycle, so it must be imported before anything else touches pyplot.
    from autolock_app import plotting  # noqa: F401
    from autolock_app import config, hardware, state, telemetry
    from autolock_app.gui.main_window import MainApp

    # Restore this machine's saved settings before any widget reads them.
    state.log(config.load_settings())

    # Connect to the outside world before the GUI starts its monitor thread.
    telemetry.init_mqtt()
    hardware.init_hardware()

    app = MainApp()
    app.mainloop()


if __name__ == "__main__":
    main()
