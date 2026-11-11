import argparse
import csv
import difflib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

AUDIO_EXTS = {".flac", ".wav", ".mp3", ".m4a", ".ogg", ".aiff", ".aif"}
SEP = chr(30)
FIELDS = ["tool", "clip_id", "file", "status", "attempts", "reference_text", "transcript",
          "type", "task_type", "speaker_id", "gender", "audio_duration_s", "clip_s",
          "start_s", "first_text_s", "stop_s", "finalize_s", "focus_events", "flag", "error", "run_at", "macos"]
PRECISE_FIELDS = ["vendor_latency_s", "text_poll_gap_ms", "received_poll_gap_ms", "stop_signal_s", "stop_received_s",
                  "polls", "poll_gap_p50_ms", "poll_gap_p95_ms", "poll_gap_max_ms", "poll_start_s", "first_change_s",
                  "poll_ms", "ax_misses", "read_cost_ms_p50", "stop_ui_after_received_ms"]
LIVE_FIELDS = ["speech_start_s", "speech_end_s", "audio_end_s", "playback_lag_s", "stream_settle_s", "edits_total",
               "edits_after_stop", "rewrites", "rewrite_chars", "live_text_gap_ms", "live_poll_gap_p50_ms",
               "live_poll_gap_max_ms", "changed_after_stop", "text_before_stop"]
SUBSTITUTIONS = [
    ("Substitutions", "Smart Quotes"), ("Substitutions", "Smart Dashes"), ("Substitutions", "Text Replacement"),
    ("Substitutions", "Smart Links"), ("Substitutions", "Data Detectors"),
    ("Spelling and Grammar", "Correct Spelling Automatically"),
    ("Spelling and Grammar", "Check Spelling While Typing"),
    ("Spelling and Grammar", "Check Grammar With Spelling"),
]


class FocusLost(Exception):
    pass


class BatchIssue(Exception):
    ...


class DictationError(Exception):
    pass


LOG_FH = None


def log(msg: str, echo: bool = True) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    if echo:
        print(line, flush=True)
    if LOG_FH:
        LOG_FH.write(line + "\n")
        LOG_FH.flush()


def osa(script: str, timeout: float = 15.0) -> str:
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DictationError("AppleScript timed out (a dialog may be blocking the app)")
    if out.returncode != 0:
        err = out.stderr.strip()
        if "-25211" in err or "assistive access" in err.lower():
            raise SystemExit("macOS blocked UI control. Give this app (VS Code / Terminal) Accessibility "
                             "permission: System Settings > Privacy & Security > Accessibility, then rerun.\n" + err)
        if "-1743" in err:
            raise SystemExit("macOS blocked Automation. System Settings > Privacy & Security > Automation: "
                             "allow this app to control TextEdit and System Events.\n" + err)
        raise DictationError(err)
    return out.stdout.rstrip("\n")


def frontmost_app() -> str:
    return osa('tell application "System Events" to get name of first application process whose frontmost is true')


def app_running(name: str) -> bool:
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


def front_window_guard(owner_ok):
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly
    strikes = [0]

    def check_focus():
        for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
            if w.get("kCGWindowLayer", 0) == 0 and w["kCGWindowBounds"]["Height"] > 50:
                owner = str(w.get("kCGWindowOwnerName") or "")
                strikes[0] = 0 if owner_ok(owner) or "wispr" in owner.lower() else strikes[0] + 1
                if strikes[0] >= 2:
                    raise FocusLost(f"'{owner}' took focus")
                return

    return check_focus


class Target:
    def prepare(self) -> None: ...
    def read(self) -> str: ...
    def close(self) -> None: ...
    def fast_reader(self):
        return None


