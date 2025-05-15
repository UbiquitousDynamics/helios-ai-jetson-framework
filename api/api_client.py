import json
import logging
import requests
import re
from tenacity import retry, wait_fixed, stop_after_attempt
from audio.tts import Pyttsx3TTS
import config as CONFIG
from ollama import Client


class APIClient:
    """
    A client wrapper that uses the official Ollama Python SDK for chat and generate with TTS support.
    """
    def __init__(self,
                 api_url: str = CONFIG.OLLAMA_API_URL,
                 model_talk: str = CONFIG.MODEL_TALK,
                 model_think: str = CONFIG.MODEL_THINK,
                 tts: Pyttsx3TTS = None):
        # Initialize Ollama client (defaults to localhost:11434 or CONFIG.OLLAMA_API_URL)
        self.client = Client()
        self.models = {
            'talk': model_talk,
            'think': model_think
        }
        self.tts = tts or Pyttsx3TTS()
        # Init model
        self.client.chat(
                model=self.models['talk'],
                messages=[{'role': 'user', 'content': ''}],
                stream=False
            )
        logging.basicConfig(level=logging.DEBUG)

    @retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
    def talk(self, message: str, context: str = None):
        """
        Send a user message to the talk model, stream responses, print and TTS sentence-by-sentence.
        """
        try:
            # Log dell'inizio della funzione
            logging.info(f"Sending message to model '{self.models['talk']}': {message[:50]}...")
            
            # Prepare messages for chat endpoint
            msgs = []
            if context:
                msgs.append({'role': 'system', 'content': context})
            msgs.append({'role': 'user', 'content': message})
    
            # Log dei messaggi inviati
            logging.debug(f"Full messages being sent: {msgs}")
            
            response_parts = []
            sentence_buffer = []
            punctuation = set(['.', ',', '!', '?', ';', ':'])
    
            # Aggiungiamo un contatore per monitorare i chunk
            chunk_count = 0
            
            # Stream chat completions
            logging.info("Starting to stream responses...")
            for chunk in self.client.chat(
                model=self.models['talk'],
                messages=msgs,
                stream=True
            ):
                chunk_count += 1
                # Log del chunk ricevuto
                logging.debug(f"Received chunk #{chunk_count}: {chunk}")
                
                # Verifica della struttura del chunk
                if not hasattr(chunk, 'message') or not hasattr(chunk.message, 'content'):
                    logging.error(f"Unexpected chunk structure: {chunk}")
                    continue
                    
                # Ogni chunk è un ChatCompletionChunk
                text = chunk.message.content
                if text:  # Verifica che ci sia effettivamente del testo
                    logging.info(f"Text from chunk #{chunk_count}: '{text}'")
                    #print(text, end='', flush=True)
                    response_parts.append(text)
                    sentence_buffer.append(text)
    
                    # If sentence boundary or end of stream
                    if any(p in text for p in punctuation) or hasattr(chunk, 'done') and chunk.done:
                        sentence = ''.join(sentence_buffer)
                        logging.info(f"Speaking sentence: '{sentence}'")
                        sentence = re.sub(r'[*$#@]', '', sentence)
                        self.tts.speak(sentence)
                        sentence_buffer.clear()
                else:
                    logging.warning(f"Empty text in chunk #{chunk_count}")
    
            # Assicurati di pronunciare l'ultimo buffer se non è vuoto
            if sentence_buffer:
                sentence = ''.join(sentence_buffer)
                logging.info(f"Speaking final sentence: '{sentence}'")
                sentence = re.sub(r'[*$#@]', '', sentence)
                self.tts.speak(sentence)
                
            logging.info(f"Completed. Received {chunk_count} chunks in total.")
            #print()
            return ''.join(response_parts)
           
        except Exception as e:
            logging.error(f"Error in talk method: {e}", exc_info=True)
            #print(f"\nERROR: {e}")
            return f"Errore nella comunicazione con il modello: {e}"
    
    @retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
    def think(self, message: str, context: str = None, tts: bool = False):
        """
        Send a user message to the think model, stream internally and optionally speak.
        """
        msgs = []
        if context:
            msgs.append({'role': 'system', 'content': context})
        msgs.append({'role': 'user', 'content': message})

        output = []
        sentence_buffer = []
        punctuation = set(['.', ',', '!', '?', ';', ':'])

        for chunk in self.client.chat(
            model=self.models['think'],
            messages=msgs,
            stream=True
        ):
            text = chunk.message.content
            output.append(text)
            sentence_buffer.append(text)

            if tts and (any(p in text for p in punctuation) or chunk.finish_reason):
                sentence = ''.join(sentence_buffer)
                sentence = re.sub(r'[*$#@]', '', sentence)
                self.tts.speak(sentence)
                sentence_buffer.clear()

        return ''.join(output)
        

