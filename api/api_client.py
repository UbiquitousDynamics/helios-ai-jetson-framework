# api/api_client.py
import logging
import requests
from tenacity import retry, wait_fixed, stop_after_attempt
import config

class APIClient:
    def __init__(self, api_url: str = config.OLLAMA_API_URL, model_talk: str = config.MODEL_TALK, model_think: str = config.MODEL_THINK):
        self.api_url = api_url
        self.model_talk = model_talk
        self.model_think = model_think

    @retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
    def _send_request(self, model: str, prompt: str) -> str:
        payload = {"model": model, "prompt": prompt, "stream": False}
        logging.debug(f"Invio richiesta a Ollama con modello {model} e prompt: {prompt[:50]}...")
        response = requests.post(self.api_url, json=payload, timeout=150)
        response.raise_for_status()
        logging.debug(f"Risposta ricevuta: {response.text}")
        return response.json().get('response', "Nessuna risposta ricevuta.")

    def talk(self, message: str, context: str = None) -> str:
        prompt = f"Context:\n{context}\n\nPrompt:\n{message}" if context else message
        return self._send_request(self.model_talk, prompt)

    def think(self, message: str, context: str = None) -> str:
        prompt = f"Context:\n{context}\n\nPrompt:\n{message}" if context else message
        return self._send_request(self.model_think, prompt)