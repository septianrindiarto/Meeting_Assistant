"""
Meeting Scribe — Local Transcription Engine
Wraps faster-whisper for local, offline speech-to-text.
Supports selectable model sizes with hardware-aware defaults.
"""
from __future__ import annotations

import os
import logging
import time
from typing import List, Optional, Tuple

import numpy as np

from src.core.models import TranscriptSegment
from src.utils.hardware_probe import recommend_whisper_model
from src.utils.file_utils import get_models_dir

logger = logging.getLogger(__name__)

# Default model configuration
DEFAULT_MODEL_SIZE = "small"
SUPPORTED_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]


# ─── Hallucination detection ────────────────────────────────────────────
# Phrase fragments Whisper emits on silent/noisy audio — YouTube-outro style
# junk, in ENGLISH and MALAY/INDONESIAN (both observed in real meetings).
# Matched as substrings: any short segment containing one is dropped.
_ALWAYS_JUNK_FRAGMENTS = (
    # English
    "for watching",
    "please subscribe",
    "subscribe to",
    "like and subscribe",
    "translated by",
    "transcribed by",
    "captions by",
    "subtitles by",
    # Malay / Indonesian
    "kerana menonton",      # "for watching" (ms)
    "karena menonton",      # "for watching" (id)
    "sudah menonton",
    "telah menonton",
    "terima kasih kerana",
    "terima kasih karena",
    "jangan lupa subscribe",
    "jangan lupa like",
    "sampai jumpa di video",
)

# Phrases that CAN be legitimate speech ("thank you" happens in real meetings)
# — dropped only when confidence is also weak.
_SOFT_JUNK = {
    "thank you", "thank you very much", "thanks", "thank you, thank you",
    "terima kasih", "terima kasih banyak",
    "you", "music", "[music]", "(music)", "♪", "♫",
}


def _looks_like_prompt_leak(text: str, prompt: Optional[str]) -> bool:
    """Detect Whisper regurgitating its own initial_prompt as output.
    (Observed in production: the biasing prompt appeared verbatim in the
    transcript during a quiet stretch.) Flags any segment whose words are
    mostly contained in the prompt."""
    if not prompt or len(text) < 15:
        return False
    prompt_words = set(prompt.lower().split())
    words = [w.strip(".,!?") for w in text.lower().split()]
    if len(words) < 4:
        return False
    overlap = sum(1 for w in words if w in prompt_words)
    return overlap / len(words) > 0.6


def _is_hallucinated(text: str, avg_logprob: float,
                     prompt: Optional[str] = None) -> bool:
    """Heuristically detect Whisper hallucinations.

    Signals:
    1. Known junk fragments (bilingual EN + MS/ID), always dropped.
    2. Soft junk ("thank you", "terima kasih") dropped only with weak confidence.
    3. Catastrophically low confidence (avg_logprob < -1.5).
    4. Character / word repetition loops.
    5. The segment parrots the initial_prompt back (prompt leak).
    """
    if not text:
        return True

    norm = text.strip().lower().rstrip(".!?")

    # 1. Always-junk fragments (outro phrases in either language)
    if len(norm) < 80 and any(frag in norm for frag in _ALWAYS_JUNK_FRAGMENTS):
        return True

    # 2. Soft junk — only drop when confidence is also poor
    if norm in _SOFT_JUNK and avg_logprob < -0.5:
        return True

    # 3. Catastrophic confidence drop
    if avg_logprob < -1.5:
        return True

    # 4a. Character repetition — >70% of the (non-space) text is a single char.
    clean = "".join(c for c in norm if not c.isspace())
    if len(clean) >= 8:
        most_common_count = max((clean.count(c) for c in set(clean)), default=0)
        if most_common_count / len(clean) > 0.7:
            return True

    # 4b. Word repetition — same token repeated 5+ times in a row.
    words = norm.split()
    if len(words) >= 5:
        repeats = 1
        for i in range(1, len(words)):
            if words[i] == words[i - 1]:
                repeats += 1
                if repeats >= 5:
                    return True
            else:
                repeats = 1

    # 5. Prompt leak
    if _looks_like_prompt_leak(text, prompt):
        return True

    return False


