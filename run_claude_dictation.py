# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-ApplicationServices",
#                 "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
import re
import subprocess
import sys
import threading
import time

from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
)
from Quartz import CGEventCreateKeyboardEvent, CGEventPostToPid, CGEventSetFlags, kCGEventFlagMaskCommand

import audio_input
from dictation_common import DictationError, Driver, FocusLost, Target, front_window_guard, frontmost_app, log, run_tool

BUNDLE_ID = "com.anthropic.claudefordesktop"
APP_MATCH = "Claude.app/Contents"
KEY_A, KEY_DELETE, KEY_ESCAPE = 0, 51, 53


class ClaudeApp:
    def __init__(self) -> None:
        apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.bundleIdentifier() == BUNDLE_ID]
        if not apps:
            sys.exit("Claude desktop isn't running.")
        self.pid = apps[0].processIdentifier()
        self.proc_name = str(apps[0].localizedName())
        self.ax = AXUIElementCreateApplication(self.pid)
        AXUIElementSetAttributeValue(self.ax, "AXManualAccessibility", True)

    @staticmethod
    def get(el, attr):
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None

    def label(self, el) -> str:
        return " ".join(str(x) for x in (self.get(el, "AXTitle"), self.get(el, "AXDescription")) if x)

    def find(self, pred):
        out = []

        def walk(el, depth=0):
            if depth > 80 or out:
                return
            if pred(el):
                out.append(el)
            for child in self.get(el, "AXChildren") or []:
                walk(child, depth + 1)

        for win in self.get(self.ax, "AXWindows") or []:
            walk(win)
        return out[0] if out else None

    ALIASES = {"Dictate": ("Dictate", "Dictate again")}

    def button(self, name: str):
        names = self.ALIASES.get(name, (name,))
        return self.find(lambda e: self.get(e, "AXRole") == "AXButton" and self.label(e) in names)

    def wait_button(self, name: str, timeout: float) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.button(name) is not None:
                return time.monotonic() - t0
            time.sleep(0.1)
        return None

    def press(self, name: str, timeout: float = 5.0) -> None:
        if self.wait_button(name, timeout) is None:
            raise DictationError(f"Claude button '{name}' not found")
        AXUIElementPerformAction(self.button(name), "AXPress")

    def chat_mode(self) -> None:
        rb = self.find(lambda e: self.get(e, "AXRole") == "AXRadioButton"
                       and "Claude" in (self.get(e, "AXTitle"), self.get(e, "AXDescription")))
        if rb is None:
            return
        if self.get(rb, "AXValue") not in (1, True):
            AXUIElementPerformAction(rb, "AXPress")
            time.sleep(2.0)
            log("switched Claude to chat mode")

    def mic_settings(self) -> dict:
        pop = self.find(lambda e: self.get(e, "AXRole") == "AXPopUpButton"
                        and self.get(e, "AXDescription") == "Microphone")
        if pop is None:
            return {}
        AXUIElementPerformAction(pop, "AXPress")
        time.sleep(1.0)
        mic, lang, items = "", "", []

        def walk(el, depth=0):
            if depth > 80:
                return
            if self.get(el, "AXRole") == "AXMenuItem":
                items.append(el)
            for child in self.get(el, "AXChildren") or []:
                walk(child, depth + 1)

        for win in self.get(self.ax, "AXWindows") or []:
            walk(win)
        for it in items:
            title = str(self.get(it, "AXTitle") or "")
            if title.startswith("Language"):
                lang = title[len("Language"):].strip()
            elif self.get(it, "AXMenuItemMarkChar") or self.get(it, "AXValue") in (1, True):
                mic = title
        self._key(KEY_ESCAPE)
        time.sleep(0.5)
        return {"claude_mic": mic, "claude_lang": lang}

    def composer(self):
        return self.find(lambda e: self.get(e, "AXRole") == "AXTextArea"
                         and re.search("prompt", str(self.get(e, "AXDescription")), re.I))

    def text(self) -> str:
        c = self.composer()
        return str(self.get(c, "AXValue") or "").strip() if c is not None else ""

    def activate(self) -> None:
        subprocess.run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'])
        time.sleep(0.5)

    def _key(self, code: int, flags: int = 0) -> None:
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, code, down)
            CGEventSetFlags(ev, flags)
            CGEventPostToPid(self.pid, ev)
            time.sleep(0.03)

    def clear(self) -> None:
        c = self.composer()
        if c is None:
            raise DictationError("Claude composer not found")
        AXUIElementSetAttributeValue(c, "AXFocused", True)
        time.sleep(0.1)
        self._key(KEY_A, kCGEventFlagMaskCommand)
        self._key(KEY_DELETE)
        time.sleep(0.3)


