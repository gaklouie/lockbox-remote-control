"""
matplotlib setup (backend, color cycle) and the PNGs saved by the automatic autolock.

Import this module before anything else imports matplotlib.pyplot, so the
backend and color cycle are in place for every figure.
"""

import os

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure

from .config import settings

# --------------------------------------------------------------------------
# Plot color cycle (applies to every figure created after this point)
# --------------------------------------------------------------------------
COLOR_CYCLE = ['#882255', '#0F7D33', '#332288', '#DDCC77',
               '#C7112F', '#4AAF9E', '#AA4499', '#C1DA49']
matplotlib.rcParams['axes.prop_cycle'] = matplotlib.cycler(color=COLOR_CYCLE)


def place_legend_outside(ax, fontsize=8):
    """Put a legend to the right of the axes instead of overlapping the data."""
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), fontsize=fontsize, borderaxespad=0.0)


# --------------------------------------------------------------------------
# Scan-plot saving (used by the automatic autolock)
# --------------------------------------------------------------------------
def _plot_autolock_pass(ax, data, title):
    ax.plot(data["voltages"], data["raw"], ".", alpha=0.4, label=f"raw {data['signal_name']}")
    ax.plot(data["voltages"], data["smoothed"], "-", label=f"smoothed {data['signal_name']}")

    if data["mode"] == "zero_crossing":
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        for v, _ in data["crossings"]:
            ax.axvline(v, color="gray", linestyle=":", alpha=0.6)
    else:
        ax.axhspan(data["safe_min"], data["safe_max"], color="tab:green", alpha=0.15, label="safe range")
        for start, end, _, _ in data["segments"]:
            ax.axvspan(start, end, color="gray", alpha=0.15)

    if data["chosen"] is not None:
        chosen_v = data["chosen"][0]
        y_at_chosen = 0 if data["mode"] == "zero_crossing" else (data["safe_min"] + data["safe_max"]) / 2
        ax.plot(chosen_v, y_at_chosen, "r*", markersize=16, label=f"chosen ({chosen_v:.4f} V)")

    ax.set_xlabel("control out, physical (V)")
    ax.set_ylabel(f"{data['signal_name']} (V)")
    ax.set_title(title)
    place_legend_outside(ax)
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
    fig.subplots_adjust(right=0.85)
    filename = os.path.join(settings.SCAN_PLOT_DIR, f"{session_id}_scan.png")
    fig.savefig(filename, dpi=150)
