# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-ApplicationServices",
#                 "pyobjc-framework-Cocoa"]
# ///
import subprocess
import sys
import time

from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
)

import audio_input
from dictation_common import (DictationError, Driver, FocusLost, Target, front_window_guard, frontmost_app, log,
                               run_tool)

BUNDLE_ID = "ai.perplexity.macv3"
MIC, STOP, SEND = "microphone", "player-stop-filled", "arrow-right"


class PerplexityApp:
    def __init__(self) -> None:
        apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.bundleIdentifier() == BUNDLE_ID]
        if not apps:
            sys.exit("Perplexity isn't running. Open it and sign in.")
        self.pid = apps[0].processIdentifier()
        self.proc_name = str(apps[0].localizedName())
        self.ax = AXUIElementCreateApplication(self.pid)

    @staticmethod
    def get(el, attr):
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None

    def label(self, el) -> str:
        return " | ".join(str(x) for x in (self.get(el, "AXTitle"), self.get(el, "AXDescription")) if x)

    def _walk(self, pred, first=True):
        out = []

        def walk(el, depth=0):
            if depth > 60 or (first and out):
                return
            if pred(el):
                out.append(el)
            for child in self.get(el, "AXChildren") or []:
                walk(child, depth + 1)

        for win in self.get(self.ax, "AXWindows") or []:
            walk(win)
        return out

    def button(self, name: str):
        hits = self._walk(lambda e: self.get(e, "AXRole") == "AXButton" and self.label(e) == name)
        return hits[0] if hits else None

    def wait_button(self, name: str, timeout: float) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.button(name) is not None:
                return time.monotonic() - t0
            time.sleep(0.05)
        return None

    def composer(self):
        hits = self._walk(lambda e: self.get(e, "AXRole") == "AXTextArea")
        return hits[0] if hits else None

    def text(self) -> str:
        c = self.composer()
        return str(self.get(c, "AXValue") or "") if c is not None else ""

    def activate(self) -> None:
        subprocess.run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'])
        time.sleep(0.4)

    def clear(self) -> None:
        c = self.composer()
        if c is None:
            raise DictationError("Perplexity's search box not found (is the main window on a new session?)")
        AXUIElementSetAttributeValue(c, "AXValue", "")
        AXUIElementSetAttributeValue(c, "AXFocused", True)
        time.sleep(0.3)


class PerplexityTarget(Target):
    def __init__(self, app: PerplexityApp) -> None:
        self.app = app
        self.area = None

    def prepare(self) -> None:
        self.app.activate()
        self.area = None
        for _ in range(4):
            self.app.clear()
            time.sleep(0.5)
            if not self.read().strip():
                return
            time.sleep(1.0)
        raise DictationError("Perplexity's search box keeps refilling after clearing")

    def read(self) -> str:
        fp = frontmost_app()
        if fp != self.app.proc_name:
            raise FocusLost(f"'{fp}' took focus")
        return self.app.text()

    def fast_reader(self):
        def read() -> str | None:
            for _ in range(2):
                if self.area is None:
                    self.area = self.app.composer()
                if self.area is not None:
                    err, value = AXUIElementCopyAttributeValue(self.area, "AXValue", None)
                    if err == 0:
                        return str(value or "")
                    self.area = None
            return None

        return read, front_window_guard(lambda owner: owner == self.app.proc_name)

    def close(self) -> None:
        try:
            self.app.clear()
        except Exception:
            pass


class Perplexity(Driver):
    name = "perplexity"
    extra_fields = ["pplx_input", "pplx_noise_reduction", "pplx_stop_after"]
    precise_timing = True
    live_typing = True
    audio_obj = None
    pids: set[int] = set()

    def add_args(self, p) -> None:
        p.add_argument("--start-timeout", type=float, default=5.0, help="max wait for the stop button (s)")
        p.add_argument("--stop-timeout", type=float, default=15.0, help="max wait for the microphone button (s)")

    def preflight(self, args) -> None:
        self.app = PerplexityApp()
        self.start_timeout, self.stop_timeout = args.start_timeout, args.stop_timeout
        self.device = args.device
        self.old_input = None

    def make_target(self, args) -> Target:
        self.app.activate()
        if self.app.composer() is None or (self.app.button(MIC) is None and self.app.button(STOP) is None):
            raise SystemExit("Couldn't find Perplexity's search box and microphone button - open the main window "
                             "on a new session.")
        return PerplexityTarget(self.app)

    def setup(self, args, target) -> None:
        self.old_input = audio_input.get_default_input()
        bh, bh_name = audio_input.find_input(self.device)
        audio_input.set_default_input(bh)
        log(f"system default input -> {bh_name} (Perplexity records from it; restored at the end)")

    @staticmethod
    def _pref(key: str) -> str:
        out = subprocess.run(["defaults", "read", BUNDLE_ID, key], capture_output=True, text=True).stdout.strip()
        return out or "default"

    def env(self) -> dict:
        return {"pplx_input": audio_input.device_name(audio_input.get_default_input()),
                "pplx_noise_reduction": self._pref("dictationNoiseReduction"),
                "pplx_stop_after": self._pref("dictationSilenceTimeout")}

    def env_problem(self, env: dict) -> str | None:
        if self.old_input is not None and "blackhole" not in env.get("pplx_input", "").lower():
            return f"system default input is '{env.get('pplx_input')}', not BlackHole (Perplexity records from it)"
        return None

    def start(self) -> float | None:
        mic = self.app.button(MIC)
        if mic is None:
            raise DictationError("Perplexity's microphone button not found")
        AXUIElementPerformAction(mic, "AXPress")
        return self.app.wait_button(STOP, self.start_timeout)

    def before_clip(self) -> None:
        if self.app.wait_button(MIC, self.stop_timeout) is None:
            raise DictationError("Perplexity is still dictating from an earlier attempt")
        self.audio_obj = None
        self.pids = {int(p) for p in subprocess.run(["pgrep", "-f", "Perplexity"], capture_output=True, text=True).stdout.split()}

    def listening(self) -> bool | None:
        if self.audio_obj is None:
            self.audio_obj = next((o for p, o in audio_input.capturing_processes().items() if p in self.pids), None)
            return True if self.audio_obj else None
        return audio_input.process_input_running(self.audio_obj)

    def stop_received(self) -> bool:
        return self.listening() is False

    def stop(self) -> float | None:
        self.stop_sent_t = None
        stop = self.app.button(STOP)
        if stop is not None:
            AXUIElementPerformAction(stop, "AXPress")
            self.stop_sent_t = time.monotonic()
        return None

    def reset(self) -> None:
        stop = self.app.button(STOP)
        if stop is not None:
            AXUIElementPerformAction(stop, "AXPress")
            self.app.wait_button(MIC, self.stop_timeout)

    def teardown(self) -> None:
        if self.old_input is not None:
            audio_input.set_default_input(self.old_input)
            log(f"default input restored to {audio_input.device_name(self.old_input)}")


if __name__ == "__main__":
    run_tool(Perplexity())
