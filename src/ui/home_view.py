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
    QLineEdit, QScrollArea, QFrame, QSizePolicy, QSpacerItem
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


class HomeView(QWidget):
    """
    Home screen showing past meetings and the new meeting button.
    """

    meeting_selected = pyqtSignal(str)  # bundle_path
    new_meeting_requested = pyqtSignal()

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
        header_layout.addWidget(new_btn)

        layout.addLayout(header_layout)

        # ── Search Bar ──
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍  Search meetings by title, speaker, or content...")
        self.search_input.setMinimumHeight(40)
        self.search_input.textChanged.connect(self._on_search)
        layout.addWidget(self.search_input)

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
        """Reload the meeting list."""
        self._load_meetings()
