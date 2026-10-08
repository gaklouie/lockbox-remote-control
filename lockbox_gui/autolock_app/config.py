"""
Constants, the Settings dataclass, and per-machine save/load of settings.

Settings are editable live from the GUI and saved as JSON on the machine
running the GUI (i.e. on each Pi), so every Pi keeps its own values between
runs. Default location: ~/.config/autolock_gui/settings.json, overridable
with the AUTOLOCK_SETTINGS_PATH environment variable.
"""

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# --------------------------------------------------------------------------
# Hardware constants
# --------------------------------------------------------------------------
VDD_VOLTAGE = 5.0   # Supply voltage feeding the MCP4728 (raw DAC full scale)
ADS_GAIN = 1         # 1 -> +/-4.096V input range; use 2/3 for +/-6.144V

# Channel A shift/amplify circuit: V_physical = V_DAC * GAIN + OFFSET
DAC_SHIFT_GAIN = 3.826
DAC_SHIFT_OFFSET = -9.78

# Channels B, C, D: fixed raw DAC volts, not physical-converted, not GUI-adjustable
DAC_HIGH_VOLTAGE = 4.0
DAC_LOW_VOLTAGE = 0.0

# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------
MQTT_TOPIC = "experiment/sensor/ncc-1701/adc"

# --------------------------------------------------------------------------
# Settings file location
# --------------------------------------------------------------------------
SETTINGS_PATH = Path(os.environ.get(
    "AUTOLOCK_SETTINGS_PATH",
    Path.home() / ".config" / "autolock_gui" / "settings.json",
))


# --------------------------------------------------------------------------
# Settings (editable live from the GUI; plain attributes, no extra lock -
# the GIL makes simple read/write of these safe enough between threads)
# --------------------------------------------------------------------------
@dataclass
class Settings:
    # --- Identity (edited from the window header, not the Settings tab) ---
    DEVICE_NAME: str = ""

    # --- Safe range parameters ---
    SLOW_OUTPUT_SAFE_MIN: float = -10.0
    SLOW_OUTPUT_SAFE_MAX: float = 10.0
    FAST_OUTPUT_SAFE_MIN: float = -10.0
    FAST_OUTPUT_SAFE_MAX: float = 10.0
    DC_ERR_SAFE_MIN: float = 1.0
    DC_ERR_SAFE_MAX: float = 3.0

    # --- Scan parameters ---
    SCAN_MIN_VOLTAGE: float = 0.0    # control out, PHYSICAL volts
    SCAN_MAX_VOLTAGE: float = 4.0    # control out, PHYSICAL volts
    ADAPTIVE_SCAN: bool = True       # vary point spacing with how fast the signal changes
    SCAN_MAX_STEP: float = 0.04      # adaptive: largest step, PHYSICAL volts - keep it below the
                                     # width of the narrowest feature, or the sweep can step over it
    SCAN_MIN_STEP: float = 0.005     # adaptive: smallest step, PHYSICAL volts
    ADAPTIVE_TARGET_CHANGE: float = 0.2  # adaptive: aim for this much signal change (V) per step
    NUM_COARSE_POINTS: int = 51      # uniform scans only
    NUM_FINE_POINTS: int = 41
    MANUAL_SCAN_POINTS: int = 101    # uniform scans only
    SCAN_SETTLE_TIME: float = 0.01
    NUM_SAMPLES_PER_POINT: int = 5
    SMOOTHING_WINDOW: int = 3

    # --- General program parameters ---
    AUTOLOCK_MODE: str = "zero_crossing"   # "zero_crossing" or "dc_err_range"
    CROSSING_SIGN: str = "positive"        # "positive", "negative", or "both"
    MIN_CROSSING_SLOPE_FRACTION: float = 0.1  # ignore crossings shallower than this x the steepest one
    RESET_VOLTAGE_ON_FAILURE: float = 2.0  # control out, PHYSICAL volts
    PRIME_SETTLE_TIME: float = 0.1
    MONITOR_INTERVAL: float = 0.2          # fast: GUI display sampling rate (s)
    LOG_INTERVAL: float = 1.0              # slow: MQTT/Influx logging rate (s)
    LOCK_RETRY_DELAY: float = 1.0
    LIVE_MONITOR_TIME_SPAN: float = 60.0   # seconds of history shown on the live monitor
    SAVE_SCAN_PLOTS: bool = True
    SCAN_PLOT_DIR: str = "scan_plots"

    # --- Scan tab "constant output" entry (control out, PHYSICAL volts) ---
    CONSTANT_OUTPUT_VOLTAGE: float = 2.0


