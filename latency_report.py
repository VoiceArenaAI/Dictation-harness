# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
import argparse
import csv
import random
import re


def pct(v, q):
    s = sorted(v)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ci(v, q, rng, n=10000):
    stats = sorted(pct(rng.choices(v, k=len(v)), q) for _ in range(n))
    return stats[int(0.025 * n)], stats[int(0.975 * n)]


def col(rows, key, scale=1.0):
    return [float(r[key]) * scale for r in rows if r.get(key) not in (None, "")]


def visible(text):
    text = re.sub(r"\s*</li>\s*</[uo]l>", "", text or "")
    text = re.sub(r"<[uo]l>\s*<li>", "• ", text)
    text = re.sub(r"\s*</li>\s*<li>\s*", " • ", text)
    return " ".join(re.sub(r"(?m)^\s*-\s+", "• ", text).split())


def same(a, b):
    return visible(a) == visible(b)


p = argparse.ArgumentParser()
p.add_argument("results")
p.add_argument("--old", help="earlier results file, for the same clips under the earlier method")
p.add_argument("-o", "--out")
p.add_argument("--speech-bounds", help="speech_bounds.py output: time live tools from where speech really starts and ends")
args = p.parse_args()

allrows = list(csv.DictReader(open(args.results, newline="", encoding="utf-8")))
rows = [r for r in allrows if r["type"] != "silence" and r["status"] == "ok"]
rng = random.Random(1604)

PAD_S = 0.3
live_rows = [r for r in rows if r.get("edits_total") not in (None, "")]
stalled = [r for r in live_rows if len(r["transcript"]) - len(r["text_before_stop"]) > 20]
steady = [r for r in live_rows if r not in stalled]
speech_src = "volume"
settle_ms = [float(r["stream_settle_s"]) * 1000 for r in steady if r.get("stream_settle_s")]
first_ms = [float(r["first_text_s"]) * 1000 for r in live_rows if r.get("first_text_s")]
if args.speech_bounds and live_rows:
    bounds = {}
    for b in csv.DictReader(open(args.speech_bounds, newline="", encoding="utf-8")):
        bounds[b.get("clip_id") or b["file"].rsplit(".", 1)[0]] = (float(b.get("vad_start_s") or b["speech_start_s"]),
                                                                   float(b.get("vad_end_s") or b["speech_end_s"]))
    speech_src = "VAD"
    settle_ms = [(float(r["stream_settle_s"]) + float(r["speech_end_s"]) - PAD_S - bounds[r["clip_id"]][1]) * 1000
                 for r in steady if r.get("stream_settle_s")]
    first_ms = [(float(r["first_text_s"]) + float(r["speech_start_s"]) - PAD_S - bounds[r["clip_id"]][0]) * 1000
                for r in live_rows if r.get("first_text_s")]

table = [("Vendor latency: stop received -> last text change", col(rows, "vendor_latency_s", 1000)),
         (f"Live: end of speech ({speech_src}) -> last text change before stop", settle_ms),
         (f"Live: start of speech ({speech_src}) -> first text", first_ms),
         ("Earlier definition's clock: stop sent -> last text change", col(rows, "finalize_s", 1000)),
         ("Our side: begin sending stop -> stop sent", col(rows, "stop_signal_s", 1000)),
         ("Delivery: stop sent -> capture stopped", col(rows, "stop_received_s", 1000))]
if args.old:
    old = {r["file"]: r for r in csv.DictReader(open(args.old, newline="", encoding="utf-8"))}
    table.append(("EARLIER run, same clips: stop sent, 100 ms poll", [float(old[r["file"]]["finalize_s"]) * 1000 for r in rows]))

out = []
print(f"{'measure (ms)':58s}{'n':>5s}{'p50':>9s}{'p50 95% CI':>18s}{'p95':>9s}{'p95 95% CI':>20s}{'max':>9s}{'min':>9s}")
for label, v in table:
    if not v:
        continue
    a, b = ci(v, 0.5, rng), ci(v, 0.95, rng)
    print(f"{label:58s}{len(v):5d}{pct(v,.5):9.1f}{f'{a[0]:.0f}-{a[1]:.0f}':>18s}{pct(v,.95):9.1f}{f'{b[0]:.0f}-{b[1]:.0f}':>20s}{max(v):9.1f}{min(v):9.1f}")
    out.append({"measure": label, "n": len(v), "p50_ms": round(pct(v, .5), 1), "p50_ci_low": round(a[0], 1), "p50_ci_high": round(a[1], 1),
                "p95_ms": round(pct(v, .95), 1), "p95_ci_low": round(b[0], 1), "p95_ci_high": round(b[1], 1),
                "max_ms": round(max(v), 1), "min_ms": round(min(v), 1)})