class WhisperTranscriber:
    """
    Local transcription engine using faster-whisper.
    Provides both file-based and segment-based transcription.

    Usage:
        transcriber = WhisperTranscriber(model_size="small")
        segments = transcriber.transcribe_file("recording.wav")
        for seg in segments:
            print(f"[{seg.start:.1f}s] {seg.text}")
    """

    def __init__(self, model_size: Optional[str] = None,
                 device: str = "auto",
                 compute_type: str = "auto",
                 language: Optional[str] = None,
                 quality_preset: str = "balanced"):
        """
        Args:
            model_size: Whisper model size. If None, auto-detected from hardware
                        guided by quality_preset.
                        Options: tiny, base, small, medium, large-v3
            device: "cpu", "cuda", or "auto" (detect).
            compute_type: "int8", "float16", "float32", or "auto".
            language: ISO language code (e.g., "en"). None = auto-detect.
            quality_preset: "fast", "balanced", "accurate", or "best".
                            Used only when model_size is None.
        """
        if model_size is None:
            model_size = recommend_whisper_model(quality_preset=quality_preset)
            logger.info(
                f"Auto-selected Whisper model: {model_size} "
                f"(quality preset: {quality_preset})"
            )

        if model_size not in SUPPORTED_MODELS:
            logger.warning(f"Unknown model '{model_size}', falling back to 'small'")
            model_size = DEFAULT_MODEL_SIZE

        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language

        self._model = None
        self._loaded = False
        # Set to True (e.g. from the UI thread) to abort a long transcription
        # at the next segment boundary. Already-transcribed segments are kept.
        self.cancel_requested = False

    def _ensure_loaded(self) -> None:
        """Lazy-load the Whisper model on first use."""
        if self._loaded:
            return

        from faster_whisper import WhisperModel

        # Determine device and compute type
        device = self.device
        compute_type = self.compute_type

        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"

        if compute_type == "auto":
            if device == "cuda":
                compute_type = "float16"
            else:
                compute_type = "int8"

        # Model download directory
        model_dir = str(get_models_dir())

        # Check if model is already cached locally.
        # If cached, force offline mode for the duration of the load so we
        # never hit the network. We restore the previous value afterwards
        # because pyannote.audio (used for diarization) needs network access
        # for its first-run model download.
        cached_model_path = self._find_cached_model(model_dir, self.model_size)
        prev_offline = os.environ.get("HF_HUB_OFFLINE")
        if cached_model_path:
            logger.info(f"Using cached model: {cached_model_path}")
            os.environ["HF_HUB_OFFLINE"] = "1"
        else:
            logger.info(f"Model '{self.model_size}' not cached, will download...")

        logger.info(
            f"Loading Whisper model: {self.model_size} "
            f"(device={device}, compute={compute_type})"
        )

        # Try loading the requested model, with fallback to smaller models
        models_to_try = [self.model_size]
        fallback_order = ["small", "base", "tiny"]
        for fb in fallback_order:
            if fb != self.model_size and fb not in models_to_try:
                models_to_try.append(fb)

        try:
            last_error = None
            for model_name in models_to_try:
                try:
                    start = time.time()
                    self._model = WhisperModel(
                        model_name,
                        device=device,
                        compute_type=compute_type,
                        download_root=model_dir,
                    )
                    elapsed = time.time() - start
                    self.model_size = model_name  # update to actual loaded model
                    logger.info(f"Whisper model '{model_name}' loaded in {elapsed:.1f}s")
                    self._loaded = True
                    return
                except Exception as e:
                    last_error = e
                    logger.warning(f"Failed to load model '{model_name}': {e}")
                    cached = self._find_cached_model(model_dir, model_name)
                    if not cached:
                        logger.info(f"Model '{model_name}' not available locally, trying next...")
                    continue

            raise RuntimeError(
                f"Could not load any Whisper model. Last error: {last_error}\n"
                f"Run: python scripts/download_models.py\n"
                f"to pre-download models for offline use."
            )
        finally:
            # Restore previous HF offline state so it doesn't leak to other
            # huggingface_hub consumers (e.g. pyannote diarization).
            if prev_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev_offline

    @staticmethod
    def _find_cached_model(models_dir: str, model_name: str) -> Optional[str]:
        """Check if a faster-whisper model is already cached locally."""
        # faster-whisper uses huggingface_hub cache format:
        # models_dir/models--Systran--faster-whisper-{size}/
        hf_cache_name = f"models--Systran--faster-whisper-{model_name}"
        cache_path = os.path.join(models_dir, hf_cache_name)
        if os.path.isdir(cache_path):
            # Check for actual model files inside snapshots
            snapshots_dir = os.path.join(cache_path, "snapshots")
            if os.path.isdir(snapshots_dir):
                for snapshot in os.listdir(snapshots_dir):
                    snapshot_path = os.path.join(snapshots_dir, snapshot)
                    model_bin = os.path.join(snapshot_path, "model.bin")
                    if os.path.exists(model_bin):
                        return snapshot_path
        return None

    # Vocabulary-biasing prompt for code-mixed Bahasa + English meetings.
    # IMPORTANT: Whisper does NOT follow instructions — it treats the prompt
    # as "previous dialogue" and, on quiet audio, will recite it back into
    # the transcript (observed in production). So this must read like real
    # meeting speech, NOT like instructions, and the hallucination filter
    # additionally drops any segment that parrots it.
    CODE_MIXED_PROMPT = (
        "Okey, kita mula meeting sekarang. Please share screen, kita review "
        "project timeline dan deadline. Server deployment minggu depan, "
        "pastikan login dan password dah setup."
    )

    # Temperature fallback ladder. Whisper retries with the next temperature
    # when log-probability or compression-ratio thresholds fail — this is the
    # single most effective fix for hallucination loops on quiet / noisy audio.
    TEMPERATURE_LADDER = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

    def transcribe_file(self, audio_path: str,
                        vad_filter: bool = True,
                        word_timestamps: bool = False,
                        initial_prompt: Optional[str] = None,
                        on_progress: Optional[callable] = None) -> List[TranscriptSegment]:
        """
        Transcribe an audio file and return timestamped segments.

        Args:
            audio_path: Path to WAV/Opus/MP3 audio file.
            vad_filter: Use faster-whisper's built-in VAD as safety net.
            word_timestamps: If True, include word-level timestamps.
            initial_prompt: Optional context that biases Whisper's vocabulary.
                            Defaults to a code-mixed Bahasa+English prompt.
            on_progress: Optional callback receiving progress fraction (0.0-1.0)
                         as segments complete. Enables % / ETA display.

        Returns:
            List of TranscriptSegment with start, end, text, confidence.
        """
        self._ensure_loaded()
        self.cancel_requested = False

        # Pre-normalize the audio so Whisper sees a healthy level regardless
        # of input gain (especially important for Bluetooth HFP mics).
        from src.utils.audio_utils import load_wav, save_wav_chunk, normalize_for_whisper
        try:
            audio, sr = load_wav(audio_path)
            audio = normalize_for_whisper(audio, sr)
            # Write to a sibling temp file rather than mutating the original.
            norm_path = os.path.splitext(audio_path)[0] + "_normalized.wav"
            save_wav_chunk(audio, norm_path, sr)
            audio_path_for_whisper = norm_path
            logger.info(
                f"Pre-normalized audio: peak={float(np.max(np.abs(audio))):.3f}, "
                f"{len(audio)/sr:.1f}s"
            )
        except Exception as e:
            logger.warning(f"Could not pre-normalize audio, using raw file: {e}")
            audio_path_for_whisper = audio_path

        logger.info(f"Transcribing: {audio_path_for_whisper} (language={self.language or 'auto'})")
        start = time.time()

        # When language is unset, leave it as auto-detect so Whisper picks the
        # right one per chunk — critical for code-mixed Bahasa/English audio.
        # Temperature fallback ladder breaks hallucination loops without
        # disabling cross-segment context.
        segments_gen, info = self._model.transcribe(
            audio_path_for_whisper,
            language=self.language,
            task="transcribe",  # NEVER translate — that's a separate step
            vad_filter=vad_filter,
            vad_parameters=dict(
                min_speech_duration_ms=500,        # was 250 — fewer mid-word cuts
                min_silence_duration_ms=600,       # was 300 — fewer false splits
                speech_pad_ms=400,                 # pad to avoid clipping word edges
            ),
            word_timestamps=word_timestamps,
            beam_size=5,
            initial_prompt=initial_prompt or self.CODE_MIXED_PROMPT,
            temperature=self.TEMPERATURE_LADDER,   # << the big hallucination fix
            condition_on_previous_text=True,       # safe now that temperature fallback works
            compression_ratio_threshold=2.4,       # default — flags repetitive output
            log_prob_threshold=-1.0,               # default — flags low-confidence segments
            no_speech_threshold=0.6,               # skip segments Whisper sees as non-speech
        )

        detected_language = info.language
        language_prob = info.language_probability
        logger.info(
            f"Detected language: {detected_language} "
            f"(probability: {language_prob:.2f})"
        )

        used_prompt = initial_prompt or self.CODE_MIXED_PROMPT
        total_duration = getattr(info, "duration", 0.0) or 0.0
        transcript_segments = []
        dropped = 0
        for segment in segments_gen:
            if self.cancel_requested:
                logger.info(
                    f"Transcription cancelled at {segment.start:.0f}s — "
                    f"keeping {len(transcript_segments)} segments"
                )
                break

            text = segment.text.strip()
            if _is_hallucinated(text, segment.avg_logprob, prompt=used_prompt):
                dropped += 1
                logger.debug(
                    f"Dropping hallucinated segment [{segment.start:.1f}s]: "
                    f"'{text[:60]}' (logprob={segment.avg_logprob:.2f})"
                )
                continue
            ts = TranscriptSegment(
                start=segment.start,
                end=segment.end,
                text=text,
                confidence=segment.avg_logprob,
                language=detected_language or "en",
            )
            transcript_segments.append(ts)

            if on_progress and total_duration > 0:
                try:
                    on_progress(min(segment.end / total_duration, 1.0))
                except Exception:
                    pass

        elapsed = time.time() - start
        total_text = sum(len(s.text) for s in transcript_segments)
        logger.info(
            f"Transcription complete: {len(transcript_segments)} segments, "
            f"{total_text} chars, {elapsed:.1f}s "
            f"({dropped} hallucinated segments dropped)"
        )

        # Clean up the temporary normalized file
        try:
            if audio_path_for_whisper != audio_path and os.path.exists(audio_path_for_whisper):
                os.remove(audio_path_for_whisper)
        except OSError:
            pass

        return transcript_segments

    def transcribe_audio(self, audio: np.ndarray,
                         sample_rate: int = 16000,
                         vad_filter: bool = True) -> List[TranscriptSegment]:
        """
        Transcribe audio from a numpy array (in-memory).

        Args:
            audio: 1D numpy array of float32 samples.
            sample_rate: Sample rate of the audio.
            vad_filter: Use built-in VAD filtering.

        Returns:
            List of TranscriptSegment.
        """
        self._ensure_loaded()

        from src.utils.audio_utils import normalize_for_whisper
        audio = normalize_for_whisper(audio, sample_rate)

        # NOTE: deliberately NO initial_prompt here. This path serves live
        # transcription with short 3-10s windows, where a prompt is far more
        # likely to be recited back as output than to help. Vocabulary
        # biasing only happens in the full-file pass (transcribe_file).
        segments_gen, info = self._model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            vad_filter=vad_filter,
            vad_parameters=dict(
                min_speech_duration_ms=500,
                min_silence_duration_ms=600,
                speech_pad_ms=400,
            ),
            beam_size=5,
            temperature=self.TEMPERATURE_LADDER,
            condition_on_previous_text=False,  # windows are independent
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
        )

        transcript_segments = []
        for segment in segments_gen:
            text = segment.text.strip()
            if _is_hallucinated(text, segment.avg_logprob):
                continue
            ts = TranscriptSegment(
                start=segment.start,
                end=segment.end,
                text=text,
                confidence=segment.avg_logprob,
                language=info.language or "en",
            )
            transcript_segments.append(ts)

        return transcript_segments

    def transcribe_chunk(self, audio: np.ndarray,
                         chunk_offset: float = 0.0) -> List[TranscriptSegment]:
        """
        Transcribe a single audio chunk and offset timestamps
        to match the overall recording timeline.

        Args:
            audio: 1D numpy array of float32 samples at 16kHz.
            chunk_offset: Time offset in seconds for this chunk.

        Returns:
            List of TranscriptSegment with adjusted timestamps.
        """
        segments = self.transcribe_audio(audio, vad_filter=True)

        # Offset timestamps
        for seg in segments:
            seg.start += chunk_offset
            seg.end += chunk_offset

        return segments

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @staticmethod
    def available_models() -> List[dict]:
        """
        Return info about available Whisper models for the UI.
        """
        return [
            {
                "id": "tiny",
                "name": "Tiny",
                "size_mb": 75,
                "description": "Fastest, lowest accuracy. Good for quick drafts.",
                "min_ram_gb": 2,
            },
            {
                "id": "base",
                "name": "Base",
                "size_mb": 150,
                "description": "Fast with decent accuracy.",
                "min_ram_gb": 4,
            },
            {
                "id": "small",
                "name": "Small",
                "size_mb": 500,
                "description": "Best balance of speed and accuracy. Recommended.",
                "min_ram_gb": 6,
            },
            {
                "id": "medium",
                "name": "Medium",
                "size_mb": 1500,
                "description": "High accuracy, slower on CPU.",
                "min_ram_gb": 10,
            },
            {
                "id": "large-v3",
                "name": "Large v3",
                "size_mb": 3000,
                "description": "Highest accuracy. Requires GPU or very fast CPU.",
                "min_ram_gb": 16,
            },
            {
                "id": "large-v3-turbo",
                "name": "Large v3 Turbo",
                "size_mb": 1600,
                "description": "Near large-v3 accuracy at ~8x the speed. "
                               "Best accuracy-per-minute on CPU.",
                "min_ram_gb": 8,
            },
        ]
