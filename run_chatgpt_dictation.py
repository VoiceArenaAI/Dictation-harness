# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-ApplicationServices",
#                 "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
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

BUNDLE_ID = "com.openai.codex"
APP_PATH = "/Applications/ChatGPT.app/"
KEY_A, KEY_DELETE, KEY_ESCAPE = 0, 51, 53


class ChatGPTApp:
    def __init__(self) -> None:
        apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.bundleIdentifier() == BUNDLE_ID]
        if not apps:
            sys.exit("ChatGPT desktop isn't running.")
        self.pid = apps[0].processIdentifier()
        self.proc_name = str(apps[0].localizedName())
        from Foundation import NSBundle
        b = NSBundle.bundleWithURL_(apps[0].bundleURL())
        self.version = str(b.objectForInfoDictionaryKey_("CFBundleShortVersionString") or "") if b else ""
        self.ax = AXUIElementCreateApplication(self.pid)
        AXUIElementSetAttributeValue(self.ax, "AXEnhancedUserInterface", True)
        AXUIElementSetAttributeValue(self.ax, "AXManualAccessibility", True)
        time.sleep(1.5)

    @staticmethod
    def get(el, attr):
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None

    def labels(self, el) -> set[str]:
        return {str(x) for x in (self.get(el, "AXTitle"), self.get(el, "AXDescription")) if x}

    def find_all(self, pred, limit: int = 0) -> list:
        out = []

        def walk(el, depth=0):
            if depth > 90 or (limit and len(out) >= limit):
                return
            if pred(el):
                out.append(el)
            for child in self.get(el, "AXChildren") or []:
                walk(child, depth + 1)

        for win in self.get(self.ax, "AXWindows") or []:
            walk(win)
        return out

    def find(self, pred):
        hits = self.find_all(pred, limit=1)
        return hits[0] if hits else None

    def button(self, name: str):
        return self.find(lambda e: self.get(e, "AXRole") == "AXButton" and name in self.labels(e))

    def wait_button(self, name: str, timeout: float) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.button(name) is not None:
                return time.monotonic() - t0
            time.sleep(0.1)
        return None

    def press(self, name: str, timeout: float = 5.0) -> None:
        if "send" in name.lower():
            raise RuntimeError(f"refusing to press '{name}': this run never sends anything")
        if self.wait_button(name, timeout) is None:
            raise DictationError(f"ChatGPT button '{name}' not found")
        AXUIElementPerformAction(self.button(name), "AXPress")

    def mode(self) -> str:
        pop = self.find(lambda e: self.get(e, "AXRole") == "AXPopUpButton"
                        and any(l.startswith("Switch mode") for l in self.labels(e)))
        lab = next((l for l in self.labels(pop) if "current mode:" in l), "") if pop is not None else ""
        return lab.split("current mode:")[-1].strip()

    def ensure_chat(self) -> None:
        if self.mode() != "ChatGPT":
            pop = self.find(lambda e: self.get(e, "AXRole") == "AXPopUpButton"
                            and any(l.startswith("Switch mode") for l in self.labels(e)))
            if pop is None:
                raise SystemExit("Couldn't find ChatGPT's mode switch.")
            AXUIElementPerformAction(pop, "AXPress")
            time.sleep(1.2)
            item = self.find(lambda e: self.get(e, "AXRole") == "AXMenuItem"
                             and any(l.startswith("ChatGPT") for l in self.labels(e)))
            if item is None:
                raise SystemExit("Couldn't find the 'ChatGPT' mode in the mode switch.")
            AXUIElementPerformAction(item, "AXPress")
            time.sleep(2)
            log("switched ChatGPT to ChatGPT mode (from Codex)")
        chat = self.find(lambda e: self.get(e, "AXRole") == "AXCheckBox" and "Chat" in self.labels(e))
        if chat is not None and self.get(chat, "AXValue") not in (1, True):
            AXUIElementPerformAction(chat, "AXPress")
            time.sleep(1.5)
            log("switched the composer to Chat (from Work)")

    def composer(self):
        return self.find(lambda e: self.get(e, "AXRole") == "AXTextArea")

    def composer_text(self, c) -> str:
        val = str(self.get(c, "AXValue") or "").strip()
        if val and val in {l.strip() for l in self.labels(c)} | {str(self.get(c, "AXPlaceholderValue") or "").strip()}:
            return ""
        return val

    def text(self) -> str:
        c = self.composer()
        return self.composer_text(c) if c is not None else ""

    def model(self) -> str:
        pop = self.find(lambda e: self.get(e, "AXRole") == "AXPopUpButton" and "Select ChatGPT model" in self.labels(e))
        if pop is None:
            return ""
        vals = []

        def walk(el, d=0):
            if d > 6:
                return
            if self.get(el, "AXRole") == "AXStaticText" and self.get(el, "AXValue"):
                vals.append(str(self.get(el, "AXValue")))
            for ch in self.get(el, "AXChildren") or []:
                walk(ch, d + 1)

        walk(pop)
        return " ".join(vals).strip()

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
            raise DictationError("ChatGPT prompt box not found (still recording/transcribing?)")
        AXUIElementSetAttributeValue(c, "AXFocused", True)
        time.sleep(0.1)
        self._key(KEY_A, kCGEventFlagMaskCommand)
        self._key(KEY_DELETE)
        time.sleep(0.3)

    def idle(self) -> bool:
        return self.button("Dictate") is not None


