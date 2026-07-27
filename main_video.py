
from audio.tts import Pyttsx3TTS
import time

welcome_message = f"Hi, Emilia five point nine here, good morning crew! How can I help you today in crossing the Australian desert?"
Pyttsx3TTS().speak(welcome_message)
time.sleep(60)
