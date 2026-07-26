"""
Meeting Scribe — Templates View
Browse, import, validate, and preview document templates.
"""
from __future__ import annotations

import os
import shutil
import logging

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QFrame, QMessageBox,
    QFileDialog, QTextEdit
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from src.core.template_engine import TemplateEngine
from src.utils.file_utils import get_templates_dir

logger = logging.getLogger(__name__)


class TemplatesView(QWidget):
    """Template manager: browse, import, and test templates."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = TemplateEngine()
        self.templates_dir = str(get_templates_dir())
        self._setup_ui()
        self._refresh_list()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)

        # ── Header ──
        header_layout = QHBoxLayout()

        title = QLabel("Document Templates")
        title.setObjectName("heading")
        title.setFont(QFont("Inter", 22, QFont.Weight.Bold))
        header_layout.addWidget(title)

        header_layout.addStretch()

        import_btn = QPushButton("📥  Import Template")
        import_btn.setObjectName("primary_button")
        import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        import_btn.clicked.connect(self._on_import)
        header_layout.addWidget(import_btn)

        layout.addLayout(header_layout)

        desc = QLabel(
            "Templates are .docx files with Jinja2 placeholders like "
            "{{ meeting.title }} and {{ action_items }}.\n"
            "Create your own in Word, or use the built-in starter templates."
        )
        desc.setStyleSheet("color: #a0a0b8; font-size: 13px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # ── Content: List + Details ──
        content_layout = QHBoxLayout()
        content_layout.setSpacing(20)

        # Template list
        list_frame = QFrame()
        list_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(22, 22, 42, 0.85);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 12px;
            }
        """)
        list_layout = QVBoxLayout(list_frame)
        list_layout.setContentsMargins(12, 12, 12, 12)

        self.template_list = QListWidget()
        self.template_list.setAlternatingRowColors(True)
        self.template_list.currentItemChanged.connect(self._on_selection_changed)
        list_layout.addWidget(self.template_list)

        # Actions row
        actions_layout = QHBoxLayout()
        test_btn = QPushButton("🧪 Test Render")
        test_btn.clicked.connect(self._on_test_render)
        actions_layout.addWidget(test_btn)

        delete_btn = QPushButton("🗑️ Delete")
        delete_btn.setObjectName("danger_button")
        delete_btn.clicked.connect(self._on_delete)
        actions_layout.addWidget(delete_btn)

        list_layout.addLayout(actions_layout)

        content_layout.addWidget(list_frame, stretch=1)

        # Details panel
        details_frame = QFrame()
        details_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(22, 22, 42, 0.85);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 12px;
            }
        """)
        details_layout = QVBoxLayout(details_frame)
        details_layout.setContentsMargins(16, 16, 16, 16)

        self.details_title = QLabel("Select a template")
        self.details_title.setObjectName("subheading")
        self.details_title.setFont(QFont("Inter", 14, QFont.Weight.DemiBold))
        details_layout.addWidget(self.details_title)

        self.details_info = QTextEdit()
        self.details_info.setReadOnly(True)
        self.details_info.setPlaceholderText("Template details will appear here.")
        details_layout.addWidget(self.details_info)

        content_layout.addWidget(details_frame, stretch=1)

        layout.addLayout(content_layout)

    def _refresh_list(self):
        """Reload the template list."""
        self.template_list.clear()

        templates = self.engine.list_templates(self.templates_dir)
        for tmpl in templates:
            item = QListWidgetItem(f"📄 {tmpl.name}")
            item.setData(Qt.ItemDataRole.UserRole, tmpl.path)
            item.setData(Qt.ItemDataRole.UserRole + 1, tmpl.placeholders)
            self.template_list.addItem(item)

        if not templates:
            item = QListWidgetItem("No templates found — click Import to add one")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.template_list.addItem(item)

    def _on_selection_changed(self, current, previous):
        """Show details for the selected template."""
        if not current:
            return

        path = current.data(Qt.ItemDataRole.UserRole)
        placeholders = current.data(Qt.ItemDataRole.UserRole + 1)

        if not path:
            return

        self.details_title.setText(current.text().replace("📄 ", ""))

        # Validate
        result = self.engine.validate_template(path)

        info_lines = [
            f"<b>File:</b> {os.path.basename(path)}",
            f"<b>Path:</b> {path}",
            "",
            f"<b>Placeholders ({len(result.placeholders)}):</b>",
        ]

        for ph in result.placeholders:
            info_lines.append(f"  • <code>{{{{{ph}}}}}</code>")

        if result.unknown:
            info_lines.append("")
            info_lines.append(f"<b style='color: #f59e0b;'>⚠️ Unknown placeholders:</b>")
            for ph in result.unknown:
                info_lines.append(f"  • <code>{{{{{ph}}}}}</code>")

        if result.valid:
            info_lines.append("")
            info_lines.append("<span style='color: #22c55e;'>✅ Template is valid</span>")
        else:
            for err in result.errors:
                info_lines.append(f"<span style='color: #ef4444;'>❌ {err}</span>")

        self.details_info.setHtml("<br>".join(info_lines))

    def _on_import(self):
        """Import a .docx template file."""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Import Template",
            "", "Word Documents (*.docx)"
        )
        if filepath:
            dest = os.path.join(self.templates_dir, os.path.basename(filepath))
            os.makedirs(self.templates_dir, exist_ok=True)
            shutil.copy2(filepath, dest)
            self._refresh_list()
            QMessageBox.information(self, "Imported", f"Template imported:\n{os.path.basename(filepath)}")

    def _on_delete(self):
        """Delete the selected template."""
        current = self.template_list.currentItem()
        if not current:
            return

        path = current.data(Qt.ItemDataRole.UserRole)
        if not path:
            return

        reply = QMessageBox.question(
            self, "Delete Template",
            f"Are you sure you want to delete:\n{os.path.basename(path)}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            os.remove(path)
            self._refresh_list()

    def _on_test_render(self):
        """Render the selected template with sample data."""
        current = self.template_list.currentItem()
        if not current:
            return

        path = current.data(Qt.ItemDataRole.UserRole)
        if not path:
            return

        try:
            preview_path = self.engine.render_preview(path)
            os.startfile(preview_path)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Test render failed:\n{e}")
