import logging
import subprocess
import os

try:
    subprocess.run(
        ['aplay', '--version'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True
    )
except (subprocess.CalledProcessError, FileNotFoundError) as e:
    logging.error(f"Error initializing aplay: {e}")

class SoundPlayer:
    def play_sound(self, sound_file: str):
        try:
            if not os.path.isfile(sound_file):
                raise FileNotFoundError(f"Sound file not found: {sound_file}")
            subprocess.run(['aplay', sound_file], check=True)
            logging.debug(f"Sound played: {sound_file}")
        except Exception as e:
            logging.error(f"Error playing sound: {e}")