class TextEditTarget(Target):

    def __init__(self) -> None:
        self.was_running = app_running("TextEdit")
        self.create()

    @staticmethod
    def dismiss_open_panel() -> None:
        for _ in range(3):
            try:
                if osa('tell application "System Events" to tell process "TextEdit" to '
                       'exists window "Open"', timeout=5) != "true":
                    return
                osa('tell application "System Events" to tell process "TextEdit" to '
                    'click button "Cancel" of splitter group 1 of window "Open"', timeout=5)
                log("dismissed TextEdit's Open panel")
                time.sleep(0.5)
            except DictationError:
                time.sleep(0.5)

    def create(self) -> None:
        self.launch()
        self.dismiss_open_panel()
        self.win = int(osa(
            'tell application "TextEdit"\n activate\n set d to make new document\n delay 0.3\n'
            ' repeat with w in windows\n  try\n   if document of w is d then return id of w\n  end try\n'
            ' end repeat\n return id of window 1\n end tell'))
        self.doc = f"document of window id {self.win}"
        time.sleep(0.5)
        log(f"scratch document: window id {self.win}")
        subs = self.disable_substitutions()
        log("TextEdit substitutions: " + ", ".join(f"{k}={v}" for k, v in subs.items()))
        if "STILL ON" in subs.values():
            raise SystemExit("Couldn't switch off TextEdit autocorrect/substitutions - stopping so they don't "
                             "alter the transcripts.")

    @staticmethod
    def launch(timeout: float = 20.0) -> None:
        subprocess.run(["open", "-a", "TextEdit"], check=False)
        t0, last = time.monotonic(), ""
        while time.monotonic() - t0 < timeout:
            try:
                osa('tell application "TextEdit" to get name')
                return
            except DictationError as e:
                last = str(e)
                time.sleep(0.5)
        raise SystemExit(f"TextEdit didn't start: {last}")

    def bring_front(self) -> None:
        osa(f'tell application "TextEdit"\n activate\n set index of window id {self.win} to 1\n end tell')
        time.sleep(0.3)

    def disable_substitutions(self) -> dict[str, str]:
        self.bring_front()
        report = {}
        for submenu, item in SUBSTITUTIONS:
            path = (f'menu item "{item}" of menu 1 of menu item "{submenu}" of menu "Edit" of menu bar 1 '
                    'of process "TextEdit"')

            def checked() -> bool:
                m = osa(f'tell application "System Events" to get value of attribute "AXMenuItemMarkChar" of {path}')
                return bool(m) and m != "missing value"

            try:
                if checked():
                    osa(f'tell application "System Events" to click {path}')
                    t0 = time.monotonic()
                    while checked() and time.monotonic() - t0 < 3:
                        time.sleep(0.2)
                    report[item] = "STILL ON" if checked() else "turned off"
                else:
                    report[item] = "already off"
            except DictationError:
                report[item] = "not in menu"
        return report

    def exists(self) -> bool:
        try:
            return osa(f'tell application "TextEdit" to exists window id {self.win}') == "true"
        except DictationError:
            return False

    def minimize_others(self) -> None:
        osa('tell application "TextEdit"\n repeat with w in windows\n  try\n'
            f'   if id of w is not {self.win} and (document of w) is not missing value then '
            'set miniaturized of w to true\n'
            '  end try\n end repeat\n end tell')

    def prepare(self) -> None:
        if not self.exists():
            log(f"scratch document (window id {self.win}) was closed - creating a new one")
            self.create()
        self.minimize_others()
        self.bring_front()
        for i in range(4):
            osa(f'tell application "TextEdit" to set text of {self.doc} to ""')
            time.sleep(0.15 if i == 0 else 1.5)
            if not self.read().strip():
                return
        raise DictationError("document keeps refilling after clearing")

    def read(self) -> str:
        out = osa(
            'tell application "System Events" to set fp to name of first application process whose frontmost is true\n'
            'tell application "TextEdit"\n set fw to ""\n set fn to ""\n'
            ' repeat with w in windows\n  try\n'
            '   if (document of w) is not missing value and not (miniaturized of w) then\n'
            '    set fw to (id of w) as text\n    set fn to name of w\n    exit repeat\n   end if\n'
            '  end try\n end repeat\n'
            f' set t to text of {self.doc}\n'
            'end tell\n'
            'return fp & (ASCII character 30) & fw & (ASCII character 30) & fn & (ASCII character 30) & t'
        )
        fp, fw, fn, txt = (out.split(SEP, 3) + ["", "", "", ""])[:4]
        if fp != "TextEdit":
            raise FocusLost(f"'{fp}' took focus")
        if fw and fw != str(self.win):
            raise FocusLost(f"another TextEdit window ('{fn}') is in front")
        return txt

    def fast_reader(self):
        from ApplicationServices import AXUIElementCopyAttributeValue, AXUIElementCreateApplication

        def get(el, attr):
            err, val = AXUIElementCopyAttributeValue(el, attr, None)
            return val if err == 0 else None

        pid = subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True, text=True).stdout.split()
        if not pid:
            raise DictationError("TextEdit isn't running")
        app = AXUIElementCreateApplication(int(pid[0]))
        win = get(app, "AXFocusedWindow") or get(app, "AXMainWindow")

        def find(el, depth=0):
            if el is None or depth > 8:
                return None
            if get(el, "AXRole") == "AXTextArea":
                return el
            for child in get(el, "AXChildren") or []:
                hit = find(child, depth + 1)
                if hit is not None:
                    return hit
            return None

        area = find(win)
        if area is None or str(get(area, "AXValue") or "").strip():
            raise DictationError("couldn't find the empty scratch document through Accessibility")

        def read():
            val = get(area, "AXValue")
            return None if val is None else str(val)

        return read, front_window_guard(lambda owner: owner == "TextEdit")

    def close(self) -> None:
        try:
            if self.exists():
                osa(f'tell application "TextEdit" to close {self.doc} saving no')
        except Exception as e:
            log(f"(couldn't close scratch document: {e})")
        if not self.was_running:
            time.sleep(0.5)
            try:
                osa('tell application "TextEdit" to if (count of documents) is 0 then quit')
            except Exception:
                pass


