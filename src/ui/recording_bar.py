"""
Meeting Scribe — Recording Bar
Compact, always-on-top floating window shown during recording.
Displays: REC indicator, waveform, elapsed time, pause/stop controls.
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QSizePolicy, QComboBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint
from PyQt6.QtGui import QFont, QPainter, QColor, QPen

from src.core.pipeline import MeetingPipeline, PipelineState

logger = logging.getLogger(__name__)


class WaveformMiniWidget(QWidget):
    """Tiny real-time waveform display for the recording bar.

    Also tracks rolling peak level so the parent RecordingBar can show
    an audio-quality verdict (silent / quiet / good / loud).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(120, 36)
        self._levels = [0.0] * 30  # rolling history
        self._recent_peak = 0.0    # rolling-window peak for quality verdict

    def update_level(self, level: float):
        """Push a new audio level (0.0 to 1.0)."""
        self._levels.append(min(level * 3.0, 1.0))  # amplify for visibility
        if len(self._levels) > 30:
            self._levels.pop(0)
        # Track raw (not amplified) peak over the rolling window
        self._recent_peak = max(self._levels)
        self.update()

    def quality_verdict(self) -> tuple:
        """Return (label, color_hex) describing the audio quality."""
        peak = self._recent_peak
        if peak < 0.02:
            return ("Silent — mic not picking up", "#ef4444")
        if peak < 0.10:
            return ("Quiet — move closer / speak up", "#f59e0b")
        if peak < 0.85:
            return ("Good level", "#22c55e")
        return ("Too loud — clipping risk", "#f59e0b")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        mid_y = h / 2
        bar_width = w / len(self._levels)

        for i, level in enumerate(self._levels):
            bar_height = max(2, level * (h - 4))
            x = i * bar_width

            # Gradient from indigo to purple based on level
            r = int(99 + level * 50)
            g = int(102 - level * 30)
            b = int(241)
            color = QColor(r, g, b, int(180 + level * 75))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(
                int(x + 1), int(mid_y - bar_height / 2),
                int(bar_width - 2), int(bar_height),
                2, 2
            )

        painter.end()


class RecordingBar(QWidget):
    """
    Floating recording bar — always-on-top, compact, draggable.

    Signals:
        pause_clicked: Emitted when pause/resume is pressed.
        stop_clicked: Emitted when stop is pressed.
    """

    pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()

    def __init__(self, pipeline: MeetingPipeline, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline

        # Window flags: frameless, always-on-top, tool window
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("recording_bar")
        self.setFixedHeight(72)
        self.setMinimumWidth(520)

        # For dragging
        self._drag_pos: Optional[QPoint] = None

        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self):
        # Outer vertical layout: top row is controls, bottom row is the active
        # device label so the user can see at a glance which mic is in use.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 6, 16, 6)
        outer.setSpacing(2)

        top_widget = QWidget()
        layout = QHBoxLayout(top_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        outer.addWidget(top_widget)

        # Active-device line — updated by _update_display once capture is live.
        self.device_label = QLabel("🎙 Mic: (waiting)")
        self.device_label.setStyleSheet("color: #8b8ba0; font-size: 10px;")
        outer.addWidget(self.device_label)

        # Background styling
        self.setStyleSheet("""
            RecordingBar {
                background-color: rgba(15, 15, 26, 0.95);
                border: 1px solid rgba(99, 102, 241, 0.3);
                border-radius: 14px;
            }
        """)

        # REC indicator (pulsing dot)
        self.rec_label = QLabel("🔴 REC")
        self.rec_label.setObjectName("rec_indicator")
        self.rec_label.setFont(QFont("Inter", 12, QFont.Weight.Bold))
        layout.addWidget(self.rec_label)

        # Waveform
        self.waveform = WaveformMiniWidget()
        layout.addWidget(self.waveform)

        # Elapsed time
        self.elapsed_label = QLabel("00:00:00")
        self.elapsed_label.setObjectName("elapsed_timer")
        self.elapsed_label.setFont(QFont("Consolas", 16, QFont.Weight.DemiBold))
        layout.addWidget(self.elapsed_label)

        layout.addSpacing(8)

        # Pause / Resume button
        self.pause_btn = QPushButton("⏸️")
        self.pause_btn.setToolTip("Pause / Resume")
        self.pause_btn.setFixedSize(36, 36)
        self.pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_btn.clicked.connect(self._on_pause)
        layout.addWidget(self.pause_btn)

        # Stop button
        self.stop_btn = QPushButton("⏹️")
        self.stop_btn.setObjectName("stop_button")
        self.stop_btn.setToolTip("Stop Recording")
        self.stop_btn.setFixedSize(36, 36)
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.clicked.connect(self._on_stop)
        layout.addWidget(self.stop_btn)

    def _setup_timer(self):
        """Timer to update elapsed time display."""
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(100)  # 10fps
        self.update_timer.timeout.connect(self._update_display)

    def start(self):
        """Show the bar and start updating."""
        self.show()
        self.update_timer.start()

        # Wire audio level callback
        self.pipeline.on_level_change = self.waveform.update_level

        # Position at top-center of screen
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            x = (geo.width() - self.width()) // 2
            self.move(x, 20)

    def stop(self):
        """Hide the bar and stop updating."""
        self.update_timer.stop()
        self.hide()

    def _update_display(self):
        """Update the elapsed time label."""
        elapsed = self.pipeline.elapsed_seconds
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        seconds = int(elapsed % 60)
        self.elapsed_label.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")

        # Show which mic is actually being used, or surface a failure reason.
        engine = getattr(self.pipeline, "_capture_engine", None)
        if engine is not None:
            err = getattr(engine, "mic_start_error", None)
            if err:
                self.device_label.setText(f"⚠ Mic FAILED — {err[:80]}")
                self.device_label.setStyleSheet("color: #ef4444; font-size: 10px;")
            else:
                name = getattr(engine, "active_mic_device", None)
                verdict, color = self.waveform.quality_verdict()
                if name:
                    self.device_label.setText(
                        f"🎙 {name}  ·  {verdict}"
                    )
                    self.device_label.setStyleSheet(
                        f"color: {color}; font-size: 10px;"
                    )

        # Pulse the REC indicator
        if self.pipeline.state == PipelineState.PAUSED:
            self.rec_label.setText("⏸️ PAUSED")
            self.rec_label.setStyleSheet("color: #f59e0b;")
            self.pause_btn.setText("▶️")
        else:
            self.rec_label.setText("🔴 REC")
            self.rec_label.setStyleSheet("color: #ef4444;")
            self.pause_btn.setText("⏸️")

    def _on_pause(self):
        """Toggle pause/resume."""
        self.pause_clicked.emit()

    def _on_stop(self):
        """Stop recording."""
        self.stop_clicked.emit()

    # ── Dragging ──

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
