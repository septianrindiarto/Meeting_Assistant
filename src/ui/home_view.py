"""
Meeting Scribe — Home View
Displays past meetings, search bar, and the "New Meeting" button.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QSizePolicy, QSpacerItem,
    QListWidget, QListWidgetItem, QCheckBox, QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QFont

from src.core.pipeline import MeetingPipeline

logger = logging.getLogger(__name__)


class MeetingCard(QFrame):
    """A styled card displaying meeting summary info."""

    clicked = pyqtSignal(str)  # emits bundle_path

    def __init__(self, meeting_data: dict, parent=None):
        super().__init__(parent)
        self.bundle_path = meeting_data.get("bundle_path", "")
        self.setProperty("class", "card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("""
            MeetingCard {
                background-color: rgba(22, 22, 42, 0.85);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 12px;
                padding: 16px;
            }
            MeetingCard:hover {
                border-color: rgba(99, 102, 241, 0.3);
                background-color: rgba(26, 26, 50, 0.95);
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        # Title
        title = QLabel(meeting_data.get("title", "Untitled Meeting"))
        title.setFont(QFont("Inter", 14, QFont.Weight.DemiBold))
        title.setStyleSheet("color: #f0f0f8;")
        layout.addWidget(title)

        # Meta row: date, duration, speakers
        meta_layout = QHBoxLayout()
        meta_layout.setSpacing(16)

        date_str = meeting_data.get("date", "Unknown date")
        date_label = QLabel(f"📅 {date_str}")
        date_label.setStyleSheet("color: #a0a0b8; font-size: 12px;")
        meta_layout.addWidget(date_label)

        duration = meeting_data.get("duration", "")
        if duration:
            dur_label = QLabel(f"⏱️ {duration}")
            dur_label.setStyleSheet("color: #a0a0b8; font-size: 12px;")
            meta_layout.addWidget(dur_label)

        speakers = meeting_data.get("speakers", "")
        if speakers:
            spk_label = QLabel(f"👥 {speakers}")
            spk_label.setStyleSheet("color: #a0a0b8; font-size: 12px;")
            spk_label.setMaximumWidth(300)
            spk_label.setWordWrap(True)
            meta_layout.addWidget(spk_label)

        meta_layout.addStretch()

        size_mb = meeting_data.get("file_size_mb", 0)
        if size_mb:
            size_label = QLabel(f"📦 {size_mb:.1f} MB")
            size_label.setStyleSheet("color: #707088; font-size: 11px;")
            meta_layout.addWidget(size_label)

        layout.addLayout(meta_layout)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.bundle_path)
        super().mousePressEvent(event)


