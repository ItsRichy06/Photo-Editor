"""Library/browser view: pick a folder, see thumbnails, rate and flag photos.

Uses a model/view architecture (QAbstractListModel + QSortFilterProxyModel +
QStyledItemDelegate) instead of one live QWidget per photo -- at a few
thousand photos, a widget per cell made scanning/scrolling unusably slow and
caused visible layout-thrashing artifacts. QListView only ever paints the
cells actually in the viewport, and thumbnail generation itself is farmed out
across a thread pool instead of done one file at a time.
"""

import itertools
import os
import queue
import threading
from pathlib import Path

import send2trash

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPoint,
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
    QLineEdit,
    QListView,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .. import image_io, quality, thumbnails
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
    SourceKindRole = Qt.ItemDataRole.UserRole + 8  # None | "focus" | "hdr" -- see Catalog.set_source_kind

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
        if role == self.SourceKindRole:
            return item["source_kind"]
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
                    "source_kind": photo.get("source_kind"),
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

    def set_rating_by_id(self, photo_id, rating: int):
        """Like setData(..., RatingRole) but addressed by photo id -- for
        callers (e.g. a background auto-rate pass) that only have the id,
        not a QModelIndex into the current (possibly filtered/sorted) view."""
        row = self._id_to_row.get(photo_id)
        if row is None:
            return
        self._items[row]["rating"] = rating
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [self.RatingRole])

    def unrated_photos(self) -> list:
        """[{"id", "path", "is_raw"}, ...] for every photo with no rating yet
        -- what a bulk auto-rate pass should consider, so it never touches a
        photo the user already rated by hand."""
        return [
            {"id": item["id"], "path": item["path"], "is_raw": item["is_raw"]}
            for item in self._items
            if not item["rating"]
        ]

    def clear(self):
        self.beginResetModel()
        self._items = []
        self._thumbnails = {}
        self._id_to_row = {}
        self.endResetModel()


class PhotoFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rating_filter = 0  # 0 = no filter, else an exact 1-5 star match
        self.type_filter = "all"
        self.kind_filter = "all"  # "all" | "focus" | "hdr" | "originals"
        self.search_text = ""
        self.sort_mode = "name"
        self.setDynamicSortFilter(True)

    def set_rating_filter(self, rating: int):
        """rating: 0 shows everything; 1-5 shows only photos with exactly
        that many stars (not "at least" -- exact, so you can isolate e.g.
        just the 2-stars still waiting on a second look)."""
        self.rating_filter = rating
        self.invalidateFilter()

    def set_type_filter(self, type_filter: str):
        self.type_filter = type_filter
        self.invalidateFilter()

    def set_kind_filter(self, kind_filter: str):
        self.kind_filter = kind_filter
        self.invalidateFilter()

    def set_search_text(self, text: str):
        self.search_text = text.strip().lower()
        self.invalidateFilter()

    def set_sort_mode(self, mode: str):
        self.sort_mode = mode
        self.invalidate()
        self.sort(0)

    def filterAcceptsRow(self, source_row, source_parent):
        index = self.sourceModel().index(source_row, 0, source_parent)
        if self.rating_filter > 0:
            rating = index.data(PhotoListModel.RatingRole) or 0
            if rating != self.rating_filter:
                return False
        if self.type_filter != "all":
            is_raw = bool(index.data(PhotoListModel.IsRawRole))
            if self.type_filter == "jpeg" and is_raw:
                return False
            if self.type_filter == "raw" and not is_raw:
                return False
        if self.kind_filter != "all":
            kind = index.data(PhotoListModel.SourceKindRole)
            if self.kind_filter == "originals" and kind is not None:
                return False
            if self.kind_filter in ("focus", "hdr") and kind != self.kind_filter:
                return False
        if self.search_text:
            name = (index.data(PhotoListModel.NameRole) or "").lower()
            if self.search_text not in name:
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
    """Loads thumbnails for a whole folder on a worker pool, off a priority
    queue rather than a flat FIFO -- with a few thousand+ photos, loading in
    plain scan order means whatever the user actually has on screen is stuck
    behind everything alphabetically/chronologically earlier in the folder.
    `prioritize()` lets the GUI thread bump the photos currently visible (or
    about to be) to the front of the queue as the user opens or scrolls the
    library, so what's on screen finishes first; everything else still loads
    in the background at the same total speed.
    """

    scan_registered = Signal(list)  # list of (photo_dict, is_raw), emitted once
    photo_ready = Signal(int, object)  # photo_id, PIL.Image
    finished_scanning = Signal(int)
    scan_failed = Signal(str)  # error message

    def __init__(self, folder, catalog, recursive: bool = True, thumb_size: int = THUMB_SIZE, max_workers=None):
        super().__init__()
        self.folder = folder
        self.catalog = catalog
        self.recursive = recursive
        self.thumb_size = thumb_size
        self.max_workers = max_workers or min(8, os.cpu_count() or 4)
        self._stop = False
        # Set up before scan_registered is emitted (see run()) so prioritize()
        # is always safe to call as soon as the GUI has photo ids to give it.
        self._path_by_id = {}
        self._work_queue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._claimed_lock = threading.Lock()
        self._claimed = set()
        self._done_count = 0

    def stop(self):
        self._stop = True

    def prioritize(self, photo_ids):
        """Bump these photo ids to the front of the load queue. Safe to call
        from the GUI thread at any point after scan_registered has fired
        (before that, self._path_by_id is empty and this is a no-op)."""
        with self._claimed_lock:
            claimed = self._claimed
        for pid in photo_ids:
            if pid in claimed:
                continue
            path = self._path_by_id.get(pid)
            if path is None:
                continue
            self._work_queue.put((0, next(self._seq), pid, path))

    def _worker(self):
        while not self._stop:
            try:
                _, _, pid, path = self._work_queue.get_nowait()
            except queue.Empty:
                return
            with self._claimed_lock:
                if pid in self._claimed:
                    continue
                self._claimed.add(pid)
            try:
                thumb = thumbnails.get_thumbnail(path, self.thumb_size)
            except Exception as exc:  # noqa: BLE001 - keep going past a bad file
                print(f"[library] failed to load {path}: {exc}")
                continue
            if self._stop:
                return
            self.photo_ready.emit(pid, thumb)
            with self._claimed_lock:
                self._done_count += 1

    def run(self):
        # Scanning the folder and registering everything in the catalog used
        # to be unguarded -- any failure here (e.g. a transient "database is
        # locked" from another process briefly writing to the same
        # catalog.db, confirmed as a real way to trigger this) silently
        # killed the thread before it ever emitted scan_registered, leaving
        # the library stuck showing "Scanning..." forever with no error and
        # no way to recover short of restarting the app. Now it reports the
        # failure instead of just disappearing.
        try:
            paths = list(image_io.scan_folder(self.folder, self.recursive))
            if not paths or self._stop:
                self.finished_scanning.emit(0)
                return
            photos_by_path = self.catalog.get_or_create_photos_bulk([str(p) for p in paths])
        except Exception as exc:  # noqa: BLE001 - see comment above
            print(f"[library] folder scan failed: {exc}")
            self.scan_failed.emit(str(exc))
            return

        registered = []
        path_by_id = {}
        for p in paths:
            photo = photos_by_path.get(str(p.resolve()))
            if photo is None:
                continue
            path_by_id[photo["id"]] = p
            registered.append((photo, image_io.is_raw(p)))

        # Populate the queue (default/low priority) before announcing the
        # scan -- once scan_registered is emitted the GUI thread may call
        # prioritize() at any moment, and it needs self._path_by_id and
        # self._work_queue to already exist and be in their normal state.
        self._path_by_id = path_by_id
        for pid, p in path_by_id.items():
            self._work_queue.put((1, next(self._seq), pid, p))

        self.scan_registered.emit(registered)

        workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.max_workers)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        self.finished_scanning.emit(self._done_count)


