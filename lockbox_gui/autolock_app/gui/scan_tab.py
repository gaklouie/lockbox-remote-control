"""The "Scan & Lock" tab: manual scan, lock candidates, lock/release, constant output."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from .. import hardware, state
from ..channels import ERROR_SIGNAL_NAMES, OUTPUT_SIGNAL_NAMES
from ..config import coerce_value, settings
from ..plotting import SIGNAL_COLORS, draw_safe_range_bars, place_legend_outside
from ..workers import run_manual_scan


class ScanTab(ttk.Frame):
    """Manual scan, mode selection, candidate selection, and lock controls."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.current_voltages = None
        self.current_trace = None
        self.current_candidates = []

        controls = ttk.Frame(self)
        controls.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        ttk.Label(controls, text="Scan min (physical V)").grid(row=0, column=0, sticky="w")
        self.min_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.min_var, width=8).grid(row=0, column=1)

        ttk.Label(controls, text="Scan max (physical V)").grid(row=0, column=2, sticky="w")
        self.max_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.max_var, width=8).grid(row=0, column=3)

        ttk.Label(controls, text="Points").grid(row=0, column=4, sticky="w")
        self.points_var = tk.StringVar()
        self.points_entry = ttk.Entry(controls, textvariable=self.points_var, width=8)
        self.points_entry.grid(row=0, column=5)

        self.adaptive_var = tk.BooleanVar()
        ttk.Checkbutton(controls, text="Adaptive spacing", variable=self.adaptive_var,
                        command=self._update_points_entry).grid(row=0, column=6, sticky="w", padx=(12, 0))

        ttk.Label(controls, text="Lock mode:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mode_var = tk.StringVar(value=settings.AUTOLOCK_MODE)
        ttk.Radiobutton(controls, text="Search for crossings in error", value="zero_crossing",
                        variable=self.mode_var).grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Radiobutton(controls, text="Search for safe range in dc_err", value="dc_err_range",
                        variable=self.mode_var).grid(row=1, column=3, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(controls, text="Crossing sign:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.sign_var = tk.StringVar(value=settings.CROSSING_SIGN)
        ttk.Radiobutton(controls, text="Positive", value="positive",
                        variable=self.sign_var).grid(row=2, column=1, sticky="w", pady=(4, 0))
        ttk.Radiobutton(controls, text="Negative", value="negative",
                        variable=self.sign_var).grid(row=2, column=2, sticky="w", pady=(4, 0))
        ttk.Radiobutton(controls, text="Both", value="both",
                        variable=self.sign_var).grid(row=2, column=3, sticky="w", pady=(4, 0))

        self.scan_button = ttk.Button(controls, text="Run Scan", command=self.on_run_scan)
        self.scan_button.grid(row=1, column=5, padx=(12, 0), pady=(6, 0))

        self.progress = ttk.Progressbar(controls, orient="horizontal", length=160, mode="determinate",
                                        maximum=1.0)
        self.progress.grid(row=1, column=6, padx=(8, 0), pady=(6, 0))

        # Constant output, directly under Run Scan: hold control out at a fixed
        # physical voltage instead of scanning/locking.
        ttk.Button(controls, text="Set Output", command=self.on_set_constant).grid(
            row=2, column=5, padx=(12, 0), pady=(4, 0), sticky="ew")
        constant_frame = ttk.Frame(controls)
        constant_frame.grid(row=2, column=6, padx=(8, 0), pady=(4, 0), sticky="w")
        self.constant_var = tk.StringVar()
        ttk.Entry(constant_frame, textvariable=self.constant_var, width=8).pack(side=tk.LEFT)
        ttk.Label(constant_frame, text="V physical (control out only)").pack(side=tk.LEFT, padx=(4, 0))

        fig = Figure(figsize=(10, 6))
        self.ax_err = fig.add_subplot(211)
        self.ax_out = fig.add_subplot(212)
        self.fig = fig

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = FigureCanvasTkAgg(fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, canvas_frame)
        toolbar.update()

        bottom = ttk.Frame(self)
        bottom.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        ttk.Label(bottom, text="Lock candidates:").pack(side=tk.LEFT)
        self.candidate_list = tk.Listbox(bottom, height=4, width=60)
        self.candidate_list.pack(side=tk.LEFT, padx=(6, 6))

        ttk.Button(bottom, text="Lock to Selected", command=self.on_lock_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(bottom, text="Release (restart PID)", command=self.on_release).pack(side=tk.LEFT, padx=4)

        self.refresh_from_settings()

    def refresh_from_settings(self):
        """Show the current settings values in this tab's entries."""
        self.min_var.set(str(settings.SCAN_MIN_VOLTAGE))
        self.max_var.set(str(settings.SCAN_MAX_VOLTAGE))
        self.points_var.set(str(settings.MANUAL_SCAN_POINTS))
        self.adaptive_var.set(settings.ADAPTIVE_SCAN)
        self._update_points_entry()
        self.constant_var.set(str(settings.CONSTANT_OUTPUT_VOLTAGE))

    def _update_points_entry(self):
        """The point count only applies to uniform scans; grey it out for adaptive ones."""
        self.points_entry.configure(state="disabled" if self.adaptive_var.get() else "normal")

    def on_run_scan(self):
        reason = state.busy_reason()
        if reason:
            messagebox.showwarning("Busy", reason)
            return
        try:
            v_min = coerce_value("SCAN_MIN_VOLTAGE", self.min_var.get())
            v_max = coerce_value("SCAN_MAX_VOLTAGE", self.max_var.get())
            num_points = coerce_value("MANUAL_SCAN_POINTS", self.points_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Scan min/max/points: {exc}")
            return

        # The scan range is shared with the autolock settings; keep both tabs
        # and the saved file in sync with what was just entered here.
        settings.SCAN_MIN_VOLTAGE = v_min
        settings.SCAN_MAX_VOLTAGE = v_max
        settings.MANUAL_SCAN_POINTS = num_points
        settings.ADAPTIVE_SCAN = self.adaptive_var.get()
        self.app.settings_tab.refresh_from_settings()
        self.app.save_settings()

        self.candidate_list.delete(0, tk.END)
        self.progress.configure(value=0)
        self.scan_button.configure(state="disabled")

        threading.Thread(
            target=run_manual_scan,
            args=(v_min, v_max, num_points, self.mode_var.get(), self.sign_var.get(),
                  settings.ADAPTIVE_SCAN),
            daemon=True,
        ).start()

    def on_scan_progress(self, fraction):
        self.progress.configure(value=fraction)

    def on_scan_done(self, data):
        """Plot a finished scan. `data` is None if the scan failed."""
        self.scan_button.configure(state="normal")
        if data is None:
            return
        self.current_voltages = data["voltages"]
        self.current_trace = data["trace"]
        self.current_candidates = data["candidates"]
        mode = data["mode"]

        self.ax_err.clear()
        self.ax_out.clear()

        # Small markers show where the (possibly adaptive) scan actually sampled.
        for name in ERROR_SIGNAL_NAMES:
            self.ax_err.plot(data["voltages"], data["trace"][name], ".-", markersize=3,
                             color=SIGNAL_COLORS[name], label=name, alpha=0.6)
        self.ax_err.plot(data["voltages"], data["signal_smoothed"], "k-",
                         label=f"{'error' if mode == 'zero_crossing' else 'dc_err'} (smoothed)")

        if mode == "zero_crossing":
            self.ax_err.axhline(0, color="black", linewidth=0.8, linestyle="--")
            marker_y = 0
        else:
            marker_y = (settings.DC_ERR_SAFE_MIN + settings.DC_ERR_SAFE_MAX) / 2

        # Visually indicate every candidate: shading/lines, plus a numbered marker
        # matching the candidate list order below.
        for idx, c in enumerate(self.current_candidates):
            if mode == "zero_crossing":
                self.ax_err.axvline(c["lock_voltage"], color="gray", linestyle=":", alpha=0.6)
            else:
                self.ax_err.axvspan(c["start"], c["end"], color="gray", alpha=0.2)
            self.ax_err.plot(c["lock_voltage"], marker_y, "o", color="black", markersize=6)
            self.ax_err.annotate(str(idx + 1), (c["lock_voltage"], marker_y),
                                 textcoords="offset points", xytext=(0, 8),
                                 ha="center", fontsize=8, fontweight="bold")

        self.ax_err.set_xlabel("control out, physical (V)")
        self.ax_err.set_ylabel("error signals (V)")
        bars, handles = draw_safe_range_bars(self.ax_err, [
            ("dc_err", settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX),
        ])
        place_legend_outside(self.ax_err, extra_handles=handles, n_bars=len(bars))
        self.ax_err.grid(True, alpha=0.3)

        for name in OUTPUT_SIGNAL_NAMES:
            self.ax_out.plot(data["voltages"], data["trace"][name], ".-", markersize=3,
                             color=SIGNAL_COLORS[name], label=name)
        self.ax_out.set_xlabel("control out, physical (V)")
        self.ax_out.set_ylabel("outputs (V)")
        bars, handles = draw_safe_range_bars(self.ax_out, [
            ("slow_output", settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX),
            ("fast_output", settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX),
        ])
        place_legend_outside(self.ax_out, extra_handles=handles, n_bars=len(bars))
        self.ax_out.grid(True, alpha=0.3)

        self.fig.tight_layout()
        self.fig.subplots_adjust(right=0.75)
        self.canvas.draw_idle()

        for idx, c in enumerate(self.current_candidates):
            self.candidate_list.insert(tk.END, f"{idx + 1}. {c['label']}")

    def on_lock_selected(self):
        reason = state.busy_reason()
        if reason:
            messagebox.showwarning("Busy", reason)
            return
        sel = self.candidate_list.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Select a candidate from the list first.")
            return
        v = self.current_candidates[sel[0]]["lock_voltage"]
        hardware.set_control_out(v)
        hardware.restart_pid()
        self.app.monitor_panel.log(f"[manual] Locked to control out = {v:.4f} V physical")

    def on_release(self):
        hardware.restart_pid()
        self.app.monitor_panel.log("[manual] PID restarted (B, C, D set LOW) without changing control out.")

    def on_set_constant(self):
        """Hold control out at a fixed voltage. Release only drives B/C/D, so this value survives it."""
        reason = state.busy_reason()
        if reason:
            messagebox.showwarning("Busy", reason)
            return
        try:
            v = coerce_value("CONSTANT_OUTPUT_VOLTAGE", self.constant_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Constant output: {exc}")
            return
        settings.CONSTANT_OUTPUT_VOLTAGE = v
        self.app.save_settings()
        hardware.set_control_out(v)
        self.app.monitor_panel.log(
            f"[manual] Control out set to constant {v:.4f} V physical "
            f"(B/C/D unchanged; Release won't change this - a scan or autolock will).")
