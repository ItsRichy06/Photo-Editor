"""App-wide dark theme: one set of design tokens and one stylesheet for every
screen (library, viewer, editor).

Every standard Qt widget is styled here explicitly. Before this, main.py set
no palette or stylesheet at all, so standard widgets inherited whatever
Windows itself was set to -- on a machine in light mode (a friend's install)
the app rendered as plain white native controls around its own dark
custom-painted widgets. The custom-painted widgets (ImageCanvas,
HistogramWidget, StarRatingWidget, the grid delegates) take their colors
from the same tokens below.

Screens opt into variants through Qt dynamic properties rather than inline
setStyleSheet() calls, so they can't drift apart again:
    button.setProperty("variant", "primary" | "ghost" | "danger")
    label.setProperty("role", "title" | "muted" | "section")
"""

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication

# --- design tokens ------------------------------------------------------------
BG = "#141414"            # window background, image surrounds
PANEL = "#1a1a1a"         # sidebar, side panels, top bars
SURFACE = "#232323"       # controls at rest
SURFACE_HOVER = "#2c2c2c"
BORDER = "#323232"
BORDER_HOVER = "#474747"
TEXT = "#e6e6e6"
MUTED = "#9a9a9a"
FAINT = "#6b6b6b"
ACCENT = "#33ccff"
ACCENT_HOVER = "#5cd7ff"
ACCENT_DIM = "#174a5f"    # selected rows / cells
ACCENT_TEXT = "#04202b"   # text on an ACCENT background
DANGER = "#e57373"
STAR = "#f5b301"
RADIUS = 6
CONTROL_H = 30

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"


def _asset_url(name: str) -> str:
    # QSS url() wants forward slashes even on Windows -- a raw backslashed
    # Windows path breaks the stylesheet parser.
    return (ASSETS_DIR / name).as_posix()


def icon(name: str) -> QIcon:
    """One of the generated UI icons (assets/make_ui_icons.py)."""
    return QIcon(str(ASSETS_DIR / f"{name}.png"))