class AutoRateThread(QThread):
    """Bulk classical (non-AI) technical-quality pass over every unrated
    photo in the library -- see app/quality.py for the actual sharpness/
    exposure scoring. Runs off the UI thread even though each photo is fast
    (sub-millisecond once its thumbnail is cached), since a library-wide
    pass over thousands of photos still adds up to real wall-clock time and
    the UI would otherwise freeze for the whole thing.

    When both a RAW and a JPEG exist for the same shot (same filename stem
    -- this app's own convention, also relied on by stacking.dedupe_pairs),
    only the RAW gets decoded and scored, and the result is applied to both
    catalog entries. On a library that's ~99% RAW+JPEG pairs, that halves
    the actual decode work."""

    progress = Signal(int, int)  # groups done, groups total
    photo_rated = Signal(int, int)  # photo_id, rating
    finished_rating = Signal(int)  # total photo ids rated (both pair members counted)

    def __init__(self, unrated_photos: list, catalog):
        super().__init__()
        self.unrated_photos = unrated_photos
        self.catalog = catalog
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        groups = {}  # stem -> list of {"id", "path", "is_raw"}
        for photo in self.unrated_photos:
            groups.setdefault(Path(photo["path"]).stem, []).append(photo)

        total = len(groups)
        done = 0
        rated_count = 0
        for members in groups.values():
            if self._stop:
                break
            representative = next((m for m in members if m["is_raw"]), members[0])
            try:
                rating = quality.assess_photo(representative["path"])
            except Exception as exc:  # noqa: BLE001 - keep going past a bad file
                print(f"[auto-rate] failed to assess {representative['path']}: {exc}")
                done += 1
                self.progress.emit(done, total)
                continue
            for member in members:
                self.catalog.set_rating(member["id"], rating)
                self.photo_rated.emit(member["id"], rating)
                rated_count += 1
            done += 1
            self.progress.emit(done, total)

        self.finished_rating.emit(rated_count)


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
        self._auto_rate_thread = None
        self._auto_rate_progress = None

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

        self.auto_rate_btn = QPushButton("Auto-Rate Unrated…")
        self.auto_rate_btn.setToolTip(
            "Give every photo with no star rating yet a rough 1-5 rating from automatic "
            "sharpness/exposure detection (long exposures are exempted from the sharpness "
            "check). Never touches a photo you've already rated by hand."
        )
        self.auto_rate_btn.clicked.connect(self._on_auto_rate_clicked)
        toolbar.addWidget(self.auto_rate_btn)

        self.delete_btn = QPushButton("Delete Selected…")
        self.delete_btn.setToolTip(
            "Select 1+ photos (Ctrl/Shift-click), then remove them from the library and send "
            "the actual files to the Recycle Bin -- not a permanent delete, still recoverable "
            "there afterward."
        )
        self.delete_btn.clicked.connect(self._on_delete_selected)
        toolbar.addWidget(self.delete_btn)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search filename…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMaximumWidth(200)
        # Debounced rather than filtering on every keystroke -- re-evaluating
        # the filter is cheap per keystroke individually, but typing a whole
        # word into a library of thousands of photos would otherwise re-run
        # it once per character for no benefit over running it once when
        # typing pauses.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(lambda: self.proxy.set_search_text(self.search_box.text()))
        self.search_box.textChanged.connect(lambda _: self._search_timer.start())
        toolbar.addWidget(self.search_box)

        toolbar.addWidget(QLabel("Show:"))
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All formats", "JPEG only", "RAW only"])
        self.type_filter.currentIndexChanged.connect(self._on_type_filter_changed)
        toolbar.addWidget(self.type_filter)

        self.kind_filter = QComboBox()
        self.kind_filter.addItems(["All photos", "Originals only", "Focus stacks", "HDR merges"])
        self.kind_filter.setToolTip("Filter to just the merged outputs from Stack Photos, or hide them.")
        self.kind_filter.currentIndexChanged.connect(self._on_kind_filter_changed)
        toolbar.addWidget(self.kind_filter)

        toolbar.addWidget(QLabel("Rating:"))
        self.rating_filter = QComboBox()
        self.rating_filter.addItems(["All", "1", "2", "3", "4", "5"])
        self.rating_filter.setToolTip("Show only photos with exactly this many stars.")
        self.rating_filter.currentIndexChanged.connect(lambda i: self.proxy.set_rating_filter(i))
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

        # Debounced so a fast scroll/drag doesn't re-walk the viewport on
        # every single pixel of movement -- one pass ~150ms after motion
        # settles is plenty responsive and far cheaper.
        self._prioritize_timer = QTimer(self)
        self._prioritize_timer.setSingleShot(True)
        self._prioritize_timer.setInterval(150)
        self._prioritize_timer.timeout.connect(self._prioritize_visible)
        self.list_view.verticalScrollBar().valueChanged.connect(lambda _: self._prioritize_timer.start())

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._install_shortcuts()

        # Deferred (not called directly here) so the window can actually
        # show/paint first instead of the whole app appearing to hang while
        # this folder scan runs. Guarded by _auto_load_pending: confirmed
        # live as a real bug that this singleShot(0) can still be sitting in
        # the event queue by the time the user manually opens a different
        # folder (e.g. clicking Open Folder right at startup) -- without the
        # guard, this callback still fires afterward and silently reverts
        # back to the old folder, discarding the folder the user actually
        # picked. load_folder() clears the flag as soon as ANYTHING (this
        # auto-load included) actually starts a scan, so whichever happens
        # first wins and nothing queued after it can clobber that choice.
        self._auto_load_pending = False
        last_folder = self.settings.value(LAST_LIBRARY_FOLDER, "")
        if last_folder and Path(last_folder).is_dir():
            self._auto_load_pending = True
            QTimer.singleShot(0, lambda: self._auto_load_last_folder(last_folder))

    def _auto_load_last_folder(self, folder: str):
        if self._auto_load_pending:
            self.load_folder(folder)

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
        for sig in (thread.scan_registered, thread.photo_ready, thread.finished_scanning, thread.scan_failed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._retiring_threads.append(thread)
        thread.finished.connect(lambda: self._retiring_threads.remove(thread))

    def load_folder(self, folder: str):
        self._auto_load_pending = False  # whatever triggered this wins -- see __init__'s comment
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
        self.loader_thread.scan_failed.connect(self._on_scan_failed)
        self.loader_thread.start()

    def _on_scan_registered(self, registered: list):
        self.model.add_photos(registered)
        self.status_label.setText(f"{len(registered)} photos, loading thumbnails…")
        # The view needs a layout pass over the newly-added rows before it can
        # report what's actually visible -- that happens on the next event
        # loop iteration, which a 0ms singleShot lands right after.
        QTimer.singleShot(0, self._prioritize_visible)

    def _prioritize_visible(self):
        """Tell the loader thread to load whatever's currently on screen (plus
        a screen's worth of buffer above/below, so scrolling stays ahead of
        the queue) before the rest of the folder."""
        thread = self.loader_thread
        if thread is None or not thread.isRunning():
            return
        viewport_rect = self.list_view.viewport().rect()
        if viewport_rect.isEmpty():
            return
        buffer = viewport_rect.height()
        scan_rect = viewport_rect.adjusted(0, -buffer, 0, buffer)

        step_x = max(1, CELL_W // 2)
        step_y = max(1, CELL_H // 2)
        photo_ids = set()
        y = scan_rect.top()
        while y <= scan_rect.bottom():
            x = scan_rect.left()
            while x <= scan_rect.right():
                index = self.list_view.indexAt(QPoint(x, y))
                if index.isValid():
                    pid = index.data(PhotoListModel.PhotoIdRole)
                    if pid is not None:
                        photo_ids.add(pid)
                x += step_x
            y += step_y

        if photo_ids:
            thread.prioritize(photo_ids)

    def _on_photo_ready(self, photo_id: int, pil_thumb):
        pixmap = widgets.pil_to_qpixmap(pil_thumb)
        self.model.set_thumbnail(photo_id, pixmap)

    def _on_scan_finished(self, count: int):
        self.status_label.setText(f"{count} photos")

    def _on_scan_failed(self, error: str):
        self.status_label.setText("Scan failed -- click Open Folder to retry")
        QMessageBox.warning(
            self,
            "Scan Failed",
            f"Couldn't finish scanning this folder:\n\n{error}\n\n"
            "This is usually transient (e.g. another process briefly using the photo catalog). "
            "Try Open Folder again -- your ratings and edits are untouched either way.",
        )

    # ----------------------------------------------------------- deleting

    def _on_delete_selected(self):
        photos = self._selected_photos()
        if not photos:
            QMessageBox.information(self, "Delete Selected", "Select 1+ photos to delete (Ctrl/Shift-click).")
            return

        choice = QMessageBox.question(
            self,
            "Delete Selected Photos",
            f"Remove {len(photos)} photo{'s' if len(photos) != 1 else ''} from the library and send "
            f"the file{'s' if len(photos) != 1 else ''} to the Recycle Bin?\n\n"
            "This is not a permanent delete -- the file(s) can still be restored from the Recycle "
            "Bin afterward, the same as deleting them in Explorer. Their rating, flag, and edit "
            "history in this app's catalog will be removed either way.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        deleted_ids = []
        failed = []
        for photo in photos:
            try:
                send2trash.send2trash(photo["path"])
                deleted_ids.append(photo["id"])
            except Exception as exc:  # noqa: BLE001 - keep going past a locked/missing file
                failed.append((photo["path"], str(exc)))

        if deleted_ids:
            self.catalog.delete_photos(deleted_ids)

        message = f"Deleted {len(deleted_ids)} photo{'s' if len(deleted_ids) != 1 else ''}."
        if failed:
            message += f"\n\n{len(failed)} failed:\n" + "\n".join(f"  {Path(p).name}: {err}" for p, err in failed[:8])
        QMessageBox.information(self, "Delete Selected", message)

        if deleted_ids and self.current_folder:
            self.load_folder(self.current_folder)  # picks up the removal, non-blocking (see _retire_loader_thread)

    # ---------------------------------------------------------- stacking

    def _selected_paths(self) -> list:
        indexes = self.list_view.selectionModel().selectedIndexes()
        return [Path(idx.data(PhotoListModel.PathRole)) for idx in indexes]

    def _selected_photos(self) -> list:
        """[{"id", "path"}, ...] for the current selection."""
        indexes = self.list_view.selectionModel().selectedIndexes()
        return [{"id": idx.data(PhotoListModel.PhotoIdRole), "path": idx.data(PhotoListModel.PathRole)} for idx in indexes]

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

    # -------------------------------------------------------- auto-rating

    def _on_auto_rate_clicked(self):
        unrated = self.model.unrated_photos()
        if not unrated:
            QMessageBox.information(self, "Auto-Rate Unrated", "Every photo in this folder already has a rating.")
            return

        choice = QMessageBox.question(
            self,
            "Auto-Rate Unrated Photos",
            f"Give {len(unrated)} unrated photo{'s' if len(unrated) != 1 else ''} a rough 1-5 star rating based "
            "on automatic sharpness and exposure detection?\n\n"
            "This only ever fills in photos with no rating yet -- it never changes a rating "
            "you've set by hand. You can always re-rate anything afterward.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        stems = {Path(p["path"]).stem for p in unrated}
        self._auto_rate_progress = QProgressDialog("Rating photos…", "Cancel", 0, len(stems), self)
        self._auto_rate_progress.setWindowTitle("Auto-Rate Unrated")
        self._auto_rate_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._auto_rate_progress.setMinimumDuration(0)
        self._auto_rate_progress.setValue(0)

        self._auto_rate_thread = AutoRateThread(unrated, self.catalog)
        self._auto_rate_thread.progress.connect(self._on_auto_rate_progress)
        self._auto_rate_thread.photo_rated.connect(self._on_photo_rated)
        self._auto_rate_thread.finished_rating.connect(self._on_auto_rate_finished)
        self._auto_rate_progress.canceled.connect(self._auto_rate_thread.stop)
        self._auto_rate_thread.start()

    def _on_auto_rate_progress(self, done: int, total: int):
        if self._auto_rate_progress is not None:
            self._auto_rate_progress.setValue(done)

    def _on_photo_rated(self, photo_id: int, rating: int):
        self.model.set_rating_by_id(photo_id, rating)

    def _on_auto_rate_finished(self, rated_count: int):
        if self._auto_rate_progress is not None:
            self._auto_rate_progress.close()
            self._auto_rate_progress = None
        QMessageBox.information(
            self,
            "Auto-Rate Unrated",
            f"Rated {rated_count} photo{'s' if rated_count != 1 else ''}. These are rough, automatic "
            "ratings -- review and adjust any that look off.",
        )

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

    def _on_kind_filter_changed(self, idx: int):
        self.proxy.set_kind_filter({0: "all", 1: "originals", 2: "focus", 3: "hdr"}.get(idx, "all"))

    def _on_sort_changed(self, idx: int):
        self.proxy.set_sort_mode({0: "name", 1: "date", 2: "rating"}.get(idx, "name"))
