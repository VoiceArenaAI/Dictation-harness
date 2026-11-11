# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-Quartz",
#                 "pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa"]
# ///
import subprocess
import time

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetType,
    CGWindowListCopyWindowInfo,
    kCGEventFlagMaskSecondaryFn,
    kCGEventFlagsChanged,
    kCGHIDEventTap,
    kCGNullWindowID,
    kCGWindowListOptionOnScreenOnly,
)

import audio_input
from dictation_common import Driver, run_tool

FN = 63
FN_DOWN_FLAGS = kCGEventFlagMaskSecondaryFn | 0x100
FN_UP_FLAGS = 0x100


def _fn(down: bool) -> None:
    ev = CGEventCreateKeyboardEvent(None, FN, down)
    CGEventSetType(ev, kCGEventFlagsChanged)
    CGEventSetFlags(ev, FN_DOWN_FLAGS if down else FN_UP_FLAGS)
    CGEventPost(kCGHIDEventTap, ev)


def tap_fn(times: int) -> float:
    key_down = 0.0
    try:
        for i in range(times):
            _fn(True)
            key_down = key_down or time.monotonic()
            time.sleep(0.05)
            _fn(False)
            if i < times - 1:
                time.sleep(0.15)
        return key_down
    finally:
        _fn(False)


def pill_visible() -> bool:
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerName") == "Granola" and w.get("kCGWindowName") == "Dictation":
            return True
    return False


def wait_pill(visible: bool, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pill_visible() == visible:
            return time.monotonic() - t0
        time.sleep(0.05)
    return None


class Granola(Driver):
    name = "granola"
    precise_timing = True
    audio_obj = None
    pids: set[int] = set()

    def add_args(self, p) -> None:
        p.add_argument("--start-timeout", type=float, default=5.0,
                       help="max wait for Granola's Dictation pill to appear after the shortcut (s)")
        p.add_argument("--stop-timeout", type=float, default=20.0,
                       help="max wait for the pill to close after the stop tap (s)")

    def preflight(self, args) -> None:
        self.start_timeout, self.stop_timeout = args.start_timeout, args.stop_timeout
        self.active = False
        if subprocess.run(["pgrep", "-x", "Granola"], capture_output=True).returncode != 0:
            raise SystemExit("Granola isn't running. Open it, sign in and turn on Settings > Voice typing.")

    def before_clip(self) -> None:
        if pill_visible() and wait_pill(False, self.stop_timeout) is None:
            from dictation_common import DictationError
            raise DictationError("Granola's Dictation pill is still open from an earlier attempt")
        self.audio_obj = None
        self.pids = {int(p) for p in subprocess.run(["pgrep", "-f", "Granola"], capture_output=True, text=True).stdout.split()}

    def start(self) -> float | None:
        tap_fn(2)
        waited = wait_pill(True, self.start_timeout)
        self.active = waited is not None
        return waited

    def listening(self) -> bool | None:
        if self.audio_obj is None:
            self.audio_obj = next((o for p, o in audio_input.capturing_processes().items() if p in self.pids), None)
            return True if self.audio_obj else None
        return audio_input.process_input_running(self.audio_obj)

    def stop_received(self) -> bool:
        return self.listening() is False

    def stop(self) -> float | None:
        if not self.active:
            return None
        self.stop_sent_t = tap_fn(1)
        self.active = False
        return None

    def reset(self) -> None:
        self.stop()


if __name__ == "__main__":
    run_tool(Granola())
