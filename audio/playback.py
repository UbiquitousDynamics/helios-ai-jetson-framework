"""Compatibility exports for the consolidated TTS implementation."""

from audio.tts import (
    AudioPlaybackError,
    AudioSynthesisError,
    PiperTTS,
    Pyttsx3TTS,
    SoundDeviceBackend,
    TTSError,
)

__all__ = [
    "AudioPlaybackError",
    "AudioSynthesisError",
    "PiperTTS",
    "Pyttsx3TTS",
    "SoundDeviceBackend",
    "TTSError",
]