class ChatGPTTarget(Target):
    def __init__(self, app: ChatGPTApp) -> None:
        self.app = app

    def prepare(self) -> None:
        self.app.activate()
        for i in range(4):
            self.app.clear()
            time.sleep(0.15 if i == 0 else 1.5)
            if not self.read().strip():
                return
        raise DictationError("ChatGPT's prompt box keeps refilling after clearing")

    def read(self) -> str:
        fp = frontmost_app()
        if fp != self.app.proc_name:
            raise FocusLost(f"'{fp}' took focus")
        return self.app.text()

    def fast_reader(self):
        app = self.app
        box = app.composer()
        if box is None or app.composer_text(box):
            raise DictationError("couldn't find ChatGPT's empty prompt box through Accessibility")
        parent = app.get(box, "AXParent")

        def find(el, depth=0):
            if depth > 8:
                return None
            if app.get(el, "AXRole") == "AXTextArea":
                return el
            for child in app.get(el, "AXChildren") or []:
                hit = find(child, depth + 1)
                if hit is not None:
                    return hit
            return None

        def read():
            if app.get(parent, "AXRole") is None:
                return None
            current = find(parent)
            return "" if current is None else app.composer_text(current)

        return read, front_window_guard(lambda owner: owner == app.proc_name)

    def close(self) -> None:
        try:
            if self.app.idle():
                self.app.clear()
        except Exception:
            pass


class ChatGPT(Driver):
    name = "chatgpt"
    extra_fields = ["chatgpt_app", "chatgpt_mode", "chatgpt_composer", "chatgpt_model", "chatgpt_plan", "system_input"]
    precise_timing = True
    has_stop_ui = True
    stop_btn = None

    def preflight(self, args) -> None:
        self.app = ChatGPTApp()
        self.capture = audio_input.CaptureFlag(APP_PATH)
        self.old_input = None
        self.device = args.device
        self._env = {}

    def env(self) -> dict:
        return self._env

    def env_problem(self, env: dict) -> str | None:
        if env and env.get("chatgpt_mode") != "ChatGPT":
            return f"ChatGPT is in '{env.get('chatgpt_mode')}' mode, not ChatGPT"
        if env and env.get("system_input") and "blackhole" not in env["system_input"].lower():
            return f"system default input is '{env['system_input']}', not BlackHole"
        return None

    def make_target(self, args) -> Target:
        if not self.app.get(self.app.ax, "AXWindows"):
            subprocess.run(["open", "-b", BUNDLE_ID])
            time.sleep(3)
        self.app.activate()
        self.app.ensure_chat()
        if self.app.wait_button("New chat", 10) is None:
            raise SystemExit("Couldn't find ChatGPT's 'New chat' button - is the main window open?")
        self.app.press("New chat")
        time.sleep(2.5)
        self.app.ensure_chat()
        self.reset()
        if self.app.composer() is None or not self.app.idle():
            raise SystemExit("Couldn't find ChatGPT's prompt box / Dictate button.")
        return ChatGPTTarget(self.app)

    def setup(self, args, target) -> None:
        self.old_input = audio_input.get_default_input()
        bh, bh_name = audio_input.find_input(self.device)
        audio_input.set_default_input(bh)
        log(f"system default input -> {bh_name} (restored at the end)")
        self._env = {"chatgpt_app": self.app.version, "chatgpt_mode": self.app.mode(),
                     "chatgpt_composer": "Chat", "chatgpt_model": self.app.model(),
                     "chatgpt_plan": "free" if (self.app.button("Get Plus") or self.app.button("Upgrade")) else "paid (Plus)",
                     "system_input": audio_input.device_name(audio_input.get_default_input())}
        log("ChatGPT settings: " + ", ".join(f"{k}={v}" for k, v in self._env.items()))
        if (why := self.env_problem(self._env)):
            raise SystemExit(why)

    def start(self) -> float | None:
        if self.app.button("Retry dictation") is not None:
            self.reset()
        self.app.press("Dictate")
        waited = self.app.wait_button("Stop dictation", 10)
        if waited is None or not self.capture.wait(3):
            return None
        self.stop_btn = self.app.button("Stop dictation")
        return waited if self.stop_btn is not None else None

    def before_clip(self) -> None:
        self.capture.refresh()
        self.stop_btn = None
        if self.capture.find():
            raise DictationError("ChatGPT was still capturing before the clip started")

    def listening(self) -> bool | None:
        return self.capture.running()

    def stop_ui_changed(self) -> bool:
        return self.app.get(self.stop_btn, "AXEnabled") is not True

    def stop(self) -> float | None:
        button = self.stop_btn

        def press() -> None:
            if AXUIElementPerformAction(button, "AXPress"):
                self.app.press("Stop dictation")
            self.stop_sent_t = time.monotonic()

        threading.Thread(target=press, daemon=True).start()
        return None

    def reset(self) -> None:
        try:
            if self.app.button("Retry dictation") is not None:
                self.app.press("Dismiss")
                self.app.wait_button("Dictate", 5)
                return
            for name in ("Cancel dictation", "Cancel transcription", "Starting dictation; click to cancel"):
                if self.app.button(name) is not None:
                    self.app.press(name)
                    self.app.wait_button("Dictate", 10)
                    return
        except DictationError:
            pass

    def teardown(self) -> None:
        if self.old_input is not None:
            audio_input.set_default_input(self.old_input)
            log(f"default input restored to {audio_input.device_name(self.old_input)}")
        try:
            AXUIElementSetAttributeValue(self.app.ax, "AXEnhancedUserInterface", False)
        except Exception:
            pass


if __name__ == "__main__":
    run_tool(ChatGPT())
