"""
Meeting Scribe — Settings View
Configuration for audio, transcription, LLM, privacy, and project folder.
"""
from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QCheckBox, QGroupBox, QFormLayout,
    QFileDialog, QMessageBox, QScrollArea, QFrame, QSpinBox,
    QProgressBar
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from src.core.settings import Settings
from src.core.models import LLMBackend, AudioSource
from src.core.audio_capture import AudioCaptureEngine
from src.utils.hardware_probe import get_system_info

logger = logging.getLogger(__name__)


class SettingsView(QWidget):
    """Application settings screen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = Settings.instance()
        self._setup_ui()
        self._load_values()

    def _setup_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(32, 28, 32, 28)
        outer_layout.setSpacing(16)

        # Header
        title = QLabel("Settings")
        title.setObjectName("heading")
        title.setFont(QFont("Inter", 22, QFont.Weight.Bold))
        outer_layout.addWidget(title)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(16)

        # ── General ──
        general_group = QGroupBox("General")
        general_layout = QFormLayout(general_group)

        self.project_folder_input = QLineEdit()
        self.project_folder_input.setReadOnly(True)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_project_folder)
        folder_layout = QHBoxLayout()
        folder_layout.addWidget(self.project_folder_input)
        folder_layout.addWidget(browse_btn)
        general_layout.addRow("Project Folder:", folder_layout)

        layout.addWidget(general_group)

        # ── Audio Devices ──
        audio_group = QGroupBox("Audio")
        audio_layout = QFormLayout(audio_group)

        self.audio_source_combo = QComboBox()
        self.audio_source_combo.addItem("Both (microphone + system audio)", "both")
        self.audio_source_combo.addItem("Microphone only", "mic")
        self.audio_source_combo.addItem("System audio only", "system")
        audio_layout.addRow("Source:", self.audio_source_combo)

        # Microphone device picker
        mic_row = QHBoxLayout()
        self.mic_device_combo = QComboBox()
        self.mic_device_combo.setMinimumWidth(300)
        mic_row.addWidget(self.mic_device_combo)
        refresh_mic_btn = QPushButton("↻")
        refresh_mic_btn.setToolTip("Re-scan audio devices (plug/unplug detection)")
        refresh_mic_btn.setFixedWidth(32)
        refresh_mic_btn.clicked.connect(self._refresh_audio_devices)
        mic_row.addWidget(refresh_mic_btn)
        audio_layout.addRow("Microphone:", mic_row)

        # System loopback device picker
        self.system_device_combo = QComboBox()
        self.system_device_combo.setMinimumWidth(300)
        audio_layout.addRow("System Audio:", self.system_device_combo)

        audio_hint = QLabel(
            "Headsets, Bluetooth earphones, and USB microphones now appear "
            "in this list (including WASAPI devices). If you just plugged "
            "in a new device, click ↻ to re-scan.\n\n"
            "Bluetooth tip: pick the entry with \"Hands-Free\" or \"Headset\" "
            "in the name for recording. The stereo (A2DP) entry is output-only."
        )
        audio_hint.setStyleSheet("color: #707088; font-size: 11px;")
        audio_hint.setWordWrap(True)
        audio_layout.addRow("", audio_hint)

        # Test-mic row: 3-second level meter to verify the selected device
        # actually captures audio before committing to a recording.
        test_row = QHBoxLayout()
        self.test_mic_btn = QPushButton("🎤 Test Microphone (3s)")
        self.test_mic_btn.clicked.connect(self._on_test_mic)
        test_row.addWidget(self.test_mic_btn)

        self.test_mic_meter = QProgressBar()
        self.test_mic_meter.setRange(0, 100)
        self.test_mic_meter.setValue(0)
        self.test_mic_meter.setTextVisible(True)
        self.test_mic_meter.setFormat("Idle")
        self.test_mic_meter.setMinimumWidth(220)
        test_row.addWidget(self.test_mic_meter)

        audio_layout.addRow("", test_row)

        layout.addWidget(audio_group)

        # Populate device dropdowns
        self._refresh_audio_devices()

        # ── Transcription Backend ──
        backend_group = QGroupBox("Transcription Backend")
        backend_layout = QFormLayout(backend_group)

        self.stt_backend_combo = QComboBox()
        self.stt_backend_combo.addItem(
            "Local (Whisper) — private, offline, slower", "local")
        self.stt_backend_combo.addItem(
            "Groq Cloud — free tier, ~2 min for a 2h meeting", "groq")
        self.stt_backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        backend_layout.addRow("Backend:", self.stt_backend_combo)

        self.groq_key_input = QLineEdit()
        self.groq_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key_input.setPlaceholderText("gsk_...")
        backend_layout.addRow("Groq API Key:", self.groq_key_input)

        self.groq_model_combo = QComboBox()
        self.groq_model_combo.addItem(
            "whisper-large-v3-turbo — fastest", "whisper-large-v3-turbo")
        self.groq_model_combo.addItem(
            "whisper-large-v3 — slightly more accurate", "whisper-large-v3")
        backend_layout.addRow("Groq Model:", self.groq_model_combo)

        self.fallback_check = QCheckBox(
            "Roll back to local Whisper automatically if the cloud fails")
        backend_layout.addRow(self.fallback_check)

        groq_hint = QLabel(
            "Free API key at <a style='color: #6366f1;' "
            "href='https://console.groq.com'>console.groq.com</a>. "
            "Free tier: 8 hours of audio/day (2 h per clock hour). Audio is "
            "sent to Groq over TLS — use Local for sensitive meetings."
        )
        groq_hint.setOpenExternalLinks(True)
        groq_hint.setStyleSheet("color: #707088; font-size: 11px;")
        groq_hint.setWordWrap(True)
        backend_layout.addRow("", groq_hint)

        layout.addWidget(backend_group)

        # ── Transcription ──
        trans_group = QGroupBox("Transcription")
        trans_layout = QFormLayout(trans_group)

        # Quality preset is the user-friendly knob. It maps to a Whisper model
        # size at runtime based on the detected hardware.
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Fast — quick draft, lower accuracy", "fast")
        self.quality_combo.addItem("Balanced — recommended", "balanced")
        self.quality_combo.addItem("Accurate — slower, higher accuracy", "accurate")
        self.quality_combo.addItem("Best — slowest, highest accuracy", "best")
        trans_layout.addRow("Quality:", self.quality_combo)

        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "auto", "tiny", "base", "small", "medium",
            "large-v3", "large-v3-turbo",
        ])
        trans_layout.addRow("Override Model:", self.model_combo)

        override_hint = QLabel(
            "Leave on \"auto\" to let the Quality preset pick the best model "
            "for your hardware. Choose a specific model only if you want to "
            "override the auto-selection."
        )
        override_hint.setStyleSheet("color: #707088; font-size: 11px;")
        override_hint.setWordWrap(True)
        trans_layout.addRow("", override_hint)

        # Live (real-time) transcription
        self.live_check = QCheckBox("Show transcript in real time while recording")
        trans_layout.addRow(self.live_check)

        self.live_model_combo = QComboBox()
        self.live_model_combo.addItem("tiny — lowest latency", "tiny")
        self.live_model_combo.addItem("base — recommended for live", "base")
        self.live_model_combo.addItem("small — more accurate, more lag", "small")
        trans_layout.addRow("Live Model:", self.live_model_combo)

        live_hint = QLabel(
            "The live transcript is a fast draft. Clicking \"Process\" after "
            "the meeting re-transcribes everything with the higher-quality "
            "model selected above."
        )
        live_hint.setStyleSheet("color: #707088; font-size: 11px;")
        live_hint.setWordWrap(True)
        trans_layout.addRow("", live_hint)

        # Show system info
        try:
            sys_info = get_system_info()
            rec = sys_info['recommended_whisper_model']
            hw_label = QLabel(
                f"💻 {sys_info['cpu_cores']} cores, {sys_info['ram_gb']}GB RAM"
                f"{', GPU ' + str(sys_info['vram_gb']) + 'GB VRAM' if sys_info['has_nvidia_gpu'] else ''}"
                f"  →  Recommended: {rec}"
            )
            hw_label.setStyleSheet("color: #6366f1; font-size: 11px;")
            trans_layout.addRow("", hw_label)
        except Exception:
            pass

        self.language_input = QLineEdit()
        self.language_input.setPlaceholderText("Leave empty for auto-detect")
        trans_layout.addRow("Language:", self.language_input)

        layout.addWidget(trans_group)

        # ── Speaker Diarization ──
        diar_group = QGroupBox("Speaker Diarization")
        diar_layout = QFormLayout(diar_group)

        self.diarization_check = QCheckBox("Enable speaker identification")
        diar_layout.addRow(self.diarization_check)

        self.hf_token_input = QLineEdit()
        self.hf_token_input.setPlaceholderText("hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self.hf_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        diar_layout.addRow("HuggingFace Token:", self.hf_token_input)

        hf_info = QLabel(
            "Free account required. Get a token at: "
            "<a style='color: #6366f1;' href='https://huggingface.co/settings/tokens'>"
            "huggingface.co/settings/tokens</a>"
        )
        hf_info.setOpenExternalLinks(True)
        hf_info.setStyleSheet("color: #707088; font-size: 11px;")
        hf_info.setWordWrap(True)
        diar_layout.addRow("", hf_info)

        self.max_speakers_spin = QSpinBox()
        self.max_speakers_spin.setRange(2, 20)
        self.max_speakers_spin.setValue(10)
        diar_layout.addRow("Max Speakers:", self.max_speakers_spin)

        layout.addWidget(diar_group)

        # ── LLM Backend ──
        llm_group = QGroupBox("AI Document Structuring (Optional)")
        llm_layout = QFormLayout(llm_group)

        self.llm_combo = QComboBox()
        self.llm_combo.addItems(["none", "groq", "ollama", "openai", "anthropic"])
        self.llm_combo.currentTextChanged.connect(self._on_llm_changed)
        llm_layout.addRow("Backend:", self.llm_combo)

        self.llm_model_input = QLineEdit()
        self.llm_model_input.setPlaceholderText("llama-3.3-70b-versatile")
        llm_layout.addRow("Model:", self.llm_model_input)

        self.llm_api_key_input = QLineEdit()
        self.llm_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_api_key_input.setPlaceholderText(
            "Leave empty for Groq — reuses your Groq transcription key"
        )
        llm_layout.addRow("API Key:", self.llm_api_key_input)

        ollama_info = QLabel(
            "<b style='color:#22c55e;'>groq — FREE.</b> Uses the same key as "
            "Groq transcription (leave API Key empty). Model: "
            "<code>llama-3.3-70b-versatile</code>.<br>"
            "<b>ollama</b> — free &amp; fully local: install "
            "<a style='color: #6366f1;' href='https://ollama.com'>ollama.com</a>, "
            "run <code>ollama pull llama3.1:8b</code>.<br>"
            "<b>openai / anthropic</b> — paid, needs your own API key."
        )
        ollama_info.setOpenExternalLinks(True)
        ollama_info.setStyleSheet("color: #707088; font-size: 11px;")
        ollama_info.setWordWrap(True)
        llm_layout.addRow("", ollama_info)

        layout.addWidget(llm_group)

        # ── Privacy ──
        privacy_group = QGroupBox("Privacy")
        privacy_layout = QFormLayout(privacy_group)

        self.cloud_check = QCheckBox("Allow cloud LLM connections (requires API key)")
        privacy_layout.addRow(self.cloud_check)

        privacy_note = QLabel(
            "⚠️ When disabled, the app makes ZERO outbound network connections.\n"
            "Your meeting audio and transcripts never leave your device."
        )
        privacy_note.setStyleSheet("color: #f59e0b; font-size: 11px;")
        privacy_note.setWordWrap(True)
        privacy_layout.addRow("", privacy_note)

        layout.addWidget(privacy_group)

        # ── Storage ──
        storage_group = QGroupBox("Storage")
        storage_layout = QFormLayout(storage_group)

        self.storage_label = QLabel("Calculating...")
        self.storage_label.setStyleSheet("color: #a0a0b8; font-size: 12px;")
        self.storage_label.setWordWrap(True)
        storage_layout.addRow("Temp usage:", self.storage_label)

        self.auto_cleanup_check = QCheckBox(
            "Automatically remove abandoned temp files on startup")
        storage_layout.addRow(self.auto_cleanup_check)

        self.cleanup_save_check = QCheckBox(
            "Delete working audio after saving a bundle (recommended)")
        storage_layout.addRow(self.cleanup_save_check)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 720)
        self.retention_spin.setSuffix(" hours")
        storage_layout.addRow("Keep temp files for:", self.retention_spin)

        storage_btns = QHBoxLayout()
        refresh_storage_btn = QPushButton("↻ Refresh")
        refresh_storage_btn.clicked.connect(self._refresh_storage)
        storage_btns.addWidget(refresh_storage_btn)

        clean_btn = QPushButton("🧹 Clean Now")
        clean_btn.setToolTip(
            "Delete all temporary working files immediately.\n"
            "Saved meetings (.mscribe) and documents are never touched."
        )
        clean_btn.clicked.connect(self._on_clean_now)
        storage_btns.addWidget(clean_btn)
        storage_btns.addStretch()
        storage_layout.addRow("", storage_btns)

        storage_note = QLabel(
            "Imported video/audio is decoded to ~115 MB per hour of audio "
            "while it is being transcribed. These working files are removed "
            "once the meeting is saved. Your .mscribe bundles and generated "
            "documents are never deleted."
        )
        storage_note.setStyleSheet("color: #707088; font-size: 11px;")
        storage_note.setWordWrap(True)
        storage_layout.addRow("", storage_note)

        layout.addWidget(storage_group)

        self._refresh_storage()

        # Save / Reset buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(reset_btn)

        save_btn = QPushButton("💾 Save Settings")
        save_btn.setObjectName("primary_button")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)
        layout.addStretch()

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

    def _refresh_audio_devices(self):
        """Re-scan audio devices and repopulate the dropdowns."""
        try:
            devices = AudioCaptureEngine.list_audio_devices()
        except Exception as e:
            logger.warning(f"Could not enumerate audio devices: {e}")
            devices = {'mic_devices': [], 'system_devices': []}

        # Microphone dropdown
        self.mic_device_combo.clear()
        self.mic_device_combo.addItem("System default", None)
        for dev in devices['mic_devices']:
            self.mic_device_combo.addItem(dev['name'], dev['index'])

        # System loopback dropdown
        self.system_device_combo.clear()
        self.system_device_combo.addItem("Auto-detect (current default output)", None)
        for dev in devices['system_devices']:
            self.system_device_combo.addItem(dev['name'], dev['index'])

        # Restore previously-saved selections (matching by index value)
        saved_mic = self.settings.get("mic_device_index")
        saved_sys = self.settings.get("system_device_index")
        for combo, saved in ((self.mic_device_combo, saved_mic),
                              (self.system_device_combo, saved_sys)):
            for i in range(combo.count()):
                if combo.itemData(i) == saved:
                    combo.setCurrentIndex(i)
                    break

    def _load_values(self):
        """Populate UI from saved settings."""
        self.project_folder_input.setText(self.settings.get("project_folder"))

        # Audio source — find the dropdown index matching the saved value
        saved_src = self.settings.get("audio_source", "both")
        for i in range(self.audio_source_combo.count()):
            if self.audio_source_combo.itemData(i) == saved_src:
                self.audio_source_combo.setCurrentIndex(i)
                break

        # Transcription backend
        saved_backend = self.settings.get("stt_backend", "local")
        for i in range(self.stt_backend_combo.count()):
            if self.stt_backend_combo.itemData(i) == saved_backend:
                self.stt_backend_combo.setCurrentIndex(i)
                break
        self.groq_key_input.setText(self.settings.get("groq_api_key", ""))
        saved_groq_model = self.settings.get("groq_model", "whisper-large-v3-turbo")
        for i in range(self.groq_model_combo.count()):
            if self.groq_model_combo.itemData(i) == saved_groq_model:
                self.groq_model_combo.setCurrentIndex(i)
                break
        self.fallback_check.setChecked(
            self.settings.get("cloud_stt_fallback_local", True))
        self._on_backend_changed(0)  # sync enabled/disabled state

        # Quality preset — match by data, not text
        saved_quality = self.settings.get("whisper_quality", "balanced")
        for i in range(self.quality_combo.count()):
            if self.quality_combo.itemData(i) == saved_quality:
                self.quality_combo.setCurrentIndex(i)
                break

        self.model_combo.setCurrentText(self.settings.get("whisper_model", "auto"))
        self.language_input.setText(self.settings.get("transcription_language") or "")

        self.live_check.setChecked(self.settings.get("live_transcription", True))
        saved_live = self.settings.get("live_model", "base")
        for i in range(self.live_model_combo.count()):
            if self.live_model_combo.itemData(i) == saved_live:
                self.live_model_combo.setCurrentIndex(i)
                break

        self.diarization_check.setChecked(self.settings.get("diarization_enabled", False))
        self.hf_token_input.setText(self.settings.get("hf_token", ""))
        self.max_speakers_spin.setValue(self.settings.get("max_speakers", 10))

        self.llm_combo.setCurrentText(self.settings.get("llm_backend", "none"))
        self.llm_model_input.setText(self.settings.get("llm_model", "llama3.1:8b"))
        self.llm_api_key_input.setText(self.settings.get("llm_api_key", ""))
        self.cloud_check.setChecked(self.settings.get("allow_cloud_llm", False))

        self.auto_cleanup_check.setChecked(
            self.settings.get("auto_cleanup_temp", True))
        self.cleanup_save_check.setChecked(
            self.settings.get("cleanup_after_save", True))
        self.retention_spin.setValue(
            self.settings.get("temp_retention_hours", 24))

    def _on_llm_changed(self, text):
        """Toggle API key field based on backend selection.
        Groq is enabled too (optional separate key) — if left empty it
        falls back to the Groq transcription key."""
        self.llm_api_key_input.setEnabled(text in ("openai", "anthropic", "groq"))
        # Helpful default model per backend
        defaults = {
            "groq": "llama-3.3-70b-versatile",
            "ollama": "llama3.1:8b",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-sonnet-4-20250514",
        }
        if text in defaults and not self.llm_model_input.text().strip():
            self.llm_model_input.setText(defaults[text])

    def _on_backend_changed(self, _index):
        """Enable Groq fields only when the Groq backend is selected."""
        is_groq = self.stt_backend_combo.currentData() == "groq"
        self.groq_key_input.setEnabled(is_groq)
        self.groq_model_combo.setEnabled(is_groq)
        self.fallback_check.setEnabled(is_groq)

    def _browse_project_folder(self):
        """Open folder picker for project directory."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Project Folder",
            self.project_folder_input.text()
        )
        if folder:
            self.project_folder_input.setText(folder)

    def _on_save(self):
        """Save all settings."""
        self.settings.set("project_folder", self.project_folder_input.text())

        # Audio
        self.settings.set("audio_source", self.audio_source_combo.currentData())
        self.settings.set("mic_device_index", self.mic_device_combo.currentData())
        self.settings.set("system_device_index", self.system_device_combo.currentData())

        self.settings.set("stt_backend", self.stt_backend_combo.currentData())
        self.settings.set("groq_api_key", self.groq_key_input.text().strip())
        self.settings.set("groq_model", self.groq_model_combo.currentData())
        self.settings.set("cloud_stt_fallback_local", self.fallback_check.isChecked())

        self.settings.set("whisper_quality", self.quality_combo.currentData())
        self.settings.set("whisper_model", self.model_combo.currentText())
        lang = self.language_input.text().strip()
        self.settings.set("transcription_language", lang if lang else None)

        self.settings.set("live_transcription", self.live_check.isChecked())
        self.settings.set("live_model", self.live_model_combo.currentData())

        self.settings.set("diarization_enabled", self.diarization_check.isChecked())
        self.settings.set("hf_token", self.hf_token_input.text())
        self.settings.set("max_speakers", self.max_speakers_spin.value())

        self.settings.set("llm_backend", self.llm_combo.currentText())
        self.settings.set("llm_model", self.llm_model_input.text())
        self.settings.set("llm_api_key", self.llm_api_key_input.text())
        self.settings.set("allow_cloud_llm", self.cloud_check.isChecked())

        self.settings.set("auto_cleanup_temp", self.auto_cleanup_check.isChecked())
        self.settings.set("cleanup_after_save", self.cleanup_save_check.isChecked())
        self.settings.set("temp_retention_hours", self.retention_spin.value())

        self.settings.save()
        QMessageBox.information(self, "Saved", "Settings saved successfully.")

    def _on_reset(self):
        """Reset settings to defaults."""
        reply = QMessageBox.question(
            self, "Reset Settings",
            "Reset all settings to defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.settings.reset_to_defaults()
            self._load_values()

    # ─── Storage ─────────────────────────────────────────────────────

    def _refresh_storage(self):
        """Show current temp storage usage."""
        try:
            from src.utils.housekeeping import get_storage_usage
            u = get_storage_usage()
            self.storage_label.setText(
                f"<b>{u['temp_total_mb']:.0f} MB</b> temporary "
                f"(recordings {u['recordings_mb']:.0f} MB · "
                f"imports {u['imports_mb']:.0f} MB · "
                f"other {u['other_temp_mb']:.0f} MB)<br>"
                f"AI models: {u['models_mb']:.0f} MB · "
                f"cloud jobs: {u['cloud_jobs_mb']:.1f} MB"
            )
        except Exception as e:
            self.storage_label.setText(f"Could not read usage: {e}")

    def _on_clean_now(self):
        """Delete all temp working files immediately."""
        reply = QMessageBox.question(
            self, "Clean Temporary Files",
            "Delete all temporary working files now?\n\n"
            "Saved meetings (.mscribe), transcripts and generated documents "
            "are NOT affected.\n\n"
            "Note: this will also drop any in-progress cloud transcription "
            "resume data.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            from src.utils.housekeeping import cleanup_temp, cleanup_cloud_jobs
            result = cleanup_temp(aggressive=True)
            cleanup_cloud_jobs(retention_days=0)
            self._refresh_storage()
            QMessageBox.information(
                self, "Cleaned",
                f"Removed {result['files_removed']} item(s), "
                f"freed {result['freed_mb']:.0f} MB."
            )
        except Exception as e:
            QMessageBox.warning(self, "Cleanup Failed", str(e))

    # ─── Microphone test ─────────────────────────────────────────────

    def _on_test_mic(self):
        """Record 3 seconds from the selected mic and show the live RMS level.

        This lets the user verify a Bluetooth / USB headset actually picks up
        audio BEFORE they commit to a recording. If the bar never moves, the
        device is wrong (often the A2DP profile instead of HFP).
        """
        device_index = self.mic_device_combo.currentData()
        device_label = self.mic_device_combo.currentText()

        if hasattr(self, "_test_thread") and self._test_thread is not None \
                and self._test_thread.isRunning():
            return  # already testing

        self.test_mic_btn.setEnabled(False)
        self.test_mic_meter.setFormat(f"Testing {device_label}...")
        self.test_mic_meter.setValue(0)

        self._test_thread = MicTestThread(device_index, duration=3.0)
        self._test_thread.level.connect(self._on_test_level)
        self._test_thread.done.connect(self._on_test_done)
        self._test_thread.failed.connect(self._on_test_failed)
        self._test_thread.start()

    def _on_test_level(self, level: float):
        # level is RMS 0.0 – 1.0
        self.test_mic_meter.setValue(min(100, int(level * 100 * 4)))

    def _on_test_done(self, peak: float):
        self.test_mic_btn.setEnabled(True)
        if peak < 0.01:
            self.test_mic_meter.setFormat("⚠ No audio detected — wrong device?")
        elif peak < 0.05:
            self.test_mic_meter.setFormat(f"Quiet (peak {peak:.2f}) — speak louder")
        else:
            self.test_mic_meter.setFormat(f"✓ OK (peak {peak:.2f})")

    def _on_test_failed(self, message: str):
        self.test_mic_btn.setEnabled(True)
        self.test_mic_meter.setValue(0)
        self.test_mic_meter.setFormat(f"✗ {message[:60]}")


class MicTestThread(QThread):
    """Runs a short live capture so the Settings page can show a level meter.

    Emits `level` ~10x per second while recording, then `done(peak)` when the
    duration elapses, or `failed(message)` if the device can't be opened.
    """
    level = pyqtSignal(float)
    done = pyqtSignal(float)
    failed = pyqtSignal(str)

    def __init__(self, device_index, duration: float = 3.0):
        super().__init__()
        self.device_index = device_index
        self.duration = duration

    def run(self):
        import time
        import numpy as np
        try:
            import sounddevice as sd

            if self.device_index is not None:
                dev_info = sd.query_devices(self.device_index, 'input')
            else:
                dev_info = sd.query_devices(kind='input')

            if dev_info.get('max_input_channels', 0) <= 0:
                self.failed.emit(
                    "Selected device has no input channels. "
                    "For Bluetooth, pick the Hands-Free/Headset entry."
                )
                return

            sr = int(dev_info['default_samplerate'])
            channels = min(dev_info['max_input_channels'], 2)
            peak = 0.0
            t_end = time.time() + self.duration

            def cb(indata, frames, time_info, status):
                nonlocal peak
                audio = indata.astype(np.float32).flatten() if channels == 1 \
                    else indata.astype(np.float32).mean(axis=1)
                if len(audio):
                    rms = float(np.sqrt(np.mean(audio ** 2)))
                    self.level.emit(rms)
                    if rms > peak:
                        peak = rms

            with sd.InputStream(device=self.device_index, samplerate=sr,
                                channels=channels, dtype='float32',
                                blocksize=1024, callback=cb):
                while time.time() < t_end:
                    time.sleep(0.05)

            self.done.emit(peak)

        except Exception as e:
            self.failed.emit(str(e))
