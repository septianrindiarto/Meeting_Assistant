"""
Meeting Scribe — Application Entry
Sets up logging, loads theme, and launches the main window.
"""
import sys
import os
import logging
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QFontDatabase, QIcon
from PyQt6.QtCore import Qt


def setup_logging():
    """Configure application-wide logging."""
    from src.utils.file_utils import get_app_data_dir

    log_dir = get_app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "meeting_scribe.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_file), encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ]
    )


def load_stylesheet(app: QApplication):
    """Load the QSS stylesheet."""
    qss_path = os.path.join(
        os.path.dirname(__file__), "ui", "resources", "style.qss"
    )
    if os.path.exists(qss_path):
        with open(qss_path, 'r', encoding='utf-8') as f:
            app.setStyleSheet(f.read())
        logging.info(f"Stylesheet loaded: {qss_path}")
    else:
        logging.warning(f"Stylesheet not found: {qss_path}")


def load_fonts():
    """Load bundled fonts (Inter)."""
    fonts_dir = os.path.join(
        os.path.dirname(__file__), "ui", "resources", "fonts"
    )
    if os.path.isdir(fonts_dir):
        for filename in os.listdir(fonts_dir):
            if filename.endswith(('.ttf', '.otf')):
                font_path = os.path.join(fonts_dir, filename)
                font_id = QFontDatabase.addApplicationFont(font_path)
                if font_id >= 0:
                    families = QFontDatabase.applicationFontFamilies(font_id)
                    logging.debug(f"Loaded font: {families}")


def run():
    """Main application entry point."""
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Meeting Scribe starting...")

    # High DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Meeting Scribe")
    app.setOrganizationName("Areloa")
    app.setApplicationVersion("1.0.0")

    # Set default font
    font = QFont("Inter", 13)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    # Load app icon
    icon_path = os.path.join(
        os.path.dirname(__file__), "ui", "resources", "icons", "app_icon.png"
    )
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Load theme
    load_fonts()
    load_stylesheet(app)

    # Housekeeping: clear temp files orphaned by crashes or cancelled jobs.
    # Imported/decoded audio is ~115 MB per hour, so this matters.
    try:
        from src.utils.housekeeping import startup_cleanup
        from src.core.settings import Settings
        _settings = Settings.instance()
        if _settings.get("auto_cleanup_temp", True):
            result = startup_cleanup(
                retention_hours=_settings.get("temp_retention_hours", 24)
            )
            if result["freed_mb"] > 1:
                logger.info(
                    f"Startup cleanup freed {result['freed_mb']:.0f} MB"
                )
    except Exception as e:
        logger.warning(f"Startup housekeeping skipped: {e}")

    # Create and show main window
    from src.ui.main_window import MainWindow
    window = MainWindow()
    window.show()

    logger.info("Application ready")
    sys.exit(app.exec())
