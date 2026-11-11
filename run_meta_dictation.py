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
    kCGEventFlagMaskAlternate,
    kCGEventFlagsChanged,
    kCGHIDEventTap,
    kCGNullWindowID,
    kCGWindowListOptionOnScreenOnly,
)

import audio_input
from dictation_common import Driver, run_tool

PREFS = "com.meta.endo.vanilla"
RIGHT_OPTION = 61
RIGHT_OPTION_DOWN = kCGEventFlagMaskAlternate | 0x40 | 0x100
NO_FLAGS = 0x100


def _right_option(down: bool) -> None:
    ev = CGEventCreateKeyboardEvent(None, RIGHT_OPTION, down)
    CGEventSetType(ev, kCGEventFlagsChanged)
    CGEventSetFlags(ev, RIGHT_OPTION_DOWN if down else NO_FLAGS)
    CGEventPost(kCGHIDEventTap, ev)


def tap_right_option() -> float:
    try:
        _right_option(True)
        key_down = time.monotonic()
        time.sleep(0.06)
        return key_down
    finally:
        _right_option(False)


def overlay_visible() -> bool:
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerName") == "Meta AI" and (w.get("kCGWindowLayer") or 0) > 0:
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 999) < 300 and b.get("Height", 999) < 150:
                return True
    return False


def wait_overlay(visible: bool, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if overlay_visible() == visible:
            return time.monotonic() - t0
        time.sleep(0.05)
    return None


def read_pref(key: str) -> str:
    return subprocess.run(["defaults", "read", PREFS, key], capture_output=True, text=True).stdout.strip()


class Meta(Driver):
    name = "meta"
    extra_fields = ["meta_mic", "meta_toggle_key", "meta_dictation_enabled"]
    precise_timing = True
    audio_obj = None

    def add_args(self, p) -> None:
        p.add_argument("--start-timeout", type=float, default=5.0,
                       help="max wait for Meta AI's listening overlay after the tap (s)")
        p.add_argument("--stop-timeout", type=float, default=20.0,
                       help="max wait for the overlay to close after the stop tap (s)")

    def preflight(self, args) -> None:
        self.start_timeout, self.stop_timeout = args.start_timeout, args.stop_timeout
        self.active = False
        if subprocess.run(["pgrep", "-x", "Meta AI"], capture_output=True).returncode != 0:
            raise SystemExit("Meta AI isn't running. Open it and sign in.")
        if overlay_visible():
            raise SystemExit("Meta AI is already dictating - stop it first.")

    def env(self) -> dict:
        return {"meta_mic": read_pref("endo_input_device_uid"),
                "meta_toggle_key": read_pref("endo_vanilla_dictation_toggle_key"),
                "meta_dictation_enabled": read_pref("endo_vanilla_dictation_enabled")}

    def env_problem(self, env: dict) -> str | None:
        if env.get("meta_dictation_enabled") != "1":
            return "Meta AI dictation is off (Settings > Dictation > Enable dictation)"
        if "blackhole" not in env.get("meta_mic", "").lower():
            return f"Meta AI's input device is '{env.get('meta_mic')}', not BlackHole 2ch (Settings > Dictation > Device)"
        if "rightoption" not in env.get("meta_toggle_key", "").lower():
            return f"Meta AI's Tap to talk key is '{env.get('meta_toggle_key')}', not Right Option"
        return None

    def before_clip(self) -> None:
        if overlay_visible() and wait_overlay(False, self.stop_timeout) is None:
            from dictation_common import DictationError
            raise DictationError("Meta AI's dictation overlay is still open from an earlier attempt")

    def start(self) -> float | None:
        tap_right_option()
        waited = wait_overlay(True, self.start_timeout)
        self.active = waited is not None
        return waited

    def listening(self) -> bool | None:
        if self.audio_obj is None:
            pids = subprocess.run(["pgrep", "-x", "Meta AI"], capture_output=True, text=True).stdout.split()
            self.audio_obj = audio_input.process_object(int(pids[0])) if pids else None
        return audio_input.process_input_running(self.audio_obj) if self.audio_obj else None

    def stop_received(self) -> bool:
        return self.listening() is False

    def stop(self) -> float | None:
        if not self.active:
            return None
        self.stop_sent_t = tap_right_option()
        self.active = False
        return None

    def reset(self) -> None:
        self.stop()
        if self.listening():
            tap_right_option()
            time.sleep(1.0)


if __name__ == "__main__":
    run_tool(Meta())
