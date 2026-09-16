"""Non-destructive editor: live-preview canvas + adjustment panel + export."""

import math
import threading
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import auto_enhance, image_io, processing, segmentation
from ..settings import AUTO_ENHANCE_CONSENT, LAST_EXPORT_FOLDER, get_settings
from . import widgets

PREVIEW_MAX_DIM = 2400  # long-edge px for the interactive-editing preview (full-res
# only on export). Was 1400 -- fine for fitting a whole photo on screen, but
# looked visibly soft/blurry once zoomed in past ~150% to inspect detail.
# 2400 stays under this camera's half-size-decode threshold (RAW decode time
# barely changes vs. 1400: ~350ms either way; a render pass with sharpen/
# exposure/contrast is ~110ms, still comfortably interactive) while roughly
# tripling the visible pixel count.
HANDLE_PX = 9
MIN_CROP_NORM = 0.04

ASPECT_RATIOS = [
    ("Free", None),
    ("Original", "original"),
    ("1:1", 1.0),
    ("4:5 (portrait)", 4 / 5),
    ("5:4 (landscape)", 5 / 4),
    ("3:4 (portrait)", 3 / 4),
    ("4:3 (landscape)", 4 / 3),
    ("2:3 (portrait)", 2 / 3),
    ("3:2 (landscape)", 3 / 2),
    ("9:16 (portrait)", 9 / 16),
    ("16:9 (landscape)", 16 / 9),
    ("5:7 (portrait)", 5 / 7),
    ("7:5 (landscape)", 7 / 5),
]

RATIO_LABELS = {
    (3, 2): "3:2", (2, 3): "2:3", (4, 3): "4:3", (3, 4): "3:4",
    (16, 9): "16:9", (9, 16): "9:16", (1, 1): "1:1", (5, 4): "5:4", (4, 5): "4:5",
    (5, 7): "5:7", (7, 5): "7:5",
}


