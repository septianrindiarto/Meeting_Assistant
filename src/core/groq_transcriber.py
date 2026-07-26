"""
Meeting Scribe — Groq Cloud Transcription Backend
Transcribes audio via Groq's hosted Whisper large-v3 models (~216x real-time).

Free-tier constraints handled here:
    - 100 MB per file           → audio is split into ~10-minute FLAC chunks
    - 7,200 audio-sec per hour  → proactive quota window tracking + wait
    - 20 req/min, 429 responses → retry with Retry-After backoff
    - network failures          → per-chunk retries; permanent failure raises
                                  GroqTranscriptionError so the pipeline can
                                  ROLL BACK to the local Whisper backend

Chunks are cut at silence boundaries (lowest-RMS point near the target cut)
so words are not split in half. Timestamps are offset per chunk so the final
transcript lines up with the full recording timeline.
"""
from __future__ import annotations

import os
import json
import time
import hashlib
import logging
from typing import Callable, List, Optional

import numpy as np

from src.core.models import TranscriptSegment
from src.core.transcriber import _is_hallucinated
from src.utils.file_utils import get_temp_dir, safe_write_json, safe_read_json

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SAMPLE_RATE = 16000

CHUNK_SEC = 600                 # 10-minute chunks
CHUNK_SEEK_BACK_SEC = 20        # search this window before the cut for silence
HOURLY_AUDIO_BUDGET = 7200      # free tier: audio-seconds per rolling hour
MAX_CHUNK_RETRIES = 3
REQUEST_TIMEOUT = 300           # seconds per chunk upload+transcribe


class GroqTranscriptionError(Exception):
    """Raised when Groq transcription permanently fails.
    Carries any segments transcribed before the failure."""

    def __init__(self, message: str, partial_segments: Optional[List[TranscriptSegment]] = None):
        super().__init__(message)
        self.partial_segments = partial_segments or []