STYLESHEET = f"""
QWidget {{
    color: {TEXT};
}}
QMainWindow, QStackedWidget {{
    background: {BG};
}}

/* ---- labels ---------------------------------------------------------- */
QLabel[role="title"] {{
    font-size: 15px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel[role="muted"] {{
    color: {MUTED};
}}
QLabel[role="section"] {{
    color: {FAINT};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.6px;
}}

/* ---- buttons --------------------------------------------------------- */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    min-height: {CONTROL_H - 2}px;
    padding: 0 12px;
    color: {TEXT};
}}
QPushButton:hover {{
    background: {SURFACE_HOVER};
    border-color: {BORDER_HOVER};
}}
QPushButton:pressed {{
    background: #1c1c1c;
}}
QPushButton:checked {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    color: #ffffff;
}}
QPushButton:disabled {{
    color: {FAINT};
    background: #1e1e1e;
    border-color: #2a2a2a;
}}
QPushButton[variant="primary"] {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: {ACCENT_TEXT};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[variant="primary"]:disabled {{
    background: #1d3f4c;
    border-color: #1d3f4c;
    color: #5f8796;
}}
QPushButton[variant="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT};
}}
QPushButton[variant="ghost"]:hover {{
    background: {SURFACE_HOVER};
}}
QPushButton[variant="ghost"]:checked {{
    background: {ACCENT_DIM};
    border-color: transparent;
}}
QPushButton[variant="ghost"]:disabled {{
    background: transparent;
    color: {FAINT};
}}
QPushButton[variant="icon"] {{
    background: transparent;
    border: 1px solid transparent;
    padding: 0;
    min-height: 0;
    color: {MUTED};
}}
QPushButton[variant="icon"]:hover {{
    background: {SURFACE_HOVER};
    color: {TEXT};
}}
QPushButton[variant="danger"] {{
    background: #2a1d1d;
    border-color: #4a2c2c;
    color: {DANGER};
}}
QPushButton[variant="danger"]:hover {{
    background: #3a2222;
    border-color: #6b3535;
    color: #ff9d9d;
}}

/* ---- dropdowns ------------------------------------------------------- */
/* The ::drop-down subcontrol must be styled explicitly: left alone, Qt
   paints its own square-cornered arrow button on the right edge, which is
   what made every dropdown rounded on the left and sharp on the right. */
QComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    min-height: {CONTROL_H - 2}px;
    padding: 0 30px 0 10px;
    color: {TEXT};
    combobox-popup: 0;
}}
QComboBox:hover {{
    background: {SURFACE_HOVER};
    border-color: {BORDER_HOVER};
}}
QComboBox:on {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 28px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    image: url({_asset_url("chevron_down.png")});
    width: 10px;
    height: 6px;
}}
QComboBox QAbstractItemView {{
    background: #1f1f1f;
    border: 1px solid {BORDER_HOVER};
    padding: 4px;
    outline: none;
    color: {TEXT};
    selection-background-color: {ACCENT_DIM};
    selection-color: #ffffff;
}}
QComboBox QAbstractItemView::item {{
    min-height: 26px;
    padding: 0 8px;
    border-radius: 4px;
}}
QComboBox QAbstractItemView::item:hover {{
    background: {SURFACE_HOVER};
}}
QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT_DIM};
}}

/* ---- text fields ----------------------------------------------------- */
QLineEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    min-height: {CONTROL_H - 2}px;
    padding: 0 8px;
    color: {TEXT};
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:hover {{
    border-color: {BORDER_HOVER};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}

/* ---- menus ----------------------------------------------------------- */
QMenu {{
    background: #1f1f1f;
    border: 1px solid {BORDER_HOVER};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 28px 6px 12px;
    border-radius: 4px;
    color: {TEXT};
}}
QMenu::item:selected {{
    background: {ACCENT_DIM};
}}
QMenu::item:disabled {{
    color: {FAINT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}

/* ---- lists and trees ------------------------------------------------- */
QListWidget, QListView {{
    background: {BG};
    border: none;
    outline: none;
    color: {TEXT};
}}
QListWidget::item:selected, QListView::item:selected {{
    background: {ACCENT_DIM};
    color: #ffffff;
}}
/* show-decoration-selected stays 0 and ::branch stays transparent: if the
   branch strip (left of the item, where the expand arrow sits) takes the
   selection color too, it's painted as its own square block next to the
   item's rounded highlight -- a visible notch. Styling ::branch at all also
   switches off Qt's own expand arrows, hence the explicit chevrons. */
QTreeView, QTreeWidget {{
    background: transparent;
    border: none;
    outline: none;
    color: {TEXT};
    show-decoration-selected: 0;
}}
QTreeView::branch {{
    background: transparent;
    border: none;
}}
QTreeView::branch:selected {{
    background: {ACCENT_DIM};
}}
QTreeView::branch:has-children:closed {{
    image: url({_asset_url("chevron_right.png")});
}}
QTreeView::branch:has-children:open {{
    image: url({_asset_url("chevron_down.png")});
}}
/* No border-radius here: Qt paints the row's highlight in two pieces (the
   branch strip and the item), and rounding each piece separately is what
   produced the notch next to the selected folder. Square-cornered, the two
   pieces join into one continuous bar. */
QTreeView::item {{
    min-height: 28px;
    padding-left: 2px;
}}
QTreeView::item:hover {{
    background: {SURFACE_HOVER};
}}
QTreeView::item:selected {{
    background: {ACCENT_DIM};
    color: #ffffff;
}}
QTreeView::branch {{
    background: transparent;
}}

/* ---- sections (formerly boxed group frames) -------------------------- */
QGroupBox {{
    border: none;
    border-top: 1px solid {BORDER};
    margin-top: 16px;
    padding-top: 12px;
    font-size: 11px;
    font-weight: 600;
    color: {MUTED};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 0px;
    padding: 0 6px 0 0;
    color: {MUTED};
}}

/* ---- spin boxes ------------------------------------------------------ */
QDoubleSpinBox, QSpinBox {{
    background: #181818;
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 2px 4px;
    color: {TEXT};
}}
QDoubleSpinBox:hover, QSpinBox:hover {{
    border-color: {BORDER_HOVER};
}}
QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
/* As soon as a QSS rule touches QDoubleSpinBox/QSpinBox at all, Qt stops
   drawing its own native up/down arrows -- every subcontrol switches to
   "style all of it or none of it" -- so the buttons and arrow images are
   styled explicitly or they render as blank boxes (that shipped once). */
QDoubleSpinBox::up-button, QSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 16px;
    border-left: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    border-top-right-radius: 4px;
    background: #202020;
}}
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 16px;
    border-left: 1px solid {BORDER};
    border-bottom-right-radius: 4px;
    background: #202020;
}}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
    background: {SURFACE_HOVER};
}}
QDoubleSpinBox::up-button:pressed, QSpinBox::up-button:pressed,
QDoubleSpinBox::down-button:pressed, QSpinBox::down-button:pressed {{
    background: {ACCENT_DIM};
}}
QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{
    image: url({_asset_url("spin_up_arrow.png")});
    width: 8px;
    height: 5px;
}}
QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{
    image: url({_asset_url("spin_down_arrow.png")});
    width: 8px;
    height: 5px;
}}

/* ---- scrolling ------------------------------------------------------- */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #3a3a3a;
    border-radius: 3px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #505050;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0;
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #3a3a3a;
    border-radius: 3px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #505050;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    width: 0;
    background: transparent;
}}

/* ---- misc ------------------------------------------------------------ */
QSplitter::handle {{
    background: {BORDER};
}}
QCheckBox {{
    color: {TEXT};
}}
QToolTip {{
    background: #262626;
    color: {TEXT};
    border: 1px solid {BORDER_HOVER};
    padding: 4px 6px;
}}
QProgressBar {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT};
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
}}
"""


def apply_dark_theme(app: QApplication):
    # Fusion is the one built-in Qt style that fully respects a custom
    # QPalette everywhere -- the native "windows11" style only partially
    # does, so some widgets silently kept following the OS theme.
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor("#181818"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#262626"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ff5555"))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_DIM))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(FAINT))
    app.setPalette(palette)

    app.setStyleSheet(STYLESHEET)