class Driver:
    name = "tool"
    extra_fields: list[str] = []
    volatile_fields: list[str] = []
    precise_timing = False
    stop_sent_t: float | None = None
    has_stop_ui = False
    live_typing = False

    def add_args(self, p: argparse.ArgumentParser) -> None: ...
    def preflight(self, args) -> None: ...
    def make_target(self, args) -> Target:
        return TextEditTarget()
    def setup(self, args, target: Target) -> None: ...
    def before_clip(self) -> None: ...
    def stop_received(self) -> bool:
        return self.listening() is False
    def stop_ui_changed(self) -> bool:
        return False
    def start(self) -> float | None:
        raise NotImplementedError
    def listening(self) -> bool | None:
        return None
    def stop(self) -> float | None:
        raise NotImplementedError
    def reset(self) -> None:
        ...
    def after_clip(self, started_utc: datetime, stop_mono: float) -> dict:
        return {}
    def env(self) -> dict:
        return {}
    def env_problem(self, env: dict) -> str | None:
        return None
    def teardown(self) -> None: ...


def find_device(name: str) -> tuple[int, int]:
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_output_channels"] > 0:
            return i, d["max_output_channels"]
    raise SystemExit(f"Output device matching '{name}' not found. Available:\n{sd.query_devices()}")


def load_clip(path: Path, pad_ms: int, rate: int, channels: int) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if audio.size == 0:
        raise ValueError("empty audio")
    if sr != rate:
        import soxr
        audio = soxr.resample(audio, sr, rate).astype("float32")
    peak = float(np.abs(audio).max())
    if peak > 1.0:
        audio /= peak
    pad = np.zeros(int(rate * pad_ms / 1000), dtype="float32")
    return np.repeat(np.concatenate([pad, audio, pad])[:, None], channels, axis=1)


def preflight(args) -> tuple[int, int, int]:
    dev, channels = find_device(args.device)
    rate = int(sd.query_devices(dev)["default_samplerate"])
    log(f"audio -> {sd.query_devices(dev)['name']} ({channels} ch, {rate} Hz)")
    try:
        out_name = sd.query_devices(kind="output")["name"]
    except Exception:
        out_name = ""
    if args.device.lower() in out_name.lower():
        raise SystemExit("Your system OUTPUT is set to BlackHole, so alert/notification sounds would leak into "
                         "the dictation mic. Set System Settings > Sound > Output back to your speakers.")
    if not osa('tell application "System Events" to get name of every menu bar item of menu bar 1 '
               'of process "Finder"').strip():
        raise SystemExit("Can't read other apps' menus - grant Accessibility to this app and rerun.")
    if (why := global_text_problem()):
        raise SystemExit(why)
    if (why := power_problem()):
        raise SystemExit(why)
    return dev, channels, rate


GLOBAL_TEXT_SETTINGS = [
    "NSAutomaticCapitalizationEnabled", "NSAutomaticPeriodSubstitutionEnabled",
    "NSAutomaticSpellingCorrectionEnabled", "NSAutomaticQuoteSubstitutionEnabled",
    "NSAutomaticDashSubstitutionEnabled", "NSAutomaticTextReplacementEnabled",
    "NSAutomaticInlinePredictionEnabled", "NSAutomaticTextCompletionEnabled",
]


MIN_BATTERY = 20


def power_problem() -> str | None:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    if "Battery Power" not in out:
        return None
    m = re.search(r"(\d+)%", out)
    if m and int(m.group(1)) < MIN_BATTERY:
        return f"on battery at {m.group(1)}% - plug in the charger (the Mac would force-sleep mid-clip)"
    return None


