"""
Lockbox web service (prototype).

One always-running program on the Pi owns the DAC/ADC and runs the same
monitor / logging / autolock loop as the Tk GUI. Any number of browsers can
open http://<pi>:8000 at the same time to watch and control it.

    uv sync
    uv run python server.py [--host 0.0.0.0] [--port 8000]

The lock logic is not duplicated: this imports the `autolock_app` package
from ../lockbox_gui (everything except its Tk `gui/` modules).

How the pieces talk:

    worker threads --state.gui_queue--> pump thread --> asyncio queue
        --> Hub.run(): updates the shared history, then sends the event as
            JSON to every connected browser over a WebSocket (/ws)

    browser --WebSocket JSON {"cmd": ...}--> handle_command() --> hardware /
        settings / worker threads, then a fresh "status" goes to everyone

See README.md for the message list and what is still missing (no login!).
"""

import argparse
import asyncio
import json
import queue
import socket
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lockbox_gui"))

from autolock_app import config, hardware, headless, state, telemetry  # noqa: E402
from autolock_app.channels import (DISPLAY_NAMES, ERROR_SIGNAL_NAMES,  # noqa: E402
                                   OUTPUT_SIGNAL_NAMES, RANGE_NAMES)
from autolock_app.config import (CHOICES, FIELD_TYPES, MODE_DISPLAY_NAMES,  # noqa: E402
                                 coerce_value, save_settings, settings)
from autolock_app.locking import autolock_once  # noqa: E402
from autolock_app.plotting import SIGNAL_COLORS  # noqa: E402
from autolock_app.workers import monitor_loop, run_manual_scan  # noqa: E402

STATIC_DIR = HERE / "static"
ICON_DIR = HERE.parent / "lockbox_gui"  # the Tk GUI's icon doubles as the browser-tab icon
MAX_HISTORY_POINTS = 5000
MAX_LOG_LINES = 300

# Settings shown in the page's Settings tab: (group title, [(field, label), ...]).
# Mirrors the Tk GUI's Settings tab (lockbox_gui/autolock_app/gui/settings_tab.py).
SETTINGS_GROUPS = [
    ("Safe / Lock Range Parameters", [
        ("SLOW_OUTPUT_SAFE_MIN", "Slow output safe min (V)"),
        ("SLOW_OUTPUT_SAFE_MAX", "Slow output safe max (V)"),
        ("FAST_OUTPUT_SAFE_MIN", "Fast output safe min (V)"),
        ("FAST_OUTPUT_SAFE_MAX", "Fast output safe max (V)"),
        ("DC_ERR_SAFE_MIN", "DC error lock range min (V)"),
        ("DC_ERR_SAFE_MAX", "DC error lock range max (V)"),
    ]),
    ("Scan Parameters", [
        ("SCAN_MIN_VOLTAGE", "Scan output min (physical V)"),
        ("SCAN_MAX_VOLTAGE", "Scan output max (physical V)"),
        ("ADAPTIVE_SCAN", "Adaptive point spacing"),
        ("SCAN_MAX_STEP", "Adaptive max step (physical V)"),
        ("SCAN_MIN_STEP", "Adaptive min step (physical V)"),
        ("ADAPTIVE_TARGET_CHANGE", "Adaptive target change/step (V)"),
        ("NUM_COARSE_POINTS", "Coarse pass points (uniform only)"),
        ("MANUAL_SCAN_POINTS", "Manual scan points (uniform only)"),
        ("NUM_FINE_POINTS", "Fine pass points"),
        ("SCAN_SETTLE_TIME", "Settle time (s)"),
        ("NUM_SAMPLES_PER_POINT", "Samples averaged/point"),
        ("SMOOTHING_WINDOW", "Smoothing window (1 = off)"),
    ]),
    ("General Program Parameters", [
        ("AUTOLOCK_MODE", "Autolock mode"),
        ("CROSSING_SIGN", "Crossing sign"),
        ("MIN_CROSSING_SLOPE_FRACTION", "Min crossing slope (x steepest, 0-1)"),
        ("RESET_VOLTAGE_ON_FAILURE", "Reset voltage on failure (physical V)"),
        ("PRIME_SETTLE_TIME", "Prime (short caps) settle time (s)"),
        ("MONITOR_INTERVAL", "Monitor sample interval (s)"),
        ("LOG_INTERVAL", "Logging interval (s)"),
        ("LIVE_MONITOR_TIME_SPAN", "Live monitor time span (s)"),
        ("LOCK_RETRY_DELAY", "Retry delay on failure (s)"),
        ("SAVE_SCAN_PLOTS", "Save autolock scan plots"),
        ("SCAN_PLOT_DIR", "Scan plot directory"),
    ]),
]
EDITABLE_FIELDS = {name for _title, fields in SETTINGS_GROUPS for name, _label in fields}
CHOICE_LABELS = {
    "AUTOLOCK_MODE": MODE_DISPLAY_NAMES,
    "CROSSING_SIGN": {"positive": "Positive", "negative": "Negative", "both": "Both"},
}


