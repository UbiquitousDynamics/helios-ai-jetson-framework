import logging
from multiprocessing import Process
from audio.tts import Pyttsx3TTS
from audio.sound_player import SoundPlayer
from api.api_client import APIClient
from recognizer.speech_recognizer import SpeechRecognizer # you can switch to recognizer.speech_recognizer_pocketsphinx: lower performances
from document.document_retriever import DocumentRetriever
import speech_recognition as sr
import config
import random
import time
from document.rag_system import RagSystem
# -*- coding: utf-8 -*-

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
        Check if the command contains the wake word.
        """
        if not command:
            return False
        return config.WAKE_WORD.lower() in command.lower()

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
        if not command:
            logging.warning("No RAG command to process.")
            return

        logging.info(f"Processing RAG command: {command}")
        # Use the searcher to run the query
        result = searcher.run(query=command, top_k=3, visualize=False)
        self.tts.speak(result)

    def play_sound_async(self, sound_file: str):
        """
        Plays a sound in a separate process so that it doesn't block the main thread.
        """
        process = Process(target=self.sound_player.play_sound, args=(sound_file,))
        process.start()

    def run(self):
        logging.info("Starting Voice Assistant...")
        # Initialize the text-to-speech engine  
        searcher = RagSystem(
            txt_file="uploads/regolamento.txt",
            emb_file="embeddings.npy",
            model_name='all-MiniLM-L6-v2',
            reindex=False
        )
          # Test searcher
        searcher.run(query="How many liters of water?", top_k=5, visualize=False)

        # Speak a welcome message before starting to listen for the wake word
        if config.LANGUAGE == "it":
            welcome_message = f" Ciao! Mi sono appena svegliata e sono pronta ad aiutarti. Ricordati solo di chiamarmi '{config.WAKE_WORD}' quando mi parli."
        else:
            welcome_message = f" Hi! I just woke up and I'm ready to help. Just remember to call me '{config.WAKE_WORD}' when you talk to me."
        
        self.tts.speak(welcome_message)
        logging.info("Welcome message delivered. Waiting for wake word.")
        
        while True:
            try:
                # Wait for the wake word
                if True: #self.speech_recognizer.listen_for_wake_word(config.WAKE_WORD):
                   # self.play_sound_async(config.WAKE_SOUND)
                    logging.info("Entering command mode.")
                    
                    # Enter command mode: continuously listen for commands
                    while True:
                        try:
                            command = self.speech_recognizer.listen(timeout=config.LISTEN_TIMEOUT+2)
                            
                            # If the stop command is detected, exit command mode
                            if "stop" in command.lower():
                                self.play_sound_async(config.STOP_SOUND)
                                logging.info("'Stop' command detected, exiting command mode.")
                                break
                    
                            # Check for RAG mode activation
                            if config.RAG_WORD in command.lower():
                                self.play_sound_async(config.WAKE_SOUND)
                                #time.sleep(0.1)
                                #self.play_sound_async(config.WAKE_SOUND)
                                #time.sleep(0.1)
                                #self.play_sound_async(config.WAKE_SOUND)
                                logging.info("'Regolamento' command detected")
                                logging.info("Entering RAG mode.")
            
                                # Enter RAG mode: continuously listen for regulation questions
                                while True:
                                    try:
                                        rag_command = self.speech_recognizer.listen(timeout=config.LISTEN_TIMEOUT)
                                        
                                        # If the stop command is detected, exit RAG mode
                                        if "stop" in rag_command.lower():
                                            self.play_sound_async(config.STOP_SOUND)
                                            #time.sleep(0.1)
                                            #self.play_sound_async(config.STOP_SOUND)
                                            #time.sleep(0.1)
                                            #self.play_sound_async(config.STOP_SOUND)
                                            logging.info("'Stop' command detected, exiting RAG mode.")
                                            break
                                        
                                        if rag_command:
                                            self.process_rag_command(rag_command, searcher)
                                        
                                        time.sleep(0.3)
                                        
                                    except sr.WaitTimeoutError:
                                        logging.info("No RAG command received within timeout.")
                                        self.play_sound_async(config.TIMEOUT_SOUND)
                                        # Exit RAG mode after a timeout
                                        break
                         
                            # Process regular command ONLY if it contains the wake word
                            elif self.contains_wake_word(command):
                                self.process_command(command)
                            # else: do nothing

                        except sr.WaitTimeoutError:
                            logging.info("No command received within timeout.")
                            self.play_sound_async(config.TIMEOUT_SOUND)
                            # Exit command mode after a timeout
                            break

            except Exception as e:
                logging.error(f"Error in the main loop: {e}")
                # Optional: add a small delay before retrying to prevent rapid error loops
                time.sleep(1)
