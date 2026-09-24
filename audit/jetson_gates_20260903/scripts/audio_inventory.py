#!/usr/bin/env python3
"""Emit PortAudio device inventory without opening or changing a device."""

from __future__ import annotations

import json
import platform

import pyaudio
import sounddevice as sd


def clean(value):
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


result = {
    "platform": platform.platform(),
    "sounddevice_default": clean(sd.default.device),
    "sounddevice_devices": [clean(dict(row)) for row in sd.query_devices()],
    "sounddevice_hostapis": [clean(dict(row)) for row in sd.query_hostapis()],
}

audio = pyaudio.PyAudio()
try:
    result["pyaudio_version"] = pyaudio.get_portaudio_version_text()
    result["pyaudio_hostapis"] = [
        clean(audio.get_host_api_info_by_index(index))
        for index in range(audio.get_host_api_count())
    ]
    result["pyaudio_devices"] = [
        clean(audio.get_device_info_by_index(index)) for index in range(audio.get_device_count())
    ]
    for direction, getter in (
        ("input", audio.get_default_input_device_info),
        ("output", audio.get_default_output_device_info),
    ):
        try:
            result[f"pyaudio_default_{direction}"] = clean(getter())
        except Exception as exc:
            result[f"pyaudio_default_{direction}"] = {"error": f"{type(exc).__name__}: {exc}"}
finally:
    audio.terminate()

print(json.dumps(result, indent=2, sort_keys=True))
