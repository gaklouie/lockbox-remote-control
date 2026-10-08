"""The "Settings" tab: grouped settings, Apply (and save), Engage/Disengage, Run Now."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import state
from ..config import FIELD_TYPES, SETTINGS_PATH, coerce_value, settings
from ..locking import autolock_once


class SettingsTab(ttk.Frame):
    """Autolock settings, grouped, + engage/disengage control."""

    SAFE_RANGE_FIELDS = {
        "SLOW_OUTPUT_SAFE_MIN": "slow_output safe min (V)",
        "SLOW_OUTPUT_SAFE_MAX": "slow_output safe max (V)",
        "FAST_OUTPUT_SAFE_MIN": "fast_output safe min (V)",
        "FAST_OUTPUT_SAFE_MAX": "fast_output safe max (V)",
        "DC_ERR_SAFE_MIN": "dc_err safe min (V)",
        "DC_ERR_SAFE_MAX": "dc_err safe max (V)",
    }
    SCAN_FIELDS = {
        "SCAN_MIN_VOLTAGE": "Scan output min (physical V)",
        "SCAN_MAX_VOLTAGE": "Scan output max (physical V)",
        "ADAPTIVE_SCAN": "Adaptive point spacing",
        "SCAN_MAX_STEP": "Adaptive max step (physical V)",
        "SCAN_MIN_STEP": "Adaptive min step (physical V)",
        "ADAPTIVE_TARGET_CHANGE": "Adaptive target change/step (V)",
        "NUM_COARSE_POINTS": "Coarse pass points (uniform only)",
        "NUM_FINE_POINTS": "Fine pass points",
        "SCAN_SETTLE_TIME": "Settle time (s)",
        "NUM_SAMPLES_PER_POINT": "Samples averaged/point",
        "SMOOTHING_WINDOW": "Smoothing window (1 = off)",
    }
    GENERAL_FIELDS = {
        "MIN_CROSSING_SLOPE_FRACTION": "Min crossing slope (x steepest, 0-1)",
        "RESET_VOLTAGE_ON_FAILURE": "Reset voltage on failure (physical V)",
        "PRIME_SETTLE_TIME": "Prime (short caps) settle time (s)",
        "MONITOR_INTERVAL": "Monitor sample interval (s)",
        "LOG_INTERVAL": "Logging interval (s)",
        "LIVE_MONITOR_TIME_SPAN": "Live monitor time span (s)",
        "LOCK_RETRY_DELAY": "Retry delay on failure (s)",
        "SAVE_SCAN_PLOTS": "Save autolock scan plots",
        "SCAN_PLOT_DIR": "Scan plot directory",
    }

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.vars = {}

        groups_frame = ttk.Frame(self)
        groups_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        safe_frame = ttk.LabelFrame(groups_frame, text="Safe Range Parameters")
        safe_frame.grid(row=0, column=0, sticky="n", padx=(0, 8))
        self._build_field_group(safe_frame, self.SAFE_RANGE_FIELDS)

        scan_frame = ttk.LabelFrame(groups_frame, text="Scan Parameters")
        scan_frame.grid(row=0, column=1, sticky="n", padx=8)
        self._build_field_group(scan_frame, self.SCAN_FIELDS)

        general_frame = ttk.LabelFrame(groups_frame, text="General Program Parameters")
        general_frame.grid(row=0, column=2, sticky="n", padx=(8, 0))

        ttk.Label(general_frame, text="Autolock mode:").grid(row=0, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.mode_var = tk.StringVar()
        ttk.Radiobutton(general_frame, text="Zero crossing (error + output range)", value="zero_crossing",
                        variable=self.mode_var).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(general_frame, text="DC err safe range", value="dc_err_range",
                        variable=self.mode_var).grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Label(general_frame, text="Crossing sign:").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.sign_var = tk.StringVar()
        ttk.Radiobutton(general_frame, text="Positive", value="positive",
                        variable=self.sign_var).grid(row=4, column=0, sticky="w")
        ttk.Radiobutton(general_frame, text="Negative", value="negative",
                        variable=self.sign_var).grid(row=4, column=1, sticky="w")
        ttk.Radiobutton(general_frame, text="Both", value="both",
                        variable=self.sign_var).grid(row=5, column=0, sticky="w")

        self._build_field_group(general_frame, self.GENERAL_FIELDS, start_row=6)

        button_row = ttk.Frame(self)
        button_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Button(button_row, text="Apply & Save Settings", command=self.on_apply).pack(side=tk.LEFT, padx=4)

        self.engage_var = tk.StringVar(value="Engage Autolock")
        self.engage_button = ttk.Button(button_row, textvariable=self.engage_var, command=self.on_toggle_engage)
        self.engage_button.pack(side=tk.LEFT, padx=4)

        ttk.Button(button_row, text="Run Autolock Now", command=self.on_run_now).pack(side=tk.LEFT, padx=4)

        self.status_label = ttk.Label(self, text="Autolock: disengaged")
        self.status_label.pack(side=tk.TOP, anchor="w", padx=10)

        ttk.Label(self, text=f"Settings file: {SETTINGS_PATH}", foreground="gray").pack(
            side=tk.TOP, anchor="w", padx=10, pady=(6, 0))

        self.refresh_from_settings()

    def _build_field_group(self, frame, field_dict, start_row=0):
        for i, (field_name, label) in enumerate(field_dict.items()):
            row = start_row + i
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=4)
            if FIELD_TYPES[field_name] is bool:
                var = tk.BooleanVar()
                ttk.Checkbutton(frame, variable=var).grid(row=row, column=1, sticky="w", pady=2, padx=4)
            else:
                var = tk.StringVar()
                ttk.Entry(frame, textvariable=var, width=18).grid(row=row, column=1, sticky="w", pady=2, padx=4)
            self.vars[field_name] = var

    def refresh_from_settings(self):
        """Show the current settings values in this tab's widgets."""
        for field_name, var in self.vars.items():
            value = getattr(settings, field_name)
            var.set(value if isinstance(var, tk.BooleanVar) else str(value))
        self.mode_var.set(settings.AUTOLOCK_MODE)
        self.sign_var.set(settings.CROSSING_SIGN)

    def on_apply(self):
        """
        Validate every field, then apply them all and save to disk. If any
        field is invalid nothing is changed. Returns True on success.
        """
        try:
            new_values = {name: coerce_value(name, var.get()) for name, var in self.vars.items()}
            new_values["AUTOLOCK_MODE"] = coerce_value("AUTOLOCK_MODE", self.mode_var.get())
            new_values["CROSSING_SIGN"] = coerce_value("CROSSING_SIGN", self.sign_var.get())
            if new_values["SCAN_MIN_STEP"] > new_values["SCAN_MAX_STEP"]:
                raise ValueError("adaptive min step must not exceed max step")
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Could not parse a field: {exc}")
            return False

        for name, value in new_values.items():
            setattr(settings, name, value)
        self.app.save_settings()

        self.app.scan_tab.refresh_from_settings()
        self.app.monitor_panel.refresh_from_settings()
        self.app.monitor_panel.apply_time_span()
        self.app.monitor_panel.log(
            f"Settings applied and saved. Autolock mode = {settings.AUTOLOCK_MODE}, "
            f"crossing sign = {settings.CROSSING_SIGN}")
        return True

    def on_toggle_engage(self):
        if state.autolock_engaged.is_set():
            state.autolock_engaged.clear()
            self.engage_var.set("Engage Autolock")
            self.status_label.configure(text="Autolock: disengaged")
            self.app.monitor_panel.log("Autolock disengaged.")
            return

        reason = state.busy_reason()
        if reason:
            messagebox.showwarning("Busy", reason)
            return
        if not self.on_apply():
            return
        state.autolock_engaged.set()
        self.engage_var.set("Disengage Autolock")
        self.status_label.configure(text=f"Autolock: engaged ({settings.AUTOLOCK_MODE})")
        self.app.monitor_panel.log(f"Autolock engaged (mode={settings.AUTOLOCK_MODE}).")

    def on_run_now(self):
        reason = state.busy_reason()
        if reason:
            messagebox.showwarning("Busy", reason)
            return
        if not self.on_apply():
            return
        state.autolock_running.set()  # mark busy now, so a quick second click can't start another
        threading.Thread(target=autolock_once, daemon=True).start()
