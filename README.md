# Ubi - AI Voice Assistant

Ubi is a voice assistant powered by artificial intelligence, utilizing local LLM models to generate responses, recognize voice commands, and interact with uploaded documents.

## Features

- **Speech Recognition**: Uses `speech_recognition` to interpret voice commands.
- **Text-to-Speech (TTS)**: Supports `pyttsx3` (offline) and `gTTS` (Google TTS).
- **AI API Interaction**: Integrated with `Ollama` to generate responses using LLM models.
- **Document Management**: Load and extract text from PDF and TXT files.
- **Information Retrieval**: Implements TF-IDF to find relevant documents based on user queries.
- **Audio Playback**: Sound effects for assistant feedback.

## Project Structure

```
project/
│
├── main.py                   # Entry point
├── assistant.py              # Assistant orchestration
│
├── config.py                 # Configuration file for environment variables
│
├── api/
│   ├── __init__.py
│   ├── api_client.py         # Communication with Ollama
│
├── document/
│   ├── __init__.py
│   ├── document_loader.py    # Document loading
│   ├── document_retriever.py # TF-IDF document retrieval
│
├── audio/
│   ├── __init__.py
│   ├── sound_player.py       # Audio playback
│   ├── tts.py                # TTS implementation
│
├── recognizer/
│   ├── __init__.py
│   ├── speech_recognizer.py  # Speech recognition
```

## Installation

### Prerequisites

- Python 3.8+
- AI models served via Ollama (`ollama` installed and running)
- Required packages (installable via `pip`)

### Install Required Packages

```bash
pip install -r requirements.txt
```

### Start the Assistant

```bash
python main.py
```

## Configuration

The assistant's settings are stored in `config.py`, where you can modify environment variables such as:

```python
# config.py
LANGUAGE = "en"
WAKE_WORD = "Ubi"
LISTEN_TIMEOUT = 7
WAKE_SOUND = "sounds/wake_up.mp3"
STOP_SOUND = "sounds/stop.mp3"
TIMEOUT_SOUND = "sounds/timeout.mp3"
TTS_VOICE = "Microsoft Zira Desktop - English (United States)"
```

## Contributions

Contributions are welcome! Feel free to open an issue or submit a pull request with improvements or fixes.

## License

This project is distributed under the MIT License.