class GroqTranscriber:
    """
    Cloud transcription via Groq's Whisper endpoint.

    Mirrors WhisperTranscriber's interface where it matters:
        - transcribe_file(audio_path, on_progress=...) -> List[TranscriptSegment]
        - cancel_requested flag (checked between chunks)
        - model_size attribute (for progress messages)
    """

    def __init__(self, api_key: str,
                 model: str = "whisper-large-v3-turbo",
                 language: Optional[str] = None,
                 prompt: Optional[str] = None,
                 resume_dir: Optional[str] = None):
        """
        Args:
            resume_dir: Directory for persistent job state. When set, every
                completed chunk's transcript is written to disk immediately,
                so a crash / app restart / PC reboot resumes exactly where it
                left off — finished chunks are never re-uploaded and never
                consume quota twice.
        """
        if not api_key:
            raise ValueError("Groq API key is required")
        self.api_key = api_key
        self.model_size = model            # name parity with WhisperTranscriber
        self.language = language
        self.prompt = prompt
        self.resume_dir = resume_dir
        self.cancel_requested = False

        # Rolling-hour quota tracking
        self._window_start: Optional[float] = None
        self._window_sent_sec = 0.0

        self.on_status: Optional[Callable[[str], None]] = None
        # Called with the full segment list so far after every chunk —
        # lets the UI show the growing transcript during long jobs.
        self.on_partial: Optional[Callable[[List[TranscriptSegment]], None]] = None

    # ─── Public API ──────────────────────────────────────────────

    def transcribe_file(self, audio_path: str,
                        on_progress: Optional[Callable[[float], None]] = None,
                        **_ignored) -> List[TranscriptSegment]:
        """
        Transcribe an audio file via Groq, chunking as needed.

        Raises:
            GroqTranscriptionError: on permanent failure (auth, quota
                exhausted, network down). Partial segments are attached
                so the caller can decide what to keep.
        """
        self.cancel_requested = False

        try:
            import httpx  # noqa: F401
        except ImportError:
            raise GroqTranscriptionError(
                "httpx not installed — run: pip install httpx"
            )

        from src.utils.audio_utils import load_wav, normalize_for_whisper

        audio, sr = load_wav(audio_path)
        if sr != SAMPLE_RATE:
            from src.utils.audio_utils import resample
            audio = resample(audio, sr, SAMPLE_RATE)
        audio = normalize_for_whisper(audio, SAMPLE_RATE)

        total_sec = len(audio) / SAMPLE_RATE
        chunks = self._split_at_silence(audio)
        logger.info(
            f"Groq transcription: {total_sec:.0f}s audio in {len(chunks)} chunk(s), "
            f"model={self.model_size}"
        )

        # ── Resume support ──
        # Chunk boundaries are deterministic (same audio → same split), so a
        # saved job maps cleanly onto the chunks we just computed.
        job_path, job = self._load_job(audio_path, len(chunks))
        already_done = len(job["completed"])
        if already_done:
            self._status(
                f"Resuming previous job — {already_done} of {len(chunks)} "
                "parts already transcribed (no quota re-spent)"
            )
        # Restore the quota window if it's still current
        saved_ws = job.get("window_start")
        if saved_ws and (time.time() - saved_ws) < 3600:
            self._window_start = saved_ws
            self._window_sent_sec = job.get("window_sent", 0.0)

        all_segments: List[TranscriptSegment] = []
        offset = 0.0

        for i, chunk in enumerate(chunks):
            chunk_sec = len(chunk) / SAMPLE_RATE

            # Skip chunks finished in a previous run — free and instant.
            done = job["completed"].get(str(i))
            if done is not None:
                all_segments.extend(TranscriptSegment(**d) for d in done)
                offset += chunk_sec
                if on_progress:
                    try:
                        on_progress(min(offset / total_sec, 1.0))
                    except Exception:
                        pass
                continue

            if self.cancel_requested:
                logger.info("Groq transcription cancelled by user")
                return all_segments  # job file stays — resume later

            self._respect_hourly_budget(chunk_sec, i, len(chunks))

            if self.cancel_requested:      # may have been set during wait
                return all_segments

            self._status(f"Uploading part {i + 1} of {len(chunks)}...")
            try:
                segments = self._transcribe_chunk(chunk, offset, i, len(chunks))
                all_segments.extend(segments)
            except GroqTranscriptionError as e:
                # Permanent chunk failure — surface with partials for rollback.
                # The job file survives, so a later retry resumes from here.
                e.partial_segments = all_segments
                raise

            offset += chunk_sec
            self._window_sent_sec += chunk_sec

            # Persist immediately: crash-safe from this moment on.
            job["completed"][str(i)] = [s.to_dict() for s in segments]
            job["window_start"] = self._window_start
            job["window_sent"] = self._window_sent_sec
            if job_path:
                try:
                    safe_write_json(job_path, job)
                except Exception as e:
                    logger.warning(f"Could not persist job state: {e}")

            if self.on_partial:
                try:
                    self.on_partial(list(all_segments))
                except Exception:
                    pass

            if on_progress:
                try:
                    on_progress(min(offset / total_sec, 1.0))
                except Exception:
                    pass

        # Job finished — clean up the manifest.
        if job_path and os.path.exists(job_path):
            try:
                os.remove(job_path)
            except OSError:
                pass

        logger.info(f"Groq transcription complete: {len(all_segments)} segments")
        return all_segments

    def _load_job(self, audio_path: str, n_chunks: int):
        """Load or create the persistent job manifest for this audio file.
        Job identity = file path + size + mtime, so a re-exported file gets
        a fresh job while the same file resumes."""
        if not self.resume_dir:
            return None, {"completed": {}}

        try:
            os.makedirs(self.resume_dir, exist_ok=True)
            st = os.stat(audio_path)
            key = f"{audio_path}|{st.st_size}|{int(st.st_mtime)}|{self.model_size}"
            job_id = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
            job_path = os.path.join(self.resume_dir, f"groq_job_{job_id}.json")

            job = safe_read_json(job_path)
            if job and job.get("n_chunks") == n_chunks:
                return job_path, job

            job = {"job_id": job_id, "n_chunks": n_chunks, "completed": {}}
            return job_path, job
        except Exception as e:
            logger.warning(f"Job persistence unavailable: {e}")
            return None, {"completed": {}}

    # ─── Internals ───────────────────────────────────────────────

    def _status(self, msg: str) -> None:
        logger.info(msg)
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _split_at_silence(self, audio: np.ndarray) -> List[np.ndarray]:
        """Split audio into ~CHUNK_SEC pieces, cutting at the quietest point
        within the last CHUNK_SEEK_BACK_SEC before each target boundary."""
        chunk_len = CHUNK_SEC * SAMPLE_RATE
        if len(audio) <= chunk_len:
            return [audio]

        chunks = []
        pos = 0
        seek = CHUNK_SEEK_BACK_SEC * SAMPLE_RATE
        step = int(0.1 * SAMPLE_RATE)  # 100ms RMS windows

        while pos < len(audio):
            end = pos + chunk_len
            if end >= len(audio):
                chunks.append(audio[pos:])
                break

            # Find the quietest 100ms window in [end-seek, end]
            search_start = max(pos, end - seek)
            best_cut = end
            best_rms = float("inf")
            for w in range(search_start, end - step, step):
                rms = float(np.sqrt(np.mean(audio[w:w + step] ** 2)))
                if rms < best_rms:
                    best_rms = rms
                    best_cut = w + step // 2

            chunks.append(audio[pos:best_cut])
            pos = best_cut

        return chunks

    def _respect_hourly_budget(self, chunk_sec: float,
                               chunk_idx: int, total_chunks: int) -> None:
        """Free tier allows 7,200 audio-seconds per rolling hour. If the next
        chunk would exceed it, wait (with status updates) until the window
        resets. Cancellation is honored during the wait."""
        now = time.time()
        if self._window_start is None:
            self._window_start = now
            return

        # Reset window if an hour has passed
        if now - self._window_start >= 3600:
            self._window_start = now
            self._window_sent_sec = 0.0
            return

        if self._window_sent_sec + chunk_sec <= HOURLY_AUDIO_BUDGET:
            return

        wait_until = self._window_start + 3600
        wait_sec = max(0, wait_until - now)
        self._status(
            f"Hourly free-tier quota reached — part {chunk_idx + 1} of "
            f"{total_chunks} will be sent in {int(wait_sec / 60) + 1} min..."
        )
        while time.time() < wait_until:
            if self.cancel_requested:
                return
            remaining = int((wait_until - time.time()) / 60) + 1
            self._status(
                f"Waiting for quota window — {remaining} min until part "
                f"{chunk_idx + 1} of {total_chunks}..."
            )
            time.sleep(min(30, max(1, wait_until - time.time())))

        self._window_start = time.time()
        self._window_sent_sec = 0.0

    def _transcribe_chunk(self, chunk: np.ndarray, offset: float,
                          idx: int, total: int) -> List[TranscriptSegment]:
        """Encode one chunk as FLAC, POST to Groq, parse verbose_json.
        Retries transient failures; raises GroqTranscriptionError on
        permanent ones (bad key, daily quota, repeated network errors)."""
        import httpx

        flac_path = self._encode_flac(chunk, idx)

        try:
            data = {
                "model": self.model_size,
                "response_format": "verbose_json",
                "temperature": "0",
            }
            if self.language:
                data["language"] = self.language
            if self.prompt:
                data["prompt"] = self.prompt

            last_error = None
            for attempt in range(1, MAX_CHUNK_RETRIES + 1):
                if self.cancel_requested:
                    return []
                try:
                    with open(flac_path, "rb") as f:
                        resp = httpx.post(
                            GROQ_URL,
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            data=data,
                            files={"file": (os.path.basename(flac_path), f, "audio/flac")},
                            timeout=REQUEST_TIMEOUT,
                        )

                    if resp.status_code == 200:
                        return self._parse_response(resp.json(), offset)

                    if resp.status_code == 401:
                        raise GroqTranscriptionError(
                            "Groq API key rejected (401). Check the key in Settings."
                        )

                    if resp.status_code == 413:
                        raise GroqTranscriptionError(
                            "Chunk exceeded Groq's file size limit (413)."
                        )

                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("retry-after", "60"))
                        retry_after = min(retry_after, 3700)
                        self._status(
                            f"Groq rate limit hit — retrying part {idx + 1} of "
                            f"{total} in {retry_after}s..."
                        )
                        waited = 0
                        while waited < retry_after and not self.cancel_requested:
                            time.sleep(min(15, retry_after - waited))
                            waited += 15
                        continue  # does not consume a retry attempt? keep simple: it does

                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning(f"Groq chunk {idx + 1} attempt {attempt}: {last_error}")

                except GroqTranscriptionError:
                    raise
                except Exception as e:
                    last_error = str(e)
                    logger.warning(
                        f"Groq chunk {idx + 1} attempt {attempt} failed: {e}"
                    )
                    time.sleep(min(10 * attempt, 30))

            raise GroqTranscriptionError(
                f"Chunk {idx + 1}/{total} failed after {MAX_CHUNK_RETRIES} "
                f"attempts. Last error: {last_error}"
            )
        finally:
            try:
                os.remove(flac_path)
            except OSError:
                pass

    def _encode_flac(self, chunk: np.ndarray, idx: int) -> str:
        """Write a chunk as 16kHz mono FLAC (~6x smaller than WAV).
        Falls back to WAV if soundfile is unavailable."""
        temp_dir = str(get_temp_dir())
        try:
            import soundfile as sf
            path = os.path.join(temp_dir, f"groq_chunk_{idx:03d}.flac")
            sf.write(path, chunk, SAMPLE_RATE, format="FLAC")
        except ImportError:
            from src.utils.audio_utils import save_wav_chunk
            path = os.path.join(temp_dir, f"groq_chunk_{idx:03d}.wav")
            save_wav_chunk(chunk, path, SAMPLE_RATE)
        return path

    def _parse_response(self, payload: dict, offset: float) -> List[TranscriptSegment]:
        """Convert Groq's verbose_json into TranscriptSegments with the
        chunk offset applied and hallucination filtering."""
        language = payload.get("language", "") or ""
        segments = []
        for seg in payload.get("segments", []):
            text = (seg.get("text") or "").strip()
            avg_logprob = float(seg.get("avg_logprob", 0.0))
            if _is_hallucinated(text, avg_logprob, prompt=self.prompt):
                continue
            segments.append(TranscriptSegment(
                start=float(seg.get("start", 0.0)) + offset,
                end=float(seg.get("end", 0.0)) + offset,
                text=text,
                confidence=avg_logprob,
                language=language[:2] if language else "en",
            ))
        return segments