def global_text_problem() -> str | None:
    on = []
    for k in GLOBAL_TEXT_SETTINGS:
        v = subprocess.run(["defaults", "read", "-g", k], capture_output=True, text=True)
        if v.returncode != 0 or v.stdout.strip() not in ("0", "false"):
            on.append(k)
    if on:
        return ("Mac-wide text substitutions are on and could change transcripts: " + ", ".join(on)
                + ". Turn off with: defaults write -g <key> -bool false (then relaunch TextEdit).")
    return None


def start_caffeinate() -> subprocess.Popen | None:
    try:
        return subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    except OSError as e:
        log(f"(caffeinate unavailable: {e}; disable sleep manually)")
        return None


class StopWatch(threading.Thread):
    INTERVAL = 0.002

    def __init__(self, stopped, timeout: float = 5.0) -> None:
        super().__init__(daemon=True)
        self.stopped, self.timeout = stopped, timeout
        self.received_at: float | None = None
        self.gap: float | None = None

    def run(self) -> None:
        t0 = prev = time.monotonic()
        while time.monotonic() - t0 < self.timeout:
            hit = self.stopped()
            now = time.monotonic()
            if hit:
                self.received_at, self.gap = now, now - prev
                return
            prev = now
            time.sleep(self.INTERVAL)


def speech_span(audio: np.ndarray, rate: int) -> tuple[float, float]:
    size = rate // 50
    mono = audio[:, 0]
    rms = np.array([np.sqrt(np.mean(mono[i:i + size] ** 2)) for i in range(0, len(mono), size)])
    loud = np.nonzero(rms >= rms.max() * 0.01)[0]
    return loud[0] * size / rate, min((loud[-1] + 1) * size, len(mono)) / rate


def edit_counts(changes: list[tuple[float, str]]) -> tuple[int, int]:
    prev, rewrites, chars = "", 0, 0
    for _, text in changes:
        if not text.startswith(prev):
            rewrites += 1
            ops = difflib.SequenceMatcher(None, prev, text, autojunk=False).get_opcodes()
            chars += sum(i2 - i1 for op, i1, i2, _, _ in ops if op in ("replace", "delete"))
        prev = text
    return rewrites, chars


