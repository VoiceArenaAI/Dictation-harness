# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sounddevice", "soundfile", "numpy", "soxr", "pyobjc-framework-ApplicationServices",
#                 "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
import subprocess
import threading
import time
from pathlib import Path

import audio_input

from dictation_common import DictationError, Driver, log, osa, run_tool


def dictation_items() -> list[str]:
    out = osa('tell application "System Events" to tell process "TextEdit" to get name of every menu item '
              'of menu "Edit" of menu bar 1 whose name contains "Dictation"')
    return [x.strip() for x in out.split(",") if x.strip()]


def listening(items: list[str]) -> bool:
    return any(x.startswith("Stop") for x in items)


def idle(items: list[str]) -> bool:
    return any(x.startswith("Start") for x in items)


def click(prefix: str) -> None:
    try:
        osa('tell application "System Events" to tell process "TextEdit" to click '
            f'(first menu item of menu "Edit" of menu bar 1 whose name starts with "{prefix}")')
    except DictationError as e:
        raise DictationError(f"no '{prefix} Dictation' menu item right now ({e})") from e


def wait_for(want_listening: bool, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        items = dictation_items()
        if listening(items) if want_listening else idle(items):
            return time.monotonic() - t0
        time.sleep(0.05)
    return None


PREFS = "com.apple.speech.recognition.AppleSpeechRecognition.prefs"
ASR_ASSETS = Path("/System/Library/AssetsV2/com_apple_MobileAsset_UAF_Speech_AutomaticSpeechRecognition")


def asr_model_version() -> str:
    import plistlib
    for info in ASR_ASSETS.glob("*/*.asset/Info.plist"):
        try:
            d = plistlib.loads(info.read_bytes())
        except Exception:
            continue
        if d.get("CFBundleName") == "com.apple.speech.asr.transcription.en":
            return str(d.get("MobileAssetProperties", {}).get("AssetVersion") or d.get("AssetVersion") or "?")
    return ""


def read_pref(key: str) -> str:
    out = subprocess.run(["defaults", "read", PREFS, key], capture_output=True, text=True).stdout
    return " ".join(out.replace("(", " ").replace(")", " ").replace('"', " ").replace(",", " ").split())


class Apple(Driver):
    name = "apple"
    extra_fields = ["dictation_lang", "dictation_mic", "asr_model"]
    precise_timing = True
    live_typing = True

    def env(self) -> dict:
        langs = read_pref("DictationIMPreferredLanguageIdentifiers").split()
        return {"dictation_lang": langs[0] if langs else "", "dictation_mic": read_pref("DictationIMMicrophoneName"),
                "asr_model": asr_model_version()}

    def add_args(self, p) -> None:
        p.add_argument("--language", default="en_US", help="required Dictation language ('' = don't check)")

    def preflight(self, args) -> None:
        self.language = args.language
        self.capture = audio_input.CaptureFlag("corespeechd")

    def env_problem(self, env: dict) -> str | None:
        if self.language and env.get("dictation_lang") != self.language:
            return (f"Dictation language is '{env.get('dictation_lang')}', not {self.language} "
                    "(System Settings > Keyboard > Dictation > Languages)")
        if env.get("dictation_mic") and "blackhole" not in env["dictation_mic"].lower():
            return (f"Dictation's microphone is '{env['dictation_mic']}', not BlackHole 2ch "
                    "(System Settings > Keyboard > Dictation > Microphone source)")
        return None

    def setup(self, args, target) -> None:
        if not dictation_items():
            raise SystemExit("TextEdit's Edit menu has no Dictation item. Turn Dictation on: "
                             "System Settings > Keyboard > Dictation.")
        self.recover_stuck_state(target)

    @staticmethod
    def stuck() -> bool:
        time.sleep(1.5)
        if not listening(dictation_items()):
            return False
        try:
            return not any("blackhole" in n.lower() for n in audio_input.running_inputs())
        except Exception:
            return False

    def recover_stuck_state(self, target) -> None:
        if not self.stuck():
            return
        log("Dictation is stuck in 'Stop Dictation' with nothing recording - restarting TextEdit")
        n_docs = int(osa('tell application "TextEdit" to count of documents') or 0)
        if n_docs > 1:
            raise SystemExit("Dictation's state in TextEdit is stuck ('Stop Dictation' but nothing recording). "
                             "Close your other TextEdit documents, quit TextEdit, then rerun.")
        target.close()
        try:
            osa('tell application "TextEdit" to quit', timeout=15)
        except Exception:
            subprocess.run(["pkill", "-x", "TextEdit"])
        time.sleep(2)
        target.create()
        if self.stuck():
            raise SystemExit("Dictation still stuck after restarting TextEdit - log out and back in, then rerun.")
        log("Dictation state OK after restarting TextEdit")

    def before_clip(self) -> None:
        self.capture.refresh()
        if self.capture.find():
            raise DictationError("Dictation was still capturing before the clip started")

    def start(self) -> float | None:
        if not idle(dictation_items()):
            self.reset()
        click("Start")
        waited = wait_for(True, 10)
        if waited is None or not self.capture.wait(3):
            return None
        return waited

    def listening(self) -> bool | None:
        return self.capture.running()

    def stop(self) -> float | None:

        def press() -> None:
            try:
                click("Stop")
                self.stop_sent_t = time.monotonic()
            except DictationError as e:
                log(f"  stop click failed: {e}")

        threading.Thread(target=press, daemon=True).start()
        return None

    def reset(self) -> None:
        try:
            items = dictation_items()
            if listening(items):
                click("Stop")
                if wait_for(False, 10) is not None:
                    return
                items = dictation_items()
            if any(x.startswith("Cancel") for x in items):
                click("Cancel")
                wait_for(False, 5)
        except DictationError:
            pass


if __name__ == "__main__":
    run_tool(Apple())