def describe_page():
    """Static description the page builds itself from: signals and the settings form."""
    def field(name, label):
        kind = {bool: "bool", int: "int", float: "float"}.get(FIELD_TYPES[name], "text")
        entry = {"name": name, "label": label, "kind": kind}
        if name in CHOICES:
            entry["kind"] = "choice"
            entry["choices"] = [{"value": v, "label": CHOICE_LABELS[name][v]} for v in CHOICES[name]]
        return entry

    return {
        "signals": {name: {"label": DISPLAY_NAMES[name], "color": SIGNAL_COLORS[name],
                           "range_name": RANGE_NAMES.get(name)} for name in DISPLAY_NAMES},
        "output_signals": OUTPUT_SIGNAL_NAMES,
        "error_signals": ERROR_SIGNAL_NAMES,
        "settings_groups": [{"title": title, "fields": [field(n, lab) for n, lab in fields]}
                            for title, fields in SETTINGS_GROUPS],
    }


def current_status():
    """Everything a browser needs to show the current state of the lockbox."""
    return {
        "hostname": socket.gethostname(),
        "name": settings.DEVICE_NAME,
        "settings": {name: getattr(settings, name) for name in EDITABLE_FIELDS | {"CONSTANT_OUTPUT_VOLTAGE"}},
        "engaged": state.autolock_engaged.is_set(),
        "mode_name": MODE_DISPLAY_NAMES[settings.AUTOLOCK_MODE],
        "busy": state.busy_reason(),
        "scanning": state.manual_scan_active.is_set(),
        "autolock_running": state.autolock_running.is_set(),
    }


class CommandError(Exception):
    """A command was refused; the message is shown to the browser that sent it."""


class Hub:
    """Shared history plus the set of connected browsers. Lives on the asyncio loop."""

    def __init__(self):
        self.clients = set()
        self.events = None  # asyncio.Queue, created once the loop is running
        self.loop = None
        self.readings = deque(maxlen=MAX_HISTORY_POINTS)   # (timestamp, values)
        self.log_lines = deque(maxlen=MAX_LOG_LINES)       # (timestamp, message)
        self.last_scan = None
        self.last_status_json = None

    # ---- called from other threads ----
    def post(self, event):
        """Hand an event to the loop, from any thread."""
        self.loop.call_soon_threadsafe(self.events.put_nowait, event)

    def log(self, message):
        self.post({"type": "log", "t": time.time(), "message": message})

    # ---- on the loop ----
    def snapshot(self):
        """What a newly connected browser needs to catch up."""
        cutoff = time.time() - settings.LIVE_MONITOR_TIME_SPAN
        return {
            "type": "hello",
            "page": describe_page(),
            "status": current_status(),
            "readings": [{"t": t, "values": v} for t, v in self.readings if t >= cutoff],
            "log": [{"t": t, "message": m} for t, m in self.log_lines],
            "scan": self.last_scan,
        }

    async def send_to_all(self, event):
        if not self.clients:
            return
        text = json.dumps(event)
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=2.0)
            except Exception:
                self.clients.discard(ws)

    async def broadcast_status(self, force=False):
        status = current_status()
        as_json = json.dumps(status, sort_keys=True)
        if force or as_json != self.last_status_json:
            self.last_status_json = as_json
            await self.send_to_all({"type": "status", "status": status})

    async def run(self):
        """Take events in order, record them in the shared history, send them to everyone."""
        while True:
            event = await self.events.get()
            kind = event["type"]
            if kind == "reading":
                self.readings.append((event["t"], event["values"]))
            elif kind == "log":
                self.log_lines.append((event["t"], event["message"]))
            elif kind == "scan":
                self.last_scan = event["scan"]
            await self.send_to_all(event)
            if kind in ("scan", "scan_failed"):
                await self.broadcast_status()

    async def status_ticker(self):
        """Catch state changes nobody announced (e.g. a triggered autolock starting or ending)."""
        while True:
            await asyncio.sleep(1.0)
            await self.broadcast_status()