def dictate_once(driver: Driver, target: Target, audio: np.ndarray, rate: int, dev: int, args,
                 expect_speech: bool) -> dict:
    driver.reset()
    target.prepare()
    driver.before_clip()
    precise = driver.precise_timing
    poll_ms = args.poll_ms or (10 if precise else 100)
    wants_fast = precise or poll_ms < 50
    fast = target.fast_reader() if wants_fast else None
    if wants_fast and fast is None:
        raise SystemExit(f"--poll-ms {poll_ms} needs a capture target with a fast reader")
    started_utc = datetime.now(timezone.utc)
    start_s = driver.start()
    if start_s is None:
        raise DictationError("dictation didn't start")
    time.sleep(args.start_delay)

    first_text, last_text, last_change = None, "", None
    self_stopped = None
    play_t0 = time.monotonic()
    speech_t0 = play_t0 + args.pad_ms / 1000
    play_end = play_t0 + len(audio) / rate
    last_state_check = play_t0
    live = precise and driver.live_typing
    live_stamps, changes, live_gap, ax_misses, first_text_t = [], [], None, 0, None
    next_live = last_live_focus = play_t0
    playback_end = {}
    sd.play(audio, rate, device=dev)
    if live:

        def note_playback_end() -> None:
            sd.wait()
            playback_end["t"] = time.monotonic()

        threading.Thread(target=note_playback_end, daemon=True).start()
    try:
        while time.monotonic() < play_end + args.tail:
            txt = fast[0]() if live else target.read()
            now = time.monotonic()
            if live:
                live_stamps.append(now)
                if now - last_live_focus >= 0.25:
                    last_live_focus = now
                    fast[1]()
                if txt is None:
                    ax_misses += 1
                    if ax_misses > 200:
                        raise DictationError("Accessibility stopped answering during the clip")
                    txt = last_text
            if txt != last_text:
                if live:
                    changes.append((now, txt))
                    live_gap = now - (live_stamps[-2] if len(live_stamps) > 1 else play_t0)
                last_text, last_change = txt, now
                if first_text is None and txt.strip():
                    first_text, first_text_t = now - speech_t0, now
            if now - last_state_check >= 1.0 and now < play_end - 0.5 and self_stopped is None:
                last_state_check = now
                if driver.listening() is False:
                    if expect_speech:
                        raise DictationError(f"dictation stopped by itself {now - play_t0:.1f}s into the clip")
                    self_stopped = now - play_t0
            if live:
                next_live += poll_ms / 1000
                delay = next_live - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_live = time.monotonic()
            else:
                time.sleep(0.1)
        sd.wait()
    finally:
        sd.stop()
        stop_t0 = time.monotonic()
        driver.stop_sent_t = None
        capturing = driver.listening() if precise else None
        watch = StopWatch(driver.stop_received) if precise else None
        ui_watch = StopWatch(driver.stop_ui_changed) if precise and driver.has_stop_ui else None
        for w in (watch, ui_watch):
            if w:
                w.start()
        try:
            stop_s = driver.stop()
        except DictationError:
            stop_s = None

    if precise and capturing is not True:
        raise DictationError("the tool was not capturing when the stop was sent, so this clip can't be timed")
    text_before_stop, last_live_change = last_text, last_change
    read_text, check_focus = fast if fast else (target.read, None)
    timeout = args.timeout if expect_speech else min(args.timeout, args.settle + 6)
    t0 = next_poll = last_focus = time.monotonic()
    stamps, costs = [], []
    first_change, detect_gap = None, None
    while True:
        before = time.monotonic()
        txt = read_text()
        now = time.monotonic()
        if fast:
            stamps.append(now)
            costs.append(now - before)
            if now - last_focus >= 0.25:
                last_focus = now
                check_focus()
            if txt is None:
                ax_misses += 1
                if ax_misses > 200:
                    raise DictationError("Accessibility stopped answering while waiting for the final text")
                txt = last_text
        if txt != last_text:
            last_text, last_change = txt, now
            if live:
                changes.append((now, txt))
            if first_text is None and txt.strip():
                first_text, first_text_t = now - speech_t0, now
            if fast:
                first_change = now if first_change is None else first_change
                detect_gap = now - (stamps[-2] if len(stamps) > 1 else t0)
        if last_text.strip() and last_change is not None and now - max(last_change, t0) >= args.settle:
            break
        if now - t0 >= timeout:
            break
        if fast:
            next_poll += poll_ms / 1000
            delay = next_poll - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_poll = time.monotonic()
        else:
            time.sleep(poll_ms / 1000)

    if fast and " ".join(target.read().split()) != " ".join(last_text.split()):
        raise DictationError("the fast text read disagrees with TextEdit's document")

    received = None
    if precise:
        watch.join(watch.timeout + 1)
        received = watch.received_at
        if received is None:
            raise DictationError(f"the tool never acted on the stop within {watch.timeout:.0f} s")
        if watch.gap > 0.1:
            raise DictationError(f"timing not trustworthy: stop-received check stalled {watch.gap * 1000:.0f} ms")
        if not live and last_change is not None and last_change <= received:
            raise DictationError("the text changed before the stop was received, so this clip can't be timed")

    finalize = max(0.0, last_change - stop_t0) if last_change is not None else None
    row = {
        "transcript": " ".join(last_text.split()),
        "start_s": f"{start_s:.2f}",
        "first_text_s": f"{first_text:.2f}" if first_text is not None else "",
        "stop_s": f"{stop_s:.2f}" if stop_s is not None else "",
        "finalize_s": f"{finalize:.2f}" if finalize is not None else "",
        "self_stopped_s": f"{self_stopped:.1f}" if self_stopped is not None else "",
    }
    if fast:
        gaps = np.diff(stamps) * 1000
        row.update({
            "poll_ms": poll_ms, "polls": len(stamps), "ax_misses": ax_misses,
            "poll_gap_p50_ms": f"{np.percentile(gaps, 50):.2f}", "poll_gap_p95_ms": f"{np.percentile(gaps, 95):.2f}",
            "poll_gap_max_ms": f"{gaps.max():.2f}", "read_cost_ms_p50": f"{np.percentile(costs, 50) * 1000:.2f}",
            "text_poll_gap_ms": f"{detect_gap * 1000:.1f}" if detect_gap is not None else "",
        })
    if precise:
        sent = driver.stop_sent_t
        changed = last_change is not None and last_change > received
        row.update({
            "vendor_latency_s": f"{last_change - received:.4f}" if changed else "",
            "received_poll_gap_ms": f"{watch.gap * 1000:.2f}",
            "stop_signal_s": f"{sent - stop_t0:.4f}" if sent is not None else "",
            "stop_received_s": f"{received - sent:.4f}" if sent is not None else "",
            "poll_start_s": f"{stamps[0] - received:.4f}",
            "first_change_s": f"{first_change - received:.4f}" if first_change is not None else "",
            "finalize_s": f"{finalize:.3f}" if finalize is not None else "",
        })
        if ui_watch:
            ui_watch.join(ui_watch.timeout + 1)
            if ui_watch.received_at is not None:
                row["stop_ui_after_received_ms"] = f"{(ui_watch.received_at - received) * 1000:.1f}"
    if live:
        played_to = playback_end.get("t", play_end)
        real_start = played_to - len(audio) / rate
        start_offset, end_offset = speech_span(audio, rate)
        speech_start, speech_end = real_start + start_offset, real_start + end_offset
        gaps = np.diff(live_stamps) * 1000
        rewrites, rewrite_chars = edit_counts(changes)
        row.update({
            "speech_start_s": f"{start_offset:.3f}", "speech_end_s": f"{end_offset:.3f}", "audio_end_s": f"{play_end - play_t0:.3f}",
            "playback_lag_s": f"{played_to - play_end:.4f}" if "t" in playback_end else "",
            "first_text_s": f"{first_text_t - speech_start:.3f}" if first_text_t is not None else "",
            "stream_settle_s": f"{last_live_change - speech_end:.4f}" if last_live_change is not None else "",
            "edits_total": len(changes), "edits_after_stop": sum(t > received for t, _ in changes),
            "rewrites": rewrites, "rewrite_chars": rewrite_chars,
            "live_text_gap_ms": f"{live_gap * 1000:.1f}" if live_gap is not None else "",
            "live_poll_gap_p50_ms": f"{np.percentile(gaps, 50):.2f}", "live_poll_gap_max_ms": f"{gaps.max():.2f}",
            "changed_after_stop": "yes" if changed else "no",
            "text_before_stop": " ".join(text_before_stop.split()),
            "_text_changes": [{"ms_after_speech_end": round((t - speech_end) * 1000, 1),
                               "ms_after_stop_received": round((t - received) * 1000, 1), "text": text}
                              for t, text in changes],
        })
    row.update(driver.after_clip(started_utc, stop_t0))
    return row


