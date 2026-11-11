# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-ApplicationServices",
#                 "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import audio_input
from dictation_common import DictationError, Driver, log, run_tool

WISPR_DB = Path.home() / "Library/Application Support/Wispr Flow/flow.sqlite"
WISPR_CONFIG = Path.home() / "Library/Application Support/Wispr Flow/config.json"


def wispr(action: str) -> None:
    r = subprocess.run(["open", "-g", f"wispr-flow://{action}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise DictationError(f"Wispr deep link '{action}' failed: {r.stderr.strip()}")


def history_since(since: datetime) -> dict:
    try:
        con = sqlite3.connect(f"file:{WISPR_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "select timestamp, status, asrText, formattedText, pastedText, micDevice, detectedLanguage, duration "
            "from History where timestamp > ? order by timestamp desc limit 1",
            (since.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " +00:00",),
        ).fetchone()
        con.close()
        return dict(row) if row else {}
    except sqlite3.Error as e:
        log(f"  (couldn't read Wispr history: {e})")
        return {}


class Wispr(Driver):
    name = "wispr"
    extra_fields = ["asr_text", "wispr_text", "wispr_status", "mic", "wispr_detected_lang",
                    "wispr_langs", "wispr_ai_formatting", "wispr_cleanup", "wispr_plan",
                    "wispr_duration_s", "power", "wispr_pasted"]
    precise_timing = True
    capture_obj = 0

    def env(self) -> dict:
        try:
            u = json.loads(WISPR_CONFIG.read_text()).get("prefs", {}).get("user", {})
        except (OSError, ValueError):
            return {}
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
        return {"power": "AC" if "AC Power" in batt else "Battery",
                "wispr_langs": ",".join(u.get("selectedLanguages") or []),
                "wispr_ai_formatting": str(u.get("aiFormatting", "")),
                "wispr_cleanup": str((u.get("autoCleanup") or {}).get("level", "")),
                "wispr_plan": str((u.get("subscription") or {}).get("plan", ""))}

    def env_problem(self, env: dict) -> str | None:
        if env and env.get("power") != "AC":
            return "the Mac is on battery; latency runs must be on the charger"
        if env and env.get("wispr_langs") != "en":
            return f"Wispr's languages are '{env.get('wispr_langs')}', not English only (Settings > General > My languages)"
        if getattr(self, "bad_mic", ""):
            return f"Wispr recorded from '{self.bad_mic}', not BlackHole (Settings > General > Microphone)"
        return None

    def add_args(self, p) -> None:
        p.add_argument("--ready-wait", type=float, default=0.5,
                       help="fixed wait for Wispr to start listening; it gives no ready signal (s)")

    def preflight(self, args) -> None:
        self.ready_wait = args.ready_wait
        if subprocess.run(["pgrep", "-if", "Wispr Flow"], capture_output=True).returncode != 0:
            raise SystemExit("Wispr Flow isn't running. Open it and sign in.")
        if not WISPR_DB.exists():
            log(f"(Wispr history database not found at {WISPR_DB}; asr_text will be blank)")

    def capturing_object(self) -> int:
        for pid in self.pids:
            obj = audio_input.process_object(pid)
            if obj and audio_input.process_input_running(obj):
                return obj
        return 0

    def before_clip(self) -> None:
        out = subprocess.run(["pgrep", "-f", "/Applications/Wispr Flow.app/"], capture_output=True, text=True).stdout
        self.pids = [int(p) for p in out.split()]
        self.capture_obj = 0
        if self.capturing_object():
            raise DictationError("Wispr was still capturing before the clip started")

    def listening(self) -> bool | None:
        return audio_input.process_input_running(self.capture_obj) if self.capture_obj else None

    def start(self) -> float | None:
        wispr("start-hands-free")
        t0 = time.monotonic()
        while not self.capture_obj and time.monotonic() - t0 < 3:
            self.capture_obj = self.capturing_object()
            time.sleep(0.005)
        if not self.capture_obj:
            return None
        time.sleep(max(0.0, self.ready_wait - (time.monotonic() - t0)))
        return self.ready_wait

    def stop(self) -> float | None:
        p = subprocess.Popen(["open", "-g", "wispr-flow://stop-hands-free"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

        def sent() -> None:
            p.wait()
            self.stop_sent_t = time.monotonic()

        threading.Thread(target=sent, daemon=True).start()
        return None

    def reset(self) -> None:
        try:
            wispr("stop-hands-free")
        except DictationError:
            pass

    def after_clip(self, started_utc, stop_mono) -> dict:
        h = {}
        for _ in range(20):
            h = history_since(started_utc)
            if h:
                break
            time.sleep(0.25)
        mic = h.get("micDevice") or ""
        if mic and "blackhole" not in mic.lower():
            log(f"  warning: Wispr recorded from '{mic}', not BlackHole")
            self.bad_mic = mic
        if not h:
            log("  warning: no Wispr history row for this clip")
        return {"asr_text": h.get("asrText") or "", "wispr_text": h.get("formattedText") or h.get("pastedText") or "",
                "wispr_status": h.get("status") or "", "mic": mic,
                "wispr_duration_s": h.get("duration") if h.get("duration") is not None else "",
                "wispr_pasted": h.get("pastedText") or "",
                "wispr_detected_lang": h.get("detectedLanguage") or ""}


if __name__ == "__main__":
    run_tool(Wispr())
