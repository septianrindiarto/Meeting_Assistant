"""
Meeting Scribe — Project Bundle Manager
Handles .mscribe bundles: ZIP archives containing audio, transcript,
structured data, generated documents, and metadata.
"""
from __future__ import annotations

import os
import json
import shutil
import zipfile
import logging
import time
import uuid
import socket
from pathlib import Path
from typing import Optional, List

from src.core.models import Meeting, MeetingMetadata, TranscriptSegment, StructuredMeeting
from src.utils.file_utils import safe_write_json, safe_read_json, get_temp_dir
from src.utils.audio_utils import concatenate_chunks, convert_to_opus

logger = logging.getLogger(__name__)

BUNDLE_EXTENSION = ".mscribe"
BUNDLE_VERSION = "1.0"


class BundleManager:
    """
    Manages .mscribe project bundles — self-contained ZIP archives
    that store everything about a meeting.

    Bundle layout:
        audio.opus          — compressed source audio
        transcript.json     — speaker-labelled timestamped utterances
        structured.json     — decisions, action items, timeline, summary
        documents/          — generated .docx and .pdf files
        meta.json           — title, date, duration, app version
        lock.json           — session lock (present during active recording)

    Usage:
        manager = BundleManager()
        path = manager.create_bundle(meeting, output_dir="/path/to/projects")
        meeting = manager.open_bundle(path)
        manager.update_bundle(path, meeting)
    """

    def create_bundle(self, meeting: Meeting, output_dir: str,
                      filename: Optional[str] = None) -> str:
        """
        Create a new .mscribe bundle from a Meeting object.

        Args:
            meeting: Meeting with audio, transcript, and optionally structured data.
            output_dir: Directory to save the bundle.
            filename: Optional bundle filename (without extension).

        Returns:
            Path to the created .mscribe file.
        """
        if filename is None:
            date_str = meeting.metadata.date or time.strftime("%Y-%m-%d")
            title_slug = meeting.metadata.title.replace(" ", "_")[:40]
            filename = f"{date_str}_{title_slug}"

        # Sanitize filename
        filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
        bundle_path = os.path.join(output_dir, f"{filename}{BUNDLE_EXTENSION}")

        # Ensure unique name
        counter = 1
        while os.path.exists(bundle_path):
            bundle_path = os.path.join(output_dir, f"{filename}_{counter}{BUNDLE_EXTENSION}")
            counter += 1

        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"Creating bundle: {bundle_path}")

        # Prepare audio: concatenate chunks and compress
        audio_file = None
        if meeting.chunk_paths:
            temp_dir = str(get_temp_dir())
            combined_wav = os.path.join(temp_dir, "combined_audio.wav")
            concatenate_chunks(meeting.chunk_paths, combined_wav)

            # Try to compress to Opus
            opus_file = os.path.join(temp_dir, "audio.opus")
            opus_result = convert_to_opus(combined_wav, opus_file)
            audio_file = opus_result or combined_wav
        elif meeting.audio_path and os.path.exists(meeting.audio_path):
            audio_file = meeting.audio_path

        # Build the ZIP
        with zipfile.ZipFile(bundle_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Audio
            if audio_file and os.path.exists(audio_file):
                audio_name = "audio.opus" if audio_file.endswith(".opus") else "audio.wav"
                zf.write(audio_file, audio_name)

            # Transcript
            if meeting.transcript:
                transcript_json = json.dumps(
                    [s.to_dict() for s in meeting.transcript],
                    indent=2, ensure_ascii=False
                )
                zf.writestr("transcript.json", transcript_json)

            # Structured data
            if meeting.structured:
                zf.writestr("structured.json", meeting.structured.to_json())

            # Metadata
            meta = meeting.metadata.to_dict()
            meta["bundle_version"] = BUNDLE_VERSION
            meta["attendees"] = meeting.metadata.attendees or meeting.speaker_list
            zf.writestr("meta.json", json.dumps(meta, indent=2, ensure_ascii=False))

        meeting.bundle_path = bundle_path
        file_size = os.path.getsize(bundle_path)
        logger.info(f"Bundle created: {bundle_path} ({file_size / 1024 / 1024:.1f} MB)")

        return bundle_path

    def open_bundle(self, bundle_path: str) -> Meeting:
        """
        Open a .mscribe bundle and reconstruct a Meeting object.

        Args:
            bundle_path: Path to the .mscribe file.

        Returns:
            Meeting object with all data from the bundle.

        Raises:
            FileNotFoundError: If bundle doesn't exist.
            ValueError: If bundle is corrupted or invalid.
        """
        if not os.path.exists(bundle_path):
            raise FileNotFoundError(f"Bundle not found: {bundle_path}")

        logger.info(f"Opening bundle: {bundle_path}")

        # Check for session lock
        self._check_lock(bundle_path)

        with zipfile.ZipFile(bundle_path, 'r') as zf:
            names = zf.namelist()

            # Read metadata
            metadata = MeetingMetadata()
            if "meta.json" in names:
                meta_data = json.loads(zf.read("meta.json").decode('utf-8'))
                metadata = MeetingMetadata.from_dict(meta_data)

            # Read transcript
            transcript = []
            if "transcript.json" in names:
                transcript_data = json.loads(zf.read("transcript.json").decode('utf-8'))
                transcript = [
                    TranscriptSegment(**seg) for seg in transcript_data
                ]

            # Read structured data
            structured = None
            if "structured.json" in names:
                structured_data = json.loads(zf.read("structured.json").decode('utf-8'))
                structured = StructuredMeeting.from_dict(structured_data)

            # Extract audio to temp directory for playback
            audio_path = ""
            audio_files = [n for n in names if n.startswith("audio.")]
            if audio_files:
                temp_dir = str(get_temp_dir())
                audio_name = audio_files[0]
                audio_path = os.path.join(temp_dir, audio_name)
                with zf.open(audio_name) as src, open(audio_path, 'wb') as dst:
                    dst.write(src.read())

            # List generated documents
            doc_files = [n for n in names if n.startswith("documents/")]

        meeting = Meeting(
            metadata=metadata,
            transcript=transcript,
            structured=structured,
            audio_path=audio_path,
            bundle_path=bundle_path,
        )

        logger.info(
            f"Bundle opened: {metadata.title}, "
            f"{len(transcript)} segments, "
            f"{len(metadata.attendees)} attendees"
        )

        return meeting

    def update_bundle(self, bundle_path: str, meeting: Meeting) -> str:
        """
        Update an existing bundle with new data.
        Uses atomic write: creates a temp file, then replaces.

        Args:
            bundle_path: Path to the existing .mscribe file.
            meeting: Updated Meeting object.

        Returns:
            The bundle path.
        """
        temp_path = bundle_path + ".tmp"
        output_dir = os.path.dirname(bundle_path)
        filename = os.path.splitext(os.path.basename(bundle_path))[0]

        # Create new bundle at temp location
        with zipfile.ZipFile(bundle_path, 'r') as old_zf:
            with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as new_zf:
                # Copy audio from old bundle (unchanged)
                for name in old_zf.namelist():
                    if name.startswith("audio."):
                        new_zf.writestr(name, old_zf.read(name))

                # Write updated transcript
                if meeting.transcript:
                    transcript_json = json.dumps(
                        [s.to_dict() for s in meeting.transcript],
                        indent=2, ensure_ascii=False
                    )
                    new_zf.writestr("transcript.json", transcript_json)

                # Write updated structured data
                if meeting.structured:
                    new_zf.writestr("structured.json", meeting.structured.to_json())

                # Write updated metadata
                meta = meeting.metadata.to_dict()
                meta["bundle_version"] = BUNDLE_VERSION
                new_zf.writestr("meta.json", json.dumps(meta, indent=2, ensure_ascii=False))

                # Copy documents from old bundle
                for name in old_zf.namelist():
                    if name.startswith("documents/"):
                        new_zf.writestr(name, old_zf.read(name))

        # Atomic replace
        os.replace(temp_path, bundle_path)
        logger.info(f"Bundle updated: {bundle_path}")

        return bundle_path

    def add_document_to_bundle(self, bundle_path: str,
                                doc_path: str, doc_name: str) -> None:
        """
        Add a generated document to an existing bundle.

        Args:
            bundle_path: Path to the .mscribe file.
            doc_path: Path to the document file to add.
            doc_name: Filename inside the bundle (e.g., "minutes.docx").
        """
        temp_path = bundle_path + ".tmp"

        with zipfile.ZipFile(bundle_path, 'r') as old_zf:
            with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as new_zf:
                # Copy existing entries (except same-name doc)
                target_name = f"documents/{doc_name}"
                for name in old_zf.namelist():
                    if name != target_name:
                        new_zf.writestr(name, old_zf.read(name))

                # Add the new document
                new_zf.write(doc_path, target_name)

        os.replace(temp_path, bundle_path)
        logger.info(f"Added document to bundle: {doc_name}")

    def extract_documents(self, bundle_path: str, output_dir: str) -> List[str]:
        """
        Extract all generated documents from a bundle.

        Args:
            bundle_path: Path to .mscribe file.
            output_dir: Directory to extract documents to.

        Returns:
            List of extracted file paths.
        """
        extracted = []
        os.makedirs(output_dir, exist_ok=True)

        with zipfile.ZipFile(bundle_path, 'r') as zf:
            for name in zf.namelist():
                if name.startswith("documents/") and len(name) > len("documents/"):
                    doc_filename = name.replace("documents/", "")
                    output_path = os.path.join(output_dir, doc_filename)
                    with zf.open(name) as src, open(output_path, 'wb') as dst:
                        dst.write(src.read())
                    extracted.append(output_path)

        return extracted

    def acquire_lock(self, bundle_path: str) -> None:
        """Place a session lock on a bundle to prevent concurrent edits."""
        lock_data = {
            "machine": socket.gethostname(),
            "machine_id": str(uuid.getnode()),
            "timestamp": time.time(),
            "pid": os.getpid(),
        }

        # We store the lock outside the ZIP for speed
        lock_path = bundle_path + ".lock"
        safe_write_json(lock_path, lock_data)
        logger.debug(f"Lock acquired: {lock_path}")

    def release_lock(self, bundle_path: str) -> None:
        """Release the session lock on a bundle."""
        lock_path = bundle_path + ".lock"
        if os.path.exists(lock_path):
            os.remove(lock_path)
            logger.debug(f"Lock released: {lock_path}")

    def _check_lock(self, bundle_path: str) -> None:
        """Check if a bundle is locked by another session."""
        lock_path = bundle_path + ".lock"
        if os.path.exists(lock_path):
            lock_data = safe_read_json(lock_path)
            if lock_data:
                machine = lock_data.get("machine", "unknown")
                ts = lock_data.get("timestamp", 0)
                age_hours = (time.time() - ts) / 3600

                if age_hours < 24:
                    logger.warning(
                        f"Bundle is locked by {machine} "
                        f"({age_hours:.1f} hours ago). "
                        "Another instance may be editing this meeting."
                    )
                else:
                    # Stale lock — auto-release
                    logger.info(f"Releasing stale lock ({age_hours:.0f}h old)")
                    self.release_lock(bundle_path)

    @staticmethod
    def list_bundles(directory: str) -> List[dict]:
        """
        Scan a directory for .mscribe bundles and return their metadata.

        Args:
            directory: Directory to scan.

        Returns:
            List of metadata dicts (title, date, duration, path).
        """
        bundles = []
        if not os.path.isdir(directory):
            return bundles

        for filename in os.listdir(directory):
            if filename.endswith(BUNDLE_EXTENSION):
                filepath = os.path.join(directory, filename)
                try:
                    with zipfile.ZipFile(filepath, 'r') as zf:
                        if "meta.json" in zf.namelist():
                            meta = json.loads(zf.read("meta.json").decode('utf-8'))
                            meta["bundle_path"] = filepath
                            meta["file_size_mb"] = os.path.getsize(filepath) / 1024 / 1024
                            bundles.append(meta)
                except (zipfile.BadZipFile, json.JSONDecodeError) as e:
                    logger.warning(f"Could not read bundle {filename}: {e}")

        # Sort by date, newest first
        bundles.sort(key=lambda b: b.get("date", ""), reverse=True)
        return bundles
