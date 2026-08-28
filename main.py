import sys
from pathlib import Path

# The bundled installer runs this via a self-contained embeddable Python
# distribution, which -- unlike a normal Python install -- does NOT
# automatically add this script's own directory to sys.path (its _pth file
# fully overrides the usual path setup). Without this, `import app...` below
# fails with ModuleNotFoundError even though app/ sits right next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from app.ui.theme import apply_dark_theme

APP_ID = "PhotoEditor.PhotoEditor.1"
ICON_PATH = Path(__file__).parent / "assets" / "app_icon.ico"


def _set_windows_app_id():
    """Without this, Windows groups the taskbar icon under python.exe's
    generic icon instead of showing the app's own -- this tells the shell to
    treat this process as its own distinct application. Must run before the
    QApplication/any window is created."""
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


def main():
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("Photo Editor")
    # Explicit, OS-theme-independent styling -- without this the app just
    # inherited whatever light/dark mode Windows itself was set to, which is
    # why it looked completely different (plain white, unstyled controls) on
    # a machine with Windows in light mode.
    apply_dark_theme(app)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
