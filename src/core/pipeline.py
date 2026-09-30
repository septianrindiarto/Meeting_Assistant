"""
Meeting Scribe — Pipeline Orchestrator
Wires together all processing stages:
    Capture → VAD → Transcribe → Diarize → Structure → Render → Persist

Each stage runs in sequence (post-meeting) or is deferred appropriately.
"""
from __future__ import annotations

import os
import logging
import time
from datetime import datetime
from typing import Optional, List, Callable

from src.core.models import (
    Meeting, MeetingMetadata, AudioSource, LLMBackend,
    TranscriptSegment, StructuredMeeting
)
from src.core.audio_capture import AudioCaptureEngine
from src.core.template_engine import TemplateEngine
from src.core.bundle_manager import BundleManager
from src.core.database import MeetingDatabase
from src.core.settings import Settings
from src.utils.audio_utils import (
    concatenate_chunks, concat_wavs_streaming, format_duration,
    get_audio_duration, wav_duration,
)
from src.utils.file_utils import get_temp_dir, get_app_data_dir, safe_read_json, safe_write_json
from src.core.import_plan import ImportPlan, DOCUMENTS, context_markdown

_UNSET = object()
# Roughly the most a single free-tier Groq LLM request can take. Longer
# transcripts skip the automatic Analysis panel instead of failing.
MAX_ANALYSIS_CHARS = 60_000


def _ts(seconds: float) -> str:
    """m:ss under an hour, h:mm:ss above."""
    s = int(seconds or 0)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _import_jobs_dir() -> str:
    d = os.path.join(str(get_app_data_dir()), "import_jobs")
    os.makedirs(d, exist_ok=True)
    return d

logger = logging.getLogger(__name__)


class PipelineState:
    """Tracks the current state of the processing pipeline."""
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    STRUCTURING = "structuring"
    RENDERING = "rendering"
    SAVING = "saving"
    COMPLETE = "complete"
    ERROR = "error"