def load_csv(path: Path | None, key: str = "file") -> dict[str, dict]:
    if not path or not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r[key]: r for r in csv.DictReader(fh) if r.get(key)}


def save_results(path: Path, results: dict[str, dict], fields: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".tmp_", suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results.values())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def add_timing_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pad-ms", type=int, default=300, help="silence added before and after each clip (ms)")
    p.add_argument("--start-delay", type=float, default=0.5, help="wait after the app is listening (s)")
    p.add_argument("--tail", type=float, default=4.0, help="keep listening this long after playback (s)")
    p.add_argument("--settle", type=float, default=2.0, help="final text must be stable this long (s)")
    p.add_argument("--timeout", type=float, default=30.0, help="max wait for final text (s)")
    p.add_argument("--gap", type=float, default=1.0, help="pause between clips (s)")
    p.add_argument("--poll-ms", type=int, default=0,
                   help="how often to read the text after stop (0 = 10 ms for precise-timing tools, otherwise 100 ms)")


def run_tool(driver: Driver) -> None:
    global LOG_FH
    p = argparse.ArgumentParser(description=f"{driver.name} dictation runner")
    p.add_argument("audio_dir", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path(f"results_{driver.name}.csv"))
    p.add_argument("--manifest", type=Path, help="defaults to <audio_dir>/manifest.csv if present")
    p.add_argument("--device", default="BlackHole", help="output device name substring")
    p.add_argument("--attempts", type=int, default=3, help="tries per clip before giving up")
    p.add_argument("--limit", type=int, default=0, help="only run this many clips (0 = all)")
    p.add_argument("--clips-file", type=Path,
                   help="one clip id per line: run exactly these, in this order; stops if any is missing")
    p.add_argument("--abort-after-empty", type=int, default=3,
                   help="stop if this many speech clips in a row produce no text (mic probably not on BlackHole)")
    p.add_argument("--check", action="store_true", help="run the preflight checks and exit")
    p.add_argument("--batch-size", type=int, default=5,
                   help="after every N clips: back up results, sanity-check the batch and the environment (0 = off)")
    p.add_argument("--no-stop-on-issue", action="store_true",
                   help="log batch problems instead of stopping the run")
    add_timing_args(p)
    driver.add_args(p)
    args = p.parse_args()

    extras = driver.extra_fields + (PRECISE_FIELDS if driver.precise_timing else []) + (LIVE_FIELDS if driver.live_typing else [])
    fields = FIELDS + [f for f in extras if f not in FIELDS]
    LOG_FH = args.output.with_suffix(".log").open("a", encoding="utf-8")
    log(f"=== {driver.name} run started {datetime.now().isoformat(timespec='seconds')} ===", echo=False)

    if not args.audio_dir.is_dir():
        sys.exit(f"{args.audio_dir} is not a folder")
    manifest = load_csv(args.manifest or args.audio_dir / "manifest.csv")
    files = sorted(f for f in args.audio_dir.iterdir() if f.suffix.lower() in AUDIO_EXTS)
    if args.clips_file:
        wanted = [x.strip() for x in args.clips_file.read_text().splitlines() if x.strip()]
        by_id = {f.stem: f for f in files}
        missing = [c for c in wanted if c not in by_id]
        if missing:
            sys.exit(f"{len(missing)} clip(s) in {args.clips_file} are not in {args.audio_dir}: {missing[:10]}")
        files = [by_id[c] for c in wanted]
    results = load_csv(args.output)
    todo = [f for f in files if results.get(f.name, {}).get("status") != "ok"]
    n_done = len(files) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(files)} clips, {n_done} done, {len(todo)} to run"
        + ("" if manifest else " (no manifest: reference text will be blank)"))

    dev, channels, rate = preflight(args)
    driver.preflight(args)
    env = driver.env()
    if env:
        log("tool settings before first clip: " + ", ".join(f"{k}={v}" for k, v in env.items()))
    if (why := driver.env_problem(env)):
        log(f"  note: {why} - will be re-checked after the first batch")
    args._env = None
    if args.check:
        log("preflight ok")
        return
    if not todo:
        return

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    caf, target, exit_code = None, None, 0
    try:
        caf = start_caffeinate()
        target = driver.make_target(args)
        driver.setup(args, target)
        log("Starting in 3s - don't touch the keyboard/mouse. Ctrl-C stops safely; rerun to resume.")
        time.sleep(3)
        _loop(driver, target, todo, results, manifest, args, rate, channels, dev, fields)
    except KeyboardInterrupt:
        log("interrupted - progress saved, rerun the same command to resume")
        exit_code = 130
    except BatchIssue as e:
        log(f"STOPPED FOR REVIEW: {e} - all data saved; fix, then rerun the same command to resume")
        exit_code = 3
    except SystemExit:
        raise
    except Exception as e:
        log(f"run aborted by unexpected {type(e).__name__}: {e} - progress saved, rerun to resume")
        LOG_FH.write(traceback.format_exc())
        exit_code = 1
    finally:
        sd.stop()
        for step in (driver.reset, target.close if target else None, driver.teardown):
            if step:
                try:
                    step()
                except Exception as e:
                    log(f"(cleanup step failed: {e})")
        if caf:
            caf.terminate()
        save_results(args.output, results, fields)
        counts: dict[str, int] = {}
        for r in results.values():
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        log(f"results: {args.output} - " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if exit_code:
        sys.exit(exit_code)


def backup_results(path: Path, n_total_ok: int, keep: int = 5) -> None:
    import shutil
    bdir = path.parent / f"{path.stem}_backups"
    bdir.mkdir(exist_ok=True)
    shutil.copy2(path, bdir / f"{path.stem}_after_{n_total_ok:05d}_ok.csv")
    for old in sorted(bdir.glob(f"{path.stem}_after_*_ok.csv"))[:-keep]:
        old.unlink()


def length_flag(r: dict) -> str:
    if r.get("status") != "ok" or r.get("type") == "silence" or not r.get("reference_text"):
        return ""
    ref_n = len(r["reference_text"].split())
    hyp_n = len(r.get("transcript", "").split())
    ratio = hyp_n / ref_n if ref_n else 1
    if ratio < 0.6:
        return f"short_transcript ({hyp_n}/{ref_n} words)"
    if ratio > 1.4:
        return f"long_transcript ({hyp_n}/{ref_n} words)"
    return ""


def check_batch(rows: list[dict], driver: Driver, args) -> list[str]:
    issues = []
    for r in rows:
        if r.get("status") != "ok":
            issues.append(f"{r['file']}: status {r.get('status')} ({r.get('error', '')[-160:]})")
    try:
        out_name = sd.query_devices(kind="output")["name"]
        if args.device.lower() in out_name.lower():
            issues.append("system output was switched to BlackHole (alert sounds would leak into the mic)")
    except Exception:
        pass
    if (why := global_text_problem()):
        issues.append(why)
    env = driver.env()
    stable = {k: v for k, v in env.items() if k not in driver.volatile_fields}
    if (why := driver.env_problem(env)):
        issues.append(why)
    elif stable and args._env is None:
        args._env = stable
        log("tool settings in use: " + ", ".join(f"{k}={v}" for k, v in env.items()))
    elif stable and stable != args._env:
        issues.append(f"tool settings changed mid-run: {args._env} -> {stable}")
    return issues


def _loop(driver, target, todo, results, manifest, args, rate, channels, dev, fields) -> None:
    empty_streak = 0
    macos = platform.mac_ver()[0]
    batch: list[dict] = []
    for n, f in enumerate(todo, 1):
        if (why := power_problem()):
            raise BatchIssue(why)
        meta = manifest.get(f.name, {})
        row = {k: meta.get(k, "") for k in ("clip_id", "reference_text", "type", "task_type",
                                            "speaker_id", "gender", "audio_duration_s")}
        row.update(tool=driver.name, file=f.name, clip_id=row["clip_id"] or f.stem, macos=macos)
        expect_speech = row["type"] != "silence"
        tag = f"[{n}/{len(todo)}] {f.name}"
        try:
            audio = load_clip(f, args.pad_ms, rate, channels)
        except Exception as e:
            row.update(status="bad_audio", attempts=0, error=str(e),
                       run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            results[f.name] = row
            save_results(args.output, results, fields)
            log(f"{tag}: unreadable audio ({e})")
            if not args.no_stop_on_issue:
                raise BatchIssue(f"{f.name}: unreadable audio ({e})")
            continue
        row["clip_s"] = f"{len(audio) / rate:.2f}"

        focus_events, errors, out = 0, [], None
        for attempt in range(1, args.attempts + 1):
            row["attempts"] = attempt
            try:
                out = dictate_once(driver, target, audio, rate, dev, args, expect_speech)
                if expect_speech and not out["transcript"]:
                    raise DictationError("no text produced")
                break
            except FocusLost as e:
                focus_events += 1
                errors.append(f"try {attempt}: focus lost ({e})")
                log(f"{tag}: focus lost ({e}) - retrying")
            except DictationError as e:
                errors.append(f"try {attempt}: {e}")
                log(f"{tag}: {e}" + (" - retrying" if attempt < args.attempts else ""))
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                errors.append(f"try {attempt}: {type(e).__name__}: {e}")
                log(f"{tag}: unexpected {type(e).__name__}: {e}")
                LOG_FH.write(traceback.format_exc())
            out = None
            sd.stop()
            try:
                driver.reset()
            except Exception:
                pass
            time.sleep(2 if attempt == 1 else 15)

        if out:
            text_changes = out.pop("_text_changes", None)
            row.update(out, status="ok")
            empty_streak = 0
            if text_changes is not None:
                with args.output.with_suffix(".text_changes.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"clip_id": row["clip_id"], "changes": text_changes}, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        else:
            last = errors[-1] if errors else ""
            row["status"] = ("focus_lost" if "focus lost" in last else
                             "empty" if "no text" in last else
                             "didnt_start" if "didn't start" in last else "error")
            empty_streak = empty_streak + 1 if row["status"] == "empty" else 0
        row["focus_events"] = focus_events
        row["error"] = " | ".join(errors)
        row["run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row.update(driver.env())
        row["flag"] = length_flag(row)
        if row["flag"]:
            log(f"{tag}: flagged {row['flag']} - kept as data, review later")
        results[f.name] = row
        save_results(args.output, results, fields)
        log(f"{tag} [{row['status']}] {row.get('transcript', '')!r}")
        batch.append(row)

        if args.abort_after_empty and empty_streak >= args.abort_after_empty:
            raise SystemExit(f"{empty_streak} speech clips in a row produced no text. Check the tool's microphone "
                             "is set to BlackHole 2ch, then rerun.")
        stop = not args.no_stop_on_issue
        if row["status"] != "ok" and stop:
            raise BatchIssue(f"{f.name} failed after {row['attempts']} tries: {row['error'][-200:]}")
        if args.batch_size and (len(batch) >= args.batch_size or n == len(todo)):
            n_ok = sum(1 for r in results.values() if r.get("status") == "ok")
            backup_results(args.output, n_ok)
            issues = check_batch(batch, driver, args)
            log(f"--- batch check after {n}/{len(todo)} ({n_ok} ok in total): "
                + ("OK" if not issues else f"{len(issues)} issue(s)"))
            for i in issues:
                log(f"    ! {i}")
            if issues and stop:
                raise BatchIssue("; ".join(issues))
            batch = []
        time.sleep(args.gap)
