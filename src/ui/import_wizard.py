"""
Meeting Scribe — Import Wizard

A step-by-step pop-up inside Meeting Scribe that opens when you import audio
or video. The app can't see what the recording is about, so the wizard asks —
and everything you answer is saved with the meeting (meta.json, the
transcript .md and the request file) so documents can be written later with
full context.

Pages
  1. Files      one file or several parts of ONE meeting (ordering + ▶ checks)
  2. Details    title, date, client, engagement, meeting type, roles, topic,
                participants
  3. Language   spoken language (+ optional detection) and document language
  4. Context    key terms / correct spellings, instructions for the documents
  5. Documents  which documents to prepare (suggested by meeting type)
  6. Review     summary, time / quota / disk estimate, Start
"""
from __future__ import annotations

import os
import re
import logging
from datetime import datetime
from typing import List, Optional

from PyQt6.QtCore import Qt, QDate, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QCheckBox, QLineEdit, QComboBox, QPlainTextEdit, QDateEdit, QFileDialog,
    QMessageBox, QApplication, QWidget, QDialog,
)

from src.core.settings import Settings
from src.core.media_parts import (
    MediaInfo, probe_media, order_parts, estimate_gaps, duplicate_warnings,
    boundary_preview, language_sample, _NAME_DT_RE,
)
from src.core.import_plan import (
    ImportPlan, ImportPart, MEETING_TYPES, OUR_ROLES, LANGUAGES, DOC_LANGUAGES,
    DOCUMENTS, SUGGESTED_DOCUMENTS, estimate, detect_language,
)
from src.core.media_import import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

MEDIA_FILTER = ("Media Files (" + " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTENSIONS))
                + ");;All Files (*)")
HINT_STYLE = "color: #8b8ba0; font-size: 11px;"


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(HINT_STYLE)
    return lbl


