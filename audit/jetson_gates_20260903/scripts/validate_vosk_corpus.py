#!/usr/bin/env python3
"""Validate the deterministic near-end corpus against deployed Italian Vosk."""

from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly
from vosk import KaldiRecognizer, Model, SetLogLevel


parser = argparse.ArgumentParser()
parser.add_argument("--model", type=Path, required=True)
parser.add_argument("--manifest", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()

SetLogLevel(-1)
model = Model(str(args.model))
manifest = json.loads(args.manifest.read_text())
root = args.manifest.parent
results = []
for record in manifest["records"]:
    if record["role"] != "near":
        continue
    path = root / record["path"]
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    data = data.reshape(-1, channels).mean(axis=1)
    divisor = math.gcd(rate, 16_000)
    audio = resample_poly(data, 16_000 // divisor, rate // divisor)
    pcm = np.clip(audio, -32768, 32767).astype("<i2").tobytes()
    recognizer = KaldiRecognizer(model, 16_000)
    recognizer.SetWords(True)
    for offset in range(0, len(pcm), 3_200):
        recognizer.AcceptWaveform(pcm[offset : offset + 3_200])
    parsed = json.loads(recognizer.FinalResult())
    results.append(
        {
            "path": record["path"],
            "talker": record["talker"],
            "reference": record["text"],
            "recognized": parsed.get("text", ""),
            "words": parsed.get("result", []),
        }
    )
output = {"count": len(results), "nonempty": sum(bool(row["recognized"]) for row in results), "results": results}
args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({"count": output["count"], "nonempty": output["nonempty"]}))
