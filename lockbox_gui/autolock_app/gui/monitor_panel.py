"""The "Live Monitor" tab: Outputs / Errors strip-charts plus a scrolling log."""

import time
import tkinter as tk
from collections import deque
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ..channels import CHANNEL_CONVERTERS, ERROR_SIGNAL_NAMES, OUTPUT_SIGNAL_NAMES
from ..config import settings
from ..plotting import SIGNAL_COLORS, draw_safe_range_bars, place_legend_outside


class LiveMonitorPanel(ttk.Frame):
    """Full-size, separate Outputs / Errors strip-charts + a shared scrolling log."""

    MAX_POINTS = 20000          # memory safety net; normally LIVE_MONITOR_TIME_SPAN prunes first
    MAX_LOG_LINES = 500
    MIN_REDRAW_INTERVAL = 0.2   # seconds; caps the actual canvas redraw rate at ~5 Hz,
                                # independent of how fast readings are arriving

    def __init__(self, parent):
        super().__init__(parent)

        self.times = deque(maxlen=self.MAX_POINTS)
        self.series = {name: deque(maxlen=self.MAX_POINTS) for name in CHANNEL_CONVERTERS}
        self.t0 = time.time()

        # Whether this tab is the one currently selected in the Notebook.
        # MainApp updates this on tab-change; redraw_plots() skips the
        # (expensive) matplotlib work entirely while it's False, since
        # FigureCanvasTkAgg still does full CPU-bound rasterization on
        # draw_idle() even for a hidden/unmapped tab - there's no point
        # paying that cost for a plot nobody can see.
        self.is_visible = False
        self._last_redraw_time = 0.0

        plots_frame = ttk.Frame(self)
        plots_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        out_frame = ttk.LabelFrame(plots_frame, text="Outputs")
        out_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        fig_out = Figure(figsize=(7, 5))
        self.ax_out = fig_out.add_subplot(111)
        self.lines_out = {}
        for name in OUTPUT_SIGNAL_NAMES:
            (line,) = self.ax_out.plot([], [], color=SIGNAL_COLORS[name], label=name)
            self.lines_out[name] = line
        self.ax_out.set_xlabel("time (s)")
        self.ax_out.set_ylabel("volts")
        self.ax_out.grid(True, alpha=0.3)
        fig_out.subplots_adjust(right=0.69)
        self.canvas_out = FigureCanvasTkAgg(fig_out, master=out_frame)
        self.canvas_out.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        err_frame = ttk.LabelFrame(plots_frame, text="Errors")
        err_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        fig_err = Figure(figsize=(7, 5))
        self.ax_err = fig_err.add_subplot(111)
        self.lines_err = {}
        for name in ERROR_SIGNAL_NAMES:
            (line,) = self.ax_err.plot([], [], color=SIGNAL_COLORS[name], label=name)
            self.lines_err[name] = line
        self.ax_err.set_xlabel("time (s)")
        self.ax_err.set_ylabel("volts")
        self.ax_err.grid(True, alpha=0.3)
        fig_err.subplots_adjust(right=0.69)
        self.canvas_err = FigureCanvasTkAgg(fig_err, master=err_frame)
        self.canvas_err.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.out_bars = []
        self.err_bars = []
        self.refresh_safe_bands()

        log_frame = ttk.Frame(self)
        log_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="none")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh_safe_bands(self):
        """(Re)draw the safe-range bars beside each plot from the current settings."""
        for bar in self.out_bars + self.err_bars:
            bar.remove()

        self.out_bars, handles = draw_safe_range_bars(self.ax_out, [
            ("slow_output", settings.SLOW_OUTPUT_SAFE_MIN, settings.SLOW_OUTPUT_SAFE_MAX),
            ("fast_output", settings.FAST_OUTPUT_SAFE_MIN, settings.FAST_OUTPUT_SAFE_MAX),
        ])
        place_legend_outside(self.ax_out, extra_handles=handles, n_bars=len(self.out_bars))

        self.err_bars, handles = draw_safe_range_bars(self.ax_err, [
            ("dc_err", settings.DC_ERR_SAFE_MIN, settings.DC_ERR_SAFE_MAX),
        ])
        place_legend_outside(self.ax_err, extra_handles=handles, n_bars=len(self.err_bars))

        self.canvas_out.draw_idle()
        self.canvas_err.draw_idle()

    def _prune_to_time_span(self):
        """Drop readings older than LIVE_MONITOR_TIME_SPAN before the newest one."""
        if not self.times:
            return
        cutoff = self.times[-1] - settings.LIVE_MONITOR_TIME_SPAN
        while self.times and self.times[0] < cutoff:
            self.times.popleft()
            for s in self.series.values():
                s.popleft()

    def apply_time_span(self):
        """Re-trim to a (possibly shorter) LIVE_MONITOR_TIME_SPAN and redraw right away."""
        self._prune_to_time_span()
        if self.is_visible:
            self.redraw_plots(force=True)

    def record_reading(self, timestamp, values, log_buffer=None):
        """
        Cheap, data-only update: append to the history deques (and
        optionally a log-line buffer). Does NOT touch the canvases or the
        log widget - call redraw_plots() / log_many() separately, once per
        batch, rather than once per reading. This is what keeps a fast
        stream of readings (e.g. during a scan) from forcing a full
        matplotlib redraw on every single point.
        """
        self.times.append(timestamp - self.t0)
        for name in self.series:
            self.series[name].append(values[name])
        self._prune_to_time_span()
        if log_buffer is not None:
            line_str = " ".join(f"{k}={v:.4f}" for k, v in values.items())
            log_buffer.append(f"[{time.strftime('%H:%M:%S')}] {line_str}")

    def redraw_plots(self, force=False):
        """
        Push the current deque contents onto the lines and redraw - unless
        this tab isn't currently visible, or we redrew too recently, in
        which case skip entirely (data is still safely accumulating in the
        deques via record_reading(); nothing is lost, we just don't pay
        for a redraw nobody benefits from). Pass force=True to redraw
        unconditionally, e.g. right after this tab becomes visible, so the
        view catches up immediately rather than waiting for the next tick.
        """
        if not force:
            if not self.is_visible:
                return
            now = time.time()
            if now - self._last_redraw_time < self.MIN_REDRAW_INTERVAL:
                return
        self._last_redraw_time = time.time()

        for name, line in self.lines_out.items():
            line.set_data(self.times, self.series[name])
        for name, line in self.lines_err.items():
            line.set_data(self.times, self.series[name])

        if self.times:
            xmax = self.times[-1]
            xmin = max(0.0, xmax - settings.LIVE_MONITOR_TIME_SPAN)
            self.ax_out.set_xlim(xmin, max(xmax, xmin + 1))
            self.ax_err.set_xlim(xmin, max(xmax, xmin + 1))

            m = 0.5
            out_vals = [v for n in OUTPUT_SIGNAL_NAMES for v in self.series[n]]
            self.ax_out.set_ylim(min(out_vals) - m, max(out_vals) + m)
            err_vals = [v for n in ERROR_SIGNAL_NAMES for v in self.series[n]]
            self.ax_err.set_ylim(min(err_vals) - m, max(err_vals) + m)

        self.canvas_out.draw_idle()
        self.canvas_err.draw_idle()

    def log(self, message):
        self.log_many([message])

    def log_many(self, messages):
        """Insert several log lines in one Text widget operation, and do
        the overflow-trim/scroll-to-end just once, instead of per line."""
        if not messages:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, "\n".join(messages) + "\n")
        num_lines = int(self.log_text.index("end-1c").split(".")[0])
        if num_lines > self.MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{num_lines - self.MAX_LOG_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")
