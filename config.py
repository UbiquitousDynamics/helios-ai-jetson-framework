"""Application configuration with validated, project-rooted paths.

The module-level constants are retained for compatibility with the original
application.  New code should prefer :data:`SETTINGS`, which keeps related
values together and validates the selected language.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    """Raised when application settings are internally inconsistent."""


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    vosk_model: Path
    tts_model: Path
    voice: str
    wake_word: str
    wake_word_aliases: tuple[str, ...]
    rag_word: str
    presentation_questions: tuple[str, str, str]
    presentation_answers: tuple[str, str, str]
    talk_model: str
    welcome_message: str
    rag_result_prefix: str
    model_error_message: str


def _profile_paths(root: Path) -> Mapping[str, LanguageProfile]:
    return {
        "en": LanguageProfile(
            code="en",
            vosk_model=root / "recognizer/models/vosk-model-small-en-us-0.15",
            tts_model=root / "audio/models/en_GB-alba-medium.onnx",
            voice="mb-us1",
            wake_word="emilia",
            wake_word_aliases=("emilia", "amelia", "hello"),
            rag_word="regulation",
            presentation_questions=(
                "what's your name",
                "who are you",
                "introduce yourself",
            ),
            presentation_answers=(
                "Hi, I'm Emilia five point nine, your solar-powered AI vehicle, "
                "ready to support the crew across the Australian desert!",
                "Greetings, this is Emilia five point nine, your intelligent solar "
                "companion. How can I assist you today on this desert mission?",
                "Hi, Emilia five point nine here. Good morning crew! How can I help "
                "you today in crossing the Australian desert?",
            ),
            talk_model="emilia-en-gemma3:1b",
            welcome_message=(
                "Hi! I just woke up and I'm ready to help. Just remember to call "
                "me '{wake_word}' when you talk to me."
            ),
            rag_result_prefix="Here's what I found: ",
            model_error_message="I could not contact the language model.",
        ),
        "it": LanguageProfile(
            code="it",
            vosk_model=root / "recognizer/models/vosk-model-small-it-0.22",
            tts_model=root / "audio/models/it_IT-paola-medium.onnx",
            voice="mb-it4",
            wake_word="emilia",
            wake_word_aliases=("emilia", "amelia", "hello"),
            rag_word="regolamento",
            presentation_questions=("come ti chiami", "chi sei", "presentati a"),
            presentation_answers=(
                "Sono Emilia, un'auto solare dotata di intelligenza artificiale.",
                "Io sono Emilia, un'auto solare dotata di intelligenza artificiale.",
                "Piacere di conoscerti! Sono Emilia, un'auto solare dotata di "
                "intelligenza artificiale.",
            ),
            talk_model="emilia-gemma3:1b",
            welcome_message=(
                "Ciao! Mi sono appena svegliata e sono pronta ad aiutarti. "
                "Ricordati solo di chiamarmi '{wake_word}' quando mi parli."
            ),
            rag_result_prefix="Ecco cosa ho trovato: ",
            model_error_message="Non riesco a contattare il modello linguistico.",
        ),
    }


def normalize_ollama_host(value: str) -> str:
    """Return an Ollama SDK host from either a host or legacy endpoint URL."""

    candidate = value.strip()
    if not candidate:
        raise ConfigurationError("Ollama host cannot be empty")
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlsplit(candidate)
    if not parsed.hostname:
        raise ConfigurationError(f"Invalid Ollama host: {value!r}")

    path = parsed.path.rstrip("/")
    for endpoint in ("/api/generate", "/api/chat", "/api/embeddings", "/api/embed"):
        if path.endswith(endpoint):
            path = path[: -len(endpoint)]
            break

    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings.

    ``project_root`` can be replaced in tests or deployments while all derived
    paths remain anchored to it.
    """

    project_root: Path = PROJECT_ROOT
    language: str = "it"
    name: str = "emilia"
    listen_timeout: float = 6.5
    log_level: int = logging.INFO
    log_format: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    log_file_name: str | None = "app.log"
    ollama_host: str = "http://localhost:11434"
    think_model: str = "qwen3:0.6b"
    top_k: int = 4
    embedding_model: str = "mxbai-embed-large"

    def __post_init__(self) -> None:
        root = Path(self.project_root).expanduser().resolve()
        language = self.language.strip().lower()
        if language not in _profile_paths(root):
            supported = ", ".join(sorted(_profile_paths(root)))
            raise ConfigurationError(
                f"Unsupported language {self.language!r}; expected one of: {supported}"
            )
        if self.listen_timeout <= 0:
            raise ConfigurationError("listen_timeout must be greater than zero")
        if self.top_k < 1:
            raise ConfigurationError("top_k must be at least one")

        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "ollama_host", normalize_ollama_host(self.ollama_host))

    @classmethod
    def from_env(cls, project_root: Path = PROJECT_ROOT) -> Settings:
        """Build settings from the small set of supported deployment overrides."""

        return cls(
            project_root=project_root,
            language=os.getenv("HELIOS_LANGUAGE", "it"),
            ollama_host=os.getenv("HELIOS_OLLAMA_HOST", "http://localhost:11434"),
        )

    @property
    def profile(self) -> LanguageProfile:
        return _profile_paths(self.project_root)[self.language]

    @property
    def log_file(self) -> Path | None:
        return self.project_root / self.log_file_name if self.log_file_name else None

    @property
    def upload_folder(self) -> Path:
        return self.project_root / "uploads"

    @property
    def tts_folder(self) -> Path:
        return self.project_root / "tts_audio"

    @property
    def wake_sound(self) -> Path:
        return self.project_root / "sounds/wake_up.wav"

    @property
    def stop_sound(self) -> Path:
        return self.project_root / "sounds/stop.wav"

    @property
    def embeddings_file(self) -> Path:
        return self.project_root / "embeddings.npz"

    @property
    def sentence_transformer_model(self) -> Path:
        return self.project_root / "models/all-MiniLM-L6-v2"

    @property
    def qa_json_path(self) -> Path:
        return self.project_root / "document/q&a/IT-WSC_25.json"


