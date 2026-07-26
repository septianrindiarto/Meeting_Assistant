"""
Meeting Scribe — Audio Player Widget
Embedded audio player for meeting playback with seek, waveform display,
and transcript synchronization.
"""
from __future__ import annotations

import os
import logging
from typing import Optional, Callable

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QSlider, QSizePolicy, QFrame, QStyle
)
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QColor, QPen, QLinearGradient
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

import numpy as np

logger = logging.getLogger(__name__)


class WaveformWidget(QWidget):
    """
    Displays the full waveform of the recording and highlights
    the current playback position.
    """

    seek_requested = pyqtSignal(float)  # emits position in seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(48)
        self.setMaximumHeight(60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._waveform_data: list[float] = []  # normalized peaks (0-1)
        self._duration: float = 0.0
        self._position: float = 0.0  # current playback position in seconds
        self._num_bars: int = 200

    def set_audio_data(self, audio: np.ndarray, sample_rate: int):
        """Compute waveform peaks from raw audio."""
        if len(audio) == 0:
            self._waveform_data = []
            return

        self._duration = len(audio) / sample_rate

        # Downsample into N bars
        n = self._num_bars
        chunk_size = max(1, len(audio) // n)
        peaks = []
        for i in range(n):
            start = i * chunk_size
            end = min(start + chunk_size, len(audio))
            if start >= len(audio):
                break
            chunk = audio[start:end]
            peak = float(np.max(np.abs(chunk)))
            peaks.append(peak)

        # Normalize to 0-1
        max_peak = max(peaks) if peaks else 1.0
        if max_peak > 0:
            self._waveform_data = [p / max_peak for p in peaks]
        else:
            self._waveform_data = [0.0] * len(peaks)

        self.update()

    def set_position(self, seconds: float):
        """Update current playback position."""
        self._position = seconds
        self.update()

    def set_duration(self, seconds: float):
        """Set total duration."""
        self._duration = seconds

    def paintEvent(self, event):
        if not self._waveform_data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        mid_y = h / 2
        n = len(self._waveform_data)
        bar_width = w / n
        progress_ratio = (self._position / self._duration) if self._duration > 0 else 0

        for i, peak in enumerate(self._waveform_data):
            bar_height = max(2, peak * (h - 8))
            x = i * bar_width
            bar_ratio = i / n

            # Color: played = indigo, unplayed = dim gray
            if bar_ratio <= progress_ratio:
                color = QColor(99, 102, 241, 220)  # indigo
            else:
                color = QColor(80, 80, 110, 120)  # dim

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(
                int(x + 1), int(mid_y - bar_height / 2),
                max(1, int(bar_width - 2)), int(bar_height),
                1, 1
            )

        # Draw position indicator line
        if self._duration > 0:
            px = int(progress_ratio * w)
            pen = QPen(QColor(255, 255, 255, 200))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(px, 2, px, h - 2)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._duration > 0:
            ratio = event.position().x() / self.width()
            ratio = max(0.0, min(1.0, ratio))
            seek_time = ratio * self._duration
            self.seek_requested.emit(seek_time)
        super().mousePressEvent(event)


class AudioPlayer(QFrame):
    """
    Compact audio player with play/pause, waveform seek bar,
    time display, and speed control.

    Signals:
        position_changed(float): Emits current playback position in seconds.
    """

    position_changed = pyqtSignal(float)  # seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("audio_player")
        self.setStyleSheet("""
            AudioPlayer {
                background-color: #0d0d1a;
                border: 1px solid #1e1e3a;
                border-radius: 10px;
                padding: 4px;
            }
        """)

        self._audio_path: str = ""
        self._duration: float = 0.0
        self._is_playing: bool = False

        # Qt Media Player
        self._player = QMediaPlayer()
        self._audio_output = QAudioOutput()
        self._player.setAudioOutput(self._audio_output)
        self._audio_output.setVolume(1.0)

        # Connect signals
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.errorOccurred.connect(self._on_error)

        self._setup_ui()

        # Update timer for smooth waveform updates
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(50)  # 20fps
        self._update_timer.timeout.connect(self._tick)

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        # Play / Pause button
        self.play_btn = QPushButton("  Play  ")
        self.play_btn.setFixedWidth(80)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.clicked.connect(self.toggle_play)
        self.play_btn.setStyleSheet("""
            QPushButton {
                background-color: #6366f1;
                color: white;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #818cf8;
            }
            QPushButton:pressed {
                background-color: #4f46e5;
            }
        """)
        layout.addWidget(self.play_btn)

        # Current time label
        self.time_label = QLabel("0:00")
        self.time_label.setFont(QFont("Consolas", 11))
        self.time_label.setStyleSheet("color: #e0e0e8; min-width: 45px;")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.time_label)

        # Waveform / seek bar
        self.waveform = WaveformWidget()
        self.waveform.seek_requested.connect(self.seek_to)
        layout.addWidget(self.waveform, 1)  # stretch

        # Duration label
        self.duration_label = QLabel("0:00")
        self.duration_label.setFont(QFont("Consolas", 11))
        self.duration_label.setStyleSheet("color: #707088; min-width: 45px;")
        layout.addWidget(self.duration_label)

        # Speed button
        self._speed_index = 2  # default 1.0x
        self._speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        self.speed_btn = QPushButton("1.0x")
        self.speed_btn.setFixedWidth(50)
        self.speed_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed_btn.setToolTip("Playback speed (click to cycle)")
        self.speed_btn.clicked.connect(self._cycle_speed)
        self.speed_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(99, 102, 241, 0.15);
                color: #a0a0b8;
                border: 1px solid #1e1e3a;
                border-radius: 6px;
                padding: 4px;
                font-size: 11px;
            }
            QPushButton:hover { color: #e0e0e8; }
        """)
        layout.addWidget(self.speed_btn)

        # Volume slider
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(70)
        self.volume_slider.setToolTip("Volume")
        self.volume_slider.valueChanged.connect(
            lambda v: self._audio_output.setVolume(v / 100.0)
        )
        layout.addWidget(self.volume_slider)

    def load_file(self, audio_path: str):
        """Load an audio file for playback."""
        if not audio_path or not os.path.exists(audio_path):
            logger.warning(f"Audio file not found: {audio_path}")
            return

        self._audio_path = audio_path
        self._player.setSource(QUrl.fromLocalFile(audio_path))

        # Load waveform data for display
        try:
            from src.utils.audio_utils import load_wav
            audio_data, sr = load_wav(audio_path)
            self.waveform.set_audio_data(audio_data, sr)
            self._duration = len(audio_data) / sr
            self.waveform.set_duration(self._duration)
            self.duration_label.setText(self._format_time(self._duration))
            logger.info(f"Audio loaded: {audio_path} ({self._duration:.1f}s)")
        except Exception as e:
            logger.warning(f"Could not load waveform: {e}")

        self.show()

    def toggle_play(self):
        """Toggle play/pause."""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()
            self._update_timer.start()

    def seek_to(self, seconds: float):
        """Seek to a specific position in seconds."""
        ms = int(seconds * 1000)
        self._player.setPosition(ms)
        self.waveform.set_position(seconds)
        self.time_label.setText(self._format_time(seconds))
        self.position_changed.emit(seconds)

    def stop(self):
        """Stop playback."""
        self._player.stop()
        self._update_timer.stop()

    def _on_position_changed(self, position_ms: int):
        """Handle QMediaPlayer position change."""
        seconds = position_ms / 1000.0
        self.waveform.set_position(seconds)
        self.time_label.setText(self._format_time(seconds))
        self.position_changed.emit(seconds)

    def _on_duration_changed(self, duration_ms: int):
        """Handle duration detection from QMediaPlayer."""
        self._duration = duration_ms / 1000.0
        self.waveform.set_duration(self._duration)
        self.duration_label.setText(self._format_time(self._duration))

    def _on_state_changed(self, state):
        """Update button text based on playback state."""
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_btn.setText("  Pause  ")
            self._is_playing = True
        else:
            self.play_btn.setText("  Play  ")
            self._is_playing = False
            if state == QMediaPlayer.PlaybackState.StoppedState:
                self._update_timer.stop()

    def _on_error(self, error, error_string):
        """Handle playback errors."""
        logger.error(f"Playback error: {error_string}")

    def _tick(self):
        """Timer tick for smooth UI updates."""
        pass  # position_changed signal handles updates

    def _cycle_speed(self):
        """Cycle through playback speeds."""
        self._speed_index = (self._speed_index + 1) % len(self._speeds)
        speed = self._speeds[self._speed_index]
        self._player.setPlaybackRate(speed)
        self.speed_btn.setText(f"{speed}x")

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Format seconds as M:SS or H:MM:SS."""
        if seconds < 0:
            seconds = 0
        total_sec = int(seconds)
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60
        secs = total_sec % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_position(self) -> float:
        """Current playback position in seconds."""
        return self._player.position() / 1000.0
