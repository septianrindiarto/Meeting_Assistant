"""
Meeting Scribe — Meeting Workspace View
Three-pane layout: Transcript (left), Structured Data (middle), Documents (right).
Includes audio playback with waveform visualization and transcript sync.
"""
from __future__ import annotations

import os
import logging
from typing import Optional, List

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QFrame, QTextEdit, QTabWidget, QListWidget,
    QListWidgetItem, QLineEdit, QInputDialog, QMessageBox,
    QFileDialog, QScrollArea, QSizePolicy, QProgressBar,
    QDialog, QDialogButtonBox, QCheckBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMetaObject, Q_ARG
from PyQt6.QtGui import QFont, QColor

from src.core.pipeline import MeetingPipeline, PipelineState
from src.core.models import Meeting, TranscriptSegment
from src.ui.recording_bar import RecordingBar
from src.ui.audio_player import AudioPlayer

logger = logging.getLogger(__name__)


class ProcessingThread(QThread):
    """Background thread for post-meeting processing."""
    finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)  # Thread-safe progress signal

    def __init__(self, pipeline: MeetingPipeline):
        super().__init__()
        self.pipeline = pipeline

    def run(self):
        try:
            # Wire progress through the signal (thread-safe)
            self.pipeline.on_progress = lambda msg: self.progress.emit(msg)
            self.pipeline.process_meeting()
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class ImportThread(QThread):
    """Background thread: decode a media file and transcribe it."""
    finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, pipeline: MeetingPipeline, media_path: str):
        super().__init__()
        self.pipeline = pipeline
        self.media_path = media_path

    def run(self):
        try:
            self.pipeline.on_progress = lambda msg: self.progress.emit(msg)
            self.pipeline.import_media_file(self.media_path)
            self.pipeline.process_meeting()
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class SaveWithDocumentsDialog(QDialog):
    """Asked on Save Bundle: which documents should be produced from this
    meeting, and whether the app should generate them itself."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Save Meeting — Documents")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QLabel("Which documents should be created from this meeting?")
        header.setFont(QFont("Inter", 12, QFont.Weight.DemiBold))
        header.setWordWrap(True)
        layout.addWidget(header)

        self.checks = {}
        for key, (label, _instr) in MeetingPipeline.DOCUMENT_TYPES.items():
            cb = QCheckBox(label)
            cb.setChecked(key == "mom")  # MoM ticked by default
            layout.addWidget(cb)
            self.checks[key] = cb

        layout.addSpacing(6)

        self.generate_check = QCheckBox(
            "Generate them now using the app's AI backend"
        )
        self.generate_check.setChecked(True)
        self.generate_check.setToolTip(
            "Uses the LLM configured in Settings (Groq is free).\n"
            "Untick if you prefer your own AI assistant to write them "
            "from the request file."
        )
        layout.addWidget(self.generate_check)

        note = QLabel(
            "A readable transcript (.md) and a request file (.request.md) are "
            "always saved next to the bundle — so any AI assistant with access "
            "to the folder can produce these documents later."
        )
        note.setStyleSheet("color: #8b8ba0; font-size: 11px;")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save Bundle")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_documents(self):
        return [k for k, cb in self.checks.items() if cb.isChecked()]

    def generate_in_app(self) -> bool:
        return self.generate_check.isChecked()


class RequestedDocsThread(QThread):
    """Background thread: generate the documents ticked in the save dialog."""
    finished_with_files = pyqtSignal(list)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, pipeline: MeetingPipeline, requested: list, output_dir: str):
        super().__init__()
        self.pipeline = pipeline
        self.requested = requested
        self.output_dir = output_dir

    def run(self):
        try:
            self.pipeline.on_progress = lambda msg: self.progress.emit(msg)
            paths = self.pipeline.generate_requested_documents(
                self.requested, output_dir=self.output_dir
            )
            if not paths:
                self.error.emit(
                    "No documents were generated. Check that an AI backend is "
                    "configured in Settings → AI Document Structuring."
                )
                return
            self.finished_with_files.emit(paths)
        except Exception as e:
            self.error.emit(str(e))


class AIDocumentThread(QThread):
    """Background thread: ask the LLM to write a document."""
    finished_with_files = pyqtSignal(list)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, pipeline: MeetingPipeline, instruction: str):
        super().__init__()
        self.pipeline = pipeline
        self.instruction = instruction

    def run(self):
        try:
            self.pipeline.on_progress = lambda msg: self.progress.emit(msg)
            paths = self.pipeline.generate_ai_document(self.instruction)
            self.finished_with_files.emit(paths)
        except Exception as e:
            self.error.emit(str(e))


class TranscriptListItem(QListWidgetItem):
    """Custom list item storing transcript segment data."""

    def __init__(self, segment: TranscriptSegment):
        self.segment = segment
        speaker = segment.speaker or "Speaker"
        time_str = self._format_time(segment.start)

        display = f"[{time_str}]  {speaker}:  {segment.text}"
        super().__init__(display)

        # Styling
        self.setForeground(QColor("#e0e0e8"))
        font = QFont("Inter", 12)
        self.setFont(font)

    @staticmethod
    def _format_time(seconds: float) -> str:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"{m}:{s:02d}"


class MeetingWorkspace(QWidget):
    """
    The main workspace shown during and after a meeting.
    Contains the audio player, transcript viewer, structured data tabs,
    and document generation panel.
    """

    # Emitted when a meeting is saved so the main window can refresh home
    meeting_saved = pyqtSignal()
    # Thread-safe bridge: live/processing transcript updates arrive from
    # worker threads; Qt queues this signal onto the main thread.
    transcript_updated = pyqtSignal(list)

    def __init__(self, pipeline: MeetingPipeline, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self.recording_bar: Optional[RecordingBar] = None
        self._processing_thread: Optional[ProcessingThread] = None
        self._transcript_segments: List[TranscriptSegment] = []

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Toolbar ──
        toolbar = QFrame()
        toolbar.setStyleSheet("background-color: #12122a; border-bottom: 1px solid #1e1e3a;")
        toolbar.setFixedHeight(56)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(20, 8, 20, 8)

        self.title_label = QLabel("New Meeting")
        self.title_label.setFont(QFont("Inter", 16, QFont.Weight.DemiBold))
        self.title_label.setStyleSheet("color: #f0f0f8;")
        toolbar_layout.addWidget(self.title_label)

        toolbar_layout.addStretch()

        # Status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #6366f1; font-size: 12px;")
        self.status_label.setMinimumWidth(200)
        toolbar_layout.addWidget(self.status_label)

        # Action buttons
        self.import_btn = QPushButton("  📂 Import Media  ")
        self.import_btn.setToolTip(
            "Transcribe an audio or video file (mp3, mp4, m4a, wav, ...)"
        )
        self.import_btn.clicked.connect(self._on_import_media)
        toolbar_layout.addWidget(self.import_btn)

        self.process_btn = QPushButton("  Process  ")
        self.process_btn.setToolTip("Transcribe, diarize, and extract structure")
        self.process_btn.clicked.connect(self._on_process)
        self.process_btn.setEnabled(False)
        toolbar_layout.addWidget(self.process_btn)

        self.save_btn = QPushButton("  Save Bundle  ")
        self.save_btn.clicked.connect(self._on_save)
        self.save_btn.setEnabled(False)
        toolbar_layout.addWidget(self.save_btn)

        layout.addWidget(toolbar)

        # ── Progress Bar (hidden by default) ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximum(0)  # indeterminate
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # ── Audio Player (hidden until audio is loaded) ──
        self.audio_player = AudioPlayer()
        self.audio_player.position_changed.connect(self._on_playback_position)
        self.audio_player.hide()
        layout.addWidget(self.audio_player)

        # ── Three-Pane Splitter ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        # Left: Transcript
        self.transcript_panel = self._create_transcript_panel()
        splitter.addWidget(self.transcript_panel)

        # Middle: Structured Data (tabs)
        self.structured_panel = self._create_structured_panel()
        splitter.addWidget(self.structured_panel)

        # Right: Documents
        self.documents_panel = self._create_documents_panel()
        splitter.addWidget(self.documents_panel)

        # Set initial sizes (40%, 30%, 30%)
        splitter.setSizes([500, 400, 350])

        layout.addWidget(splitter)

        # Pipeline callbacks — route through the Qt signal so updates coming
        # from worker threads (live transcription, processing) are marshaled
        # safely onto the main thread before touching widgets.
        self.pipeline.on_transcript_update = self.transcript_updated.emit
        self.transcript_updated.connect(self._on_transcript_update)

    def _create_transcript_panel(self) -> QFrame:
        """Create the transcript viewer panel with clickable segments."""
        panel = QFrame()
        panel.setStyleSheet("background-color: #12122a;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header with playback hint
        header_row = QHBoxLayout()
        header = QLabel("Transcript")
        header.setObjectName("subheading")
        header.setFont(QFont("Inter", 14, QFont.Weight.DemiBold))
        header_row.addWidget(header)

        header_row.addStretch()

        self.playback_hint = QLabel("Click a segment to seek playback")
        self.playback_hint.setStyleSheet("color: #505068; font-size: 11px;")
        self.playback_hint.hide()
        header_row.addWidget(self.playback_hint)

        layout.addLayout(header_row)

        # Transcript list (clickable segments)
        self.transcript_list = QListWidget()
        self.transcript_list.setAlternatingRowColors(False)
        self.transcript_list.setWordWrap(True)
        self.transcript_list.setSpacing(2)
        self.transcript_list.itemClicked.connect(self._on_transcript_click)
        self.transcript_list.setStyleSheet("""
            QListWidget {
                background-color: #0f0f1a;
                border: 1px solid #1e1e3a;
                border-radius: 8px;
                padding: 8px;
                outline: none;
            }
            QListWidget::item {
                padding: 8px 12px;
                border-radius: 6px;
                border: none;
            }
            QListWidget::item:selected {
                background-color: rgba(99, 102, 241, 0.15);
                color: #e0e0f0;
            }
            QListWidget::item:hover {
                background-color: rgba(99, 102, 241, 0.08);
            }
        """)
        layout.addWidget(self.transcript_list)

        # Empty state placeholder (shown when no transcript)
        self.transcript_empty = QLabel(
            "Start a recording to capture audio.\n\n"
            "After recording, click 'Process' to transcribe the audio.\n"
            "The transcript will appear here with speaker labels."
        )
        self.transcript_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.transcript_empty.setStyleSheet(
            "color: #505068; font-size: 13px; padding: 40px; "
            "background-color: #0f0f1a; border: 1px solid #1e1e3a; border-radius: 8px;"
        )
        self.transcript_empty.setWordWrap(True)
        layout.addWidget(self.transcript_empty)

        # Start with empty state visible, list hidden
        self.transcript_list.hide()

        return panel

    def _create_structured_panel(self) -> QFrame:
        """Create the structured data panel with tabs."""
        panel = QFrame()
        panel.setStyleSheet("background-color: #12122a;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QLabel("Analysis")
        header.setObjectName("subheading")
        header.setFont(QFont("Inter", 14, QFont.Weight.DemiBold))
        layout.addWidget(header)

        tabs = QTabWidget()

        # Summary tab
        self.summary_text = QTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.setPlaceholderText(
            "Executive summary will appear here after processing.\n\n"
            "Requires Ollama (local LLM) to be installed and running.\n"
            "See Settings for setup instructions."
        )
        tabs.addTab(self.summary_text, "Summary")

        # Action Items tab
        self.actions_list = QListWidget()
        self.actions_list.setAlternatingRowColors(True)
        tabs.addTab(self.actions_list, "Actions")

        # Decisions tab
        self.decisions_list = QListWidget()
        self.decisions_list.setAlternatingRowColors(True)
        tabs.addTab(self.decisions_list, "Decisions")

        # Timeline tab
        self.timeline_list = QListWidget()
        self.timeline_list.setAlternatingRowColors(True)
        tabs.addTab(self.timeline_list, "Timeline")

        layout.addWidget(tabs)

        return panel

    def _create_documents_panel(self) -> QFrame:
        """Create the documents generation panel."""
        panel = QFrame()
        panel.setStyleSheet("background-color: #12122a;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QLabel("Documents")
        header.setObjectName("subheading")
        header.setFont(QFont("Inter", 14, QFont.Weight.DemiBold))
        layout.addWidget(header)

        # Template selector & generate button
        self.template_combo = QListWidget()
        self.template_combo.setMaximumHeight(200)
        layout.addWidget(self.template_combo)

        generate_btn = QPushButton("  Generate from Template  ")
        generate_btn.setObjectName("primary_button")
        generate_btn.setToolTip(
            "Fill the selected template with this meeting's data"
        )
        generate_btn.clicked.connect(self._on_generate)
        layout.addWidget(generate_btn)

        # Free-form AI document generation
        ai_btn = QPushButton("  ✨ Ask AI for a Document  ")
        ai_btn.setToolTip(
            "Describe any document you want — the AI writes it from the transcript"
        )
        ai_btn.clicked.connect(self._on_ai_document)
        layout.addWidget(ai_btn)

        # Export transcript (free — no AI needed)
        export_btn = QPushButton("  📄 Export Transcript  ")
        export_btn.setToolTip(
            "Save the transcript as .txt or .md — paste into claude.ai to "
            "create any document for free"
        )
        export_btn.clicked.connect(self._on_export_transcript)
        layout.addWidget(export_btn)

        # Generated documents list
        layout.addSpacing(12)
        docs_header = QLabel("Generated Files")
        docs_header.setStyleSheet("color: #a0a0b8; font-weight: 600;")
        layout.addWidget(docs_header)

        self.docs_list = QListWidget()
        self.docs_list.itemDoubleClicked.connect(self._on_open_document)
        layout.addWidget(self.docs_list)

        # Load available templates
        self._load_templates()

        return panel

    def _load_templates(self):
        """Load available document templates into the combo."""
        from src.core.template_engine import TemplateEngine
        from src.utils.file_utils import get_templates_dir

        self.template_combo.clear()
        engine = TemplateEngine()
        templates_dir = str(get_templates_dir())

        if os.path.isdir(templates_dir):
            templates = engine.list_templates(templates_dir)
            for tmpl in templates:
                item = QListWidgetItem(f"  {tmpl.name}")
                item.setData(Qt.ItemDataRole.UserRole, tmpl.path)
                self.template_combo.addItem(item)

        if self.template_combo.count() == 0:
            item = QListWidgetItem("No templates found")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.template_combo.addItem(item)

    # ─── Audio Playback & Transcript Sync ────────────────────────

    def _load_audio_player(self, audio_path: str):
        """Load audio into the player and show it."""
        if audio_path and os.path.exists(audio_path):
            self.audio_player.load_file(audio_path)
            self.audio_player.show()
            self.playback_hint.show()
        else:
            self.audio_player.hide()
            self.playback_hint.hide()

    def _on_transcript_click(self, item: QListWidgetItem):
        """Seek audio player when a transcript segment is clicked."""
        if isinstance(item, TranscriptListItem):
            self.audio_player.seek_to(item.segment.start)

    def _on_playback_position(self, seconds: float):
        """
        Highlight the transcript segment matching the current playback position.
        Auto-scrolls to keep the active segment visible.
        """
        if not self._transcript_segments:
            return

        # Find the segment closest to current position
        active_index = 0
        for i, seg in enumerate(self._transcript_segments):
            if seg.start <= seconds < seg.end:
                active_index = i
                break
            elif seg.start > seconds:
                active_index = max(0, i - 1)
                break
        else:
            # Past all segments — highlight last
            active_index = len(self._transcript_segments) - 1

        # Highlight active segment
        for i in range(self.transcript_list.count()):
            item = self.transcript_list.item(i)
            if i == active_index:
                item.setBackground(QColor(99, 102, 241, 35))
                item.setForeground(QColor("#f0f0f8"))
                # Ensure it's visible
                self.transcript_list.scrollToItem(
                    item, QListWidget.ScrollHint.PositionAtCenter
                )
            else:
                item.setBackground(QColor(0, 0, 0, 0))
                item.setForeground(QColor("#e0e0e8"))

    # ─── Recording Lifecycle ─────────────────────────────────────

    def start_new_meeting(self):
        """Begin a new meeting recording."""
        title, ok = QInputDialog.getText(
            self, "New Meeting", "Meeting title:",
            QLineEdit.EchoMode.Normal, "Untitled Meeting"
        )
        if not ok:
            return
        if not title.strip():
            title = "Untitled Meeting"

        self.title_label.setText(title)
        self.status_label.setText("Recording...")

        # Clear previous content
        self.transcript_list.clear()
        self.transcript_list.hide()
        self.transcript_empty.show()
        self._transcript_segments = []
        self.summary_text.clear()
        self.actions_list.clear()
        self.decisions_list.clear()
        self.timeline_list.clear()
        self.docs_list.clear()
        self.audio_player.stop()
        self.audio_player.hide()
        self.playback_hint.hide()

        # Start recording with error handling
        try:
            self.pipeline.start_recording(title=title)
        except Exception as e:
            QMessageBox.critical(
                self, "Recording Error",
                f"Failed to start recording:\n{e}\n\n"
                "Make sure PyAudioWPatch is installed:\n"
                "pip install PyAudioWPatch"
            )
            self.status_label.setText("Recording failed")
            return

        # Show recording bar
        self.recording_bar = RecordingBar(self.pipeline)
        self.recording_bar.pause_clicked.connect(self._on_recording_pause)
        self.recording_bar.stop_clicked.connect(self._on_recording_stop)
        self.recording_bar.start()

        self.process_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

    def _on_recording_pause(self):
        """Toggle pause/resume."""
        if self.pipeline.state == PipelineState.RECORDING:
            self.pipeline.pause_recording()
            self.status_label.setText("Paused")
        elif self.pipeline.state == PipelineState.PAUSED:
            self.pipeline.resume_recording()
            self.status_label.setText("Recording...")

    def _on_recording_stop(self):
        """Stop recording and enable processing."""
        self.pipeline.stop_recording()

        if self.recording_bar:
            self.recording_bar.stop()
            self.recording_bar = None

        duration = ""
        if self.pipeline.meeting:
            duration = self.pipeline.meeting.metadata.duration

        if duration:
            self.status_label.setText(f"Recorded {duration}")
        else:
            self.status_label.setText("Recording stopped (no audio captured)")

        self.process_btn.setEnabled(True)
        self.save_btn.setEnabled(True)

        # Load audio into the player for immediate playback
        if self.pipeline.meeting and self.pipeline.meeting.audio_path:
            self._load_audio_player(self.pipeline.meeting.audio_path)

        # Check if we got any audio
        if self.pipeline.meeting and not self.pipeline.meeting.chunk_paths:
            self.transcript_empty.setText(
                "No audio was captured.\n\n"
                "Possible causes:\n"
                "- No audio devices detected\n"
                "- System audio source has no sound playing\n"
                "- Microphone not connected or muted\n\n"
                "Check Settings for audio device configuration."
            )

    # ─── Media Import ────────────────────────────────────────────

    def _on_import_media(self):
        """Pick an audio/video file and transcribe it automatically.
        mp4 and mp3 are both decoded directly — no conversion step needed."""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Import Audio / Video",
            "",
            "Media Files (*.mp3 *.mp4 *.m4a *.wav *.opus *.ogg *.flac "
            "*.aac *.mkv *.webm *.mov);;All Files (*)"
        )
        if not filepath:
            return

        if self.pipeline.state != PipelineState.IDLE:
            QMessageBox.warning(
                self, "Busy",
                "Finish or stop the current recording/processing first."
            )
            return

        # Reset the workspace for the imported meeting
        self.title_label.setText(os.path.basename(filepath))
        self.transcript_list.clear()
        self.transcript_list.hide()
        self.transcript_empty.setText("Decoding and transcribing the file...")
        self.transcript_empty.show()
        self._transcript_segments = []
        self.summary_text.clear()
        self.actions_list.clear()
        self.decisions_list.clear()
        self.timeline_list.clear()
        self.docs_list.clear()
        self.audio_player.stop()
        self.audio_player.hide()

        self.import_btn.setEnabled(False)
        # Process button doubles as Cancel during import transcription
        self._is_processing = True
        self.process_btn.setText("  ✕ Cancel  ")
        self.process_btn.setToolTip("Stop transcription — partial transcript is kept")
        self.process_btn.setEnabled(True)
        self.save_btn.setEnabled(False)
        self.status_label.setText("Importing...")
        self.progress_bar.show()

        self._import_thread = ImportThread(self.pipeline, filepath)
        self._import_thread.progress.connect(self._on_progress_update)
        self._import_thread.finished.connect(self._on_import_done)
        self._import_thread.error.connect(self._on_import_error)
        self._import_thread.start()

    def _on_import_done(self):
        self.progress_bar.hide()
        self.import_btn.setEnabled(True)
        self._reset_process_button()
        self.save_btn.setEnabled(True)
        self.status_label.setText("Import complete!")
        if self.pipeline.meeting:
            self._populate_structured_data(self.pipeline.meeting)

    def _on_import_error(self, error: str):
        self.progress_bar.hide()
        self.import_btn.setEnabled(True)
        self._reset_process_button()
        self.status_label.setText(f"Import failed: {error}")

        # Delete the partially decoded audio — it can be hundreds of MB.
        try:
            from src.utils.housekeeping import remove_working_file
            remove_working_file(getattr(self.pipeline, "_working_audio_path", None))
        except Exception:
            pass

        QMessageBox.warning(
            self, "Import Error",
            f"Could not import the media file:\n\n{error}"
        )

    # ─── Processing ──────────────────────────────────────────────

    def _on_process(self):
        """Start post-meeting processing — or cancel it if already running."""
        # Second click while running = cancel request
        if getattr(self, "_is_processing", False):
            self.pipeline.cancel_processing()
            self.process_btn.setEnabled(False)  # prevent double-cancel
            return

        if not self.pipeline.meeting:
            QMessageBox.warning(self, "No Meeting", "No meeting data to process.")
            return

        if not self.pipeline.meeting.audio_path and not self.pipeline.meeting.chunk_paths:
            QMessageBox.warning(
                self, "No Audio",
                "No audio was captured. Record a meeting first."
            )
            return

        self._is_processing = True
        self.process_btn.setText("  ✕ Cancel  ")
        self.process_btn.setToolTip("Stop transcription — partial transcript is kept")
        self.save_btn.setEnabled(False)
        self.status_label.setText("Processing...")
        self.progress_bar.show()

        self._processing_thread = ProcessingThread(self.pipeline)
        # Thread-safe signal connections
        self._processing_thread.progress.connect(self._on_progress_update)
        self._processing_thread.finished.connect(self._on_processing_done)
        self._processing_thread.error.connect(self._on_processing_error)
        self._processing_thread.start()

    def _reset_process_button(self):
        """Restore the Process button to its idle state."""
        self._is_processing = False
        self.process_btn.setText("  Process  ")
        self.process_btn.setToolTip("Transcribe, diarize, and extract structure")
        self.process_btn.setEnabled(True)

    def _on_progress_update(self, message: str):
        """Thread-safe progress update (called via signal)."""
        self.status_label.setText(message)

    def _on_processing_done(self):
        """Handle processing completion."""
        self.progress_bar.hide()
        self._reset_process_button()
        self.save_btn.setEnabled(True)

        # If the user cancelled, offer to discard the (large) working audio
        # instead of leaving it on disk.
        transcriber = getattr(self.pipeline, "_transcriber", None)
        if transcriber is not None and getattr(transcriber, "cancel_requested", False):
            self.status_label.setText("Cancelled — partial transcript kept")
            reply = QMessageBox.question(
                self, "Cancelled",
                "Transcription was cancelled.\n\n"
                "Keep this meeting (you can save or resume it), or discard it "
                "and free the temporary audio files?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Discard:
                try:
                    freed = self.pipeline.discard_meeting()
                    self.status_label.setText(
                        f"Discarded — freed {freed:.0f} MB"
                    )
                    self.transcript_list.clear()
                    self.transcript_list.hide()
                    self.transcript_empty.setText(
                        "Meeting discarded. Import a file or start a new "
                        "recording to begin."
                    )
                    self.transcript_empty.show()
                    self.save_btn.setEnabled(False)
                    self.process_btn.setEnabled(False)
                except Exception as e:
                    QMessageBox.warning(self, "Cleanup Error", str(e))
            return

        self.status_label.setText("Processing complete!")

        if self.pipeline.meeting:
            self._populate_structured_data(self.pipeline.meeting)

            # Auto-prompt to save
            reply = QMessageBox.question(
                self, "Save Meeting",
                "Processing complete! Save this meeting as a .mscribe bundle?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._on_save()

    def _on_processing_error(self, error: str):
        """Handle processing error."""
        self.progress_bar.hide()
        self.status_label.setText(f"Error: {error}")
        self._reset_process_button()
        self.save_btn.setEnabled(True)

        QMessageBox.warning(
            self, "Processing Error",
            f"An error occurred during processing:\n\n{error}\n\n"
            "Tip: Make sure faster-whisper is installed:\n"
            "pip install faster-whisper"
        )

    # ─── Data Display ────────────────────────────────────────────

    def _on_transcript_update(self, segments: List[TranscriptSegment]):
        """Update transcript display when new segments arrive."""
        self._transcript_segments = segments
        self.transcript_list.clear()

        if segments:
            self.transcript_empty.hide()
            self.transcript_list.show()

            for seg in segments:
                item = TranscriptListItem(seg)
                self.transcript_list.addItem(item)
        else:
            self.transcript_list.hide()
            self.transcript_empty.setText(
                "No transcript segments were generated.\n"
                "The recording may have been too short or silent."
            )
            self.transcript_empty.show()

    def _populate_structured_data(self, meeting: Meeting):
        """Fill in the structured data tabs from the meeting."""
        # Update transcript
        if meeting.transcript:
            self._on_transcript_update(meeting.transcript)
        else:
            self.transcript_list.hide()
            self.transcript_empty.setText(
                "No transcript segments were generated.\n"
                "The recording may have been too short or silent."
            )
            self.transcript_empty.show()

        # Load audio player if available
        if meeting.audio_path:
            self._load_audio_player(meeting.audio_path)

        if meeting.structured:
            # Summary
            self.summary_text.setPlainText(meeting.structured.summary)

            # Action Items
            self.actions_list.clear()
            for item in meeting.structured.action_items:
                due = f" (due: {item.due_date})" if item.due_date else ""
                text = f"[{item.owner}] {item.description}{due}"
                list_item = QListWidgetItem(text)
                self.actions_list.addItem(list_item)

            # Decisions
            self.decisions_list.clear()
            for dec in meeting.structured.decisions:
                text = f"[{dec.speaker}] {dec.description}"
                list_item = QListWidgetItem(text)
                self.decisions_list.addItem(list_item)

            # Timeline
            self.timeline_list.clear()
            for event in meeting.structured.timeline:
                phase_labels = {
                    "intro": "[INTRO]",
                    "discussion": "[DISCUSS]",
                    "decision": "[DECIDE]",
                    "action": "[ACTION]",
                    "closing": "[CLOSE]",
                }
                label = phase_labels.get(event.phase, "[---]")
                text = f"[{event.timestamp:.0f}s] {label} {event.topic}"
                list_item = QListWidgetItem(text)
                self.timeline_list.addItem(list_item)
        else:
            self.summary_text.setPlainText(
                "No LLM backend configured.\n\n"
                "Structured analysis (summary, action items, decisions) "
                "requires Ollama or a cloud API key.\n\n"
                "Go to Settings > AI Document Structuring to configure."
            )

    def load_meeting(self, meeting: Meeting):
        """Load a previously saved meeting into the workspace."""
        self.title_label.setText(meeting.metadata.title)
        self.status_label.setText(f"Duration: {meeting.metadata.duration}")
        self.process_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self._populate_structured_data(meeting)

    # ─── Documents ───────────────────────────────────────────────

    def _on_generate(self):
        """Generate a document from the selected template."""
        selected = self.template_combo.currentItem()
        if not selected:
            QMessageBox.information(self, "Select Template", "Please select a template first.")
            return

        template_path = selected.data(Qt.ItemDataRole.UserRole)
        if not template_path or not os.path.exists(template_path):
            QMessageBox.warning(self, "Error", "Please select a valid template.")
            return

        if not self.pipeline.meeting:
            QMessageBox.warning(self, "Error", "No meeting data to generate from.")
            return

        try:
            self.status_label.setText("Generating document...")
            generated = self.pipeline.generate_documents(template_path)

            for path in generated:
                name = os.path.basename(path)
                item = QListWidgetItem(f"  {name}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.docs_list.addItem(item)

            self.status_label.setText(f"Generated {len(generated)} file(s)")

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Document generation failed:\n{e}")
            self.status_label.setText("Generation failed")

    def _on_ai_document(self):
        """Ask the LLM to write a free-form document from the transcript."""
        if not self.pipeline.meeting or not self.pipeline.meeting.transcript:
            QMessageBox.warning(
                self, "No Transcript",
                "Process a meeting first — the AI writes from the transcript."
            )
            return

        presets = [
            "Formal Minutes of Meeting",
            "Formal Minutes of Meeting in Bahasa",
            "Executive summary (1 page)",
            "Action items with owners and deadlines",
            "Follow-up email to participants",
            "Client-facing progress report",
            "Decision log with rationale",
            "(type my own...)",
        ]
        choice, ok = QInputDialog.getItem(
            self, "Ask AI for a Document",
            "What document do you want?", presets, 0, False
        )
        if not ok:
            return

        if choice == "(type my own...)":
            choice, ok = QInputDialog.getText(
                self, "Describe the Document",
                "Describe what you want the AI to write:",
                QLineEdit.EchoMode.Normal,
                "A formal meeting report with sections for background, "
                "discussion, decisions and next steps"
            )
            if not ok or not choice.strip():
                return

        self.status_label.setText("AI is writing your document...")
        self.progress_bar.show()

        self._ai_thread = AIDocumentThread(self.pipeline, choice)
        self._ai_thread.progress.connect(self._on_progress_update)
        self._ai_thread.finished_with_files.connect(self._on_ai_document_done)
        self._ai_thread.error.connect(self._on_ai_document_error)
        self._ai_thread.start()

    def _on_ai_document_done(self, paths: list):
        self.progress_bar.hide()
        for path in paths:
            name = os.path.basename(path)
            item = QListWidgetItem(f"  {name}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.docs_list.addItem(item)
        self.status_label.setText(f"AI generated {len(paths)} file(s)")

        # Open the .docx immediately — that's what the user wants to see
        docx = next((p for p in paths if p.endswith(".docx")), None)
        if docx and os.path.exists(docx):
            try:
                os.startfile(docx)
            except Exception:
                pass

    def _on_ai_document_error(self, error: str):
        self.progress_bar.hide()
        self.status_label.setText("AI generation failed")
        QMessageBox.warning(
            self, "AI Document Failed",
            f"{error}\n\nTip: Settings → AI Document Structuring → select "
            "'groq' and paste your free Groq API key."
        )

    def _on_export_transcript(self):
        """Export the transcript as .txt or .md (no AI needed, always free)."""
        if not self.pipeline.meeting or not self.pipeline.meeting.transcript:
            QMessageBox.warning(
                self, "No Transcript", "There is no transcript to export yet."
            )
            return

        default_name = (self.pipeline.meeting.metadata.title or "transcript")
        default_name = "".join(
            c for c in default_name if c.isalnum() or c in " -_"
        ).strip().replace(" ", "_")

        filepath, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Transcript",
            f"{default_name}_transcript.md",
            "Markdown (*.md);;Plain Text (*.txt)"
        )
        if not filepath:
            return

        fmt = "md" if filepath.lower().endswith(".md") else "txt"
        try:
            self.pipeline.export_transcript(filepath, fmt=fmt)
            self.status_label.setText(f"Exported: {os.path.basename(filepath)}")
            QMessageBox.information(
                self, "Transcript Exported",
                f"Saved to:\n{filepath}\n\n"
                "You can paste this into claude.ai (or any AI chat) and ask "
                "for any document you need — completely free."
            )
        except Exception as e:
            QMessageBox.warning(self, "Export Failed", str(e))

    def _on_open_document(self, item: QListWidgetItem):
        """Open a generated document in the default application."""
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            os.startfile(path)

    def _on_save(self):
        """Save the meeting — asking first which documents should be produced."""
        if not self.pipeline.meeting:
            QMessageBox.warning(self, "No Meeting", "Nothing to save.")
            return

        dlg = SaveWithDocumentsDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        requested = dlg.selected_documents()
        generate_now = dlg.generate_in_app()

        try:
            path = self.pipeline.save_bundle(requested_documents=requested)
            self.status_label.setText(f"Saved: {os.path.basename(path)}")
            self.meeting_saved.emit()  # Signal to refresh home view
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save:\n{e}")
            return

        folder = os.path.dirname(path)

        if requested and generate_now:
            # Free-tier path: the app writes the documents itself via the
            # configured LLM (Groq is free). Runs in the background.
            self.progress_bar.show()
            self.status_label.setText("Generating documents...")
            self._docs_thread = RequestedDocsThread(self.pipeline, requested, folder)
            self._docs_thread.progress.connect(self._on_progress_update)
            self._docs_thread.finished_with_files.connect(self._on_ai_document_done)
            self._docs_thread.error.connect(self._on_ai_document_error)
            self._docs_thread.start()
            return

        if requested:
            QMessageBox.information(
                self, "Saved — documents requested",
                f"Bundle saved:\n{os.path.basename(path)}\n\n"
                f"Also written to the same folder:\n"
                f"• {os.path.splitext(os.path.basename(path))[0]}.md "
                "(readable transcript)\n"
                f"• {os.path.splitext(os.path.basename(path))[0]}.request.md "
                "(what to produce)\n\n"
                "Your AI assistant with access to this folder can now create "
                "the documents. Just say: \"process pending meeting requests\"."
            )
        else:
            QMessageBox.information(
                self, "Saved", f"Meeting bundle saved:\n{path}"
            )
