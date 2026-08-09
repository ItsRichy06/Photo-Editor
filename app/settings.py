"""Small persistent-settings helper (last-used folders, etc.), backed by QSettings."""

from PySide6.QtCore import QSettings

LAST_LIBRARY_FOLDER = "library/last_folder"
LAST_EXPORT_FOLDER = "export/last_folder"


def get_settings() -> QSettings:
    return QSettings("FotoEditor", "FotoEditor")