SETTINGS = Settings.from_env()
PROFILE = SETTINGS.profile

# Backward-compatible constants.
LOG_LEVEL = SETTINGS.log_level
LOG_FORMAT = SETTINGS.log_format
LOG_FILE = str(SETTINGS.log_file) if SETTINGS.log_file else None

NAME = SETTINGS.name
LANGUAGE = SETTINGS.language
TTS_FOLDER = str(SETTINGS.tts_folder)
TTS_MODEL = str(PROFILE.tts_model)

WAKE_WORD = PROFILE.wake_word
WAKE_WORD_ALIASES = PROFILE.wake_word_aliases
RAG_WORD = PROFILE.rag_word
LISTEN_TIMEOUT = SETTINGS.listen_timeout
WAKE_SOUND = str(SETTINGS.wake_sound)
STOP_SOUND = str(SETTINGS.stop_sound)
TIMEOUT_SOUND = STOP_SOUND

UPLOAD_FOLDER = str(SETTINGS.upload_folder)

VOSK_MODEL_PATH = str(PROFILE.vosk_model)
VOICE = PROFILE.voice
MODEL_TALK = PROFILE.talk_model
MODEL_THINK = SETTINGS.think_model

PRES_Q_1, PRES_Q_2, PRES_Q_3 = PROFILE.presentation_questions
PRES_A_1, PRES_A_2, PRES_A_3 = PROFILE.presentation_answers
PRES_A_SWITCH = {1: PRES_A_1, 2: PRES_A_2, 3: PRES_A_3}

OLLAMA_HOST = SETTINGS.ollama_host
# Legacy callers may still pass the generate endpoint; APIClient normalizes it.
OLLAMA_API_URL = f"{OLLAMA_HOST}/api/generate"
QA_JSON_PATH = str(SETTINGS.qa_json_path)
TOP_K = SETTINGS.top_k
EMBEDDING_MODEL = SETTINGS.embedding_model