def _fmt_dur(seconds: float) -> str:
    if not seconds:
        return "?"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def suggest_title(stem: str) -> str:
    t = _NAME_DT_RE.sub(" ", stem)
    t = re.sub(r"[_\-.]+", " ", t)
    t = re.sub(r"\b(part|pt|bagian|sesi|session)\s*\d+\b", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ─── Page 1: Files ──────────────────────────────────────────────────────

class FilesPage(QWizardPage):
    def __init__(self, initial_files: Optional[List[str]] = None):
        super().__init__()
        self.setTitle("1 · Files")
        self.setSubTitle("Choose the recording. If one meeting was saved as several "
                         "files, add all parts — they'll become one transcript.")
        self.items: List[MediaInfo] = []
        self.confidence = "high"
        self.order_reason = ""
        self.order_confirmed = True
        self._player = None
        self._audio_out = None

        lay = QVBoxLayout(self)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["#", "File", "Length", "Audio", "Recorded at", "Check boundary"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3, 4, 5):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        self.add_btn = QPushButton("＋ Add files…")
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._on_remove)
        self.up_btn = QPushButton("↑ Up")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("↓ Down")
        self.down_btn.clicked.connect(lambda: self._move(1))
        for b in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

        self.order_label = QLabel("")
        self.order_label.setWordWrap(True)
        lay.addWidget(self.order_label)

        self.confirm_order_btn = QPushButton("✓ I checked — the order is correct")
        self.confirm_order_btn.clicked.connect(self._on_confirm_order)
        lay.addWidget(self.confirm_order_btn)

        self.consecutive_check = QCheckBox(
            "These files are consecutive parts of ONE meeting")
        self.consecutive_check.toggled.connect(lambda _: self.completeChanged.emit())
        lay.addWidget(self.consecutive_check)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(HINT_STYLE)
        lay.addWidget(self.info_label)

        lay.addWidget(_hint("Only the audio is kept — the video picture is dropped. "
                            "mp4 and mp3 are read directly; no conversion needed."))

        if initial_files:
            self.add_paths(initial_files)
        self._refresh()

    # ── file handling ──
    def add_paths(self, paths: List[str]) -> None:
        known = {i.path for i in self.items}
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for p in paths:
                if p and p not in known and os.path.isfile(p):
                    self.items.append(probe_media(p))
        finally:
            QApplication.restoreOverrideCursor()
        self.items, self.confidence, self.order_reason = order_parts(self.items)
        self.order_confirmed = self.confidence != "low"
        self._refresh()

    def _on_add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add audio / video files", "",
                                                MEDIA_FILTER)
        if paths:
            self.add_paths(paths)

    def _on_remove(self):
        r = self.table.currentRow()
        if 0 <= r < len(self.items):
            self.items.pop(r)
            self._refresh()

    def _move(self, delta: int):
        r = self.table.currentRow()
        t = r + delta
        if 0 <= r < len(self.items) and 0 <= t < len(self.items):
            self.items[r], self.items[t] = self.items[t], self.items[r]
            # Arranging the parts yourself counts as confirming the order.
            self.order_confirmed = True
            self.order_reason = "Order set by you."
            self._refresh()
            self.table.selectRow(t)

    def _on_confirm_order(self):
        self.order_confirmed = True
        self.order_reason = "Order confirmed by you."
        self._refresh()

    def _refresh(self):
        n = len(self.items)
        self.table.setRowCount(n)
        for r, it in enumerate(self.items):
            vals = [str(r + 1), it.name, _fmt_dur(it.duration),
                    "✓" if it.has_audio else "✗ none",
                    it.recorded_at.strftime("%Y-%m-%d %H:%M") if it.recorded_at else "—"]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                if c == 3 and not it.has_audio:
                    cell.setForeground(Qt.GlobalColor.red)
                self.table.setItem(r, c, cell)
            if r < n - 1:
                btn = QPushButton(f"▶ {r + 1}→{r + 2}")
                btn.setToolTip("Play the last 10 s of this part and the first 10 s "
                               "of the next — the conversation should flow on.")
                btn.clicked.connect(lambda _=False, i=r: self._preview(i))
                self.table.setCellWidget(r, 5, btn)
            else:
                self.table.setCellWidget(r, 5, QWidget())

        multi = n > 1
        self.consecutive_check.setVisible(multi)
        self.up_btn.setEnabled(multi)
        self.down_btn.setEnabled(multi)
        self.remove_btn.setEnabled(n > 0)

        if not multi:
            self.order_label.setText("")
            self.confirm_order_btn.setVisible(False)
        elif self.order_confirmed:
            self.order_label.setText(f"✓ {self.order_reason}")
            self.order_label.setStyleSheet("color: #22c55e;")
            self.confirm_order_btn.setVisible(False)
        else:
            self.order_label.setText(f"⚠ Order not confirmed. {self.order_reason}")
            self.order_label.setStyleSheet("color: #f59e0b;")
            self.confirm_order_btn.setVisible(True)

        notes = []
        if n:
            total = sum(i.duration for i in self.items)
            notes.append(f"Total length: {_fmt_dur(total)}")
        for it in self.items:
            if it.error:
                notes.append(f"⚠ {it.name}: {it.error}")
            elif not it.has_audio:
                notes.append(f"⚠ {it.name} has no audio track and can't be transcribed.")
        notes += [f"⚠ {w}" for w in duplicate_warnings(self.items)]
        gaps = estimate_gaps(self.items) if multi else []
        for k, g in enumerate(gaps):
            if g and g >= 30:
                notes.append(f"Break before part {k + 1}: ≈ {round(g / 60)} min "
                             "(from the recording times; the transcript timeline "
                             "still runs straight on).")
        self.info_label.setText("\n".join(notes))
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        if not self.items:
            return False
        if any(i.error or not i.has_audio for i in self.items):
            return False
        if len(self.items) > 1:
            return self.consecutive_check.isChecked() and self.order_confirmed
        return True

    # ── ▶ boundary preview ──
    def _preview(self, i: int):
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
            from src.utils.file_utils import get_temp_dir
            out = os.path.join(str(get_temp_dir()), f"preview_boundary_{i + 1}.wav")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                boundary_preview(self.items[i], self.items[i + 1], out)
            finally:
                QApplication.restoreOverrideCursor()
            if self._player is None:
                self._player = QMediaPlayer(self)
                self._audio_out = QAudioOutput(self)
                self._player.setAudioOutput(self._audio_out)
            self._player.stop()
            self._player.setSource(QUrl.fromLocalFile(out))
            self._player.play()
        except Exception as e:
            QMessageBox.warning(self, "Preview", f"Could not play the preview:\n{e}")

    def cleanupPage(self):
        if self._player is not None:
            self._player.stop()


