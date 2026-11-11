# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["numpy", "soundfile", "soxr", "pysilero-vad"]
# ///
import csv
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from pysilero_vad import SileroVoiceActivityDetector

RATE = 16000
folder = Path(sys.argv[1])
rows = []
for path in sorted(p for p in folder.iterdir() if p.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aiff", ".aif"}):
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    pcm = (np.clip(soxr.resample(audio, sr, RATE), -1, 1) * 32767).astype(np.int16)
    vad = SileroVoiceActivityDetector()
    size = vad.chunk_samples()
    speech = np.array([vad(pcm[i:i + size].tobytes()) >= 0.5 for i in range(0, len(pcm) - size + 1, size)])
    hits = np.flatnonzero(np.convolve(speech, np.ones(3), "valid") == 3)
    clip_s = len(pcm) / RATE
    start, end = (hits[0] * size / RATE, (hits[-1] + 3) * size / RATE) if len(hits) else (0.0, clip_s)
    rows.append({"file": path.name, "clip_s": f"{clip_s:.3f}", "speech_start_s": f"{start:.3f}", "speech_end_s": f"{end:.3f}",
                 "leading_silence_s": f"{start:.3f}", "trailing_silence_s": f"{clip_s - end:.3f}", "method": "silero 0.5/3 frames"})
out = folder / "speech_bounds.csv"
with out.open("w", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(f"{out}: {len(rows)} clips")
