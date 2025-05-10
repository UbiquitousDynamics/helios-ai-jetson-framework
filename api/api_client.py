import json
import logging
import requests
from tenacity import retry, wait_fixed, stop_after_attempt
from audio.tts import Pyttsx3TTS
import config as CONFIG
# Assicurati che CONFIG.OLLAMA_API_URL, CONFIG.MODEL_TALK, CONFIG.MODEL_THINK siano definiti
# Esempio di configurazione (sostituisci con i tuoi valori effettivi)


class APIClient:
    def __init__(self, api_url: str = CONFIG.OLLAMA_API_URL, model_talk: str = CONFIG.MODEL_TALK, model_think: str = CONFIG.MODEL_THINK):
        self.api_url = api_url
        self.model_talk = model_talk
        self.model_think = model_think
        self.tts = Pyttsx3TTS()

    @retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
    def _send_request_stream(self, model: str, prompt: str): # Rinominiamo per chiarezza e modifichiamo per lo streaming
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True  # Abilita lo streaming
        }
        logging.debug(f"Invio richiesta a Ollama con modello [{model}] e prompt: [{prompt[:50]}...]")

        try:
            # stream=True in requests.post fa sì che la connessione rimanga aperta
            # e il contenuto sia scaricato solo quando si accede a response.iter_lines()
            with requests.post(self.api_url, json=payload, stream=True, timeout=150) as response:
                response.raise_for_status()  # Solleva un'eccezione per codici di stato HTTP 4xx/5xx
                
                full_response_text = ""
                for line in response.iter_lines():
                    if line:
                        try:
                            json_line = json.loads(line.decode('utf-8'))
                            # La struttura del JSON di streaming di Ollama per /api/generate
                            # ha la parte di testo nel campo 'response'
                            # e un campo 'done' che indica se è l'ultimo blocco
                            # Per /api/chat, il contenuto è in json_line['message']['content']
                            
                            # Adatta il parsing in base all'endpoint che stai usando (/api/generate o /api/chat)
                            # Questo esempio assume /api/generate
                            response_part = json_line.get("response", "") 
                            # Se usi /api/chat, potrebbe essere qualcosa come:
                            # if json_line.get("message"):
                            #    response_part = json_line["message"].get("content", "")
                                
                            if response_part:
                                yield response_part # Restituisce ogni parte della risposta
                                full_response_text += response_part

                            if json_line.get("done"): # L'ultimo blocco ha "done": true
                                logging.debug(f"Risposta completa ricevuta: {full_response_text}")
                                break
                        except json.JSONDecodeError as e:
                            logging.warning(f"Errore nel decodificare una riga JSON dallo stream: {line.decode('utf-8')}, errore: {e}")
                        except Exception as e:
                            logging.error(f"Errore durante l'elaborazione di una riga dello stream: {e}")
                            
        except requests.exceptions.RequestException as e:
            logging.error(f"Errore nella richiesta a Ollama: {e}")
            raise # Rilancia l'eccezione per farla gestire da tenacity o dal chiamante

    def talk(self, message: str, context: str = None):
        prompt = f"Context: {context}\n\nInput: {message}" if context else message
        # Ora _send_request_stream restituisce un generatore
        # Puoi iterare su di esso per ottenere le parti della risposta
        # o unirle se hai bisogno dell'intera risposta alla fine
        response_parts = []
        response_phrase = []
        print("Assistant: ", end="", flush=True) # Per stampare sulla stessa riga
        for part in self._send_request_stream(self.model_talk, prompt):
            print(part, end="", flush=True) # Stampa ogni parte ricevuta
            response_parts.append(part)
            response_phrase.append(part)
            chars = ".,!?;:"
            if any(c in part for c in chars):
               self.tts.speak("".join(response_phrase))
               response_phrase = []
        print() # Nuova riga alla fine della risposta
        return "".join(response_parts) # Restituisce la risposta completa se necessario

    def think(self, message: str, context: str = None):
        prompt = f"Context: {context}\n\nInput: {message}" if context else message
        response_parts = []
        response_phrase = []
        # Non stampiamo per 'think', ma potresti volerlo fare o gestire i token in altro modo
        for part in self._send_request_stream(self.model_think, prompt):
            response_parts.append(part)
            response_phrase.append(part)
            chars = ".,!?;:"
            if any(c in part for c in chars):
               self.tts.speak("".join(response_phrase))
               response_phrase = []
        return "".join(response_parts)