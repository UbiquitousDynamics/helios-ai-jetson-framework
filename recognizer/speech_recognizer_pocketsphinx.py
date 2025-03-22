import logging
from pocketsphinx import LiveSpeech
import config

class SpeechRecognizer:
    def __init__(self, language: str = config.LANGUAGE):
        # Configurazione per PocketSphinx
        # Modifica i percorsi in base a dove sono installati i modelli per la lingua italiana.
        self.config_ps = {
            'verbose': False,
            'hmm': 'recognizer\\models\\pocketSphinx\\pocketsphinx-main\\model\\en-us\\en-us',       # Cartella dell'acoustic model per l'inglese
            'lm': 'recognizer\\models\\pocketSphinx\\pocketsphinx-main\\model\\en-us\\en-us.lm.bin',        # File del language model per l'inglese
            'dict': 'recognizer\\models\\pocketSphinx\\pocketsphinx-main\\model\\en-us\\cmudict-en-us.dict'  # File del dizionario per l'inglese
        }
        self.language = language
        print(f"Using PocketSphinx model for language: {self.language}")

    def listen(self, timeout: int = None) -> str:
        logging.info("Listening...")
        # PocketSphinx LiveSpeech è un iteratore che fornisce i risultati del riconoscimento vocale
        # Non gestisce direttamente il timeout, quindi si prende il primo risultato disponibile.
        recognized_text = ""
        for phrase in LiveSpeech(**self.config_ps):
            recognized_text = str(phrase)
            break  # Prendi solo il primo risultato
        logging.debug(f"Recognized command: {recognized_text}")
        return recognized_text

    def listen_for_wake_word(self, wake_word: str) -> bool:
        try:
            command = self.listen()
            logging.debug(f"Comando riconosciuto: {command}")
            if wake_word.lower() in command.lower():
                logging.info("Wake word detected!")
                return True
        except Exception as e:
            logging.error(f"Error listening for wake word: {e}")
        return False

# Esempio di utilizzo
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    recognizer = SpeechRecognizer(language=config.LANGUAGE)
    
    if recognizer.listen_for_wake_word("ciao"):
        logging.info("Wake word 'ciao' riconosciuto!")
    else:
        logging.info("Wake word non riconosciuto.")