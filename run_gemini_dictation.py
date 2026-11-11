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
from dictation_common import DictationError, Driver, FocusLost, Target, front_window_guard, frontmost_app, log, osa, run_tool

BUNDLE_ID = "com.google.GeminiMacOS"
APP_PATH = "/Applications/Gemini.app/"
PROC = "Gemini"
KEY_A, KEY_DELETE, KEY_W, KEY_ESCAPE = 0, 51, 13, 53


def local_asr_pids() -> set[str]:
    out = subprocess.run(["pgrep", "-f", "localspeechrecognition"], capture_output=True, text=True).stdout
    return set(out.split())


class GeminiApp:
    def __init__(self) -> None:
        apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.bundleIdentifier() == BUNDLE_ID]
        if not apps:
            sys.exit("Gemini isn't running.")
        self.pid = apps[0].processIdentifier()
        self.proc_name = str(apps[0].localizedName())
        from Foundation import NSBundle
        b = NSBundle.bundleWithURL_(apps[0].bundleURL())
        self.version = str(b.objectForInfoDictionaryKey_("CFBundleShortVersionString") or "") if b else ""
        self.ax = AXUIElementCreateApplication(self.pid)

    @staticmethod
    def get(el, attr):
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None

    def labels(self, el) -> set[str]:
        return {str(x) for x in (self.get(el, "AXTitle"), self.get(el, "AXDescription")) if x}

    def windows(self):
        return self.get(self.ax, "AXWindows") or []

    def main_window(self):
        return next((w for w in self.windows() if str(self.get(w, "AXTitle") or "").startswith("Gemini")), None)

    def settings_window(self):
        return next((w for w in self.windows() if not str(self.get(w, "AXTitle") or "").startswith("Gemini")), None)

    def find(self, pred, root=None, depth_max=40):
        out = []

        def walk(el, d=0):
            if d > depth_max or out:
                return
            if pred(el):
                out.append(el)
            for ch in self.get(el, "AXChildren") or []:
                walk(ch, d + 1)

        roots = [root] if root is not None else ([self.main_window()] if self.main_window() is not None else [])
        for r in roots:
            walk(r)
        return out[0] if out else None

    def button(self, prefix: str):
        return self.find(lambda e: self.get(e, "AXRole") == "AXButton"
                         and any(l.startswith(prefix) for l in self.labels(e)))

    def wait_button(self, prefix: str, timeout: float) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.button(prefix) is not None:
                return time.monotonic() - t0
            time.sleep(0.1)
        return None

    def press(self, prefix: str, timeout: float = 5.0) -> None:
        if any(k in prefix.lower() for k in ("submit", "send")):
            raise RuntimeError(f"refusing to press '{prefix}': this run never sends anything")
        if self.wait_button(prefix, timeout) is None:
            raise DictationError(f"Gemini button '{prefix}' not found")
        AXUIElementPerformAction(self.button(prefix), "AXPress")

    def composer(self):
        return self.find(lambda e: self.get(e, "AXRole") == "AXTextArea")

    def text(self) -> str:
        c = self.composer()
        return str(self.get(c, "AXValue") or "").strip() if c is not None else ""

    def model(self) -> str:
        b = self.find(lambda e: self.get(e, "AXRole") == "AXButton" and any("Choose model" in l for l in self.labels(e)))
        lab = next((l for l in self.labels(b) if "Choose model" in l), "") if b is not None else ""
        return lab.replace(", Choose model", "").strip()

    def activate(self) -> None:
        subprocess.run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'])
        time.sleep(0.4)
        w = self.main_window()
        if w is not None:
            AXUIElementPerformAction(w, "AXRaise")

    def _key(self, code: int, flags: int = 0) -> None:
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, code, down)
            CGEventSetFlags(ev, flags)
            CGEventPostToPid(self.pid, ev)
            time.sleep(0.03)

    def clear(self) -> None:
        c = self.composer()
        if c is None:
            raise DictationError("Gemini prompt box not found")
        AXUIElementSetAttributeValue(c, "AXFocused", True)
        time.sleep(0.1)
        self._key(KEY_A, kCGEventFlagMaskCommand)
        self._key(KEY_DELETE)
        time.sleep(0.3)

    def read_usage(self) -> dict:
        osa(f'tell application id "{BUNDLE_ID}" to activate\n'
            f'tell application "System Events" to tell process "{PROC}" to click menu item "Settings…" '
            f'of menu 1 of menu bar item "{PROC}" of menu bar 1')
        sw = None
        for _ in range(20):
            sw = self.settings_window()
            if sw is not None:
                break
            time.sleep(0.25)
        if sw is None:
            return {}
        try:
            rows = []

            def walk(e, d=0):
                if d > 20:
                    return
                if self.get(e, "AXRole") == "AXRow":
                    rows.append(e)
                for c in self.get(e, "AXChildren") or []:
                    walk(c, d + 1)

            walk(sw)
            for r in rows:
                if self.find(lambda e: "Usage limits" in (str(self.get(e, "AXValue") or ""), str(self.get(e, "AXTitle") or ""),
                                                          str(self.get(e, "AXDescription") or "")), root=r, depth_max=4):
                    AXUIElementSetAttributeValue(r, "AXSelected", True)
                    break
            time.sleep(1.5)
            sw = self.settings_window()
            texts = []

            def walk2(e, d=0):
                if d > 30:
                    return
                r = self.get(e, "AXRole")
                if r == "AXOutline":
                    return
                if r == "AXStaticText" and self.get(e, "AXValue"):
                    texts.append(str(self.get(e, "AXValue")))
                if r == "AXCheckBox" and "Auto model selection" in self.labels(e):
                    texts.append(f"__auto={self.get(e, 'AXValue')}")
                for c in self.get(e, "AXChildren") or []:
                    walk2(c, d + 1)

            walk2(sw)
            out = {}
            for i, t in enumerate(texts):
                if t == "Current usage" and i + 2 < len(texts):
                    out["gemini_usage_now"] = texts[i + 1].replace(" used", "")
                    out["gemini_usage_now_resets"] = texts[i + 2]
                if t == "Weekly limit" and i + 2 < len(texts):
                    out["gemini_usage_week"] = texts[i + 1].replace(" used", "")
                    out["gemini_usage_week_resets"] = texts[i + 2]
                if t.startswith("__auto="):
                    out["gemini_auto_model"] = "on" if t.endswith("1") else "off"
            return out
        finally:
            sw = self.settings_window()
            if sw is not None:
                cb = self.get(sw, "AXCloseButton")
                if cb is not None:
                    AXUIElementPerformAction(cb, "AXPress")
            time.sleep(0.5)
            self.activate()