# ─── Page 2: Details ────────────────────────────────────────────────────

class DetailsPage(QWizardPage):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.setTitle("2 · Meeting details")
        self.setSubTitle("Tell the app what this meeting is. This context is saved "
                         "with the transcript and used when documents are written.")
        form = QFormLayout(self)

        self.title_edit = QLineEdit()
        self.title_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        form.addRow("Title *", self.title_edit)

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("Date", self.date_edit)

        self.client_combo = QComboBox()
        self.client_combo.setEditable(True)
        self.client_combo.addItems([""] + list(settings.get("recent_clients") or []))
        self.client_combo.lineEdit().setPlaceholderText("e.g. CIMB Niaga")
        form.addRow("Client / organisation", self.client_combo)

        self.engagement_combo = QComboBox()
        self.engagement_combo.setEditable(True)
        self.engagement_combo.addItems([""] + list(settings.get("recent_engagements") or []))
        self.engagement_combo.lineEdit().setPlaceholderText("e.g. FDS replacement RFP")
        form.addRow("Engagement / project", self.engagement_combo)

        self.type_combo = QComboBox()
        self.type_combo.addItems(MEETING_TYPES)
        form.addRow("Meeting type", self.type_combo)

        self.role_combo = QComboBox()
        self.role_combo.addItems(OUR_ROLES)
        form.addRow("Our role", self.role_combo)

        self.topic_edit = QPlainTextEdit()
        self.topic_edit.setPlaceholderText(
            "Purpose / agenda, e.g. \"Clarification session for the FDS RFP — "
            "scope Phase 1 OCTO channel, timeline, POC schedule\"")
        self.topic_edit.setFixedHeight(64)
        form.addRow("Topic / purpose", self.topic_edit)

        self.participants_edit = QPlainTextEdit()
        self.participants_edit.setPlaceholderText(
            "One per line — Name — role / organisation\n"
            "Pak Kadek — CIMB procurement\nMas Rama — CIMB fraud team")
        self.participants_edit.setFixedHeight(84)
        form.addRow("Participants", self.participants_edit)
        form.addRow("", _hint("Participants help spell names correctly. Speaker "
                              "labels in the transcript can't be matched to names "
                              "automatically."))

    def prefill(self, items: List[MediaInfo]):
        if not items or self.title_edit.text().strip():
            return
        first = items[0]
        self.title_edit.setText(suggest_title(os.path.splitext(first.name)[0]))
        when = first.recorded_at or first.name_time or first.mtime or datetime.now()
        self.date_edit.setDate(QDate(when.year, when.month, when.day))

    def isComplete(self) -> bool:
        return bool(self.title_edit.text().strip())


# ─── Page 3: Language ───────────────────────────────────────────────────

class _DetectThread(QThread):
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, info: MediaInfo, settings: Settings):
        super().__init__()
        self.info, self.settings = info, settings

    def run(self):
        try:
            audio = language_sample(self.info)
            if len(audio) < 16000 * 5:
                self.failed.emit("Not enough audio to detect a language.")
                return
            self.done.emit(detect_language(audio, self.settings))
        except Exception as e:
            self.failed.emit(str(e))


