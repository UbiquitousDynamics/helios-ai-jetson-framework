import json
import logging
import time
import pyaudio  # for audio recording
from vosk import Model, KaldiRecognizer
import config

class SpeechRecognizer:
    def __init__(self):
        # Load the Vosk model for the specified language
        self.model = Model(config.VOSK_MODEL_PATH)
        # Initialize PyAudio
        self.p = pyaudio.PyAudio()

    def listen(self, timeout: int = None) -> str:
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

        while True:
            data = stream.read(chunk, exception_on_overflow=False)
            if recognizer.AcceptWaveform(data):
                res = json.loads(recognizer.Result())
                result_text += res.get("text", "")
            else:
                # Optionally, get partial result:
                partial = json.loads(recognizer.PartialResult()).get("partial", "")
                # You can log the partial result if needed
            if timeout is not None and (time.time() - start_time) > timeout:
                break

        stream.stop_stream()
        stream.close()
        logging.debug(f"Recognized command: {result_text}")
        return result_text

    def listen_for_wake_word(self, wake_word: str) -> bool:
        try:
            command = self.listen(timeout=config.LISTEN_TIMEOUT)
            logging.debug(f"Recognized command: {command}")
            if wake_word.lower() in command.lower():
                logging.info("Wake word detected!")
                return True
        except Exception as e:
            logging.error(f"Error listening for wake word: {e}")
        return False