def pct(v: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", v or "")
    return float(m.group(1)) if m else 0.0


class GeminiTarget(Target):
    def __init__(self, app: GeminiApp) -> None:
        self.app = app

    def prepare(self) -> None:
        self.app.activate()
        for i in range(4):
            self.app.clear()
            time.sleep(0.15 if i == 0 else 1.5)
            if not self.read().strip():
                return
        raise DictationError("Gemini's prompt box keeps refilling after clearing")

    def read(self) -> str:
        fp = frontmost_app()
        if fp != self.app.proc_name:
            raise FocusLost(f"'{fp}' took focus")
        return self.app.text()

    def fast_reader(self):
        app = self.app
        box = app.composer()
        if box is None or str(app.get(box, "AXValue") or "").strip():
            raise DictationError("couldn't find Gemini's empty prompt box through Accessibility")

        def read():
            val = app.get(box, "AXValue")
            return None if val is None else str(val).strip()

        return read, front_window_guard(lambda owner: owner == app.proc_name)

    def close(self) -> None:
        try:
            self.app.clear()
        except Exception:
            pass


class Gemini(Driver):
    name = "gemini"
    extra_fields = ["gemini_app", "gemini_model", "gemini_auto_model", "gemini_usage_now", "gemini_usage_now_resets",
                    "gemini_usage_week", "gemini_usage_week_resets", "local_asr_started", "system_input",
                    "self_stopped_s"]
    volatile_fields = ["gemini_usage_now", "gemini_usage_now_resets", "gemini_usage_week", "gemini_usage_week_resets",
                       "local_asr_started"]
    precise_timing = True
    stop_btn = None

    def add_args(self, p) -> None:
        p.add_argument("--usage-every", type=int, default=25, help="re-read Gemini's usage %% every N clips")
        p.add_argument("--max-usage", type=float, default=95.0,
                       help="stop the run when Gemini's current or weekly usage reaches this %% (fallback risk)")
        p.add_argument("--model", default="", help="required Gemini model label ('' = don't check)")

    def preflight(self, args) -> None:
        self.app = GeminiApp()
        self.capture = audio_input.CaptureFlag(APP_PATH)
        self.old_input = None
        self.device = args.device
        self.usage_every, self.max_usage, self.want_model = args.usage_every, args.max_usage, args.model
        self._env, self.n_clips, self.local_flag = {}, 0, ""

    def env(self) -> dict:
        return {**self._env, "local_asr_started": self.local_flag}

    def env_problem(self, env: dict) -> str | None:
        if not env:
            return None
        if self.want_model and env.get("gemini_model") != self.want_model:
            return f"Gemini model is '{env.get('gemini_model')}', not '{self.want_model}' (model picker in the prompt box)"
        for k in ("gemini_usage_now", "gemini_usage_week"):
            if pct(env.get(k, "")) >= self.max_usage:
                return (f"Gemini {k.split('_')[-1]} usage at {env.get(k)} (>= {self.max_usage}%): past this it may fall "
                        f"back to other models - stopping so no clip is recorded on a fallback "
                        f"(resets: {env.get(k + '_resets')})")
        return None

    def refresh_usage(self) -> None:
        u = self.app.read_usage()
        self._env.update(u)
        self._env["gemini_model"] = self.app.model()
        log("Gemini usage: " + ", ".join(f"{k}={v}" for k, v in u.items()) + f", model={self._env['gemini_model']}")

    def make_target(self, args) -> Target:
        if self.app.main_window() is None:
            subprocess.run(["open", "-b", BUNDLE_ID])
            time.sleep(3)
        self.app.activate()
        if self.app.wait_button("New chat", 10) is None:
            raise SystemExit("Couldn't find Gemini's 'New chat' button - is the main window open?")
        self.app.press("New chat")
        time.sleep(2)
        if self.app.composer() is None or self.app.button("Use microphone") is None:
            raise SystemExit("Couldn't find Gemini's prompt box / microphone button.")
        return GeminiTarget(self.app)

    def setup(self, args, target) -> None:
        self.old_input = audio_input.get_default_input()
        bh, bh_name = audio_input.find_input(self.device)
        audio_input.set_default_input(bh)
        log(f"system default input -> {bh_name} (restored at the end)")
        self._env.update({"gemini_app": self.app.version,
                          "system_input": audio_input.device_name(audio_input.get_default_input())})
        self.refresh_usage()
        if (why := self.env_problem(self.env())):
            raise SystemExit(why)

    def before_clip(self) -> None:
        if self.n_clips and self.n_clips % self.usage_every == 0:
            self.refresh_usage()
            if (why := self.env_problem(self.env())):
                raise SystemExit("STOPPED (Gemini usage): " + why)
        self.n_clips += 1
        self._pids_before = local_asr_pids()
        self.capture.refresh()
        self.stop_btn = None
        if self.capture.find():
            raise DictationError("Gemini was still capturing before the clip started")

    def start(self) -> float | None:
        self.app.press("Use microphone")
        waited = self.app.wait_button("Stop", 10)
        if waited is None or not self.capture.wait(3):
            return None
        self.stop_btn = self.app.button("Stop")
        return waited if self.stop_btn is not None else None

    def listening(self) -> bool | None:
        return self.capture.running()

    def stop(self) -> float | None:
        button = self.stop_btn

        def press() -> None:
            if AXUIElementPerformAction(button, "AXPress"):
                self.app.press("Stop")
            self.stop_sent_t = time.monotonic()

        threading.Thread(target=press, daemon=True).start()
        return None

    def after_clip(self, started_utc, stop_mono) -> dict:
        new = local_asr_pids() - getattr(self, "_pids_before", set())
        self.local_flag = "yes" if new else "no"
        if new:
            raise SystemExit("STOPPED (possible local fallback): Apple's on-device recogniser (localspeechrecognition) started while Gemini dictated - "
                             "Gemini may have fallen back to local speech recognition; stopping for review")
        return {}

    def reset(self) -> None:
        try:
            if self.app.button("Stop") is not None:
                self.app.press("Stop")
                self.app.wait_button("Use microphone", 10)
        except DictationError:
            pass

    def teardown(self) -> None:
        if self.old_input is not None:
            audio_input.set_default_input(self.old_input)
            log(f"default input restored to {audio_input.device_name(self.old_input)}")


if __name__ == "__main__":
    run_tool(Gemini())
