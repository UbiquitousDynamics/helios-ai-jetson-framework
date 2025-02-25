
# Configuration constants for the speech synthesizer
LANGUAGE = "en"
VOICE = "zira"
TTS_FOLDER = 'tts_audio'

# Configuration constants for the speech recognizer
WAKE_WORD = 'UB'
LISTEN_TIMEOUT = 7
WAKE_SOUND = 'sounds/wake_up.mp3'
STOP_SOUND = 'sounds/stop.mp3'
TIMEOUT_SOUND = 'sounds/stop.mp3'

# Configuration constants for document loading
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'txt'}

# Configuration constants for the API client
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_TALK = "hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q8_0"
MODEL_THINK = "deepseek-r1:7b"
