"""Main window: hostname/device-name header, the three tabs, and worker->GUI message handling."""

import os
import queue
import socket
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

from ttkthemes import ThemedTk

from .. import headless, state, telemetry
from ..config import save_settings, settings
from ..workers import monitor_loop
from .monitor_panel import LiveMonitorPanel
from .scan_tab import ScanTab
from .settings_tab import SettingsTab

ICON_PATH = Path(__file__).resolve().parents[2] / "icon.png"  # next to autolock_gui.py


class MainApp(ThemedTk):
    def __init__(self, took_over_from_headless=False):
        super().__init__(theme="clearlooks")
        self.hostname = socket.gethostname()
        self.bold_font = tkfont.nametofont("TkDefaultFont", root=self).copy()
        self.bold_font.configure(weight="bold")
        self.geometry("1150x880")

        if ICON_PATH.exists():
            self.app_icon = tk.PhotoImage(file=str(ICON_PATH))
            self.iconphoto(True, self.app_icon)

        # --- Header: which Pi this is, and a user-editable label for the hardware it drives ---
        header = ttk.Frame(self)
        header.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(6, 2))
        ttk.Label(header, text="Host:").pack(side=tk.LEFT)
        ttk.Label(header, text=self.hostname, font=self.bold_font).pack(side=tk.LEFT, padx=(4, 20))
        ttk.Label(header, text="Lockbox name:").pack(side=tk.LEFT)
        self.name_var = tk.StringVar(value=settings.DEVICE_NAME)
        name_entry = ttk.Entry(header, textvariable=self.name_var, width=30)
        name_entry.pack(side=tk.LEFT, padx=4)
        name_entry.bind("<Return>", lambda _event: self.on_save_name())
        ttk.Button(header, text="Save Name", command=self.on_save_name).pack(side=tk.LEFT, padx=4)
        self.headless_var = tk.BooleanVar(value=settings.HEADLESS_ON_CLOSE)
        ttk.Checkbutton(header, text="Keep running after GUI closes (headless)",
                        variable=self.headless_var, command=self.on_toggle_headless).pack(side=tk.RIGHT, padx=4)
        self._update_title()

        notebook = ttk.Notebook(self)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # The monitor panel is created first because the other tabs log to it.
        self.monitor_panel = LiveMonitorPanel(notebook)
        self.scan_tab = ScanTab(notebook, self)
        self.settings_tab = SettingsTab(notebook, self)
        notebook.add(self.scan_tab, text="Scan & Lock")
        notebook.add(self.settings_tab, text="Settings")
        notebook.add(self.monitor_panel, text="Live Monitor")
        self.notebook = notebook
        notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_queue)

        # Autolock normally starts disengaged. The exception: this GUI just took
        # over from a headless process that had it engaged - keep it engaged.
        if took_over_from_headless and settings.AUTOLOCK_ENGAGED:
            self.settings_tab.set_engaged(True)
        elif settings.AUTOLOCK_ENGAGED:
            settings.AUTOLOCK_ENGAGED = False
            self.save_settings()

        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()

        self._sync_headless_standby()

    def _update_title(self):
        name = settings.DEVICE_NAME.strip()
        label = f"{name} ({self.hostname})" if name else self.hostname
        self.title(f"Autolock Control - {label}")

    def on_save_name(self):
        settings.DEVICE_NAME = self.name_var.get().strip()
        self.name_var.set(settings.DEVICE_NAME)
        self._update_title()
        self.save_settings()
        self.monitor_panel.log(f"Lockbox name set to {settings.DEVICE_NAME!r}.")

    def on_toggle_headless(self):
        settings.HEADLESS_ON_CLOSE = self.headless_var.get()
        self.save_settings()
        self._sync_headless_standby()

    def _sync_headless_standby(self):
        """
        Start or stop the standby process to match HEADLESS_ON_CLOSE. The
        standby waits for this GUI to end - however it ends - and then keeps
        monitoring, logging and autolock going (see headless.py).
        """
        if settings.HEADLESS_ON_CLOSE:
            if headless.spawn_standby(os.getpid()):
                self.monitor_panel.log(
                    "Headless standby ready: monitoring, logging and autolock will continue "
                    f"after this window closes. Its log: {headless.LOG_PATH}")
            else:
                self.monitor_panel.log(
                    f"WARNING: the headless standby did not start - see {headless.LOG_PATH}")
        elif headless.stop_running():
            self.monitor_panel.log("Headless standby stopped: everything stops when this window closes.")

    def save_settings(self):
        """Save settings to this machine's settings file, logging (not raising) on failure."""
        try:
            save_settings()
        except OSError as exc:
            self.monitor_panel.log(f"WARNING: could not save settings: {exc}")

    def on_tab_changed(self, event):
        selected_widget = self.nametowidget(self.notebook.select())
        is_monitor = selected_widget is self.monitor_panel
        self.monitor_panel.is_visible = is_monitor
        if is_monitor:
            # Catch the view up immediately rather than waiting for the
            # next reading to trickle in and trigger a redraw.
            self.monitor_panel.redraw_plots(force=True)

    def poll_queue(self):
        """
        Drain everything currently queued, but only touch the expensive
        stuff (matplotlib redraws, progress bar, log widget) ONCE per
        call - regardless of how many readings arrived this tick. During a
        fast scan, dozens of "reading" messages can pile up between ticks;
        updating the plot/log for each one individually is what made the
        GUI feel slow, especially on Windows where Tk's canvas blitting is
        already the slower path. Batching keeps the redraw rate capped at
        roughly 1 / (after-delay) regardless of data rate.
        """
        had_reading = False
        log_lines = []
        latest_progress = None

        try:
            while True:
                item = state.gui_queue.get_nowait()
                kind = item[0]
                if kind == "reading":
                    _, timestamp, values = item
                    self.monitor_panel.record_reading(timestamp, values, log_lines)
                    had_reading = True
                elif kind == "log":
                    _, message = item
                    log_lines.append(message)
                elif kind == "scan_progress":
                    _, latest_progress = item
                elif kind == "scan_done":
                    _, data = item
                    self.scan_tab.on_scan_done(data)
        except queue.Empty:
            pass

        if had_reading:
            self.monitor_panel.redraw_plots()
        if log_lines:
            self.monitor_panel.log_many(log_lines)
        if latest_progress is not None:
            self.scan_tab.on_scan_progress(latest_progress)

        self.after(100, self.poll_queue)

    def on_close(self):
        # Let go of the hardware before the window goes away: the headless
        # standby (if enabled) takes over as soon as this process has ended,
        # and two programs must never drive the DAC/ADC at once.
        state.stop_event.set()
        self.monitor_thread.join(timeout=10.0)
        telemetry.close_mqtt()
        if not settings.HEADLESS_ON_CLOSE:
            headless.stop_running()  # make sure no standby is left behind
        self.destroy()
