import logging
import os
from piper.voice import PiperVoice
import sounddevice as sd
import numpy as np
import wave
import config

class Pyttsx3TTS:
    def __init__(self, voice_model=config.TTS_MODEL):
        # Carica il modello vocale specificato
        self.voice = PiperVoice.load(voice_model)
        logging.debug(f"PiperTTS inizializzato con il modello: {voice_model}")

    def speak(self, text: str):
        try:
            logging.debug(f"Pronuncia TTS per il testo: {text[:50]}...")
            with wave.open("output.wav", "wb") as wav_file:
                self.voice.synthesize(text, wav_file)
            self.play_audio("output.wav")
        except Exception as e:
            logging.error(f"Errore durante la sintesi vocale con Piper: {e}")

    def play_audio(self, filename: str):
        try:
            with wave.open(filename, "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                frames = wav_file.readframes(wav_file.getnframes())
                audio_data = np.frombuffer(frames, dtype=np.int16)
            sd.play(audio_data, sample_rate)
            sd.wait()
        except Exception as e:
            logging.error(f"Errore durante la riproduzione dell'audio: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    welcome_message = (
        "Hi, Emilia five point nine here, good morning crew! "
        "How can I help you today in crossing the Australian desert?"
    )

    tts = Pyttsx3TTS()
    tts.speak(welcome_message)

