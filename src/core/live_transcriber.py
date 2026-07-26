"""
Meeting Scribe — Real-Time Live Transcription
Consumes mixed audio chunks from the capture engine WHILE recording and
produces transcript segments with only a few seconds of latency.

Design:
    - A dedicated worker thread owns a small, fast Whisper model (base by
      default — ~150MB, faster than real-time on any modern CPU).
    - Audio arrives via feed() from the capture engine's mixer loop.
    - The worker accumulates audio and flushes a window to Whisper when:
        a) the buffer ends in silence (natural utterance boundary), or
        b) the buffer exceeds MAX_WINDOW_SEC (force flush to bound latency).
    - Flushing at silence boundaries avoids cutting words in half.
    - Segment timestamps are offset by the amount of audio already committed,
      so they line up with the full recording timeline.

The live transcript is a fast draft. The post-meeting "Process" step re-runs
the full pipeline with a larger model and overwrites it with a better pass.
"""
from __future__ import annotations

import time
import logging
import threading
from queue import Queue, Empty
from typing import Callable, List, Optional

import numpy as np

from src.core.models import TranscriptSegment

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MIN_WINDOW_SEC = 3.0      # don't bother transcribing less than this
MAX_WINDOW_SEC = 10.0     # force a flush at this size to bound latency
SILENCE_TAIL_SEC = 0.45   # trailing window inspected for silence
SILENCE_RMS = 0.008       # below this RMS the tail counts as silence


class LiveTranscriber:
    """
    Streaming-ish transcription built on faster-whisper.

    Usage:
        live = LiveTranscriber(model_size="base",
                               on_segments=handle_new_segments,
                               on_status=show_status)
        live.start()                       # loads model in background
        capture_engine.on_audio_chunk = live.feed
        ...
        live.stop(flush=True)              # transcribe any remaining audio
    """

    def __init__(self,
                 model_size: str = "base",
                 language: Optional[str] = None,
                 on_segments: Optional[Callable[[List[TranscriptSegment]], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None):
        self.model_size = model_size
        self.language = language
        self.on_segments = on_segments
        self.on_status = on_status

        self._queue: Queue = Queue(maxsize=2000)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._transcriber = None          # WhisperTranscriber, created in thread
        self._model_ready = threading.Event()

        # Buffer state (owned by worker thread)
        self._buffer: List[np.ndarray] = []
        self._buf_samples = 0
        self._committed_sec = 0.0         # timeline offset for next flush

    # ─── Public API ──────────────────────────────────────────────

    def start(self) -> None:
        """Start the worker thread (loads the model in the background)."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="LiveTranscriber"
        )
        self._thread.start()

    def feed(self, audio: np.ndarray) -> None:
        """Called from the capture mixer with each mixed 16kHz mono block.
        Non-blocking; drops audio only if the queue is pathologically full."""
        try:
            self._queue.put_nowait(audio)
        except Exception:
            logger.warning("Live transcriber queue full — dropping audio block")

    def stop(self, flush: bool = True, timeout: float = 30.0) -> None:
        """Stop the worker. If flush=True, transcribe the remaining buffer
        before returning so the last words of the meeting aren't lost."""
        if self._thread is None:
            return
        self._flush_on_stop = flush
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def is_ready(self) -> bool:
        """True once the model has finished loading."""
        return self._model_ready.is_set()

    # ─── Worker ──────────────────────────────────────────────────

    def _status(self, msg: str) -> None:
        logger.info(msg)
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _run(self) -> None:
        self._flush_on_stop = True
        try:
            from src.core.transcriber import WhisperTranscriber

            self._status(
                f"Loading live transcription model '{self.model_size}' "
                "(first use downloads it)..."
            )
            self._transcriber = WhisperTranscriber(
                model_size=self.model_size,
                language=self.language,
            )
            self._transcriber._ensure_loaded()
            self._model_ready.set()
            self._status("● Live transcription active")

        except Exception as e:
            logger.error(f"Live transcriber failed to load model: {e}", exc_info=True)
            self._status(f"Live transcription unavailable: {e}")
            # Drain the queue so the capture engine's feed() never blocks
            while not self._stop_event.is_set():
                try:
                    self._queue.get(timeout=0.2)
                except Empty:
                    pass
            return

        while not self._stop_event.is_set():
            self._drain_queue()

            dur = self._buf_samples / SAMPLE_RATE
            if dur >= MIN_WINDOW_SEC:
                if dur >= MAX_WINDOW_SEC or self._tail_is_silent():
                    self._flush()

            time.sleep(0.1)

        # Shutdown: pick up any audio still in the queue, then final flush.
        self._drain_queue()
        if self._flush_on_stop and self._buf_samples > int(0.5 * SAMPLE_RATE):
            self._status("Transcribing final audio...")
            self._flush()
        self._status("Live transcription stopped")

    def _drain_queue(self) -> None:
        while True:
            try:
                audio = self._queue.get_nowait()
                if len(audio):
                    self._buffer.append(audio)
                    self._buf_samples += len(audio)
            except Empty:
                break

    def _tail_is_silent(self) -> bool:
        """Check whether the last SILENCE_TAIL_SEC of the buffer is silence —
        a natural utterance boundary where cutting won't split a word."""
        need = int(SILENCE_TAIL_SEC * SAMPLE_RATE)
        if self._buf_samples < need:
            return False
        # Walk backwards through buffer blocks to collect the tail
        collected: List[np.ndarray] = []
        total = 0
        for block in reversed(self._buffer):
            collected.append(block)
            total += len(block)
            if total >= need:
                break
        tail = np.concatenate(list(reversed(collected)))[-need:]
        rms = float(np.sqrt(np.mean(tail ** 2)))
        return rms < SILENCE_RMS

    def _flush(self) -> None:
        """Transcribe the accumulated buffer and emit segments."""
        if not self._buffer:
            return

        audio = np.concatenate(self._buffer)
        offset = self._committed_sec
        window_sec = len(audio) / SAMPLE_RATE

        self._buffer = []
        self._buf_samples = 0
        self._committed_sec += window_sec

        # Skip windows that are entirely silence — transcribing them is the
        # #1 source of hallucinated junk ("thank you for watching" etc).
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < SILENCE_RMS:
            logger.debug(f"Live window skipped (silent, rms={rms:.4f})")
            return

        try:
            start = time.time()
            segments = self._transcriber.transcribe_chunk(audio, chunk_offset=offset)
            elapsed = time.time() - start
            logger.debug(
                f"Live window: {window_sec:.1f}s audio → {len(segments)} segments "
                f"in {elapsed:.1f}s"
            )

            # Language pinning: per-window auto-detection flaps wildly on
            # code-mixed speech (Malay → English → Tagalog...). Once we get a
            # confident window, lock its language for the rest of the session.
            # A user-set language in Settings is respected and never overridden.
            if (segments and self._transcriber.language is None):
                total_chars = sum(len(s.text) for s in segments)
                mean_conf = sum(s.confidence for s in segments) / len(segments)
                if total_chars >= 25 and mean_conf > -0.8:
                    lang = segments[0].language
                    if lang:
                        self._transcriber.language = lang
                        self._status(f"● Live — language locked: {lang}")

            if segments and self.on_segments:
                try:
                    self.on_segments(segments)
                except Exception:
                    logger.warning("on_segments callback failed", exc_info=True)
        except Exception as e:
            logger.error(f"Live transcription window failed: {e}", exc_info=True)
