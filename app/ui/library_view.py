"""Library/browser view: pick a folder, see thumbnails, rate and flag photos.

Uses a model/view architecture (QAbstractListModel + QSortFilterProxyModel +
QStyledItemDelegate) instead of one live QWidget per photo -- at a few
thousand photos, a widget per cell made scanning/scrolling unusably slow and
caused visible layout-thrashing artifacts. QListView only ever paints the
cells actually in the viewport, and thumbnail generation itself is farmed out
across a thread pool instead of done one file at a time.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPointF,
    QRect,
    QSize,
    Qt,
    QSortFilterProxyModel,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .. import image_io, thumbnails
from ..settings import LAST_LIBRARY_FOLDER, get_settings
from . import stacking_dialogs, widgets

THUMB_SIZE = 320
CELL_W = 190
CELL_H = 236
THUMB_MARGIN = 8
THUMB_H = 150
STAR_SIZE = 16
STAR_Y_OFFSET = 178


class PhotoListModel(QAbstractListModel):
    RatingRole = Qt.ItemDataRole.UserRole + 1
    FlagRole = Qt.ItemDataRole.UserRole + 2
    PathRole = Qt.ItemDataRole.UserRole + 3
    IsRawRole = Qt.ItemDataRole.UserRole + 4
    MtimeRole = Qt.ItemDataRole.UserRole + 5
    PhotoIdRole = Qt.ItemDataRole.UserRole + 6
    NameRole = Qt.ItemDataRole.UserRole + 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._thumbnails = {}
        self._id_to_row = {}

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnails.get(item["id"])
        if role == self.RatingRole:
            return item["rating"]
        if role == self.FlagRole:
            return item["flag"]
        if role == self.PathRole:
            return item["path"]
        if role == self.IsRawRole:
            return item["is_raw"]
        if role == self.MtimeRole:
            return item["mtime"]
        if role == self.PhotoIdRole:
            return item["id"]
        if role == self.NameRole:
            return item["name"]
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid():
            return False
        item = self._items[index.row()]
        if role == self.RatingRole:
            item["rating"] = value
        elif role == self.FlagRole:
            item["flag"] = value
        else:
            return False
        self.dataChanged.emit(index, index, [role])
        return True

    def add_photos(self, photos_and_flags):
        """photos_and_flags: list of (photo_dict, is_raw). Assumes all-new ids."""
        if not photos_and_flags:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(photos_and_flags) - 1)
        for photo, is_raw in photos_and_flags:
            row = len(self._items)
            self._items.append(
                {
                    "id": photo["id"],
                    "path": photo["path"],
                    "rating": photo["rating"],
                    "flag": photo["flag"],
                    "mtime": photo["mtime"],
                    "is_raw": is_raw,
                    "name": Path(photo["path"]).name,
                }
            )
            self._id_to_row[photo["id"]] = row
        self.endInsertRows()

    def set_thumbnail(self, photo_id, pixmap):
        row = self._id_to_row.get(photo_id)
        if row is None:
            return
        self._thumbnails[photo_id] = pixmap
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def clear(self):
        self.beginResetModel()
        self._items = []
        self._thumbnails = {}
        self._id_to_row = {}
        self.endResetModel()


class PhotoFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.min_rating = 0
        self.type_filter = "all"
        self.sort_mode = "name"
        self.setDynamicSortFilter(True)

    def set_min_rating(self, rating: int):
        self.min_rating = rating
        self.invalidateFilter()

    def set_type_filter(self, type_filter: str):
        self.type_filter = type_filter
        self.invalidateFilter()

    def set_sort_mode(self, mode: str):
        self.sort_mode = mode
        self.invalidate()
        self.sort(0)

    def filterAcceptsRow(self, source_row, source_parent):
        index = self.sourceModel().index(source_row, 0, source_parent)
        if self.min_rating > 0:
            rating = index.data(PhotoListModel.RatingRole) or 0
            if rating < self.min_rating:
                return False
        if self.type_filter != "all":
            is_raw = bool(index.data(PhotoListModel.IsRawRole))
            if self.type_filter == "jpeg" and is_raw:
                return False
            if self.type_filter == "raw" and not is_raw:
                return False
        return True

    def lessThan(self, left, right):
        if self.sort_mode == "date":
            return (left.data(PhotoListModel.MtimeRole) or 0) < (right.data(PhotoListModel.MtimeRole) or 0)
        if self.sort_mode == "rating":
            return (left.data(PhotoListModel.RatingRole) or 0) > (right.data(PhotoListModel.RatingRole) or 0)
        return (left.data(PhotoListModel.NameRole) or "").lower() < (right.data(PhotoListModel.NameRole) or "").lower()


class PhotoDelegate(QStyledItemDelegate):
    ratingEdited = Signal(int, int)  # photo_id, rating

    def sizeHint(self, option, index):
        return QSize(CELL_W, CELL_H)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect
        painter.fillRect(rect, QColor("#2d5a8c") if option.state & QStyle.StateFlag.State_Selected else QColor("#262626"))

        thumb_rect = QRect(rect.x() + THUMB_MARGIN, rect.y() + THUMB_MARGIN, CELL_W - 2 * THUMB_MARGIN, THUMB_H)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if pixmap:
            scaled = pixmap.scaled(
                thumb_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            x = thumb_rect.x() + (thumb_rect.width() - scaled.width()) // 2
            y = thumb_rect.y() + (thumb_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.fillRect(thumb_rect, QColor("#3a3a3a"))
            painter.setPen(QColor("#888888"))
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "Loading…")

        if index.data(PhotoListModel.IsRawRole):
            painter.setPen(QColor("#66aaff"))
            painter.drawText(QRect(rect.right() - 42, rect.y() + 10, 36, 16), Qt.AlignmentFlag.AlignRight, "RAW")

        name = index.data(PhotoListModel.NameRole) or ""
        name_rect = QRect(rect.x() + THUMB_MARGIN, rect.y() + THUMB_MARGIN + THUMB_H + 4, CELL_W - 2 * THUMB_MARGIN, 16)
        painter.setPen(QColor("#cccccc"))
        elided = painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, name_rect.width())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft, elided)

        rating = index.data(PhotoListModel.RatingRole) or 0
        star_y = rect.y() + STAR_Y_OFFSET
        for i in range(5):
            filled = i < rating
            color = QColor("#f5b301") if filled else QColor("#777777")
            painter.setPen(color)
            painter.setBrush(color if filled else QColor(0, 0, 0, 0))
            widgets.StarRatingWidget._draw_star(painter, rect.x() + THUMB_MARGIN + i * STAR_SIZE + STAR_SIZE / 2, star_y + STAR_SIZE / 2, STAR_SIZE * 0.42)

        flag = index.data(PhotoListModel.FlagRole)
        flag_rect = QRect(rect.x() + THUMB_MARGIN, rect.y() + STAR_Y_OFFSET + 18, CELL_W - 2 * THUMB_MARGIN, 16)
        if flag == "pick":
            painter.setPen(QColor("#4caf50"))
            painter.drawText(flag_rect, Qt.AlignmentFlag.AlignLeft, "✓ Pick")
        elif flag == "reject":
            painter.setPen(QColor("#e05252"))
            painter.drawText(flag_rect, Qt.AlignmentFlag.AlignLeft, "✗ Reject")

        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonPress:
            pos = event.position().toPoint()
            rel_x = pos.x() - option.rect.x() - THUMB_MARGIN
            rel_y = pos.y() - option.rect.y() - STAR_Y_OFFSET
            if 0 <= rel_y <= STAR_SIZE and 0 <= rel_x <= STAR_SIZE * 5:
                star_idx = int(rel_x // STAR_SIZE) + 1
                photo_id = index.data(PhotoListModel.PhotoIdRole)
                current = index.data(PhotoListModel.RatingRole) or 0
                new_rating = 0 if star_idx == current else star_idx
                model.setData(index, new_rating, PhotoListModel.RatingRole)
                self.ratingEdited.emit(photo_id, new_rating)
                return True
        return super().editorEvent(event, model, option, index)


class ThumbnailLoaderThread(QThread):
    scan_registered = Signal(list)  # list of (photo_dict, is_raw), emitted once
    photo_ready = Signal(int, object)  # photo_id, PIL.Image
    finished_scanning = Signal(int)

    def __init__(self, folder, catalog, recursive: bool = True, thumb_size: int = THUMB_SIZE, max_workers=None):
        super().__init__()
        self.folder = folder
        self.catalog = catalog
        self.recursive = recursive
        self.thumb_size = thumb_size
        self.max_workers = max_workers or min(8, os.cpu_count() or 4)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        paths = list(image_io.scan_folder(self.folder, self.recursive))
        if not paths or self._stop:
            self.finished_scanning.emit(0)
            return

        photos_by_path = self.catalog.get_or_create_photos_bulk([str(p) for p in paths])

        registered = []
        path_by_id = {}
        for p in paths:
            photo = photos_by_path.get(str(p.resolve()))
            if photo is None:
                continue
            path_by_id[photo["id"]] = p
            registered.append((photo, image_io.is_raw(p)))
        self.scan_registered.emit(registered)

        done = 0
        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            future_to_id = {
                executor.submit(thumbnails.get_thumbnail, p, self.thumb_size): pid
                for pid, p in path_by_id.items()
            }
            for future in as_completed(future_to_id):
                if self._stop:
                    break
                pid = future_to_id[future]
                try:
                    thumb = future.result()
                except Exception as exc:  # noqa: BLE001 - keep going past a bad file
                    print(f"[library] failed to load {path_by_id[pid]}: {exc}")
                    continue
                self.photo_ready.emit(pid, thumb)
                done += 1
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        self.finished_scanning.emit(done)


class LibraryView(QWidget):
    openPhotoRequested = Signal(int, str)

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.settings = get_settings()
        self.loader_thread = None
        self.current_folder = None
        # Old ThumbnailLoaderThreads being wound down after a folder switch
        # (see _retire_loader_thread) -- kept referenced here so Python
        # doesn't garbage-collect the wrapper object while the underlying
        # QThread is still actually running.
        self._retiring_threads = []
        self._stack_progress = None
        self._stack_analysis_thread = None
        self._stack_process_thread = None
        self._stack_results = []

        self.model = PhotoListModel(self)
        self.proxy = PhotoFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        main_layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.open_btn = QPushButton("Open Folder…")
        self.open_btn.clicked.connect(self.choose_folder)
        toolbar.addWidget(self.open_btn)

        self.stack_btn = QPushButton("Stack Photos…")
        self.stack_btn.setToolTip(
            "Select 2+ photos (Ctrl/Shift-click), then auto-detect and merge HDR exposure "
            "brackets and focus stacks among them."
        )
        self.stack_btn.clicked.connect(self._on_stack_photos)
        toolbar.addWidget(self.stack_btn)

        toolbar.addWidget(QLabel("Show:"))
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All formats", "JPEG only", "RAW only"])
        self.type_filter.currentIndexChanged.connect(self._on_type_filter_changed)
        toolbar.addWidget(self.type_filter)

        toolbar.addWidget(QLabel("Min rating:"))
        self.rating_filter = QComboBox()
        self.rating_filter.addItems(["All", "1+", "2+", "3+", "4+", "5"])
        self.rating_filter.currentIndexChanged.connect(lambda i: self.proxy.set_min_rating(i))
        toolbar.addWidget(self.rating_filter)

        toolbar.addWidget(QLabel("Sort:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Name", "Date modified", "Rating"])
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        toolbar.addWidget(self.sort_combo)

        self.status_label = QLabel("No folder opened")
        toolbar.addWidget(self.status_label)
        toolbar.addStretch(1)
        main_layout.addLayout(toolbar)

        hint = QLabel(
            "Select a photo, then 1-5 = rate, P = pick, X = reject, U = clear flag, double-click = edit. "
            "Ctrl/Shift-click to select multiple, then Stack Photos to auto-merge HDR/focus brackets."
        )
        hint.setStyleSheet("color: #888888; font-size: 11px;")
        main_layout.addWidget(hint)

        self.list_view = QListView()
        self.list_view.setModel(self.proxy)
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSpacing(6)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.delegate = PhotoDelegate(self.list_view)
        self.delegate.ratingEdited.connect(self._on_rating_edited)
        self.list_view.setItemDelegate(self.delegate)
        self.list_view.doubleClicked.connect(self._on_double_clicked)
        main_layout.addWidget(self.list_view, stretch=1)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._install_shortcuts()

        last_folder = self.settings.value(LAST_LIBRARY_FOLDER, "")
        if last_folder and Path(last_folder).is_dir():
            QTimer.singleShot(0, lambda: self.load_folder(last_folder))

    def _install_shortcuts(self):
        for i in range(6):
            sc = QShortcut(QKeySequence(str(i)), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda rating=i: self._rate_selected(rating))
        sc_pick = QShortcut(QKeySequence("P"), self)
        sc_pick.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_pick.activated.connect(lambda: self._flag_selected("pick"))
        sc_reject = QShortcut(QKeySequence("X"), self)
        sc_reject.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_reject.activated.connect(lambda: self._flag_selected("reject"))
        sc_clear = QShortcut(QKeySequence("U"), self)
        sc_clear.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_clear.activated.connect(lambda: self._flag_selected("none"))

    def _current_index(self):
        sel = self.list_view.selectionModel()
        if sel is None:
            return None
        indexes = sel.selectedIndexes()
        return indexes[0] if indexes else None

    def _rate_selected(self, rating: int):
        index = self._current_index()
        if index is None:
            return
        photo_id = index.data(PhotoListModel.PhotoIdRole)
        self.catalog.set_rating(photo_id, rating)
        self.proxy.setData(index, rating, PhotoListModel.RatingRole)

    def _flag_selected(self, flag: str):
        index = self._current_index()
        if index is None:
            return
        photo_id = index.data(PhotoListModel.PhotoIdRole)
        self.catalog.set_flag(photo_id, flag)
        self.proxy.setData(index, flag, PhotoListModel.FlagRole)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose photo folder", self.settings.value(LAST_LIBRARY_FOLDER, "")
        )
        if folder:
            self.load_folder(folder)

    def _retire_loader_thread(self, thread: "ThumbnailLoaderThread"):
        """Ask a superseded loader thread to stop and detach it from the UI,
        without blocking the calling (GUI) thread on it finishing.

        This used to be `thread.stop(); thread.wait()`, which deadlocks: the
        thread being waited on is Python code (plus rawpy/PIL calls) that
        needs the GIL to make progress, but QThread.wait() does not release
        the GIL while the GUI thread blocks in it -- so the old thread can
        never get the GIL back to actually finish, and the whole app hangs
        forever (confirmed: no progress after 30+ seconds on a 12-photo
        folder that normally loads in under a second). Instead, detach its
        signals so any results it still emits while winding down land on
        nothing, and keep a reference until it genuinely finishes so Python
        doesn't garbage-collect the wrapper while the QThread is still alive.
        """
        thread.stop()
        for sig in (thread.scan_registered, thread.photo_ready, thread.finished_scanning):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._retiring_threads.append(thread)
        thread.finished.connect(lambda: self._retiring_threads.remove(thread))

    def load_folder(self, folder: str):
        if self.loader_thread and self.loader_thread.isRunning():
            self._retire_loader_thread(self.loader_thread)

        self.model.clear()
        self.current_folder = folder
        self.settings.setValue(LAST_LIBRARY_FOLDER, folder)
        self.status_label.setText("Scanning…")

        self.loader_thread = ThumbnailLoaderThread(folder, self.catalog)
        self.loader_thread.scan_registered.connect(self._on_scan_registered)
        self.loader_thread.photo_ready.connect(self._on_photo_ready)
        self.loader_thread.finished_scanning.connect(self._on_scan_finished)
        self.loader_thread.start()

    def _on_scan_registered(self, registered: list):
        self.model.add_photos(registered)
        self.status_label.setText(f"{len(registered)} photos, loading thumbnails…")

    def _on_photo_ready(self, photo_id: int, pil_thumb):
        pixmap = widgets.pil_to_qpixmap(pil_thumb)
        self.model.set_thumbnail(photo_id, pixmap)

    def _on_scan_finished(self, count: int):
        self.status_label.setText(f"{count} photos")

    # ---------------------------------------------------------- stacking

    def _selected_paths(self) -> list:
        indexes = self.list_view.selectionModel().selectedIndexes()
        return [Path(idx.data(PhotoListModel.PathRole)) for idx in indexes]

    def _on_stack_photos(self):
        paths = self._selected_paths()
        if len(paths) < 2:
            QMessageBox.information(self, "Stack Photos", "Select at least 2 photos to stack (Ctrl/Shift-click).")
            return

        self._stack_progress = QProgressDialog("Analyzing selected photos…", "Cancel", 0, len(paths), self)
        self._stack_progress.setWindowTitle("Stack Photos")
        self._stack_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._stack_progress.setMinimumDuration(0)
        self._stack_progress.setValue(0)

        self._stack_analysis_thread = stacking_dialogs.StackAnalysisThread(paths, self)
        self._stack_analysis_thread.progress.connect(self._on_stack_analysis_progress)
        self._stack_analysis_thread.finished_analysis.connect(self._on_stack_analysis_finished)
        self._stack_progress.canceled.connect(self._stack_analysis_thread.stop)
        self._stack_analysis_thread.start()

    def _on_stack_analysis_progress(self, done: int, total: int):
        if self._stack_progress is not None:
            self._stack_progress.setValue(done)

    def _on_stack_analysis_finished(self, candidates: list, leftover: list):
        if self._stack_progress is not None:
            self._stack_progress.close()
            self._stack_progress = None
        if not candidates:
            QMessageBox.information(
                self, "Stack Photos", "No HDR exposure brackets or focus stacks were detected in the selected photos."
            )
            return
        dialog = stacking_dialogs.StackPreviewDialog(candidates, leftover, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.selected_candidates()
        if chosen:
            self._run_stack_processing(chosen)

    def _run_stack_processing(self, candidates: list):
        self._stack_results = []
        self._stack_progress = QProgressDialog("Processing…", "Cancel", 0, len(candidates), self)
        self._stack_progress.setWindowTitle("Stack Photos")
        self._stack_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._stack_progress.setMinimumDuration(0)
        self._stack_progress.setValue(0)

        self._stack_process_thread = stacking_dialogs.StackProcessingThread(candidates, self.catalog, self)
        self._stack_process_thread.progress.connect(self._on_stack_process_progress)
        self._stack_process_thread.group_done.connect(self._on_stack_group_done)
        self._stack_process_thread.finished_processing.connect(self._on_stack_process_finished)
        self._stack_progress.canceled.connect(self._stack_process_thread.stop)
        self._stack_process_thread.start()

    def _on_stack_process_progress(self, done: int, total: int, label: str):
        if self._stack_progress is None:
            return
        if done < total:
            self._stack_progress.setLabelText(f"Processing {done + 1}/{total}: {label}")
        else:
            self._stack_progress.setLabelText("Finishing…")
        self._stack_progress.setValue(done)

    def _on_stack_group_done(self, candidate, out_path, error):
        self._stack_results.append((candidate, out_path, error))

    def _on_stack_process_finished(self):
        if self._stack_progress is not None:
            self._stack_progress.close()
            self._stack_progress = None

        succeeded = [r for r in self._stack_results if r[1] is not None]
        failed = [r for r in self._stack_results if r[1] is None]
        message = f"Created {len(succeeded)} new photo{'s' if len(succeeded) != 1 else ''}."
        if failed:
            message += f"\n\n{len(failed)} group{'s' if len(failed) != 1 else ''} failed:\n" + "\n".join(
                f"  {c.label}: {err}" for c, _, err in failed[:8]
            )
        QMessageBox.information(self, "Stack Photos", message)

        if succeeded and self.current_folder:
            self.load_folder(self.current_folder)  # picks up the new files, non-blocking (see _retire_loader_thread)

    def _on_double_clicked(self, index):
        if not index.isValid():
            return
        photo_id = index.data(PhotoListModel.PhotoIdRole)
        path = index.data(PhotoListModel.PathRole)
        self.openPhotoRequested.emit(photo_id, path)

    def _on_rating_edited(self, photo_id: int, rating: int):
        self.catalog.set_rating(photo_id, rating)

    def _on_type_filter_changed(self, idx: int):
        self.proxy.set_type_filter({0: "all", 1: "jpeg", 2: "raw"}.get(idx, "all"))

    def _on_sort_changed(self, idx: int):
        self.proxy.set_sort_mode({0: "name", 1: "date", 2: "rating"}.get(idx, "name"))
