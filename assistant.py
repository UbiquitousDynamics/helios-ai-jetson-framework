import logging
from multiprocessing import Process
from audio.tts import Pyttsx3TTS
from audio.sound_player import SoundPlayer
from api.api_client import APIClient
from recognizer.speech_recognizer import SpeechRecognizer # you can switch to recognizer.speech_recognizer_pocketsphinx: lower performances
import config
import random
import time
from document.rag_system import RagSystem
from enum import Enum, auto
# -- coding: utf-8 --

class AssistantState(Enum):
    IDLE = auto()
    COMMAND = auto()
    RAG = auto()

class VoiceAssistant:
    def __init__(self):
        #self.documents = documents
        self.tts = Pyttsx3TTS()
        self.sound_player = SoundPlayer()
        self.api_client = APIClient()
        self.speech_recognizer = SpeechRecognizer()
        # Initialize DocumentRetriever if documents are provided
       # self.document_retriever = DocumentRetriever(documents) if documents else None

    def contains_wake_word(self, command: str) -> bool:
        """
        Check if the command contains the wake word or alternative activation words.
        """
        if not command:
            return False
        wake_words = [config.WAKE_WORD.lower(), "hello", "amelia"]
        command_lower = command.lower()
        return any(word in command_lower for word in wake_words)

    def process_command(self, command: str):
        if not command:
            logging.warning("No command to process.")
            return

        # Check if command contains wake word - only process if it does
        if not self.contains_wake_word(command):
            logging.info(f"Command '{command}' does not contain wake word '{config.WAKE_WORD}'. Ignoring.")
        #    if config.LANGUAGE == "it":
        #        rejection_message = f"Devi includere '{config.WAKE_WORD}' nel tuo comando."
        #    else:
        #        rejection_message = f"You must include '{config.WAKE_WORD}' in your command."
        #    self.tts.speak(rejection_message)
            return

        logging.info(f"Processing command: {command}")

        # Retrieve context from documents if available
        context = ""
        #if self.document_retriever:
        #    retrieved_docs = self.document_retriever.retrieve(command)
        #    context = "\n".join([doc for _, doc in retrieved_docs])

        # Check for predefined questions
        if config.PRES_Q_1 in command.lower() or config.PRES_Q_2 in command.lower() or config.PRES_Q_3 in command.lower():
            option = random.randint(1, 3)
            command_pres = config.PRES_A_SWITCH[option]
            response = self.tts.speak(command_pres)
        else:
            response = self.api_client.talk(command, context)

        logging.info(f"Received response: {response}")

    def process_rag_command(self, command: str, searcher):
        """
        Process RAG commands with wake word validation.
        """
        logging.debug("Entered process_rag_command")
        if not command:
            logging.debug("Command is empty or None")
            logging.warning("No RAG command to process.")
            return

        logging.debug(f"Processing RAG command: {command}")
        # Use the searcher to run the query
        logging.debug("Calling searcher.run() with query and top_k=1")
        result = searcher.run(query=command, top_k=1)
        logging.debug(f"Result from searcher.run: {result}")
        # Convert result to a readable string for TTS
        if isinstance(result, list):
            logging.debug("Result is a list, formatting result_str as list of answers")
            result_str = "\n".join([
                f"Risposta {i+1}: indice {idx}, score {score:.2f}" for i, (idx, score) in enumerate(result)
            ])
        else:
            logging.debug("Result is not a list, converting result to string")
            result_str = str(result)
        logging.debug(f"Final result_str to speak: {result_str}")
        self.tts.speak("Here's what I found: " + result_str)
        logging.debug("Called self.tts.speak with result_str")

    def play_sound_async(self, sound_file: str):
        """
        Plays a sound in a separate process so that it doesn't block the main thread.
        """
        process = Process(target=self.sound_player.play_sound, args=(sound_file,))
        process.start()

    # threaded_listen rimossa: ora si usa direttamente listen()

    def run(self):
        logging.info("Starting Voice Assistant...")
        searcher = RagSystem(
            txt_dir="uploads",
            emb_file="embeddings.npz",
            model_name='./models/all-MiniLM-L6-v2',
            reindex=False
        )
        searcher.run(query="How many liters of water?", top_k=1)

        if config.LANGUAGE == "it":
            welcome_message = f" Ciao! Mi sono appena svegliata e sono pronta ad aiutarti. Ricordati solo di chiamarmi '{config.WAKE_WORD}' quando mi parli."
        else:
            welcome_message = f" Hi! I just woke up and I'm ready to help. Just remember to call me '{config.WAKE_WORD}' when you talk to me."
        self.tts.speak(welcome_message)
        logging.info("Welcome message delivered. Waiting for commands.")
        state = AssistantState.COMMAND  # Avvia direttamente in COMMAND mode

        while True:
            try:
                if state == AssistantState.COMMAND:
                    command = None
                    for text in self.speech_recognizer.listen(timeout=config.LISTEN_TIMEOUT):
                        if text.strip():
                            command = text.strip()  # Prendi sempre l'ultimo non vuoto
                    if command:
                        if config.RAG_WORD in command.lower():
                            self.play_sound_async(config.WAKE_SOUND)
                            logging.info("'Regolamento' command detected. Entering RAG mode.")
                            state = AssistantState.RAG
                        elif self.contains_wake_word(command):
                            self.process_command(command)
                    # Se timeout, semplicemente continua ad ascoltare

                elif state == AssistantState.RAG:
                    rag_command = None
                    for text in self.speech_recognizer.listen(timeout=config.LISTEN_TIMEOUT):
                        if text.strip():
                            rag_command = text.strip()  # Prendi sempre l'ultimo non vuoto
                    if rag_command:
                        self.process_rag_command(rag_command, searcher)
                        self.play_sound_async(config.STOP_SOUND)
                        logging.info("Exiting RAG mode after response.")
                        state = AssistantState.COMMAND
                    else:
                        # Se timeout, semplicemente continua ad ascoltare in RAG
                        pass

            except Exception as e:
                logging.error(f"Error in the main loop: {e}")
                time.sleep(1)