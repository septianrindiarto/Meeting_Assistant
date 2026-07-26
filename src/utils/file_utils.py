"""
Meeting Scribe — File Utilities
Path helpers, safe file operations, and OS-specific paths.
"""
import os
import sys
import json
import shutil
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Application name used for directory naming
APP_NAME = "MeetingScribe"


def get_app_data_dir() -> Path:
    """
    Return the application data directory.
    On Windows: %APPDATA%/MeetingScribe
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.expanduser("~/.config")

    app_dir = Path(base) / APP_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_models_dir() -> Path:
    """Return the directory where AI models are stored."""
    models_dir = get_app_data_dir() / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir


def get_default_project_dir() -> Path:
    """Return the default directory for storing meeting bundles.

    Defaults to a 'meetings' folder next to the application source so that
    bundles, companion transcripts and document requests all live inside the
    project folder — which is also what an AI assistant (Cowork/Claude) is
    given access to. Falls back to Documents if the app dir isn't writable.
    """
    app_root = Path(__file__).parent.parent.parent  # <project>/src/utils -> <project>
    meetings = app_root / "meetings"
    try:
        meetings.mkdir(parents=True, exist_ok=True)
        return meetings
    except OSError:
        docs = Path.home() / "Documents" / APP_NAME
        docs.mkdir(parents=True, exist_ok=True)
        return docs


def get_templates_dir() -> Path:
    """
    Return the directory containing document templates.
    First checks the app's installed templates directory,
    then the user's custom templates directory.
    """
    # Bundled templates (next to the source code)
    bundled = Path(__file__).parent.parent.parent / "templates"
    if bundled.exists():
        return bundled

    # User templates directory
    user_templates = get_app_data_dir() / "templates"
    user_templates.mkdir(parents=True, exist_ok=True)
    return user_templates


def get_temp_dir() -> Path:
    """Return a temporary directory for in-progress recordings."""
    temp = get_app_data_dir() / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    return temp


def get_recording_temp_dir(session_id: str) -> Path:
    """
    Return a temporary directory for a specific recording session.
    Audio chunks are written here during recording.
    """
    rec_dir = get_temp_dir() / f"session_{session_id}"
    rec_dir.mkdir(parents=True, exist_ok=True)
    return rec_dir


def safe_write_json(filepath: str, data: dict, indent: int = 2) -> None:
    """
    Atomically write JSON to a file (write to temp, then rename).
    Prevents corruption if the app crashes during write.
    """
    temp_path = filepath + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        # Atomic rename on same filesystem
        if os.path.exists(filepath):
            os.replace(temp_path, filepath)
        else:
            os.rename(temp_path, filepath)
    except Exception as e:
        logger.error(f"Failed to write JSON to {filepath}: {e}")
        # Clean up temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def safe_read_json(filepath: str) -> Optional[dict]:
    """
    Read a JSON file safely, returning None if the file doesn't exist
    or is corrupted.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Corrupted JSON file {filepath}: {e}")
        return None


def cleanup_temp_session(session_id: str) -> None:
    """Remove temporary files for a completed recording session."""
    rec_dir = get_temp_dir() / f"session_{session_id}"
    if rec_dir.exists():
        shutil.rmtree(rec_dir, ignore_errors=True)
        logger.debug(f"Cleaned up temp session: {session_id}")


def ensure_dir(path: str) -> str:
    """Ensure a directory exists and return the path."""
    os.makedirs(path, exist_ok=True)
    return path


def unique_filename(directory: str, base_name: str, extension: str) -> str:
    """
    Generate a unique filename in a directory by appending a number
    if the file already exists.

    Args:
        directory: Target directory.
        base_name: Base filename without extension.
        extension: File extension including dot (e.g. ".docx").

    Returns:
        Full path to a unique filename.
    """
    filepath = os.path.join(directory, f"{base_name}{extension}")
    if not os.path.exists(filepath):
        return filepath

    counter = 1
    while True:
        filepath = os.path.join(directory, f"{base_name}_{counter}{extension}")
        if not os.path.exists(filepath):
            return filepath
        counter += 1