class LanguagePage(QWizardPage):
    def __init__(self, settings: Settings, files_page: FilesPage):
        super().__init__()
        self.settings, self.files_page = settings, files_page
        self.setTitle("3 · Language")
        self.setSubTitle("Setting the spoken language improves accuracy a lot — "
                         "especially for Bahasa Indonesia / Malaysia.")
        form = QFormLayout(self)

        self.lang_combo = QComboBox()
        for label, code in LANGUAGES:
            self.lang_combo.addItem(label, code)
        saved = settings.get("transcription_language")
        if saved:
            idx = next((i for i in range(self.lang_combo.count())
                        if self.lang_combo.itemData(i) == saved), 0)
            self.lang_combo.setCurrentIndex(idx)
        form.addRow("Spoken language", self.lang_combo)

        det_row = QHBoxLayout()
        self.detect_btn = QPushButton("🔎 Detect from first 30 seconds")
        self.detect_btn.clicked.connect(self._on_detect)
        det_row.addWidget(self.detect_btn)
        det_row.addStretch()
        form.addRow("", det_row)
        self.detect_label = QLabel("")
        self.detect_label.setWordWrap(True)
        form.addRow("", self.detect_label)
        form.addRow("", _hint("Detection listens to ~30 seconds (after the first minute "
                              "when the file is long). With Groq it takes a few "
                              "seconds; locally it may take 10–20 s the first time "
                              "while the model loads. Indonesian and Malay sound "
                              "alike — check the suggestion."))

        self.doclang_combo = QComboBox()
        self.doclang_combo.addItems(DOC_LANGUAGES)
        form.addRow("Documents written in", self.doclang_combo)
        self._thread = None

    def _on_detect(self):
        items = self.files_page.items
        if not items:
            return
        self.detect_btn.setEnabled(False)
        self.detect_label.setStyleSheet("color: #8b8ba0;")
        self.detect_label.setText("Listening…")
        self._thread = _DetectThread(items[0], self.settings)
        self._thread.done.connect(self._on_detected)
        self._thread.failed.connect(self._on_detect_failed)
        self._thread.start()

    def _on_detected(self, res: dict):
        self.detect_btn.setEnabled(True)
        code, name = res.get("code", ""), res.get("name", "")
        prob = res.get("probability")
        txt = f"Detected: <b>{name}</b>"
        if prob:
            txt += f" ({prob:.0%})"
        alts = [f"{n} {p:.0%}" for n, p in res.get("alternatives", [])[1:3]]
        if alts:
            txt += " · also possible: " + ", ".join(alts)
        txt += f" · via {res.get('method')}"
        self.detect_label.setStyleSheet("color: #22c55e;")
        self.detect_label.setText(txt)
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == code and "Mixed" not in self.lang_combo.itemText(i):
                self.lang_combo.setCurrentIndex(i)
                break

    def _on_detect_failed(self, msg: str):
        self.detect_btn.setEnabled(True)
        self.detect_label.setStyleSheet("color: #f59e0b;")
        self.detect_label.setText(f"Could not detect: {msg[:160]}")


# ─── Page 4: Context ────────────────────────────────────────────────────

class ContextPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("4 · Context for accuracy")
        self.setSubTitle("Optional, but it fixes the words speech recognition "
                         "gets wrong (product names, acronyms, companies).")
        form = QFormLayout(self)
        self.terms_edit = QPlainTextEdit()
        self.terms_edit.setPlaceholderText(
            "Comma or line separated, e.g.\nCIMB Niaga, OCTO, OCTO Clicks, Safer Payments, "
            "MSA, POC, UAT, SaaS")
        self.terms_edit.setFixedHeight(90)
        form.addRow("Key terms / spellings", self.terms_edit)
        self.names_check = QCheckBox("Also use the client, engagement and participant "
                                     "names as spelling hints")
        self.names_check.setChecked(True)
        form.addRow("", self.names_check)
        self.instr_edit = QPlainTextEdit()
        self.instr_edit.setPlaceholderText(
            "Anything the documents should focus on, e.g. \"Highlight commercial "
            "deadlines and open questions for the client; formal tone.\"")
        self.instr_edit.setFixedHeight(80)
        form.addRow("Instructions for the documents", self.instr_edit)

    def terms(self) -> List[str]:
        raw = self.terms_edit.toPlainText().replace("\n", ",")
        return [t.strip() for t in raw.split(",") if t.strip()]