if args.out:
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)

text_gap, recv_gap = col(rows, "text_poll_gap_ms"), col(rows, "received_poll_gap_ms")
changed = [r for r in rows if r.get("vendor_latency_s")]
print(f"\nPrecision per clip: text gap median {pct(text_gap,.5) if text_gap else float('nan'):.1f} ms, worst {max(text_gap) if text_gap else float('nan'):.1f} ms; "
      f"stop-received gap median {pct(recv_gap,.5):.1f} ms, worst {max(recv_gap):.1f} ms")
print(f"Text changed after the stop on {len(changed)}/{len(rows)} clips; landed in one change on "
      f"{sum(abs(float(r['first_change_s']) - float(r['vendor_latency_s'])) < 0.0001 for r in changed)}/{len(changed)}; "
      f"Accessibility misses: {sum(int(r['ax_misses'] or 0) for r in rows)}")
if live_rows:
    n, rw, rc = col(live_rows, "edits_total"), col(live_rows, "rewrites"), col(live_rows, "rewrite_chars")
    settle = settle_ms
    print(f"Live typing, per clip (median): {pct(n,.5):.0f} text changes, {pct(rw,.5):.0f} of them rewrites of earlier text, {pct(rc,.5):.0f} characters rewritten; "
          f"changes after the stop: median {pct(col(live_rows,'edits_after_stop'),.5):.0f}")
    print(f"Live poll gap median {pct(col(live_rows,'live_poll_gap_p50_ms'),.5):.2f} ms, worst {max(col(live_rows,'live_poll_gap_max_ms')):.1f} ms; "
          f"playback ended {pct(col(live_rows,'playback_lag_s',1000),.5):.0f} ms later than planned (median); "
          f"negative settle times: {sum(x < 0 for x in settle)}/{len(settle)}; streaming stalled (excluded from settle): {[r['clip_id'] for r in stalled]}")
for k in ("poll_ms", "power", "mic", "wispr_plan", "wispr_status", "system_input", "chatgpt_app", "chatgpt_mode",
          "chatgpt_model", "chatgpt_plan", "gemini_app", "gemini_model", "local_asr_started", "claude_mic", "claude_lang",
          "dictation_lang", "dictation_mic"):
    if any(r.get(k) for r in rows):
        print(f"{k}: {sorted({r.get(k, '') for r in rows})}")

print("\nAcceptance checks")


def check(name, ok, detail):
    print(f"  [{'PASS' if ok is True else 'FAIL' if ok is False else 'N/A '}] {name}: {detail}")


speech = [r for r in allrows if r["type"] != "silence"]
retried = sum(int(r["attempts"] or 0) > 1 for r in rows)
check("Rows ok", len(rows) == len(speech) and len(rows) > 0, f"{len(rows)}/{len(speech)}, {retried} retried")
poll = pct(col(rows, "poll_gap_p50_ms"), .5)
check("Median gap between polls about 10 ms", 8 <= poll <= 15, f"{poll:.2f} ms")
after = sum(float(r["vendor_latency_s"]) > float(r["poll_start_s"]) for r in changed)
check("Text arrived after polling began", after == len(changed), f"{after}/{len(changed)} clips whose text changed after the stop")
stop_ui = col(rows, "stop_ui_after_received_ms")
if stop_ui:
    check("Second sign: stop button changed, relative to received", None,
          f"{len(stop_ui)}/{len(rows)} clips, median {pct(stop_ui,.5):+.1f} ms, range {min(stop_ui):+.1f} to {max(stop_ui):+.1f} ms")
print(f"  [info] Stop received after the signal was sent: median {pct(col(rows,'stop_received_s',1000),.5):.1f} ms, "
      f"worst {max(col(rows,'stop_received_s',1000)):.1f} ms")
saved_col = "wispr_pasted" if rows and "wispr_pasted" in rows[0] else "wispr_text"
saved = [r for r in rows if r.get(saved_col)]
if saved:
    match = sum(same(r["transcript"], r[saved_col]) for r in saved)
    check("Pasted text equals the tool's saved text", match == len(saved), f"{match}/{len(saved)} (against {saved_col})")
long_enough = [float(r["wispr_duration_s"]) >= float(r["clip_s"]) for r in rows if r.get("wispr_duration_s")]
if long_enough:
    check("Recording at least as long as the clip", all(long_enough), f"{sum(long_enough)}/{len(long_enough)}")
