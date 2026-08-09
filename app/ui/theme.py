"""Explicit app-wide dark theme.

Every custom-painted widget in this app (ImageCanvas, HistogramWidget,
StarRatingWidget, the library grid's PhotoDelegate) already hardcodes its own
dark colors via QPainter, so those always look right. But main.py never set
an app-level palette or stylesheet, which meant every *standard* Qt widget
(QGroupBox, QPushButton, QComboBox, QListWidget, scrollbars, QDoubleSpinBox,
...) was just inheriting whatever theme Windows itself was set to. On a
machine with Windows in dark mode that accidentally looked fine and this went
unnoticed for a long time -- but on a machine in light mode (confirmed: a
friend's install), the whole app around those custom-painted widgets renders
as plain white native Qt controls. This makes the app's look explicit and
consistent regardless of the OS theme, using the same palette (dark
backgrounds, #33ccff cyan accent, light gray text) already established by
those custom-painted widgets.
"""

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#33ccff"
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"


def _asset_url(name: str) -> str:
    # QSS url() wants forward slashes even on Windows -- a raw backslashed
    # Windows path breaks the stylesheet parser.
    return (ASSETS_DIR / name).as_posix()

STYLESHEET = f"""
QGroupBox {{
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: 600;
    color: #cccccc;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: #999999;
}}
QPushButton {{
    background: #2a2a2a;
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    padding: 5px 10px;
    color: #e0e0e0;
}}
QPushButton:hover {{
    background: #333333;
    border-color: #4a4a4a;
}}
QPushButton:pressed {{
    background: #1a1a1a;
}}
QPushButton:checked {{
    background: #1c4a63;
    border-color: {ACCENT};
    color: #ffffff;
}}
QPushButton:disabled {{
    color: #6a6a6a;
    border-color: #2a2a2a;
}}
QComboBox {{
    background: #2a2a2a;
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    padding: 4px 8px;
    color: #e0e0e0;
}}
QComboBox:hover {{
    border-color: #4a4a4a;
}}
QComboBox QAbstractItemView {{
    background: #2a2a2a;
    color: #e0e0e0;
    selection-background-color: {ACCENT};
    selection-color: #0a0a0a;
    border: 1px solid #3a3a3a;
    outline: none;
}}
QListWidget, QListView {{
    background: #181818;
    border: 1px solid #3a3a3a;
    color: #e0e0e0;
    outline: none;
}}
QListWidget::item:selected, QListView::item:selected {{
    background: #1c4a63;
    color: #ffffff;
}}
QDoubleSpinBox, QSpinBox {{
    background: #151515;
    border: 1px solid #3a3a3a;
    border-radius: 3px;
    padding: 2px 4px;
    color: #e0e0e0;
}}
QDoubleSpinBox:hover, QSpinBox:hover {{
    border-color: #4a4a4a;
}}
/* As soon as a QSS rule touches QDoubleSpinBox/QSpinBox at all, Qt stops
   drawing ITS OWN native up/down arrows for it -- every subcontrol (buttons,
   arrows) switches to "you style all of it or none of it", so without these
   rules the buttons render as blank boxes with no arrow glyph in them at
   all (confirmed -- that's what shipped before this fix). Drawing the
   arrows as plain CSS triangles (a zero-size box with colored/transparent
   borders) is the standard, reliable way to get a crisp, always-visible
   arrow back rather than depending on the style to supply one. */
QDoubleSpinBox::up-button, QSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 16px;
    border-left: 1px solid #3a3a3a;
    border-bottom: 1px solid #3a3a3a;
    border-top-right-radius: 3px;
    background: #202020;
}}
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 16px;
    border-left: 1px solid #3a3a3a;
    border-bottom-right-radius: 3px;
    background: #202020;
}}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
    background: #2f2f2f;
}}
QDoubleSpinBox::up-button:pressed, QSpinBox::up-button:pressed,
QDoubleSpinBox::down-button:pressed, QSpinBox::down-button:pressed {{
    background: #1c4a63;
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
QScrollArea {{
    background: #1e1e1e;
    border: none;
}}
QScrollBar:vertical {{
    background: #1a1a1a;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #3a3a3a;
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: #4a4a4a;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: #1a1a1a;
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #3a3a3a;
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #4a4a4a;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QCheckBox {{
    color: #e0e0e0;
}}
QToolTip {{
    background: #2a2a2a;
    color: #e0e0e0;
    border: 1px solid #3a3a3a;
}}
"""


def apply_dark_theme(app: QApplication):
    # Fusion is the one built-in Qt style that fully respects a custom
    # QPalette everywhere -- the native "windows11" style (Qt's default on
    # Windows) only partially does, which is how this got missed before:
    # some widgets would follow the palette and others would silently keep
    # following the OS theme regardless.
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e0e0e0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#151515"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#252525"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2a2a2a"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#e0e0e0"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e0e0e0"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#2a2a2a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e0e0e0"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ff5555"))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#6a6a6a"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#6a6a6a"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#6a6a6a"))
    app.setPalette(palette)

    app.setStyleSheet(STYLESHEET)
