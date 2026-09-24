#!/usr/bin/env python3
"""On-device scripted barge-in A/B harness for Helios commit b86781c.

This deliberately imports the deployed Python orchestration and recognition code.  It
does not modify application source.  Audio transport uses PulseAudio so the paired
system echo-cancel sink/source can be selected without changing server defaults.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import correlate, resample_poly


CAPTURE_RATE = 16_000
RENDER_RATE = 22_050
AEC_FRAME_SAMPLES = 320
VOSK_FRAME_SAMPLES = 1_600
PULSE_FRAME_MS = 20
PLAYBACK_BLOCK_SAMPLES = 2_205
EXPECTED_COMMIT = "b86781c7c424ce3a9972198b3a8f0470025461f1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--path", choices=("baseline", "pulse_webrtc", "pywebrtc"), required=True)
    parser.add_argument("--mode", choices=("interruption", "echo_only"), required=True)
    parser.add_argument("--trials", type=int, required=True)
    parser.add_argument("--raw-source", required=True)
    parser.add_argument("--raw-source-channel-map", default="front-left")
    parser.add_argument("--processing-source", required=True)
    parser.add_argument("--processing-source-channel-map", default="front-left")
    parser.add_argument("--far-sink", required=True)
    parser.add_argument("--near-sink", required=True)
    parser.add_argument("--monitor-source")
    parser.add_argument("--stream-delay-ms", type=int, default=35)
    parser.add_argument("--pulse-latency-ms", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260903)
    parser.add_argument("--far-active-rms", type=float, default=0.03)
    return parser.parse_args()


ARGS = parse_args()
if ARGS.trials < 1:
    raise SystemExit("--trials must be positive")
if ARGS.path == "pulse_webrtc" and ARGS.processing_source == ARGS.raw_source:
    raise SystemExit("Pulse WebRTC must capture its virtual echo-cancel source")

os.environ.setdefault("HELIOS_LLM_CONFIG", "")
os.environ.setdefault("HELIOS_LLM_REMOTE_ENABLED", "false")
os.environ.setdefault("HELIOS_LLM_EMERGENCY_LOCAL_ONLY", "true")
os.environ.setdefault("HELIOS_KPI_ENABLED", "false")
os.environ.setdefault("HELIOS_LOG_FILE", "-")
sys.path.insert(0, str(ARGS.repo.resolve()))

import config  # noqa: E402
from assistant import VoiceAssistant  # noqa: E402
from recognizer.barge_in_detector import BargeInDetector  # noqa: E402
from recognizer.echo_suppression_policy import ConservativeEchoSuppressionPolicy  # noqa: E402
from recognizer.speech_recognizer import SpeechRecognizer  # noqa: E402
from vosk import KaldiRecognizer, Model, SetLogLevel  # noqa: E402

if ARGS.path == "pywebrtc":
    from pywebrtc_audio import AudioProcessor, VoiceDetector  # noqa: E402


ARGS.out.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler(ARGS.out / "assistant.log", encoding="utf-8")],
)
LOGGER = logging.getLogger("helios.audit.barge_in_ab")
SetLogLevel(-1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def normalized_words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise RuntimeError(f"unsupported WAV width={width}: {path}")
    values = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    mono = np.mean(values.astype(np.float64), axis=1)
    return rate, np.clip(mono, -32768, 32767).astype("<i2")


def write_wav(path: Path, rate: int, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.asarray(audio, dtype="<i2").tobytes())


def resample_int16(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return np.asarray(audio, dtype="<i2").copy()
    divisor = math.gcd(source_rate, target_rate)
    output = resample_poly(
        audio.astype(np.float64), target_rate // divisor, source_rate // divisor
    )
    return np.clip(output, -32768, 32767).astype("<i2")


def active_rms(audio: np.ndarray) -> float:
    floating = audio.astype(np.float64) / 32768.0
    active = floating[np.abs(floating) >= 10 ** (-45 / 20)]
    return float(np.sqrt(np.mean(active * active))) if len(active) else 0.0


def scale_to_rms(audio: np.ndarray, target: float) -> tuple[np.ndarray, float]:
    current = active_rms(audio)
    if current <= 0:
        raise RuntimeError("cannot normalize silent audio")
    gain = target / current
    peak = float(np.max(np.abs(audio.astype(np.float64) / 32768.0))) * gain
    if peak > 0.92:
        gain *= 0.92 / peak
    output = np.clip(audio.astype(np.float64) * gain, -32768, 32767).astype("<i2")
    return output, gain


def read_exact(handle: Any, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = handle.read(remaining)
        if not chunk:
            raise EOFError(f"audio pipe ended with {remaining} byte(s) missing")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def parec_command(source: str, channel_map: str | None, client_name: str) -> list[str]:
    command = [
        "parec",
        "--raw",
        "--format=s16le",
        f"--rate={CAPTURE_RATE}",
        "--channels=1",
        f"--latency-msec={ARGS.pulse_latency_ms}",
        f"--process-time-msec={PULSE_FRAME_MS}",
        f"--device={source}",
        f"--client-name={client_name}",
    ]
    if channel_map:
        command.append(f"--channel-map={channel_map}")
    return command


def pacat_command(sink: str, client_name: str) -> list[str]:
    return [
        "pacat",
        "--raw",
        "--playback",
        "--format=s16le",
        f"--rate={RENDER_RATE}",
        "--channels=1",
        f"--latency-msec={ARGS.pulse_latency_ms}",
        f"--process-time-msec={PULSE_FRAME_MS}",
        "--volume=65536",
        f"--device={sink}",
        f"--client-name={client_name}",
    ]


class ContinuousRecorder:
    def __init__(self, source: str, channel_map: str | None, client_name: str) -> None:
        self.source = source
        self.channel_map = channel_map
        self.client_name = client_name
        self.blocks: list[bytes] = []
        self.block_end_times: list[float] = []
        self.stderr = ""
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        self.process = subprocess.Popen(
            parec_command(self.source, self.channel_map, self.client_name),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.thread = threading.Thread(target=self._reader, daemon=False)
        self.thread.start()

    def _reader(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        size = AEC_FRAME_SAMPLES * 2
        try:
            while not self.stop_event.is_set():
                block = read_exact(self.process.stdout, size)
                self.blocks.append(block)
                self.block_end_times.append(time.monotonic())
        except EOFError:
            return

    def close(self) -> None:
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.process is not None:
            try:
                _, stderr = self.process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, stderr = self.process.communicate()
            self.stderr = stderr.decode(errors="replace") if stderr else ""

    @property
    def audio(self) -> np.ndarray:
        return np.frombuffer(b"".join(self.blocks), dtype="<i2").copy()

    @property
    def sample_zero_time(self) -> float | None:
        if not self.block_end_times:
            return None
        return self.block_end_times[0] - len(self.blocks[0]) / 2 / CAPTURE_RATE


class PlaybackTTS:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.controller: FarPlayback | None = None
        self.events: list[dict[str, Any]] = []

    def configure(self, controller: "FarPlayback") -> None:
        with self.lock:
            self.controller = controller
            self.events = []

    def _event(self, name: str) -> None:
        self.events.append({"event": name, "monotonic": time.monotonic()})

    @property
    def is_speaking(self) -> bool:
        with self.lock:
            return bool(self.controller and self.controller.speaking)

    @property
    def active_playback_started_at(self) -> float | None:
        with self.lock:
            return self.controller.origin if self.controller and self.controller.speaking else None

    @property
    def active_playback_text(self) -> str | None:
        with self.lock:
            return self.controller.text if self.controller and self.controller.speaking else None

    @property
    def last_playback_text(self) -> str | None:
        with self.lock:
            return self.controller.text if self.controller else None

    @property
    def last_playback_window(self) -> tuple[float, float | None] | None:
        with self.lock:
            if not self.controller or self.controller.origin is None:
                return None
            return self.controller.origin, self.controller.ended_at

    def duck(self) -> bool:
        with self.lock:
            controller = self.controller
        self._event("duck_requested")
        if controller:
            controller.paused.set()
            return True
        return False

    def resume(self) -> bool:
        with self.lock:
            controller = self.controller
        was_paused = bool(controller and controller.paused.is_set())
        if controller:
            controller.paused.clear()
        self._event("resume_requested")
        return was_paused

    def interrupt(self) -> bool:
        with self.lock:
            controller = self.controller
        active = bool(controller and controller.speaking)
        self._event("interrupt_requested")
        if controller:
            controller.stop_event.set()
        return active

    def close(self) -> None:
        self.interrupt()


class FarPlayback:
    def __init__(
        self,
        pcm: np.ndarray,
        text: str,
        duration_s: float,
        sink: str,
        input_ready: threading.Event,
        response_future: concurrent.futures.Future,
        tts: PlaybackTTS,
    ) -> None:
        total_blocks = math.ceil(duration_s * 10)
        repeated = np.resize(pcm, total_blocks * PLAYBACK_BLOCK_SAMPLES).astype("<i2")
        self.content22 = repeated
        self.content16 = resample_int16(repeated, RENDER_RATE, CAPTURE_RATE)
        self.text = text
        self.sink = sink
        self.input_ready = input_ready
        self.response_future = response_future
        self.tts = tts
        self.total_blocks = total_blocks
        self.paused = threading.Event()
        self.stop_event = threading.Event()
        self.started = threading.Event()
        self.speaking = False
        self.origin: float | None = None
        self.ended_at: float | None = None
        self.process_exit: int | None = None
        self.stderr = ""
        self.blocks: list[dict[str, Any]] = []
        self.block_map: dict[int, int | None] = {}
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=False)

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            self.stop_event.set()
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("far playback thread did not stop")

    def _run(self) -> None:
        process: subprocess.Popen | None = None
        content_block = 0
        try:
            if not self.input_ready.wait(5):
                raise RuntimeError("capture did not become ready")
            time.sleep(1.0)
            process = subprocess.Popen(
                pacat_command(self.sink, "helios-audit-far"),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            self.origin = time.monotonic()
            self.speaking = True
            self.started.set()
            for wall_block in range(self.total_blocks):
                target = self.origin + wall_block / 10
                remaining = target - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if self.stop_event.is_set():
                    break
                active = not self.paused.is_set()
                mapped = content_block if active else None
                if active:
                    begin = content_block * PLAYBACK_BLOCK_SAMPLES
                    block = self.content22[begin : begin + PLAYBACK_BLOCK_SAMPLES]
                    content_block += 1
                else:
                    block = np.zeros(PLAYBACK_BLOCK_SAMPLES, dtype="<i2")
                sent_at = time.monotonic()
                process.stdin.write(block.tobytes())
                process.stdin.flush()
                with self.lock:
                    self.block_map[wall_block] = mapped
                    self.blocks.append(
                        {
                            "wall_block": wall_block,
                            "content_block": mapped,
                            "target": target,
                            "sent_at": sent_at,
                            "nonzero": bool(np.any(block)),
                        }
                    )
            if self.stop_event.is_set():
                process.terminate()
            else:
                process.stdin.close()
            try:
                self.process_exit = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                self.process_exit = process.wait(timeout=2)
            self.stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        except Exception as exc:
            self.stderr += f"{type(exc).__name__}: {exc}\n"
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        finally:
            self.speaking = False
            self.ended_at = time.monotonic()
            if not self.response_future.done():
                self.response_future.set_result(None)
            self.started.set()

    def reference_for_time(self, frame_start_time: float) -> np.ndarray:
        if self.origin is None or frame_start_time < self.origin:
            return np.zeros(AEC_FRAME_SAMPLES, dtype=np.int16)
        wall_frame = int((frame_start_time - self.origin) * 50)
        if wall_frame < 0:
            return np.zeros(AEC_FRAME_SAMPLES, dtype=np.int16)
        wall_block, subframe = divmod(wall_frame, 5)
        with self.lock:
            content_block = self.block_map.get(wall_block)
        if content_block is None:
            return np.zeros(AEC_FRAME_SAMPLES, dtype=np.int16)
        begin = content_block * 1600 + subframe * AEC_FRAME_SAMPLES
        block = self.content16[begin : begin + AEC_FRAME_SAMPLES]
        if len(block) < AEC_FRAME_SAMPLES:
            block = np.pad(block, (0, AEC_FRAME_SAMPLES - len(block)))
        return np.ascontiguousarray(block, dtype=np.int16)

    def actual_reference16(self) -> np.ndarray:
        blocks = []
        with self.lock:
            mappings = [self.block_map.get(index) for index in range(len(self.blocks))]
        for content_block in mappings:
            if content_block is None:
                blocks.append(np.zeros(1600, dtype="<i2"))
            else:
                begin = content_block * 1600
                blocks.append(self.content16[begin : begin + 1600])
        return np.concatenate(blocks) if blocks else np.empty(0, dtype="<i2")


class NearPlayback:
    def __init__(self, pcm: np.ndarray, sink: str, far: FarPlayback, offset_s: float) -> None:
        self.pcm = pcm
        self.sink = sink
        self.far = far
        self.offset_s = offset_s
        self.origin: float | None = None
        self.ended_at: float | None = None
        self.stderr = ""
        self.process_exit: int | None = None
        self.thread = threading.Thread(target=self._run, daemon=False)

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise RuntimeError("near playback thread did not stop")

    def _run(self) -> None:
        if not self.far.started.wait(8) or self.far.origin is None:
            self.stderr = "far playback did not start"
            return
        target_origin = self.far.origin + self.offset_s
        remaining = target_origin - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        process = subprocess.Popen(
            pacat_command(self.sink, "helios-audit-near-proxy"),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        self.origin = time.monotonic()
        for offset in range(0, len(self.pcm), PLAYBACK_BLOCK_SAMPLES):
            target = self.origin + offset / RENDER_RATE
            remaining = target - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            process.stdin.write(self.pcm[offset : offset + PLAYBACK_BLOCK_SAMPLES].tobytes())
            process.stdin.flush()
        process.stdin.close()
        try:
            self.process_exit = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            self.process_exit = process.wait(timeout=2)
        self.stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        self.ended_at = time.monotonic()


class CaptureStream:
    def __init__(self, source: str, channel_map: str | None, far: FarPlayback, path: str) -> None:
        self.source = source
        self.channel_map = channel_map
        self.far = far
        self.path = path
        self.process: subprocess.Popen | None = None
        self.stderr = ""
        self.input_ready = far.input_ready
        self.frame_end_times: list[float] = []
        self.raw_blocks: list[bytes] = []
        self.clean_blocks: list[bytes] = []
        self.frame_records: list[dict[str, Any]] = []
        self.closed = False
        self.processor = None
        self.detector = None
        if path == "pywebrtc":
            self.processor = AudioProcessor(
                sample_rate=CAPTURE_RATE,
                echo_cancellation=True,
                noise_suppression=True,
                auto_gain_control=True,
                stream_delay_ms=ARGS.stream_delay_ms,
            )
            self.detector = VoiceDetector(sample_rate=CAPTURE_RATE)

    def start_stream(self) -> None:
        self.process = subprocess.Popen(
            parec_command(self.source, self.channel_map, "helios-audit-processing"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.input_ready.set()

    def read(self, frames: int, exception_on_overflow: bool = False) -> bytes:
        del exception_on_overflow
        if self.closed or self.process is None or self.process.stdout is None:
            return b""
        blocks_needed = math.ceil(frames / AEC_FRAME_SAMPLES)
        clean_parts = []
        for _ in range(blocks_needed):
            raw = read_exact(self.process.stdout, AEC_FRAME_SAMPLES * 2)
            ended = time.monotonic()
            frame_start = ended - PULSE_FRAME_MS / 1000
            values = np.frombuffer(raw, dtype="<i2").copy()
            far = self.far.reference_for_time(frame_start)
            started_ns = time.perf_counter_ns()
            if self.processor is None:
                clean = values
                probability = None
            else:
                clean = np.ascontiguousarray(self.processor.process(values, far), dtype=np.int16)
                probability = float(self.detector.process(clean)) if self.detector else None
            processing_ns = time.perf_counter_ns() - started_ns
            clean_bytes = clean.astype("<i2", copy=False).tobytes()
            self.raw_blocks.append(raw)
            self.clean_blocks.append(clean_bytes)
            self.frame_end_times.append(ended)
            self.frame_records.append(
                {
                    "sequence": len(self.frame_records),
                    "frame_start_monotonic": frame_start,
                    "frame_end_monotonic": ended,
                    "processing_ns": processing_ns,
                    "raw_rms": float(np.sqrt(np.mean((values.astype(np.float64) / 32768) ** 2))),
                    "clean_rms": float(np.sqrt(np.mean((clean.astype(np.float64) / 32768) ** 2))),
                    "far_rms": float(np.sqrt(np.mean((far.astype(np.float64) / 32768) ** 2))),
                    "speech_probability": probability,
                }
            )
            clean_parts.append(clean_bytes)
        combined = b"".join(clean_parts)
        return combined[: frames * 2]

    def stop_stream(self) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        if self.process is not None:
            try:
                _, stderr = self.process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, stderr = self.process.communicate()
            self.stderr = stderr.decode(errors="replace") if stderr else ""

    @property
    def sample_zero_time(self) -> float | None:
        return self.frame_end_times[0] - PULSE_FRAME_MS / 1000 if self.frame_end_times else None


class AudioInterface:
    def __init__(self, stream: CaptureStream) -> None:
        self.stream = stream

    def open(self, **kwargs: Any) -> CaptureStream:
        if kwargs.get("channels") != 1 or kwargs.get("rate") != CAPTURE_RATE:
            raise RuntimeError(f"unexpected deployed capture request: {kwargs}")
        return self.stream

    def terminate(self) -> None:
        self.stream.close()


class LoggedKaldi:
    def __init__(self, model: Any, rate: int, records: list[dict[str, Any]]) -> None:
        self.inner = KaldiRecognizer(model, rate)
        self.records = records

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def AcceptWaveform(self, data: bytes) -> bool:
        started = time.monotonic()
        accepted = bool(self.inner.AcceptWaveform(data))
        self.records.append(
            {
                "kind": "accept_waveform",
                "started": started,
                "ended": time.monotonic(),
                "bytes": len(data),
                "accepted": accepted,
            }
        )
        return accepted

    def _result(self, method: str) -> str:
        started = time.monotonic()
        payload = str(getattr(self.inner, method)())
        self.records.append(
            {
                "kind": method,
                "started": started,
                "ended": time.monotonic(),
                "payload": payload,
            }
        )
        return payload

    def Result(self) -> str:
        return self._result("Result")

    def PartialResult(self) -> str:
        return self._result("PartialResult")

    def FinalResult(self) -> str:
        return self._result("FinalResult")


class LoggedRecognizer:
    def __init__(self, inner: SpeechRecognizer, records: list[dict[str, Any]]) -> None:
        self.inner = inner
        self.records = records

    def listen_events(self, *args: Any, **kwargs: Any):
        for event in self.inner.listen_events(*args, **kwargs):
            self.records.append(
                {
                    "observed": time.monotonic(),
                    "text": event.text,
                    "is_final": event.is_final,
                    "frame_energy": event.frame_energy,
                    "segment_id": event.segment_id,
                    "segment_started_at": event.segment_started_at,
                    "confidence": event.confidence,
                    "speech_duration_seconds": event.speech_duration_seconds,
                    "segment_peak_energy": event.segment_peak_energy,
                    "word_confidences": event.word_confidences,
                    "word_timings": event.word_timings,
                }
            )
            yield event

    def close(self) -> None:
        self.inner.close()


class DummyAPI:
    def __init__(self) -> None:
        self.cancel_count = 0

    def cancel_current(self) -> None:
        self.cancel_count += 1

    def close(self) -> None:
        return None


class DummySound:
    def close(self) -> None:
        return None


def locate_reference(
    recording: np.ndarray,
    reference: np.ndarray,
    expected_index: int,
    radius: int = 16_000,
) -> tuple[int | None, float | None]:
    if not len(recording) or not len(reference) or len(recording) < len(reference):
        return None, None
    low = max(0, expected_index - radius)
    high = min(len(recording) - len(reference), expected_index + radius)
    if high < low:
        return None, None
    region = recording[low : high + len(reference)].astype(np.float64)
    ref = reference.astype(np.float64)
    region -= float(np.mean(region))
    ref -= float(np.mean(ref))
    values = correlate(region, ref, mode="valid", method="fft")
    relative = int(np.argmax(np.abs(values)))
    index = low + relative
    window = region[relative : relative + len(ref)]
    denominator = math.sqrt(
        max(float(np.dot(ref, ref)), 1e-30)
        * max(float(np.dot(window, window)), 1e-30)
    )
    return index, float(abs(values[relative]) / denominator)


def time_slice(audio: np.ndarray, zero: float | None, start: float, end: float) -> np.ndarray:
    if zero is None or end <= start:
        return np.empty(0, dtype=audio.dtype)
    begin = max(0, round((start - zero) * CAPTURE_RATE))
    finish = min(len(audio), round((end - zero) * CAPTURE_RATE))
    return audio[begin:finish] if finish > begin else np.empty(0, dtype=audio.dtype)


def frame_erle(raw: np.ndarray, clean: np.ndarray, ambient_rms: float) -> list[float]:
    frame_count = min(len(raw), len(clean)) // AEC_FRAME_SAMPLES
    values = []
    threshold = max(10 ** (-45 / 20), ambient_rms * 10 ** (15 / 20))
    for index in range(frame_count):
        begin = index * AEC_FRAME_SAMPLES
        raw_frame = raw[begin : begin + AEC_FRAME_SAMPLES].astype(np.float64) / 32768
        clean_frame = clean[begin : begin + AEC_FRAME_SAMPLES].astype(np.float64) / 32768
        raw_rms = float(np.sqrt(np.mean(raw_frame * raw_frame)))
        if raw_rms < threshold or np.max(np.abs(raw_frame)) >= 0.995:
            continue
        raw_power = float(np.mean(raw_frame * raw_frame))
        clean_power = float(np.mean(clean_frame * clean_frame))
        values.append(10 * math.log10(max(raw_power, 1e-20) / max(clean_power, 1e-20)))
    return values


def trial_plan(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    far = [row for row in manifest["records"] if row["role"] == "far"]
    near = [row for row in manifest["records"] if row["role"] == "near"]
    rng = random.Random(ARGS.seed)
    plans = []
    if ARGS.mode == "interruption":
        ratios = [-6.0, 0.0, 6.0]
        offsets = [1.0, 2.5, 4.0]
        for index in range(ARGS.trials):
            plans.append(
                {
                    "trial": index,
                    "far": far[index % len(far)],
                    "near": near[index % len(near)],
                    "ratio_db": ratios[index % len(ratios)],
                    "offset_s": offsets[(index // len(ratios)) % len(offsets)],
                }
            )
        rng.shuffle(plans)
    else:
        for index in range(ARGS.trials):
            plans.append(
                {
                    "trial": index,
                    "far": far[index % len(far)],
                    "near": None,
                    "ratio_db": None,
                    "offset_s": None,
                }
            )
        rng.shuffle(plans)
    return plans


def run_trial(
    plan: dict[str, Any],
    corpus_root: Path,
    model: Model,
    assistant: VoiceAssistant,
    tts: PlaybackTTS,
) -> dict[str, Any]:
    trial_id = f"{ARGS.mode}_{plan['trial']:04d}"
    trial_dir = ARGS.out / "trials" / trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)
    far_rate, far_source = read_wav(corpus_root / plan["far"]["path"])
    if far_rate != RENDER_RATE:
        far_source = resample_int16(far_source, far_rate, RENDER_RATE)
    far_pcm, far_gain = scale_to_rms(far_source, ARGS.far_active_rms)
    near_pcm = None
    near_gain = None
    near_ref16 = None
    if plan["near"] is not None:
        near_rate, near_source = read_wav(corpus_root / plan["near"]["path"])
        if near_rate != RENDER_RATE:
            near_source = resample_int16(near_source, near_rate, RENDER_RATE)
        near_target = ARGS.far_active_rms * 10 ** (float(plan["ratio_db"]) / 20)
        near_pcm, near_gain = scale_to_rms(near_source, near_target)
        near_ref16 = resample_int16(near_pcm, RENDER_RATE, CAPTURE_RATE)

    near_duration = len(near_pcm) / RENDER_RATE if near_pcm is not None else 0
    duration = (
        max(8.0, float(plan["offset_s"]) + near_duration + 2.0)
        if near_pcm is not None
        else max(3.0, len(far_pcm) / RENDER_RATE)
    )
    response_future: concurrent.futures.Future = concurrent.futures.Future()
    input_ready = threading.Event()
    far = FarPlayback(
        far_pcm,
        plan["far"]["text"],
        duration,
        ARGS.far_sink,
        input_ready,
        response_future,
        tts,
    )
    tts.configure(far)
    raw_recorder = ContinuousRecorder(
        ARGS.raw_source,
        ARGS.raw_source_channel_map or None,
        "helios-audit-raw-observer",
    )
    monitor_recorder = (
        ContinuousRecorder(ARGS.monitor_source, "mono", "helios-audit-monitor")
        if ARGS.monitor_source
        else None
    )
    capture = CaptureStream(
        ARGS.processing_source,
        ARGS.processing_source_channel_map or None,
        far,
        ARGS.path,
    )
    kaldi_records: list[dict[str, Any]] = []
    recognition_records: list[dict[str, Any]] = []
    recognizer = SpeechRecognizer(
        ARGS.repo / "recognizer/models/vosk-model-small-it-0.22",
        model=model,
        audio_interface=AudioInterface(capture),
        recognizer_factory=lambda loaded_model, rate: LoggedKaldi(
            loaded_model, rate, kaldi_records
        ),
        audio_format=8,
        rate=CAPTURE_RATE,
        chunk=VOSK_FRAME_SAMPLES,
        owns_audio=False,
    )
    logged_recognizer = LoggedRecognizer(recognizer, recognition_records)
    assistant.speech_recognizer = logged_recognizer
    near = (
        NearPlayback(near_pcm, ARGS.near_sink, far, float(plan["offset_s"]))
        if near_pcm is not None
        else None
    )
    started = time.monotonic()
    error = None
    follow_up = None
    try:
        raw_recorder.start()
        if monitor_recorder:
            monitor_recorder.start()
        far.start()
        if near:
            near.start()
        follow_up = assistant._listen_for_barge_in(response_future)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        LOGGER.exception("trial failed id=%s", trial_id)
        far.stop_event.set()
        if not response_future.done():
            response_future.set_result(None)
    finally:
        far.stop_event.set() if error else None
        far.join()
        if near:
            near.join()
        time.sleep(0.5)
        capture.close()
        raw_recorder.close()
        if monitor_recorder:
            monitor_recorder.close()
    ended = time.monotonic()

    raw_audio = raw_recorder.audio
    processing_input = np.frombuffer(b"".join(capture.raw_blocks), dtype="<i2").copy()
    clean_audio = np.frombuffer(b"".join(capture.clean_blocks), dtype="<i2").copy()
    monitor_audio = monitor_recorder.audio if monitor_recorder else np.empty(0, dtype="<i2")
    far_reference = far.actual_reference16()
    write_wav(trial_dir / "raw_mic.wav", CAPTURE_RATE, raw_audio)
    write_wav(trial_dir / "processing_input.wav", CAPTURE_RATE, processing_input)
    write_wav(trial_dir / "processed.wav", CAPTURE_RATE, clean_audio)
    write_wav(trial_dir / "far_reference_16k.wav", CAPTURE_RATE, far_reference)
    if len(monitor_audio):
        write_wav(trial_dir / "sink_monitor.wav", CAPTURE_RATE, monitor_audio)
    if near_ref16 is not None:
        write_wav(trial_dir / "near_reference_16k.wav", CAPTURE_RATE, near_ref16)
    with (trial_dir / "frames.ndjson").open("w", encoding="utf-8") as handle:
        for row in capture.frame_records:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    (trial_dir / "kaldi.ndjson").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kaldi_records),
        encoding="utf-8",
    )
    (trial_dir / "recognition.ndjson").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in recognition_records
        ),
        encoding="utf-8",
    )

    onset_index = None
    onset_correlation = None
    onset_time = None
    if near is not None and near.origin is not None and near_ref16 is not None:
        raw_zero = raw_recorder.sample_zero_time
        if raw_zero is not None:
            expected = round(
                (near.origin + ARGS.stream_delay_ms / 1000 - raw_zero) * CAPTURE_RATE
            )
            onset_index, onset_correlation = locate_reference(raw_audio, near_ref16, expected)
            if onset_index is not None:
                onset_time = raw_zero + onset_index / CAPTURE_RATE

    interrupt_times = [
        row["monotonic"] for row in tts.events if row["event"] == "interrupt_requested"
    ]
    duck_times = [row["monotonic"] for row in tts.events if row["event"] == "duck_requested"]
    interrupt_time = interrupt_times[0] if interrupt_times else None
    last_nonzero_end = None
    nonzero_blocks = [row for row in far.blocks if row["nonzero"]]
    if nonzero_blocks:
        last_nonzero_end = nonzero_blocks[-1]["target"] + 0.1
    onset_to_interrupt = (
        (interrupt_time - onset_time) * 1000
        if interrupt_time is not None and onset_time is not None
        else None
    )
    onset_to_stop_proxy = (
        (last_nonzero_end + ARGS.stream_delay_ms / 1000 - onset_time) * 1000
        if last_nonzero_end is not None and onset_time is not None
        else None
    )

    far_start = (far.origin or started) + 0.5
    far_end = min(
        value
        for value in (
            near.origin if near and near.origin else float("inf"),
            last_nonzero_end if last_nonzero_end else float("inf"),
            ended,
        )
    )
    raw_far = time_slice(raw_audio, raw_recorder.sample_zero_time, far_start, far_end)
    clean_far = time_slice(clean_audio, capture.sample_zero_time, far_start, far_end)
    ambient = time_slice(
        raw_audio,
        raw_recorder.sample_zero_time,
        started,
        min(far.origin or started, started + 0.75),
    )
    ambient_rms = (
        float(np.sqrt(np.mean((ambient.astype(np.float64) / 32768) ** 2)))
        if len(ambient)
        else 0.0
    )
    erle_values = frame_erle(raw_far, clean_far, ambient_rms)

    projection_loss_db = None
    if onset_time is not None and near_ref16 is not None:
        raw_near = time_slice(
            raw_audio,
            raw_recorder.sample_zero_time,
            onset_time,
            onset_time + len(near_ref16) / CAPTURE_RATE,
        )
        clean_near = time_slice(
            clean_audio,
            capture.sample_zero_time,
            onset_time,
            onset_time + len(near_ref16) / CAPTURE_RATE,
        )
        length = min(len(raw_near), len(clean_near), len(near_ref16))
        if length:
            reference = near_ref16[:length].astype(np.float64)
            denominator = max(float(np.dot(reference, reference)), 1e-30)
            raw_coefficient = float(np.dot(raw_near[:length], reference) / denominator)
            clean_coefficient = float(np.dot(clean_near[:length], reference) / denominator)
            projection_loss_db = 20 * math.log10(
                max(abs(clean_coefficient), 1e-12) / max(abs(raw_coefficient), 1e-12)
            )

    timing_ms = [row["processing_ns"] / 1e6 for row in capture.frame_records]
    all_stderr = "\n".join(
        value
        for value in (
            raw_recorder.stderr,
            monitor_recorder.stderr if monitor_recorder else "",
            capture.stderr,
            far.stderr,
            near.stderr if near else "",
        )
        if value
    )
    xrun_matches = re.findall(
        r"overflow|underflow|overrun|underrun|xrun|broken pipe",
        all_stderr,
        flags=re.IGNORECASE,
    )
    record = {
        "trial_id": trial_id,
        "path": ARGS.path,
        "mode": ARGS.mode,
        "error": error,
        "valid": error is None and not xrun_matches,
        "far": plan["far"],
        "near": plan["near"],
        "ratio_db": plan["ratio_db"],
        "offset_s": plan["offset_s"],
        "far_gain": far_gain,
        "near_gain": near_gain,
        "far_active_rms": active_rms(far_pcm),
        "near_active_rms": active_rms(near_pcm) if near_pcm is not None else None,
        "started": started,
        "ended": ended,
        "duration_s": ended - started,
        "follow_up": follow_up,
        "detected": bool(follow_up),
        "raw_sample_zero_time": raw_recorder.sample_zero_time,
        "processing_sample_zero_time": capture.sample_zero_time,
        "near_playback_origin": near.origin if near else None,
        "near_onset_index_raw": onset_index,
        "near_onset_correlation": onset_correlation,
        "near_onset_time": onset_time,
        "duck_time": duck_times[0] if duck_times else None,
        "interrupt_time": interrupt_time,
        "far_last_nonzero_end": last_nonzero_end,
        "onset_to_interrupt_ms": onset_to_interrupt,
        "onset_to_far_stop_proxy_ms": onset_to_stop_proxy,
        "stop_proxy_boundary": (
            "last scheduled nonzero 100 ms far block plus measured app-to-mic delay; "
            "timing uncertainty at least one 20 ms capture block"
        ),
        "erle_frame_count": len(erle_values),
        "erle_median_db": percentile(erle_values, 0.50),
        "erle_p10_db": percentile(erle_values, 0.10),
        "projection_loss_db_vs_raw_mix": projection_loss_db,
        "processing_frame_count": len(timing_ms),
        "processing_p50_ms": percentile(timing_ms, 0.50),
        "processing_p99_ms": percentile(timing_ms, 0.99),
        "processing_max_ms": max(timing_ms) if timing_ms else None,
        "processing_deadline_misses": sum(value >= 20 for value in timing_ms),
        "raw_blocks": len(raw_recorder.blocks),
        "processed_blocks": len(capture.clean_blocks),
        "monitor_blocks": len(monitor_recorder.blocks) if monitor_recorder else 0,
        "xrun_matches": xrun_matches,
        "stderr": all_stderr,
        "tts_events": tts.events,
        "far_blocks": far.blocks,
        "kaldi_event_count": len(kaldi_records),
        "recognition_event_count": len(recognition_records),
    }
    (trial_dir / "result.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def aggregate(results: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    valid = [row for row in results if row["valid"]]
    detected = [row for row in valid if row["detected"]]
    latency_interrupt = [
        row["onset_to_interrupt_ms"]
        for row in detected
        if row["onset_to_interrupt_ms"] is not None
    ]
    latency_stop = [
        row["onset_to_far_stop_proxy_ms"]
        for row in detected
        if row["onset_to_far_stop_proxy_ms"] is not None
    ]
    erle_medians = [
        row["erle_median_db"] for row in valid if row["erle_median_db"] is not None
    ]
    erle_p10s = [row["erle_p10_db"] for row in valid if row["erle_p10_db"] is not None]
    processing = []
    for row in valid:
        trial_path = ARGS.out / "trials" / row["trial_id"] / "frames.ndjson"
        for line in trial_path.read_text().splitlines():
            processing.append(json.loads(line)["processing_ns"] / 1e6)
    return {
        "metadata": metadata,
        "requested_trials": ARGS.trials,
        "completed_trials": len(results),
        "valid_trials": len(valid),
        "invalid_trials": len(results) - len(valid),
        "detections": len(detected),
        "recall_or_false_trigger_fraction": len(detected) / len(valid) if valid else None,
        "latency_interrupt_p50_ms": percentile(latency_interrupt, 0.50),
        "latency_interrupt_p95_ms": percentile(latency_interrupt, 0.95),
        "latency_interrupt_p99_ms": percentile(latency_interrupt, 0.99),
        "latency_interrupt_max_ms": max(latency_interrupt) if latency_interrupt else None,
        "latency_stop_proxy_p50_ms": percentile(latency_stop, 0.50),
        "latency_stop_proxy_p95_ms": percentile(latency_stop, 0.95),
        "latency_stop_proxy_p99_ms": percentile(latency_stop, 0.99),
        "latency_stop_proxy_max_ms": max(latency_stop) if latency_stop else None,
        "erle_trial_median_of_medians_db": percentile(erle_medians, 0.50),
        "erle_trial_p10_db": percentile(erle_p10s, 0.10),
        "processing_p50_ms": percentile(processing, 0.50),
        "processing_p99_ms": percentile(processing, 0.99),
        "processing_max_ms": max(processing) if processing else None,
        "processing_deadline_misses": sum(value >= 20 for value in processing),
        "total_xrun_matches": sum(len(row["xrun_matches"]) for row in results),
        "proxy_limitation": (
            "Both scripted far and reference-excluded interruption signals use the same "
            "physical USB speaker enclosure. Recall and preservation are regression proxies, "
            "not valid human near-end/double-talk acceptance measurements."
        ),
    }


def main() -> None:
    head = subprocess.check_output(
        ["git", "-C", str(ARGS.repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != EXPECTED_COMMIT:
        raise SystemExit(f"refusing wrong commit: {head}")
    manifest = json.loads(ARGS.manifest.read_text())
    corpus_root = ARGS.manifest.parent
    model_path = ARGS.repo / "recognizer/models/vosk-model-small-it-0.22"
    metadata = {
        "schema": 1,
        "path": ARGS.path,
        "mode": ARGS.mode,
        "commit": head,
        "branch": subprocess.check_output(
            ["git", "-C", str(ARGS.repo), "symbolic-ref", "--short", "HEAD"], text=True
        ).strip(),
        "manifest_sha256": sha256(ARGS.manifest),
        "vosk_model": str(model_path),
        "raw_source": ARGS.raw_source,
        "processing_source": ARGS.processing_source,
        "far_sink": ARGS.far_sink,
        "near_sink": ARGS.near_sink,
        "monitor_source": ARGS.monitor_source,
        "stream_delay_ms": ARGS.stream_delay_ms,
        "pulse_latency_ms": ARGS.pulse_latency_ms,
        "capture_rate": CAPTURE_RATE,
        "render_rate": RENDER_RATE,
        "aec_frame_samples": AEC_FRAME_SAMPLES,
        "vosk_frame_samples": VOSK_FRAME_SAMPLES,
        "far_active_rms": ARGS.far_active_rms,
        "seed": ARGS.seed,
        "started_at": subprocess.check_output(["date", "--iso-8601=seconds"], text=True).strip(),
        "nvpmodel": subprocess.run(
            ["nvpmodel", "-q"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ).stdout,
        "jetson_clocks": subprocess.run(
            ["jetson_clocks", "--show"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ).stdout,
        "pulse_info": subprocess.check_output(["pactl", "info"], text=True),
        "pulse_modules": subprocess.check_output(["pactl", "list", "short", "modules"], text=True),
    }
    (ARGS.out / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    plans = trial_plan(manifest)
    (ARGS.out / "trial_plan.json").write_text(
        json.dumps(plans, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    LOGGER.info("loading Vosk model")
    model = Model(str(model_path))
    tts = PlaybackTTS()
    dummy_api = DummyAPI()
    settings = config.Settings(project_root=ARGS.repo, language="it", log_file_name=None)
    detector = BargeInDetector(
        recognition_event_energy=settings.barge_in_event_energy,
        suppression_policy=ConservativeEchoSuppressionPolicy(
            expected_echo_energy=settings.barge_in_expected_echo_energy,
            minimum_interrupt_energy=settings.barge_in_minimum_interrupt_energy,
        ),
    )
    assistant = VoiceAssistant(
        settings=settings,
        tts=tts,
        sound_player=DummySound(),
        api_client=dummy_api,
        speech_recognizer=None,
        barge_in_detector=detector,
    )
    results = []
    for ordinal, plan in enumerate(plans, 1):
        LOGGER.info("trial_start ordinal=%s total=%s id=%s", ordinal, len(plans), plan["trial"])
        row = run_trial(plan, corpus_root, model, assistant, tts)
        results.append(row)
        with (ARGS.out / "results.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(
            json.dumps(
                {
                    "ordinal": ordinal,
                    "trial": row["trial_id"],
                    "valid": row["valid"],
                    "detected": row["detected"],
                    "latency_ms": row["onset_to_far_stop_proxy_ms"],
                    "erle_median_db": row["erle_median_db"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    summary = aggregate(results, metadata)
    summary["ended_at"] = subprocess.check_output(["date", "--iso-8601=seconds"], text=True).strip()
    (ARGS.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assistant.close()
    print(json.dumps({"summary": str(ARGS.out / "summary.json")}, sort_keys=True))


if __name__ == "__main__":
    main()
