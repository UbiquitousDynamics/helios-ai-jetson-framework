#!/usr/bin/env python3
"""Pinned on-device latency benchmarks for Helios b86781c (NDJSON output)."""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from array import array
from contextlib import nullcontext
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable


EXPECTED_COMMIT = "b86781c7c424ce3a9972198b3a8f0470025461f1"
SAMPLE_RATE = 16_000
CAPTURE_SAMPLES = 1_600
AEC_SAMPLES = 320
CAPTURE_BUDGET_MS = 100.0
AEC_BUDGET_MS = 20.0
RAG_BUDGET_MS = 100.0
TAPS = 128


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--cpu", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--blas-threads", type=int, default=1)
    parser.add_argument("--stream-delay-ms", type=int, default=40)
    return parser.parse_args()


ARGS = arguments()
if ARGS.repeats < 3 or ARGS.scale <= 0 or ARGS.blas_threads < 1:
    raise SystemExit("invalid repeats, scale, or BLAS thread count")
for variable in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[variable] = str(ARGS.blas_threads)
os.sched_setaffinity(0, {ARGS.cpu})

import numpy as np  # noqa: E402

try:
    from threadpoolctl import threadpool_info, threadpool_limits
except ImportError:
    threadpool_info = None
    threadpool_limits = None


def emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True, ensure_ascii=False), flush=True)


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8,
            check=False,
        )
        return result.stdout.strip()
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").replace("\0", "").strip()
    except OSError:
        return None


def thermal_snapshot() -> list[dict[str, Any]]:
    result = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw = read_text(zone / "temp")
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if abs(value) > 1000:
            value /= 1000
        result.append({"zone": zone.name, "type": read_text(zone / "type"), "celsius": value})
    return result


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def scaled(base: int) -> int:
    return max(1, round(base * ARGS.scale))


@dataclass
class Case:
    name: str
    operation: Callable[[], Any]
    iterations: int
    warmups: int
    budget_ms: float | None
    notes: str


