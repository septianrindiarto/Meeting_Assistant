"""
Meeting Scribe — Voice Activity Detection (VAD)
Uses silero-vad (PyTorch) to detect speech segments in audio.
Filters out silence to save transcription compute.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np

from src.core.models import AudioSegment

logger = logging.getLogger(__name__)

# silero-vad expects 16kHz mono audio
SAMPLE_RATE = 16000


class VoiceActivityDetector:
    """
    Wrapper around silero-vad for detecting speech regions in audio.

    Usage:
        vad = VoiceActivityDetector()
        segments = vad.process(audio_float32_16k)
        # segments is a list of AudioSegment with start/end times
    """

    def __init__(self, threshold: float = 0.5,
                 min_speech_duration_ms: int = 250,
                 min_silence_duration_ms: int = 300,
                 window_size_samples: int = 512):
        """
        Args:
            threshold: Speech probability threshold (0.0 to 1.0).
                       Higher = more aggressive filtering.
            min_speech_duration_ms: Minimum speech segment duration.
            min_silence_duration_ms: Minimum silence to split segments.
            window_size_samples: VAD window size (512 for 16kHz).
        """
        self.threshold = threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.min_silence_duration_ms = min_silence_duration_ms
        self.window_size_samples = window_size_samples

        self._model = None
        self._utils = None
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Lazy-load the silero-vad model on first use."""
        if self._loaded:
            return

        logger.info("Loading silero-vad model...")
        try:
            model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                onnx=False,
                trust_repo=True
            )
            self._model = model
            self._utils = utils
            self._loaded = True
            logger.info("silero-vad model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load silero-vad: {e}")
            raise

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> List[AudioSegment]:
        """
        Detect speech segments in audio.

        Args:
            audio: 1D numpy array of float32 audio samples.
            sample_rate: Sample rate of the audio (must be 16000).

        Returns:
            List of AudioSegment objects marking speech regions.
        """
        self._ensure_loaded()

        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"silero-vad expects {SAMPLE_RATE}Hz audio, got {sample_rate}Hz")

        # Convert to torch tensor
        audio_tensor = torch.from_numpy(audio).float()

        # Get speech timestamps using silero utility
        get_speech_timestamps = self._utils[0]

        speech_timestamps = get_speech_timestamps(
            audio_tensor,
            self._model,
            threshold=self.threshold,
            sampling_rate=SAMPLE_RATE,
            min_speech_duration_ms=self.min_speech_duration_ms,
            min_silence_duration_ms=self.min_silence_duration_ms,
            window_size_samples=self.window_size_samples,
            return_seconds=False,  # get sample indices
        )

        segments = []
        for ts in speech_timestamps:
            start_sec = ts['start'] / SAMPLE_RATE
            end_sec = ts['end'] / SAMPLE_RATE

            # Extract the audio data for this segment
            start_sample = ts['start']
            end_sample = min(ts['end'], len(audio))
            segment_audio = audio[start_sample:end_sample]

            segments.append(AudioSegment(
                start=start_sec,
                end=end_sec,
                audio_data=segment_audio.tobytes()
            ))

        logger.debug(
            f"VAD found {len(segments)} speech segments in "
            f"{len(audio)/SAMPLE_RATE:.1f}s of audio"
        )

        return segments

    def get_speech_ratio(self, audio: np.ndarray) -> float:
        """
        Calculate the ratio of speech to total audio duration.
        Useful for skipping mostly-silent chunks.

        Args:
            audio: 1D numpy array of float32 audio.

        Returns:
            Float between 0.0 (all silence) and 1.0 (all speech).
        """
        segments = self.process(audio)
        if not segments:
            return 0.0

        speech_duration = sum(s.duration for s in segments)
        total_duration = len(audio) / SAMPLE_RATE
        return speech_duration / total_duration if total_duration > 0 else 0.0

    def reset(self) -> None:
        """Reset the VAD model state (for streaming mode)."""
        if self._model is not None:
            self._model.reset_states()
