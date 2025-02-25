import logging
import pyttsx3
from gtts import gTTS
import os
import config

class Pyttsx3TTS:
    def __init__(self, voce=config.VOICE, rate=130, volume=1.0):
        self.engine = pyttsx3.init()
        voices = self.engine.getProperty('voices')
        voice_set = False

        for voice in voices:
            print(f"ID: {voice.id}, Name: {voice.name}")
        # Attempt to set a voice matching the desired language
        for voice in self.engine.getProperty('voices'):
            if voce in voice.name.lower():  # Change to "david" or "hazel" if needed
                self.engine.setProperty('voice', voice.id)
                print(f"Selected voice: {voice.name}")
                break

        if not voice_set:
            logging.warning(f"No voice found for language: {config.LANGUAGE}")
        self.engine.setProperty('rate', rate)
        self.engine.setProperty('volume', volume)
        logging.debug("Pyttsx3 engine initialized.")

    def speak(self, text: str):
        try:
            logging.debug(f"Performing TTS for text: {text[:50]}...")
            self.engine.say(text)
            self.engine.runAndWait()
        except Exception as e:
            logging.error(f"Error during TTS: {e}")

class GttsTTS:
    def generate_audio(self, text: str, filename: str, language=config.LANGUAGE) -> str:
        try:
            tts = gTTS(text, lang=language)
            file_path = os.path.join(config.TTS_FOLDER, filename)
            tts.save(file_path)
            logging.debug(f"Generated audio file: {file_path}")
            return file_path
        except Exception as e:
            logging.error(f"Error generating TTS audio for text: {e}")
            return ""
