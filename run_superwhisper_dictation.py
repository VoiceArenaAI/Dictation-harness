# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-Quartz",
#                 "pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa"]
# ///
import json
import subprocess
import time
from pathlib import Path

from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly

import audio_input
from dictation_common import DictationError, Driver, log, run_tool

RECORDINGS = Path.home() / "superwhisper/recordings"
PREFS = "com.superduper.superwhisper"


def deep_link() -> None:
    r = subprocess.run(["open", "-g", "superwhisper://record"], capture_output=True, text=True)
    if r.returncode != 0:
        raise DictationError(f"superwhisper://record failed: {r.stderr.strip()}")


def recorder_visible() -> bool:
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:
        if str(w.get("kCGWindowOwnerName", "")).lower() == "superwhisper" and (w.get("kCGWindowLayer") or 0) >= 3:
            b = w.get("kCGWindowBounds", {})
            if b.get("Height", 0) < 300:
                return True
    return False


def wait_recorder(visible: bool, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if recorder_visible() == visible:
            return time.monotonic() - t0
        time.sleep(0.05)
    return None


def read_pref(key: str) -> str:
    return subprocess.run(["defaults", "read", PREFS, key], capture_output=True, text=True).stdout.strip()


def superwhisper_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "superwhisper"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def recording_dirs() -> set[Path]:
    return {p for p in RECORDINGS.iterdir() if p.is_dir()} if RECORDINGS.exists() else set()


class Superwhisper(Driver):
    name = "superwhisper"
    extra_fields = ["sw_raw", "sw_llm", "sw_result", "sw_model", "sw_language_model", "sw_mode",
                    "sw_duration_ms", "sw_mic", "sw_mode_key"]
    precise_timing = True

    def add_args(self, p) -> None:
        p.add_argument("--start-timeout", type=float, default=5.0,
                       help="max wait for Superwhisper's recording window after the start link (s)")
        p.add_argument("--stop-timeout", type=float, default=20.0,
                       help="max wait for the recording window to close after the stop link (s)")

    def preflight(self, args) -> None:
        self.start_timeout, self.stop_timeout = args.start_timeout, args.stop_timeout
        self.active = False
        self.before: set[Path] = set()
        if subprocess.run(["pgrep", "-x", "superwhisper"], capture_output=True).returncode != 0:
            raise SystemExit("Superwhisper isn't running. Open it and sign in.")
        if recorder_visible():
            raise SystemExit("Superwhisper is already recording - stop it first.")
        if not RECORDINGS.exists():
            log(f"(Superwhisper recordings folder not found at {RECORDINGS}; sw_* columns will be blank)")

    def env(self) -> dict:
        return {"sw_mic": read_pref("selectedDeviceID"), "sw_mode_key": read_pref("activeModeKey")}

    def env_problem(self, env: dict) -> str | None:
        if "blackhole" not in env.get("sw_mic", "").lower():
            return (f"Superwhisper's microphone is '{env.get('sw_mic')}', not BlackHole 2ch "
                    "(menu bar > Superwhisper > microphone)")
        return None

    def before_clip(self) -> None:
        if recorder_visible() and wait_recorder(False, self.stop_timeout) is None:
            raise DictationError("Superwhisper's recording window is still open from an earlier attempt")
        self.before = recording_dirs()
        pid = superwhisper_pid()
        self.audio_obj = audio_input.process_object(pid) if pid else None

    def start(self) -> float | None:
        deep_link()
        waited = wait_recorder(True, self.start_timeout)
        self.active = waited is not None
        return waited

    def listening(self) -> bool | None:
        return audio_input.process_input_running(self.audio_obj) if getattr(self, "audio_obj", None) else None

    def stop_received(self) -> bool:
        return self.listening() is False

    def stop(self) -> float | None:
        if not self.active:
            return None
        deep_link()
        self.stop_sent_t = time.monotonic()
        self.active = False
        return None

    def reset(self) -> None:
        try:
            self.stop()
            if self.listening():
                deep_link()
                time.sleep(1.0)
        except DictationError:
            pass

    def after_clip(self, started_utc, stop_mono) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            new = recording_dirs() - self.before
            metas = [d / "meta.json" for d in new if (d / "meta.json").exists()]
            if metas:
                path = max(metas, key=lambda p: p.stat().st_mtime)
                for _ in range(10):
                    try:
                        m = json.loads(path.read_text())
                        return {"sw_raw": m.get("rawResult", ""), "sw_llm": m.get("llmResult", ""),
                                "sw_result": m.get("result", ""), "sw_model": m.get("modelName", ""),
                                "sw_language_model": m.get("languageModelName", ""), "sw_mode": m.get("modeName", ""),
                                "sw_duration_ms": m.get("duration", "")}
                    except (json.JSONDecodeError, OSError):
                        time.sleep(0.2)
            time.sleep(0.2)
        log("  (no Superwhisper meta.json for this clip; sw_* columns blank)")
        return {}


if __name__ == "__main__":
    run_tool(Superwhisper())
