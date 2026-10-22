"""
Lockbox web service (prototype).

One always-running program on the Pi owns the DAC/ADC and runs the
monitor / logging / autolock loop. Any number of browsers can open
http://<pi>:8000 at the same time to watch and control it.

    uv sync
    uv run python server.py [--host 0.0.0.0] [--port 8000]

The hardware access, scans and lock logic live in the `autolock_app`
package next to this file; this file is the web side.

How the pieces talk:

    worker threads --state.event_queue--> pump thread --> asyncio queue
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

if __name__ == "__main__":
    # Printed before the slow imports below, so it's clear something is happening.
    print("Starting the lockbox web service (loading libraries, then connecting to MQTT "
          "and the hardware)...", flush=True)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from autolock_app import config, hardware, state, telemetry
from autolock_app.channels import DISPLAY_NAMES, ERROR_SIGNAL_NAMES, OUTPUT_SIGNAL_NAMES, RANGE_NAMES
from autolock_app.config import (CHOICES, FIELD_TYPES, MODE_DISPLAY_NAMES, SETTINGS_PATH,
                                 coerce_value, save_settings, settings)
from autolock_app.following import FollowFailed, follow_crossing
from autolock_app.locking import autolock_once
from autolock_app.scanning import ScanStopped
from autolock_app.signal_processing import Crossing
from autolock_app.plotting import SIGNAL_COLORS
from autolock_app.workers import monitor_loop, run_manual_scan

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
MAX_HISTORY_POINTS = 5000
MAX_LOG_LINES = 300

# Settings shown in the page's Settings tab: (group title, [(field, label), ...]).
SETTINGS_GROUPS = [
    ("Calibration", [
        ("ERROR_READ_OFFSET", "Error read offset (V, added to error)"),
    ]),
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
        ("FAST_SWEEP", "Fast sweep (searched signal only)"),
        ("SCAN_SETTLE_MS", "Settle time (ms)"),
        ("NUM_SAMPLES_PER_POINT", "Samples averaged/point"),
        ("SMOOTHING_WINDOW", "Smoothing window (1 = off)"),
    ]),
    ("General Program Parameters", [
        ("AUTOLOCK_MODE", "Autolock mode"),
        ("CROSSING_SIGN", "Crossing sign"),
        ("MIN_CROSSING_SLOPE_FRACTION", "Min crossing slope (x steepest, 0-1)"),
        ("MIN_CROSSING_SYMMETRY", "Min crossing symmetry (0-1, 1 = equal lobes)"),
        ("MIN_LOBE_HEIGHT_FRACTION", "Min lobe height (x tallest symmetric, 0-1)"),
        ("LOCK_ACQUIRE", "Home in on the crossing by"),
        ("SERVO_P_GAIN", "Follow: proportional gain (0-1.5)"),
        ("SERVO_I_GAIN", "Follow: integral gain (0-1)"),
        ("SERVO_TOLERANCE", "Follow: converged at |error| <= (x lobe height)"),
        ("SERVO_MAX_ITERATIONS", "Follow: max steps"),
        ("RESET_VOLTAGE_ON_FAILURE", "Reset voltage on failure (physical V)"),
        ("PRIME_SETTLE_TIME", "Prime (short caps) settle time (s)"),
        ("MONITOR_INTERVAL", "Monitor sample interval (s)"),
        ("LOG_INTERVAL", "Logging interval (s)"),
        ("LIVE_MONITOR_TIME_SPAN", "Live monitor time span (s)"),
        ("SHOW_DC_ERROR", "Show DC error in plots"),
        ("LOCK_RETRY_DELAY", "Retry delay on failure (s)"),
        ("SAVE_SCAN_PLOTS", "Save autolock scan plots"),
        ("SCAN_PLOT_DIR", "Scan plot directory"),
    ]),
]
EDITABLE_FIELDS = {name for _title, fields in SETTINGS_GROUPS for name, _label in fields}
CHOICE_LABELS = {
    "AUTOLOCK_MODE": MODE_DISPLAY_NAMES,
    "CROSSING_SIGN": {"positive": "Positive", "negative": "Negative", "both": "Both"},
    "LOCK_ACQUIRE": {"fine_scan": "Fine scan", "follow": "Following the crossing (servo)"},
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
        "settings": {name: getattr(settings, name) for name in
                     EDITABLE_FIELDS | {"CONSTANT_OUTPUT_VOLTAGE", "MANUAL_SCAN_MODE", "MANUAL_SCAN_SIGN"}},
        "engaged": state.autolock_engaged.is_set(),
        "mode_name": MODE_DISPLAY_NAMES[settings.AUTOLOCK_MODE],
        "busy": state.busy_reason(),
        "scanning": state.manual_scan_active.is_set(),
        "autolock_running": state.autolock_running.is_set(),
        "lock_acquiring": state.lock_acquiring.is_set(),
        "pid_running": hardware.pid_running(),   # True / False / None (B, C, D disagree)
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
            item = state.event_queue.get(timeout=0.5)
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
                                "start": c["start"], "end": c["end"], "crossing": c.get("crossing")}
                               for c in data["candidates"]],
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


def _follow_and_lock(crossing, who):
    """Thread for "Lock to Selected" in follow mode: follow the crossing, then hand over to the PID."""
    try:
        hub.log(f"[{who}] Locking: following the crossing seen at {crossing.voltage:.4f} V...")
        hardware.short_caps_and_block_pid()          # control out alone moves the actuator while following
        lock_v = follow_crossing(crossing, hub.log)
        hardware.set_control_out(lock_v)
        hardware.restart_pid()
        hub.log(f"[{who}] Locked to control out = {lock_v:.4f} V physical (followed onto the crossing)")
    except FollowFailed as exc:
        hub.log(f"Lock failed: {exc}. B/C/D left high (PID off).")
    except ScanStopped as exc:
        hub.log(f"Locking {exc}. B/C/D left high (PID off).")
    except Exception as exc:
        hub.log(f"Lock failed with an error: {exc!r}. B/C/D left high (PID off).")
    finally:
        state.stop_scan_requested.clear()
        state.lock_acquiring.clear()


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
        settings.MANUAL_SCAN_MODE, settings.MANUAL_SCAN_SIGN = mode, sign
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
        if settings.LOCK_ACQUIRE == "follow" and candidate.get("crossing"):
            # Follow the crossing as it is now (it may have moved since the scan).
            state.stop_scan_requested.clear()
            state.lock_acquiring.set()   # mark busy now, so a second click can't start another
            threading.Thread(target=_follow_and_lock, args=(Crossing(**candidate["crossing"]), who),
                             daemon=True).start()
            return
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

    elif cmd == "stop_scan":
        if not (state.manual_scan_active.is_set() or state.autolock_running.is_set()
                or state.lock_acquiring.is_set()):
            raise CommandError("No scan is running.")
        stopping_autolock = state.autolock_running.is_set()
        state.stop_scan_requested.set()
        hub.log(f"[{who}] Stop requested.")
        if stopping_autolock and state.autolock_engaged.is_set():
            # Otherwise the monitor loop would start a new autolock scan a moment later.
            _set_engaged(False)

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
    if web_server is not None:
        tasks.append(asyncio.create_task(announce_when_listening()))
    try:
        yield
    finally:
        state.stop_event.set()
        for task in tasks:
            task.cancel()
        for thread in threads:
            await asyncio.to_thread(thread.join, 10.0)
        telemetry.close_mqtt()
        if web_server is not None:
            print("Lockbox web service stopped.", flush=True)


web_server = None  # the uvicorn.Server, set by main() when run as a program


def page_addresses(host, port):
    """Addresses a browser on another computer can use to reach this service."""
    if host not in ("0.0.0.0", "::", ""):
        return [f"http://{host}:{port}"]
    addresses = []
    try:
        # Finds the address of the network interface used to reach other computers.
        # Connecting a UDP socket sends nothing; it only picks a route.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            addresses.append(f"http://{probe.getsockname()[0]}:{port}")
    except OSError:
        pass
    addresses.append(f"http://{socket.gethostname()}.local:{port}")
    return addresses


async def announce_when_listening():
    """Print a ready message once uvicorn is actually accepting connections."""
    while not web_server.started:
        await asyncio.sleep(0.1)
    config_ = web_server.config
    addresses = "  or  ".join(page_addresses(config_.host, config_.port))
    print(f"Lockbox web service is running. Open {addresses} in a browser.\n"
          f"Stop it with Ctrl-C (or, if started in the background: pkill -f \"python server.py\").",
          flush=True)


app = FastAPI(title="Lockbox web service", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def always_check_for_newer_page(request, call_next):
    """
    Tell browsers to check back before reusing their saved copy of the page,
    its scripts and styles. Without this, a browser can keep running the old
    app.js/plot.js after the service is updated. The check is cheap: an
    unchanged file comes back as a tiny "not modified" reply.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/icon.png")
async def icon():
    return FileResponse(STATIC_DIR / "icon.png", media_type="image/png")


@app.get("/icon_32.png")
async def icon_32():
    return FileResponse(STATIC_DIR / "icon_32.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon():
    # Browsers ask for this by default even when the page names its own icon.
    return FileResponse(STATIC_DIR / "icon_32.png", media_type="image/png")


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


def archived_gui_headless_pid():
    """
    Process id of a headless process left running by the archived Tk GUI
    (old/lockbox_gui), or None. It records itself in headless.pid next to the
    settings file. Checked via /proc, so only on Linux (the Pi).
    """
    try:
        pid = int(SETTINGS_PATH.with_name("headless.pid").read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if b"--headless" in cmdline else None


def main():
    parser = argparse.ArgumentParser(description="Lockbox web service (prototype)")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Only one program may drive the DAC/ADC.
    old_pid = archived_gui_headless_pid()
    if old_pid is not None:
        sys.exit(f"The archived Tk GUI's headless process (process {old_pid}) is still running and "
                 f"driving the hardware. Stop it first:\n    kill {old_pid}")

    global web_server
    web_server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    web_server.run()


if __name__ == "__main__":
    main()