def run_case(case: Case) -> None:
    try:
        for _ in range(case.warmups):
            case.operation()
        pooled: list[float] = []
        repeats: list[dict[str, float]] = []
        elapsed_start = time.perf_counter()
        for _ in range(ARGS.repeats):
            gc.collect()
            samples: list[float] = []
            last_result: Any = None
            for _ in range(case.iterations):
                started = time.perf_counter_ns()
                last_result = case.operation()
                samples.append((time.perf_counter_ns() - started) / 1_000_000)
            if last_result is NotImplemented:
                raise AssertionError("unreachable")
            pooled.extend(samples)
            repeats.append(
                {
                    "p50_ms": percentile(samples, 0.50),
                    "p99_ms": percentile(samples, 0.99),
                    "max_ms": max(samples),
                }
            )
        record: dict[str, Any] = {
            "type": "benchmark",
            "case": case.name,
            "status": "ok",
            "iterations_per_repeat": case.iterations,
            "repeats": ARGS.repeats,
            "total_samples": len(pooled),
            "p50_ms": percentile(pooled, 0.50),
            "p99_ms": percentile(pooled, 0.99),
            "max_ms": max(pooled),
            "mean_ms": statistics.fmean(pooled),
            "repeat_p50_ms": [row["p50_ms"] for row in repeats],
            "repeat_p99_ms": [row["p99_ms"] for row in repeats],
            "repeat_max_ms": [row["max_ms"] for row in repeats],
            "elapsed_s": time.perf_counter() - elapsed_start,
            "budget_ms": case.budget_ms,
            "notes": case.notes,
            "thermal_after": thermal_snapshot(),
        }
        if case.budget_ms is not None:
            for metric in ("p50_ms", "p99_ms", "max_ms"):
                record[metric.replace("_ms", "_budget_pct")] = (
                    100.0 * record[metric] / case.budget_ms
                )
            record["deadline_misses"] = sum(value >= case.budget_ms for value in pooled)
        emit(record)
    except Exception as exc:
        emit(
            {
                "type": "benchmark",
                "case": case.name,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def pcm16_rms(frame: bytes) -> float:
    if len(frame) % 2:
        raise ValueError("PCM frame must contain complete 16-bit samples")
    if not frame:
        return 0.0
    samples = array("h")
    samples.frombytes(frame)
    if sys.byteorder != "little":
        samples.byteswap()
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return math.sqrt(mean_square) / 32768.0


class RecognitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedRecognition:
    text: str
    confidence: float | None
    speech_duration_seconds: float | None
    word_confidences: tuple[float | None, ...]
    word_timings: tuple[tuple[float, float] | None, ...]


def parse_recognition(payload: str, key: str) -> ParsedRecognition:
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RecognitionError("Recognizer returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RecognitionError("Recognizer returned invalid JSON")
    text = str(parsed.get(key) or "")
    detail_key = "partial_result" if key == "partial" else "result"
    raw_words = parsed.get(detail_key)
    if not isinstance(raw_words, list):
        return ParsedRecognition(text, None, None, (), ())
    confidences: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    word_confidences: list[float | None] = []
    word_timings: list[tuple[float, float] | None] = []
    for raw_word in raw_words:
        if not isinstance(raw_word, dict):
            continue
        confidence = raw_word.get("conf")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
            and 0 <= float(confidence) <= 1
        ):
            parsed_confidence: float | None = float(confidence)
            confidences.append(parsed_confidence)
        else:
            parsed_confidence = None
        word_confidences.append(parsed_confidence)
        start = raw_word.get("start")
        end = raw_word.get("end")
        if (
            isinstance(start, (int, float))
            and not isinstance(start, bool)
            and math.isfinite(float(start))
            and float(start) >= 0
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and math.isfinite(float(end))
            and float(end) >= float(start)
        ):
            timing: tuple[float, float] | None = (float(start), float(end))
            starts.append(timing[0])
            ends.append(timing[1])
        else:
            timing = None
        word_timings.append(timing)
    aggregate = sum(confidences) / len(confidences) if confidences else None
    duration = max(ends) - min(starts) if starts and ends else None
    return ParsedRecognition(
        text,
        aggregate,
        duration,
        tuple(word_confidences),
        tuple(word_timings),
    )


class ScalarFIR:
    def __init__(self, reference: list[float]) -> None:
        self.reference = reference
        self.weights = [(index + 1) * 1e-5 for index in range(TAPS)]
        self.history = [0.0] * TAPS
        self.position = 0

    def process(self) -> float:
        total = 0.0
        for sample in self.reference:
            self.history[self.position] = sample
            estimate = 0.0
            history_index = self.position
            for tap in range(TAPS):
                estimate += self.weights[tap] * self.history[history_index]
                history_index = history_index - 1 if history_index else TAPS - 1
            total += estimate
            self.position = self.position + 1 if self.position + 1 < TAPS else 0
        return total


class ScalarNLMS:
    def __init__(self, reference: list[float], microphone: list[float]) -> None:
        self.reference = reference
        self.microphone = microphone
        self.weights = [0.0] * TAPS
        self.history = [0.0] * TAPS
        self.position = 0

    def process(self) -> float:
        error_sum = 0.0
        for sample_index, sample in enumerate(self.reference):
            self.history[self.position] = sample
            estimate = 0.0
            power = 1e-8
            history_index = self.position
            for tap in range(TAPS):
                value = self.history[history_index]
                estimate += self.weights[tap] * value
                power += value * value
                history_index = history_index - 1 if history_index else TAPS - 1
            error = self.microphone[sample_index] - estimate
            step = 0.5 * error / power
            history_index = self.position
            for tap in range(TAPS):
                self.weights[tap] += step * self.history[history_index]
                history_index = history_index - 1 if history_index else TAPS - 1
            error_sum += error
            self.position = self.position + 1 if self.position + 1 < TAPS else 0
        return error_sum


class NumpyBlockNLMS:
    def __init__(self, reference: np.ndarray, microphone: np.ndarray) -> None:
        self.reference = reference
        self.microphone = microphone
        self.weights = np.zeros(TAPS, dtype=np.float32)
        self.history = np.zeros(TAPS - 1, dtype=np.float32)

    def process(self) -> float:
        padded = np.concatenate((self.history, self.reference))
        windows = np.lib.stride_tricks.sliding_window_view(padded, TAPS)[:, ::-1]
        estimate = windows @ self.weights
        error = self.microphone - estimate
        powers = np.einsum("ij,ij->i", windows, windows) + np.float32(1e-8)
        self.weights += np.float32(0.5 / AEC_SAMPLES) * (windows.T @ (error / powers))
        self.history = padded[-(TAPS - 1) :].copy()
        return float(error[0])


def corpus_snapshot(repo: Path) -> dict[str, Any]:
    boundary = re.compile(r"(?<=[.!?])\s+")
    rows = []
    total = 0
    for path in sorted((repo / "uploads").glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        chunks = [part.strip() for part in boundary.split(text) if part.strip()]
        total += len(chunks)
        rows.append({"name": path.name, "bytes": path.stat().st_size, "chunks": len(chunks)})
    return {"files": rows, "total_chunks": total}


def rag_case(repo: Path) -> Case:
    with np.load(repo / "embeddings.npz", allow_pickle=False) as archive:
        matrix = np.ascontiguousarray(np.asarray(archive["embeddings"]))
    if matrix.shape != (1115, 384) or matrix.dtype != np.float32:
        raise ValueError(f"unexpected real index {matrix.shape} {matrix.dtype}")
    query = np.ascontiguousarray(matrix[0].copy())
    emit(
        {
            "type": "rag_matrix",
            "shape": list(matrix.shape),
            "dtype": str(matrix.dtype),
            "contiguous": bool(matrix.flags.c_contiguous),
            "query": "copy of real row 0; query encoding excluded",
        }
    )

    def rank_top20() -> list[tuple[int, float]]:
        similarities = np.dot(matrix, query)
        requested = 20
        cutoff = np.partition(similarities, len(similarities) - requested)[
            len(similarities) - requested
        ]
        better = np.flatnonzero(similarities > cutoff)
        boundary = np.flatnonzero(similarities == cutoff)
        candidates = np.concatenate((better, boundary[: requested - len(better)]))
        order = np.lexsort((candidates, -similarities[candidates]))
        ranked = candidates[order]
        return [(int(index), float(similarities[index])) for index in ranked]

    return Case(
        "rag_real_1115_dot_plus_top20",
        rank_top20,
        scaled(500),
        30,
        RAG_BUDGET_MS,
        "Real deployed index; excludes query encoding and index loading.",
    )


def make_webrtc_audio(samples: int) -> tuple[np.ndarray, np.ndarray]:
    timeline = np.arange(samples, dtype=np.float64) / SAMPLE_RATE
    far = (np.sin(2 * np.pi * 500 * timeline) * 8000).astype(np.int16)
    near = np.clip(
        np.roll(far, 80).astype(np.float64) * 0.35 + np.sin(2 * np.pi * 700 * timeline) * 1000,
        -32768,
        32767,
    ).astype(np.int16)
    return np.ascontiguousarray(near), np.ascontiguousarray(far)


def add_webrtc_cases(cases: list[Case]) -> None:
    try:
        from pywebrtc_audio import AudioProcessor, VoiceDetector

        emit(
            {
                "type": "dependency",
                "name": "pywebrtc-audio",
                "version": metadata.version("pywebrtc-audio"),
            }
        )
        near20, far20 = make_webrtc_audio(AEC_SAMPLES)
        near100, far100 = make_webrtc_audio(CAPTURE_SAMPLES)
        processor20 = AudioProcessor(
            sample_rate=SAMPLE_RATE,
            echo_cancellation=True,
            noise_suppression=True,
            auto_gain_control=True,
            stream_delay_ms=ARGS.stream_delay_ms,
        )
        processor100 = AudioProcessor(
            sample_rate=SAMPLE_RATE,
            echo_cancellation=True,
            noise_suppression=True,
            auto_gain_control=True,
            stream_delay_ms=ARGS.stream_delay_ms,
        )
        detector = VoiceDetector(sample_rate=SAMPLE_RATE)
        cases.extend(
            [
                Case(
                    "pywebrtc_audio_aec_ns_agc_20ms",
                    lambda: processor20.process(near20, far20),
                    scaled(1000),
                    50,
                    AEC_BUDGET_MS,
                    "State retained; int16 mono; native 10 ms subframes.",
                ),
                Case(
                    "pywebrtc_audio_aec_ns_agc_100ms",
                    lambda: processor100.process(near100, far100),
                    scaled(500),
                    20,
                    CAPTURE_BUDGET_MS,
                    "Binding internally processes 10 ms subframes.",
                ),
                Case(
                    "pywebrtc_audio_vad_20ms",
                    lambda: detector.process(near20),
                    scaled(2000),
                    50,
                    AEC_BUDGET_MS,
                    "Native speech-probability path.",
                ),
            ]
        )
    except Exception as exc:
        emit(
            {
                "type": "dependency",
                "name": "pywebrtc-audio",
                "status": "skipped",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def main() -> None:
    repo = ARGS.repo.resolve()
    head = command_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    emit(
        {
            "type": "environment",
            "expected_commit": EXPECTED_COMMIT,
            "actual_commit": head,
            "commit_matches": head == EXPECTED_COMMIT,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "affinity": sorted(os.sched_getaffinity(0)),
            "blas_threads_requested": ARGS.blas_threads,
            "threadpools": threadpool_info() if threadpool_info else None,
            "device_model": read_text(Path("/proc/device-tree/model")),
            "nv_tegra_release": read_text(Path("/etc/nv_tegra_release")),
            "nvpmodel": command_output(["nvpmodel", "-q"]),
            "jetson_clocks": command_output(["jetson_clocks", "--show"]),
            "thermal_start": thermal_snapshot(),
            "gc_enabled": gc.isenabled(),
        }
    )
    corpus = corpus_snapshot(repo)
    emit(
        {
            "type": "corpus",
            **corpus,
            "expected_chunks": 1115,
            "matches_expected": corpus["total_chunks"] == 1115,
        }
    )
    frame = array(
        "h", (((index * 7919) % 65536) - 32768 for index in range(CAPTURE_SAMPLES))
    ).tobytes()
    partial_payload = json.dumps({"partial": "please stop speaking now"}, separators=(",", ":"))
    final_payload = json.dumps(
        {
            "text": "echo echo user",
            "result": [
                {"word": "echo", "conf": 0.95, "start": 0.0, "end": 0.2},
                {"word": "echo", "conf": 0.95, "start": 0.2, "end": 0.4},
                {"word": "user", "conf": 0.1, "start": 0.4, "end": 0.6},
            ],
        },
        separators=(",", ":"),
    )
    rng = np.random.default_rng(20260903)
    reference = rng.standard_normal(AEC_SAMPLES).astype(np.float32) * np.float32(0.08)
    microphone = np.roll(reference, 8) * np.float32(0.4) + rng.standard_normal(AEC_SAMPLES).astype(
        np.float32
    ) * np.float32(0.005)
    scalar_fir = ScalarFIR([float(value) for value in reference])
    scalar_nlms = ScalarNLMS(
        [float(value) for value in reference], [float(value) for value in microphone]
    )
    block_nlms = NumpyBlockNLMS(reference, microphone)
    emit(
        {
            "type": "inputs",
            "capture_samples": CAPTURE_SAMPLES,
            "capture_bytes": len(frame),
            "partial_payload_bytes": len(partial_payload.encode()),
            "final_payload_bytes": len(final_payload.encode()),
            "aec_samples": AEC_SAMPLES,
            "taps": TAPS,
        }
    )
    cases = [
        Case("timer_envelope", lambda: None, scaled(20_000), 1000, None, "Timer/call envelope."),
        Case(
            "pcm16_rms_current_1600",
            lambda: pcm16_rms(frame),
            scaled(2000),
            200,
            CAPTURE_BUDGET_MS,
            "Exact deployed pure-Python RMS.",
        ),
        Case(
            "json_loads_partial",
            lambda: json.loads(partial_payload),
            scaled(10_000),
            1000,
            CAPTURE_BUDGET_MS,
            "38-byte Vosk partial JSON decoding.",
        ),
        Case(
            "parse_recognition_final_three_words",
            lambda: parse_recognition(final_payload, "text"),
            scaled(2000),
            200,
            CAPTURE_BUDGET_MS,
            "Exact deployed confidence/timing path.",
        ),
        rag_case(repo),
        Case(
            "scalar_fir_128tap_20ms",
            scalar_fir.process,
            scaled(100),
            3,
            AEC_BUDGET_MS,
            "40,960 tap visits; FIR only.",
        ),
        Case(
            "scalar_streaming_nlms_128tap_20ms",
            scalar_nlms.process,
            scaled(100),
            3,
            AEC_BUDGET_MS,
            "81,920 visits including weight update.",
        ),
        Case(
            "numpy_block_nlms_128tap_20ms",
            block_nlms.process,
            scaled(500),
            20,
            AEC_BUDGET_MS,
            "One accumulated update per block; throughput prototype.",
        ),
    ]
    add_webrtc_cases(cases)
    context = (
        threadpool_limits(limits=ARGS.blas_threads, user_api="blas")
        if threadpool_limits
        else nullcontext()
    )
    with context:
        emit(
            {
                "type": "threadpools_effective",
                "value": threadpool_info() if threadpool_info else None,
            }
        )
        for case in cases:
            run_case(case)
    emit({"type": "finished", "thermal_end": thermal_snapshot()})


if __name__ == "__main__":
    main()
