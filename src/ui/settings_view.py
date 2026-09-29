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
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from src.core.settings import Settings
from src.core.models import LLMBackend, AudioSource
from src.core.devices import (
    DeviceSnapshot, scan_devices, resolve_plan, conflict_warning,
)
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
        self.audio_source_combo.currentIndexChanged.connect(lambda _: self._update_plan_preview())
        audio_layout.addRow("Source:", self.audio_source_combo)

        mic_row = QHBoxLayout()
        self.mic_device_combo = QComboBox()
        self.mic_device_combo.setMinimumWidth(320)
        self.mic_device_combo.currentIndexChanged.connect(lambda _: self._update_plan_preview())
        mic_row.addWidget(self.mic_device_combo)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setToolTip("Re-scan audio devices now (they also refresh "
                                    "automatically every few seconds)")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.clicked.connect(self._refresh_audio_devices)
        mic_row.addWidget(self.refresh_btn)
        audio_layout.addRow("Microphone:", mic_row)

        self.system_device_combo = QComboBox()
        self.system_device_combo.setMinimumWidth(320)
        self.system_device_combo.currentIndexChanged.connect(lambda _: self._update_plan_preview())
        audio_layout.addRow("System Audio:", self.system_device_combo)

        self.prefer_bt_check = QCheckBox(
            "Prefer the first Bluetooth headset connected (also when it connects "
            "during a recording)")
        self.prefer_bt_check.toggled.connect(lambda _: self._update_plan_preview())
        audio_layout.addRow("", self.prefer_bt_check)

        self.bt_mode_combo = QComboBox()
        self.bt_mode_combo.addItem("Use its microphone AND system audio "
                                   "(phone-call quality)", "both")
        self.bt_mode_combo.addItem("Use it for system audio only — keep the "
                                   "other microphone (better quality)", "system")
        self.bt_mode_combo.currentIndexChanged.connect(lambda _: self._update_plan_preview())
        audio_layout.addRow("Bluetooth headset:", self.bt_mode_combo)

        self.plan_label = QLabel("")
        self.plan_label.setWordWrap(True)
        self.plan_label.setStyleSheet("color: #6366f1; font-size: 11px;")
        audio_layout.addRow("Right now:", self.plan_label)

        self.conflict_label = QLabel("")
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet("color: #ef4444; font-size: 11px;")
        self.conflict_label.hide()
        audio_layout.addRow("", self.conflict_label)

        audio_hint = QLabel(
            "\"Automatic\" is recommended: the app uses the first Bluetooth headset "
            "you connect, otherwise the Windows default devices — and switches on "
            "its own if you connect or disconnect a headset mid-recording. "
            "Devices are remembered by name, so plugging things in or out "
            "doesn't change your choice.")
        audio_hint.setStyleSheet("color: #707088; font-size: 11px;")
        audio_hint.setWordWrap(True)
        audio_layout.addRow("", audio_hint)

        test_row = QHBoxLayout()
        self.test_mic_btn = QPushButton("🎤 Test audio setup (3 s)")
        self.test_mic_btn.setToolTip("Records 3 seconds from exactly what a recording "
                                     "would use — mic AND system audio together.")
        self.test_mic_btn.clicked.connect(self._on_test_mic)
        test_row.addWidget(self.test_mic_btn)
        meters = QVBoxLayout()
        self.test_mic_meter = QProgressBar()
        self.test_mic_meter.setRange(0, 100)
        self.test_mic_meter.setTextVisible(True)
        self.test_mic_meter.setFormat("Mic: idle")
        self.test_mic_meter.setMinimumWidth(240)
        self.test_sys_meter = QProgressBar()
        self.test_sys_meter.setRange(0, 100)
        self.test_sys_meter.setTextVisible(True)
        self.test_sys_meter.setFormat("System audio: idle")
        meters.addWidget(self.test_mic_meter)
        meters.addWidget(self.test_sys_meter)
        test_row.addLayout(meters)
        audio_layout.addRow("", test_row)

        layout.addWidget(audio_group)

        # Device lists: scanned in a helper process (sees headsets connected
        # after the app started) and refreshed automatically while visible.
        self._snapshot: DeviceSnapshot = DeviceSnapshot()
        self._scan_thread = None
        self._device_timer = QTimer(self)
        self._device_timer.setInterval(5000)
        self._device_timer.timeout.connect(self._auto_refresh_devices)
        self._device_timer.start()
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

        self.llm_info = QLabel("")
        self.llm_info.setOpenExternalLinks(True)
        self.llm_info.setStyleSheet("color: #707088; font-size: 11px;")
        self.llm_info.setWordWrap(True)
        llm_layout.addRow("", self.llm_info)
        llm_layout.addRow("", QLabel(
            "<span style='color:#707088;font-size:11px;'>This powers the in-app "
            "Analysis panel and \u201cAsk AI\u201d button only. Documents written by "
            "Claude in Cowork don't use this setting.</span>"))

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
        """Re-scan devices in the background (helper process → fresh list)."""
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self._scan_thread = DeviceScanThread()
        self._scan_thread.done.connect(self._on_devices_scanned)
        self._scan_thread.start()

    def shutdown(self):
        """Stop background device scanning (called when the app closes)."""
        self._device_timer.stop()
        for t in (self._scan_thread, getattr(self, "_test_thread", None)):
            if t is not None and t.isRunning():
                t.wait(3000)

    def _auto_refresh_devices(self):
        if self.isVisible():
            self._refresh_audio_devices()

    def _on_devices_scanned(self, snapshot: DeviceSnapshot):
        self.refresh_btn.setEnabled(True)
        changed = snapshot.signature() != self._snapshot.signature()
        self._snapshot = snapshot
        if changed or self.mic_device_combo.count() == 0:
            self._fill_device_combos()
        self._update_plan_preview()

    def _fill_device_combos(self):
        snap = self._snapshot
        bt_bases = {g.base for g in snap.bluetooth_groups()}

        def label(dev):
            tag = "🎧 " if dev.base in bt_bases else ""
            default = "  (Windows default)" if dev.is_default else ""
            return f"{tag}{dev.display_name}{default}"

        for combo, devices, auto_text, setting in (
                (self.mic_device_combo, snap.inputs(),
                 "Automatic — Bluetooth headset first, then Windows default", "mic_device"),
                (self.system_device_combo, snap.loopbacks(),
                 "Automatic — Bluetooth headset first, then Windows default output",
                 "system_device")):
            current = combo.currentData() if combo.count() else self.settings.get(setting, "")
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(auto_text, "")
            for d in devices:
                combo.addItem(label(d), d.key)
            if current and combo.findData(current) < 0:
                # Keep a saved choice visible even while it's unplugged.
                name = current.split(":", 1)[-1]
                combo.addItem(f"⚠ {name}  (not connected — Automatic is used)", current)
            idx = combo.findData(current or "")
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _update_plan_preview(self):
        snap = self._snapshot
        if snap.error:
            self.plan_label.setText(f"⚠ {snap.error}")
            return
        if not snap.devices:
            self.plan_label.setText("Scanning devices…")
            return
        mic_pref = self.mic_device_combo.currentData() or None
        sys_pref = self.system_device_combo.currentData() or None
        plan = resolve_plan(snap, source=self.audio_source_combo.currentData() or "both",
                            mic_pref=mic_pref, system_pref=sys_pref,
                            prefer_bluetooth=self.prefer_bt_check.isChecked(),
                            bluetooth_mode=self.bt_mode_combo.currentData() or "both")
        self.plan_label.setText("A recording would use " + plan.describe())
        self.bt_mode_combo.setEnabled(self.prefer_bt_check.isChecked())
        warn = conflict_warning(snap, mic_pref, sys_pref)
        self.conflict_label.setVisible(bool(warn))
        self.conflict_label.setText(f"⚠ {warn}" if warn else "")

    def _load_values(self):
        """Populate UI from saved settings."""
        self.project_folder_input.setText(self.settings.get("project_folder"))

        # Audio source — find the dropdown index matching the saved value
        saved_src = self.settings.get("audio_source", "both")
        for i in range(self.audio_source_combo.count()):
            if self.audio_source_combo.itemData(i) == saved_src:
                self.audio_source_combo.setCurrentIndex(i)
                break
        self.prefer_bt_check.setChecked(bool(self.settings.get("prefer_bluetooth", True)))
        idx = self.bt_mode_combo.findData(self.settings.get("bluetooth_mode", "both"))
        self.bt_mode_combo.setCurrentIndex(max(0, idx))
        for combo, key in ((self.mic_device_combo, "mic_device"),
                           (self.system_device_combo, "system_device")):
            i = combo.findData(self.settings.get(key, "") or "")
            if i >= 0:
                combo.setCurrentIndex(i)

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
        self.llm_model_input.setText(
            valid_model(self.llm_combo.currentText(), self.settings.get("llm_model", "")))
        self._on_llm_changed(self.llm_combo.currentText())
        self.llm_api_key_input.setText(self.settings.get("llm_api_key", ""))
        self.cloud_check.setChecked(self.settings.get("allow_cloud_llm", False))

        self.auto_cleanup_check.setChecked(
            self.settings.get("auto_cleanup_temp", True))
        self.cleanup_save_check.setChecked(
            self.settings.get("cleanup_after_save", True))
        self.retention_spin.setValue(
            self.settings.get("temp_retention_hours", 24))

    def _on_llm_changed(self, text):
        """Keep Model / API key / hint consistent with the chosen backend."""
        text = text or "none"
        self.llm_model_input.setEnabled(text != "none")
        self.llm_api_key_input.setEnabled(text in ("groq", "openai", "anthropic"))
        self.llm_api_key_input.setPlaceholderText({
            "groq": "Optional — leave empty to reuse your Groq transcription key",
            "openai": "Required — your paid OpenAI API key",
            "anthropic": "Required — paid Anthropic API key (not your Claude subscription)",
        }.get(text, "Not needed for this backend"))
        self.llm_model_input.setText(valid_model(text, self.llm_model_input.text()))
        self.llm_info.setText({
            "none": "No in-app AI. The Analysis panel stays empty; transcripts and "
                    "request files for Claude are still created.",
            "groq": "<b style='color:#22c55e;'>FREE</b> (Groq free tier). Uses your Groq "
                    "transcription key when the field above is empty. Recommended model: "
                    "<code>llama-3.3-70b-versatile</code>. Drafts — verify before sharing.",
            "ollama": "Free &amp; fully local: install <a style='color:#6366f1;' "
                      "href='https://ollama.com'>ollama.com</a>, then run "
                      "<code>ollama pull llama3.1:8b</code>.",
            "openai": "<b style='color:#f59e0b;'>PAID</b> — billed per use to your "
                      "OpenAI API account.",
            "anthropic": "<b style='color:#f59e0b;'>PAID</b> — billed per use to an "
                         "Anthropic API account. A Claude Pro/Max subscription does "
                         "<b>not</b> include API access.",
        }.get(text, ""))

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
        self.settings.set("mic_device", self.mic_device_combo.currentData() or "")
        self.settings.set("system_device", self.system_device_combo.currentData() or "")
        self.settings.set("prefer_bluetooth", self.prefer_bt_check.isChecked())
        self.settings.set("bluetooth_mode", self.bt_mode_combo.currentData() or "both")

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
        self.settings.set("llm_model", valid_model(self.llm_combo.currentText(),
                                                   self.llm_model_input.text()))
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
        """Record 3 s from exactly the devices a recording would use."""
        if getattr(self, "_test_thread", None) is not None and self._test_thread.isRunning():
            return
        self.test_mic_btn.setEnabled(False)
        self.test_mic_meter.setValue(0)
        self.test_sys_meter.setValue(0)
        self.test_mic_meter.setFormat("Mic: testing…")
        self.test_sys_meter.setFormat("System audio: testing — play some sound…")
        self._test_thread = AudioTestThread(
            source=self.audio_source_combo.currentData() or "both",
            mic_pref=self.mic_device_combo.currentData() or None,
            sys_pref=self.system_device_combo.currentData() or None,
            prefer_bluetooth=self.prefer_bt_check.isChecked(),
            bluetooth_mode=self.bt_mode_combo.currentData() or "both",
        )
        self._test_thread.level.connect(self._on_test_level)
        self._test_thread.done.connect(self._on_test_done)
        self._test_thread.failed.connect(self._on_test_failed)
        self._test_thread.start()

    def _on_test_level(self, role: str, level: float):
        bar = self.test_mic_meter if role == "mic" else self.test_sys_meter
        bar.setValue(min(100, int(level * 400)))

    def _on_test_done(self, result: dict):
        self.test_mic_btn.setEnabled(True)
        mic = result.get("mic")
        if mic is None:
            self.test_mic_meter.setFormat("Mic: not used")
        elif mic["peak"] < 0.01:
            self.test_mic_meter.setFormat(f"⚠ Mic: no sound — {mic['device'][:30]}")
        elif mic["peak"] < 0.05:
            self.test_mic_meter.setFormat(f"Mic quiet — speak up ({mic['device'][:28]})")
        else:
            self.test_mic_meter.setFormat(f"✓ Mic OK — {mic['device'][:34]}")
        sys_ = result.get("system")
        if sys_ is None:
            self.test_sys_meter.setFormat("System audio: not used")
        elif sys_["peak"] < 0.005:
            self.test_sys_meter.setFormat("System audio: nothing heard — play a video "
                                          "and test again")
        else:
            self.test_sys_meter.setFormat(f"✓ System audio OK — {sys_['device'][:28]}")

    def _on_test_failed(self, message: str):
        self.test_mic_btn.setEnabled(True)
        self.test_mic_meter.setValue(0)
        self.test_mic_meter.setFormat(f"✗ {message[:70]}")
        self.test_sys_meter.setFormat("")


