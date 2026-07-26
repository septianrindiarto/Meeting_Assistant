"""
Meeting Scribe — Media File Import
Decodes audio from any media file (mp3, mp4, m4a, wav, opus, mkv, webm, ...)
directly to 16kHz mono PCM for transcription.

Why there is NO mp4 → mp3 conversion step:
    MP3 is a lossy codec. Re-encoding mp4 audio to mp3 would (a) take extra
    time and (b) DEGRADE the audio Whisper sees, hurting accuracy. Decoding
    the source's audio stream directly to raw PCM is strictly better on both
    speed and accuracy — so that is what this module does, for every format.

Primary decoder: PyAV (bundled with faster-whisper, no external install).
Fallback: ffmpeg CLI if PyAV can't open the file.
"""
from __future__ import annotations

import os
import logging
import subprocess
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000

SUPPORTED_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".wav", ".opus", ".ogg", ".flac",
    ".aac", ".wma", ".mkv", ".webm", ".mov", ".avi",
}


def is_supported_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def decode_media_to_wav(media_path: str, output_wav: str) -> Tuple[str, float]:
    """
    Decode any media file's audio stream to a 16kHz mono WAV.

    Args:
        media_path: Path to the source media file (mp3/mp4/...).
        output_wav: Path for the decoded WAV file.

    Returns:
        (output_wav_path, duration_seconds)

    Raises:
        ValueError: If the file has no audio stream or can't be decoded.
    """
    if not os.path.exists(media_path):
        raise FileNotFoundError(f"Media file not found: {media_path}")

    audio = _decode_with_pyav(media_path)
    if audio is None:
        audio = _decode_with_ffmpeg(media_path)
    if audio is None or len(audio) == 0:
        raise ValueError(
            f"Could not decode audio from '{os.path.basename(media_path)}'. "
            "The file may have no audio track or use an unsupported codec."
        )

    from src.utils.audio_utils import save_wav_chunk
    save_wav_chunk(audio, output_wav, TARGET_SAMPLE_RATE)

    duration = len(audio) / TARGET_SAMPLE_RATE
    logger.info(
        f"Decoded '{os.path.basename(media_path)}' → "
        f"{duration:.1f}s of 16kHz mono audio"
    )
    return output_wav, duration


def _decode_with_pyav(media_path: str) -> Optional[np.ndarray]:
    """Decode using PyAV (ships with faster-whisper). Returns float32 mono
    16kHz array, or None if PyAV can't handle the file."""
    try:
        import av
        from av.audio.resampler import AudioResampler
    except ImportError:
        logger.warning("PyAV not available — falling back to ffmpeg CLI")
        return None

    try:
        container = av.open(media_path)
        audio_stream = next(
            (s for s in container.streams if s.type == "audio"), None
        )
        if audio_stream is None:
            logger.warning(f"No audio stream in {media_path}")
            container.close()
            return None

        resampler = AudioResampler(
            format="s16", layout="mono", rate=TARGET_SAMPLE_RATE
        )

        pieces = []
        for frame in container.decode(audio_stream):
            for rframe in resampler.resample(frame):
                arr = rframe.to_ndarray()  # (1, n) int16
                pieces.append(arr.flatten())

        # Flush the resampler's internal buffer
        for rframe in resampler.resample(None):
            arr = rframe.to_ndarray()
            pieces.append(arr.flatten())

        container.close()

        if not pieces:
            return None

        pcm = np.concatenate(pieces).astype(np.float32) / 32768.0
        return pcm

    except Exception as e:
        logger.warning(f"PyAV decode failed for {media_path}: {e}")
        return None


def _decode_with_ffmpeg(media_path: str) -> Optional[np.ndarray]:
    """Fallback: decode via ffmpeg CLI piping raw PCM to stdout."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", media_path,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
                "-",
            ],
            capture_output=True,
            timeout=600,
        )
        if result.returncode != 0 or not result.stdout:
            logger.warning(
                f"ffmpeg decode failed: {result.stderr.decode(errors='ignore')[:200]}"
            )
            return None
        pcm = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm
    except FileNotFoundError:
        logger.warning("ffmpeg not found on PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg decode timed out")
        return None
