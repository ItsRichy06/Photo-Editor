"""The library's left sidebar: saved photo folders, each with its subfolders,
so switching folders never needs the Open Folder dialog.

Saved folders persist in QSettings ("library/folders"). The first time the
sidebar runs it seeds that list from the folders the catalog already has
photos in, so an existing library shows up without re-adding anything.
"""

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import image_io
from . import theme

FOLDERS_KEY = "library/folders"
PATH_ROLE = Qt.ItemDataRole.UserRole + 1
MISSING_ROLE = Qt.ItemDataRole.UserRole + 2
WIDTH = 236


def _folder_icon(color: str) -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(color), 2.2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(4, 10, 24, 17, 3, 3)
    p.drawLine(4, 12, 4, 8)
    p.drawLine(4, 8, 12, 8)
    p.drawLine(12, 8, 15, 11)
    p.end()
    return QIcon(pm)


def _has_photos_or_subfolders(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir() and not entry.name.startswith("."):
                    return True
                if entry.is_file() and image_io.is_supported(entry.name):
                    return True
    except OSError:
        pass
    return False


def _subfolders(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as it:
            dirs = [Path(e.path) for e in it if e.is_dir() and not e.name.startswith((".", "$"))]
    except OSError:
        return []
    return sorted((d for d in dirs if _has_photos_or_subfolders(d)), key=lambda d: d.name.lower())


def seed_folders_from_catalog(catalog) -> list[str]:
    """Top-level folders the catalog has photos in (a folder whose parent is
    also in the set is a subfolder and shows under it instead), that still
    exist on disk."""
    dirs = set()
    with catalog._lock:
        for (path,) in catalog.conn.execute("SELECT path FROM photos"):
            dirs.add(os.path.dirname(path))
    roots = []
    for d in sorted(dirs, key=str.lower):
        if any(d.lower().startswith(r.lower() + os.sep) for r in roots):
            continue
        if os.path.isdir(d):
            roots.append(d)
    return roots


class FolderSidebar(QWidget):
    folderSelected = Signal(str)

    def __init__(self, settings, catalog=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setFixedWidth(WIDTH)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"FolderSidebar {{ background: {theme.PANEL}; border-right: 1px solid {theme.BORDER}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._icon = _folder_icon(theme.MUTED)
        self._icon_missing = _folder_icon(theme.FAINT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 14, 8, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(6, 0, 0, 0)
        title = QLabel("FOLDERS")
        title.setProperty("role", "section")
        header.addWidget(title)
        header.addStretch(1)
        self.add_btn = QPushButton("+")
        self.add_btn.setProperty("variant", "icon")
        self.add_btn.setFixedSize(26, 26)
        self.add_btn.setToolTip("Add a folder to the library")
        f = self.add_btn.font()
        f.setPointSize(14)
        self.add_btn.setFont(f)
        self.add_btn.clicked.connect(self.choose_and_add)
        header.addWidget(self.add_btn)
        layout.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setAnimated(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemExpanded.connect(self._populate_children)
        layout.addWidget(self.tree, stretch=1)

        self.empty_hint = QLabel("Add a folder with + to start\nbrowsing your photos here.")
        self.empty_hint.setProperty("role", "muted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_hint)

        stored = self.settings.value(FOLDERS_KEY, None)
        if stored is None:
            stored = seed_folders_from_catalog(catalog) if catalog is not None else []
            self.settings.setValue(FOLDERS_KEY, stored)
        if isinstance(stored, str):  # QSettings returns a bare str for a 1-item list
            stored = [stored]
        self._folders = [str(Path(p)) for p in (stored or [])]
        self._rebuild()

    # ------------------------------------------------------------- folders
    def folders(self) -> list[str]:
        return list(self._folders)

    def add_folder(self, path: str):
        path = str(Path(path))
        if not any(path.lower() == f.lower() for f in self._folders):
            self._folders.append(path)
            self._folders.sort(key=lambda p: Path(p).name.lower())
            self._save()
            self._rebuild()
        self.select_folder(path)

    def remove_folder(self, path: str):
        self._folders = [f for f in self._folders if f.lower() != path.lower()]
        self._save()
        self._rebuild()

    def choose_and_add(self):
        start = self._folders[-1] if self._folders else str(Path.home() / "Pictures")
        folder = QFileDialog.getExistingDirectory(self, "Add a photo folder", start)
        if folder:
            self.add_folder(folder)
            self.folderSelected.emit(str(Path(folder)))

    def _save(self):
        self.settings.setValue(FOLDERS_KEY, self._folders)

    # ---------------------------------------------------------------- tree
    def _make_item(self, path: Path, top_level: bool) -> QTreeWidgetItem:
        missing = not path.is_dir()
        item = QTreeWidgetItem([path.name or str(path)])
        item.setData(0, PATH_ROLE, str(path))
        item.setData(0, MISSING_ROLE, missing)
        item.setToolTip(0, f"{path}  (not found)" if missing else str(path))
        item.setIcon(0, self._icon_missing if missing else self._icon)
        if missing:
            item.setForeground(0, QColor(theme.FAINT))
        elif _subfolders(path):
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if top_level:
            font = item.font(0)
            font.setWeight(QFont.Weight.DemiBold)
            item.setFont(0, font)
        return item

    def _rebuild(self):
        current = self.current_path()
        self.tree.clear()
        for f in self._folders:
            self.tree.addTopLevelItem(self._make_item(Path(f), top_level=True))
        self.empty_hint.setVisible(not self._folders)
        if current:
            self.select_folder(current, emit=False)

    def _populate_children(self, item: QTreeWidgetItem):
        if item.data(0, Qt.ItemDataRole.UserRole + 3):  # already populated
            return
        item.setData(0, Qt.ItemDataRole.UserRole + 3, True)
        item.takeChildren()
        for sub in _subfolders(Path(item.data(0, PATH_ROLE))):
            item.addChild(self._make_item(sub, top_level=False))
        if item.childCount() == 0:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)

    def refresh(self, path: str):
        """Re-read the subfolders under `path` (e.g. after Group into Folder
        created one), keeping it expanded."""
        item = self._find_item(path)
        if item is None:
            return
        item.setData(0, Qt.ItemDataRole.UserRole + 3, False)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._populate_children(item)
        item.setExpanded(True)

    def _find_item(self, path: str) -> QTreeWidgetItem | None:
        target = str(Path(path)).lower()
        best = None
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            top_path = top.data(0, PATH_ROLE).lower()
            if target == top_path:
                return top
            if target.startswith(top_path + os.sep):
                best = top
        if best is None:
            return None
        node = best
        while True:
            node.setExpanded(True)
            self._populate_children(node)
            nxt = None
            for c in range(node.childCount()):
                cp = node.child(c).data(0, PATH_ROLE).lower()
                if target == cp:
                    return node.child(c)
                if target.startswith(cp + os.sep):
                    nxt = node.child(c)
            if nxt is None:
                return None
            node = nxt

    def select_folder(self, path: str, emit: bool = False):
        item = self._find_item(path)
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(item) if item else self.tree.clearSelection()
        self.tree.blockSignals(False)
        if item is not None and emit:
            self.folderSelected.emit(item.data(0, PATH_ROLE))

    def current_path(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, PATH_ROLE) if item else None

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int):
        if item.data(0, MISSING_ROLE):
            return
        self.folderSelected.emit(item.data(0, PATH_ROLE))

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            menu = QMenu(self)
            menu.addAction("Add Folder", self.choose_and_add)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        path = item.data(0, PATH_ROLE)
        menu = QMenu(self)
        open_act = menu.addAction("Open", lambda: self.folderSelected.emit(path))
        open_act.setEnabled(not item.data(0, MISSING_ROLE))
        menu.addAction("Show in Explorer", lambda: _show_in_explorer(path))
        menu.addAction("Refresh Subfolders", lambda: self.refresh(path))
        if item.parent() is None:
            menu.addSeparator()
            menu.addAction("Remove from Sidebar", lambda: self.remove_folder(path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))


def _show_in_explorer(path: str):
    p = Path(path)
    if p.is_file():
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif p.is_dir():
        subprocess.Popen(["explorer", str(p)])
