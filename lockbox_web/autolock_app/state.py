"""
Cross-thread state shared between the web service and the background workers.

Workers report everything through event_queue; the service's pump thread
(server.pump_worker_queue) turns each item into an event for the browsers.
Message shapes:

    ("reading", timestamp, values_dict)   a new set of physical readings
    ("log", message)                      a line for the live log
    ("scan_progress", fraction)           manual scan progress, 0-1 of the voltage range
    ("scan_done", data_dict)              manual scan finished
"""

import queue
import threading

stop_event = threading.Event()          # set on shutdown; stops the monitor loop and scans
autolock_engaged = threading.Event()    # background trigger-and-relock is armed
autolock_running = threading.Event()    # an autolock scan is sweeping control out right now
manual_scan_active = threading.Event()  # a manual scan is sweeping control out right now
lock_acquiring = threading.Event()      # "Lock to Selected" is following a crossing onto lock
stop_scan_requested = threading.Event() # "Stop Scan" pressed: the running sweep stops at its next point
event_queue = queue.Queue()


def log(message):
    """Send a line to the live log on the page (safe to call from any thread)."""
    event_queue.put(("log", message))


def busy_reason():
    """
    Return why control out can't be driven by a command right now (a scan
    or autolock is sweeping it, or autolock is armed and may start one at
    any moment), or None if it's free.
    """
    if manual_scan_active.is_set():
        return "A manual scan is running."
    if autolock_running.is_set():
        return "An autolock scan is running."
    if lock_acquiring.is_set():
        return "Locking to the selected crossing."
    if autolock_engaged.is_set():
        return "Autolock is engaged - disengage it first."
    return None
