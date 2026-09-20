#!/usr/bin/env python3
"""Generate deterministic Piper far-end and eSpeak near-end speech WAVs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly


FAR_TEXTS = (
    "Posso aiutarti a organizzare la giornata e ricordare i prossimi appuntamenti.",
    "La temperatura prevista per domani sarà mite, con qualche nuvola nel pomeriggio.",
    "Sto cercando le informazioni richieste nei documenti disponibili sul dispositivo.",
    "Per completare questa operazione controllerò prima i dati e poi ti darò una risposta.",
    "Il sistema funziona localmente e mantiene separati i contenuti della conversazione.",
    "Ecco un riepilogo dettagliato delle attività programmate per questa settimana.",
    "La riproduzione continua mentre il riconoscimento vocale ascolta una nuova richiesta.",
    "Puoi chiedermi di cambiare argomento, fermare la risposta oppure aggiungere un dettaglio.",
    "Ho trovato diversi risultati pertinenti e li sto ordinando per importanza.",
    "Prima di procedere verificherò che tutti i parametri siano corretti e aggiornati.",
    "Questa frase contiene parole diverse per esercitare il filtro durante la riproduzione.",
    "Continuerò a parlare per alcuni secondi così da misurare in modo stabile il segnale di eco.",
)

NEAR_TEXTS = (
    "fermati per favore",
    "aspetta un momento",
    "scusa voglio interrompere",
    "stop ho una domanda",
    "vorrei aggiungere una cosa",
    "puoi cambiare argomento",
    "non continuare adesso",
    "basta così grazie",
    "puoi ascoltarmi ora",
    "devo correggere la richiesta",
)

TALKERS = (
    ("espeak_it_slow", "it", 135, 42),
    ("espeak_it_fast", "it", 175, 66),
    ("mbrola_it4", "mb-it4", 150, 50),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise RuntimeError(f"unsupported sample width {width}: {path}")
    audio = np.frombuffer(frames, dtype="<i2").reshape(-1, channels)
    mono = np.mean(audio.astype(np.float64), axis=1)
    return rate, np.clip(mono, -32768, 32767).astype(np.int16)


def write_wav(path: Path, rate: int, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.asarray(audio, dtype="<i2").tobytes())


def standardize(path: Path, target_rate: int = 22_050) -> None:
    rate, audio = read_wav(path)
    floating = audio.astype(np.float64) / 32768.0
    if rate != target_rate:
        divisor = math.gcd(rate, target_rate)
        floating = resample_poly(floating, target_rate // divisor, rate // divisor)
    active = np.flatnonzero(np.abs(floating) >= 10 ** (-48 / 20))
    if len(active):
        margin = round(0.08 * target_rate)
        start = max(0, int(active[0]) - margin)
        end = min(len(floating), int(active[-1]) + margin + 1)
        floating = floating[start:end]
    peak = float(np.max(np.abs(floating))) if len(floating) else 0.0
    if peak > 0.92:
        floating *= 0.92 / peak
    write_wav(path, target_rate, np.clip(floating * 32767, -32768, 32767).astype(np.int16))


parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
far_dir = args.out / "far_piper_22050"
near_dir = args.out / "near_espeak_22050"
far_dir.mkdir(exist_ok=True)
near_dir.mkdir(exist_ok=True)

from piper.voice import PiperVoice

model = args.repo / "audio/models/it_IT-paola-medium.onnx"
voice = PiperVoice.load(str(model))
records = []
for index, text in enumerate(FAR_TEXTS):
    path = far_dir / f"far_{index:02d}.wav"
    with wave.open(str(path), "wb") as handle:
        synthesize_wav = getattr(voice, "synthesize_wav", None)
        if callable(synthesize_wav):
            synthesize_wav(text, handle)
        else:
            voice.synthesize(text, handle)
    standardize(path)
    rate, audio = read_wav(path)
    records.append(
        {
            "role": "far",
            "index": index,
            "text": text,
            "path": str(path.relative_to(args.out)),
            "sample_rate": rate,
            "samples": len(audio),
            "duration_s": len(audio) / rate,
            "sha256": sha256(path),
            "generator": "Piper it_IT-paola-medium deployment model",
        }
    )

for talker, voice_name, speed, pitch in TALKERS:
    for index, text in enumerate(NEAR_TEXTS):
        path = near_dir / f"near_{talker}_{index:02d}.wav"
        result = subprocess.run(
            [
                "/usr/bin/espeak",
                "-v",
                voice_name,
                "-s",
                str(speed),
                "-p",
                str(pitch),
                "-w",
                str(path),
                text,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"eSpeak failed for {voice_name}: {result.stdout}")
        standardize(path)
        rate, audio = read_wav(path)
        records.append(
            {
                "role": "near",
                "index": index,
                "talker": talker,
                "text": text,
                "path": str(path.relative_to(args.out)),
                "sample_rate": rate,
                "samples": len(audio),
                "duration_s": len(audio) / rate,
                "sha256": sha256(path),
                "generator": f"eSpeak voice={voice_name} speed={speed} pitch={pitch}",
            }
        )

manifest = {
    "schema": 1,
    "target_rate": 22_050,
    "far_count": len(FAR_TEXTS),
    "near_count": len(NEAR_TEXTS) * len(TALKERS),
    "synthetic_speech_limitation": (
        "Three deterministic synthetic voices/speaking styles are not a substitute for "
        "a diverse recorded human-speech corpus."
    ),
    "records": records,
}
manifest_path = args.out / "manifest.json"
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({"manifest": str(manifest_path), "records": len(records)}))
