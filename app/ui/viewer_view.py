"""Full-size single-photo viewer ("loupe" view): opened by double-clicking a
photo in the library, for looking at it large before deciding whether to
edit it. Shares the library's live model/proxy (not a private copy), so a
rating change or delete here is immediately reflected back in the grid, and
navigation always matches whatever filter/sort/search is currently active
there.
"""

from collections import OrderedDict
from html import escape
from pathlib import Path

import send2trash

from PySide6.QtCore import QEvent, QModelIndex, QPointF, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .. import image_io
from . import widgets
from .library_view import PhotoListModel

# Long-edge px for the viewer's preview decode -- smaller than the editor's
# PREVIEW_MAX_DIM (2400) since this is for browsing/looking, not pixel-level
# editing, and navigation (arrow keys, filmstrip clicks) needs to stay snappy.
VIEWER_MAX_DIM = 2000
FILMSTRIP_ITEM_W = 72
FILMSTRIP_ITEM_H = 52
FILMSTRIP_MAX_ITEM_W = 160  # cap so a folder with very few photos doesn't blow thumbnails up absurdly wide

# How many decoded full previews to keep around (path -> QPixmap) so
# stepping back to a recently-viewed photo is instant instead of re-running
# a RAW demosaic. ~8MB/entry at VIEWER_MAX_DIM, so this stays well under
# 100MB even at full.
PREVIEW_CACHE_SIZE = 6


class _ImageLoadThread(QThread):
    """Decodes one photo's preview off the GUI thread. generation lets the
    caller discard a stale result if the user navigated again before this
    finished (holding an arrow key fires several of these in a row) --
    the same pattern editor_view.py's render thread uses."""

    loaded = Signal(int, object)  # generation, QPixmap | None on failure

    def __init__(self, path, generation: int, parent=None):
        super().__init__(parent)
        self.path = path
        self.generation = generation

    def run(self):
        try:
            arr = image_io.load_preview_float(self.path, max_dim=VIEWER_MAX_DIM)
            pixmap = widgets.numpy_to_qpixmap(arr)
        except Exception as exc:  # noqa: BLE001 -- show a blank frame, not a crash
            print(f"[viewer] failed to load {self.path}: {exc}")
            pixmap = None
        self.loaded.emit(self.generation, pixmap)


