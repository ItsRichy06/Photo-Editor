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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import send2trash

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QItemSelection,
    QModelIndex,
    QPersistentModelIndex,
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
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainterPath, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .. import image_io, organize, quality, thumbnails
from ..settings import LAST_LIBRARY_FOLDER, get_settings
from . import stacking_dialogs, theme, widgets
from .folder_sidebar import FolderSidebar, _show_in_explorer

THUMB_SIZE = 320
# Grid cells are sized to fill the view's width (LibraryView._update_cell_size):
# as many columns as fit at roughly CELL_TARGET_W, then stretched to fill.
CELL_TARGET_W = 210
CELL_W = 210
CELL_H = 236
CARD_GAP = 5          # space around each card inside its grid cell
CARD_PAD = 8          # card edge to thumbnail
INFO_H = 46           # filename + stars below the thumbnail
THUMB_ASPECT = 0.84   # thumbnail area height / width: between 3:2 landscape and portrait, so neither looks lost
STAR_SIZE = 14


class PhotoListModel(QAbstractListModel):
    RatingRole = Qt.ItemDataRole.UserRole + 1
    FlagRole = Qt.ItemDataRole.UserRole + 2
    PathRole = Qt.ItemDataRole.UserRole + 3
    IsRawRole = Qt.ItemDataRole.UserRole + 4
    MtimeRole = Qt.ItemDataRole.UserRole + 5
    PhotoIdRole = Qt.ItemDataRole.UserRole + 6
    NameRole = Qt.ItemDataRole.UserRole + 7
    SourceKindRole = Qt.ItemDataRole.UserRole + 8  # None | "focus" | "hdr" -- see Catalog.set_source_kind
    RatingSourceRole = Qt.ItemDataRole.UserRole + 9  # None | "manual" | "auto" -- see Catalog.set_rating
    PixelsRole = Qt.ItemDataRole.UserRole + 10  # width*height, or None if unknown

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
        if role == self.RatingSourceRole:
            return item["rating_source"]
        if role == self.PixelsRole:
            return item["pixels"]
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid():
            return False
        item = self._items[index.row()]
        if role == self.RatingRole:
            item["rating"] = value
        elif role == self.FlagRole:
            item["flag"] = value
        elif role == self.RatingSourceRole:
            item["rating_source"] = value
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
                    "rating_source": photo.get("rating_source"),
                    "pixels": (photo.get("width") or 0) * (photo.get("height") or 0) or None,
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

    def set_rating_by_id(self, photo_id, rating: int, source: str | None = None):
        """Like setData(..., RatingRole) but addressed by photo id -- for
        callers (e.g. a background auto-rate pass) that only have the id,
        not a QModelIndex into the current (possibly filtered/sorted) view."""
        row = self._id_to_row.get(photo_id)
        if row is None:
            return
        self._items[row]["rating"] = rating
        self._items[row]["rating_source"] = source if rating > 0 else None
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [self.RatingRole, self.RatingSourceRole])

    def set_flag_by_id(self, photo_id, flag: str):
        row = self._id_to_row.get(photo_id)
        if row is None:
            return
        self._items[row]["flag"] = flag
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [self.FlagRole])

    def remove_photo(self, photo_id):
        """Surgically drop one photo (e.g. after the viewer deletes it) --
        used instead of a full load_folder() rescan so a delete from the
        viewer doesn't reset scroll position/filmstrip state for no reason;
        the library grid updates too since it shares this same model."""
        row = self._id_to_row.get(photo_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._items[row]
        del self._id_to_row[photo_id]
        self._thumbnails.pop(photo_id, None)
        for pid, r in self._id_to_row.items():
            if r > row:
                self._id_to_row[pid] = r - 1
        self.endRemoveRows()

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
        self.rating_source_filter = "all"  # "all" | "manual" | "auto"
        self.type_filter = "all"
        self.kind_filter = "all"  # "all" | "focus" | "hdr" | "originals"
        self.search_text = ""
        self.sort_mode = "name"
        self.sort_descending = False
        self.setDynamicSortFilter(True)

    def set_rating_filter(self, rating: int):
        """rating: 0 shows everything; 1-5 shows only photos with exactly
        that many stars (not "at least" -- exact, so you can isolate e.g.
        just the 2-stars still waiting on a second look)."""
        self.rating_filter = rating
        self.invalidateFilter()

    def set_rating_source_filter(self, source: str):
        """source: "all", "manual" (only your own ratings), or "auto" (only
        ratings Auto-Rate Unrated filled in) -- lets the two be told apart
        even though they share the same 1-5 star scale."""
        self.rating_source_filter = source
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

    # Each sort starts in the direction people usually want it.
    DEFAULT_DESCENDING = {"name": False, "date": True, "rating": True, "resolution": True}
    DIRECTION_LABELS = {
        "name": ("A → Z", "Z → A"),
        "date": ("Oldest first", "Newest first"),
        "rating": ("Lowest first", "Highest first"),
        "resolution": ("Smallest first", "Largest first"),
    }

    def set_sort_mode(self, mode: str):
        self.sort_mode = mode
        self.sort_descending = self.DEFAULT_DESCENDING.get(mode, False)
        self._resort()

    def set_sort_descending(self, descending: bool):
        self.sort_descending = descending
        self._resort()

    def direction_label(self) -> str:
        asc, desc = self.DIRECTION_LABELS.get(self.sort_mode, ("Ascending", "Descending"))
        return desc if self.sort_descending else asc

    def _resort(self):
        self.invalidate()
        self.sort(0)

    def filterAcceptsRow(self, source_row, source_parent):
        index = self.sourceModel().index(source_row, 0, source_parent)
        if self.rating_filter > 0:
            rating = index.data(PhotoListModel.RatingRole) or 0
            if rating != self.rating_filter:
                return False
        if self.rating_source_filter != "all":
            if index.data(PhotoListModel.RatingSourceRole) != self.rating_source_filter:
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

    _SORT_ROLES = {"date": PhotoListModel.MtimeRole, "rating": PhotoListModel.RatingRole,
                   "resolution": PhotoListModel.PixelsRole}

    def lessThan(self, left, right):
        """Always called with Qt's ascending order; the chosen direction is
        applied here instead, so ties can fall back to filename A-Z in both
        directions (Qt's own descending order would reverse those too)."""
        lname = (left.data(PhotoListModel.NameRole) or "").lower()
        rname = (right.data(PhotoListModel.NameRole) or "").lower()
        role = self._SORT_ROLES.get(self.sort_mode)
        if role is None:  # name
            return lname > rname if self.sort_descending else lname < rname
        lk, rk = left.data(role), right.data(role)
        if self.sort_mode == "rating":
            lk, rk = lk or 0, rk or 0
        if lk != rk:
            # Unknown values (e.g. a photo whose dimensions couldn't be
            # read) go last whichever way the sort runs.
            if lk is None or rk is None:
                return rk is None
            return lk > rk if self.sort_descending else lk < rk
        return lname < rname


class PhotoDelegate(QStyledItemDelegate):
    """Paints one grid cell as a card: the thumbnail fit into a 3:2 area,
    filename, star rating, and pick/reject state. Cell size is set from
    outside (LibraryView._update_cell_size) so the grid always fills the
    view's width."""

    ratingEdited = Signal(int, int)  # photo_id, rating

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cell = QSize(CELL_W, CELL_H)

    def sizeHint(self, option, index):
        return self.cell

    # Geometry shared by paint() and editorEvent(), so the clickable stars
    # are always exactly where they're drawn.
    @staticmethod
    def card_rect(rect: QRect) -> QRect:
        return rect.adjusted(CARD_GAP, CARD_GAP, -CARD_GAP, -CARD_GAP)

    @staticmethod
    def thumb_rect(card: QRect) -> QRect:
        return QRect(card.x() + CARD_PAD, card.y() + CARD_PAD, card.width() - 2 * CARD_PAD,
                     card.height() - 2 * CARD_PAD - INFO_H)

    @staticmethod
    def stars_origin(card: QRect) -> QPoint:
        return QPoint(card.x() + CARD_PAD, card.bottom() - CARD_PAD - STAR_SIZE + 1)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        card = self.card_rect(option.rect)

        bg = QColor("#173a48") if selected else QColor(theme.SURFACE if hovered else "#1b1b1b")
        painter.setPen(QColor(theme.ACCENT) if selected else Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(card.adjusted(0, 0, -1, -1), 8, 8)

        area = self.thumb_rect(card)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        flag = index.data(PhotoListModel.FlagRole)
        if pixmap:
            scaled = pixmap.scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            target = QRect(area.x() + (area.width() - scaled.width()) // 2,
                           area.y() + (area.height() - scaled.height()) // 2, scaled.width(), scaled.height())
            clip = QPainterPath()
            clip.addRoundedRect(target, 4, 4)
            painter.save()
            painter.setClipPath(clip)
            if flag == "reject":
                painter.setOpacity(0.35)
            painter.drawPixmap(target, scaled)
            painter.restore()
        else:
            target = area
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#222222"))
            painter.drawRoundedRect(area, 4, 4)
            painter.setPen(QColor(theme.FAINT))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Loading…")

        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() - 2))
        small.setWeight(QFont.Weight.DemiBold)
        if index.data(PhotoListModel.IsRawRole) and pixmap:
            painter.setFont(small)
            badge = QRect(target.x() + 6, target.y() + 6, painter.fontMetrics().horizontalAdvance("RAW") + 10, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#e0e0e0"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "RAW")

        painter.setFont(option.font)
        name = index.data(PhotoListModel.NameRole) or ""
        name_rect = QRect(card.x() + CARD_PAD, area.bottom() + 8, card.width() - 2 * CARD_PAD, 18)
        painter.setPen(QColor("#ffffff" if selected else theme.TEXT))
        elided = painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, name_rect.width())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        rating = index.data(PhotoListModel.RatingRole) or 0
        origin = self.stars_origin(card)
        for i in range(5):
            filled = i < rating
            color = QColor(theme.STAR) if filled else QColor("#555555")
            painter.setPen(color)
            painter.setBrush(color if filled else QColor(0, 0, 0, 0))
            widgets.StarRatingWidget._draw_star(
                painter, origin.x() + i * (STAR_SIZE + 2) + STAR_SIZE / 2, origin.y() + STAR_SIZE / 2, STAR_SIZE * 0.42
            )

        if flag in ("pick", "reject"):
            painter.setFont(small)
            text, color = ("Pick", "#5cc97b") if flag == "pick" else ("Reject", theme.DANGER)
            painter.setPen(QColor(color))
            flag_rect = QRect(card.x() + CARD_PAD, origin.y() - 1, card.width() - 2 * CARD_PAD, STAR_SIZE + 2)
            painter.drawText(flag_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            origin = self.stars_origin(self.card_rect(option.rect))
            rel_x = pos.x() - origin.x()
            rel_y = pos.y() - origin.y()
            if 0 <= rel_y <= STAR_SIZE and 0 <= rel_x < (STAR_SIZE + 2) * 5:
                star_idx = int(rel_x // (STAR_SIZE + 2)) + 1
                photo_id = index.data(PhotoListModel.PhotoIdRole)
                current = index.data(PhotoListModel.RatingRole) or 0
                new_rating = 0 if star_idx == current else star_idx
                model.setData(index, new_rating, PhotoListModel.RatingRole)
                model.setData(index, "manual" if new_rating > 0 else None, PhotoListModel.RatingSourceRole)
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

    def _fill_missing_dimensions(self, registered, path_by_id):
        """Reads pixel dimensions (header only, no decode) for photos the
        catalog doesn't have them for yet, and saves them, so sorting by
        resolution is correct as soon as the grid appears instead of
        reshuffling while thumbnails load. One-time per photo: measured
        ~0.4ms per JPEG and ~6ms per ARW on one thread, ~3s for a whole
        13.5k-photo RAW+JPEG folder across 8 threads; later opens skip it.
        Best-effort -- a photo that can't be read just sorts last."""
        missing = [photo for photo, _ in registered if not photo.get("width") or not photo.get("height")]
        if not missing:
            return

        def read(photo):
            if self._stop:
                return photo, None
            try:
                return photo, image_io.get_dimensions(path_by_id[photo["id"]])
            except Exception:  # noqa: BLE001
                return photo, None

        updates = []
        with ThreadPoolExecutor(self.max_workers) as pool:
            for photo, dims in pool.map(read, missing):
                if dims:
                    photo["width"], photo["height"] = dims
                    updates.append((photo["id"], dims[0], dims[1]))
        try:
            self.catalog.set_dimensions_bulk(updates)
        except Exception as exc:  # noqa: BLE001 -- only costs a re-read next time
            print(f"[library] couldn't save photo dimensions: {exc}")

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

        self._fill_missing_dimensions(registered, path_by_id)
        if self._stop:
            self.finished_scanning.emit(0)
            return

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
    # rated photo ids (both pair members counted), photos that couldn't be
    # read, how many of those no longer exist on disk, and one example error
    finished_rating = Signal(int, int, int, str)

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
        failed = 0
        missing = 0
        example_error = ""
        for members in groups.values():
            if self._stop:
                break
            representative = next((m for m in members if m["is_raw"]), members[0])
            try:
                rating = quality.assess_photo(representative["path"])
            except Exception as exc:  # noqa: BLE001 - keep going past a bad file
                # Counted and reported, not just printed: the app runs under
                # pythonw with no console, so a pass where every photo failed
                # (confirmed: the folder had been renamed after it was opened)
                # used to end in a bare "Rated 0 photos" with no reason given.
                print(f"[auto-rate] failed to assess {representative['path']}: {exc}")
                failed += len(members)
                missing += sum(1 for m in members if not os.path.exists(m["path"]))
                if not example_error:
                    example_error = f"{Path(representative['path']).name}: {exc}"
                done += 1
                self.progress.emit(done, total)
                continue
            for member in members:
                self.catalog.set_rating(member["id"], rating, source="auto")
                self.photo_rated.emit(member["id"], rating)
                rated_count += 1
            done += 1
            self.progress.emit(done, total)

        self.finished_rating.emit(rated_count, failed, missing, example_error)


class PhotoGridView(QListView):
    """A QListView whose range selection runs in reading order.

    Qt's icon mode selects a range as a rectangle: Shift-click (or a drag
    from a photo) selects whatever intersects the box between the two
    points, so across rows you'd get a block of columns rather than every
    photo in between -- and within one row the two happen to agree, which
    is why it looked right only some of the time. Here a range is every row
    of the (filtered, sorted) list from the anchor to the target. Dragging
    from empty space keeps the rubber-band rectangle, the usual explicit
    gesture for one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._anchor = QPersistentModelIndex()
        self._pointer = None  # position of the mouse event driving the selection, or None for keyboard

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._anchor = QPersistentModelIndex(index)  # invalid when pressing on empty space
        self._pointer = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self._pointer = event.position().toPoint()
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        self._pointer = None
        super().keyPressEvent(event)
        if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._anchor = QPersistentModelIndex(self.currentIndex())

    def setSelection(self, rect, command):
        end = self.indexAt(self._pointer) if self._pointer is not None else self.currentIndex()
        anchor = QModelIndex(self._anchor) if self._anchor.isValid() else None
        if anchor is None or not anchor.isValid():
            return super().setSelection(rect, command)  # rubber band from empty space
        if not end.isValid():
            return  # dragged over a gap between cards: keep the current range
        lo, hi = sorted((anchor.row(), end.row()))
        model = self.model()
        self.selectionModel().select(QItemSelection(model.index(lo, 0), model.index(hi, 0)), command)


def _filter_combo(items, tooltip, min_width=96) -> QComboBox:
    combo = QComboBox()
    combo.addItems(items)
    combo.setToolTip(tooltip)
    # An explicit minimum lets the layout squeeze the filter row on a narrow
    # window (a 1280/1366px laptop screen) instead of the dropdowns' full text
    # widths forcing the whole window to stay ~1300px wide; at normal widths
    # each still gets its natural size.
    combo.setMinimumWidth(min_width)
    return combo


class LibraryView(QWidget):
    openPhotoRequested = Signal(int, str)  # the large viewer (double-click)
    editPhotoRequested = Signal(int, str)  # straight to the editor (E, or the right-click menu)

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

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = FolderSidebar(self.settings, catalog)
        self.sidebar.folderSelected.connect(self.load_folder)
        root.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        root.addLayout(right, stretch=1)

        header = QWidget()
        header.setObjectName("libraryHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header.setStyleSheet(f"#libraryHeader {{ background: {theme.PANEL}; border-bottom: 1px solid {theme.BORDER}; }}")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 16, 12)
        header_layout.setSpacing(10)
        right.addWidget(header)

        # Row 1: where you are, and what you can do with it.
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_label = QLabel("Library")
        self.title_label.setProperty("role", "title")
        title_row.addWidget(self.title_label)
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        title_row.addWidget(self.status_label)
        title_row.addStretch(1)

        def action_button(text, tip, slot, variant="ghost"):
            btn = QPushButton(text)
            btn.setProperty("variant", variant)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            title_row.addWidget(btn)
            return btn

        # Shown only while exactly one photo is selected (see
        # _update_selection_actions); it sits leftmost so the buttons after it
        # don't shift when it appears.
        self.edit_btn = action_button("Edit", "Open the selected photo in the editor (E)", self._edit_current, "primary")
        self.edit_btn.setVisible(False)
        self.group_btn = action_button(
            "Group into Folder",
            "Move the selected photos into a new folder inside this one (Ctrl+G). RAW+JPEG pairs move together.",
            self._on_group_into_folder,
        )
        self.stack_btn = action_button(
            "Stack Photos",
            "Select 2+ photos, then auto-detect and merge HDR exposure brackets and focus stacks among them.",
            self._on_stack_photos,
        )
        self.auto_rate_btn = action_button(
            "Auto-Rate",
            "Give every photo with no star rating yet a rough 1-5 rating from automatic sharpness/exposure "
            "detection. Never touches a photo you've already rated by hand.",
            self._on_auto_rate_clicked,
        )
        self.delete_btn = action_button(
            "Delete",
            "Send the selected photos to the Recycle Bin (Del) -- recoverable there afterward.",
            self._on_delete_selected,
            "danger",
        )
        header_layout.addLayout(title_row)

        # Row 2: narrowing down what's shown.
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search filenames")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.addAction(theme.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search_box.setMinimumWidth(130)
        self.search_box.setMaximumWidth(260)
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
        filter_row.addWidget(self.search_box)

        self.type_filter = _filter_combo(["All formats", "JPEG only", "RAW only"], "Show JPEG, RAW, or both.")
        self.type_filter.currentIndexChanged.connect(self._on_type_filter_changed)
        filter_row.addWidget(self.type_filter)
        self.kind_filter = _filter_combo(
            ["All photos", "Originals only", "Focus stacks", "HDR merges"],
            "Filter to just the merged outputs from Stack Photos, or hide them.",
        )
        self.kind_filter.currentIndexChanged.connect(self._on_kind_filter_changed)
        filter_row.addWidget(self.kind_filter)
        self.rating_filter = _filter_combo(
            ["Any rating", "1 star", "2 stars", "3 stars", "4 stars", "5 stars"],
            "Show only photos with exactly this many stars.",
        )
        self.rating_filter.currentIndexChanged.connect(lambda i: self.proxy.set_rating_filter(i))
        filter_row.addWidget(self.rating_filter)
        self.rating_source_filter = _filter_combo(
            ["Rated by anyone", "My ratings", "Auto-rated"],
            "Show only photos you rated yourself, or only ones Auto-Rate filled in.",
        )
        self.rating_source_filter.currentIndexChanged.connect(self._on_rating_source_filter_changed)
        filter_row.addWidget(self.rating_source_filter)
        filter_row.addStretch(1)
        self.sort_combo = _filter_combo(
            ["Sort: Name", "Sort: Date modified", "Sort: Rating", "Sort: Resolution"],
            "Resolution sorts by megapixels, largest first.",
            min_width=120,
        )
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        filter_row.addWidget(self.sort_combo)
        self.sort_dir_btn = QPushButton()
        self.sort_dir_btn.setToolTip("Flip the sort direction")
        self.sort_dir_btn.setMinimumWidth(96)
        self.sort_dir_btn.clicked.connect(
            lambda: (self.proxy.set_sort_descending(not self.proxy.sort_descending), self._update_sort_button())
        )
        filter_row.addWidget(self.sort_dir_btn)
        self._update_sort_button()
        header_layout.addLayout(filter_row)

        self.list_view = PhotoGridView()
        self.list_view.setModel(self.proxy)
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSpacing(0)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.verticalScrollBar().setSingleStep(40)
        self.list_view.setContentsMargins(0, 0, 0, 0)
        self.list_view.setViewportMargins(10, 8, 6, 8)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._on_grid_context_menu)
        self.delegate = PhotoDelegate(self.list_view)
        self.delegate.ratingEdited.connect(self._on_rating_edited)
        self.list_view.setItemDelegate(self.delegate)
        self.list_view.doubleClicked.connect(self._on_double_clicked)
        self.list_view.viewport().installEventFilter(self)
        self.list_view.selectionModel().selectionChanged.connect(lambda *_: self._update_selection_actions())

        for sig in (self.proxy.rowsInserted, self.proxy.rowsRemoved, self.proxy.modelReset, self.proxy.layoutChanged):
            sig.connect(lambda *_: self._update_status())

        self.content = QStackedWidget()
        self.content.addWidget(self._build_empty_state())
        self.content.addWidget(self.list_view)
        right.addWidget(self.content, stretch=1)
        self._update_selection_actions()

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
        # folder (e.g. clicking another folder right at startup) -- without the
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
        def bind(keys, slot):
            sc = QShortcut(QKeySequence(keys), self.list_view)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(slot)

        for i in range(6):
            bind(str(i), lambda rating=i: self._rate_selected(rating))
        bind("P", lambda: self._flag_selected("pick"))
        bind("X", lambda: self._flag_selected("reject"))
        bind("U", lambda: self._flag_selected("none"))
        bind("E", self._edit_current)
        bind("Return", self._view_current)
        bind("Delete", self._on_delete_selected)
        bind("Ctrl+G", self._on_group_into_folder)

    def _build_empty_state(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        title = QLabel("No folder open")
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        hint = QLabel("Pick a folder in the sidebar, or add one to your library.")
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        add = QPushButton("Add Folder")
        add.setProperty("variant", "primary")
        add.clicked.connect(self.choose_folder)
        btn_row.addWidget(add)
        btn_row.addStretch(1)
        layout.addSpacing(10)
        layout.addLayout(btn_row)
        layout.addStretch(2)
        return page

    # ------------------------------------------------------------ grid sizing

    def eventFilter(self, obj, event):
        if obj is self.list_view.viewport() and event.type() == QEvent.Type.Resize:
            self._update_cell_size()
        return super().eventFilter(obj, event)

    def _update_cell_size(self):
        """As many columns as fit at about CELL_TARGET_W, stretched so the
        grid exactly fills the width instead of leaving a dead strip."""
        # The width QListView itself wraps rows at: its maximum viewport width
        # minus a scrollbar's extent, whether or not the scrollbar is showing
        # right now (QListViewPrivate::prepareItemsLayout). Sizing cells from
        # the visible viewport width instead looked right at some window sizes
        # and silently dropped a column at others, leaving a dead strip.
        width = self.list_view.maximumViewportSize().width() - self.list_view.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent, None, self.list_view.verticalScrollBar())
        if width <= 0:
            return
        cols = max(1, width // CELL_TARGET_W)
        cell_w = (width - 1) // cols
        thumb_w = cell_w - 2 * (CARD_GAP + CARD_PAD)
        cell_h = int(thumb_w * THUMB_ASPECT) + 2 * (CARD_GAP + CARD_PAD) + INFO_H
        size = QSize(cell_w, cell_h)
        if size != self.delegate.cell:
            self.delegate.cell = size
            self.list_view.setGridSize(size)

    # -------------------------------------------------------------- selection

    def _selected_indexes(self):
        sel = self.list_view.selectionModel()
        return sel.selectedIndexes() if sel is not None else []

    def _current_index(self):
        indexes = self._selected_indexes()
        return indexes[0] if indexes else None

    def _update_selection_actions(self):
        n = len(self._selected_indexes())
        self.edit_btn.setVisible(n == 1)
        self.group_btn.setEnabled(n > 0)
        self.delete_btn.setEnabled(n > 0)
        self.stack_btn.setEnabled(n > 1)
        self._update_status()

    def _update_status(self):
        if self.current_folder is None:
            self.status_label.setText("")
            return
        if getattr(self, "_scanning", False):
            self.status_label.setText("Scanning…")
            return
        total = self.proxy.rowCount()
        text = f"{total:,} photo{'s' if total != 1 else ''}"
        n = len(self._selected_indexes())
        if n:
            text += f"  ·  {n:,} selected"
        self.status_label.setText(text)

    def _rate_selected(self, rating: int):
        # By photo id, not by index: under a rating sort, each change can
        # move rows and invalidate the remaining indexes mid-loop.
        ids = [i.data(PhotoListModel.PhotoIdRole) for i in self._selected_indexes()]
        for pid in ids:
            self.catalog.set_rating(pid, rating, source="manual")
            self.model.set_rating_by_id(pid, rating, source="manual")

    def _flag_selected(self, flag: str):
        ids = [i.data(PhotoListModel.PhotoIdRole) for i in self._selected_indexes()]
        for pid in ids:
            self.catalog.set_flag(pid, flag)
            self.model.set_flag_by_id(pid, flag)

    def _view_current(self):
        index = self._current_index()
        if index is not None:
            self._on_double_clicked(index)

    def _edit_current(self):
        index = self._current_index()
        if index is not None:
            self.editPhotoRequested.emit(index.data(PhotoListModel.PhotoIdRole), index.data(PhotoListModel.PathRole))

    def _on_grid_context_menu(self, pos):
        index = self.list_view.indexAt(pos)
        if index.isValid() and not self.list_view.selectionModel().isSelected(index):
            self.list_view.setCurrentIndex(index)  # right-clicking an unselected photo selects just it
        n = len(self._selected_indexes())
        if n == 0:
            return
        menu = QMenu(self)

        def add(text, slot, shortcut=None, enabled=True):
            act = QAction(text, menu)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.setEnabled(enabled)
            act.triggered.connect(slot)
            menu.addAction(act)
            return act

        add("View Large", self._view_current, "Return", enabled=n == 1)
        add("Edit", self._edit_current, "E", enabled=n == 1)
        menu.addSeparator()
        rate = menu.addMenu("Rating")
        for stars in range(6):
            act = QAction("No stars" if stars == 0 else "★" * stars, rate)
            act.setShortcut(QKeySequence(str(stars)))
            act.triggered.connect(lambda _=False, s=stars: self._rate_selected(s))
            rate.addAction(act)
        add("Pick", lambda: self._flag_selected("pick"), "P")
        add("Reject", lambda: self._flag_selected("reject"), "X")
        add("Clear Flag", lambda: self._flag_selected("none"), "U")
        menu.addSeparator()
        add("Group into Folder", self._on_group_into_folder, "Ctrl+G")
        add("Stack Photos", self._on_stack_photos, enabled=n > 1)
        if n == 1:
            add("Show in Explorer", lambda: _show_in_explorer(index.data(PhotoListModel.PathRole)))
        menu.addSeparator()
        add(f"Delete {n} Photo{'s' if n != 1 else ''}", self._on_delete_selected, "Del")
        menu.exec(self.list_view.viewport().mapToGlobal(pos))

    def choose_folder(self):
        self.sidebar.choose_and_add()

    # ------------------------------------------------------- group into folder

    def _on_group_into_folder(self):
        photos = self._selected_photos()
        if not photos or not self.current_folder:
            return
        parent = Path(self.current_folder)
        n = len(photos)
        name = "New folder"
        while True:
            name, ok = QInputDialog.getText(
                self,
                "Group into Folder",
                f"Move {n} selected photo{'s' if n != 1 else ''} into a new folder inside “{parent.name}”.\n"
                "RAW+JPEG pairs move together; ratings and edits come along.\n\nFolder name:",
                text=name,
            )
            if not ok:
                return
            problem = organize.folder_name_problem(name)
            if problem:
                QMessageBox.warning(self, "Group into Folder", problem)
                continue
            dest = parent / name
            if dest.exists() and not dest.is_dir():
                QMessageBox.warning(self, "Group into Folder", f"There's already a file named “{name}” here.")
                continue
            if dest.is_dir():
                choice = QMessageBox.question(
                    self, "Group into Folder",
                    f"“{name}” already exists. Move the selected photos into it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if choice != QMessageBox.StandardButton.Yes:
                    continue
            break

        result = organize.move_into_folder([p["path"] for p in photos], dest, self.catalog)
        if result.skipped:
            lines = "\n".join(f"  {Path(p).name}: {why}" for p, why in result.skipped[:8])
            more = f"\n  …and {len(result.skipped) - 8} more" if len(result.skipped) > 8 else ""
            QMessageBox.warning(
                self, "Group into Folder",
                f"Moved {result.photo_count} file{'s' if result.photo_count != 1 else ''} into “{name}”. "
                f"{len(result.skipped)} couldn't be moved and were left where they were:\n{lines}{more}",
            )
        self.sidebar.refresh(str(parent))
        self.load_folder(str(parent))

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
        folder = str(Path(folder))
        self.current_folder = folder
        self.settings.setValue(LAST_LIBRARY_FOLDER, folder)
        self._scanning = True
        self.title_label.setText(Path(folder).name or folder)
        self.title_label.setToolTip(folder)
        self.content.setCurrentWidget(self.list_view)
        if self.sidebar._find_item(folder) is None:
            self.sidebar.add_folder(folder)
        self.sidebar.select_folder(folder)
        self._update_status()

        self.loader_thread = ThumbnailLoaderThread(folder, self.catalog)
        self.loader_thread.scan_registered.connect(self._on_scan_registered)
        self.loader_thread.photo_ready.connect(self._on_photo_ready)
        self.loader_thread.finished_scanning.connect(self._on_scan_finished)
        self.loader_thread.scan_failed.connect(self._on_scan_failed)
        self.loader_thread.start()

    def _on_scan_registered(self, registered: list):
        self.model.add_photos(registered)
        self._scanning = False
        self._update_status()
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

        step_x = max(1, self.delegate.cell.width() // 2)
        step_y = max(1, self.delegate.cell.height() // 2)
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
        self._scanning = False
        self._update_status()

    def _on_scan_failed(self, error: str):
        self._scanning = False
        self.status_label.setText("Scan failed -- click the folder to retry")
        QMessageBox.warning(
            self,
            "Scan Failed",
            f"Couldn't finish scanning this folder:\n\n{error}\n\n"
            "This is usually transient (e.g. another process briefly using the photo catalog). "
            "Click the folder in the sidebar to try again -- your ratings and edits are untouched either way.\n\n"
            f"Details were saved to {self.catalog.db_path.parent / 'logs' / 'catalog.log'}",
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
        self.model.set_rating_by_id(photo_id, rating, source="auto")

    def _on_auto_rate_finished(self, rated_count: int, failed: int = 0, missing: int = 0, example_error: str = ""):
        if self._auto_rate_progress is not None:
            self._auto_rate_progress.close()
            self._auto_rate_progress = None
        message = f"Rated {rated_count} photo{'s' if rated_count != 1 else ''}."
        if rated_count:
            message += " These are rough, automatic ratings -- review and adjust any that look off."
        if failed:
            message += f"\n\n{failed} photo{'s' if failed != 1 else ''} couldn't be read"
            if missing == failed:
                message += (
                    " -- the files are no longer where the library last saw them. If the folder was "
                    "renamed or moved after opening it, click it in the sidebar to reload it and try again."
                )
            else:
                message += f", for example:\n{example_error}"
            QMessageBox.warning(self, "Auto-Rate Unrated", message)
            return
        QMessageBox.information(self, "Auto-Rate Unrated", message)

    def _on_double_clicked(self, index):
        if not index.isValid():
            return
        photo_id = index.data(PhotoListModel.PhotoIdRole)
        path = index.data(PhotoListModel.PathRole)
        self.openPhotoRequested.emit(photo_id, path)

    def _on_rating_edited(self, photo_id: int, rating: int):
        self.catalog.set_rating(photo_id, rating, source="manual")

    def _on_type_filter_changed(self, idx: int):
        self.proxy.set_type_filter({0: "all", 1: "jpeg", 2: "raw"}.get(idx, "all"))

    def _on_kind_filter_changed(self, idx: int):
        self.proxy.set_kind_filter({0: "all", 1: "originals", 2: "focus", 3: "hdr"}.get(idx, "all"))

    def _on_rating_source_filter_changed(self, idx: int):
        self.proxy.set_rating_source_filter({0: "all", 1: "manual", 2: "auto"}.get(idx, "all"))

    def _on_sort_changed(self, idx: int):
        self.proxy.set_sort_mode({0: "name", 1: "date", 2: "rating", 3: "resolution"}.get(idx, "name"))
        self._update_sort_button()

    def _update_sort_button(self):
        self.sort_dir_btn.setText(self.proxy.direction_label())
        self.sort_dir_btn.setIcon(theme.icon("arrow_down" if self.proxy.sort_descending else "arrow_up"))
