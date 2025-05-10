
# Configuration constants for the speech synthesizer
NAME = "emilia"
LANGUAGE = "it"
TTS_FOLDER = 'tts_audio'

# Configuration constants for the speech recognizer
WAKE_WORD = 'emilia'
LISTEN_TIMEOUT = 10
WAKE_SOUND = 'sounds/wake_up.wav'
STOP_SOUND = 'sounds/stop.wav'
TIMEOUT_SOUND = 'sounds/stop.wav'

# Configuration constants for document loading
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'txt'}

# Configuration constants for STT vosk model
VOSK_MODEL_PATH = "None"
# switch language
if LANGUAGE == "en": 
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-en-us-0.15"
    VOICE = "mb-us1" # Select MBROLA-voices, example: mb-us1 (american english female voice); mb-it4 (italian female voice)
elif LANGUAGE == "it":
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-it-0.22"
    VOICE = "mb-it4" # Select MBROLA-voices, example: mb-us1 (american english female voice); mb-it4 (italian female voice)

# Configuration constants for the API client
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_TALK = "qwen3:0.6b"
MODEL_THINK = "qwen2.5:0.5b"
