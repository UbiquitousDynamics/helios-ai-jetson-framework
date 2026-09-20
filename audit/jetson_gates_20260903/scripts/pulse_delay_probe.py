#!/usr/bin/env python3
"""Measure Pulse render, monitor, and raw-microphone timing with a paced chirp."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import chirp, correlate, resample_poly


CAPTURE_RATE = 16_000
RENDER_RATE = 22_050
FRAME_SAMPLES = 320


class Recorder:
    def __init__(self, source: str, channel_map: str | None) -> None:
        self.source = source
        self.channel_map = channel_map
        self.blocks: list[bytes] = []
        self.block_end_times: list[float] = []
        self.stderr = b""
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()

    def start(self) -> None:
        command = [
                "parec",
                "--raw",
                "--format=s16le",
                f"--rate={CAPTURE_RATE}",
                "--channels=1",
                "--latency-msec=20",
                "--process-time-msec=20",
                f"--device={self.source}",
                "--client-name=helios-audit-delay-probe",
            ]
        if self.channel_map:
            command.append(f"--channel-map={self.channel_map}")
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.thread = threading.Thread(target=self._read, daemon=False)
        self.thread.start()

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        size = FRAME_SAMPLES * 2
        while not self.stop.is_set():
            block = self.process.stdout.read(size)
            ended = time.monotonic()
            if not block:
                break
            if len(block) < size:
                block += self.process.stdout.read(size - len(block))
            self.blocks.append(block)
            self.block_end_times.append(ended)

    def close(self) -> None:
        self.stop.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.process is not None:
            try:
                _, self.stderr = self.process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, self.stderr = self.process.communicate()

    @property
    def audio(self) -> np.ndarray:
        return np.frombuffer(b"".join(self.blocks), dtype="<i2").copy()

    @property
    def sample_zero_time(self) -> float:
        if not self.block_end_times:
            raise RuntimeError(f"no data from {self.source}")
        return self.block_end_times[0] - len(self.blocks[0]) / 2 / CAPTURE_RATE


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CAPTURE_RATE)
        handle.writeframes(np.asarray(audio, dtype="<i2").tobytes())


def locate(recording: np.ndarray, reference: np.ndarray) -> tuple[int, float]:
    recording_f = recording.astype(np.float64)
    reference_f = reference.astype(np.float64)
    recording_f -= float(np.mean(recording_f))
    reference_f -= float(np.mean(reference_f))
    values = correlate(recording_f, reference_f, mode="valid", method="fft")
    index = int(np.argmax(np.abs(values)))
    window = recording_f[index : index + len(reference_f)]
    denominator = math.sqrt(
        max(float(np.dot(reference_f, reference_f)), 1e-30)
        * max(float(np.dot(window, window)), 1e-30)
    )
    return index, float(abs(values[index]) / denominator)


parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True)
parser.add_argument("--monitor", required=True)
parser.add_argument("--sink", required=True)
parser.add_argument("--source-channel-map")
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--amplitude", type=float, default=0.03)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)

duration = 2.0
lead = 0.5
tail = 0.5
t = np.arange(round(duration * RENDER_RATE)) / RENDER_RATE
probe = chirp(t, f0=350, f1=3_500, t1=duration, method="logarithmic")
probe = (probe * np.hanning(len(probe)) * args.amplitude * 32767).astype("<i2")
render = np.concatenate(
    (
        np.zeros(round(lead * RENDER_RATE), dtype="<i2"),
        probe,
        np.zeros(round(tail * RENDER_RATE), dtype="<i2"),
    )
)
divisor = math.gcd(CAPTURE_RATE, RENDER_RATE)
reference16 = resample_poly(
    probe.astype(np.float64), CAPTURE_RATE // divisor, RENDER_RATE // divisor
)

raw = Recorder(args.source, args.source_channel_map)
monitor = Recorder(args.monitor, "mono")
raw.start()
monitor.start()
time.sleep(0.5)
player = subprocess.Popen(
    [
        "pacat",
        "--raw",
        "--playback",
        "--format=s16le",
        f"--rate={RENDER_RATE}",
        "--channels=1",
        "--latency-msec=100",
        "--process-time-msec=20",
        f"--device={args.sink}",
        "--client-name=helios-audit-delay-probe",
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
)
assert player.stdin is not None
block_samples = round(0.1 * RENDER_RATE)
write_origin = time.monotonic()
for offset in range(0, len(render), block_samples):
    target = write_origin + offset / RENDER_RATE
    remaining = target - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    player.stdin.write(render[offset : offset + block_samples].tobytes())
    player.stdin.flush()
player.stdin.close()
player.wait(timeout=5)
player_stderr = player.stderr.read().decode(errors="replace") if player.stderr else ""
time.sleep(1.0)
raw.close()
monitor.close()

raw_audio = raw.audio
monitor_audio = monitor.audio
raw_index, raw_correlation = locate(raw_audio, reference16)
monitor_index, monitor_correlation = locate(monitor_audio, reference16)
raw_time = raw.sample_zero_time + raw_index / CAPTURE_RATE
monitor_time = monitor.sample_zero_time + monitor_index / CAPTURE_RATE
expected_app_time = write_origin + lead

write_wav(args.out / "raw_mic.wav", raw_audio)
write_wav(args.out / "sink_monitor.wav", monitor_audio)
result = {
    "source": args.source,
    "monitor": args.monitor,
    "sink": args.sink,
    "capture_rate": CAPTURE_RATE,
    "render_rate": RENDER_RATE,
    "amplitude": args.amplitude,
    "write_origin_monotonic": write_origin,
    "expected_probe_app_time": expected_app_time,
    "raw_probe_index": raw_index,
    "monitor_probe_index": monitor_index,
    "raw_correlation": raw_correlation,
    "monitor_correlation": monitor_correlation,
    "app_to_monitor_ms": (monitor_time - expected_app_time) * 1000,
    "app_to_raw_mic_ms": (raw_time - expected_app_time) * 1000,
    "monitor_to_raw_mic_ms": (raw_time - monitor_time) * 1000,
    "raw_stderr": raw.stderr.decode(errors="replace"),
    "monitor_stderr": monitor.stderr.decode(errors="replace"),
    "player_stderr": player_stderr,
}
(args.out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(json.dumps(result, sort_keys=True))
