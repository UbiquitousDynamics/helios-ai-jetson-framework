import json
import logging
import time
import pyaudio  # for audio recording
from vosk import Model, KaldiRecognizer
import config

class SpeechRecognizer:
    @staticmethod
    def remove_consecutive_duplicates(text: str) -> str:
        """
        Rimuove duplicati consecutivi di parole in una stringa.
        Esempio: "ciao ciao come stai" -> "ciao come stai"
        """
        words = text.split()
        if not words:
            return text
        filtered = [words[0]]
        for w in words[1:]:
            if w != filtered[-1]:
                filtered.append(w)
        return ' '.join(filtered)
    def __init__(self):
        # Load the Vosk model for the specified language
        self.model = Model(config.VOSK_MODEL_PATH)
        # Initialize PyAudio
        self.p = pyaudio.PyAudio()

    def listen(self, timeout: int = None):
        # Configure recording parameters
        rate = 16000
        chunk = 4000
        logging.info("Listening...")
        
        # Open the audio stream from the microphone
        stream = self.p.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True, frames_per_buffer=chunk)
        stream.start_stream()

        recognizer = KaldiRecognizer(self.model, rate)
        result_text = ""
        start_time = time.time()
        last_partial = None

        while True:
            data = stream.read(chunk, exception_on_overflow=False)
            if recognizer.AcceptWaveform(data):
                res = json.loads(recognizer.Result())
                text = res.get("text", "")
                if text:
                    clean_text = self.remove_consecutive_duplicates(text)
                    result_text += clean_text
                    yield clean_text  # yield final recognized text senza duplicati
            else:
                partial = json.loads(recognizer.PartialResult()).get("partial", "")
                if partial and partial != last_partial:
                    last_partial = partial
                    clean_partial = self.remove_consecutive_duplicates(partial)
                    yield clean_partial  # yield partial result senza duplicati
            if timeout is not None and (time.time() - start_time) > timeout:
                break

        stream.stop_stream()
        stream.close()
        logging.debug(f"Recognized command: {result_text}")
        # Optionally yield the final result_text at the end
        # yield result_text

    def listen_for_wake_word(self, wake_word: str) -> bool:
        try:
            command = self.listen(timeout=4)
            logging.debug(f"Recognized command: {command}")
            if wake_word.lower() in command.lower() or  "hello" in command.lower() or  "amelia" in command.lower():
                logging.info("Wake word detected!")
                return True
        except Exception as e:
            logging.error(f"Error listening for wake word: {e}")
        return False
