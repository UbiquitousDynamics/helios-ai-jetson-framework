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
            # Logga il testo (primi 50 caratteri) che sarà pronunciato
            logging.debug(f"Pronuncia TTS per il testo: {text[:50]}...")
            # Crea un oggetto wave per scrivere l'audio
            with wave.open("output.wav", "wb") as wav_file:
                # Sintetizza il testo in audio e scrivilo nel file wave
                self.voice.synthesize(text, wav_file)
            # Riproduci l'audio generato
            self.play_audio("output.wav")
        except Exception as e:
            logging.error(f"Errore durante la sintesi vocale con Piper: {e}")

    def play_audio(self, filename: str):
        try:
            # Leggi i dati audio dal file
            with open(filename, "rb") as f:
                audio_data = np.frombuffer(f.read(), dtype=np.int16)
            # Riproduci l'audio
            sd.play(audio_data, self.voice.config.sample_rate)
            sd.wait()  # Attendi che la riproduzione finisca
        except Exception as e:
            logging.error(f"Errore durante la riproduzione dell'audio: {e}")