# ─── Page 5: Documents ──────────────────────────────────────────────────

class DocumentsPage(QWizardPage):
    def __init__(self, details: DetailsPage):
        super().__init__()
        self.details = details
        self.setTitle("5 · Documents")
        self.setSubTitle("Which documents should be prepared from this meeting?")
        lay = QVBoxLayout(self)
        self.suggest_label = QLabel("")
        self.suggest_label.setStyleSheet("color: #6366f1;")
        lay.addWidget(self.suggest_label)
        self.checks = {}
        for key, (label, instr) in DOCUMENTS.items():
            cb = QCheckBox(label)
            cb.setToolTip(instr)
            lay.addWidget(cb)
            self.checks[key] = cb
        lay.addSpacing(6)
        lay.addWidget(QLabel("Something else (optional):"))
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("e.g. Follow-up email to the client")
        lay.addWidget(self.custom_edit)
        lay.addSpacing(8)
        lay.addWidget(_hint(
            "Documents are NOT written automatically. The app saves the transcript "
            "and a request file listing these documents plus all the context from "
            "this wizard. When you're ready, ask Claude to produce them (the Home "
            "screen's queue gives you a ready-made prompt). Untick everything for "
            "a transcript only."))
        lay.addStretch()
        self._applied_for = None

    def initializePage(self):
        mtype = self.details.type_combo.currentText()
        if self._applied_for != mtype:
            wanted = SUGGESTED_DOCUMENTS.get(mtype, ["mom"])
            for key, cb in self.checks.items():
                cb.setChecked(key in wanted)
            self._applied_for = mtype
            self.suggest_label.setText(f"Suggested for “{mtype}”: " + ", ".join(
                DOCUMENTS[k][0] for k in wanted))

    def selected(self) -> List[str]:
        return [k for k, cb in self.checks.items() if cb.isChecked()]


# ─── Page 6: Review ─────────────────────────────────────────────────────

class ReviewPage(QWizardPage):
    def __init__(self, wizard: "ImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("6 · Review & start")
        self.setSubTitle("Check everything, then click Start.")
        lay = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary)
        self.autosave_check = QCheckBox("Save the meeting automatically when "
                                        "transcription finishes")
        self.autosave_check.setChecked(bool(wizard.settings.get("auto_save_after_import", True)))
        lay.addWidget(self.autosave_check)
        lay.addStretch()

    def initializePage(self):
        plan = self.wiz.build_plan()
        s = self.wiz.settings
        backend = s.get("stt_backend", "local")
        model = s.get("whisper_model", "auto")
        if model == "auto":
            try:
                from src.utils.hardware_probe import recommend_whisper_model
                model = recommend_whisper_model(s.get("whisper_quality", "balanced"))
            except Exception:
                model = "small"
        est = estimate(plan, backend, model)

        def esc(t):
            return (t or "—").replace("&", "&amp;").replace("<", "&lt;")

        parts = "".join(f"<li>{esc(p.name)} — {_fmt_dur(p.duration)}</li>" for p in plan.parts)
        docs = ", ".join(plan.document_labels()) or "none (transcript only)"
        resume = ""
        try:
            from src.core.pipeline import _import_jobs_dir
            from src.utils.file_utils import safe_read_json
            job = safe_read_json(os.path.join(_import_jobs_dir(),
                                              f"import_{plan.job_id}.json")) or {}
            done = len(job.get("parts", {}))
            if done:
                resume = (f"<p style='color:#22c55e'>↻ {done} of {len(plan.parts)} "
                          "part(s) were already transcribed earlier — they'll be reused.</p>")
        except Exception:
            pass
        self.summary.setText(
            f"<b>{esc(plan.title)}</b> · {esc(plan.date)}<br>"
            f"Client: {esc(plan.client)} · Engagement: {esc(plan.engagement)}<br>"
            f"Type: {esc(plan.meeting_type)} · {esc(plan.our_role)}<br>"
            f"Language: {esc(plan.language_label)} · Documents in: {esc(plan.document_language)}<br>"
            f"Participants: {len(plan.participants)} · Key terms: {len(plan.vocabulary())}<br>"
            f"Documents to prepare: {esc(docs)}"
            f"<ul>{parts}</ul>"
            f"<b>Total audio:</b> {est['audio']}<br>"
            f"<b>Transcription:</b> {'Groq cloud' if backend == 'groq' else 'local Whisper'} — "
            f"{esc(est['time'])}<br>"
            f"<b>Limits:</b> {esc(est['quota'])}<br>"
            f"<b>Disk:</b> {esc(est['disk'])}{resume}"
        )