hub = Hub()


def pump_worker_queue():
    """Thread: turn the workers' queue items into JSON-ready events for the hub."""
    while not state.stop_event.is_set():
        try:
            item = state.gui_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        kind = item[0]
        if kind == "reading":
            _, timestamp, values = item
            hub.post({"type": "reading", "t": timestamp, "values": {k: float(v) for k, v in values.items()}})
        elif kind == "log":
            hub.log(item[1])
        elif kind == "scan_progress":
            hub.post({"type": "scan_progress", "fraction": float(item[1])})
        elif kind == "scan_done":
            data = item[1]
            if data is None:
                hub.post({"type": "scan_failed"})
                continue
            hub.post({"type": "scan", "scan": {
                "t": time.time(),
                "mode": data["mode"],
                "voltages": [float(v) for v in data["voltages"]],
                "trace": {name: [float(v) for v in values] for name, values in data["trace"].items()},
                "smoothed": [float(v) for v in data["signal_smoothed"]],
                "candidates": [{"lock_voltage": float(c["lock_voltage"]), "label": c["label"],
                                "start": c["start"], "end": c["end"]} for c in data["candidates"]],
            }})


# --------------------------------------------------------------------------
# Commands from browsers
# --------------------------------------------------------------------------
def _require_free():
    reason = state.busy_reason()
    if reason:
        raise CommandError(reason)


def _set_engaged(engaged):
    if engaged:
        state.autolock_engaged.set()
        hub.log(f"Autolock engaged ({MODE_DISPLAY_NAMES[settings.AUTOLOCK_MODE]}).")
    else:
        state.autolock_engaged.clear()
        hub.log("Autolock disengaged.")
    settings.AUTOLOCK_ENGAGED = engaged
    save_settings()


def _apply_settings(raw_values):
    """Validate everything first; change nothing if any value is bad."""
    try:
        new_values = {name: coerce_value(name, raw) for name, raw in raw_values.items()
                      if name in EDITABLE_FIELDS}
    except (TypeError, ValueError) as exc:
        raise CommandError(f"Invalid setting: {exc}") from exc
    min_step = new_values.get("SCAN_MIN_STEP", settings.SCAN_MIN_STEP)
    max_step = new_values.get("SCAN_MAX_STEP", settings.SCAN_MAX_STEP)
    if min_step > max_step:
        raise CommandError("Adaptive min step must not exceed max step.")
    for name, value in new_values.items():
        setattr(settings, name, value)
    save_settings()


