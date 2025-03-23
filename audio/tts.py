import logging
import pyttsx3
from gtts import gTTS
import os
import config
import subprocess

class Pyttsx3TTS:
    def __init__(self, voce=config.VOICE, rate=120, volume=2.0):
        # set parameters for espeak
        self.voce = voce
        self.rate = rate
        # Note: espeak does not support settings volume directly via command line;
        # volume control is usually handled at the system level.
        logging.debug(f"Pyttsx3 (espeak version) initialized with voice: {self.voce}, rate: {self.rate}")

    def speak(self, text: str):
        try:
            # Log the text (first 50 characters) that will be spoken
            logging.debug(f"Performing TTS for text: {text[:50]}...")
            # Execute the espeak command with the specified voices, rate, and fixed pitch of 70
            subprocess.run(["espeak", "-v", self.voce, "-s", str(self.rate), "-p", "50", text])
        except Exception as e:
            logging.error(f"Error during TTS with espaeak: {e}")

class GttsTTS:
    def generate_audio(self, text: str, filename: str, language=config.LANGUAGE) -> str:
        try:
            # Create a gTTS objects with the given text and language
            tts = gTTS(text, lang=language)
            # Construct the full file path to save the audio file
            file_path = os.path.join(config.TTS_FOLDER, filename)
            # Save the generated speech as an audio file
            tts.save(file_path)
            logging.debug(f"Generated audio file: {file_path}")
            return file_path
        except Exception as e:
            logging.error(f"Error generating TTS audio for text: {e}")
            return ""