# ─── The wizard ─────────────────────────────────────────────────────────

class ImportWizard(QWizard):
    """Collects an ImportPlan. Use `ImportWizard.run(parent, files)`."""

    def __init__(self, parent=None, initial_files: Optional[List[str]] = None):
        super().__init__(parent)
        self.settings = Settings.instance()
        self.setWindowTitle("Import audio / video")
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Start")
        self.setMinimumSize(760, 600)

        self.files_page = FilesPage(initial_files)
        self.details_page = DetailsPage(self.settings)
        self.language_page = LanguagePage(self.settings, self.files_page)
        self.context_page = ContextPage()
        self.documents_page = DocumentsPage(self.details_page)
        self.review_page = ReviewPage(self)
        for p in (self.files_page, self.details_page, self.language_page,
                  self.context_page, self.documents_page, self.review_page):
            self.addPage(p)
        self.currentIdChanged.connect(self._on_page_changed)

    def _on_page_changed(self, _id: int):
        if self.currentPage() is self.details_page:
            self.details_page.prefill(self.files_page.items)

    def build_plan(self) -> ImportPlan:
        items = self.files_page.items
        gaps = estimate_gaps(items) if len(items) > 1 else [None] * len(items)
        d, lp, cp, dp = self.details_page, self.language_page, self.context_page, \
            self.documents_page
        participants = [ln.strip() for ln in d.participants_edit.toPlainText().splitlines()
                        if ln.strip()]
        return ImportPlan(
            parts=[ImportPart(path=i.path, name=i.name, size=i.size, duration=i.duration,
                              recorded_at=i.recorded_at.isoformat() if i.recorded_at else "",
                              gap_before=g)
                   for i, g in zip(items, gaps)],
            order_note=self.files_page.order_reason,
            title=d.title_edit.text().strip(),
            date=d.date_edit.date().toString("yyyy-MM-dd"),
            client=d.client_combo.currentText().strip(),
            engagement=d.engagement_combo.currentText().strip(),
            meeting_type=d.type_combo.currentText(),
            our_role=d.role_combo.currentText(),
            topic=d.topic_edit.toPlainText().strip(),
            participants=participants,
            language_label=lp.lang_combo.currentText(),
            language=lp.lang_combo.currentData(),
            key_terms=cp.terms(),
            use_names_as_terms=cp.names_check.isChecked(),
            requested_documents=dp.selected(),
            custom_document=dp.custom_edit.text().strip(),
            document_language=lp.doclang_combo.currentText(),
            document_instructions=cp.instr_edit.toPlainText().strip(),
            auto_save=self.review_page.autosave_check.isChecked(),
        )

    def accept(self):
        plan = self.build_plan()
        s = self.settings
        s.remember_recent("recent_clients", plan.client)
        s.remember_recent("recent_engagements", plan.engagement)
        s.set("auto_save_after_import", plan.auto_save)
        try:
            s.save()
        except Exception:
            pass
        self._plan = plan
        super().accept()

    @classmethod
    def run(cls, parent=None, initial_files: Optional[List[str]] = None) -> Optional[ImportPlan]:
        wiz = cls(parent, initial_files)
        if wiz.exec() == QDialog.DialogCode.Accepted:
            return getattr(wiz, "_plan", None)
        return None
