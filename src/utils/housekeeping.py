"""
Meeting Scribe — Temporary Storage Housekeeping
Keeps the app's working directory from growing without bound.

Why this exists:
    Every imported mp4/mp3 is decoded to a 16kHz mono WAV (~115 MB per audio
    hour), every recording writes 30-second chunk WAVs, and transcription
    writes a normalized copy. Without cleanup, a handful of long meetings
    silently consumes several GB. Cancelled or crashed jobs leave the largest
    orphans because their normal cleanup path never runs.

Strategy:
    1. On app start  — purge anything older than the retention window, plus
                       orphaned session folders and stray temp audio.
    2. After a bundle is saved — the audio now lives inside the .mscribe, so
                       that meeting's working files are deleted immediately.
    3. On cancel / error — the specific partial file is removed.
    4. Manual  — Settings shows usage and offers a "Clean Now" button.

Files inside the user's project folder (.mscribe bundles, transcripts,
generated documents) are NEVER touched — only the app's temp area.
"""
from __future__ import annotations

import os
import time
import shutil
import logging
from pathlib import Path
from typing import Dict, List, Optional

from src.utils.file_utils import get_temp_dir, get_app_data_dir

logger = logging.getLogger(__name__)

# Files older than this are considered abandoned (hours)
DEFAULT_RETENTION_HOURS = 24
# Groq resume manifests are tiny but shouldn't live forever (days)
JOB_RETENTION_DAYS = 7


def _dir_size(path: Path) -> int:
    """Total bytes used by a directory tree (best effort)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def get_storage_usage() -> Dict[str, float]:
    """Return a breakdown of temp storage usage in megabytes."""
    temp = get_temp_dir()
    jobs = get_app_data_dir() / "cloud_jobs"
    models = get_app_data_dir() / "models"

    session_bytes = 0
    import_bytes = 0
    other_bytes = 0

    try:
        for entry in temp.iterdir():
            if entry.is_dir():
                if entry.name.startswith("session_"):
                    session_bytes += _dir_size(entry)
                else:
                    other_bytes += _dir_size(entry)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                if entry.name.startswith("import_"):
                    import_bytes += size
                else:
                    other_bytes += size
    except OSError:
        pass

    mb = 1024 * 1024
    return {
        "recordings_mb": session_bytes / mb,
        "imports_mb": import_bytes / mb,
        "other_temp_mb": other_bytes / mb,
        "temp_total_mb": (session_bytes + import_bytes + other_bytes) / mb,
        "cloud_jobs_mb": _dir_size(jobs) / mb,
        "models_mb": _dir_size(models) / mb,
    }


def cleanup_temp(retention_hours: int = DEFAULT_RETENTION_HOURS,
                 active_session_id: Optional[str] = None,
                 aggressive: bool = False) -> Dict[str, float]:
    """
    Remove abandoned temporary files.

    Args:
        retention_hours: Delete items whose mtime is older than this.
        active_session_id: A recording session to preserve (in progress).
        aggressive: If True, ignore age and remove everything except the
                    active session (used by the manual "Clean Now" button).

    Returns:
        {"files_removed": n, "freed_mb": x}
    """
    temp = get_temp_dir()
    cutoff = time.time() - (retention_hours * 3600)
    removed = 0
    freed = 0

    if not temp.exists():
        return {"files_removed": 0, "freed_mb": 0.0}

    for entry in list(temp.iterdir()):
        # Never delete the session currently being recorded
        if active_session_id and entry.name == f"session_{active_session_id}":
            continue

        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        if not aggressive and mtime > cutoff:
            continue

        try:
            if entry.is_dir():
                size = _dir_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
                freed += size
                removed += 1
            else:
                size = entry.stat().st_size
                entry.unlink()
                freed += size
                removed += 1
        except OSError as e:
            logger.debug(f"Could not remove {entry}: {e}")

    freed_mb = freed / (1024 * 1024)
    if removed:
        logger.info(
            f"Housekeeping: removed {removed} temp item(s), freed {freed_mb:.1f} MB"
        )
    return {"files_removed": removed, "freed_mb": freed_mb}


def cleanup_after_save(session_id: Optional[str] = None,
                       audio_paths: Optional[List[str]] = None) -> float:
    """
    Called after a bundle is saved successfully — the audio is now stored
    inside the .mscribe, so the working copies are redundant.

    Args:
        session_id: Recording session whose chunk folder can go.
        audio_paths: Specific working files to delete (decoded imports,
                     concatenated recordings, normalized copies).

    Returns:
        Megabytes freed.
    """
    freed = 0

    if session_id:
        session_dir = get_temp_dir() / f"session_{session_id}"
        if session_dir.exists():
            freed += _dir_size(session_dir)
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(f"Removed recording chunks for session {session_id}")

    for path in (audio_paths or []):
        if not path:
            continue
        # Safety: only ever delete inside our own temp directory
        try:
            if not str(Path(path).resolve()).startswith(str(get_temp_dir().resolve())):
                continue
        except OSError:
            continue

        for candidate in (path, os.path.splitext(path)[0] + "_normalized.wav"):
            try:
                if os.path.exists(candidate):
                    freed += os.path.getsize(candidate)
                    os.remove(candidate)
                    logger.debug(f"Removed working file: {candidate}")
            except OSError:
                pass

    freed_mb = freed / (1024 * 1024)
    if freed_mb > 0.1:
        logger.info(f"Housekeeping after save: freed {freed_mb:.1f} MB")
    return freed_mb


def remove_working_file(path: Optional[str]) -> None:
    """Delete a single temp working file (used on cancel / error paths)."""
    if not path:
        return
    try:
        if not str(Path(path).resolve()).startswith(str(get_temp_dir().resolve())):
            return
    except OSError:
        return

    for candidate in (path, os.path.splitext(path)[0] + "_normalized.wav"):
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
                logger.info(f"Removed abandoned working file: {candidate}")
        except OSError:
            pass


def cleanup_cloud_jobs(retention_days: int = JOB_RETENTION_DAYS) -> int:
    """Remove Groq resume manifests older than retention_days.
    Recent ones are preserved so interrupted jobs can still resume."""
    jobs_dir = get_app_data_dir() / "cloud_jobs"
    if not jobs_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for entry in list(jobs_dir.glob("groq_job_*.json")):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(f"Removed {removed} stale cloud job manifest(s)")
    return removed


def startup_cleanup(retention_hours: int = DEFAULT_RETENTION_HOURS) -> Dict[str, float]:
    """Run at application start: clear orphans left by crashes or cancels."""
    logger.info("Running startup housekeeping...")
    result = cleanup_temp(retention_hours=retention_hours)
    cleanup_cloud_jobs()
    return result
