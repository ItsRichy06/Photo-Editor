"""Small persistent-settings helper (last-used folders, etc.), backed by QSettings."""

from PySide6.QtCore import QSettings

LAST_LIBRARY_FOLDER = "library/last_folder"
LAST_EXPORT_FOLDER = "export/last_folder"
AUTO_ENHANCE_CONSENT = "ai/auto_enhance_consent"  # user has agreed to the one-time ~8GB model download


def get_settings() -> QSettings:
    return QSettings("PhotoEditor", "PhotoEditor")