_MODEL_DEFAULTS = {
    "groq": "llama-3.3-70b-versatile",
    "ollama": "llama3.1:8b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
}


def valid_model(backend: str, model: str) -> str:
    """Replace a model name that belongs to another backend with a sensible
    default (e.g. 'llama3.1:8b' is an Ollama name — Groq would reject it)."""
    model = (model or "").strip()
    if backend not in _MODEL_DEFAULTS:
        return model
    ok = {
        "groq": bool(model) and ":" not in model and "claude" not in model
                and not model.startswith("gpt"),
        "ollama": bool(model),
        "openai": model.startswith(("gpt", "o1", "o3", "o4")),
        "anthropic": model.startswith("claude"),
    }[backend]
    return model if ok else _MODEL_DEFAULTS[backend]


class DeviceScanThread(QThread):
    """Scan audio devices in a helper process without freezing the UI."""
    done = pyqtSignal(object)

    def run(self):
        try:
            snap = scan_devices()
        except Exception as e:
            snap = DeviceSnapshot(error=f"Device scan failed: {e}")
        self.done.emit(snap)


class AudioTestThread(QThread):
    """Open exactly the devices a recording would use for 3 seconds and
    report the level of the mic and of the system audio separately."""
    level = pyqtSignal(str, float)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, source, mic_pref, sys_pref, prefer_bluetooth, bluetooth_mode,
                 duration: float = 3.0):
        super().__init__()
        self.args = dict(source=source, mic_pref=mic_pref, system_pref=sys_pref,
                         prefer_bluetooth=prefer_bluetooth, bluetooth_mode=bluetooth_mode)
        self.duration = duration

    def run(self):
        import time
        import numpy as np
        try:
            import pyaudiowpatch as pa
        except ImportError:
            self.failed.emit("PyAudioWPatch is not installed")
            return
        from src.core.devices import scan_with
        p = pa.PyAudio()
        streams, peaks, devices = [], {}, {}
        try:
            plan = resolve_plan(scan_with(p), **self.args)
            wanted = ([("mic", plan.mic)] if plan.mic else []) + \
                     [("system", d) for d in plan.loopbacks]
            if not wanted:
                self.failed.emit("No audio devices found")
                return
            for role, dev in wanted:
                peaks.setdefault(role, 0.0)
                devices.setdefault(role, dev.display_name)

                def cb(data, frames, t, status, role=role, ch=max(1, dev.channels)):
                    a = np.frombuffer(data, dtype=np.float32)
                    if len(a):
                        rms = float(np.sqrt(np.mean(a ** 2)))
                        peaks[role] = max(peaks[role], rms)
                        self.level.emit(role, rms)
                    return (None, 0)

                s = p.open(format=pa.paFloat32, channels=max(1, dev.channels),
                           rate=int(dev.sample_rate), input=True,
                           input_device_index=dev.index, frames_per_buffer=1024,
                           stream_callback=cb)
                s.start_stream()
                streams.append(s)
            time.sleep(self.duration)
            self.done.emit({role: {"peak": peaks[role], "device": devices[role]}
                            for role in peaks})
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            for s in streams:
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
            try:
                p.terminate()
            except Exception:
                pass
