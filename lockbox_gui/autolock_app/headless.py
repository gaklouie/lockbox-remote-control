"""
Headless operation: keep monitoring, logging and autolock running with no GUI.

How the hand-off works when HEADLESS_ON_CLOSE is on:

    1. The GUI starts a detached "standby" process (spawn_standby). It does
       nothing but wait for the GUI process to end.
    2. When the GUI ends - closed normally, crashed, or cut off by a dropped
       SSH connection - the standby takes over the hardware and runs the same
       monitor loop the GUI runs (run), with autolock engaged if it was
       engaged in the GUI.
    3. The next GUI to start stops the headless process first (stop_running)
       and takes the hardware back.

Only one program may drive the DAC/ADC at a time, which is why the standby
waits for the GUI to be gone and the GUI waits for the headless process to
exit. The headless process records its process id in PID_PATH and writes its
log to LOG_PATH, both next to the settings file.

Run by hand with `uv run python3 autolock_gui.py --headless [--engage]`.
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, state
from .config import SETTINGS_PATH, settings

PID_PATH = SETTINGS_PATH.with_name("headless.pid")
LOG_PATH = SETTINGS_PATH.with_name("headless.log")
MAX_LOG_BYTES = 1_000_000  # the log is started afresh once it grows past this

ENTRY_SCRIPT = Path(__file__).resolve().parent.parent / "autolock_gui.py"


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------
def process_exists(pid):
    """True if a process with this id is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) TERMINATES the process on Windows, so ask the OS instead.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_headless_process(pid):
    """
    True if `pid` is running and is one of our headless processes. Checking
    the command line (where the OS lets us) means a stale pid file can never
    make us signal some unrelated program that reused the number.
    """
    if not process_exists(pid):
        return False
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            return b"--headless" in cmdline_path.read_bytes()
        except OSError:
            return False
    return True


def running_pid():
    """Process id of the running headless (or standby) process, or None."""
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _is_headless_process(pid) else None


def _remove_pid_file(only_if_pid=None):
    try:
        if only_if_pid is not None and int(PID_PATH.read_text().strip()) != only_if_pid:
            return
        PID_PATH.unlink()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# Called from the GUI
# --------------------------------------------------------------------------
def spawn_standby(gui_pid):
    """
    Start a detached headless process that waits for `gui_pid` to end and
    then takes over. Does nothing if one is already running. Returns True
    once the process has registered itself.
    """
    if running_pid() is not None:
        return True

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh = not LOG_PATH.exists() or LOG_PATH.stat().st_size > MAX_LOG_BYTES
    log_file = open(LOG_PATH, "w" if fresh else "a", encoding="utf-8")

    # Detach fully, so the process outlives this one and the SSH session.
    if os.name == "nt":
        detach = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        detach = {"start_new_session": True}
    subprocess.Popen(
        [sys.executable, str(ENTRY_SCRIPT), "--headless", "--wait-for-pid", str(gui_pid)],
        stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
        cwd=os.getcwd(), **detach,
    )
    log_file.close()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if running_pid() is not None:
            return True
        time.sleep(0.1)
    return False


def stop_running(timeout=15.0):
    """
    Stop the running headless (or standby) process and wait until it has
    let go of the hardware. Returns True if there was one to stop.
    """
    pid = running_pid()
    if pid is None:
        _remove_pid_file()
        return False

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while process_exists(pid) and time.time() < deadline:
        time.sleep(0.1)
    if process_exists(pid) and os.name != "nt":
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    _remove_pid_file()
    return True


# --------------------------------------------------------------------------
# The headless program itself
# --------------------------------------------------------------------------
def _log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def run(wait_for_pid=None, engage=False):
    """
    Run the monitor/log/autolock loop with no GUI until told to stop
    (SIGTERM, or Ctrl-C when run by hand).

    wait_for_pid: stay idle until this process (the GUI) has ended, and
                  then start only if HEADLESS_ON_CLOSE is still on.
    engage:       engage autolock regardless of the saved AUTOLOCK_ENGAGED.
    """
    if running_pid() not in (None, os.getpid()):
        _log("Another headless process is already running - exiting.")
        return

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))

    def request_stop(_signum, _frame):
        state.stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        if wait_for_pid:
            _log(f"Standing by until the GUI (process {wait_for_pid}) closes.")
            while process_exists(wait_for_pid) and not state.stop_event.is_set():
                time.sleep(0.5)
            if state.stop_event.is_set():
                _log("Stopped while standing by.")
                return

        _log(config.load_settings())
        if wait_for_pid and not settings.HEADLESS_ON_CLOSE:
            _log("GUI closed with headless operation switched off - exiting.")
            return

        # Imported here, not at the top: these pull in matplotlib and the
        # hardware libraries, and the standby should register itself fast.
        from . import hardware, telemetry
        from .workers import monitor_loop

        telemetry.init_mqtt()
        hardware.init_hardware()
        if engage or settings.AUTOLOCK_ENGAGED:
            state.autolock_engaged.set()
        if state.autolock_engaged.is_set():
            autolock_status = f"engaged ({config.MODE_DISPLAY_NAMES[settings.AUTOLOCK_MODE]})"
        else:
            autolock_status = "not engaged"
        _log(f"Headless operation started: monitoring and logging as "
             f"{settings.DEVICE_NAME or 'unnamed lockbox'}, autolock {autolock_status}.")

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

        # Nothing else reads the worker->GUI queue here, so drain it: keep the
        # log lines, drop the readings (they still go to MQTT from the loop).
        while not state.stop_event.is_set():
            try:
                item = state.gui_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item[0] == "log":
                _log(item[1])

        monitor_thread.join(timeout=10.0)
        telemetry.close_mqtt()
        _log("Headless operation stopped.")
    finally:
        _remove_pid_file(only_if_pid=os.getpid())