class _ImageArea(QWidget):
    """The main black display area: shows the current pixmap letterboxed to
    fit, with round prev/next arrow buttons floating on the left/right
    edges."""

    prevRequested = Signal()
    nextRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #111111;")
        self._pixmap = None

        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def _arrow_button(text: str) -> QPushButton:
            btn = QPushButton(text, self)
            btn.setFixedSize(46, 46)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background: rgba(0,0,0,140); color: #eeeeee; border: none; "
                "border-radius: 23px; font-size: 22px; }"
                "QPushButton:hover { background: rgba(0,0,0,200); }"
            )
            return btn

        self.left_btn = _arrow_button("‹")
        self.right_btn = _arrow_button("›")
        self.left_btn.clicked.connect(self.prevRequested)
        self.right_btn.clicked.connect(self.nextRequested)

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.image_label.setGeometry(self.rect())
        self.left_btn.move(8, (self.height() - self.left_btn.height()) // 2)
        self.right_btn.move(self.width() - self.right_btn.width() - 8, (self.height() - self.right_btn.height()) // 2)
        self._rescale()

    def _rescale(self):
        if self._pixmap is None or self._pixmap.isNull():
            self.image_label.clear()
            return
        target = self.size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(scaled)


class _ScrubBar(QWidget):
    """Thin draggable track for jumping quickly across a large set -- click
    anywhere or drag the handle to scrub straight to that fraction of the
    way through, faster than scrolling the filmstrip for hundreds of
    photos."""

    positionChanged = Signal(float)  # 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fraction = 0.0
        self._dragging = False

    def set_fraction(self, fraction: float):
        fraction = max(0.0, min(1.0, fraction))
        if fraction != self._fraction:
            self._fraction = fraction
            self.update()

    def _fraction_at(self, x: float) -> float:
        w = max(1, self.width())
        return max(0.0, min(1.0, x / w))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        self.positionChanged.emit(self._fraction_at(event.position().x()))

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.positionChanged.emit(self._fraction_at(event.position().x()))

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid_y = self.height() // 2
        w = self.width()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#333333"))
        painter.drawRoundedRect(QRect(0, mid_y - 2, w, 4), 2, 2)
        fill_w = int(w * self._fraction)
        if fill_w > 0:
            painter.setBrush(QColor("#1d9e75"))
            painter.drawRoundedRect(QRect(0, mid_y - 2, fill_w, 4), 2, 2)
        painter.setPen(QPen(QColor("#141414"), 2))
        painter.setBrush(QColor("#eaeaea"))
        painter.drawEllipse(QPointF(fill_w, mid_y), 6, 6)


class _SpecIcon(QWidget):
    """Small flat vector icon for one EXIF spec row -- drawn with QPainter
    rather than a Unicode/emoji glyph so it always renders as a plain
    monochrome shape matching the app's flat dark UI. (Unicode glyphs like a
    pencil or wastebasket render through Windows' color emoji font, which
    looks visibly out of place here -- the same problem this sidesteps.)"""

    def __init__(self, kind: str, size: int = 20, color: str = "#8a8a8a", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.color = QColor(color)
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.color, 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = self.rect().adjusted(2, 2, -2, -2)
        cx, cy = r.center().x(), r.center().y()

        if self.kind == "aperture":
            painter.drawEllipse(r)
            painter.setBrush(self.color)
            painter.drawEllipse(QPointF(cx, cy), 2.2, 2.2)
        elif self.kind == "shutter":
            painter.drawEllipse(r)
            painter.drawLine(cx, cy, cx, r.top() + 2)
            painter.drawLine(cx, cy, r.right() - 3, cy)
        elif self.kind == "iso":
            painter.drawRect(r)
            painter.setBrush(self.color)
            painter.setPen(Qt.PenStyle.NoPen)
            box = 4
            for dx in (0, 1):
                for dy in (0, 1):
                    painter.drawRect(QRect(r.left() + 2 + dx * (box + 2), r.top() + 2 + dy * (box + 2), box, box))
        elif self.kind == "focal":
            painter.drawLine(r.left(), cy, r.right(), cy)
            painter.drawLine(r.left(), r.top(), r.left(), r.bottom())
            painter.drawLine(r.right(), r.top(), r.right(), r.bottom())
        elif self.kind == "lens":
            painter.drawEllipse(r.adjusted(1, 1, -1, -1))
            painter.drawEllipse(r.adjusted(6, 6, -6, -6))
        painter.end()


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()


class _FilmstripDelegate(QStyledItemDelegate):
    """item_width is mutable (set from outside, see
    PhotoViewerView._update_filmstrip_item_width) so a folder with few
    photos stretches its thumbnails to fill the full width instead of
    leaving the rest of the strip empty -- once there are enough photos to
    need scrolling, it settles back to FILMSTRIP_ITEM_W."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.item_width = FILMSTRIP_ITEM_W

    def sizeHint(self, option, index):
        return QSize(self.item_width, FILMSTRIP_ITEM_H)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect.adjusted(3, 3, -3, -3)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        is_current = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setClipRect(rect)
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
            )
            x = rect.x() - (scaled.width() - rect.width()) // 2
            y = rect.y() - (scaled.height() - rect.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.fillRect(rect, QColor("#333333"))
        painter.setClipping(False)
        if is_current:
            painter.setPen(QPen(QColor("#1d9e75"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
        painter.restore()


class PhotoViewerView(QWidget):
    backRequested = Signal()
    editRequested = Signal(int, str)  # photo_id, path

    def __init__(self, catalog, library_view=None, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        # Only used to mirror the type-filter combo's displayed state both
        # ways (see _sync_type_filter_from_library / the combo's handler) --
        # the actual filtering always goes through the shared proxy, which
        # library_view isn't otherwise needed for.
        self._library_view = library_view
        self.proxy = None
        self._current_row = -1
        self._load_thread = None
        self._load_generation = 0
        self._current_load_path = None
        self._preview_cache = OrderedDict()  # path -> QPixmap, most-recently-used last
        self._prefetch_thread = None

        self.setStyleSheet("background: #1c1c1c;")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addLayout(self._build_header())

        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(0)
        self.image_area = _ImageArea()
        self.image_area.prevRequested.connect(self.show_previous)
        self.image_area.nextRequested.connect(self.show_next)
        main_row.addWidget(self.image_area, stretch=1)
        main_row.addWidget(self._build_side_panel())
        root.addLayout(main_row, stretch=1)

        self.scrub_bar = _ScrubBar()
        self.scrub_bar.positionChanged.connect(self._on_scrub)
        scrub_wrap = QWidget()
        scrub_wrap.setStyleSheet("background: #1a1a1a;")
        scrub_layout = QHBoxLayout(scrub_wrap)
        scrub_layout.setContentsMargins(10, 0, 10, 0)
        scrub_layout.addWidget(self.scrub_bar)
        root.addWidget(scrub_wrap)

        root.addWidget(self._build_filmstrip())

        self._install_shortcuts()

    # ------------------------------------------------------------- layout

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(18, 12, 18, 12)

        back_btn = QPushButton("✕  Back to library")
        back_btn.setFlat(True)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet("QPushButton { color: #aaaaaa; border: none; text-align: left; font-size: 15px; }")
        back_btn.clicked.connect(self.backRequested)
        header.addWidget(back_btn)

        header.addStretch(1)

        self.type_filter = QComboBox()
        self.type_filter.addItems(["All formats", "JPEG only", "RAW only"])
        self.type_filter.setToolTip("Filter which photos you navigate through here (shared with the library).")
        self.type_filter.currentIndexChanged.connect(self._on_type_filter_changed)
        self.type_filter.setStyleSheet("font-size: 14px; padding: 3px 6px;")
        header.addWidget(self.type_filter)

        self.position_label = QLabel("")
        self.position_label.setStyleSheet("color: #999999; font-size: 15px; margin-left: 16px;")
        header.addWidget(self.position_label)
        return header

    def _build_side_panel(self) -> QWidget:
        # Two earlier widening passes (148->190->260px) grew every element,
        # each confirmed by pixel-measuring real screenshots, but still read
        # as "unchanged" on a real screen and lacked the sketch's icon rows
        # for individual specs. This pass widens further, makes Edit/Delete
        # real full-width buttons with real vertical weight (not padding on
        # a text label), and gives every EXIF spec its own icon + row.
        panel = QWidget()
        panel.setFixedWidth(320)
        panel.setStyleSheet("background: #202020;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 24, 24, 0)
        layout.setSpacing(20)

        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_btn.setFixedHeight(58)
        self.edit_btn.setStyleSheet(
            "QPushButton { background: #1d9e75; color: #08211a; font-weight: 600; border: none; "
            "border-radius: 8px; font-size: 20px; }"
            "QPushButton:hover { background: #24b989; }"
        )
        self.edit_btn.clicked.connect(self._on_edit_clicked)
        layout.addWidget(self.edit_btn)

        self.camera_label = QLabel("")
        self.camera_label.setWordWrap(True)
        self.camera_label.setStyleSheet("color: #cfcfcf; font-size: 17px; font-weight: 500; margin-top: 2px;")
        layout.addWidget(self.camera_label)

        # Each spec (lens, focal length, aperture, shutter, ISO) gets its own
        # row with a small drawn icon -- rebuilt per-photo since which specs
        # exist varies (see _populate_exif), rather than one rich-text block.
        self.exif_container = QWidget()
        self.exif_layout = QVBoxLayout(self.exif_container)
        self.exif_layout.setContentsMargins(0, 6, 0, 0)
        self.exif_layout.setSpacing(14)
        layout.addWidget(self.exif_container)

        layout.addStretch(1)

        self.rating_widget = widgets.StarRatingWidget(star_size=32)
        self.rating_widget.ratingChanged.connect(self._on_rating_changed)
        rating_row = QHBoxLayout()
        rating_row.addStretch(1)
        rating_row.addWidget(self.rating_widget)
        rating_row.addStretch(1)
        layout.addLayout(rating_row)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.setFixedHeight(50)
        self.delete_btn.setStyleSheet(
            "QPushButton { background: #2a1e1e; color: #e28f8f; border: 1px solid #4a2b2b; "
            "border-radius: 8px; font-weight: 500; font-size: 16px; margin-top: 4px; }"
            "QPushButton:hover { background: #3a2222; color: #ff9d9d; border-color: #6b3535; }"
        )
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        layout.addWidget(self.delete_btn)
        layout.addSpacing(20)
        return panel

    def _spec_row(self, kind: str, text: str) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        row_layout.addWidget(_SpecIcon(kind))
        label = QLabel(escape(text))
        label.setWordWrap(True)
        label.setStyleSheet("color: #9c9c9c; font-size: 16px;")
        row_layout.addWidget(label, stretch=1)
        return row

    def _build_filmstrip(self) -> QListView:
        self.filmstrip = QListView()
        self.filmstrip.setFixedHeight(FILMSTRIP_ITEM_H + 12)
        self.filmstrip.setStyleSheet("background: #1a1a1a; border: none;")
        self.filmstrip.setViewMode(QListView.ViewMode.IconMode)
        self.filmstrip.setFlow(QListView.Flow.LeftToRight)
        self.filmstrip.setWrapping(False)
        self.filmstrip.setResizeMode(QListView.ResizeMode.Adjust)
        self.filmstrip.setSpacing(2)
        self.filmstrip.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.filmstrip.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.filmstrip_delegate = _FilmstripDelegate(self.filmstrip)
        self.filmstrip.setItemDelegate(self.filmstrip_delegate)
        self.filmstrip.clicked.connect(self._on_filmstrip_clicked)
        return self.filmstrip

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_filmstrip_item_width()

    def _update_filmstrip_item_width(self):
        """Stretch filmstrip thumbnails to fill the available width when
        there are few enough photos that FILMSTRIP_ITEM_W-sized ones
        wouldn't reach the edge -- otherwise leaves it at the normal fixed
        size and lets the strip scroll."""
        count = self.proxy.rowCount() if self.proxy is not None else 0
        if count == 0:
            return
        available = self.filmstrip.viewport().width()
        spacing_total = self.filmstrip.spacing() * (count + 1)
        natural = (available - spacing_total) // count
        width = max(FILMSTRIP_ITEM_W, min(FILMSTRIP_MAX_ITEM_W, natural))
        if width != self.filmstrip_delegate.item_width:
            self.filmstrip_delegate.item_width = width
            self.filmstrip.reset()  # cheap on a proxy this size; forces the delegate's new sizeHint to apply

    def _install_shortcuts(self):
        for seq, slot in (("Left", self.show_previous), ("Right", self.show_next), ("Escape", self.backRequested.emit)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

    # --------------------------------------------------------------- data

    def set_source(self, proxy):
        """Attach to the library's live filtered/sorted model -- navigation
        here always matches whatever's currently visible in the grid."""
        if self.proxy is not None:
            self.proxy.rowsInserted.disconnect(self._on_proxy_changed)
            self.proxy.rowsRemoved.disconnect(self._on_proxy_changed)
            self.proxy.layoutChanged.disconnect(self._on_proxy_changed)
        self.proxy = proxy
        self.filmstrip.setModel(proxy)
        proxy.rowsInserted.connect(self._on_proxy_changed)
        proxy.rowsRemoved.connect(self._on_proxy_changed)
        proxy.layoutChanged.connect(self._on_proxy_changed)
        self._preview_cache.clear()  # a different folder/filter means a different set of paths

    def open_photo(self, photo_id: int):
        if self.proxy is None:
            return
        self._sync_type_filter_from_library()
        self._update_filmstrip_item_width()
        for row in range(self.proxy.rowCount()):
            if self.proxy.index(row, 0).data(PhotoListModel.PhotoIdRole) == photo_id:
                self._show_row(row)
                return
        self._show_row(0)

    def _sync_type_filter_from_library(self):
        if self._library_view is None:
            return
        self.type_filter.blockSignals(True)
        self.type_filter.setCurrentIndex(self._library_view.type_filter.currentIndex())
        self.type_filter.blockSignals(False)

    def _on_proxy_changed(self, *args):
        # The set of visible photos changed (filter/sort/scan) while the
        # viewer is open -- clamp back onto a valid row rather than showing
        # stale data for a row that may no longer mean the same photo.
        self._update_filmstrip_item_width()
        if self._current_row >= 0:
            self._show_row(self._current_row)

    def _current_photo_id(self):
        if self.proxy is None or not (0 <= self._current_row < self.proxy.rowCount()):
            return None
        return self.proxy.index(self._current_row, 0).data(PhotoListModel.PhotoIdRole)

    def _show_row(self, row: int):
        total = self.proxy.rowCount() if self.proxy is not None else 0
        if total == 0:
            self.backRequested.emit()
            return
        row = max(0, min(row, total - 1))
        self._current_row = row
        index = self.proxy.index(row, 0)
        path = index.data(PhotoListModel.PathRole)
        name = index.data(PhotoListModel.NameRole) or ""
        rating = index.data(PhotoListModel.RatingRole) or 0

        self.position_label.setText(f"{name}    ·    {row + 1} of {total}")
        self.rating_widget.set_rating(rating)
        self._populate_exif(path)
        self.scrub_bar.set_fraction(row / (total - 1) if total > 1 else 0.0)

        self.filmstrip.setCurrentIndex(index)
        self.filmstrip.scrollTo(index, QListView.ScrollHint.EnsureVisible)

        self._load_generation += 1
        cached = self._preview_cache.get(path)
        if cached is not None:
            self._preview_cache.move_to_end(path)
            self.image_area.set_pixmap(cached)
            self._schedule_prefetch()
            return

        # Instant low-res placeholder from the thumbnail already decoded for
        # the library grid/filmstrip -- swapped for the full decode as soon
        # as it's ready, so a slow RAW demosaic doesn't show a blank/stale
        # frame while it runs.
        thumb = index.data(Qt.ItemDataRole.DecorationRole)
        if thumb is not None and not thumb.isNull():
            self.image_area.set_pixmap(thumb)

        generation = self._load_generation
        self._current_load_path = path
        self._load_thread = _ImageLoadThread(path, generation, self)
        self._load_thread.loaded.connect(self._on_image_loaded)
        self._load_thread.start()

    def _on_image_loaded(self, generation: int, pixmap):
        if generation != self._load_generation:
            return  # superseded by a newer navigation before this finished
        if pixmap is not None:
            self._cache_preview(self._current_load_path, pixmap)
        self.image_area.set_pixmap(pixmap)
        self._schedule_prefetch()

    def _cache_preview(self, path, pixmap):
        self._preview_cache[path] = pixmap
        self._preview_cache.move_to_end(path)
        while len(self._preview_cache) > PREVIEW_CACHE_SIZE:
            self._preview_cache.popitem(last=False)

    def _schedule_prefetch(self):
        """Decodes the neighbor photo(s) of the current row in the
        background, one at a time, so arrow-key navigation across
        already-visited or about-to-be-visited photos hits the cache
        instead of paying for a RAW demosaic on every keypress."""
        if self.proxy is None or (self._prefetch_thread is not None and self._prefetch_thread.isRunning()):
            return
        total = self.proxy.rowCount()
        for neighbor_row in (self._current_row + 1, self._current_row - 1):
            if not (0 <= neighbor_row < total):
                continue
            path = self.proxy.index(neighbor_row, 0).data(PhotoListModel.PathRole)
            if path in self._preview_cache or path == self._current_load_path:
                continue
            thread = _ImageLoadThread(path, 0, self)
            thread.loaded.connect(lambda _gen, pixmap, path=path: self._on_prefetched(path, pixmap))
            self._prefetch_thread = thread
            thread.start()
            return

    def _on_prefetched(self, path, pixmap):
        if pixmap is not None:
            self._cache_preview(path, pixmap)
        self._prefetch_thread = None
        self._schedule_prefetch()  # chain onto the other neighbor, if it's still missing

    def _populate_exif(self, path):
        """Rebuilds the icon-row EXIF list for the current photo -- which
        specs exist varies per file, so the rows are rebuilt each time
        rather than kept as fixed widgets with hidden/shown states."""
        try:
            summary = image_io.get_exif_summary(path)
        except Exception:  # noqa: BLE001 -- info panel is best-effort
            summary = {}

        camera = summary.get("camera")
        self.camera_label.setText(escape(camera) if camera else "")
        self.camera_label.setVisible(bool(camera))

        _clear_layout(self.exif_layout)

        lens = summary.get("lens")
        if lens:
            self.exif_layout.addWidget(self._spec_row("lens", lens))

        aperture, shutter, iso, focal = (
            summary.get("aperture"), summary.get("shutter"), summary.get("iso"), summary.get("focal_length"),
        )
        if focal:
            self.exif_layout.addWidget(self._spec_row("focal", f"{focal:.0f}mm"))
        if aperture:
            self.exif_layout.addWidget(self._spec_row("aperture", f"ƒ/{aperture:.1f}".replace(".0", "")))
        if shutter:
            text = f"1/{round(1.0 / shutter)}s" if shutter < 1 else f"{shutter:.1f}s"
            self.exif_layout.addWidget(self._spec_row("shutter", text))
        if iso:
            text = f"ISO {iso:.0f}" if isinstance(iso, float) else f"ISO {iso}"
            self.exif_layout.addWidget(self._spec_row("iso", text))

    # ---------------------------------------------------------- navigation

    def show_previous(self):
        if self._current_row >= 0:
            self._show_row(self._current_row - 1)

    def show_next(self):
        if self._current_row >= 0:
            self._show_row(self._current_row + 1)

    def _on_scrub(self, fraction: float):
        total = self.proxy.rowCount() if self.proxy is not None else 0
        if total == 0:
            return
        self._show_row(round(fraction * (total - 1)))

    def _on_filmstrip_clicked(self, index):
        if index.isValid():
            self._show_row(index.row())

    # -------------------------------------------------------------- edits

    def _on_rating_changed(self, rating: int):
        photo_id = self._current_photo_id()
        if photo_id is None:
            return
        self.catalog.set_rating(photo_id, rating)
        index = self.proxy.index(self._current_row, 0)
        self.proxy.setData(index, rating, PhotoListModel.RatingRole)

    def _on_edit_clicked(self):
        photo_id = self._current_photo_id()
        if photo_id is None:
            return
        path = self.proxy.index(self._current_row, 0).data(PhotoListModel.PathRole)
        self.editRequested.emit(photo_id, path)

    def _on_type_filter_changed(self, idx: int):
        if self.proxy is not None:
            self.proxy.set_type_filter({0: "all", 1: "jpeg", 2: "raw"}.get(idx, "all"))
        if self._library_view is not None:
            self._library_view.type_filter.blockSignals(True)
            self._library_view.type_filter.setCurrentIndex(idx)
            self._library_view.type_filter.blockSignals(False)

    def _on_delete_clicked(self):
        photo_id = self._current_photo_id()
        if photo_id is None:
            return
        path = self.proxy.index(self._current_row, 0).data(PhotoListModel.PathRole)
        name = Path(path).name

        choice = QMessageBox.question(
            self,
            "Delete Photo",
            f'Remove "{name}" from the library and send the file to the Recycle Bin?\n\n'
            "This is not a permanent delete -- the file can still be restored from the Recycle "
            "Bin afterward, the same as deleting it in Explorer. Its rating, flag, and edit "
            "history in this app's catalog will be removed either way.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        try:
            send2trash.send2trash(path)
        except Exception as exc:  # noqa: BLE001 -- report, don't crash the viewer
            QMessageBox.warning(self, "Delete Failed", f'Couldn\'t delete "{name}":\n\n{exc}')
            return

        self.catalog.delete_photos([photo_id])
        row = self._current_row
        self.filmstrip.model().sourceModel().remove_photo(photo_id)
        self._show_row(row)  # same index now points at the next photo (or the new last one)
