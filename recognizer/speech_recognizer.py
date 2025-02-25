import logging
import speech_recognition as sr
import config

class SpeechRecognizer:
    def __init__(self, language: str = config.LANGUAGE):
        self.recognizer = sr.Recognizer()
        self.mic = sr.Microphone()
        self.language = language
        print(language)

    def listen(self, timeout: int = None) -> str:
        # Listen for speech input with ambient noise adjustment
        with self.mic as source:
            self.recognizer.adjust_for_ambient_noise(source)
            logging.info("Listening...")
            try:
                audio = self.recognizer.listen(source, timeout=timeout)
                command = self.recognizer.recognize_google(audio, language=self.language)
                logging.debug(f"Recognized command: {command}")
                return command
            except sr.WaitTimeoutError:
                logging.info("Listening timeout reached.")
                raise
            except sr.UnknownValueError:
                logging.warning("Could not understand the audio.")
                return ""
            except sr.RequestError as e:
                logging.error(f"Error with the speech recognition request: {e}")
                return ""

    def listen_for_wake_word(self, wake_word: str) -> bool:
        # Listen for a wake word in the recognized speech
        try:
            command = self.listen()
            if wake_word.lower() in command.lower():
                logging.info("Wake word detected!")
                return True
        except Exception as e:
            logging.error(f"Error listening for wake word: {e}")
        return False