settings = Settings()

FIELD_TYPES = {f.name: f.type for f in fields(Settings)}

CHOICES = {
    "AUTOLOCK_MODE": ("zero_crossing", "dc_err_range"),
    "CROSSING_SIGN": ("positive", "negative", "both"),
}

# Lower bounds that keep the program working (e.g. a zero monitor interval
# would spin the monitor thread, a 1-point scan has no step size).
MINIMUMS = {
    "NUM_COARSE_POINTS": 2,
    "NUM_FINE_POINTS": 2,
    "MANUAL_SCAN_POINTS": 2,
    "SCAN_MAX_STEP": 1e-4,
    "SCAN_MIN_STEP": 1e-4,
    "ADAPTIVE_TARGET_CHANGE": 1e-4,
    "NUM_SAMPLES_PER_POINT": 1,
    "SMOOTHING_WINDOW": 1,
    "SCAN_SETTLE_TIME": 0.0,
    "PRIME_SETTLE_TIME": 0.0,
    "MONITOR_INTERVAL": 0.01,
    "LOG_INTERVAL": 0.01,
    "LOCK_RETRY_DELAY": 0.0,
    "LIVE_MONITOR_TIME_SPAN": 1.0,
    "MIN_CROSSING_SLOPE_FRACTION": 0.0,
}

MAXIMUMS = {
    "MIN_CROSSING_SLOPE_FRACTION": 1.0,
}


def coerce_value(field_name, raw):
    """
    Convert a raw value (a GUI entry string, or a value read from JSON) to
    the field's declared type, enforcing CHOICES, MINIMUMS and MAXIMUMS. Raises
    ValueError with a readable message if the value isn't acceptable.
    """
    field_type = FIELD_TYPES[field_name]
    if field_type is bool:
        if isinstance(raw, bool):
            value = raw
        else:
            value = str(raw).strip().lower() in ("1", "true", "yes", "on")
    elif field_type is int:
        value = int(raw)
    elif field_type is float:
        value = float(raw)
    else:
        value = str(raw)

    if field_name in CHOICES and value not in CHOICES[field_name]:
        raise ValueError(f"{field_name} must be one of {CHOICES[field_name]}, got {value!r}")
    if field_name in MINIMUMS and value < MINIMUMS[field_name]:
        raise ValueError(f"{field_name} must be >= {MINIMUMS[field_name]}, got {value}")
    if field_name in MAXIMUMS and value > MAXIMUMS[field_name]:
        raise ValueError(f"{field_name} must be <= {MAXIMUMS[field_name]}, got {value}")
    return value


def load_settings():
    """
    Overwrite `settings` in place with the values saved on this machine.
    Unknown or invalid entries are skipped (keeping their defaults), so an
    old or hand-edited file never stops the GUI from starting. Returns a
    message describing what happened, for the live log.
    """
    if not SETTINGS_PATH.exists():
        return f"No saved settings at {SETTINGS_PATH} - using defaults."
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level is not a JSON object")
    except (OSError, ValueError) as exc:
        return f"Could not read saved settings at {SETTINGS_PATH} ({exc}) - using defaults."

    skipped = []
    for name, raw in data.items():
        if name not in FIELD_TYPES:
            skipped.append(name)
            continue
        try:
            setattr(settings, name, coerce_value(name, raw))
        except (TypeError, ValueError):
            skipped.append(name)

    message = f"Loaded settings from {SETTINGS_PATH}."
    if skipped:
        message += f" Ignored unknown/invalid entries: {', '.join(skipped)}"
    return message


def save_settings():
    """
    Write the current settings to SETTINGS_PATH. Writes to a temporary file
    and renames it into place, so a crash mid-write can't leave a truncated
    settings file behind. Raises OSError on failure.
    """
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp_path, SETTINGS_PATH)