def handle_command(message, who):
    """Carry out one command. Runs in a worker thread, so hardware calls may block."""
    cmd = message.get("cmd")

    if cmd == "run_scan":
        _require_free()
        try:
            v_min = coerce_value("SCAN_MIN_VOLTAGE", message["v_min"])
            v_max = coerce_value("SCAN_MAX_VOLTAGE", message["v_max"])
            points = coerce_value("MANUAL_SCAN_POINTS", message["points"])
            mode = coerce_value("AUTOLOCK_MODE", message["mode"])
            sign = coerce_value("CROSSING_SIGN", message["sign"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"Invalid scan settings: {exc}") from exc
        adaptive = bool(message.get("adaptive", settings.ADAPTIVE_SCAN))
        settings.SCAN_MIN_VOLTAGE, settings.SCAN_MAX_VOLTAGE = v_min, v_max
        settings.MANUAL_SCAN_POINTS, settings.ADAPTIVE_SCAN = points, adaptive
        save_settings()
        state.manual_scan_active.set()  # mark busy now, so a second browser can't start another
        threading.Thread(target=run_manual_scan, args=(v_min, v_max, points, mode, sign, adaptive),
                         daemon=True).start()

    elif cmd == "lock":
        _require_free()
        scan = hub.last_scan
        try:
            candidate = scan["candidates"][int(message["index"])]
        except (TypeError, KeyError, IndexError, ValueError) as exc:
            raise CommandError("Select a lock candidate from the latest scan first.") from exc
        hardware.set_control_out(candidate["lock_voltage"])
        hardware.restart_pid()
        hub.log(f"[{who}] Locked to control out = {candidate['lock_voltage']:.4f} V physical")

    elif cmd == "release":
        hardware.restart_pid()
        hub.log(f"[{who}] PID restarted (B, C, D set LOW) without changing control out.")

    elif cmd == "set_output":
        _require_free()
        try:
            voltage = coerce_value("CONSTANT_OUTPUT_VOLTAGE", message["voltage"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"Invalid output voltage: {exc}") from exc
        settings.CONSTANT_OUTPUT_VOLTAGE = voltage
        save_settings()
        hardware.set_control_out(voltage)
        hub.log(f"[{who}] Control out set to constant {voltage:.4f} V physical (B/C/D unchanged).")

    elif cmd == "apply_settings":
        _apply_settings(message.get("values", {}))
        hub.log(f"[{who}] Settings applied and saved. Autolock mode = "
                f"{MODE_DISPLAY_NAMES[settings.AUTOLOCK_MODE]}, crossing sign = {settings.CROSSING_SIGN}")

    elif cmd == "engage":
        if message.get("on"):
            _require_free()
            _apply_settings(message.get("values", {}))  # refuse to engage with invalid settings
            _set_engaged(True)
        else:
            _set_engaged(False)

    elif cmd == "autolock_now":
        _require_free()
        _apply_settings(message.get("values", {}))
        state.autolock_running.set()  # mark busy now, so a quick second click can't start another
        threading.Thread(target=autolock_once, args=(hub.log,), daemon=True).start()

    elif cmd == "set_name":
        settings.DEVICE_NAME = str(message.get("name", "")).strip()
        save_settings()
        hub.log(f"[{who}] Lockbox name set to {settings.DEVICE_NAME!r}.")

    else:
        raise CommandError(f"Unknown command: {cmd!r}")


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    hub.loop = asyncio.get_running_loop()
    hub.events = asyncio.Queue()
    hub.log(config.load_settings())

    telemetry.init_mqtt()
    hardware.init_hardware()
    if settings.AUTOLOCK_ENGAGED:
        # An always-on service comes back the way it was left, e.g. after a reboot.
        state.autolock_engaged.set()
        hub.log(f"Autolock engaged ({MODE_DISPLAY_NAMES[settings.AUTOLOCK_MODE]}) - restored from last run.")

    threads = [threading.Thread(target=monitor_loop, daemon=True),
               threading.Thread(target=pump_worker_queue, daemon=True)]
    for thread in threads:
        thread.start()
    tasks = [asyncio.create_task(hub.run()), asyncio.create_task(hub.status_ticker())]
    try:
        yield
    finally:
        state.stop_event.set()
        for task in tasks:
            task.cancel()
        for thread in threads:
            await asyncio.to_thread(thread.join, 10.0)
        telemetry.close_mqtt()


app = FastAPI(title="Lockbox web service", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/icon.png")
async def icon():
    return FileResponse(ICON_DIR / "icon.png", media_type="image/png")


@app.get("/icon_32.png")
async def icon_32():
    return FileResponse(ICON_DIR / "icon_32.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon():
    # Browsers ask for this by default even when the page names its own icon.
    return FileResponse(ICON_DIR / "icon_32.png", media_type="image/png")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    who = ws.client.host if ws.client else "unknown"
    await ws.send_text(json.dumps(hub.snapshot()))
    hub.clients.add(ws)
    try:
        while True:
            try:
                message = json.loads(await ws.receive_text())
                await asyncio.to_thread(handle_command, message, who)
                await ws.send_text(json.dumps({"type": "ok", "cmd": message.get("cmd")}))
            except CommandError as exc:
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            except (ValueError, OSError) as exc:
                await ws.send_text(json.dumps({"type": "error", "message": f"Command failed: {exc}"}))
            await hub.broadcast_status()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


def main():
    parser = argparse.ArgumentParser(description="Lockbox web service (prototype)")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Only one program may drive the DAC/ADC.
    if headless.running_pid() is not None:
        sys.exit("The Tk GUI's headless process is running and owns the hardware. Stop it first:\n"
                 "open the Tk GUI and untick 'Keep running after GUI closes', then close it.")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
