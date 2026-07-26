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
from src.utils.audio_utils import concatenate_chunks, format_duration, get_audio_duration
from src.utils.file_utils import get_temp_dir

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

        # Callbacks
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_progress: Optional[Callable[[str], None]] = None
        self._on_level_change: Optional[Callable[[float], None]] = None
        self.on_transcript_update: Optional[Callable[[List[TranscriptSegment]], None]] = None

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

        # Create capture engine
        self._capture_engine = AudioCaptureEngine(
            source=source,
            mic_device=self.settings.get("mic_device_index"),
            system_device=self.settings.get("system_device_index"),
        )

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
            self._report_progress("Extracting action items and decisions...")
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
        """Run transcription using the configured backend.

        ROLLBACK MECHANISM: if the cloud backend (Groq) fails for any reason
        — bad key, quota exhausted, network down — and fallback is enabled,
        we automatically re-run the file through the local Whisper backend
        so the user always ends up with a transcript.
        """
        backend = self.settings.get("stt_backend", "local")

        if backend == "groq":
            try:
                self._transcribe_groq(audio_path)
                return
            except Exception as e:
                # User cancellation is not a failure — don't roll over to local.
                if self._transcriber is not None and \
                        getattr(self._transcriber, "cancel_requested", False):
                    raise

                logger.error(f"Groq backend failed: {e}", exc_info=True)

                if not self.settings.get("cloud_stt_fallback_local", True):
                    raise

                # Keep any partial cloud segments as a floor; the local pass
                # will overwrite them with a complete transcript if it succeeds.
                partial = getattr(e, "partial_segments", [])
                if partial:
                    self.meeting.transcript = partial
                    if self.on_transcript_update:
                        self.on_transcript_update(list(partial))

                self._report_progress(
                    f"Cloud transcription failed ({str(e)[:80]}) — "
                    "rolling back to local Whisper..."
                )

        self._transcribe_local(audio_path)

    def _transcribe_groq(self, audio_path: str) -> None:
        """Transcribe via Groq's hosted Whisper (free tier friendly).

        Long jobs are crash-safe: every completed chunk is persisted to a
        job file under the app data dir, so closing the app mid-wait (e.g.
        during the hourly quota pause) loses nothing — clicking Process
        again resumes from the last finished chunk.
        """
        from src.core.groq_transcriber import GroqTranscriber
        from src.core.transcriber import WhisperTranscriber
        from src.utils.file_utils import get_app_data_dir

        api_key = self.settings.get("groq_api_key", "")
        if not api_key:
            raise ValueError(
                "No Groq API key configured. Add one in Settings → "
                "Transcription Backend, or switch backend to Local."
            )

        jobs_dir = str(get_app_data_dir() / "cloud_jobs")

        self._transcriber = GroqTranscriber(
            api_key=api_key,
            model=self.settings.get("groq_model", "whisper-large-v3-turbo"),
            language=self.settings.get("transcription_language"),
            prompt=WhisperTranscriber.CODE_MIXED_PROMPT,
            resume_dir=jobs_dir,
        )
        self._transcriber.on_status = self._report_progress

        # Stream the growing transcript into the UI as chunks finish, and
        # keep meeting.transcript current so even a hard crash mid-job
        # leaves the meeting object with everything transcribed so far.
        def _on_partial(segments_so_far):
            self.meeting.transcript = segments_so_far
            if self.on_transcript_update:
                try:
                    self.on_transcript_update(list(segments_so_far))
                except Exception:
                    pass

        self._transcriber.on_partial = _on_partial

        self._report_progress("Transcribing via Groq cloud (fast)...")
        segments = self._transcriber.transcribe_file(
            audio_path,
            on_progress=self._make_progress_reporter("Cloud transcribing"),
        )

        self.meeting.transcript = segments
        if self.on_transcript_update:
            self.on_transcript_update(segments)

    def _transcribe_local(self, audio_path: str) -> None:
        """Transcribe with local faster-whisper."""
        from src.core.transcriber import WhisperTranscriber

        model_size = self.settings.get("whisper_model", "auto")
        if model_size == "auto":
            model_size = None  # WhisperTranscriber will auto-detect

        language = self.settings.get("transcription_language")
        quality_preset = self.settings.get("whisper_quality", "balanced")

        self._transcriber = WhisperTranscriber(
            model_size=model_size,
            language=language,
            quality_preset=quality_preset,
        )

        self._report_progress(
            f"Loading Whisper model '{self._transcriber.model_size}' "
            "(first use downloads it — this can take a few minutes)..."
        )
        self._transcriber._ensure_loaded()
        self._report_progress(
            f"Transcribing with '{self._transcriber.model_size}' model..."
        )

        segments = self._transcriber.transcribe_file(
            audio_path,
            on_progress=self._make_progress_reporter(),
        )
        self.meeting.transcript = segments

        if self.on_transcript_update:
            self.on_transcript_update(segments)

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

        try:
            self._structurer = MeetingStructurer(
                backend=backend,
                model=self.settings.get("llm_model", "llama3.1:8b"),
                api_key=self.settings.get("llm_api_key", ""),
                base_url=self.settings.get("ollama_base_url", "http://localhost:11434"),
            )

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

        if fmt == "md":
            lines.append(f"# {meta.title}")
            lines.append("")
            lines.append(f"**Date:** {meta.date}  ")
            lines.append(f"**Duration:** {meta.duration}  ")
            if meta.attendees:
                lines.append(f"**Speakers:** {', '.join(meta.attendees)}  ")
            lines.append("")
            lines.append("## Transcript")
            lines.append("")
            for seg in self.meeting.transcript:
                ts = f"{int(seg.start // 60)}:{int(seg.start % 60):02d}"
                speaker = seg.speaker or "Speaker"
                lines.append(f"**[{ts}] {speaker}:** {seg.text}")
                lines.append("")
        else:
            lines.append(f"{meta.title}")
            lines.append(f"Date: {meta.date}   Duration: {meta.duration}")
            if meta.attendees:
                lines.append(f"Speakers: {', '.join(meta.attendees)}")
            lines.append("=" * 60)
            lines.append("")
            for seg in self.meeting.transcript:
                ts = f"{int(seg.start // 60)}:{int(seg.start % 60):02d}"
                speaker = seg.speaker or "Speaker"
                lines.append(f"[{ts}] {speaker}: {seg.text}")

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
            speakers=", ".join(self.meeting.metadata.attendees),
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
                    audio_paths=[self._working_audio_path, self.meeting.audio_path],
                )
                if freed > 1:
                    self._report_progress(
                        f"Bundle saved — freed {freed:.0f} MB of working files"
                    )
                self._working_audio_path = None
            except Exception as e:
                logger.warning(f"Post-save cleanup skipped: {e}")

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

        paths = [self._working_audio_path]
        if self.meeting:
            paths.append(self.meeting.audio_path)

        freed = cleanup_after_save(session_id=session_id, audio_paths=paths)
        self._working_audio_path = None
        self.meeting = None
        self._set_state(PipelineState.IDLE)
        logger.info(f"Meeting discarded, freed {freed:.1f} MB")
        return freed

    # Document types offered when saving a bundle.
    # key -> (label, instruction sent to the AI)
    DOCUMENT_TYPES = {
        "mom": (
            "Minutes of Meeting (MoM)",
            "A formal Minutes of Meeting document with: title, date, duration, "
            "attendees, agenda/topics discussed, decisions made (with who made "
            "them), action items in a table (owner, action, due date), and "
            "next steps. Use the dominant language of the transcript.",
        ),
        "faq": (
            "FAQ",
            "A FAQ document: extract the questions raised during the meeting "
            "and the answers given, phrased as clear Q&A pairs. Group related "
            "questions under headings. If a question was raised but never "
            "answered, list it under 'Open Questions'.",
        ),
        "summary": (
            "Summary",
            "A concise executive summary: 3-5 short paragraphs covering what "
            "was discussed, what was decided, and what happens next. Add a "
            "bulleted 'Key Points' list at the end. Keep it under one page.",
        ),
    }

    def _write_document_request(self, base: str, bundle_path: str,
                                requested: List[str]) -> str:
        """Write the .request.md hand-off file next to the bundle."""
        meta = self.meeting.metadata
        transcript_md = os.path.basename(base + ".md")

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
            "## Documents requested",
            "",
        ]
        for key in requested:
            label, instruction = self.DOCUMENT_TYPES.get(
                key, (key, f"A document of type: {key}")
            )
            lines.append(f"### {label}")
            lines.append("")
            lines.append(instruction)
            lines.append("")
            lines.append(f"- Output file: `{os.path.basename(base)}_{key}.docx`")
            lines.append("")

        lines += [
            "## Instructions for the AI assistant",
            "",
            f"1. Read `{transcript_md}` in this folder.",
            "2. Produce each document listed above as a .docx in this same folder.",
            "3. Use only facts present in the transcript — never invent names, "
            "dates or commitments. Write 'Not discussed' where information is "
            "missing.",
            "4. When finished, change Status at the top of this file to **DONE**.",
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