class ImageCanvas(QWidget):
    cropPreviewChanged = Signal()
    aiSelectClicked = Signal(float, float, int, bool)  # img_x, img_y, label, is_refine
    brushStroke = Signal(float, float, bool)  # img_x, img_y, is_new_stroke
    gradientChanged = Signal(float, float, float, float, bool)  # x0, y0, x1, y1, is_final
    radialGradientChanged = Signal(float, float, float, float, bool)  # bounding box x0,y0,x1,y1, is_final
    polygonGradientChanged = Signal(object, bool)  # [(img_x, img_y), ...] vertices, is_final (True once closed)
    gradHitMissed = Signal()  # a click missed an already-placed shape's body/handles -- nothing was changed
    zoomChanged = Signal(float)  # effective display scale, as a percentage (100 == actual pixel size)

    MIN_ZOOM = 0.1
    MAX_ZOOM = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setMouseTracking(True)  # so the brush cursor circle follows the mouse without needing a button held
        self._pixmap = None
        self._zoom = 1.0  # multiplier on top of the fit-to-widget scale; 1.0 == fit
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._panning = False
        self._pan_drag_start = None
        self._pan_drag_anchor = None
        self._pre_pan_cursor = None
        self._crop_mode = False
        self._crop_rect = None  # normalized [x0,y0,x1,y1] in the displayed pixmap's frame
        self._crop_ratio = None  # None (free) | "original" | float (w/h)
        self._active_handle = None
        self._drag_start_pos = None
        self._drag_anchor_rect = None
        self._display_offset = (0, 0)
        self._display_size = (0, 0)
        self._ai_select_mode = False
        self._brush_mode = False
        self._brush_radius = 40.0  # image-pixel space
        self._brush_feather = 30.0  # 0..100, % of radius that's soft falloff
        self._brush_cursor_pos = None  # (img_x, img_y) while hovering in brush mode
        self._gradient_mode = False
        self._gradient_shape = "linear"  # "linear" | "radial" | "polygon"
        self._gradient_start = None  # (img_x, img_y), may be outside the image bounds -- linear/radial
        self._gradient_end = None
        self._polygon_points = []  # [(img_x, img_y), ...] vertices placed so far
        self._polygon_closed = False  # True once the shape is finalized; the next click starts a new one
        self._polygon_cursor_pos = None  # (img_x, img_y) while hovering, for the rubber-band line to the next vertex
        self._radial_angle = 0.0  # degrees; the Radial ellipse's own rotation, independent of its bounding box
        # Editing an already-placed shape (move/rotate) rather than creating a new one:
        self._grad_active_handle = None  # "start" | "end" | "move" | "rotate" | None
        self._grad_drag_start_img = None  # (img_x, img_y) mouse position when the edit-drag began
        self._grad_drag_anchor = None  # shape geometry snapshot at drag-start, so deltas don't drift/accumulate
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # so Escape/Enter reach keyPressEvent while drawing a polygon
        self._mask_contours = []  # cached boundary contours (mask-pixel space) for the current mask
        self._mask_shape = None  # (h, w) the contours above are expressed in
        self._points_overlay = []  # [(img_x, img_y, label), ...]

    # ------------------------------------------------------------- display

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def is_crop_mode(self) -> bool:
        return self._crop_mode

    def _image_size(self):
        if self._pixmap is None or self._pixmap.isNull():
            return (1, 1)
        return (self._pixmap.width(), self._pixmap.height())

    # -------------------------------------------------------- zoom / pan

    def _fit_scale(self):
        iw, ih = self._image_size()
        if iw <= 0 or ih <= 0 or self.width() <= 0 or self.height() <= 0:
            return 1.0
        return min(self.width() / iw, self.height() / ih)

    def _compute_display_geometry(self):
        """(ox, oy, scale, dw, dh) for the current pixmap given zoom+pan."""
        iw, ih = self._image_size()
        scale = self._fit_scale() * self._zoom
        dw, dh = iw * scale, ih * scale
        ox = (self.width() - dw) / 2 + self._pan_x
        oy = (self.height() - dh) / 2 + self._pan_y
        return ox, oy, scale, dw, dh

    def _clamp_pan(self):
        _, _, _, dw, dh = self._compute_display_geometry()
        max_pan_x = max(0.0, (dw - self.width()) / 2)
        max_pan_y = max(0.0, (dh - self.height()) / 2)
        self._pan_x = max(-max_pan_x, min(max_pan_x, self._pan_x))
        self._pan_y = max(-max_pan_y, min(max_pan_y, self._pan_y))

    def zoom_percent(self) -> float:
        return self._fit_scale() * self._zoom * 100.0

    def is_at_fit_zoom(self) -> bool:
        return abs(self._zoom - 1.0) < 1e-6

    def reset_view(self):
        """Back to fit-to-window with no pan -- called when a new photo loads."""
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()
        self.zoomChanged.emit(self.zoom_percent())

    def set_zoom(self, zoom: float, center=None):
        """Set zoom (as a multiplier on top of fit-scale), keeping the image
        point under `center` (widget coords, defaults to the widget's own
        center) stationary on screen."""
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        if abs(new_zoom - self._zoom) < 1e-9:
            return
        if center is None:
            center = QPointF(self.width() / 2, self.height() / 2)
        ox, oy, old_scale, _, _ = self._compute_display_geometry()
        img_x = (center.x() - ox) / old_scale if old_scale else 0.0
        img_y = (center.y() - oy) / old_scale if old_scale else 0.0

        self._zoom = new_zoom
        iw, ih = self._image_size()
        new_scale = self._fit_scale() * self._zoom
        base_ox = (self.width() - iw * new_scale) / 2
        base_oy = (self.height() - ih * new_scale) / 2
        self._pan_x = center.x() - base_ox - img_x * new_scale
        self._pan_y = center.y() - base_oy - img_y * new_scale
        self._clamp_pan()
        self.update()
        self.zoomChanged.emit(self.zoom_percent())

    def zoom_in(self):
        self.set_zoom(self._zoom * 1.25)

    def zoom_out(self):
        self.set_zoom(self._zoom / 1.25)

    def zoom_to_fit(self):
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()
        self.zoomChanged.emit(self.zoom_percent())

    def zoom_to_actual(self):
        """100% -- one image pixel per screen pixel."""
        fit = self._fit_scale()
        if fit > 0:
            self.set_zoom(1.0 / fit)

    def _widget_to_image_pos(self, pos):
        """Maps a widget-space point to image-pixel coordinates, or None if
        outside the displayed image (accounting for the letterbox offset)."""
        ox, oy = self._display_offset
        dw, dh = self._display_size
        if dw <= 0 or dh <= 0:
            return None
        iw, ih = self._image_size()
        img_x = (pos.x() - ox) / dw * iw
        img_y = (pos.y() - oy) / dh * ih
        if not (0 <= img_x <= iw and 0 <= img_y <= ih):
            return None
        return img_x, img_y

    def _widget_to_image_pos_unclamped(self, pos):
        """Like _widget_to_image_pos, but doesn't reject points outside the
        image -- used by the gradient tool, where dragging an endpoint past
        the image edge is a normal, useful gesture (the fade just continues
        beyond the visible area)."""
        ox, oy = self._display_offset
        dw, dh = self._display_size
        if dw <= 0 or dh <= 0:
            return None
        iw, ih = self._image_size()
        return (pos.x() - ox) / dw * iw, (pos.y() - oy) / dh * ih

    def _image_to_widget_pos(self, img_pos):
        """Inverse of _widget_to_image_pos_unclamped -- used to hit-test screen
        clicks against a polygon vertex stored in image-pixel space."""
        ox, oy = self._display_offset
        dw, dh = self._display_size
        iw, ih = self._image_size()
        if iw <= 0 or ih <= 0:
            return None
        return ox + (img_pos[0] / iw) * dw, oy + (img_pos[1] / ih) * dh

    # ---------------------------------------------------------------- crop

    def enter_crop_mode(self, existing_rect):
        self._crop_mode = True
        self._ai_select_mode = False
        self._crop_rect = list(existing_rect) if existing_rect else [0.05, 0.05, 0.95, 0.95]
        self._active_handle = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()
        self.cropPreviewChanged.emit()

    def exit_crop_mode(self):
        self._crop_mode = False
        self._active_handle = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        rect = self._crop_rect
        self.update()
        return rect

    def clear_crop(self):
        self._crop_rect = [0.0, 0.0, 1.0, 1.0]
        self.update()
        self.cropPreviewChanged.emit()

    def set_crop_rect(self, rect):
        """Sync crop overlay state when a new photo loads (outside of an
        active crop-editing gesture) so a previous photo's crop rectangle
        doesn't linger on screen."""
        self._crop_mode = False
        self._crop_rect = list(rect) if rect else None
        self._active_handle = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        self.cropPreviewChanged.emit()

    def get_crop_rect(self):
        """Current normalized crop rect [x0,y0,x1,y1], or None. Read-only --
        does not exit crop mode (unlike exit_crop_mode())."""
        return list(self._crop_rect) if self._crop_rect else None

    # ------------------------------------------------------------ AI select

    def is_ai_select_mode(self) -> bool:
        return self._ai_select_mode

    def set_ai_select_mode(self, enabled: bool):
        self._ai_select_mode = enabled
        if enabled:
            self._crop_mode = False
            self._brush_mode = False
            self._gradient_mode = False
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.update()

    # ---------------------------------------------------------------- brush

    def is_brush_mode(self) -> bool:
        return self._brush_mode

    def set_brush_mode(self, enabled: bool):
        self._brush_mode = enabled
        if enabled:
            self._crop_mode = False
            self._ai_select_mode = False
            self._gradient_mode = False
        self.setCursor(Qt.CursorShape.BlankCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_brush_radius(self, radius_px: float):
        self._brush_radius = max(1.0, radius_px)
        self.update()

    def set_brush_feather(self, feather_pct: float):
        self._brush_feather = max(0.0, min(100.0, feather_pct))

    # ------------------------------------------------------------- gradient

    def is_gradient_mode(self) -> bool:
        return self._gradient_mode

    def set_gradient_mode(self, enabled: bool):
        self._gradient_mode = enabled
        if enabled:
            self._crop_mode = False
            self._ai_select_mode = False
            self._brush_mode = False
            self.setFocus()  # so an in-progress polygon can be cancelled/closed with Escape/Enter
        else:
            self._gradient_start = None
            self._gradient_end = None
            self._polygon_points = []
            self._polygon_closed = False
            self._polygon_cursor_pos = None
            self._radial_angle = 0.0
            self._grad_active_handle = None
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_gradient_shape(self, shape: str):
        """Switching shape starts a fresh placement -- a half-drawn linear
        drag or in-progress polygon has no sensible translation into a radial
        box or vice versa."""
        self._gradient_shape = shape
        self._gradient_start = None
        self._gradient_end = None
        self._polygon_points = []
        self._polygon_closed = False
        self._polygon_cursor_pos = None
        self._radial_angle = 0.0
        self._grad_active_handle = None
        self.update()

    def get_radial_angle(self) -> float:
        return self._radial_angle

    def set_mask_overlay(self, mask):
        """Shows the mask as a thin boundary outline rather than a filled
        tint -- a solid color wash over the whole selected region made it
        impossible to judge how an adjustment actually looked underneath."""
        if mask is None:
            self._mask_contours = []
            self._mask_shape = None
        else:
            mask_u8 = mask.astype(np.uint8) * 255
            contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            self._mask_contours = contours
            self._mask_shape = mask.shape[:2]
        self.update()

    def set_points_overlay(self, points):
        self._points_overlay = list(points) if points else []
        self.update()

    def _resolve_ratio(self):
        if self._crop_ratio is None:
            return None
        if self._crop_ratio == "original":
            iw, ih = self._image_size()
            return iw / ih if ih else None
        return self._crop_ratio

    def set_aspect_ratio(self, ratio):
        self._crop_ratio = ratio
        resolved = self._resolve_ratio()
        if resolved is None or self._crop_rect is None:
            self.update()
            return
        iw, ih = self._image_size()
        x0, y0, x1, y1 = self._crop_rect
        px0, py0, px1, py1 = x0 * iw, y0 * ih, x1 * iw, y1 * ih
        cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
        w, h = px1 - px0, py1 - py0
        cur_ratio = w / h if h else resolved
        if cur_ratio > resolved:
            w = h * resolved
        else:
            h = w / resolved
        nx0, nx1 = cx - w / 2, cx + w / 2
        ny0, ny1 = cy - h / 2, cy + h / 2
        if nx0 < 0:
            nx1 -= nx0
            nx0 = 0
        if nx1 > iw:
            nx0 -= nx1 - iw
            nx1 = iw
        if ny0 < 0:
            ny1 -= ny0
            ny0 = 0
        if ny1 > ih:
            ny0 -= ny1 - ih
            ny1 = ih
        nx0, ny0 = max(0.0, nx0), max(0.0, ny0)
        nx1, ny1 = min(iw, nx1), min(ih, ny1)
        self._crop_rect = [nx0 / iw, ny0 / ih, nx1 / iw, ny1 / ih]
        self.update()
        self.cropPreviewChanged.emit()

    def _handle_positions_px(self):
        if self._crop_rect is None:
            return {}
        ox, oy = self._display_offset
        dw, dh = self._display_size
        x0, y0, x1, y1 = self._crop_rect
        px0, py0, px1, py1 = ox + x0 * dw, oy + y0 * dh, ox + x1 * dw, oy + y1 * dh
        mx, my = (px0 + px1) / 2, (py0 + py1) / 2
        return {
            "nw": (px0, py0), "n": (mx, py0), "ne": (px1, py0),
            "w": (px0, my), "e": (px1, my),
            "sw": (px0, py1), "s": (mx, py1), "se": (px1, py1),
        }

    def _hit_test(self, pos):
        for name, (hx, hy) in self._handle_positions_px().items():
            if abs(pos.x() - hx) <= HANDLE_PX and abs(pos.y() - hy) <= HANDLE_PX:
                return name
        if self._crop_rect is not None:
            ox, oy = self._display_offset
            dw, dh = self._display_size
            x0, y0, x1, y1 = self._crop_rect
            rect_px = QRectF(ox + x0 * dw, oy + y0 * dh, (x1 - x0) * dw, (y1 - y0) * dh)
            if rect_px.contains(pos):
                return "move"
        return None

    # ------------------------------------------------------------- painting

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202020"))
        if self._pixmap is None or self._pixmap.isNull():
            return
        ox, oy, _scale, dw, dh = self._compute_display_geometry()
        self._display_offset = (ox, oy)
        self._display_size = (dw, dh)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(
            QRectF(ox, oy, dw, dh), self._pixmap, QRectF(0, 0, self._pixmap.width(), self._pixmap.height())
        )

        if self._crop_mode and self._crop_rect is not None:
            dw, dh = self._display_size
            x0, y0, x1, y1 = self._crop_rect
            rect_px = QRectF(ox + x0 * dw, oy + y0 * dh, (x1 - x0) * dw, (y1 - y0) * dh)
            outer = QRectF(ox, oy, dw, dh)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 140))
            painter.drawRect(QRectF(outer.left(), outer.top(), outer.width(), rect_px.top() - outer.top()))
            painter.drawRect(QRectF(outer.left(), rect_px.bottom(), outer.width(), outer.bottom() - rect_px.bottom()))
            painter.drawRect(QRectF(outer.left(), rect_px.top(), rect_px.left() - outer.left(), rect_px.height()))
            painter.drawRect(QRectF(rect_px.right(), rect_px.top(), outer.right() - rect_px.right(), rect_px.height()))

            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect_px)

            painter.setPen(QPen(QColor(255, 255, 255, 120), 1, Qt.PenStyle.DashLine))
            for i in (1, 2):
                gx = rect_px.left() + rect_px.width() * i / 3
                painter.drawLine(int(gx), int(rect_px.top()), int(gx), int(rect_px.bottom()))
                gy = rect_px.top() + rect_px.height() * i / 3
                painter.drawLine(int(rect_px.left()), int(gy), int(rect_px.right()), int(gy))

            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.setBrush(QColor("#3399ff"))
            for _name, (hx, hy) in self._handle_positions_px().items():
                painter.drawRect(QRectF(hx - 5, hy - 5, 10, 10))

        if self._ai_select_mode or self._brush_mode:
            dw, dh = self._display_size
            if self._mask_contours and self._mask_shape:
                mh, mw = self._mask_shape
                painter.setPen(QPen(QColor("#33ccff"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                for contour in self._mask_contours:
                    if len(contour) < 2:
                        continue
                    points = [
                        QPointF(ox + (pt[0][0] / mw) * dw, oy + (pt[0][1] / mh) * dh) for pt in contour
                    ]
                    painter.drawPolygon(QPolygonF(points))
            if self._points_overlay:
                iw, ih = self._image_size()
                painter.setPen(QPen(QColor("#ffffff"), 1.5))
                for img_x, img_y, label in self._points_overlay:
                    px = ox + (img_x / iw) * dw if iw else ox
                    py = oy + (img_y / ih) * dh if ih else oy
                    painter.setBrush(QColor("#33cc55") if label == 1 else QColor("#e04444"))
                    painter.drawEllipse(int(px - 5), int(py - 5), 10, 10)

        if self._brush_mode and self._brush_cursor_pos is not None:
            dw, dh = self._display_size
            iw, ih = self._image_size()
            cx = ox + (self._brush_cursor_pos[0] / iw) * dw if iw else ox
            cy = oy + (self._brush_cursor_pos[1] / ih) * dh if ih else oy
            scale = (dw / iw) if iw else 1.0
            r = self._brush_radius * scale
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), r, r)
            # inner ring marks where the soft feather zone begins (hard core inside it)
            core_r = self._brush_radius * (1.0 - self._brush_feather / 100.0) * scale
            if core_r > 1.0:
                painter.setPen(QPen(QColor(255, 255, 255, 100), 1, Qt.PenStyle.DashLine))
                painter.drawEllipse(QPointF(cx, cy), core_r, core_r)

        if (
            self._gradient_mode
            and self._gradient_shape == "linear"
            and self._gradient_start is not None
            and self._gradient_end is not None
        ):
            dw, dh = self._display_size
            iw, ih = self._image_size()
            sx = ox + (self._gradient_start[0] / iw) * dw if iw else ox
            sy = oy + (self._gradient_start[1] / ih) * dh if ih else oy
            ex = ox + (self._gradient_end[0] / iw) * dw if iw else ox
            ey = oy + (self._gradient_end[1] / ih) * dh if ih else oy

            # The fade runs ALONG the start->end axis, so the actual boundaries of
            # the fade band are perpendicular to it -- a single line drawn along
            # the axis itself (the old behavior) looks exactly like a hard split
            # down the middle of the image, which is the opposite of what a
            # gradient tool should communicate. Draw it the way Lightroom/Capture
            # One do: a solid guide line at the full-effect edge, a solid guide
            # line at the zero-effect edge, a dashed one at the 50% midpoint, and
            # a thin dashed axis connecting the two drag handles.
            axis_dx, axis_dy = ex - sx, ey - sy
            axis_len = math.hypot(axis_dx, axis_dy)
            painter.setPen(QPen(QColor(255, 255, 255, 110), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(sx, sy), QPointF(ex, ey))
            if axis_len > 1e-3:
                perp_x, perp_y = -axis_dy / axis_len, axis_dx / axis_len
                half_len = max(40.0, min(dw, dh) * 0.12)

                def _guide_line(cx, cy, pen):
                    painter.setPen(pen)
                    painter.drawLine(
                        QPointF(cx - perp_x * half_len, cy - perp_y * half_len),
                        QPointF(cx + perp_x * half_len, cy + perp_y * half_len),
                    )

                _guide_line(sx, sy, QPen(QColor("#33ccff"), 2))
                _guide_line((sx + ex) / 2, (sy + ey) / 2, QPen(QColor(255, 255, 255, 150), 1, Qt.PenStyle.DashLine))
                _guide_line(ex, ey, QPen(QColor("#ffffff"), 2))

            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.setBrush(QColor("#33ccff"))
            painter.drawEllipse(QPointF(sx, sy), 6, 6)
            painter.setBrush(QColor(255, 255, 255, 60))
            painter.drawEllipse(QPointF(ex, ey), 6, 6)

        if (
            self._gradient_mode
            and self._gradient_shape == "radial"
            and self._gradient_start is not None
            and self._gradient_end is not None
        ):
            dw, dh = self._display_size
            iw, ih = self._image_size()
            x0, y0 = self._gradient_start
            x1, y1 = self._gradient_end
            cx_img, cy_img = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            cx = ox + (cx_img / iw) * dw if iw else ox
            cy = oy + (cy_img / ih) * dh if ih else oy
            rx = abs(x1 - x0) / 2.0 * (dw / iw if iw else 1.0)
            ry = abs(y1 - y0) / 2.0 * (dh / ih if ih else 1.0)

            painter.save()
            painter.translate(cx, cy)
            painter.rotate(self._radial_angle)
            painter.setPen(QPen(QColor("#33ccff"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), rx, ry)
            # inner ellipse marks where the feathered falloff begins (mirrors
            # the brush cursor's solid-outer/dashed-inner convention above)
            feather = max(0.0, min(100.0, self._brush_feather)) / 100.0
            if feather < 0.999:
                painter.setPen(QPen(QColor(255, 255, 255, 130), 1, Qt.PenStyle.DashLine))
                painter.drawEllipse(QPointF(0, 0), rx * feather, ry * feather)
            # rotate handle: a stalk running "up" in the ellipse's own rotated
            # frame, with a small grip circle at the end
            handle_dist = ry + 30
            painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
            painter.drawLine(QPointF(0, -ry), QPointF(0, -handle_dist))
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(QPointF(0, -handle_dist), 6, 6)
            painter.restore()

        if self._gradient_mode and self._gradient_shape == "polygon" and (self._polygon_points or self._polygon_cursor_pos):
            dw, dh = self._display_size
            iw, ih = self._image_size()

            def _to_screen(p):
                return ox + (p[0] / iw) * dw if iw else ox, oy + (p[1] / ih) * dh if ih else oy

            screen_pts = [_to_screen(p) for p in self._polygon_points]
            if len(screen_pts) >= 2:
                painter.setPen(QPen(QColor("#33ccff"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                poly = QPolygonF([QPointF(x, y) for x, y in screen_pts])
                if self._polygon_closed:
                    painter.drawPolygon(poly)
                else:
                    painter.drawPolyline(poly)
            if not self._polygon_closed and screen_pts and self._polygon_cursor_pos is not None:
                cx, cy = _to_screen(self._polygon_cursor_pos)
                painter.setPen(QPen(QColor(255, 255, 255, 130), 1, Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(*screen_pts[-1]), QPointF(cx, cy))
                if len(screen_pts) >= 3:
                    # rubber-band back to the first vertex too, so the
                    # about-to-close shape is visible before the final click
                    painter.drawLine(QPointF(cx, cy), QPointF(*screen_pts[0]))
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            for i, (px, py) in enumerate(screen_pts):
                painter.setBrush(QColor("#33ccff") if i == 0 else QColor(255, 255, 255, 60))
                painter.drawEllipse(QPointF(px, py), 5, 5)

            if self._polygon_closed:
                cx_img, _ = self._polygon_centroid()
                handle_img = self._polygon_rotate_handle_img_pos()
                cx_top = _to_screen((cx_img, min(p[1] for p in self._polygon_points)))
                hx, hy = _to_screen(handle_img)
                painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
                painter.drawLine(QPointF(*cx_top), QPointF(hx, hy))
                painter.setPen(QPen(QColor("#ffffff"), 1.5))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(QPointF(hx, hy), 6, 6)

    # ---------------------------------------------------- gradient shape editing
    # Once a shape is placed, clicking it again moves or rotates it instead of
    # starting over -- these helpers hit-test screen clicks against the
    # shape's current geometry and apply the resulting transform.

    @staticmethod
    def _dist_point_to_segment(px, py, x0, y0, x1, y1) -> float:
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-6:
            return math.hypot(px - x0, py - y0)
        t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length_sq))
        cx, cy = x0 + t * dx, y0 + t * dy
        return math.hypot(px - cx, py - cy)

    # Hit-test tolerances are deliberately generous: a miss is no longer
    # destructive (see gradHitMissed / mousePressEvent), but a shape that's
    # merely annoying to grab is still a bad experience, so err toward "did
    # you probably mean this shape" rather than pixel-perfect precision.
    HANDLE_HIT_PX = 14
    LINE_HIT_PX = 12

    def _linear_hit_test(self, screen_pos):
        s = self._image_to_widget_pos(self._gradient_start)
        e = self._image_to_widget_pos(self._gradient_end)
        if s is None or e is None:
            return None
        if math.hypot(screen_pos.x() - s[0], screen_pos.y() - s[1]) < self.HANDLE_HIT_PX:
            return "start"
        if math.hypot(screen_pos.x() - e[0], screen_pos.y() - e[1]) < self.HANDLE_HIT_PX:
            return "end"
        if self._dist_point_to_segment(screen_pos.x(), screen_pos.y(), s[0], s[1], e[0], e[1]) < self.LINE_HIT_PX:
            return "move"
        return None

    def _radial_point_in_shape(self, img_pos) -> bool:
        """Whether img_pos is "on the shape" for grab purposes -- deliberately
        the ellipse's own (rotated) bounding BOX rather than the strict
        ellipse curve, since a user visually associates the whole box with
        "the shape" and a click landing in one of the box's corners (outside
        the inscribed ellipse, but still clearly on top of it) should still
        grab it rather than falling through to "missed"."""
        x0, y0 = self._gradient_start
        x1, y1 = self._gradient_end
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx, ry = abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
        if rx < 1e-3 or ry < 1e-3:
            return False
        ang = math.radians(self._radial_angle)
        dx, dy = img_pos[0] - cx, img_pos[1] - cy
        local_x = dx * math.cos(ang) + dy * math.sin(ang)
        local_y = -dx * math.sin(ang) + dy * math.cos(ang)
        return abs(local_x) <= rx and abs(local_y) <= ry

    def _radial_rotate_handle_img_pos(self):
        x0, y0 = self._gradient_start
        x1, y1 = self._gradient_end
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        ry = abs(y1 - y0) / 2.0
        _, _, scale, _, _ = self._compute_display_geometry()
        gap_img = 30.0 / scale if scale else 30.0  # a constant 30 SCREEN px gap at any zoom level
        handle_dist = ry + gap_img
        ang = math.radians(self._radial_angle)
        return cx + handle_dist * math.sin(ang), cy - handle_dist * math.cos(ang)

    def _radial_rotate_handle_screen_pos(self):
        if self._gradient_start is None or self._gradient_end is None:
            return None
        return self._image_to_widget_pos(self._radial_rotate_handle_img_pos())

    def _radial_hit_test(self, screen_pos):
        handle = self._radial_rotate_handle_screen_pos()
        if handle is not None and math.hypot(screen_pos.x() - handle[0], screen_pos.y() - handle[1]) < self.HANDLE_HIT_PX:
            return "rotate"
        img_pos = self._widget_to_image_pos_unclamped(screen_pos)
        if img_pos is not None and self._radial_point_in_shape(img_pos):
            return "move"
        return None

    def _point_in_polygon(self, pt, points) -> bool:
        """cv2's own pointPolygonTest with measureDist=True returns the signed
        distance to the boundary (positive inside, negative outside) instead
        of a strict boolean -- accepting a small negative margin means a
        click just outside the polygon's edge still grabs it, the same
        generous-tolerance idea as the ellipse's bounding-box check above."""
        pts = np.array([[p[0], p[1]] for p in points], dtype=np.float32)
        _, _, scale, _, _ = self._compute_display_geometry()
        margin_img = self.LINE_HIT_PX / scale if scale else self.LINE_HIT_PX
        dist = cv2.pointPolygonTest(pts, (float(pt[0]), float(pt[1])), True)
        return dist >= -margin_img

    def _polygon_centroid(self):
        xs = [p[0] for p in self._polygon_points]
        ys = [p[1] for p in self._polygon_points]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def _polygon_rotate_handle_img_pos(self):
        cx, _ = self._polygon_centroid()
        top_y = min(p[1] for p in self._polygon_points)
        _, _, scale, _, _ = self._compute_display_geometry()
        gap_img = 30.0 / scale if scale else 30.0
        return cx, top_y - gap_img

    def _polygon_rotate_handle_screen_pos(self):
        if len(self._polygon_points) < 3:
            return None
        return self._image_to_widget_pos(self._polygon_rotate_handle_img_pos())

    def _polygon_hit_test(self, screen_pos):
        handle = self._polygon_rotate_handle_screen_pos()
        if handle is not None and math.hypot(screen_pos.x() - handle[0], screen_pos.y() - handle[1]) < self.HANDLE_HIT_PX:
            return "rotate"
        img_pos = self._widget_to_image_pos_unclamped(screen_pos)
        if img_pos is not None and self._point_in_polygon(img_pos, self._polygon_points):
            return "move"
        return None

    def _begin_grad_edit(self, handle, screen_pos):
        self._grad_active_handle = handle
        self._grad_drag_start_img = self._widget_to_image_pos_unclamped(screen_pos)
        if self._gradient_shape == "linear":
            self._grad_drag_anchor = (self._gradient_start, self._gradient_end)
        elif self._gradient_shape == "radial":
            self._grad_drag_anchor = (self._gradient_start, self._gradient_end, self._radial_angle)
        else:
            self._grad_drag_anchor = list(self._polygon_points)
        self.update()

    def _update_grad_edit(self, screen_pos):
        img_pos = self._widget_to_image_pos_unclamped(screen_pos)
        if img_pos is None or self._grad_drag_start_img is None:
            return
        handle = self._grad_active_handle
        dx_total = img_pos[0] - self._grad_drag_start_img[0]
        dy_total = img_pos[1] - self._grad_drag_start_img[1]

        if self._gradient_shape == "linear":
            anchor_s, anchor_e = self._grad_drag_anchor
            if handle == "start":
                self._gradient_start = img_pos
            elif handle == "end":
                self._gradient_end = img_pos
            elif handle == "move":
                self._gradient_start = (anchor_s[0] + dx_total, anchor_s[1] + dy_total)
                self._gradient_end = (anchor_e[0] + dx_total, anchor_e[1] + dy_total)
            self.gradientChanged.emit(
                self._gradient_start[0], self._gradient_start[1], self._gradient_end[0], self._gradient_end[1], False
            )

        elif self._gradient_shape == "radial":
            anchor_s, anchor_e, anchor_angle = self._grad_drag_anchor
            if handle == "move":
                self._gradient_start = (anchor_s[0] + dx_total, anchor_s[1] + dy_total)
                self._gradient_end = (anchor_e[0] + dx_total, anchor_e[1] + dy_total)
            elif handle == "rotate":
                cx, cy = (anchor_s[0] + anchor_e[0]) / 2, (anchor_s[1] + anchor_e[1]) / 2
                start_ang = math.degrees(math.atan2(
                    self._grad_drag_start_img[1] - cy, self._grad_drag_start_img[0] - cx
                ))
                cur_ang = math.degrees(math.atan2(img_pos[1] - cy, img_pos[0] - cx))
                self._radial_angle = anchor_angle + (cur_ang - start_ang)
            self.radialGradientChanged.emit(
                self._gradient_start[0], self._gradient_start[1], self._gradient_end[0], self._gradient_end[1], False
            )

        else:  # polygon
            anchor_pts = self._grad_drag_anchor
            if handle == "move":
                self._polygon_points = [(x + dx_total, y + dy_total) for x, y in anchor_pts]
            elif handle == "rotate":
                xs = [p[0] for p in anchor_pts]
                ys = [p[1] for p in anchor_pts]
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                start_ang = math.atan2(self._grad_drag_start_img[1] - cy, self._grad_drag_start_img[0] - cx)
                cur_ang = math.atan2(img_pos[1] - cy, img_pos[0] - cx)
                dtheta = cur_ang - start_ang
                cos_t, sin_t = math.cos(dtheta), math.sin(dtheta)
                self._polygon_points = [
                    (cx + (x - cx) * cos_t - (y - cy) * sin_t, cy + (x - cx) * sin_t + (y - cy) * cos_t)
                    for x, y in anchor_pts
                ]
            self.polygonGradientChanged.emit(list(self._polygon_points), False)

        self.update()

    # ------------------------------------------------------------ dragging

    def wheelEvent(self, event):
        if self._pixmap is None or self._pixmap.isNull():
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.0015 ** delta
        self.set_zoom(self._zoom * factor, center=event.position())
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_pan()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_drag_start = event.position()
            self._pan_drag_anchor = (self._pan_x, self._pan_y)
            self._pre_pan_cursor = self.cursor()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if self._ai_select_mode:
            img_pos = self._widget_to_image_pos(event.position())
            if img_pos is None:
                return
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                label, is_refine = 1, True
            elif modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                label, is_refine = 0, True
            else:
                label, is_refine = 1, False
            self.aiSelectClicked.emit(img_pos[0], img_pos[1], label, is_refine)
            return

        if self._brush_mode:
            if event.button() != Qt.MouseButton.LeftButton:
                return
            img_pos = self._widget_to_image_pos(event.position())
            if img_pos is None:
                return
            self.brushStroke.emit(img_pos[0], img_pos[1], True)
            return

        if self._gradient_mode:
            if event.button() != Qt.MouseButton.LeftButton:
                return
            if self._gradient_shape == "polygon":
                if self._polygon_closed:
                    handle = self._polygon_hit_test(event.position())
                    if handle is not None:
                        self._begin_grad_edit(handle, event.position())
                    else:
                        # A miss must NOT silently wipe an already-placed shape --
                        # that's what made the tool feel like a coin flip: any
                        # click that landed a few px outside the strict hit region
                        # (e.g. a corner of the ellipse's bounding box, or just
                        # past the tiny rotate handle) used to discard the whole
                        # shape and replace it with an invisible zero-effect point,
                        # with zero feedback that anything had gone wrong. Clear
                        # Selection is now the only way to deliberately start over.
                        self.gradHitMissed.emit()
                    return
                self._handle_polygon_click(event.position())
                return
            if self._gradient_start is not None and self._gradient_end is not None:
                hit_test = self._linear_hit_test if self._gradient_shape == "linear" else self._radial_hit_test
                handle = hit_test(event.position())
                if handle is not None:
                    self._begin_grad_edit(handle, event.position())
                else:
                    self.gradHitMissed.emit()
                return
            # no shape placed yet this session -- the first click always starts one
            img_pos = self._widget_to_image_pos_unclamped(event.position())
            if img_pos is None:
                return
            self._gradient_start = img_pos
            self._gradient_end = img_pos
            if self._gradient_shape == "radial":
                self._radial_angle = 0.0
            self.update()
            return

        if not self._crop_mode:
            return
        pos = event.position()
        self._active_handle = self._hit_test(pos)
        self._drag_start_pos = pos
        if self._active_handle is None:
            ox, oy = self._display_offset
            dw, dh = self._display_size
            if dw <= 0 or dh <= 0:
                return
            nx = min(max((pos.x() - ox) / dw, 0.0), 1.0)
            ny = min(max((pos.y() - oy) / dh, 0.0), 1.0)
            self._crop_rect = [nx, ny, nx, ny]
            self._active_handle = "se"
        self._drag_anchor_rect = list(self._crop_rect)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._pan_drag_start
            self._pan_x = self._pan_drag_anchor[0] + delta.x()
            self._pan_y = self._pan_drag_anchor[1] + delta.y()
            self._clamp_pan()
            self.update()
            return

        if self._brush_mode:
            img_pos = self._widget_to_image_pos(event.position())
            self._brush_cursor_pos = img_pos
            if img_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton):
                self.brushStroke.emit(img_pos[0], img_pos[1], False)
            self.update()
            return

        if self._gradient_mode:
            if self._grad_active_handle is not None and (event.buttons() & Qt.MouseButton.LeftButton):
                self._update_grad_edit(event.position())
                return
            if self._gradient_shape == "polygon":
                if not self._polygon_closed:
                    self._polygon_cursor_pos = self._widget_to_image_pos_unclamped(event.position())
                    self.update()
                return
            if self._gradient_start is not None and (event.buttons() & Qt.MouseButton.LeftButton):
                img_pos = self._widget_to_image_pos_unclamped(event.position())
                if img_pos is not None:
                    self._gradient_end = img_pos
                    signal = self.gradientChanged if self._gradient_shape == "linear" else self.radialGradientChanged
                    signal.emit(self._gradient_start[0], self._gradient_start[1], img_pos[0], img_pos[1], False)
                self.update()
            return

        if not self._crop_mode or self._active_handle is None or self._crop_rect is None:
            return
        pos = event.position()
        ox, oy = self._display_offset
        dw, dh = self._display_size
        if dw <= 0 or dh <= 0:
            return
        dx = (pos.x() - self._drag_start_pos.x()) / dw
        dy = (pos.y() - self._drag_start_pos.y()) / dh
        x0, y0, x1, y1 = self._drag_anchor_rect
        handle = self._active_handle

        if handle == "move":
            w_, h_ = x1 - x0, y1 - y0
            nx0 = min(max(x0 + dx, 0.0), 1.0 - w_)
            ny0 = min(max(y0 + dy, 0.0), 1.0 - h_)
            self._crop_rect = [nx0, ny0, nx0 + w_, ny0 + h_]
        else:
            nx0, ny0, nx1, ny1 = x0, y0, x1, y1
            if "w" in handle:
                nx0 = min(max(x0 + dx, 0.0), x1 - MIN_CROP_NORM)
            if "e" in handle:
                nx1 = max(min(x1 + dx, 1.0), x0 + MIN_CROP_NORM)
            if "n" in handle:
                ny0 = min(max(y0 + dy, 0.0), y1 - MIN_CROP_NORM)
            if "s" in handle:
                ny1 = max(min(y1 + dy, 1.0), y0 + MIN_CROP_NORM)

            ratio = self._resolve_ratio()
            if ratio:
                iw, ih = self._image_size()
                w_px, h_px = (nx1 - nx0) * iw, (ny1 - ny0) * ih
                if handle in ("n", "s"):
                    target_w = h_px * ratio
                    cx = (nx0 + nx1) / 2
                    nx0, nx1 = cx - (target_w / iw) / 2, cx + (target_w / iw) / 2
                elif handle in ("e", "w"):
                    target_h = w_px / ratio
                    cy = (ny0 + ny1) / 2
                    ny0, ny1 = cy - (target_h / ih) / 2, cy + (target_h / ih) / 2
                else:
                    target_h = w_px / ratio
                    if "n" in handle:
                        ny0 = ny1 - target_h / ih
                    else:
                        ny1 = ny0 + target_h / ih

            nx0, ny0 = max(0.0, nx0), max(0.0, ny0)
            nx1, ny1 = min(1.0, nx1), min(1.0, ny1)
            self._crop_rect = [nx0, ny0, nx1, ny1]

        self.update()
        self.cropPreviewChanged.emit()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(self._pre_pan_cursor if self._pre_pan_cursor is not None else Qt.CursorShape.ArrowCursor)
            return

        if self._gradient_mode and self._grad_active_handle is not None:
            self._grad_active_handle = None
            self._grad_drag_anchor = None
            self._grad_drag_start_img = None
            if self._gradient_shape == "linear":
                self.gradientChanged.emit(
                    self._gradient_start[0], self._gradient_start[1],
                    self._gradient_end[0], self._gradient_end[1], True,
                )
            elif self._gradient_shape == "radial":
                self.radialGradientChanged.emit(
                    self._gradient_start[0], self._gradient_start[1],
                    self._gradient_end[0], self._gradient_end[1], True,
                )
            else:
                self.polygonGradientChanged.emit(list(self._polygon_points), True)
            return

        if (
            self._gradient_mode
            and self._gradient_shape in ("linear", "radial")
            and self._gradient_start is not None
            and self._gradient_end is not None
        ):
            signal = self.gradientChanged if self._gradient_shape == "linear" else self.radialGradientChanged
            signal.emit(
                self._gradient_start[0], self._gradient_start[1],
                self._gradient_end[0], self._gradient_end[1], True,
            )
        self._active_handle = None
        self._drag_anchor_rect = None

    def mouseDoubleClickEvent(self, event):
        if (
            self._gradient_mode
            and self._gradient_shape == "polygon"
            and event.button() == Qt.MouseButton.LeftButton
            and not self._polygon_closed
        ):
            # the preceding press already added a vertex at ~this spot; a
            # double-click means "close now", not "place another point here"
            if len(self._polygon_points) > 3:
                self._polygon_points.pop()
            self._finalize_polygon()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self._gradient_mode and self._gradient_shape == "polygon" and not self._polygon_closed:
            if event.key() == Qt.Key.Key_Escape:
                self._polygon_points = []
                self._polygon_cursor_pos = None
                self.update()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and len(self._polygon_points) >= 3:
                self._finalize_polygon()
                return
        super().keyPressEvent(event)

    def _handle_polygon_click(self, screen_pos):
        # Only ever called while a polygon is still being placed (not yet
        # closed) -- mousePressEvent handles the already-closed case itself,
        # so there's no "discard the finished shape" branch needed here.
        img_pos = self._widget_to_image_pos_unclamped(screen_pos)
        if img_pos is None:
            return
        if len(self._polygon_points) >= 3:
            first_screen = self._image_to_widget_pos(self._polygon_points[0])
            if first_screen is not None and math.hypot(screen_pos.x() - first_screen[0], screen_pos.y() - first_screen[1]) < 10:
                self._finalize_polygon()
                return
        self._polygon_points.append(img_pos)
        self.update()

    def _finalize_polygon(self):
        if len(self._polygon_points) >= 3:
            self._polygon_closed = True
            self.polygonGradientChanged.emit(list(self._polygon_points), True)
        self.update()

    def leaveEvent(self, event):
        if self._brush_mode:
            self._brush_cursor_pos = None
            self.update()


class SegmentationThread(QThread):
    """Runs SAM2 point-prompted segmentation off the UI thread -- first-use
    model load (with a one-time weight download) and CPU-fallback inference
    can both take a couple of seconds, which would otherwise freeze the app."""

    result_ready = Signal(object)  # bool mask ndarray, or None on failure

    def __init__(self, image, points, labels):
        super().__init__()
        self.image = image
        self.points = points
        self.labels = labels

    def run(self):
        try:
            mask = segmentation.segment_from_points(self.image, self.points, self.labels)
        except Exception as exc:  # noqa: BLE001
            print(f"[ai-select] segmentation failed: {exc}")
            mask = None
        self.result_ready.emit(mask)


class AutoEnhanceThread(QThread):
    """Runs the local Auto Enhance model (see app/auto_enhance.py) off the UI
    thread -- first-use model load (an ~16GB one-time download, then loading
    those weights onto the GPU) and inference itself can both take a while,
    which would otherwise freeze the app."""

    result_ready = Signal(object)  # {"adjustments": {...}, "reasoning": str}

    def __init__(self, image):
        super().__init__()
        self.image = image

    def run(self):
        try:
            result = auto_enhance.suggest_adjustments(self.image)
        except Exception as exc:  # noqa: BLE001
            print(f"[auto-enhance] thread failed: {exc}")
            result = {"adjustments": {key: 0.0 for key in auto_enhance.RECIPE_FIELDS}, "reasoning": ""}
        self.result_ready.emit(result)


class PreviewRenderThread(QThread):
    """Persistent background worker that runs apply_recipe() off the UI
    thread. Rendering used to happen directly inside the debounce-timer
    callback on the UI thread; every slider tick during a drag would block
    Qt's event loop for the duration of the render (worse with denoise/
    sharpen active), so mouse-move events queued up behind it and the whole
    app felt laggy. Only ever keeps the single latest requested job -- if a
    new render is requested while one is still running, the stale one is
    simply replaced, never queued, so dragging stays smooth instead of
    catching up through a backlog of superseded renders."""

    resultReady = Signal(int, object)  # generation, rendered ndarray

    def __init__(self):
        super().__init__()
        self._condition = threading.Condition()
        self._job = None
        self._running = True

    def submit(self, source, recipe, generation):
        with self._condition:
            self._job = (source, recipe, generation)
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._running = False
            self._condition.notify()
        self.wait()

    def run(self):
        while True:
            with self._condition:
                while self._job is None and self._running:
                    self._condition.wait()
                if self._job is None and not self._running:
                    return
                source, recipe, generation = self._job
                self._job = None
            try:
                out = processing.apply_recipe(source, recipe)
            except Exception as exc:  # noqa: BLE001
                print(f"[render] failed: {exc}")
                continue
            self.resultReady.emit(generation, out)


LOCAL_SLIDER_SPECS = [
    ("exposure", "Exposure", -5.0, 5.0, 2),
    ("contrast", "Contrast", -100.0, 100.0, 0),
    ("highlights", "Highlights", -100.0, 100.0, 0),
    ("shadows", "Shadows", -100.0, 100.0, 0),
    ("temp", "Temp", -100.0, 100.0, 0),
    ("tint", "Tint", -100.0, 100.0, 0),
    ("hue", "Hue", -180.0, 180.0, 0),
    ("saturation", "Saturation", -100.0, 100.0, 0),
    ("sharpen", "Sharpen", 0.0, 100.0, 0),
]

SLIDER_GROUPS = [
    ("Light", [
        ("exposure", "Exposure", -5.0, 5.0, 2),
        ("contrast", "Contrast", -100.0, 100.0, 0),
        ("highlights", "Highlights", -100.0, 100.0, 0),
        ("shadows", "Shadows", -100.0, 100.0, 0),
        ("whites", "Whites", -100.0, 100.0, 0),
        ("blacks", "Blacks", -100.0, 100.0, 0),
    ]),
    ("Color", [
        ("temp", "Temp", -100.0, 100.0, 0),
        ("tint", "Tint", -100.0, 100.0, 0),
        ("saturation", "Saturation", -100.0, 100.0, 0),
        ("vibrance", "Vibrance", -100.0, 100.0, 0),
    ]),
    ("Channels", [
        ("red", "Red", -100.0, 100.0, 0),
        ("green", "Green", -100.0, 100.0, 0),
        ("blue", "Blue", -100.0, 100.0, 0),
    ]),
    ("Detail", [
        ("sharpen", "Sharpen", 0.0, 100.0, 0),
        ("denoise", "Denoise", 0.0, 100.0, 0),
    ]),
    ("Geometry", [
        ("rotate", "Straighten", -45.0, 45.0, 1),
    ]),
]

# Track colors for the sliders above (and the matching local-adjustment ones)
# so each control visually hints at what it does instead of every slider
# looking like flat OS gray -- a symmetric -N..N slider's track midpoint is
# always value 0, so a 3-stop gradient centered on a neutral tone lines up
# perfectly with "no adjustment" for free. Keys not listed here (Brush Size,
# Feather, ...) fall back to widgets.DEFAULT_SLIDER_GRADIENT.
SLIDER_COLORS = {
    # Tonal controls: dark <-> light, matching what turning them up/down does.
    "exposure": ["#000000", "#ffffff"],
    "contrast": ["#1a1a1a", "#f2f2f2"],
    "highlights": ["#1a1a1a", "#f2f2f2"],
    "shadows": ["#1a1a1a", "#f2f2f2"],
    "whites": ["#1a1a1a", "#f2f2f2"],
    "blacks": ["#1a1a1a", "#f2f2f2"],
    # White balance: the actual color axes they shift between.
    "temp": ["#4a90d9", "#3a3a3a", "#f5c542"],  # blue <-> yellow
    "tint": ["#4caf50", "#3a3a3a", "#d15fd1"],  # green <-> magenta
    # Hue rotates all the way around the color wheel, so its track is the
    # color wheel itself (red -> yellow -> green -> cyan -> blue -> magenta
    # -> back to red across the -180..180 range).
    "hue": ["#ff3b30", "#ffcc00", "#34c759", "#00c7be", "#0a84ff", "#bf5af2", "#ff3b30"],
    "saturation": ["#8e8e93", "#ff2d55"],  # gray (none) <-> vivid
    "vibrance": ["#8e8e93", "#ff9500"],
    # Channel gain sliders: dark-to-bright in that literal channel.
    "red": ["#2a0a0a", "#ff4040"],
    "green": ["#0a2a0a", "#40ff40"],
    "blue": ["#0a0a2a", "#4040ff"],
}


class EditorView(QWidget):
    backRequested = Signal()

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.settings = get_settings()
        self.photo_id = None
        self.path = None
        self.preview_source = None
        self._orig_dims = None  # true (full-res) (w, h), for crop dimension display
        self.recipe = processing.default_recipe()
        self.history = []
        self.history_index = -1
        self._show_original = False
        self.sliders = {}

        self.local_sliders = {}
        self.pending_points = []  # [(img_x, img_y, label), ...] at preview resolution
        self.pending_mask = None  # bool (AI select) or float32 0..1 (brush) array at preview resolution, or None
        self.pending_adjustments = processing.default_local_adjustments()
        self.editing_layer_index = None  # index into recipe['local_adjustments'] being re-edited, or None
        self.seg_thread = None
        self.auto_thread = None
        self._last_brush_pos = None  # (img_x, img_y) of the previous stroke point, for interpolation
        self._last_gradient_geom = None  # ("linear"|"radial", x0,y0,x1,y1) or ("polygon", points) -- lets
        # Invert/Feather changes recompute the current shape without needing a fresh drag

        self._render_generation = 0
        self.render_thread = PreviewRenderThread()
        self.render_thread.resultReady.connect(self._on_render_result)
        self.render_thread.start()

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_preview)

        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.timeout.connect(self._commit_history)

        main_layout = QVBoxLayout(self)
        main_layout.addLayout(self._build_toolbar())

        body = QHBoxLayout()

        info_panel = self._build_info_panel()
        info_panel.setFixedWidth(230)
        body.addWidget(info_panel, stretch=0)

        self.canvas = ImageCanvas()
        self.canvas.cropPreviewChanged.connect(self._on_crop_preview_changed)
        self.canvas.aiSelectClicked.connect(self._on_ai_select_clicked)
        self.canvas.brushStroke.connect(self._on_brush_stroke)
        self.canvas.gradientChanged.connect(self._on_gradient_changed)
        self.canvas.radialGradientChanged.connect(self._on_radial_changed)
        self.canvas.polygonGradientChanged.connect(self._on_polygon_changed)
        self.canvas.gradHitMissed.connect(self._on_grad_hit_missed)
        self.canvas.zoomChanged.connect(self._on_zoom_changed)
        body.addWidget(self.canvas, stretch=3)

        adjustment_scroll = self._build_adjustment_panel()
        adjustment_scroll.setMinimumWidth(380)
        adjustment_scroll.setMaximumWidth(420)
        body.addWidget(adjustment_scroll, stretch=0)

        main_layout.addLayout(body, stretch=1)

    # ---------------------------------------------------------------- toolbar

    def _build_toolbar(self):
        bar = QHBoxLayout()
        back_btn = QPushButton("← Library")
        back_btn.clicked.connect(self.backRequested.emit)
        bar.addWidget(back_btn)

        self.filename_label = QLabel("")
        bar.addWidget(self.filename_label)
        bar.addStretch(1)

        undo_btn = QPushButton("Undo")
        undo_btn.clicked.connect(self.undo)
        bar.addWidget(undo_btn)
        redo_btn = QPushButton("Redo")
        redo_btn.clicked.connect(self.redo)
        bar.addWidget(redo_btn)

        self.compare_btn = QPushButton("Show Original")
        self.compare_btn.setCheckable(True)
        self.compare_btn.toggled.connect(self._on_compare_toggle)
        bar.addWidget(self.compare_btn)

        zoom_out_btn = QPushButton("−")
        zoom_out_btn.setFixedWidth(28)
        zoom_out_btn.setToolTip("Zoom Out")
        zoom_out_btn.clicked.connect(lambda: self.canvas.zoom_out())
        bar.addWidget(zoom_out_btn)

        self.zoom_label = QPushButton("Fit")
        self.zoom_label.setFixedWidth(56)
        self.zoom_label.setToolTip("Click to reset to Fit")
        self.zoom_label.clicked.connect(lambda: self.canvas.zoom_to_fit())
        bar.addWidget(self.zoom_label)

        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(28)
        zoom_in_btn.setToolTip("Zoom In")
        zoom_in_btn.clicked.connect(lambda: self.canvas.zoom_in())
        bar.addWidget(zoom_in_btn)

        zoom_100_btn = QPushButton("100%")
        zoom_100_btn.setToolTip("Actual Size")
        zoom_100_btn.clicked.connect(lambda: self.canvas.zoom_to_actual())
        bar.addWidget(zoom_100_btn)

        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self.export_photo)
        bar.addWidget(export_btn)
        return bar

    # -------------------------------------------------------------- info panel

    def _build_info_panel(self):
        box = QGroupBox("Photo Info")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        self.info_filename = QLabel("")
        self.info_filename.setWordWrap(True)
        self.info_filename.setStyleSheet("font-weight: 600; font-size: 12px;")
        layout.addWidget(self.info_filename)

        self.info_camera = QLabel("")
        self.info_camera.setWordWrap(True)
        self.info_camera.setStyleSheet("color: #cccccc; font-size: 11px;")
        layout.addWidget(self.info_camera)

        self.info_lens = QLabel("")
        self.info_lens.setWordWrap(True)
        self.info_lens.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(self.info_lens)

        self.info_exposure = QLabel("")
        self.info_exposure.setWordWrap(True)
        self.info_exposure.setStyleSheet("color: #eeeeee; font-size: 13px; font-weight: 600; padding-top: 6px;")
        layout.addWidget(self.info_exposure)

        self.info_date = QLabel("")
        self.info_date.setWordWrap(True)
        self.info_date.setStyleSheet("color: #999999; font-size: 11px; padding-top: 4px;")
        layout.addWidget(self.info_date)

        self.info_dimensions = QLabel("")
        self.info_dimensions.setWordWrap(True)
        self.info_dimensions.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(self.info_dimensions)

        layout.addStretch(1)
        return box

    @staticmethod
    def _format_aperture(aperture: float) -> str:
        if abs(aperture - round(aperture)) < 0.05:
            return f"f/{round(aperture)}"
        return f"f/{aperture:.1f}"

    @staticmethod
    def _format_shutter(shutter: float) -> str:
        if shutter <= 0:
            return ""
        if shutter >= 1:
            whole = round(shutter)
            return f"{whole}s" if abs(shutter - whole) < 0.05 else f"{shutter:.1f}s"
        return f"1/{round(1.0 / shutter)}s"

    def _update_info_panel(self, path: str):
        summary = image_io.get_exif_summary(path)

        self.info_filename.setText(Path(path).name)
        self.info_camera.setText(summary.get("camera") or "Camera: unknown")

        lens = summary.get("lens")
        self.info_lens.setText(lens or "")
        self.info_lens.setVisible(bool(lens))

        parts = []
        if summary.get("aperture"):
            parts.append(self._format_aperture(summary["aperture"]))
        if summary.get("shutter"):
            parts.append(self._format_shutter(summary["shutter"]))
        if summary.get("iso"):
            parts.append(f"ISO {int(summary['iso'])}")
        if summary.get("focal_length"):
            parts.append(f"{summary['focal_length']:.0f}mm")
        self.info_exposure.setText("   ".join(parts) if parts else "No shooting data found")

        date = summary.get("date")
        self.info_date.setText(date or "")
        self.info_date.setVisible(bool(date))

        w, h = summary.get("width"), summary.get("height")
        fmt = "RAW" if image_io.is_raw(path) else "JPEG"
        self.info_dimensions.setText(f"{w} × {h} · {fmt}" if w and h else fmt)

    # ------------------------------------------------------- adjustment panel

    def _build_adjustment_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(6)

        self.histogram = widgets.HistogramWidget()
        layout.addWidget(self.histogram)

        self.auto_enhance_btn = QPushButton("Auto Enhance")
        self.auto_enhance_btn.setToolTip(
            "Let a local AI model look at this photo and suggest exposure, contrast, "
            "white balance, and color adjustments -- runs entirely on this machine, "
            "first use downloads the model (~16GB, one time)."
        )
        self.auto_enhance_btn.clicked.connect(self._on_auto_enhance_clicked)
        layout.addWidget(self.auto_enhance_btn)

        self.auto_enhance_status = QLabel("")
        self.auto_enhance_status.setWordWrap(True)
        self.auto_enhance_status.setStyleSheet("color: #999999; font-size: 11px;")
        self.auto_enhance_status.setVisible(False)
        layout.addWidget(self.auto_enhance_status)

        for title, specs in SLIDER_GROUPS:
            box = QGroupBox(title)
            vbox = QVBoxLayout(box)
            vbox.setContentsMargins(8, 6, 8, 6)
            vbox.setSpacing(2)
            for key, label, lo, hi, decimals in specs:
                slider = widgets.LabeledSlider(
                    label, lo, hi, self.recipe.get(key, 0.0), decimals, gradient=SLIDER_COLORS.get(key)
                )
                slider.valueChanged.connect(lambda v, k=key: self._on_adjustment_changed(k, v))
                vbox.addWidget(slider)
                self.sliders[key] = slider
            layout.addWidget(box)

        curves_box = QGroupBox("Curves")
        curves_layout = QVBoxLayout(curves_box)
        curves_layout.setContentsMargins(8, 6, 8, 6)
        self.curve_editor = widgets.CurveEditor()
        self.curve_editor.curveChanged.connect(self._on_curve_changed)
        curves_layout.addWidget(self.curve_editor)
        layout.addWidget(curves_box)

        crop_box = QGroupBox("Crop")
        crop_layout = QVBoxLayout(crop_box)
        crop_layout.setContentsMargins(8, 6, 8, 6)
        crop_layout.setSpacing(4)

        crop_row1 = QHBoxLayout()
        self.crop_btn = QPushButton("Crop")
        self.crop_btn.setCheckable(True)
        self.crop_btn.toggled.connect(self._on_crop_toggle)
        crop_row1.addWidget(self.crop_btn)
        self.ratio_combo = QComboBox()
        for label, _value in ASPECT_RATIOS:
            self.ratio_combo.addItem(label)
        self.ratio_combo.currentIndexChanged.connect(self._on_ratio_changed)
        crop_row1.addWidget(self.ratio_combo, stretch=1)
        crop_layout.addLayout(crop_row1)

        self.crop_info_label = QLabel("")
        self.crop_info_label.setStyleSheet("color: #999999; font-size: 11px;")
        crop_layout.addWidget(self.crop_info_label)

        reset_crop_btn = QPushButton("Reset Crop")
        reset_crop_btn.clicked.connect(self._reset_crop)
        crop_layout.addWidget(reset_crop_btn)

        geo_row = QHBoxLayout()
        rotate90_btn = QPushButton("Rotate 90°")
        rotate90_btn.clicked.connect(self._rotate_90)
        flip_h_btn = QPushButton("Flip H")
        flip_h_btn.clicked.connect(lambda: self._toggle_flip("flip_h"))
        flip_v_btn = QPushButton("Flip V")
        flip_v_btn.clicked.connect(lambda: self._toggle_flip("flip_v"))
        geo_row.addWidget(rotate90_btn)
        geo_row.addWidget(flip_h_btn)
        geo_row.addWidget(flip_v_btn)
        crop_layout.addLayout(geo_row)

        layout.addWidget(crop_box)
        layout.addWidget(self._build_ai_select_box())

        reset_btn = QPushButton("Reset All")
        reset_btn.clicked.connect(self._reset_all)
        layout.addWidget(reset_btn)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        return scroll

    def _build_ai_select_box(self):
        box = QGroupBox("Selection && Masking")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        tool_row = QHBoxLayout()
        self.ai_select_btn = QPushButton("AI Select")
        self.ai_select_btn.setCheckable(True)
        self.ai_select_btn.toggled.connect(self._on_ai_select_toggle)
        tool_row.addWidget(self.ai_select_btn)
        self.brush_btn = QPushButton("Brush")
        self.brush_btn.setCheckable(True)
        self.brush_btn.toggled.connect(self._on_brush_toggle)
        tool_row.addWidget(self.brush_btn)
        self.gradient_btn = QPushButton("Gradient")
        self.gradient_btn.setCheckable(True)
        self.gradient_btn.toggled.connect(self._on_gradient_toggle)
        tool_row.addWidget(self.gradient_btn)
        layout.addLayout(tool_row)

        # Deliberately doesn't call segmentation.device_description() here --
        # that imports torch and probes CUDA, which would otherwise happen on
        # every app launch instead of only when AI Select is actually used.
        self.ai_status_label = QLabel("Click a subject, or switch to Brush/Gradient to paint a mask")
        self.ai_status_label.setWordWrap(True)
        self.ai_status_label.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(self.ai_status_label)

        hint = QLabel(
            "AI Select: click a subject (person, animal, car, anything); shift-click adds, "
            "ctrl-click removes. Brush: drag to paint a mask by hand. Gradient: Linear drags a "
            "fade across the photo (e.g. darkening a sky); Radial drags out an oval fading from "
            "its center; Polygon clicks out a shape's corners (double-click, or click the first "
            "corner again, to close it). Invert flips which side of a Radial/Polygon gets the "
            "effect. Once placed, drag inside a shape to move it, or drag its rotate handle (the "
            "little circle on a stalk) to turn it -- Linear's two end dots can also be dragged "
            "individually to reposition/resize it. Hit Clear Selection to discard the current "
            "shape and start a new one."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(hint)

        brush_row = QHBoxLayout()
        self.brush_size_slider = widgets.LabeledSlider("Brush Size", 5.0, 250.0, 40.0, 0)
        self.brush_size_slider.valueChanged.connect(self.canvas.set_brush_radius)
        brush_row.addWidget(self.brush_size_slider, stretch=1)
        self.brush_erase_btn = QPushButton("Erase")
        self.brush_erase_btn.setCheckable(True)
        brush_row.addWidget(self.brush_erase_btn)
        layout.addLayout(brush_row)

        gradient_shape_row = QHBoxLayout()
        gradient_shape_row.addWidget(QLabel("Gradient Shape:"))
        self.gradient_shape_combo = QComboBox()
        self.gradient_shape_combo.addItems(["Linear", "Radial", "Polygon"])
        self.gradient_shape_combo.currentTextChanged.connect(self._on_gradient_shape_changed)
        gradient_shape_row.addWidget(self.gradient_shape_combo, stretch=1)
        self.gradient_invert_btn = QPushButton("Invert")
        self.gradient_invert_btn.setCheckable(True)
        self.gradient_invert_btn.toggled.connect(self._on_gradient_invert_toggled)
        gradient_shape_row.addWidget(self.gradient_invert_btn)
        layout.addLayout(gradient_shape_row)

        # Shared by Brush (its own stroke edge) and by the Radial/Polygon
        # gradient shapes (their shape's edge) -- irrelevant for Linear, which
        # has no separate feather concept since the drag length itself sets
        # how gradual the fade is.
        self.brush_feather_slider = widgets.LabeledSlider("Feather", 0.0, 100.0, 30.0, 0)
        self.brush_feather_slider.valueChanged.connect(self.canvas.set_brush_feather)
        self.brush_feather_slider.valueChanged.connect(self._on_shared_feather_changed)
        layout.addWidget(self.brush_feather_slider)

        for key, label, lo, hi, decimals in LOCAL_SLIDER_SPECS:
            slider = widgets.LabeledSlider(label, lo, hi, 0.0, decimals, gradient=SLIDER_COLORS.get(key))
            slider.valueChanged.connect(lambda v, k=key: self._on_local_adjustment_changed(k, v))
            layout.addWidget(slider)
            self.local_sliders[key] = slider

        layer_btn_row = QHBoxLayout()
        add_layer_btn = QPushButton("Add Layer")
        add_layer_btn.clicked.connect(self._commit_local_layer)
        layer_btn_row.addWidget(add_layer_btn)
        clear_btn = QPushButton("Clear Selection")
        clear_btn.clicked.connect(lambda: self._clear_pending_selection(re_render=True))
        layer_btn_row.addWidget(clear_btn)
        layout.addLayout(layer_btn_row)

        layout.addWidget(QLabel("Layers:"))
        self.layers_list = QListWidget()
        self.layers_list.setMaximumHeight(90)
        self.layers_list.currentRowChanged.connect(self._on_layer_selected)
        layout.addWidget(self.layers_list)

        delete_layer_btn = QPushButton("Delete Selected Layer")
        delete_layer_btn.clicked.connect(self._delete_selected_layer)
        layout.addWidget(delete_layer_btn)

        return box

    # ------------------------------------------------------------- open/close

    def open_photo(self, photo_id: int, path: str):
        self.photo_id = photo_id
        self.path = path
        self.filename_label.setText(Path(path).name)
        self._orig_dims = image_io.get_dimensions(path)
        self.preview_source = image_io.load_preview_float(path, max_dim=PREVIEW_MAX_DIM)
        self.canvas.reset_view()
        self._update_info_panel(path)

        recipe = self.catalog.get_edit_recipe(photo_id)
        merged = processing.default_recipe()
        if recipe:
            merged.update(recipe)
        self.recipe = merged
        self.history = [dict(merged)]
        self.history_index = 0

        # Reset crop UI state without going through _on_crop_toggle -- that
        # handler commits the canvas's current crop rect into self.recipe,
        # which at this point would be the *previous* photo's in-progress rect.
        self.crop_btn.blockSignals(True)
        self.crop_btn.setChecked(False)
        self.crop_btn.blockSignals(False)
        self.canvas.set_crop_rect(merged.get("crop"))

        # ai_select/brush/gradient reset is safe to do generically (unlike
        # crop above, none of them write into self.recipe on exit).
        for btn, mode_setter in (
            (self.ai_select_btn, self.canvas.set_ai_select_mode),
            (self.brush_btn, self.canvas.set_brush_mode),
            (self.gradient_btn, self.canvas.set_gradient_mode),
        ):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
            mode_setter(False)

        self._clear_pending_selection(re_render=False)
        self._refresh_layers_list()

        self._sync_sliders_from_recipe()
        self._render_preview()

    # ------------------------------------------------------------- rendering

    def _on_adjustment_changed(self, key, value):
        self.recipe[key] = value
        self._render_timer.start(30)
        self._history_timer.start(500)

    def _on_curve_changed(self, channel: str, points: list):
        # Reassign a new "curves" dict rather than mutating the existing one
        # in place -- _commit_history() only shallow-copies self.recipe, so
        # an in-place edit here would silently rewrite already-pushed history
        # entries too (they'd still be holding a reference to this same
        # nested dict), making undo/redo a no-op for curve changes.
        curves = dict(self.recipe.get("curves") or processing.default_curves())
        curves[channel] = points
        self.recipe["curves"] = curves
        self._render_timer.start(30)
        self._history_timer.start(500)

    def _update_histogram_displays(self, arr):
        """Feeds the same rendered array to both the standalone histogram
        panel and the curve editor's per-channel background histogram, so
        they never show data from two different renders."""
        self.histogram.set_image(arr)
        self.curve_editor.set_histograms(widgets.compute_log_histograms(arr))

    def _render_preview(self):
        if self.preview_source is None:
            return
        if self._show_original:
            # Trivial fast path -- no processing needed, no reason to bounce
            # through the background thread for this.
            self.canvas.set_pixmap(widgets.numpy_to_qpixmap(self.preview_source))
            self._update_histogram_displays(self.preview_source)
            return

        recipe_for_render = self.recipe
        if self.canvas.is_crop_mode():
            recipe_for_render = dict(self.recipe)
            recipe_for_render["crop"] = None
        self._render_generation += 1
        self.render_thread.submit(self.preview_source, recipe_for_render, self._render_generation)

    def _on_render_result(self, generation: int, out):
        if generation != self._render_generation:
            return  # superseded by a newer request while this one was rendering
        if (
            self.canvas.is_ai_select_mode() or self.canvas.is_brush_mode() or self.canvas.is_gradient_mode()
        ) and self.pending_mask is not None:
            out = processing.apply_local_layer(out, self.pending_mask.astype(np.float32), self.pending_adjustments)
            mask_for_overlay = self.pending_mask if self.pending_mask.dtype == bool else self.pending_mask > 0.3
            self.canvas.set_mask_overlay(mask_for_overlay)
        self.canvas.set_pixmap(widgets.numpy_to_qpixmap(out))
        self._update_histogram_displays(out)

    def _on_auto_enhance_clicked(self):
        if self.preview_source is None or (self.auto_thread is not None and self.auto_thread.isRunning()):
            return
        if not auto_enhance.is_model_loaded() and not self.settings.value(AUTO_ENHANCE_CONSENT, False, type=bool):
            choice = QMessageBox.question(
                self,
                "Download local AI model?",
                "Auto Enhance uses a local AI model (Qwen2.5-VL-7B, ~16GB) that runs "
                "entirely on this machine -- nothing about your photos is sent anywhere. "
                "The first use downloads and caches those model weights from Hugging Face "
                "(huggingface.co); after that it's instant to reuse.\n\n"
                "Download it now and enable Auto Enhance?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
            self.settings.setValue(AUTO_ENHANCE_CONSENT, True)
        self.auto_enhance_btn.setEnabled(False)
        self.auto_enhance_status.setText(
            "Analyzing…" if auto_enhance.is_model_loaded() else "Loading local AI model (first use only)…"
        )
        self.auto_enhance_status.setVisible(True)
        self.auto_thread = AutoEnhanceThread(self.preview_source)
        self.auto_thread.result_ready.connect(self._on_auto_enhance_result)
        self.auto_thread.start()

    def _on_auto_enhance_result(self, result):
        self.auto_enhance_btn.setEnabled(True)
        adjustments = result.get("adjustments") or {}
        reasoning = result.get("reasoning") or ""
        if not adjustments or not any(adjustments.values()):
            self.auto_enhance_status.setText(
                reasoning or "Auto Enhance couldn't suggest changes for this photo (see log for details)."
            )
            return
        for key, value in adjustments.items():
            self.recipe[key] = value
        self._sync_sliders_from_recipe()
        self._render_preview()
        self._commit_history()
        self.auto_enhance_status.setText(reasoning or "Adjustments applied.")

    def _on_compare_toggle(self, checked: bool):
        self._show_original = checked
        self._render_preview()

    def _on_zoom_changed(self, percent: float):
        self.zoom_label.setText("Fit" if self.canvas.is_at_fit_zoom() else f"{percent:.0f}%")

    # -------------------------------------------------------------- geometry

    def _rotate_90(self):
        self.recipe["rotate90"] = (int(self.recipe.get("rotate90", 0)) + 1) % 4
        self._render_preview()
        self._commit_history()

    def _toggle_flip(self, key: str):
        self.recipe[key] = not self.recipe.get(key, False)
        self._render_preview()
        self._commit_history()

    def _reset_all(self):
        self.recipe = processing.default_recipe()
        self._sync_sliders_from_recipe()
        self._clear_pending_selection(re_render=False)
        self._refresh_layers_list()
        self._render_preview()
        self._commit_history()

    # ------------------------------------------------------------------ crop

    def _deactivate_other_tools(self, keep: str | None):
        """Turns off whichever tool buttons aren't `keep`
        ('crop'|'ai_select'|'brush'|'gradient'|None), each running its normal
        exit side effect (crop commits its rect; the mask tools clear their
        pending selection) rather than just flipping the checkbox."""
        if keep != "crop" and self.crop_btn.isChecked():
            self.crop_btn.blockSignals(True)
            self.crop_btn.setChecked(False)
            self.crop_btn.blockSignals(False)
            rect = self.canvas.exit_crop_mode()
            self.recipe["crop"] = rect
            self._commit_history()
        if keep != "ai_select" and self.ai_select_btn.isChecked():
            self.ai_select_btn.blockSignals(True)
            self.ai_select_btn.setChecked(False)
            self.ai_select_btn.blockSignals(False)
            self.canvas.set_ai_select_mode(False)
            self._clear_pending_selection(re_render=False)
        if keep != "brush" and self.brush_btn.isChecked():
            self.brush_btn.blockSignals(True)
            self.brush_btn.setChecked(False)
            self.brush_btn.blockSignals(False)
            self.canvas.set_brush_mode(False)
            self._clear_pending_selection(re_render=False)
        if keep != "gradient" and self.gradient_btn.isChecked():
            self.gradient_btn.blockSignals(True)
            self.gradient_btn.setChecked(False)
            self.gradient_btn.blockSignals(False)
            self.canvas.set_gradient_mode(False)
            self._clear_pending_selection(re_render=False)

    def _on_crop_toggle(self, checked: bool):
        if checked:
            self._deactivate_other_tools("crop")
            self.canvas.enter_crop_mode(self.recipe.get("crop"))
        else:
            rect = self.canvas.exit_crop_mode()
            self.recipe["crop"] = rect
            self._commit_history()
        self._render_preview()
        self._on_crop_preview_changed()

    def _on_ratio_changed(self, idx: int):
        _label, value = ASPECT_RATIOS[idx]
        self.canvas.set_aspect_ratio(value)

    def _reset_crop(self):
        self.canvas.clear_crop()
        if not self.canvas.is_crop_mode():
            self.recipe["crop"] = None
            self._render_preview()
            self._commit_history()

    def _true_frame_size(self):
        """True (full, non-preview) pixel size of the image after rotate90 +
        straighten, before crop -- i.e. what the crop rect's fractions
        actually multiply out to at export time."""
        if self._orig_dims is None:
            return None
        w, h = self._orig_dims
        if int(self.recipe.get("rotate90", 0)) % 2 == 1:
            w, h = h, w
        angle = self.recipe.get("rotate", 0.0)
        if angle:
            w, h = processing.largest_inscribed_rect(w, h, math.radians(angle))
        return w, h

    def _on_crop_preview_changed(self):
        rect = self.canvas.get_crop_rect()
        frame = self._true_frame_size()
        if not rect or not frame:
            self.crop_info_label.setText("")
            return
        fw, fh = frame
        x0, y0, x1, y1 = rect
        w, h = round((x1 - x0) * fw), round((y1 - y0) * fh)
        self.crop_info_label.setText(f"{w} × {h}  ({self._format_ratio(w, h)})")

    @staticmethod
    def _format_ratio(w: int, h: int) -> str:
        if h <= 0 or w <= 0:
            return "-"
        g = math.gcd(w, h)
        key = (w // g, h // g)
        if key in RATIO_LABELS:
            return RATIO_LABELS[key]
        return f"{w / h:.2f}:1"

    # -------------------------------------------------------------- AI select

    def _on_ai_select_toggle(self, checked: bool):
        if checked:
            self._deactivate_other_tools("ai_select")
        self.canvas.set_ai_select_mode(checked)
        if checked:
            # First real touch of the feature -- fine to pay the torch/CUDA
            # probe cost here, unlike at panel-construction time.
            self.ai_status_label.setText(f"Ready — {segmentation.device_description()}")
        else:
            self._clear_pending_selection(re_render=False)
        self._render_preview()
        self._on_crop_preview_changed()

    def _on_brush_toggle(self, checked: bool):
        if checked:
            self._deactivate_other_tools("brush")
        self.canvas.set_brush_mode(checked)
        if checked:
            self.canvas.set_brush_radius(self.brush_size_slider.value())
            self.canvas.set_brush_feather(self.brush_feather_slider.value())
            self.ai_status_label.setText("Drag to paint a mask. Adjust size/feather above; check Erase to subtract.")
            self._last_brush_pos = None
        else:
            self._clear_pending_selection(re_render=False)
        self._render_preview()
        self._on_crop_preview_changed()

    def _on_gradient_toggle(self, checked: bool):
        if checked:
            self._deactivate_other_tools("gradient")
        self.canvas.set_gradient_mode(checked)
        if checked:
            shape = self.gradient_shape_combo.currentText().lower()
            self.canvas.set_gradient_shape(shape)
            self.ai_status_label.setText(self._gradient_status_text(shape))
        else:
            self._clear_pending_selection(re_render=False)
        self._render_preview()
        self._on_crop_preview_changed()

    @staticmethod
    def _gradient_status_text(shape: str) -> str:
        return {
            "linear": "Drag across the photo: full effect where you start, fading out where you release.",
            "radial": "Drag out an oval: full effect at the center, fading out to the edge.",
            "polygon": "Click to place corners; double-click, or click the first corner again, to close the shape.",
        }.get(shape, "")

    def _on_gradient_shape_changed(self, text: str):
        shape = text.lower()
        self.canvas.set_gradient_shape(shape)
        self.pending_mask = None
        self._last_gradient_geom = None
        self.canvas.set_mask_overlay(None)
        self._render_preview()
        if self.gradient_btn.isChecked():
            self.ai_status_label.setText(self._gradient_status_text(shape))

    def _on_gradient_invert_toggled(self, checked: bool):
        self._recompute_gradient_mask()
        self._render_preview()

    def _on_shared_feather_changed(self, _value: float):
        self._recompute_gradient_mask()
        self._render_preview()

    def _on_grad_hit_missed(self):
        # Nothing was changed -- just tell the user why the click didn't seem
        # to do anything, since the whole point of this signal is to no
        # longer silently discard their shape on a near-miss click.
        self.ai_status_label.setText(
            "That click missed the shape -- drag inside it to move, drag its "
            "handle to rotate/resize, or hit Clear Selection to start a new one."
        )

    def _on_brush_stroke(self, img_x: float, img_y: float, is_new_stroke: bool):
        if self.preview_source is None:
            return
        if self.pending_mask is None:
            h, w = self.preview_source.shape[:2]
            self.pending_mask = np.zeros((h, w), dtype=np.float32)
        elif self.pending_mask.dtype != np.float32:
            self.pending_mask = self.pending_mask.astype(np.float32)

        erase = self.brush_erase_btn.isChecked()
        radius = self.brush_size_slider.value()

        if is_new_stroke or self._last_brush_pos is None:
            self._stamp_brush(img_x, img_y, radius, erase)
        else:
            lx, ly = self._last_brush_pos
            dist = math.hypot(img_x - lx, img_y - ly)
            step = max(radius * 0.25, 2.0)
            steps = max(1, int(dist / step))
            for i in range(1, steps + 1):
                t = i / steps
                self._stamp_brush(lx + (img_x - lx) * t, ly + (img_y - ly) * t, radius, erase)

        self._last_brush_pos = (img_x, img_y)
        self.canvas.set_mask_overlay(self.pending_mask > 0.3)
        self._render_timer.start(16)

    def _stamp_brush(self, cx: float, cy: float, radius: float, erase: bool):
        h, w = self.pending_mask.shape
        x0 = max(0, int(cx - radius))
        x1 = min(w, int(cx + radius) + 1)
        y0 = max(0, int(cy - radius))
        y1 = min(h, int(cy + radius) + 1)
        if x1 <= x0 or y1 <= y0:
            return
        yy, xx = np.mgrid[y0:y1, x0:x1]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        feather_pct = self.brush_feather_slider.value() if hasattr(self, "brush_feather_slider") else 30.0
        feather = max(radius * (feather_pct / 100.0), 1.0)
        stamp = np.clip((radius - dist) / feather, 0.0, 1.0)
        region = self.pending_mask[y0:y1, x0:x1]
        if erase:
            self.pending_mask[y0:y1, x0:x1] = np.clip(region - stamp, 0.0, 1.0)
        else:
            self.pending_mask[y0:y1, x0:x1] = np.maximum(region, stamp)

    # ------------------------------------------------------------- gradient

    def _on_gradient_changed(self, x0: float, y0: float, x1: float, y1: float, is_final: bool):
        if self.preview_source is None:
            return
        self._last_gradient_geom = ("linear", x0, y0, x1, y1)
        self._recompute_gradient_mask()
        self._render_timer.start(16)

    def _on_radial_changed(self, x0: float, y0: float, x1: float, y1: float, is_final: bool):
        if self.preview_source is None:
            return
        self._last_gradient_geom = ("radial", x0, y0, x1, y1, self.canvas.get_radial_angle())
        self._recompute_gradient_mask()
        self._render_timer.start(16)

    def _on_polygon_changed(self, points, is_final: bool):
        # This only ever fires with >=3 points: either the shape just closed
        # (is_final=True), or an already-closed shape is being moved/rotated
        # (is_final=False on every step of that drag, True on release) -- a
        # <3-vertex in-progress polygon never reaches here at all, since the
        # canvas only emits this signal on close/move/rotate, never on a
        # plain vertex-placement click.
        if self.preview_source is None or len(points) < 3:
            return
        self._last_gradient_geom = ("polygon", list(points))
        self._recompute_gradient_mask()
        self._render_timer.start(16)

    def _recompute_gradient_mask(self):
        """Rebuilds pending_mask from the last-placed gradient shape --
        called both while dragging a shape and when Feather/Invert change
        without a fresh drag, so those controls take effect immediately."""
        if self.preview_source is None or self._last_gradient_geom is None:
            return
        h, w = self.preview_source.shape[:2]
        feather_pct = self.brush_feather_slider.value()
        kind = self._last_gradient_geom[0]
        if kind == "linear":
            _, x0, y0, x1, y1 = self._last_gradient_geom
            mask = self._compute_gradient_mask(x0, y0, x1, y1, (h, w))
        elif kind == "radial":
            _, x0, y0, x1, y1, angle = self._last_gradient_geom
            mask = self._compute_radial_mask(x0, y0, x1, y1, feather_pct, angle, (h, w))
        else:
            _, points = self._last_gradient_geom
            mask = self._compute_polygon_mask(points, feather_pct, (h, w))
        if self.gradient_invert_btn.isChecked():
            mask = 1.0 - mask
        self.pending_mask = mask
        self.canvas.set_mask_overlay(mask > 0.5)

    @staticmethod
    def _compute_gradient_mask(x0: float, y0: float, x1: float, y1: float, shape) -> np.ndarray:
        """Linear fade: 1.0 at (x0,y0), 0.0 at (x1,y1), constant along the
        perpendicular -- the same construction as a Lightroom Graduated
        Filter. Points may lie outside the image; the projection/clip below
        handles that naturally (the fade just runs off the edge)."""
        h, w = shape
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-6:
            return np.zeros((h, w), dtype=np.float32)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        t = ((xx - x0) * dx + (yy - y0) * dy) / length_sq
        t = np.clip(t, 0.0, 1.0)
        return (1.0 - t).astype(np.float32)

    @staticmethod
    def _compute_radial_mask(
        x0: float, y0: float, x1: float, y1: float, feather_pct: float, angle_deg: float, shape
    ) -> np.ndarray:
        """Elliptical fade inscribed in the box (x0,y0)-(x1,y1): full effect
        at the center, fading out to the box edge over `feather_pct` of the
        ellipse's own radius -- 0% is a hard-edged ellipse, 100% fades
        smoothly all the way from center to edge (a Lightroom Radial Filter,
        default un-inverted so the effect concentrates on what you circled).
        `angle_deg` rotates the ellipse's own axes around its center without
        changing its size/aspect, matching the canvas's rotate-handle drag."""
        h, w = shape
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx, ry = abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
        if rx < 1e-3 or ry < 1e-3:
            return np.zeros((h, w), dtype=np.float32)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        dx, dy = xx - cx, yy - cy
        if abs(angle_deg) > 1e-6:
            ang = math.radians(angle_deg)
            cos_a, sin_a = math.cos(ang), math.sin(ang)
            # rotate world offsets back into the ellipse's own (unrotated) frame
            local_x = dx * cos_a + dy * sin_a
            local_y = -dx * sin_a + dy * cos_a
        else:
            local_x, local_y = dx, dy
        r = np.sqrt((local_x / rx) ** 2 + (local_y / ry) ** 2)
        feather = max(1e-3, feather_pct / 100.0)
        return np.clip((1.0 - r) / feather, 0.0, 1.0).astype(np.float32)

    @staticmethod
    def _compute_polygon_mask(points, feather_pct: float, shape) -> np.ndarray:
        """Fills the closed polygon solid, then feathers the edge in/out by
        `feather_pct` of the shape's own half-diagonal via a signed distance
        transform -- 0% is a hard-edged cutout, 100% feathers deep into the
        interior and well past the boundary."""
        h, w = shape
        if len(points) < 3:
            return np.zeros((h, w), dtype=np.float32)
        pts = np.array([[p[0], p[1]] for p in points], dtype=np.int32)
        inside = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(inside, [pts], 1)

        xs, ys = [p[0] for p in points], [p[1] for p in points]
        half_diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2.0
        feather_px = max(1.0, (feather_pct / 100.0) * half_diag)

        dist_in = cv2.distanceTransform(inside, cv2.DIST_L2, 5)
        dist_out = cv2.distanceTransform(1 - inside, cv2.DIST_L2, 5)
        signed = dist_in - dist_out  # >0 inside, <0 outside, 0 exactly at the boundary
        return np.clip(signed / feather_px * 0.5 + 0.5, 0.0, 1.0).astype(np.float32)

    def _on_ai_select_clicked(self, img_x: float, img_y: float, label: int, is_refine: bool):
        if not is_refine:
            self.pending_points = []
        self.pending_points.append((img_x, img_y, int(label)))
        self.canvas.set_points_overlay(self.pending_points)
        self._run_segmentation()

    def _segmentation_source_frame(self):
        """Image exactly matching what the canvas currently displays (post
        geometry: rotate90/straighten/flip/crop, plus already-committed local
        layers), since AI-select click coordinates are computed relative to
        that displayed frame's pixel dimensions, not the raw preview_source.
        Segmenting against a mismatched frame is what made selections look
        misaligned/imprecise whenever a crop or straighten was applied first."""
        if self.preview_source is None:
            return None
        return processing.apply_recipe(self.preview_source, self.recipe)

    def _run_segmentation(self):
        if self.preview_source is None or not self.pending_points:
            return
        points = [(p[0], p[1]) for p in self.pending_points]
        labels = [p[2] for p in self.pending_points]
        segmentation_source = self._segmentation_source_frame()
        if segmentation_source is None:
            return
        if not segmentation.is_model_loaded():
            self.ai_status_label.setText("Loading AI model (first use downloads ~150MB)…")
        else:
            self.ai_status_label.setText("Segmenting…")
        if self.seg_thread is not None and self.seg_thread.isRunning():
            self.seg_thread.wait()
        self.seg_thread = SegmentationThread(segmentation_source, points, labels)
        self.seg_thread.result_ready.connect(self._on_segmentation_result)
        self.seg_thread.start()

    def _on_segmentation_result(self, mask):
        if mask is None:
            self.ai_status_label.setText("Selection failed -- try clicking again")
            return
        self.pending_mask = mask
        self.canvas.set_mask_overlay(mask)
        device = "GPU" if segmentation.is_gpu_active() else "CPU"
        self.ai_status_label.setText(f"{device} -- {int(mask.sum())} px selected. Adjust below, then Add Layer.")
        self._render_preview()

    def _on_local_adjustment_changed(self, key, value):
        self.pending_adjustments[key] = value
        self._render_timer.start(30)

    def _sync_local_sliders(self):
        for key, widget in self.local_sliders.items():
            widget.set_value(self.pending_adjustments.get(key, 0.0))

    def _clear_pending_selection(self, re_render: bool = True):
        self.pending_points = []
        self.pending_mask = None
        self._last_brush_pos = None
        self._last_gradient_geom = None
        self.pending_adjustments = processing.default_local_adjustments()
        self.editing_layer_index = None
        self._sync_local_sliders()
        self.canvas.set_points_overlay([])
        self.canvas.set_mask_overlay(None)
        if self.canvas.is_gradient_mode():
            # Also drop the placed shape's own geometry (linear points, radial
            # box/angle, polygon vertices) -- without this, clicking Clear
            # Selection reset the mask/sliders but left the old shape's
            # geometry in place, so the very next click would just hit-test
            # against that stale shape again instead of truly starting fresh.
            self.canvas.set_gradient_shape(self.canvas._gradient_shape)
        if hasattr(self, "ai_status_label"):
            self.ai_status_label.setText("Click a subject, or switch to Brush to paint a mask")
        if re_render:
            self._render_preview()

    def _commit_local_layer(self):
        if self.pending_mask is None:
            return
        encoded = processing.encode_mask(self.pending_mask)
        layer = {"mask": encoded, "adjustments": dict(self.pending_adjustments)}
        layers = list(self.recipe.get("local_adjustments", []))
        if self.editing_layer_index is not None and 0 <= self.editing_layer_index < len(layers):
            layers[self.editing_layer_index] = layer
        else:
            layers.append(layer)
        self.recipe["local_adjustments"] = layers
        self._commit_history()
        self._refresh_layers_list()
        self._clear_pending_selection(re_render=True)

    def _refresh_layers_list(self):
        self.layers_list.blockSignals(True)
        self.layers_list.clear()
        for i, _layer in enumerate(self.recipe.get("local_adjustments", [])):
            self.layers_list.addItem(f"Mask {i + 1}")
        self.layers_list.blockSignals(False)

    def _on_layer_selected(self, row: int):
        layers = self.recipe.get("local_adjustments", [])
        if row < 0 or row >= len(layers) or self.preview_source is None:
            return
        layer = layers[row]
        frame = self._segmentation_source_frame()  # masks live in the post-geometry frame, not raw preview_source
        if frame is None:
            return
        self.pending_mask = processing.decode_mask(layer["mask"], frame.shape[:2]) > 0.5
        self.pending_adjustments = dict(processing.default_local_adjustments())
        self.pending_adjustments.update(layer.get("adjustments", {}))
        self.editing_layer_index = row
        self.pending_points = []
        self._sync_local_sliders()
        self.canvas.set_points_overlay([])
        if not self.ai_select_btn.isChecked():
            self.ai_select_btn.setChecked(True)
        self.canvas.set_mask_overlay(self.pending_mask)
        self.ai_status_label.setText(f"Editing Mask {row + 1} -- adjust below, then Add Layer to update")
        self._render_preview()

    def _delete_selected_layer(self):
        row = self.layers_list.currentRow()
        layers = list(self.recipe.get("local_adjustments", []))
        if row < 0 or row >= len(layers):
            return
        del layers[row]
        self.recipe["local_adjustments"] = layers
        self._commit_history()
        self._refresh_layers_list()
        self._clear_pending_selection(re_render=True)

    # -------------------------------------------------------------- history

    def _sync_sliders_from_recipe(self):
        for key, widget in self.sliders.items():
            widget.set_value(self.recipe.get(key, 0.0))
        self.curve_editor.set_curves(self.recipe.get("curves") or processing.default_curves())

    def _commit_history(self):
        if self.photo_id is None:
            return
        if self.history_index >= 0 and self.history[self.history_index] == self.recipe:
            return
        self.history = self.history[: self.history_index + 1]
        self.history.append(dict(self.recipe))
        self.history_index = len(self.history) - 1
        self.catalog.set_edit_recipe(self.photo_id, self.recipe)

    def undo(self):
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self.recipe = dict(self.history[self.history_index])
        self._sync_sliders_from_recipe()
        self._clear_pending_selection(re_render=False)
        self._refresh_layers_list()
        self._render_preview()
        if self.photo_id is not None:
            self.catalog.set_edit_recipe(self.photo_id, self.recipe)

    def redo(self):
        if self.history_index >= len(self.history) - 1:
            return
        self.history_index += 1
        self.recipe = dict(self.history[self.history_index])
        self._sync_sliders_from_recipe()
        self._clear_pending_selection(re_render=False)
        self._refresh_layers_list()
        self._render_preview()
        if self.photo_id is not None:
            self.catalog.set_edit_recipe(self.photo_id, self.recipe)

    # --------------------------------------------------------------- export

    def export_photo(self):
        if self.path is None:
            return
        default_name = Path(self.path).stem + "_edited.jpg"
        last_dir = self.settings.value(LAST_EXPORT_FOLDER, str(Path(self.path).parent))
        initial_path = str(Path(last_dir) / default_name)
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export photo", initial_path, "JPEG (*.jpg *.jpeg);;PNG (*.png);;TIFF (*.tif *.tiff)"
        )
        if not out_path:
            return
        full = image_io.load_image_float(self.path)
        processed = processing.apply_recipe(full, self.recipe)
        try:
            image_io.save_image(processed, out_path, quality=92)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.settings.setValue(LAST_EXPORT_FOLDER, str(Path(out_path).parent))
        QMessageBox.information(self, "Export complete", f"Saved to {out_path}")
