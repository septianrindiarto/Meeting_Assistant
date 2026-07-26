"""
Meeting Scribe — Main Window
Application shell with sidebar navigation and stacked views.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QStackedWidget, QStatusBar,
    QFrame, QSizePolicy, QSpacerItem, QMessageBox
)
from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QFont, QAction

from src.core.pipeline import MeetingPipeline
from src.core.settings import Settings

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """
    Main application window with sidebar navigation.
    Hosts all views: Home, Meeting Workspace, Templates, Settings.
    """

    def __init__(self):
        super().__init__()

        self.settings = Settings.instance()
        self.pipeline = MeetingPipeline()

        self.setWindowTitle("Meeting Scribe")
        self.setMinimumSize(1100, 700)
        self.resize(1400, 850)

        # Load app icon
        icon_path = os.path.join(
            os.path.dirname(__file__), "resources", "icons", "app_icon.png"
        )
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # Restore window geometry
        geometry = self.settings.get("window_geometry")
        if geometry:
            try:
                self.restoreGeometry(bytes.fromhex(geometry))
            except Exception:
                pass

        self._setup_ui()
        self._setup_menu_bar()
        self._setup_status_bar()
        self._connect_signals()

    def _setup_ui(self):
        """Build the main layout: sidebar + content area."""
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Sidebar ──
        self.sidebar = self._create_sidebar()
        main_layout.addWidget(self.sidebar)

        # ── Content Stack ──
        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("content_stack")
        main_layout.addWidget(self.content_stack)

        # Import and create views (lazy to avoid circular imports)
        from src.ui.home_view import HomeView
        from src.ui.meeting_workspace import MeetingWorkspace
        from src.ui.templates_view import TemplatesView
        from src.ui.settings_view import SettingsView

        self.home_view = HomeView(self.pipeline)
        self.meeting_workspace = MeetingWorkspace(self.pipeline)
        self.templates_view = TemplatesView()
        self.settings_view = SettingsView()

        self.content_stack.addWidget(self.home_view)           # index 0
        self.content_stack.addWidget(self.meeting_workspace)   # index 1
        self.content_stack.addWidget(self.templates_view)      # index 2
        self.content_stack.addWidget(self.settings_view)       # index 3

        # Start on Home
        self._switch_view(0)

    def _create_sidebar(self) -> QFrame:
        """Create the navigation sidebar."""
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)

        # Logo / Brand
        brand_layout = QHBoxLayout()
        brand_label = QLabel("Meeting Scribe")
        brand_label.setObjectName("heading")
        brand_label.setFont(QFont("Inter", 16, QFont.Weight.Bold))
        brand_label.setStyleSheet("color: #6366f1; font-size: 16px;")
        brand_layout.addWidget(brand_label)
        layout.addLayout(brand_layout)

        # Version subtitle
        version_label = QLabel("v1.0.0 - Local Assistant")
        version_label.setObjectName("caption")
        layout.addWidget(version_label)

        # Divider
        divider = QFrame()
        divider.setObjectName("sidebar_divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)
        layout.addSpacing(8)

        # Navigation buttons
        self.nav_buttons = []

        nav_items = [
            ("  Home", 0),
            ("  Meeting", 1),
            ("  Templates", 2),
            ("  Settings", 3),
        ]

        for label, index in nav_items:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, i=index: self._switch_view(i))
            layout.addWidget(btn)
            self.nav_buttons.append(btn)

        # Spacer
        layout.addSpacerItem(
            QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        )

        # Stats
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("caption")
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)

        self._update_stats()

        return sidebar

    def _switch_view(self, index: int):
        """Switch to a content view by index."""
        self.content_stack.setCurrentIndex(index)

        # Update sidebar button states
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)

        # Refresh data when switching to certain views
        if index == 0:
            self.home_view.refresh()
            self._update_stats()

    def _setup_menu_bar(self):
        """Create the menu bar."""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")

        new_action = QAction("&New Meeting", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._on_new_meeting)
        file_menu.addAction(new_action)

        open_action = QAction("&Open Bundle...", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_bundle)
        file_menu.addAction(open_action)

        import_action = QAction("&Import Audio/Video...", self)
        import_action.setShortcut("Ctrl+I")
        import_action.triggered.connect(self._on_import_media)
        file_menu.addAction(import_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Help menu
        help_menu = menubar.addMenu("&Help")

        about_action = QAction("&About", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _setup_status_bar(self):
        """Create the status bar."""
        status = QStatusBar()
        self.setStatusBar(status)

        project_folder = self.settings.get_project_folder()
        status.showMessage(f"Project folder: {project_folder}")

    def _connect_signals(self):
        """Connect pipeline callbacks to UI updates."""
        self.pipeline.on_state_change = self._on_pipeline_state_change
        self.pipeline.on_progress = self._on_pipeline_progress

        # Connect home view signals
        self.home_view.meeting_selected.connect(self._on_meeting_selected)
        self.home_view.new_meeting_requested.connect(self._on_new_meeting)

        # Connect meeting workspace signals
        self.meeting_workspace.meeting_saved.connect(self._on_meeting_saved)

    def _on_pipeline_state_change(self, state: str):
        """Handle pipeline state changes."""
        self.statusBar().showMessage(f"Status: {state}")

    def _on_pipeline_progress(self, message: str):
        """Handle progress updates from the pipeline."""
        self.statusBar().showMessage(message)

    def _on_new_meeting(self):
        """Start a new meeting recording."""
        self._switch_view(1)  # Switch to meeting workspace
        self.meeting_workspace.start_new_meeting()

    def _on_import_media(self):
        """Import an audio/video file for transcription."""
        self._switch_view(1)
        self.meeting_workspace._on_import_media()

    def _on_open_bundle(self):
        """Open an existing .mscribe bundle."""
        from PyQt6.QtWidgets import QFileDialog

        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Open Meeting Bundle",
            self.settings.get_project_folder(),
            "Meeting Bundles (*.mscribe);;All Files (*)"
        )
        if filepath:
            try:
                self.pipeline.open_bundle(filepath)
                self._switch_view(1)
                self.meeting_workspace.load_meeting(self.pipeline.meeting)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open bundle:\n{e}")

    def _on_meeting_selected(self, bundle_path: str):
        """Handle selecting a past meeting from the home view."""
        try:
            self.pipeline.open_bundle(bundle_path)
            self._switch_view(1)
            self.meeting_workspace.load_meeting(self.pipeline.meeting)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open meeting:\n{e}")

    def _on_meeting_saved(self):
        """Handle meeting saved — refresh home view and stats."""
        self.home_view.refresh()
        self._update_stats()

    def _on_about(self):
        """Show about dialog."""
        QMessageBox.about(
            self,
            "About Meeting Scribe",
            "<h2>Meeting Scribe</h2>"
            "<p>v1.0.0 - Local Meeting Assistant</p>"
            "<p>Bot-free meeting transcription & document generation.</p>"
            "<p>All processing happens on your device.<br>"
            "Your meetings never leave your computer.</p>"
            "<p><small>2026 Areloa</small></p>"
        )

    def _update_stats(self):
        """Update the sidebar stats display."""
        try:
            stats = self.pipeline.get_database_stats()
            total = stats.get("total_meetings", 0)
            hours = stats.get("total_duration_hours", 0)
            self.stats_label.setText(
                f"{total} meetings\n"
                f"{hours:.1f} hours recorded"
            )
        except Exception:
            self.stats_label.setText("No meetings yet")

    def closeEvent(self, event):
        """Save window geometry on close."""
        self.settings.set("window_geometry", self.saveGeometry().toHex().data().decode())
        self.settings.save()
        super().closeEvent(event)
