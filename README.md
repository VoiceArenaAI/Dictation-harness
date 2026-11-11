the# Dictation harness

Plays a folder of audio clips into macOS dictation tools through a virtual microphone and records, for each clip,
the transcript the tool produced and how long it took. Every tool gets the same audio, padding and timing.

Tools: Apple Dictation, ChatGPT, Claude, Gemini, Granola, Meta AI, Perplexity, Superwhisper, Willow Voice, Wispr Flow.

## How it works

1. The clip is padded with 300 ms of silence at each end and played into **BlackHole 2ch**, a virtual audio device
   whose output loops back as a microphone. Every tool is set to record from BlackHole, so each one receives the
   same samples and nothing goes through a speaker or a real microphone.
2. The harness starts the tool, waits until it is listening plus 0.5 s, plays the clip, keeps it listening for 4 s
   more, then stops it.
3. The text lands in a scratch TextEdit document (with autocorrect, smart quotes and text replacement switched off),
   or in the tool's own text box for Claude, ChatGPT, Gemini and Perplexity. It is read through Accessibility every
   10 ms, and the transcript is saved once it has been unchanged for 2 s.

**Latency** (`vendor_latency_s`) is the time from the tool receiving the stop (macOS reporting that its audio capture
ended, checked every 2 ms) to the last change of the text. Tools that type while you speak (Apple Dictation, Claude,
Perplexity) finish before the stop, so they are timed from the end of the actual speech instead. Recordings often
start or end in silence, so speech is located with the Silero voice-activity model: each clip is resampled to 16 kHz
and scored in 32 ms frames, and speech starts at the first run of 3 frames (about 100 ms) with speech probability of
at least 0.5 and ends after the last such run (`make_speech_bounds.py`).

## Requirements

- An Apple silicon Mac, signed in to the GUI, awake and unlocked for the whole run. It does not work headless.
- [uv](https://docs.astral.sh/uv/). Each script lists its dependencies at the top; `uv run` installs them.
- [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole): `brew install blackhole-2ch`, then restart.
- For the app running the scripts (Terminal, iTerm, VS Code): **Accessibility** permission, and allow
  **Automation** of TextEdit and System Events when macOS asks.
- System Settings → Keyboard → Text Input: automatic capitalisation, spelling correction, smart quotes and dashes,
  text replacement and predictive text **off** (the harness checks).
- On the charger. Do Not Disturb on. Only the tool under test open. Don't touch the keyboard or mouse during a run.

## Tool setup

| Tool | Microphone | How the harness starts and stops it |
| --- | --- | --- |
| Apple Dictation | Keyboard → Dictation → Microphone source: BlackHole 2ch | TextEdit Edit → Start / Stop Dictation |
| ChatGPT | system default input (switched to BlackHole for the run, then restored) | Dictate / Stop dictation buttons |
| Claude | system default input (switched to BlackHole for the run, then restored); dictation language English | Dictate / Finish dictation buttons |
| Gemini | system default input (switched to BlackHole for the run, then restored) | Use microphone / Stop buttons |
| Granola | Voice typing → Microphone: BlackHole 2ch | fn twice to start, fn once to stop (quit Willow first: it also listens for fn) |
| Meta AI | Dictation → Input device: BlackHole 2ch; Tap to talk shortcut: Right Option | Right Option |
| Perplexity | system default input (switched to BlackHole for the run, then restored) | microphone / stop buttons in its search box |
| Superwhisper | BlackHole 2ch; "Automatically increase microphone volume" off | `superwhisper://record` link |
| Willow Voice | BlackHole 2ch; Hands-Free Dictation Hotkey: ⌘ + backtick | ⌘ + backtick |
| Wispr Flow | BlackHole 2ch | `wispr-flow://start-hands-free` / `stop-hands-free` links |

Each app must be installed, signed in, past its onboarding, and allowed to use the microphone.

## Run

Put the clips in a folder (`.wav`, `.flac`, `.mp3`, `.m4a`, `.ogg`, `.aiff`). An optional `manifest.csv` in the same
folder adds per-clip columns to the results (`clip_id`, `reference_text`, `type`, `speaker_id`, ...).

```sh
uv run make_speech_bounds.py clips                       # once per clip folder: writes clips/speech_bounds.csv

uv run run_willow_dictation.py clips --check             # preflight checks only
uv run run_willow_dictation.py clips -o results_willow.csv

uv run latency_report.py results_willow.csv
uv run latency_report.py results_claude.csv --speech-bounds clips/speech_bounds.csv   # tools that type live
```

The same pattern works for every `run_<tool>_dictation.py`. Run one tool at a time. To resume, rerun the same
command: clips already marked `ok` in the results file are skipped. `--clips-file` runs a fixed list of clip ids,
`--limit N` only the first N. Timing options (`--pad-ms`, `--start-delay`, `--tail`, `--settle`, `--timeout`,
`--gap`) default to the values above and should be the same for every tool you compare.

## License

MIT, see [LICENSE](LICENSE).
