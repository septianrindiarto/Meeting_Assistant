"""
Meeting Scribe — Speaker Diarization
Uses pyannote.audio to assign speaker labels to transcript segments.
Requires a free Hugging Face token for model download (one-time).
"""
from __future__ import annotations

import os
import logging
import time
from typing import List, Dict, Optional, Tuple

import numpy as np

from src.core.models import TranscriptSegment

logger = logging.getLogger(__name__)


class SpeakerSegment:
    """A time range assigned to a specific speaker."""
    def __init__(self, start: float, end: float, speaker: str):
        self.start = start
        self.end = end
        self.speaker = speaker

    @property
    def duration(self) -> float:
        return self.end - self.start

    def __repr__(self):
        return f"SpeakerSegment({self.start:.1f}-{self.end:.1f}, {self.speaker})"


class SpeakerDiarizer:
    """
    Speaker diarization using pyannote.audio.

    This module identifies WHO is speaking at each moment in the audio.
    It requires a free Hugging Face access token to download the pretrained model.

    Setup:
        1. Create a free account at https://huggingface.co
        2. Accept terms at https://huggingface.co/pyannote/speaker-diarization-3.1
        3. Generate a token at https://huggingface.co/settings/tokens
        4. Paste the token in Meeting Scribe Settings → Diarization

    Usage:
        diarizer = SpeakerDiarizer(hf_token="hf_xxxxx")
        speaker_segments = diarizer.diarize("audio.wav")
        transcript = diarizer.assign_speakers(transcript_segments, speaker_segments)
    """

    def __init__(self, hf_token: Optional[str] = None,
                 num_speakers: Optional[int] = None,
                 min_speakers: int = 1,
                 max_speakers: int = 10):
        """
        Args:
            hf_token: Hugging Face access token. Required for model download.
            num_speakers: Exact number of speakers if known. None = auto-detect.
            min_speakers: Minimum expected speakers.
            max_speakers: Maximum expected speakers.
        """
        self.hf_token = hf_token
        self.num_speakers = num_speakers
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

        self._pipeline = None
        self._loaded = False
        self._speaker_map: Dict[str, str] = {}  # internal label → user label

    @property
    def is_available(self) -> bool:
        """Check if diarization is available (token provided and pyannote installed)."""
        if not self.hf_token:
            return False
        try:
            import pyannote.audio
            return True
        except ImportError:
            return False

    def _ensure_loaded(self) -> None:
        """Lazy-load the pyannote pipeline on first use."""
        if self._loaded:
            return

        if not self.hf_token:
            raise ValueError(
                "Hugging Face token required for speaker diarization. "
                "Get one free at https://huggingface.co/settings/tokens"
            )

        try:
            import torch
            from pyannote.audio import Pipeline

            logger.info("Loading pyannote speaker diarization pipeline...")
            start = time.time()

            self._pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=self.hf_token,
            )

            # Use GPU if available
            if torch.cuda.is_available():
                self._pipeline.to(torch.device("cuda"))
                logger.info("Diarization pipeline moved to GPU")

            elapsed = time.time() - start
            logger.info(f"Diarization pipeline loaded in {elapsed:.1f}s")
            self._loaded = True

        except Exception as e:
            logger.error(f"Failed to load diarization pipeline: {e}")
            raise

    def diarize(self, audio_path: str) -> List[SpeakerSegment]:
        """
        Run speaker diarization on an audio file.

        Args:
            audio_path: Path to WAV audio file (16kHz mono preferred).

        Returns:
            List of SpeakerSegment with start, end, and speaker label.
        """
        self._ensure_loaded()

        logger.info(f"Diarizing: {audio_path}")
        start = time.time()

        # Build diarization kwargs
        kwargs = {}
        if self.num_speakers is not None:
            kwargs["num_speakers"] = self.num_speakers
        else:
            kwargs["min_speakers"] = self.min_speakers
            kwargs["max_speakers"] = self.max_speakers

        diarization = self._pipeline(audio_path, **kwargs)

        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append(SpeakerSegment(
                start=turn.start,
                end=turn.end,
                speaker=speaker,
            ))

        elapsed = time.time() - start
        unique_speakers = set(s.speaker for s in segments)
        logger.info(
            f"Diarization complete: {len(segments)} segments, "
            f"{len(unique_speakers)} speakers, {elapsed:.1f}s"
        )

        return segments

    def assign_speakers(self, transcript: List[TranscriptSegment],
                        speaker_segments: List[SpeakerSegment]) -> List[TranscriptSegment]:
        """
        Merge diarization results with transcription by timestamp overlap.
        Each transcript segment is assigned the speaker with the most overlap.

        Args:
            transcript: List of TranscriptSegment from the transcriber.
            speaker_segments: List of SpeakerSegment from diarization.

        Returns:
            The same transcript list with speaker fields populated.
        """
        for ts in transcript:
            best_speaker = ""
            best_overlap = 0.0

            for ss in speaker_segments:
                # Calculate overlap
                overlap_start = max(ts.start, ss.start)
                overlap_end = min(ts.end, ss.end)
                overlap = max(0, overlap_end - overlap_start)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = ss.speaker

            if best_speaker:
                # Apply user rename if available
                ts.speaker = self._speaker_map.get(best_speaker, best_speaker)

        return transcript

    def rename_speaker(self, internal_label: str, user_label: str) -> None:
        """
        Rename a speaker label (e.g., "SPEAKER_00" → "Sarah").

        Args:
            internal_label: The original pyannote label.
            user_label: The user-friendly name.
        """
        self._speaker_map[internal_label] = user_label
        logger.info(f"Speaker renamed: {internal_label} → {user_label}")

    def get_speaker_map(self) -> Dict[str, str]:
        """Return the current speaker label mappings."""
        return dict(self._speaker_map)

    def set_speaker_map(self, mapping: Dict[str, str]) -> None:
        """Restore speaker label mappings (e.g., from a saved bundle)."""
        self._speaker_map = dict(mapping)
