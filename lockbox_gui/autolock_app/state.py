"""
Cross-thread state shared between the GUI and the background workers.

Workers never touch Tk widgets directly; they put messages on gui_queue and
the GUI drains it on its own thread (MainApp.poll_queue). Message shapes:

    ("reading", timestamp, values_dict)   a new set of physical readings
    ("log", message)                      a line for the live log
    ("scan_progress", i, total)           manual scan progress
    ("scan_done", data_dict)              manual scan finished
"""

import queue
import threading

stop_event = threading.Event()          # set on window close; stops the monitor loop
autolock_engaged = threading.Event()    # background trigger-and-relock is armed
autolock_running = threading.Event()    # an autolock scan is sweeping control out right now
manual_scan_active = threading.Event()  # a manual scan is sweeping control out right now
gui_queue = queue.Queue()


def log(message):
    """Send a line to the GUI's live log (safe to call from any thread)."""
    gui_queue.put(("log", message))


def busy_reason():
    """
    Return why control out can't be driven from the GUI right now (a scan
    or autolock is sweeping it, or autolock is armed and may start one at
    any moment), or None if it's free.
    """
    if manual_scan_active.is_set():
        return "A manual scan is running."
    if autolock_running.is_set():
        return "An autolock scan is running."
    if autolock_engaged.is_set():
        return "Autolock is engaged - disengage it first."
    return None
