


ollama run hf.co/unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF:Q4_K_M
FROM qwen3:8b

TEMPLATE """
{{- if eq .Role "user" }}
/no_think {{ .Content }}
{{ else if eq .Role "assistant" }}
{{ .Content }}
{{ end }}
"""

ollama run hf.co/unsloth/Qwen3-0.6B-GGUF:Q4_K_M
import asyncio
import logging
import signal
from audio.tts import AsyncPyttsx3TTS
from api.async_api_client import AsyncAPIClient
import config as CONFIG

# Configurazione del logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Classe principale dell'assistente
class VoiceAssistant:
    def __init__(self):
        # Inizializza il TTS asincrono
        self.tts = AsyncPyttsx3TTS(rate=CONFIG.TTS_RATE, voice=CONFIG.TTS_VOICE)
        
        # Inizializza il client API asincrono
        self.api = AsyncAPIClient(
            api_url=CONFIG.OLLAMA_API_URL,
            model_talk=CONFIG.MODEL_TALK,
            model_think=CONFIG.MODEL_THINK,
            tts=self.tts
        )
        
        # Flag per l'arresto
        self.running = True
        
        # Configura gestori di segnali per chiusura pulita
        signal.signal(signal.SIGINT, self.handle_exit)
        signal.signal(signal.SIGTERM, self.handle_exit)
        
        logging.info("Voice Assistant initialized")

    def handle_exit(self, signum, frame):
        """Gestisce la chiusura pulita dell'applicazione"""
        logging.info("Shutdown signal received...")
        self.running = False
        self.tts.stop()
        
    async def process_command(self, command):
        """Elabora un comando vocale in modo asincrono"""
        if not command or not command.strip():
            return "Comando non riconosciuto"
            
        command = command.strip().lower()
        logging.info(f"Processing command: {command}")
        
        # Invia il comando al modello e attendi la risposta
        response = await self.api.talk(command)
        
        # Registra la risposta ottenuta
        logging.info(f"Received response: {response[:100]}..." if len(response) > 100 else f"Received response: {response}")
        return response
        
    async def welcome(self):
        """Invia un messaggio di benvenuto"""
        welcome_message = "Ciao! Sono il tuo assistente vocale. Come posso aiutarti?"
        logging.debug(f"Performing TTS for text: {welcome_message}...")
        await self.tts.async_speak(welcome_message)
        logging.info("Welcome message delivered. Waiting for wake word.")
            
    async def run(self):
        """Esegue il loop principale dell'assistente"""
        try:
            # Invia messaggio di benvenuto
            await self.welcome()
            
            # Simula un loop principale per questo esempio
            while self.running:
                # Qui andrà il codice per il riconoscimento della wake word
                # e il riconoscimento vocale, ad esempio:
                
                # Esempio: simula un'attesa di comandi
                # In un'implementazione reale, questo verrebbe sostituito dal codice
                # di riconoscimento vocale
                command = await asyncio.to_thread(input, "Inserisci un comando (o 'exit' per uscire): ")
                
                if command.lower() == 'exit':
                    self.running = False
                    continue
                    
                # Elabora il comando
                await self.process_command(command)
                
        except Exception as e:
            logging.error(f"Error in the main loop: {e}", exc_info=True)
        finally:
            # Pulizia
            self.tts.stop()
            logging.info("Voice Assistant stopped")
            
# Punto di ingresso per l'esecuzione
def main():
    """Funzione principale per avviare l'assistente"""
    assistant = VoiceAssistant()
    
    # Esegui il loop principale in modo asincrono
    asyncio.run(assistant.run())
    
if __name__ == "__main__":
    main()
