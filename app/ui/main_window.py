"""App shell: switches between the Library browser and the Editor."""

from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from ..catalog import Catalog
from .editor_view import EditorView
from .library_view import LibraryView
from .viewer_view import PhotoViewerView

ICON_PATH = Path(__file__).parent.parent.parent / "assets" / "app_icon.ico"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Photo Editor")
        self.resize(1680, 1020)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        self.catalog = Catalog()

        self.stack = QStackedWidget()
        self.library_view = LibraryView(self.catalog)
        self.viewer_view = PhotoViewerView(self.catalog, self.library_view)
        self.editor_view = EditorView(self.catalog)
        self.stack.addWidget(self.library_view)
        self.stack.addWidget(self.viewer_view)
        self.stack.addWidget(self.editor_view)
        self.setCentralWidget(self.stack)

        self.library_view.openPhotoRequested.connect(self._open_viewer)
        self.viewer_view.backRequested.connect(self._show_library)
        self.viewer_view.editRequested.connect(self._open_editor)
        self.editor_view.backRequested.connect(self._show_library)

    def _open_viewer(self, photo_id: int, path: str):
        self.viewer_view.set_source(self.library_view.proxy)
        self.viewer_view.open_photo(photo_id)
        self.stack.setCurrentWidget(self.viewer_view)

    def _open_editor(self, photo_id: int, path: str):
        self.editor_view.open_photo(photo_id, path)
        self.stack.setCurrentWidget(self.editor_view)

    def _show_library(self):
        self.stack.setCurrentWidget(self.library_view)

    def closeEvent(self, event):
        self.editor_view.render_thread.stop()
        self.catalog.close()
        super().closeEvent(event)
