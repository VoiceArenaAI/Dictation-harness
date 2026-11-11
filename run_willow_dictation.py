# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-Quartz",
#                 "pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa"]
# ///
import json
import subprocess
import sys
import time
from pathlib import Path

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetType,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskShift,
    kCGEventFlagsChanged,
    kCGHIDEventTap,
)

import audio_input
from dictation_common import Driver, log, run_tool

WILLOW_BUNDLE = "com.seewillow.WillowMac"


def willow_pid() -> int | None:
    from AppKit import NSWorkspace
    pids = [a.processIdentifier() for a in NSWorkspace.sharedWorkspace().runningApplications()
            if a.bundleIdentifier() == WILLOW_BUNDLE]
    return pids[0] if pids else None


def dismiss_willow_popups() -> int:
    from AppKit import NSWorkspace
    from ApplicationServices import (AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
                                     AXUIElementPerformAction)
    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.bundleIdentifier() == WILLOW_BUNDLE]
    if not apps:
        return 0

    def get(e, attr):
        err, v = AXUIElementCopyAttributeValue(e, attr, None)
        return v if err == 0 else None

    pressed = 0

    def walk(e, depth=0):
        nonlocal pressed
        if depth > 30:
            return
        if get(e, "AXRole") == "AXButton" and "Dismiss" in (str(get(e, "AXTitle") or "") + str(get(e, "AXDescription") or "")):
            if AXUIElementPerformAction(e, "AXPress") == 0:
                pressed += 1
            return
        for c in get(e, "AXChildren") or []:
            walk(c, depth + 1)

    for w in get(AXUIElementCreateApplication(apps[0].processIdentifier()), "AXWindows") or []:
        walk(w)
    return pressed

TRANSCRIPTS = Path.home() / "Library/Application Support/com.seewillow.WillowMac/Transcripts"
MODIFIERS = {
    "cmd": (55, kCGEventFlagMaskCommand),
    "ctrl": (59, kCGEventFlagMaskControl),
    "alt": (58, kCGEventFlagMaskAlternate),
    "shift": (56, kCGEventFlagMaskShift),
}
KEYS = {"`": 50, "space": 49, **{c: k for c, k in zip("asdfhgzxcv", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9))},
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35,
        "l": 37, "j": 38, "k": 40, "n": 45, "m": 46}


def parse_hotkey(spec: str) -> tuple[list[str], int]:
    *mods, key = spec.lower().split("+")
    bad = [m for m in mods if m not in MODIFIERS]
    if bad or key not in KEYS:
        sys.exit(f"Bad hotkey '{spec}'. Modifiers: {list(MODIFIERS)}, keys: {list(KEYS)}")
    return mods, KEYS[key]


def _post(code: int, down: bool, flags: int, modifier: bool = False) -> float:
    ev = CGEventCreateKeyboardEvent(None, code, down)
    if modifier:
        CGEventSetType(ev, kCGEventFlagsChanged)
    CGEventSetFlags(ev, flags)
    CGEventPost(kCGHIDEventTap, ev)
    posted = time.monotonic()
    time.sleep(0.04)
    return posted


def tap_hotkey(mods: list[str], key: int) -> float:
    flags = 0
    pressed = []
    try:
        for m in mods:
            flags |= MODIFIERS[m][1]
            _post(MODIFIERS[m][0], True, flags, modifier=True)
            pressed.append(m)
        key_down = _post(key, True, flags)
        _post(key, False, flags)
    finally:
        for m in reversed(pressed):
            flags &= ~MODIFIERS[m][1]
            _post(MODIFIERS[m][0], False, flags, modifier=True)
    return key_down


def transcript_files() -> set[Path]:
    return set(TRANSCRIPTS.glob("*.json")) if TRANSCRIPTS.exists() else set()


class Willow(Driver):
    name = "willow"
    extra_fields = ["willow_text", "willow_status", "recording_s"]
    precise_timing = True

    def add_args(self, p) -> None:
        p.add_argument("--hotkey", default="cmd+`", help="Willow's hands-free (toggle) dictation hotkey")
        p.add_argument("--ready-wait", type=float, default=0.5,
                       help="fixed wait for Willow to start listening; it gives no ready signal (s)")

    def preflight(self, args) -> None:
        self.mods, self.key = parse_hotkey(args.hotkey)
        self.ready_wait = args.ready_wait
        self.active = False
        self.before: set[Path] = set()
        if subprocess.run(["pgrep", "-if", "Willow"], capture_output=True).returncode != 0:
            raise SystemExit("Willow isn't running. Open it and finish onboarding.")
        if not TRANSCRIPTS.exists():
            log(f"(Willow transcript folder not found at {TRANSCRIPTS}; willow_* columns will be blank)")

    def before_clip(self) -> None:
        self.before = transcript_files()
        pid = willow_pid()
        self.audio_obj = audio_input.process_object(pid) if pid else None

    def listening(self) -> bool | None:
        return audio_input.process_input_running(self.audio_obj) if self.audio_obj else None

    def stop_received(self) -> bool:
        return self.listening() is False

    def start(self) -> float | None:
        tap_hotkey(self.mods, self.key)
        self.active = True
        time.sleep(self.ready_wait)
        return self.ready_wait

    def stop(self) -> float | None:
        if self.active:
            self.stop_sent_t = tap_hotkey(self.mods, self.key)
            self.active = False
        return None

    def reset(self) -> None:
        self.stop()
        pid = willow_pid()
        obj = audio_input.process_object(pid) if pid else None
        if obj and audio_input.process_input_running(obj):
            tap_hotkey(self.mods, self.key)
            time.sleep(1.0)
        try:
            if (n := dismiss_willow_popups()):
                log(f"  dismissed {n} Willow popup(s)")
                time.sleep(0.5)
        except Exception as e:
            log(f"  (couldn't check Willow popups: {e})")

    def after_clip(self, started_utc, stop_mono) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            new = transcript_files() - self.before
            if new:
                path = max(new, key=lambda p: p.stat().st_mtime)
                for _ in range(10):
                    try:
                        rec = json.loads(path.read_text())
                        dur = rec.get("recordingDuration")
                        return {"willow_text": rec.get("text", ""), "willow_status": rec.get("status", ""),
                                "recording_s": f"{dur:.2f}" if dur else ""}
                    except (json.JSONDecodeError, OSError):
                        time.sleep(0.2)
            time.sleep(0.1)
        return {}


if __name__ == "__main__":
    run_tool(Willow())
