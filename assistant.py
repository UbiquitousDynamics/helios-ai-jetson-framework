import logging
from multiprocessing import Process
from audio.tts import Pyttsx3TTS
from audio.sound_player import SoundPlayer
from api.api_client import APIClient
from recognizer.speech_recognizer import SpeechRecognizer
from document.document_retriever import DocumentRetriever
import speech_recognition as sr
import config

class VoiceAssistant:
    def __init__(self, documents: dict = None):
        self.documents = documents
        self.tts = Pyttsx3TTS()
        self.sound_player = SoundPlayer()
        self.api_client = APIClient()
        self.speech_recognizer = SpeechRecognizer()
        # Initialize DocumentRetriever if documents are provided
        self.document_retriever = DocumentRetriever(documents) if documents else None

    def process_command(self, command: str):
        if not command:
            logging.warning("No command to process.")
            return

        # Retrieve context from documents if available
        context = ""
        if self.document_retriever:
            retrieved_docs = self.document_retriever.retrieve(command)
            context = "\n".join([doc for _, doc in retrieved_docs])

        # Determine whether to use 'think' or 'talk' mode based on the command
        if "think" in command.lower() or "ponder" in command.lower():
            response = self.api_client.think(command, context)
        else:
            response = self.api_client.talk(command, context)

        logging.info(f"Received response: {response}")
        self.tts.speak(response)

    def play_sound_async(self, sound_file: str):
        """
        Plays a sound in a separate process so that it doesn't block the main thread.
        """
        process = Process(target=self.sound_player.play_sound, args=(sound_file,))
        process.start()

    def run(self):
        # Speak a welcome message before starting to listen for the wake word
        welcome_message = f"Hi there! I'm {config.WAKE_WORD}, your friendly AI assistant. Just say '{config.WAKE_WORD}' whenever you need help, and I'll be right here, ready to assist you!"
        self.tts.speak(welcome_message)
        logging.info("Welcome message delivered. Waiting for wake word.")
        
        while True:
            try:
                # Wait for the wake word
                if self.speech_recognizer.listen_for_wake_word(config.WAKE_WORD):
                    self.play_sound_async(config.WAKE_SOUND)
                    logging.info("Entering command mode.")
                    # Enter command mode: continuously listen for commands
                    while True:
                        try:
                            command = self.speech_recognizer.listen(timeout=config.LISTEN_TIMEOUT)
                            
                            # If the stop command is detected, exit command mode
                            if "stop" in command.lower():
                                self.play_sound_async(config.STOP_SOUND)
                                logging.info("'Stop' command detected, exiting command mode.")
                                break

                            # Process the received command
                            self.process_command(command)
                        except sr.WaitTimeoutError:
                            logging.info("No command received within timeout.")
                            self.play_sound_async(config.TIMEOUT_SOUND)
                            # Exit command mode after a timeout
                            break
            except Exception as e:
                logging.error(f"Error in the main loop: {e}")