class ClaudeTarget(Target):
    def __init__(self, app: ClaudeApp) -> None:
        self.app = app

    def prepare(self) -> None:
        self.app.activate()
        for i in range(4):
            self.app.clear()
            time.sleep(0.15 if i == 0 else 1.5)
            if not self.read().strip():
                return
        raise DictationError("Claude's prompt box keeps refilling after clearing")

    def read(self) -> str:
        fp = frontmost_app()
        if fp != self.app.proc_name:
            raise FocusLost(f"'{fp}' took focus")
        return self.app.text()

    def fast_reader(self):
        app = self.app
        box = app.composer()
        if box is None or str(app.get(box, "AXValue") or "").strip():
            raise DictationError("couldn't find Claude's empty prompt box through Accessibility")

        def read():
            val = app.get(box, "AXValue")
            return None if val is None else str(val).strip()

        return read, front_window_guard(lambda owner: owner == app.proc_name)

    def close(self) -> None:
        try:
            self.app.clear()
        except Exception:
            pass


class Claude(Driver):
    name = "claude"
    precise_timing = True
    live_typing = True
    has_stop_ui = True
    stop_btn = None

    def preflight(self, args) -> None:
        self.app = ClaudeApp()
        self.capture = audio_input.CaptureFlag(APP_MATCH)
        self.old_input = None
        self.device = args.device
        self.language = args.language

    extra_fields = ["claude_mic", "claude_lang"]

    def env(self) -> dict:
        return getattr(self, "_env", {})

    def add_args(self, p) -> None:
        p.add_argument("--language", default="English",
                       help="required Claude dictation language, as its Microphone menu names it ('' = don't check)")

    def env_problem(self, env: dict) -> str | None:
        if self.language and env and env.get("claude_lang") != self.language:
            return (f"Claude's dictation language is '{env.get('claude_lang')}', not '{self.language}' "
                    "(composer > Microphone menu > Language)")
        mic = env.get("claude_mic", "")
        if mic and not (mic.startswith("Default") or "blackhole" in mic.lower()):
            return (f"Claude's microphone is pinned to '{mic}'; set the composer's Microphone menu to "
                    "Default (the run points the system default at BlackHole) or to BlackHole 2ch")
        return None

    def make_target(self, args) -> Target:
        if not self.app.get(self.app.ax, "AXWindows"):
            subprocess.run(["open", "-b", BUNDLE_ID])
            time.sleep(3)
        self.app.activate()
        self.app.chat_mode()
        if self.app.wait_button("New", 10) is None:
            raise SystemExit("Couldn't find Claude's 'New' chat button - is the main chat window open?")
        self.app.press("New")
        time.sleep(2.5)
        if self.app.composer() is None:
            raise SystemExit("Couldn't find Claude's prompt box.")
        return ClaudeTarget(self.app)

    def setup(self, args, target) -> None:
        self.old_input = audio_input.get_default_input()
        bh, bh_name = audio_input.find_input(self.device)
        audio_input.set_default_input(bh)
        log(f"system default input -> {bh_name} (restored at the end)")
        for _ in range(5):
            self._env = self.app.mic_settings()
            if self._env.get("claude_lang"):
                break
            time.sleep(2)
        log("Claude microphone menu: " + (", ".join(f"{k}={v}" for k, v in self._env.items()) or "not found"))
        if self.language and not self._env.get("claude_lang"):
            raise SystemExit("Couldn't read Claude's dictation language from the composer's Microphone menu, "
                             "so it can't be confirmed - rerun (or pass --language '' to skip the check)")
        if (why := self.env_problem(self._env)):
            raise SystemExit(why)

    def before_clip(self) -> None:
        self.capture.refresh()
        self.stop_btn = None
        if self.capture.find():
            raise DictationError("Claude was still capturing before the clip started")

    def start(self) -> float | None:
        self.app.press("Dictate")
        waited = self.app.wait_button("Finish dictation", 10)
        if waited is None or not self.capture.wait(3):
            return None
        return waited

    def listening(self) -> bool | None:
        return self.capture.running()

    def stop_ui_changed(self) -> bool:
        return self.stop_btn is not None and self.app.get(self.stop_btn, "AXRole") is None

    def stop(self) -> float | None:

        def press() -> None:
            button = self.app.button("Finish dictation")
            if button is None:
                return
            self.stop_btn = button
            AXUIElementPerformAction(button, "AXPress")
            self.stop_sent_t = time.monotonic()

        threading.Thread(target=press, daemon=True).start()
        return None

    def reset(self) -> None:
        try:
            if self.app.button("Finish dictation") is not None:
                self.app.press("Finish dictation")
                if self.app.wait_button("Dictate", 15) is not None:
                    return
            if self.app.button("Cancel dictation") is not None:
                self.app.press("Cancel dictation")
                self.app.wait_button("Dictate", 5)
        except DictationError:
            pass

    def teardown(self) -> None:
        if self.old_input is not None:
            audio_input.set_default_input(self.old_input)
            log(f"default input restored to {audio_input.device_name(self.old_input)}")


if __name__ == "__main__":
    run_tool(Claude())
