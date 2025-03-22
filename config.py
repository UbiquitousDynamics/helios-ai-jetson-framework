
# Configuration constants for the speech synthesizer
LANGUAGE = "en"
VOICE = "zira" #select elsa for it or zira for en
TTS_FOLDER = 'tts_audio'

# Configuration constants for the speech recognizer
WAKE_WORD = 'hello'
LISTEN_TIMEOUT = 10
WAKE_SOUND = 'sounds/wake_up.mp3'
STOP_SOUND = 'sounds/stop.mp3'
TIMEOUT_SOUND = 'sounds/stop.mp3'

# Configuration constants for document loading
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'txt'}

# Configuration constants for STT vosk model
VOSK_MODEL_PATH = "None"
# switch language
if LANGUAGE == "en": 
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-en-us-0.15"
elif LANGUAGE == "it":
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-it-0.22"

# Configuration constants for the API client
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_TALK = "llama3.2:1b"
MODEL_THINK = "llama3.2:1b"