class QueuePanel(QFrame):
    """Document request queue: what still needs documents written."""

    ICONS = {"PENDING": "⏳", "DRAFTED": "📝", "DONE": "✅", "SKIPPED": "⏭"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QueuePanel { background-color: rgba(22, 22, 42, 0.85);
                         border: 1px solid rgba(255, 255, 255, 0.06);
                         border-radius: 12px; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("Document queue")
        title.setFont(QFont("Inter", 13, QFont.Weight.DemiBold))
        head.addWidget(title)
        self.counts_label = QLabel("")
        self.counts_label.setStyleSheet("color: #a0a0b8; font-size: 12px;")
        head.addWidget(self.counts_label)
        head.addStretch()
        self.show_all = QCheckBox("Show done / skipped")
        self.show_all.toggled.connect(lambda _: self.refresh())
        head.addWidget(self.show_all)
        self.convert_btn = QPushButton("Convert .md → .docx")
        self.convert_btn.setToolTip("Turn document .md files (written by Claude) into "
                                    "Word files — locally, no AI, no internet")
        self.convert_btn.clicked.connect(self._on_convert)
        head.addWidget(self.convert_btn)
        lay.addLayout(head)

        self.list = QListWidget()
        self.list.setMaximumHeight(150)
        self.list.itemDoubleClicked.connect(lambda _: self._open("request"))
        lay.addWidget(self.list)

        row = QHBoxLayout()
        for text, fn, tip in (
                ("📋 Copy prompt for Claude", self._copy_prompt,
                 "Copy a ready-made request to paste into Claude (Cowork)"),
                ("Open transcript", lambda: self._open("transcript"), ""),
                ("Open request", lambda: self._open("request"), ""),
                ("Mark done", lambda: self._set("DONE"), ""),
                ("Skip", lambda: self._set("SKIPPED"), "Take this meeting out of the queue"),
                ("Back to pending", lambda: self._set("PENDING"), "")):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self._items = []

    def folder(self) -> str:
        from src.core.settings import Settings
        return Settings.instance().get_project_folder()

    def refresh(self):
        from src.core import request_queue, doc_convert
        items = request_queue.list_requests(self.folder())
        c = request_queue.counts(items)
        self.counts_label.setText(
            f"  {c['PENDING']} pending · {c['DRAFTED']} drafted · "
            f"{c['DONE']} done · {c['SKIPPED']} skipped")
        shown = items if self.show_all.isChecked() else \
            [i for i in items if i.status in ("PENDING", "DRAFTED")]
        self._items = shown
        self.list.clear()
        for it in shown:
            docs = ", ".join(it.documents) if it.documents else "—"
            li = QListWidgetItem(f"{self.ICONS.get(it.status, '•')} {it.status:<8} "
                                 f"{it.title}  ·  {it.date}  ·  {docs}")
            li.setToolTip(it.status_note or it.path)
            self.list.addItem(li)
        if not shown:
            li = QListWidgetItem("Nothing waiting — every requested document is done "
                                 "or skipped.")
            li.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(li)
        n = len(doc_convert.find_unconverted(self.folder()))
        self.convert_btn.setText(f"Convert .md → .docx ({n})")
        self.convert_btn.setEnabled(n > 0)

    def _selected(self):
        r = self.list.currentRow()
        if 0 <= r < len(self._items):
            return self._items[r]
        QMessageBox.information(self, "Document queue", "Select a meeting in the list first.")
        return None

    def _copy_prompt(self):
        from src.core import request_queue
        it = self._selected()
        if it:
            QApplication.clipboard().setText(request_queue.claude_prompt(it))
            QMessageBox.information(self, "Copied",
                                    "Prompt copied — paste it into Claude (Cowork).")

    def _open(self, which: str):
        it = self._selected()
        if not it:
            return
        path = it.transcript_path if which == "transcript" else it.path
        if os.path.exists(path):
            os.startfile(path)
        else:
            QMessageBox.warning(self, "Not found", f"File not found:\n{path}")

    def _set(self, status: str):
        from src.core import request_queue
        it = self._selected()
        if it:
            request_queue.set_status(it.path, status)
            self.refresh()

    def _on_convert(self):
        from src.core import doc_convert
        paths = doc_convert.find_unconverted(self.folder())
        if not paths:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            done, failed = doc_convert.convert_many(paths)
        finally:
            QApplication.restoreOverrideCursor()
        msg = f"Converted {len(done)} document(s) to .docx."
        if failed:
            msg += "\n\nFailed:\n" + "\n".join(f"• {os.path.basename(p)}: {e}"
                                                for p, e in failed)
        QMessageBox.information(self, "Convert to .docx", msg)
        self.refresh()


class HomeView(QWidget):
    """
    Home screen showing past meetings and the new meeting button.
    """

    meeting_selected = pyqtSignal(str)  # bundle_path
    new_meeting_requested = pyqtSignal()
    import_requested = pyqtSignal()

    def __init__(self, pipeline: MeetingPipeline, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self._setup_ui()
        self._load_meetings()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)

        # ── Header Row ──
        header_layout = QHBoxLayout()

        title = QLabel("Your Meetings")
        title.setObjectName("heading")
        title.setFont(QFont("Inter", 22, QFont.Weight.Bold))
        header_layout.addWidget(title)

        header_layout.addStretch()

        # New Meeting Button
        new_btn = QPushButton("  🎙️  New Meeting  ")
        new_btn.setObjectName("primary_button")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setMinimumHeight(44)
        new_btn.clicked.connect(self.new_meeting_requested.emit)

        import_btn = QPushButton("  📂  Import audio / video  ")
        import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        import_btn.setMinimumHeight(44)
        import_btn.setToolTip("Transcribe recordings (mp4, mp3, ...) — one file or "
                              "several parts of one meeting. You can also drag "
                              "files onto the window.")
        import_btn.clicked.connect(self.import_requested.emit)
        header_layout.addWidget(import_btn)
        header_layout.addWidget(new_btn)

        layout.addLayout(header_layout)

        # ── Search Bar ──
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍  Search meetings by title, speaker, or content...")
        self.search_input.setMinimumHeight(40)
        self.search_input.textChanged.connect(self._on_search)
        layout.addWidget(self.search_input)

        # ── Document queue ──
        self.queue_panel = QueuePanel()
        layout.addWidget(self.queue_panel)

        # ── Meeting List (scrollable) ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_container)
        layout.addWidget(scroll)

    def _load_meetings(self):
        """Load meetings from the database and populate the list."""
        # Clear existing cards
        while self.list_layout.count() > 1:  # keep the stretch
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        try:
            meetings = self.pipeline.list_meetings()
        except Exception as e:
            logger.warning(f"Could not load meetings: {e}")
            meetings = []

        if not meetings:
            empty_label = QLabel(
                "No meetings yet.\n\n"
                "Click \"New Meeting\" to record your first meeting,\n"
                "or open an existing .mscribe bundle from File → Open."
            )
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_label.setStyleSheet("color: #707088; font-size: 14px; padding: 60px;")
            self.list_layout.insertWidget(0, empty_label)
            return

        for meeting_data in meetings:
            card = MeetingCard(meeting_data)
            card.clicked.connect(self.meeting_selected.emit)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    def _on_search(self, query: str):
        """Handle search input changes."""
        if not query.strip():
            self._load_meetings()
            return

        # Clear existing
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        try:
            results = self.pipeline.search_meetings(query)
            for result in results:
                data = {
                    "title": result.title,
                    "date": result.date,
                    "duration": result.duration,
                    "speakers": result.speakers,
                    "bundle_path": result.bundle_path,
                }
                card = MeetingCard(data)
                card.clicked.connect(self.meeting_selected.emit)
                self.list_layout.insertWidget(self.list_layout.count() - 1, card)

            if not results:
                no_results = QLabel(f"No meetings found for \"{query}\"")
                no_results.setAlignment(Qt.AlignmentFlag.AlignCenter)
                no_results.setStyleSheet("color: #707088; font-size: 14px; padding: 40px;")
                self.list_layout.insertWidget(0, no_results)

        except Exception as e:
            logger.warning(f"Search error: {e}")

    def refresh(self):
        """Reload the meeting list and the document queue."""
        self._load_meetings()
        try:
            self.queue_panel.refresh()
        except Exception as e:
            logger.warning(f"Could not load document queue: {e}")
