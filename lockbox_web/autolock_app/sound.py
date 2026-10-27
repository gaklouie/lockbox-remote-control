"""
Relock sound: play static/laser-relocked.mp3 through the Pi's audio output
(by default the 3.5 mm headphone jack) when autolock locks successfully.

Uses the `mpg123` command-line player (sudo apt install mpg123), started in
the background so playback never delays the lock. With RELOCK_SOUND_DEVICE
set, mpg123 plays straight to that ALSA device - "plughw:CARD=Headphones" is
the 3.5 mm jack on a Pi 3/4, whatever the system's default output is. Leave it
blank to use the system default output. Problems are reported in the log.
"""

import shutil
import subprocess
import threading
from pathlib import Path

from .config import settings

SOUND_FILE = Path(__file__).resolve().parent.parent / "static" / "laser-relocked.mp3"


def _report_if_failed(process, log_fn):
    try:
        _, err = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        log_fn("Relock sound: the player didn't finish within 60 s and was stopped.")
        return
    if process.returncode != 0:
        detail = (err or b"").decode(errors="replace").strip().splitlines()
        log_fn(f"Relock sound failed (mpg123 exit code {process.returncode})"
               f"{': ' + detail[-1] if detail else ''}. Check the sound output device in Settings.")


def play_relock_sound(log_fn, force=False):
    """
    Play the relock sound in the background if PLAY_RELOCK_SOUND is on
    (or force=True, for the test button). Never raises.
    """
    if not (force or settings.PLAY_RELOCK_SOUND):
        return
    if not SOUND_FILE.exists():
        log_fn(f"Relock sound: {SOUND_FILE} not found.")
        return
    player = shutil.which("mpg123")
    if player is None:
        log_fn("Relock sound: mpg123 is not installed (on the Pi: sudo apt install mpg123).")
        return
    command = [player, "-q"]
    device = settings.RELOCK_SOUND_DEVICE.strip()
    if device:
        command += ["-o", "alsa", "-a", device]
    command.append(str(SOUND_FILE))
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
    except OSError as exc:
        log_fn(f"Relock sound: couldn't start mpg123: {exc}")
        return
    threading.Thread(target=_report_if_failed, args=(process, log_fn), daemon=True).start()
