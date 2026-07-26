"""
Meeting Scribe — Application Settings
JSON-backed persistent settings with sensible defaults.
"""
from __future__ import annotations

import os
import logging
from typing import Any, Optional
from pathlib import Path

from src.core.models import LLMBackend
from src.utils.file_utils import get_app_data_dir, get_default_project_dir, safe_write_json, safe_read_json

logger = logging.getLogger(__name__)

# ─── Default Settings ────────────────────────────────────────────────

DEFAULTS = {
    # General
    "project_folder": str(get_default_project_dir()),
    "theme": "dark",
    "language": "en",

    # Audio
    "audio_source": "both",  # mic, system, both
    "mic_device_index": None,
    "system_device_index": None,

    # Transcription
    "whisper_model": "auto",  # auto, tiny, base, small, medium, large-v3
    "whisper_quality": "balanced",  # fast, balanced, accurate, best — used when whisper_model='auto'
    "transcription_language": None,  # None = auto-detect

    # Transcription backend
    "stt_backend": "local",   # local | groq
    "groq_api_key": "",
    "groq_model": "whisper-large-v3-turbo",
    # If the cloud backend fails, automatically roll back to local Whisper
    "cloud_stt_fallback_local": True,

    # Real-time transcription (during recording)
    "live_transcription": True,
    # "small" is the minimum model with usable Malay/Indonesian accuracy.
    # tiny/base are English-centric and hallucinate badly on Bahasa speech.
    "live_model": "small",

    # Diarization
    "hf_token": "",  # Hugging Face access token
    "diarization_enabled": False,
    "max_speakers": 10,

    # LLM
    "llm_backend": "none",  # none, ollama, openai, anthropic
    "llm_model": "llama3.1:8b",
    "llm_api_key": "",
    "ollama_base_url": "http://localhost:11434",

    # Privacy
    "allow_cloud_llm": False,

    # Storage housekeeping
    "auto_cleanup_temp": True,      # purge orphaned temp files on startup
    "temp_retention_hours": 24,     # how long abandoned temp files survive
    "cleanup_after_save": True,     # delete working audio once bundled

    # Redaction
    "redaction_enabled": False,
    "redaction_patterns": [],  # list of regex patterns

    # UI
    "show_recording_bar": True,
    "hide_from_screenshare": False,
    "window_geometry": None,  # saved window size/position
}


class Settings:
    """
    Persistent application settings backed by a JSON file.

    Usage:
        settings = Settings.instance()
        project_folder = settings.get("project_folder")
        settings.set("whisper_model", "medium")
        settings.save()
    """

    _instance: Optional["Settings"] = None

    def __init__(self, settings_path: Optional[str] = None):
        if settings_path is None:
            settings_path = str(get_app_data_dir() / "settings.json")

        self._path = settings_path
        self._data: dict = dict(DEFAULTS)

        # Load existing settings
        self._load()

    @classmethod
    def instance(cls) -> "Settings":
        """Get or create the singleton Settings instance."""
        if cls._instance is None:
            cls._instance = Settings()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        cls._instance = None

    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value."""
        if key in self._data:
            return self._data[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def set(self, key: str, value: Any) -> None:
        """Set a setting value (call save() to persist)."""
        self._data[key] = value

    def get_llm_backend(self) -> LLMBackend:
        """Get the configured LLM backend as an enum."""
        backend_str = self.get("llm_backend", "none")
        try:
            return LLMBackend(backend_str)
        except ValueError:
            return LLMBackend.NONE

    def get_project_folder(self) -> str:
        """Get the project folder, ensuring it exists."""
        folder = self.get("project_folder")
        os.makedirs(folder, exist_ok=True)
        return folder

    def save(self) -> None:
        """Persist settings to disk."""
        safe_write_json(self._path, self._data)
        logger.debug(f"Settings saved to {self._path}")

    def _load(self) -> None:
        """Load settings from disk, merging with defaults."""
        existing = safe_read_json(self._path)
        if existing:
            # Merge: existing values override defaults
            for key, value in existing.items():
                self._data[key] = value
            logger.debug(f"Settings loaded from {self._path}")
        else:
            logger.info("No existing settings found, using defaults")

    def to_dict(self) -> dict:
        """Return all settings as a dictionary."""
        return dict(self._data)

    def reset_to_defaults(self) -> None:
        """Reset all settings to defaults."""
        self._data = dict(DEFAULTS)
        self.save()