class MeetingPipeline:
    """
    Central orchestrator for the entire meeting lifecycle.

    Lifecycle:
        1. start_recording()   → begins audio capture
        2. pause/resume()      → controls recording
        3. stop_recording()    → finalizes audio
        4. process_meeting()   → transcribe → diarize → structure
        5. generate_documents() → render templates → save bundle

    Usage:
        pipeline = MeetingPipeline()
        pipeline.on_state_change = lambda state: update_ui(state)
        pipeline.on_progress = lambda msg: show_status(msg)

        pipeline.start_recording(title="Weekly Standup")
        # ... meeting happens ...
        pipeline.stop_recording()
        pipeline.process_meeting()
        pipeline.generate_documents(template_path)
    """

    def __init__(self):
        self.settings = Settings.instance()
        self.state = PipelineState.IDLE
        self.meeting: Optional[Meeting] = None

        # Components (lazy-initialized — heavy AI modules imported only when needed)
        self._capture_engine: Optional[AudioCaptureEngine] = None
        self._transcriber = None  # WhisperTranscriber (lazy)
        self._live = None          # LiveTranscriber (lazy, created per recording)
        self._vad = None           # VoiceActivityDetector (lazy)
        self._diarizer = None      # SpeakerDiarizer (lazy)
        self._structurer = None    # MeetingStructurer (lazy)
        self._template_engine = TemplateEngine()
        self._bundle_manager = BundleManager()
        self._database = MeetingDatabase()

        # Temp working audio (decoded import / concatenated recording) so it
        # can be cleaned up on save, cancel, or error.
        self._working_audio_path: Optional[str] = None
        self._working_audio_paths: List[str] = []   # decoded import parts etc.
        self._current_import_job: Optional[str] = None

        # Callbacks
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_progress: Optional[Callable[[str], None]] = None
        self._on_level_change: Optional[Callable[[float], None]] = None
        self.on_transcript_update: Optional[Callable[[List[TranscriptSegment]], None]] = None
        # (level, message) — device switches / warnings during recording
        self.on_device_event: Optional[Callable[[str, str], None]] = None

    # Use a property so that assigning a level callback at any point — even
    # AFTER recording has started — still propagates to the live capture engine.
    # Without this, RecordingBar.start() wires too late and the waveform stays
    # silent for the entire session.
    @property
    def on_level_change(self) -> Optional[Callable[[float], None]]:
        return self._on_level_change

    @on_level_change.setter
    def on_level_change(self, cb: Optional[Callable[[float], None]]) -> None:
        self._on_level_change = cb
        if self._capture_engine is not None:
            self._capture_engine.on_level_change = cb

    def _set_state(self, state: str) -> None:
        """Update pipeline state and notify listeners."""
        self.state = state
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception:
                pass

    def _report_progress(self, message: str) -> None:
        """Report progress to UI."""
        logger.info(message)
        if self.on_progress:
            try:
                self.on_progress(message)
            except Exception:
                pass

    # ─── Recording ───────────────────────────────────────────────

    def start_recording(self, title: str = "Untitled Meeting",
                        source: Optional[AudioSource] = None) -> None:
        """Begin recording a new meeting."""
        if self.state != PipelineState.IDLE:
            logger.warning(f"Cannot start recording in state: {self.state}")
            return

        # Determine audio source
        if source is None:
            source_str = self.settings.get("audio_source", "both")
            source = AudioSource(source_str)

        # Initialize meeting
        self.meeting = Meeting(
            metadata=MeetingMetadata(
                title=title,
                date=datetime.now().strftime("%Y-%m-%d"),
                app_version="1.0.0",
            )
        )

        # Create capture engine. Devices are chosen by name; "" = Automatic
        # (first Bluetooth headset connected, otherwise Windows defaults).
        self._capture_engine = AudioCaptureEngine(
            source=source,
            mic_device=self.settings.get("mic_device") or None,
            system_device=self.settings.get("system_device") or None,
            prefer_bluetooth=bool(self.settings.get("prefer_bluetooth", True)),
            bluetooth_mode=self.settings.get("bluetooth_mode", "both") or "both",
        )
        self._capture_engine.on_device_event = self._on_device_event

        # Wire level callback for waveform display BEFORE starting capture
        # so the very first audio chunks update the UI immediately.
        if self.on_level_change:
            self._capture_engine.on_level_change = self.on_level_change

        # Real-time transcription: transcript appears WHILE the meeting runs.
        if self.settings.get("live_transcription", True):
            from src.core.live_transcriber import LiveTranscriber
            self._live = LiveTranscriber(
                model_size=self.settings.get("live_model", "base"),
                language=self.settings.get("transcription_language"),
                on_segments=self._on_live_segments,
                on_status=self._report_progress,
            )
            self._live.start()
            self._capture_engine.on_audio_chunk = self._live.feed

        self._capture_engine.start()
        self._set_state(PipelineState.RECORDING)
        self._report_progress(f"Recording started: {title}")

    def _on_device_event(self, level: str, message: str) -> None:
        """Forward device switches / warnings from the capture engine."""
        if level in ("warning", "error"):
            self._report_progress(message)
        if self.on_device_event:
            try:
                self.on_device_event(level, message)
            except Exception:
                pass

    def _on_live_segments(self, segments: List[TranscriptSegment]) -> None:
        """Append live segments to the transcript and notify the UI.
        Called from the LiveTranscriber worker thread — the UI layer must
        marshal to the main thread (done via Qt signal in the workspace)."""
        if not self.meeting:
            return
        self.meeting.transcript.extend(segments)
        if self.on_transcript_update:
            try:
                self.on_transcript_update(list(self.meeting.transcript))
            except Exception:
                pass

    def pause_recording(self) -> None:
        """Pause the current recording."""
        if self._capture_engine and self.state == PipelineState.RECORDING:
            self._capture_engine.pause()
            self._set_state(PipelineState.PAUSED)
            self._report_progress("Recording paused")

    def resume_recording(self) -> None:
        """Resume a paused recording."""
        if self._capture_engine and self.state == PipelineState.PAUSED:
            self._capture_engine.resume()
            self._set_state(PipelineState.RECORDING)
            self._report_progress("Recording resumed")

    def stop_recording(self) -> None:
        """Stop recording and finalize audio chunks."""
        if self._capture_engine and self.state in (PipelineState.RECORDING, PipelineState.PAUSED):
            # Stop feeding the live transcriber, then flush its final window
            # so the last words of the meeting make it into the transcript.
            self._capture_engine.on_audio_chunk = None
            chunk_paths = self._capture_engine.stop()

            if self._live is not None:
                self._live.stop(flush=True)
                self._live = None
            self.meeting.chunk_paths = chunk_paths
            # Keep device switches / warnings with the meeting: they explain
            # any gaps in the audio to whoever reads the transcript later.
            self.meeting.metadata.recording_events = [
                {"at": round(e.get("at", 0), 1), "level": e.get("level"),
                 "message": e.get("message")}
                for e in getattr(self._capture_engine, "events", [])
            ]

            # Concatenate chunks into a single file
            if chunk_paths:
                temp_dir = str(get_temp_dir())
                combined_path = os.path.join(temp_dir, "recording.wav")
                concatenate_chunks(chunk_paths, combined_path)
                self.meeting.audio_path = combined_path

                # Update duration
                duration_sec = get_audio_duration(combined_path)
                self.meeting.metadata.duration = format_duration(duration_sec)
                self.meeting.metadata.duration_seconds = duration_sec

            self._set_state(PipelineState.IDLE)
            self._report_progress(
                f"Recording stopped. Duration: {self.meeting.metadata.duration}"
            )

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed recording time in seconds."""
        if self._capture_engine:
            return self._capture_engine.elapsed_seconds
        return 0.0

    def cancel_processing(self) -> None:
        """Request cancellation of an in-flight transcription.
        Takes effect at the next segment boundary; partial results are kept."""
        if self._transcriber is not None:
            self._transcriber.cancel_requested = True
            self._report_progress("Cancelling — finishing current segment...")

    # ─── Media Import ────────────────────────────────────────────

    def import_media_file(self, media_path: str) -> None:
        """
        Import an audio/video file (mp3, mp4, m4a, wav, ...) as a new meeting.
        The audio stream is decoded DIRECTLY to 16kHz mono PCM — no lossy
        mp3 conversion step — then the meeting is ready for process_meeting().

        Args:
            media_path: Path to the media file.
        """
        from src.core.media_import import decode_media_to_wav

        filename = os.path.splitext(os.path.basename(media_path))[0]
        self._report_progress(f"Importing '{os.path.basename(media_path)}'...")

        self.meeting = Meeting(
            metadata=MeetingMetadata(
                title=filename,
                date=datetime.now().strftime("%Y-%m-%d"),
                app_version="1.0.0",
            )
        )

        temp_dir = str(get_temp_dir())
        wav_path = os.path.join(temp_dir, f"import_{filename[:40]}.wav")
        # Remember it so cancel/error paths can delete the decoded copy —
        # it is ~115 MB per audio hour and would otherwise leak.
        self._working_audio_path = wav_path
        wav_path, duration = decode_media_to_wav(media_path, wav_path)

        self.meeting.audio_path = wav_path
        self.meeting.metadata.duration = format_duration(duration)
        self.meeting.metadata.duration_seconds = duration

        self._report_progress(
            f"Imported {self.meeting.metadata.duration} of audio — transcribing..."
        )

    # ─── Processing ──────────────────────────────────────────────

    def process_meeting(self) -> None:
        """
        Run the full post-meeting processing pipeline:
        Transcribe → Diarize → Structure
        """
        if not self.meeting or not self.meeting.audio_path:
            logger.warning("No audio to process")
            return

        audio_path = self.meeting.audio_path

        # Stage 1: Transcription
        self._set_state(PipelineState.TRANSCRIBING)
        self._report_progress("Transcribing audio...")
        self._transcribe(audio_path)

        # If the user cancelled mid-transcription, keep what we have and stop.
        if self._transcriber is not None and self._transcriber.cancel_requested:
            self._set_state(PipelineState.IDLE)
            self._report_progress(
                "Processing cancelled — partial transcript kept. "
                "You can still save the bundle."
            )
            return

        # Stage 2: Diarization (if enabled and configured)
        if self.settings.get("diarization_enabled") and self.settings.get("hf_token"):
            self._set_state(PipelineState.DIARIZING)
            self._report_progress("Identifying speakers...")
            self._diarize(audio_path)

        # Stage 3: LLM Structuring (if backend available)
        backend = self.settings.get_llm_backend()
        if backend != LLMBackend.NONE:
            self._set_state(PipelineState.STRUCTURING)
            self._structure()

        self._set_state(PipelineState.IDLE)
        self._report_progress("Processing complete!")

    def _make_progress_reporter(self, label: str = "Transcribing"):
        """Percent + ETA progress callback, throttled to whole-percent changes."""
        t_start = time.time()
        last_pct = [-1]

        def _progress(frac: float) -> None:
            pct = int(frac * 100)
            if pct == last_pct[0]:
                return
            last_pct[0] = pct
            elapsed = time.time() - t_start
            if frac > 0.02:
                eta_min = (elapsed * (1.0 - frac) / frac) / 60.0
                self._report_progress(
                    f"{label}... {pct}% — about {max(1, round(eta_min))} min left"
                )
            else:
                self._report_progress(f"{label}... {pct}%")

        return _progress

    def _transcribe(self, audio_path: str) -> None:
        """Transcribe the current meeting's audio (recordings / Process)."""
        segments = self._transcribe_to_segments(audio_path)
        self.meeting.transcript = segments
        if self.on_transcript_update:
            self.on_transcript_update(segments)

    def _default_partial(self, segments_so_far) -> None:
        self.meeting.transcript = segments_so_far
        if self.on_transcript_update:
            try:
                self.on_transcript_update(list(segments_so_far))
            except Exception:
                pass

    def _transcribe_to_segments(self, audio_path: str, language=_UNSET,
                                prompt: Optional[str] = None,
                                job_key: Optional[str] = None,
                                on_partial: Optional[Callable] = None,
                                label: str = "Transcribing") -> List[TranscriptSegment]:
        """Run transcription with the configured backend and return segments.

        ROLLBACK MECHANISM: if the cloud backend (Groq) fails for any reason
        — bad key, quota exhausted, network down — and fallback is enabled,
        the same audio is transcribed with local Whisper instead, so the user
        always ends up with a transcript.
        """
        if language is _UNSET:
            language = self.settings.get("transcription_language")
        backend = self.settings.get("stt_backend", "local")

        if backend == "groq":
            try:
                return self._transcribe_groq(audio_path, language, prompt,
                                             job_key, on_partial, label)
            except Exception as e:
                # User cancellation is not a failure — don't roll over to local.
                if self._transcriber is not None and \
                        getattr(self._transcriber, "cancel_requested", False):
                    raise
                logger.error(f"Groq backend failed: {e}", exc_info=True)
                if not self.settings.get("cloud_stt_fallback_local", True):
                    raise
                partial = getattr(e, "partial_segments", [])
                if partial:
                    (on_partial or self._default_partial)(list(partial))
                self._report_progress(
                    f"Cloud transcription failed ({str(e)[:80]}) — "
                    "rolling back to local Whisper..."
                )

        return self._transcribe_local(audio_path, language, prompt, label)

    def _transcribe_groq(self, audio_path: str, language, prompt, job_key,
                         on_partial, label) -> List[TranscriptSegment]:
        """Transcribe via Groq's hosted Whisper (free tier friendly).

        Long jobs are crash-safe: every completed chunk is persisted to a
        job file under the app data dir, so closing the app mid-wait (e.g.
        during the hourly quota pause) loses nothing — running it again
        resumes from the last finished chunk.
        """
        from src.core.groq_transcriber import GroqTranscriber
        from src.core.transcriber import WhisperTranscriber

        api_key = self.settings.get("groq_api_key", "")
        if not api_key:
            raise ValueError(
                "No Groq API key configured. Add one in Settings → "
                "Transcription Backend, or switch backend to Local."
            )

        self._transcriber = GroqTranscriber(
            api_key=api_key,
            model=self.settings.get("groq_model", "whisper-large-v3-turbo"),
            language=language,
            prompt=prompt or WhisperTranscriber.CODE_MIXED_PROMPT,
            resume_dir=str(get_app_data_dir() / "cloud_jobs"),
        )
        self._transcriber.on_status = self._report_progress
        self._transcriber.on_partial = on_partial or self._default_partial

        self._report_progress(f"{label} via Groq cloud...")
        return self._transcriber.transcribe_file(
            audio_path,
            on_progress=self._make_progress_reporter(f"{label} (cloud)"),
            job_key=job_key,
        )

    def _transcribe_local(self, audio_path: str, language, prompt,
                          label: str = "Transcribing") -> List[TranscriptSegment]:
        """Transcribe with local faster-whisper."""
        from src.core.transcriber import WhisperTranscriber

        model_size = self.settings.get("whisper_model", "auto")
        if model_size == "auto":
            model_size = None  # WhisperTranscriber will auto-detect

        self._transcriber = WhisperTranscriber(
            model_size=model_size,
            language=language,
            quality_preset=self.settings.get("whisper_quality", "balanced"),
        )
        self._report_progress(
            f"Loading Whisper model '{self._transcriber.model_size}' "
            "(first use downloads it — this can take a few minutes)..."
        )
        self._transcriber._ensure_loaded()
        self._report_progress(f"{label} with '{self._transcriber.model_size}' model...")
        return self._transcriber.transcribe_file(
            audio_path,
            on_progress=self._make_progress_reporter(label),
            initial_prompt=prompt,
        )

    # ─── Import Wizard: multi-part, resumable ────────────────────

    @staticmethod
    def _shift(seg: TranscriptSegment, offset: float) -> TranscriptSegment:
        d = seg.to_dict()
        d["start"] = seg.start + offset
        d["end"] = seg.end + offset
        return TranscriptSegment(**d)

    def _metadata_from_plan(self, plan: ImportPlan) -> MeetingMetadata:
        docs = list(plan.requested_documents)
        if plan.custom_document.strip():
            docs.append("custom")
        instructions = plan.document_instructions.strip()
        if plan.custom_document.strip():
            instructions = (instructions + "\n" if instructions else "") + \
                f"Custom document requested: {plan.custom_document.strip()}"
        title = plan.title.strip() or os.path.splitext(plan.parts[0].name)[0]
        return MeetingMetadata(
            title=title,
            date=plan.date or datetime.now().strftime("%Y-%m-%d"),
            app_version="1.0.0",
            client=plan.client.strip(),
            engagement=plan.engagement.strip(),
            meeting_type=plan.meeting_type,
            our_role=plan.our_role,
            topic=plan.topic.strip(),
            participants=[p.strip() for p in plan.participants if p.strip()],
            key_terms=plan.key_terms,
            language=plan.language_label if plan.language else "Auto-detect",
            document_language=plan.document_language,
            document_instructions=instructions,
            requested_documents=docs,
        )

    def run_import(self, plan: ImportPlan) -> None:
        """Transcribe one or more media files as ONE meeting.

        Each part is decoded and transcribed on its own (keeps memory low),
        its timestamps are shifted onto one continuous timeline, and progress
        is saved after every part so an interrupted import resumes where it
        stopped — finished parts are never transcribed again.
        """
        from src.core.media_import import decode_media_to_wav
        from src.core.transcriber import WhisperTranscriber

        if not plan.parts:
            raise ValueError("No files to import")

        job_path = os.path.join(_import_jobs_dir(), f"import_{plan.job_id}.json")
        job = safe_read_json(job_path) or {}
        if job.get("job_id") != plan.job_id:
            job = {"job_id": plan.job_id, "parts": {}, "created": time.time()}
        job["plan"] = plan.to_dict()          # latest wizard answers win
        job["status"] = "running"
        safe_write_json(job_path, job)
        self._current_import_job = job_path

        self.meeting = Meeting(metadata=self._metadata_from_plan(plan))
        self._working_audio_paths = []
        prompt = plan.vocabulary_prompt(WhisperTranscriber.CODE_MIXED_PROMPT)
        temp_dir = str(get_temp_dir())
        n = len(plan.parts)

        self._set_state(PipelineState.TRANSCRIBING)
        all_segments: List[TranscriptSegment] = []
        source_files: List[dict] = []
        offset = 0.0

        for i, part in enumerate(plan.parts):
            label = f"Part {i + 1} of {n}" if n > 1 else "Transcribing"
            wav = os.path.join(temp_dir, f"import_{plan.job_id}_part{i + 1}.wav")
            self._working_audio_paths.append(wav)
            if not os.path.exists(wav):
                self._report_progress(f"{label}: reading audio from '{part.name}'...")
                wav, dur = decode_media_to_wav(part.path, wav)
            else:
                dur = wav_duration(wav)

            done = job["parts"].get(str(i))
            if done is not None:
                self._report_progress(f"{label}: already transcribed earlier — reused")
                segs = [TranscriptSegment(**d) for d in done.get("segments", [])]
            else:
                base = list(all_segments)

                def _partial(segs_so_far, base=base, off=offset):
                    self._default_partial(
                        base + [self._shift(x, off) for x in segs_so_far])

                segs = self._transcribe_to_segments(
                    wav, language=plan.language, prompt=prompt,
                    job_key=f"{part.path}|{part.size}", on_partial=_partial,
                    label=label,
                )
                if self._transcriber is not None and \
                        getattr(self._transcriber, "cancel_requested", False):
                    all_segments.extend(self._shift(x, offset) for x in segs)
                    self.meeting.transcript = all_segments
                    self._finish_meeting_audio(source_files, offset)
                    self._set_state(PipelineState.IDLE)
                    self._report_progress(
                        f"Import paused at {label.lower()} — finished parts are "
                        "kept. Resume it any time from the Home screen (Unfinished imports).")
                    return
                job["parts"][str(i)] = {"duration": dur,
                                        "segments": [x.to_dict() for x in segs]}
                safe_write_json(job_path, job)

            all_segments.extend(self._shift(x, offset) for x in segs)
            source_files.append({
                "name": part.name, "path": part.path, "duration": round(dur, 2),
                "offset": round(offset, 2), "recorded_at": part.recorded_at,
                "gap_before": part.gap_before,
            })
            offset += dur
            self._default_partial(list(all_segments))

        self.meeting.transcript = all_segments
        self._finish_meeting_audio(source_files, offset)

        if self.settings.get("diarization_enabled") and self.settings.get("hf_token"):
            self._set_state(PipelineState.DIARIZING)
            self._report_progress("Identifying speakers...")
            self._diarize(self.meeting.audio_path)

        if self.settings.get_llm_backend() != LLMBackend.NONE:
            self._set_state(PipelineState.STRUCTURING)
            self._structure()

        job["status"] = "transcribed"
        safe_write_json(job_path, job)
        self._set_state(PipelineState.IDLE)
        self._report_progress(
            f"Transcription complete — {len(all_segments)} segments, "
            f"{format_duration(offset)}")

    def _finish_meeting_audio(self, source_files: List[dict], total: float) -> None:
        """Join the decoded parts into one WAV for playback and the bundle."""
        wavs = [w for w in self._working_audio_paths if os.path.exists(w)]
        if not wavs:
            return
        if len(wavs) == 1:
            combined = wavs[0]
        else:
            self._report_progress("Joining parts into one recording...")
            combined = os.path.join(str(get_temp_dir()),
                                    os.path.basename(wavs[0]).replace("_part1", "_combined"))
            concat_wavs_streaming(wavs, combined)
            self._working_audio_paths.append(combined)
        self._working_audio_path = combined
        self.meeting.audio_path = combined
        self.meeting.metadata.source_files = source_files
        total = total or wav_duration(combined)
        self.meeting.metadata.duration = format_duration(total)
        self.meeting.metadata.duration_seconds = total

    # ─── Transcript completeness ─────────────────────────────────

    def transcript_coverage(self, meeting: Optional[Meeting] = None) -> Optional[dict]:
        """{'end', 'duration', 'missing', 'incomplete'} for a meeting's transcript.

        A transcript is incomplete when more than 5 minutes at the END of the
        audio have no text — e.g. a Groq job stopped at the hourly quota limit
        and the partial result was saved.
        """
        from src.core.request_queue import INCOMPLETE_GAP_SEC
        m = meeting or self.meeting
        if not m or not m.metadata.duration_seconds:
            return None
        end = max((x.end for x in m.transcript), default=0.0)
        dur = float(m.metadata.duration_seconds)
        missing = max(0.0, dur - end)
        return {"end": end, "duration": dur, "missing": missing,
                "incomplete": missing > INCOMPLETE_GAP_SEC}

    def _language_code(self) -> Optional[str]:
        """Spoken-language code for this meeting (wizard choice, else Settings)."""
        from src.core.import_plan import LANGUAGES
        label = (self.meeting.metadata.language if self.meeting else "") or ""
        code = next((c for lbl, c in LANGUAGES if lbl == label), None)
        return code or self.settings.get("transcription_language")

    def complete_missing_transcript(self) -> int:
        """Transcribe ONLY the untranscribed end of the current meeting and
        append it on the same timeline. If the meeting is already saved, the
        bundle, transcript .md and request file are updated in place (no
        second copy). Returns the number of segments added."""
        from src.core.media_parts import extract_clip
        from src.core.transcriber import WhisperTranscriber
        from src.utils.audio_utils import save_wav_chunk

        cov = self.transcript_coverage()
        if not cov or not cov["incomplete"]:
            return 0
        m = self.meeting
        if not m.audio_path or not os.path.exists(m.audio_path):
            raise ValueError("This meeting's audio isn't available — reopen it from Home.")

        start = cov["end"]
        self._set_state(PipelineState.TRANSCRIBING)
        self._report_progress(
            f"Reading audio {_ts(start)}–{_ts(cov['duration'])} "
            f"({round(cov['missing'] / 60)} min)...")
        audio = extract_clip(m.audio_path, start, cov["duration"] - start + 1.0)
        wav = os.path.join(str(get_temp_dir()), f"complete_{int(start)}_{int(time.time())}.wav")
        save_wav_chunk(audio, wav, 16000)
        self._working_audio_paths.append(wav)

        base = list(m.transcript)
        terms = [t for t in (m.metadata.key_terms or [])]
        prompt = WhisperTranscriber.CODE_MIXED_PROMPT
        if terms:
            prompt = (prompt + " " + ", ".join(terms) + ".")[:600]

        def _partial(segs, base=base):
            self._default_partial(base + [self._shift(x, start) for x in segs])

        segs = self._transcribe_to_segments(
            wav, language=self._language_code(), prompt=prompt,
            job_key=f"{m.bundle_path or m.metadata.title}|from|{int(start)}",
            on_partial=_partial, label="Missing part")
        new = [self._shift(x, start) for x in segs if x.end > 0.5]
        m.transcript = base + new
        self._default_partial(m.transcript)
        cancelled = self._transcriber is not None and \
            getattr(self._transcriber, "cancel_requested", False)

        if m.bundle_path and os.path.exists(m.bundle_path):
            self._update_saved_meeting()
        try:
            os.remove(wav)
        except OSError:
            pass
        self._set_state(PipelineState.IDLE)
        after = self.transcript_coverage()
        if cancelled or (after and after["incomplete"]):
            self._report_progress(
                f"Added {len(new)} segments — still incomplete up to "
                f"{_ts(after['end'] if after else 0)}. Run it again to continue.")
        else:
            self._report_progress(f"Missing part transcribed — {len(new)} segments added. "
                                  "Transcript is complete.")
        return len(new)

    def _update_saved_meeting(self) -> None:
        """Write the current transcript back into the SAME saved meeting."""
        from src.core.request_queue import sync_coverage_warning
        m = self.meeting
        self._report_progress("Updating the saved meeting...")
        self._bundle_manager.update_bundle(m.bundle_path, m)
        base = os.path.splitext(m.bundle_path)[0]
        try:
            self.export_transcript(base + ".md", fmt="md")
        except Exception as e:
            logger.warning(f"Could not rewrite transcript .md: {e}")
        cov = self.transcript_coverage() or {"end": 0, "duration": 0}
        sync_coverage_warning(base + ".request.md", cov["end"], cov["duration"])
        try:
            self._database.index_bundle(
                bundle_path=m.bundle_path, title=m.metadata.title,
                date=m.metadata.date, duration=m.metadata.duration,
                duration_seconds=m.metadata.duration_seconds,
                speakers=", ".join(m.metadata.attendees or m.metadata.participants),
                transcript_text=" ".join(x.text for x in m.transcript),
                file_size_mb=os.path.getsize(m.bundle_path) / 1024 / 1024,
            )
        except Exception as e:
            logger.warning(f"Could not re-index meeting: {e}")

    def _cloud_piece_progress(self, part_key: str, language) -> Optional[tuple]:
        """(pieces_done, pieces_total) of the Groq job for one import part,
        or None if that part has no cloud job yet (or runs on local Whisper).
        Groq splits audio into ~10-minute pieces; each finished piece is
        saved, so this is exactly what a resume will NOT redo."""
        import hashlib
        folder = str(get_app_data_dir() / "cloud_jobs")
        if not os.path.isdir(folder):
            return None
        found = None
        for name in os.listdir(folder):
            if not (name.startswith("groq_job_") and name.endswith(".json")):
                continue
            job = safe_read_json(os.path.join(folder, name)) or {}
            if job.get("job_key") == part_key:
                found = job
                break
        if found is None:
            # Job files written before job_key was stored: rebuild the id.
            key = f"{part_key}|{self.settings.get('groq_model', 'whisper-large-v3-turbo')}|{language}"
            job_id = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
            found = safe_read_json(os.path.join(folder, f"groq_job_{job_id}.json"))
        if not found or not found.get("n_chunks"):
            return None
        return len(found.get("completed", {})), int(found["n_chunks"])

    def import_job_progress(self, job: dict, plan: ImportPlan) -> dict:
        """Human-meaningful progress of an unfinished import: whole files
        done, and 10-minute pieces done inside the file that was running."""
        parts_done = job.get("parts", {})
        total = len(plan.parts)
        done_files = len(parts_done)
        current = next((i for i in range(total) if str(i) not in parts_done), None)
        pieces = None
        if current is not None:
            p = plan.parts[current]
            pieces = self._cloud_piece_progress(f"{p.path}|{p.size}", plan.language)
        # Rough time still to transcribe (audio minutes, not wall time)
        left = 0.0
        for i, p in enumerate(plan.parts):
            if str(i) in parts_done:
                continue
            dur = p.duration or 0.0
            if i == current and pieces and pieces[1]:
                dur = dur * (1 - pieces[0] / pieces[1])
            left += dur
        return {"files_done": done_files, "files_total": total,
                "current_part": current, "pieces": pieces,
                "minutes_left": round(left / 60)}

    @staticmethod
    def describe_import_progress(prog: dict) -> str:
        """One line like 'File 1 of 1: 12 of 15 pieces transcribed (~25 min left)'."""
        total = prog["files_total"]
        cur = prog["current_part"]
        bits = []
        if total > 1:
            bits.append(f"{prog['files_done']} of {total} files finished")
        if cur is not None:
            where = f"file {cur + 1} of {total}: " if total > 1 else ""
            if prog["pieces"]:
                d, n = prog["pieces"]
                bits.append(f"{where}{d} of {n} 10-min pieces transcribed")
            else:
                bits.append(f"{where}not started yet")
        if prog["minutes_left"]:
            bits.append(f"~{prog['minutes_left']} min of audio left")
        return " · ".join(bits) or "ready to finish"

    def pending_import_jobs(self) -> List[dict]:
        """Unfinished imports that can be resumed (app closed, or Cancel
        pressed mid-import). The import running right now is excluded."""
        out = []
        folder = _import_jobs_dir()
        active = self._current_import_job if self.state != PipelineState.IDLE else None
        for name in os.listdir(folder):
            if not name.startswith("import_") or not name.endswith(".json"):
                continue
            path = os.path.join(folder, name)
            if active and os.path.abspath(path) == os.path.abspath(active):
                continue
            job = safe_read_json(path) or {}
            if job.get("status") != "running" or "plan" not in job:
                continue
            try:
                plan = ImportPlan.from_dict(job["plan"])
            except Exception:
                continue
            if not all(os.path.exists(p.path) for p in plan.parts):
                continue  # source files moved/deleted — can't resume
            try:
                prog = self.import_job_progress(job, plan)
            except Exception as e:
                logger.warning(f"Could not read import progress: {e}")
                prog = {"files_done": len(job.get("parts", {})),
                        "files_total": len(plan.parts), "current_part": None,
                        "pieces": None, "minutes_left": 0}
            out.append({"path": path, "plan": plan,
                        "done": prog["files_done"], "total": prog["files_total"],
                        "progress": prog,
                        "summary": self.describe_import_progress(prog),
                        "updated": os.path.getmtime(path)})
        out.sort(key=lambda j: j["updated"], reverse=True)
        return out

    @staticmethod
    def discard_import_job(job_path: str) -> None:
        try:
            if job_path and os.path.exists(job_path):
                os.remove(job_path)
        except OSError:
            pass

    def _diarize(self, audio_path: str) -> None:
        """Run speaker diarization."""
        from src.core.diarizer import SpeakerDiarizer

        hf_token = self.settings.get("hf_token", "")
        if not hf_token:
            self._report_progress("Skipping diarization — no HuggingFace token")
            return

        try:
            self._diarizer = SpeakerDiarizer(
                hf_token=hf_token,
                max_speakers=self.settings.get("max_speakers", 10),
            )

            speaker_segments = self._diarizer.diarize(audio_path)
            self.meeting.transcript = self._diarizer.assign_speakers(
                self.meeting.transcript, speaker_segments
            )

            # Update attendees from speaker labels
            self.meeting.metadata.attendees = self.meeting.speaker_list

            if self.on_transcript_update:
                self.on_transcript_update(self.meeting.transcript)

        except Exception as e:
            self._report_progress(f"Diarization failed: {e}")
            logger.error(f"Diarization error: {e}", exc_info=True)

    def _structure(self) -> None:
        """Extract structured data using LLM."""
        from src.core.structurer import MeetingStructurer

        backend = self.settings.get_llm_backend()

        chars = sum(len(x.text) for x in (self.meeting.transcript or []))
        if chars > MAX_ANALYSIS_CHARS:
            self._report_progress(
                "Analysis panel skipped: the transcript is too long for a single "
                "AI request. The full transcript is saved for document writing.")
            return
        # Groq reuses the transcription key when no separate key is set.
        api_key = self.settings.get("llm_api_key", "")
        if backend == LLMBackend.GROQ and not api_key:
            api_key = self.settings.get("groq_api_key", "")

        try:
            self._structurer = MeetingStructurer(
                backend=backend,
                model=self.settings.get("llm_model", ""),
                api_key=api_key,
                base_url=self.settings.get("ollama_base_url", "http://localhost:11434"),
            )

            self._report_progress("Extracting action items and decisions...")
            structured = self._structurer.extract_structure(self.meeting.transcript)
            self.meeting.structured = structured

        except Exception as e:
            self._report_progress(f"Structuring failed: {e}")
            logger.error(f"Structuring error: {e}", exc_info=True)

    # ─── Document Generation ─────────────────────────────────────

    def generate_documents(self, template_path: str,
                           output_dir: Optional[str] = None,
                           export_pdf: bool = True) -> List[str]:
        """
        Generate documents from a template and the current meeting data.

        Args:
            template_path: Path to .docx template.
            output_dir: Output directory. Defaults to temp dir.
            export_pdf: Also export as PDF.

        Returns:
            List of generated file paths.
        """
        if not self.meeting:
            logger.warning("No meeting data to generate documents from")
            return []

        self._set_state(PipelineState.RENDERING)

        if output_dir is None:
            output_dir = str(get_temp_dir())

        generated = []

        # Generate DOCX
        template_name = os.path.splitext(os.path.basename(template_path))[0]
        docx_path = os.path.join(output_dir, f"{template_name}.docx")

        self._report_progress(f"Generating {template_name}...")
        docx_path = self._template_engine.render(
            template_path, self.meeting, docx_path
        )
        generated.append(docx_path)

        # Export PDF
        if export_pdf:
            self._report_progress("Exporting PDF...")
            pdf_path = self._template_engine.export_pdf(docx_path)
            if pdf_path:
                generated.append(pdf_path)

        self._set_state(PipelineState.IDLE)
        return generated

    def export_transcript(self, output_path: str, fmt: str = "txt") -> str:
        """
        Export the transcript as a plain text or Markdown file.

        Free path: paste the result into claude.ai (or any chat AI) and ask
        for whatever document you want — no API key, no cost.

        Args:
            output_path: Destination file path.
            fmt: "txt" or "md".

        Returns:
            The output path.
        """
        if not self.meeting or not self.meeting.transcript:
            raise ValueError("No transcript to export")

        meta = self.meeting.metadata
        lines = []
        parts = meta.source_files if len(meta.source_files or []) > 1 else []

        def part_markers(seg_start, state):
            """Yield a heading each time the transcript enters the next part."""
            out = []
            while state["next"] < len(parts) and \
                    seg_start >= parts[state["next"]].get("offset", 0) - 0.5:
                f = parts[state["next"]]
                state["next"] += 1
                out.append((state["next"], f))
            return out

        if fmt == "md":
            lines.append(f"# {meta.title}")
            lines.append("")
            lines.append(f"**Date:** {meta.date}  ")
            lines.append(f"**Duration:** {meta.duration}  ")
            if meta.attendees:
                lines.append(f"**Speakers:** {', '.join(meta.attendees)}  ")
            lines.append("")
            cov = self.transcript_coverage()
            if cov and cov["incomplete"]:
                from src.core.request_queue import coverage_warning
                lines += [coverage_warning(cov["end"], cov["duration"]), ""]
            lines += context_markdown(meta)
            lines.append("## Transcript")
            lines.append("")
            state = {"next": 0}
            for seg in self.meeting.transcript:
                for k, f in part_markers(seg.start, state):
                    lines.append(f"### Part {k} — `{f.get('name')}` "
                                 f"(starts at {_ts(f.get('offset', 0))})")
                    lines.append("")
                speaker = seg.speaker or "Speaker"
                lines.append(f"**[{_ts(seg.start)}] {speaker}:** {seg.text}")
                lines.append("")
        else:
            lines.append(f"{meta.title}")
            lines.append(f"Date: {meta.date}   Duration: {meta.duration}")
            if meta.attendees:
                lines.append(f"Speakers: {', '.join(meta.attendees)}")
            for ln in context_markdown(meta, heading="Meeting context:"):
                lines.append(ln.replace("**", ""))
            lines.append("=" * 60)
            lines.append("")
            state = {"next": 0}
            for seg in self.meeting.transcript:
                for k, f in part_markers(seg.start, state):
                    lines.append(f"── Part {k}: {f.get('name')} "
                                 f"(starts at {_ts(f.get('offset', 0))}) ──")
                speaker = seg.speaker or "Speaker"
                lines.append(f"[{_ts(seg.start)}] {speaker}: {seg.text}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"Transcript exported: {output_path}")
        return output_path

    def generate_ai_document(self, instruction: str,
                             output_dir: Optional[str] = None,
                             export_pdf: bool = False) -> List[str]:
        """
        Ask the configured LLM to write a document from the transcript.

        This is the free-form path: the user describes what they want
        ("formal MoM in Bahasa", "client follow-up email") and the AI writes
        it. Distinct from generate_documents(), which fills fixed templates.

        Returns:
            List of generated file paths (.docx, optionally .pdf, .md).
        """
        from src.core.structurer import MeetingStructurer
        from src.core.markdown_docx import markdown_to_docx

        if not self.meeting or not self.meeting.transcript:
            raise ValueError("No transcript available — process a meeting first.")

        backend = self.settings.get_llm_backend()
        if backend == LLMBackend.NONE:
            raise ValueError(
                "No AI backend configured. Go to Settings → AI Document "
                "Structuring and select one (Groq is free)."
            )

        # Groq LLM reuses the Groq STT key unless a separate key is set.
        api_key = self.settings.get("llm_api_key", "")
        if backend == LLMBackend.GROQ and not api_key:
            api_key = self.settings.get("groq_api_key", "")

        structurer = MeetingStructurer(
            backend=backend,
            model=self.settings.get("llm_model", ""),
            api_key=api_key,
            base_url=self.settings.get("ollama_base_url", "http://localhost:11434"),
        )

        self._set_state(PipelineState.RENDERING)
        self._report_progress(f"Asking {backend.value} to write your document...")

        markdown = structurer.generate_document(
            transcript=self.meeting.transcript,
            instruction=instruction,
            structured=self.meeting.structured,
            meeting_title=self.meeting.metadata.title,
            meeting_date=self.meeting.metadata.date,
        )

        if output_dir is None:
            output_dir = str(get_temp_dir())
        os.makedirs(output_dir, exist_ok=True)

        safe = "".join(c for c in instruction[:40] if c.isalnum() or c in " -_").strip()
        safe = safe.replace(" ", "_") or "ai_document"
        base = os.path.join(output_dir, safe)

        generated = []

        # Keep the raw markdown too — useful for editing / re-use
        md_path = base + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown)
        generated.append(md_path)

        self._report_progress("Formatting document...")
        docx_path = markdown_to_docx(
            markdown, base + ".docx",
            title=self.meeting.metadata.title,
            subtitle=f"{self.meeting.metadata.date} · {self.meeting.metadata.duration}",
        )
        generated.append(docx_path)

        if export_pdf:
            pdf_path = self._template_engine.export_pdf(docx_path)
            if pdf_path:
                generated.append(pdf_path)

        self._set_state(PipelineState.IDLE)
        self._report_progress(f"Document ready: {os.path.basename(docx_path)}")
        return generated

    # ─── Bundle Management ───────────────────────────────────────

    def save_bundle(self, output_dir: Optional[str] = None,
                    requested_documents: Optional[List[str]] = None) -> str:
        """Save the current meeting as a .mscribe bundle.

        Also writes, next to the bundle:
          - <name>.md          a plain-text transcript (no unzip needed)
          - <name>.request.md  what documents the user asked for, if any

        The request file is how an AI assistant with access to this folder
        (Cowork/Claude) knows which documents to produce for this meeting.
        """
        if not self.meeting:
            raise ValueError("No meeting to save")

        self._set_state(PipelineState.SAVING)

        if output_dir is None:
            output_dir = self.settings.get_project_folder()

        self._report_progress("Saving project bundle...")
        bundle_path = self._bundle_manager.create_bundle(self.meeting, output_dir)

        # Companion transcript (.md) — readable without unzipping the bundle
        base = os.path.splitext(bundle_path)[0]
        try:
            if self.meeting.transcript:
                self.export_transcript(base + ".md", fmt="md")
        except Exception as e:
            logger.warning(f"Could not write companion transcript: {e}")

        # Document request file — the hand-off to an AI assistant
        if requested_documents:
            try:
                self._write_document_request(base, bundle_path, requested_documents)
            except Exception as e:
                logger.warning(f"Could not write document request: {e}")

        # Index in database
        transcript_text = " ".join(s.text for s in self.meeting.transcript)
        self._database.index_bundle(
            bundle_path=bundle_path,
            title=self.meeting.metadata.title,
            date=self.meeting.metadata.date,
            duration=self.meeting.metadata.duration,
            duration_seconds=self.meeting.metadata.duration_seconds,
            speakers=", ".join(self.meeting.metadata.attendees
                               or self.meeting.metadata.participants),
            transcript_text=transcript_text,
            file_size_mb=os.path.getsize(bundle_path) / 1024 / 1024,
        )

        # Housekeeping: the audio now lives inside the .mscribe, so the
        # working copies (decoded import, recording chunks, normalized WAV)
        # are redundant. Without this they accumulate at ~115 MB/audio-hour.
        if self.settings.get("cleanup_after_save", True):
            try:
                from src.utils.housekeeping import cleanup_after_save
                session_id = None
                if self._capture_engine is not None:
                    session_id = self._capture_engine.session_id
                freed = cleanup_after_save(
                    session_id=session_id,
                    audio_paths=[self._working_audio_path, self.meeting.audio_path]
                    + list(self._working_audio_paths),
                )
                if freed > 1:
                    self._report_progress(
                        f"Bundle saved — freed {freed:.0f} MB of working files"
                    )
                self._working_audio_path = None
                self._working_audio_paths = []
            except Exception as e:
                logger.warning(f"Post-save cleanup skipped: {e}")

        # A saved import no longer needs its resume manifest.
        if self._current_import_job:
            self.discard_import_job(self._current_import_job)
            self._current_import_job = None

        self._set_state(PipelineState.IDLE)
        self._report_progress(f"Bundle saved: {bundle_path}")
        return bundle_path

    def discard_meeting(self) -> float:
        """Abandon the current meeting and delete its temporary audio.

        Call this when the user cancels an import or discards a recording
        without saving — otherwise the decoded audio stays on disk forever.

        Returns:
            Megabytes freed.
        """
        from src.utils.housekeeping import cleanup_after_save

        session_id = None
        if self._capture_engine is not None:
            session_id = self._capture_engine.session_id

        paths = [self._working_audio_path] + list(self._working_audio_paths)
        if self.meeting:
            paths.append(self.meeting.audio_path)

        freed = cleanup_after_save(session_id=session_id, audio_paths=paths)
        self._working_audio_path = None
        self._working_audio_paths = []
        if self._current_import_job:
            self.discard_import_job(self._current_import_job)
            self._current_import_job = None
        self.meeting = None
        self._set_state(PipelineState.IDLE)
        logger.info(f"Meeting discarded, freed {freed:.1f} MB")
        return freed

    # Document types offered when saving a bundle (shared with the wizard).
    # key -> (label, instruction for whoever writes the document)
    DOCUMENT_TYPES = dict(DOCUMENTS)

    def _write_document_request(self, base: str, bundle_path: str,
                                requested: List[str]) -> str:
        """Write the .request.md hand-off file next to the bundle.

        It carries everything needed to write the documents later without
        guessing: the requested documents, the meeting context entered in the
        Import Wizard (client, engagement, roles, participants, key terms,
        instructions), and the rules for staying faithful to the transcript.
        """
        meta = self.meeting.metadata
        transcript_md = os.path.basename(base + ".md")
        stem = os.path.basename(base)

        lines = [
            f"# Document Request — {meta.title}",
            "",
            "> Status: **PENDING** — change to DONE once the documents exist.",
            "",
            f"- **Meeting:** {meta.title}",
            f"- **Date:** {meta.date}",
            f"- **Duration:** {meta.duration}",
            f"- **Bundle:** `{os.path.basename(bundle_path)}`",
            f"- **Transcript:** `{transcript_md}` (read this — no unzip needed)",
            "",
        ]
        cov = self.transcript_coverage()
        if cov and cov["incomplete"]:
            from src.core.request_queue import coverage_warning
            lines[3:3] = [coverage_warning(cov["end"], cov["duration"])]
        lines += context_markdown(meta)
        lines += ["## Documents requested", ""]
        for key in requested:
            if key == "custom":
                custom = next((ln.split(":", 1)[1].strip() for ln in
                               (meta.document_instructions or "").splitlines()
                               if ln.startswith("Custom document requested:")), "")
                label, instruction = ("Custom document", custom or "As described by the user.")
            else:
                label, instruction = self.DOCUMENT_TYPES.get(
                    key, (key, f"A document of type: {key}"))
            lines += [f"### {label}", "", instruction, "",
                      f"- Output file: `{stem}_{key}.docx`", ""]

        lang = meta.document_language
        lang_rule = ("Write in the dominant language of the transcript."
                     if not lang or lang == "Same as the meeting"
                     else f"Write the documents in {lang}.")
        lines += [
            "## Instructions for the AI assistant",
            "",
            f"1. Read `{transcript_md}` in this folder, including its Meeting context section.",
            "2. Produce each document listed above as a .docx in this same folder.",
            "3. Use only facts present in the transcript — never invent names, "
            "dates or commitments. Write 'Not discussed' where information is "
            "missing, and flag unclear passages for verification instead of guessing.",
            "4. Use the key terms and participant names above for correct spelling; "
            "the transcript may contain misheard versions of them.",
            f"5. {lang_rule}",
            "6. Cite timestamps (e.g. [1:02:15]) for decisions, action items and open points.",
            "7. When finished, change Status at the top of this file to **DONE**.",
            "",
        ]

        request_path = base + ".request.md"
        with open(request_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"Document request written: {request_path}")
        return request_path

    def generate_requested_documents(self, requested: List[str],
                                     output_dir: Optional[str] = None) -> List[str]:
        """Generate the requested documents in-app via the configured LLM.

        Used by the free tier (Groq) so the whole flow completes without any
        external assistant. Returns the list of generated file paths.
        """
        generated: List[str] = []
        for key in requested:
            label, instruction = self.DOCUMENT_TYPES.get(
                key, (key, f"A document of type: {key}")
            )
            self._report_progress(f"Generating {label}...")
            try:
                paths = self.generate_ai_document(instruction, output_dir=output_dir)
                generated.extend(paths)
            except Exception as e:
                logger.error(f"Failed to generate {label}: {e}")
                self._report_progress(f"Could not generate {label}: {e}")
        return generated

    def open_bundle(self, bundle_path: str) -> Meeting:
        """Open an existing .mscribe bundle."""
        self.meeting = self._bundle_manager.open_bundle(bundle_path)
        return self.meeting

    # ─── Database ────────────────────────────────────────────────

    def search_meetings(self, query: str):
        """Search past meetings."""
        return self._database.search(query)

    def list_meetings(self):
        """List all indexed meetings."""
        return self._database.list_meetings()

    def get_database_stats(self):
        """Get meeting database statistics."""
        return self._database.get_stats()
