# Configuration constants for the speech synthesizer
NAME = "emilia"
LANGUAGE = "en"
TTS_FOLDER = 'tts_audio'
TTS_MODEL = "audio/models/en_GB-alba-medium.onnx"  # default, sovrascritto poi se "it"

# Configuration constants for the speech recognizer
WAKE_WORD = 'emilia'
RAG_WORD = 'rag'  # sovrascritto poi se necessario
LISTEN_TIMEOUT = 7
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

# Modelli AI di default
MODEL_TALK = "emilia-en-gemma3:1b"
MODEL_THINK = "qwen3:0.6b"

# switch language
if LANGUAGE == "en": 
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-en-us-0.15"
    VOICE = "mb-us1"  # american english female voice
    PRES_Q_1 = "what's your name"
    PRES_Q_2 = "who are you"
    PRES_Q_3 = "introduce yourself"
    PRES_A_1 = "hi, i'm emilia five point nine, your solar-powered ai vehicle, ready to support the crew across the australian desert!"
    PRES_A_2 = "greetings, this is emilia five point nine, your intelligent solar companion—how can i assist you today on this desert mission?"
    PRES_A_3 = "hi, emilia five point nine here, good morning crew! how can i help you today in crossing the australian desert?"
    RAG_WORD = 'regulations'
    TTS_MODEL = "audio/models/en_GB-alba-medium.onnx"

elif LANGUAGE == "it":
    VOSK_MODEL_PATH = "recognizer/models/vosk-model-small-it-0.22"
    VOICE = "mb-it4"  # italian female voice
    PRES_Q_1 = "come ti chiami"
    PRES_Q_2 = "chi sei"
    PRES_Q_3 = "presentati a"
    PRES_A_1 = "sono emilia, un'auto solare dotata di intelligenza artificiale, soccia"
    PRES_A_2 = "io sono emilia, un'auto solare dotata di intelligenza artificiale"
    PRES_A_3 = "piacere di conoiscerti!, sono emilia, sono un'auto solare dotata di intelligenza artificiale"
    RAG_WORD = 'regolamento'
    TTS_MODEL = "audio/models/it_IT-paola-medium.onnx"
    MODEL_TALK = "emilia-gemma3:1b"

PRES_A_SWITCH = {
   1: PRES_A_1,
   2: PRES_A_2,
   3: PRES_A_3,
}

# Configuration constants for the API client
OLLAMA_API_URL = "http://localhost:11434/api/generate"
QA_JSON_PATH = "document/q&a/IT-WSC_25.json"
TOP_K = 4
EMBEDDING_MODEL = "mxbai-embed-large"
