"""
matplotlib setup (backend, color cycle) and the PNGs saved by the automatic autolock.

Import this module before anything else imports matplotlib.pyplot, so the
backend and color cycle are in place for every figure.
"""

import os

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.artist import Artist
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.transforms import ScaledTranslation

from .config import settings

# --------------------------------------------------------------------------
# Plot color cycle (applies to every figure created after this point)
# --------------------------------------------------------------------------
COLOR_CYCLE = ['#882255', '#0F7D33', '#332288', '#DDCC77',
               '#C7112F', '#4AAF9E', '#AA4499', '#C1DA49']
matplotlib.rcParams['axes.prop_cycle'] = matplotlib.cycler(color=COLOR_CYCLE)

# One fixed color per signal, used on every plot, so a signal's line and its
# safe-range bar always match.
SIGNAL_COLORS = {
    "slow_output": COLOR_CYCLE[0],
    "fast_output": COLOR_CYCLE[1],
    "error": COLOR_CYCLE[2],
    "dc_err": COLOR_CYCLE[4],
}

# Safe-range bar geometry, in points, measured from the right edge of the axes.
BAR_GAP = 4.0       # space between the axes and the first bar
BAR_WIDTH = 6.0
BAR_SPACING = 9.0   # center-to-center distance between neighbouring bars
BAR_ALPHA = 0.6


def _right_of_axes(ax, points):
    """Axes-coordinate transform shifted right by `points`."""
    return ax.transAxes + ScaledTranslation(points / 72.0, 0, ax.figure.dpi_scale_trans)


def place_legend_outside(ax, fontsize=8, extra_handles=(), n_bars=0):
    """
    Put a legend to the right of the axes (and of any safe-range bars)
    instead of overlapping the data. extra_handles are added to the
    legend after the axes' own labelled artists.
    """
    handles, labels = ax.get_legend_handles_labels()
    handles += list(extra_handles)
    labels += [h.get_label() for h in extra_handles]
    offset = BAR_GAP + n_bars * BAR_SPACING + 4.0 if n_bars else 8.0
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(1.0, 1.0),
              bbox_transform=_right_of_axes(ax, offset), fontsize=fontsize, borderaxespad=0.0)


class SafeRangeBar(Artist):
    """
    A vertical bar just outside the right edge of `ax`, spanning lo..hi in
    data units of the y axis. Reads the current y-limits when drawn, so it
    never affects autoscaling. A range partly off-screen is cut at the axes
    edge; a range entirely off-screen shows as an arrow at the top or bottom
    edge pointing toward it.
    """

    def __init__(self, ax, lo, hi, color, index):
        super().__init__()
        self._ax = ax
        self._lo, self._hi = sorted((lo, hi))
        transform = _right_of_axes(ax, BAR_GAP + BAR_WIDTH / 2 + index * BAR_SPACING)
        self._bar = Line2D([1, 1], [0, 1], transform=transform, color=color,
                           linewidth=BAR_WIDTH, solid_capstyle="butt", alpha=BAR_ALPHA)
        self._arrow = Line2D([1], [0], transform=transform, color=color, marker="^",
                             markersize=7, linestyle="none")
        for child in (self._bar, self._arrow):
            child.set_figure(ax.figure)
        self.set_zorder(3)

    def draw(self, renderer):
        if not self.get_visible():
            return
        y0, y1 = self._ax.get_ylim()
        lo = (self._lo - y0) / (y1 - y0)
        hi = (self._hi - y0) / (y1 - y0)
        if hi < 0:
            self._arrow.set_data([1], [0])
            self._arrow.set_marker("v")
            self._arrow.draw(renderer)
        elif lo > 1:
            self._arrow.set_data([1], [1])
            self._arrow.set_marker("^")
            self._arrow.draw(renderer)
        else:
            self._bar.set_data([1, 1], [max(lo, 0.0), min(hi, 1.0)])
            self._bar.draw(renderer)
        self.stale = False


def draw_safe_range_bars(ax, ranges):
    """
    Draw one SafeRangeBar per (signal_name, lo, hi) in `ranges`, colored by
    SIGNAL_COLORS. Returns (bars, legend_handles); remove the bars with
    bar.remove() and pass the handles to place_legend_outside().
    """
    bars, handles = [], []
    for i, (name, lo, hi) in enumerate(ranges):
        color = SIGNAL_COLORS[name]
        bars.append(ax.add_artist(SafeRangeBar(ax, lo, hi, color, i)))
        handles.append(Line2D([], [], color=color, linewidth=BAR_WIDTH, solid_capstyle="butt",
                              alpha=BAR_ALPHA, label=f"{name} safe range"))
    return bars, handles


# --------------------------------------------------------------------------
# Scan-plot saving (used by the automatic autolock)
# --------------------------------------------------------------------------
def _plot_autolock_pass(ax, data, title):
    name = data["signal_name"]
    color = SIGNAL_COLORS[name]
    ax.plot(data["voltages"], data["raw"], ".", color=color, alpha=0.4, label=f"raw {name}")
    ax.plot(data["voltages"], data["smoothed"], "-", color="black", linewidth=1.0, label=f"smoothed {name}")

    bar_handles = []
    if data["mode"] == "zero_crossing":
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        for v, _ in data["crossings"]:
            ax.axvline(v, color="gray", linestyle=":", alpha=0.6)
    else:
        _, bar_handles = draw_safe_range_bars(ax, [(name, data["safe_min"], data["safe_max"])])
        for start, end, _, _ in data["segments"]:
            ax.axvspan(start, end, color="gray", alpha=0.15)

    if data["chosen"] is not None:
        chosen_v = data["chosen"][0]
        y_at_chosen = 0 if data["mode"] == "zero_crossing" else (data["safe_min"] + data["safe_max"]) / 2
        ax.plot(chosen_v, y_at_chosen, "r*", markersize=16, label=f"chosen ({chosen_v:.4f} V)")

    ax.set_xlabel("control out, physical (V)")
    ax.set_ylabel(f"{data['signal_name']} (V)")
    ax.set_title(title)
    place_legend_outside(ax, extra_handles=bar_handles, n_bars=len(bar_handles))
    ax.grid(True, alpha=0.3)


def save_autolock_scan_plot(session_id, coarse_data, fine_data):
    """
    Save coarse + fine passes side by side as <SCAN_PLOT_DIR>/<session_id>_scan.png.

    Runs on a worker thread, so it builds a bare Figure rather than going
    through pyplot: pyplot would create a Tk-backed figure manager, and Tk
    must only be touched from the GUI thread.
    """
    if not settings.SAVE_SCAN_PLOTS:
        return
    os.makedirs(settings.SCAN_PLOT_DIR, exist_ok=True)
    fig = Figure(figsize=(15, 5))
    axes = fig.subplots(1, 2)
    _plot_autolock_pass(axes[0], coarse_data, "Coarse scan")
    if fine_data is not None:
        _plot_autolock_pass(axes[1], fine_data, "Fine scan")
    else:
        axes[1].text(0.5, 0.5, "No lock candidate found -\nfine pass skipped",
                     ha="center", va="center", transform=axes[1].transAxes)
        axes[1].set_title("Fine scan")
    fig.suptitle(f"Autolock scan ({coarse_data['mode']}) - {session_id}")
    fig.tight_layout()
    fig.subplots_adjust(right=0.83)
    filename = os.path.join(settings.SCAN_PLOT_DIR, f"{session_id}_scan.png")
    fig.savefig(filename, dpi=150)
