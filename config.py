
# Configuration constants for the speech synthesizer
NAME = "emilia"
LANGUAGE = "it"
TTS_FOLDER = 'tts_audio'

# Configuration constants for the speech recognizer
WAKE_WORD = 'emilia'
LISTEN_TIMEOUT = 5
WAKE_SOUND = 'sounds/wake_up.wav'
STOP_SOUND = 'sounds/stop.wav'
TIMEOUT_SOUND = 'sounds/stop.wav'

# Configuration constants for document loading
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'txt'}

PRES_A_1 = ""
PRES_A_2 = ""
PRES_A_3 = ""

# Configuration constants for STT vosk model
VOSK_MODEL_PATH = "None"
# switch language
if LANGUAGE == "en": 
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-en-us-0.15"
    VOICE = "mb-us1" # Select MBROLA-voices, example: mb-us1 (american english female voice); mb-it4 (italian female voice)
    PRES_Q_1 = "What's your name"
    PRES_Q_2 = "Who are you"
    PRES_Q_3 = "Introduce yourself"
    PRES_A_1 = ""
    PRES_A_2 = ""
    PRES_A_3 = ""

elif LANGUAGE == "it":
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-it-0.22"
    VOICE = "mb-it4" # Select MBROLA-voices, example: mb-us1 (american english female voice); mb-it4 (italian female voice)
    PRES_Q_1 = "come ti chiami"
    PRES_Q_2 = "chi sei"
    PRES_Q_3 = "presentati a"
    PRES_A_1 = "sono emilia, un'auto solare dotata di intelligenza artificiale, soccia"
    PRES_A_2 = "io sono emilia, un'auto solare dotata di intelligenza artificiale"
    PRES_A_3 = "piacere di conoiscerti!, sono emilia, sono un'auto solare dotata di intelligenza artificiale"

PRES_A_SWITCH = {
   1:PRES_A_1,
   2:PRES_A_2,
   3:PRES_A_3,
}
    
# Configuration constants for the API client
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_TALK = "emilia-gemma3:1b"              #    "emilia-qwen3:0.6b" # KO
MODEL_THINK = "qwen3:0.6b"

QA_JSON_PATH = "document/q&a/IT-WSC_25.json"
TOP_K = 4
EMBEDDING_MODEL = "mxbai-embed-large"