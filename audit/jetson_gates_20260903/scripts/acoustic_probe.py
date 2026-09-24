#!/usr/bin/env python3
"""Low-level repeatable acoustic coupling probe for one input/output pair."""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.io import wavfile
from scipy.signal import chirp, correlate


def rms(values: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(values.astype(np.float64)))))


parser = argparse.ArgumentParser()
parser.add_argument("--input", type=int, required=True)
parser.add_argument("--output", type=int, required=True)
parser.add_argument("--output-channels", type=int, default=1)
parser.add_argument("--rate", type=int, default=16_000)
parser.add_argument("--amplitude", type=float, default=0.03)
parser.add_argument("--wav", type=Path, required=True)
parser.add_argument("--json", type=Path, required=True)
args = parser.parse_args()

rate = args.rate
lead_seconds = 1.0
probe_seconds = 2.0
tail_seconds = 1.0
probe_count = round(probe_seconds * rate)
timeline = np.arange(probe_count, dtype=np.float64) / rate
probe = chirp(timeline, f0=350, f1=3_500, t1=probe_seconds, method="logarithmic")
probe *= np.hanning(probe_count)
probe = (probe * args.amplitude).astype(np.float32)
playback_mono = np.concatenate(
    (
        np.zeros(round(lead_seconds * rate), dtype=np.float32),
        probe,
        np.zeros(round(tail_seconds * rate), dtype=np.float32),
    )
)
playback = np.repeat(playback_mono[:, None], args.output_channels, axis=1)

captured: list[np.ndarray] = []
input_status: list[str] = []
output_status: list[str] = []
capture_started_ns = 0
playback_started_ns = 0


def input_callback(indata, frames, time_info, status):
    if status:
        input_status.append(str(status))
    captured.append(indata[:, 0].copy())


def writer(stream: sd.OutputStream) -> None:
    global playback_started_ns
    playback_started_ns = time.perf_counter_ns()
    stream.write(playback)


started = time.perf_counter()
with (
    sd.InputStream(
        device=args.input,
        samplerate=rate,
        channels=1,
        dtype="float32",
        blocksize=320,
        latency="high",
        callback=input_callback,
    ) as input_stream,
    sd.OutputStream(
        device=args.output,
        samplerate=rate,
        channels=args.output_channels,
        dtype="float32",
        blocksize=320,
        latency="high",
    ) as output_stream,
):
    capture_started_ns = time.perf_counter_ns()
    thread = threading.Thread(target=writer, args=(output_stream,), daemon=False)
    thread.start()
    thread.join()
    time.sleep(0.75)
elapsed = time.perf_counter() - started

recording = np.concatenate(captured) if captured else np.empty(0, dtype=np.float32)
args.wav.parent.mkdir(parents=True, exist_ok=True)
wavfile.write(args.wav, rate, np.clip(recording * 32767, -32768, 32767).astype(np.int16))

expected_start = max(0, round((playback_started_ns - capture_started_ns) / 1e9 * rate))
pre = recording[: max(1, expected_start + round(0.8 * rate))]
during_start = expected_start + round(lead_seconds * rate)
during_end = during_start + probe_count
during = recording[during_start:during_end]
post = recording[during_end : during_end + round(0.8 * rate)]

if len(recording) >= len(probe):
    centered_recording = recording.astype(np.float64) - float(np.mean(recording))
    centered_probe = probe.astype(np.float64) - float(np.mean(probe))
    correlation = correlate(centered_recording, centered_probe, mode="valid", method="fft")
    best_index = int(np.argmax(np.abs(correlation)))
    best_window = centered_recording[best_index : best_index + len(probe)]
    denominator = math.sqrt(
        max(float(np.dot(centered_probe, centered_probe)), 1e-30)
        * max(float(np.dot(best_window, best_window)), 1e-30)
    )
    peak_normalized = float(abs(correlation[best_index]) / denominator)
    acoustic_start_sample = best_index
else:
    peak_normalized = 0.0
    acoustic_start_sample = -1

baseline_rms = rms(pre) if len(pre) else 0.0
during_rms = rms(during) if len(during) else 0.0
result = {
    "input": args.input,
    "input_name": sd.query_devices(args.input)["name"],
    "output": args.output,
    "output_name": sd.query_devices(args.output)["name"],
    "output_channels": args.output_channels,
    "rate": rate,
    "amplitude": args.amplitude,
    "recorded_samples": int(len(recording)),
    "elapsed_s": elapsed,
    "expected_probe_start_sample": during_start,
    "correlated_probe_start_sample": acoustic_start_sample,
    "estimated_extra_delay_ms": (
        1000.0 * (acoustic_start_sample - during_start) / rate
        if acoustic_start_sample >= 0
        else None
    ),
    "normalized_correlation_peak": peak_normalized,
    "pre_rms": baseline_rms,
    "during_rms": during_rms,
    "post_rms": rms(post) if len(post) else 0.0,
    "during_to_pre_db": (20 * math.log10(max(during_rms, 1e-12) / max(baseline_rms, 1e-12))),
    "input_status": input_status,
    "output_status": output_status,
}
args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(json.dumps(result, sort_keys=True))
