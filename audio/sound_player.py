import logging
import pygame

# Initialize pygame mixer for audio playback
try:
    pygame.mixer.init()
except pygame.error as e:
    logging.error(f"Error initializing pygame mixer: {e}")

class SoundPlayer:
    def play_sound(self, sound_file: str):
        try:
            pygame.mixer.music.load(sound_file)
            pygame.mixer.music.play()
            # Wait until the sound has finished playing
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
            logging.debug(f"Sound played: {sound_file}")
        except Exception as e:
            logging.error(f"Error playing sound: {e}")
