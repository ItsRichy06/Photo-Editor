"""Small reusable widgets: image conversion helpers, star rating, labeled slider."""

import math

import numpy as np
from PIL import Image
from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import processing


def pil_to_qpixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


def numpy_to_qpixmap(arr: np.ndarray) -> QPixmap:
    arr8 = np.ascontiguousarray((np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8))
    h, w = arr8.shape[:2]
    qimg = QImage(arr8.data, w, h, w * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


class StarRatingWidget(QWidget):
    ratingChanged = Signal(int)

    def __init__(self, rating: int = 0, star_size: int = 16, parent=None):
        super().__init__(parent)
        self._rating = rating
        self._star_size = star_size
        self._hover = -1
        self.setFixedSize(star_size * 5, star_size)
        self.setMouseTracking(True)

    def rating(self) -> int:
        return self._rating

    def set_rating(self, rating: int):
        self._rating = max(0, min(5, rating))
        self.update()

    def mousePressEvent(self, event):
        idx = int(event.position().x() // self._star_size) + 1
        idx = max(0, min(5, idx))
        self._rating = 0 if idx == self._rating else idx
        self.update()
        self.ratingChanged.emit(self._rating)

    def mouseMoveEvent(self, event):
        idx = int(event.position().x() // self._star_size) + 1
        self._hover = max(0, min(5, idx))
        self.update()

    def leaveEvent(self, event):
        self._hover = -1
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        display = self._hover if self._hover >= 0 else self._rating
        for i in range(5):
            filled = i < display
            color = QColor("#f5b301") if filled else QColor("#999999")
            painter.setPen(color)
            painter.setBrush(color if filled else QColor(0, 0, 0, 0))
            cx = i * self._star_size + self._star_size / 2
            cy = self._star_size / 2
            self._draw_star(painter, cx, cy, self._star_size * 0.42)

    @staticmethod
    def _draw_star(painter: QPainter, cx: float, cy: float, r: float):
        points = []
        for i in range(10):
            angle = math.pi / 2 + i * math.pi / 5
            rad = r if i % 2 == 0 else r * 0.45
            points.append(QPointF(cx + rad * math.cos(angle), cy - rad * math.sin(angle)))
        painter.drawPolygon(QPolygonF(points))


# Default track color for sliders with no specific semantic mapping (Sharpen,
# Denoise, Brush Size, ...) -- the same accent blue used for handles/overlays
# everywhere else on the canvas, so an "uncolored" slider still looks
# deliberate rather than just falling back to flat OS gray.
DEFAULT_SLIDER_GRADIENT = ["#2a2a2a", "#33ccff"]


class LabeledSlider(QWidget):
    valueChanged = Signal(float)

    def __init__(self, label: str, min_val: float, max_val: float, value: float = 0.0,
                 decimals: int = 0, parent=None, gradient: list[str] | None = None):
        super().__init__(parent)
        self._scale = 10 ** decimals
        self._updating = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.name_label = QLabel(label)
        self.name_label.setFixedWidth(90)
        layout.addWidget(self.name_label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(int(round(min_val * self._scale)))
        self.slider.setMaximum(int(round(max_val * self._scale)))
        self.slider.setValue(int(round(value * self._scale)))
        self._style_slider(gradient or DEFAULT_SLIDER_GRADIENT)
        layout.addWidget(self.slider, stretch=1)

        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setMinimum(min_val)
        self.spin.setMaximum(max_val)
        self.spin.setValue(value)
        # Qt's default spinbox step is 1.0 regardless of decimals -- fine for
        # whole-number sliders, but way too coarse for e.g. Exposure (a full
        # EV stop per arrow click). Anything with decimal precision steps by
        # 0.1 instead.
        self.spin.setSingleStep(0.1 if decimals > 0 else 1.0)
        self.spin.setMinimumWidth(92)
        self.spin.setMaximumWidth(92)
        self.spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.spin)

        self.slider.valueChanged.connect(self._on_slider_changed)
        self.spin.valueChanged.connect(self._on_spin_changed)

    def _style_slider(self, colors: list[str]):
        """Paints the groove as a gradient spanning the slider's full
        min..max range (not just up to the handle) -- for a symmetric
        -N..N slider, a 3-color gradient centered on a neutral tone lines
        the visually "neutral" middle color up with value 0 for free, since
        the track's midpoint is 0 on any symmetric range. A plain OS-gray
        groove doesn't hint at all which direction does what; this does."""
        if len(colors) < 2:
            colors = colors * 2
        stops = ", ".join(f"stop:{i / (len(colors) - 1):.4f} {c}" for i, c in enumerate(colors))
        self.slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px;
                border-radius: 2px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, {stops});
            }}
            QSlider::handle:horizontal {{
                background: #ffffff;
                border: 1px solid rgba(0, 0, 0, 140);
                width: 14px;
                height: 14px;
                margin: -6px 0;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #f2f2f2;
                border: 1px solid #33ccff;
            }}
            QSlider::handle:horizontal:pressed {{
                background: #33ccff;
                border: 1px solid #33ccff;
            }}
        """)

    def _on_slider_changed(self, int_value):
        if self._updating:
            return
        self._updating = True
        value = int_value / self._scale
        self.spin.setValue(value)
        self._updating = False
        self.valueChanged.emit(value)

    def _on_spin_changed(self, value):
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(int(round(value * self._scale)))
        self._updating = False
        self.valueChanged.emit(value)

    def value(self) -> float:
        return self.spin.value()

    def set_value(self, value: float):
        self._updating = True
        self.spin.setValue(value)
        self.slider.setValue(int(round(value * self._scale)))
        self._updating = False


def compute_log_histograms(arr: np.ndarray) -> dict:
    """Per-channel + luminance histogram counts (256 bins, log-scaled so a
    tall spike like a solid sky doesn't flatten the rest), keyed to match
    CURVE_CHANNELS' "rgb"/"red"/"green"/"blue". NOT normalized to a peak --
    shared raw data for HistogramWidget and CurveEditor, which each want a
    different normalization (see their own set_image()/set_histograms())."""
    img8 = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
    r = np.bincount(img8[..., 0].ravel(), minlength=256).astype(np.float32)
    g = np.bincount(img8[..., 1].ravel(), minlength=256).astype(np.float32)
    b = np.bincount(img8[..., 2].ravel(), minlength=256).astype(np.float32)
    luminance = (
        0.299 * img8[..., 0].astype(np.float32)
        + 0.587 * img8[..., 1].astype(np.float32)
        + 0.114 * img8[..., 2].astype(np.float32)
    ).astype(np.uint8)
    lum = np.bincount(luminance.ravel(), minlength=256).astype(np.float32)
    return {"rgb": np.log1p(lum), "red": np.log1p(r), "green": np.log1p(g), "blue": np.log1p(b)}


class HistogramWidget(QWidget):
    """Live histogram of the currently rendered preview image, drawn as two
    separate charts (each normalized to its own peak) rather than one
    overlaid chart, so the exposure curve doesn't get visually buried under
    the more saturated R/G/B fills: overall exposure/luminance on top,
    R/G/B channels below."""

    LABEL_H = 14
    GAP = 6
    CAPTION_H = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160 + self.CAPTION_H)
        self._channels = None  # (r, g, b) each a length-256 float array normalized 0..1
        self._luminance = None  # length-256 float array normalized 0..1

    def set_image(self, arr: np.ndarray):
        hist = compute_log_histograms(arr)
        r, g, b, lum_counts = hist["red"], hist["green"], hist["blue"], hist["rgb"]
        # Each chart normalizes against its own peak -- otherwise a much
        # taller luminance peak would flatten the RGB curves or vice versa.
        rgb_peak = max(r.max(), g.max(), b.max(), 1e-6)
        self._channels = (r / rgb_peak, g / rgb_peak, b / rgb_peak)
        self._luminance = lum_counts / max(lum_counts.max(), 1e-6)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#181818"))

        w = self.width()
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        # Always drawn, even before a photo is open -- otherwise this whole
        # widget is just empty black with no indication of what it's for.
        caption_rect = QRect(2, self.height() - self.CAPTION_H, w - 4, self.CAPTION_H)
        painter.setPen(QColor("#777777"))
        painter.drawText(
            caption_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            "Left = dark, right = bright. A channel piled up at the right is near or fully "
            "blown out (clipped).",
        )

        if self._channels is None:
            return

        chart_h = max(10, (self.height() - 2 * self.LABEL_H - self.GAP - self.CAPTION_H) // 2)

        top_label = QRect(2, 0, w - 4, self.LABEL_H)
        top_chart = QRect(0, self.LABEL_H, w, chart_h)
        bottom_label = QRect(2, self.LABEL_H + chart_h + self.GAP, w - 4, self.LABEL_H)
        bottom_chart = QRect(0, 2 * self.LABEL_H + chart_h + self.GAP, w, chart_h)

        painter.setPen(QColor("#999999"))
        painter.drawText(top_label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Exposure")
        painter.drawText(bottom_label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Red / Green / Blue")

        self._draw_chart(painter, top_chart, [(self._luminance, QColor(230, 230, 230, 220))])
        self._draw_chart(
            painter,
            bottom_chart,
            list(zip(self._channels, [QColor(255, 90, 90, 170), QColor(90, 220, 90, 170), QColor(110, 150, 255, 170)])),
        )

    @staticmethod
    def _draw_chart(painter, rect, curves):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#101010"))
        painter.drawRect(rect)
        n = 256
        x0, y0, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        for values, color in curves:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            points = [QPointF(x0 + i / (n - 1) * w, y0 + h - v * (h - 4) - 2) for i, v in enumerate(values)]
            points.append(QPointF(x0 + w, y0 + h))
            points.append(QPointF(x0, y0 + h))
            painter.drawPolygon(QPolygonF(points))


# (key, short label, accent color) for each editable curve channel -- the
# master "rgb" curve plus one per color channel for color grading (e.g.
# lifting blue specifically in the shadows without touching red/green).
CURVE_CHANNELS = [
    ("rgb", "RGB", QColor(230, 230, 230)),
    ("red", "R", QColor(255, 90, 90)),
    ("green", "G", QColor(90, 220, 90)),
    ("blue", "B", QColor(110, 150, 255)),
]


def _curve_channel_button_style(color: QColor) -> str:
    c = color.name()
    return f"""
        QPushButton {{
            background: #262626; color: {c}; border: 1px solid #3a3a3a;
            border-radius: 3px; padding: 2px 8px; font-weight: bold;
        }}
        QPushButton:checked {{ background: #333333; border: 1px solid {c}; }}
        QPushButton:hover {{ border: 1px solid {c}; }}
    """


class _CurveCanvas(QWidget):
    """The actual draggable plot -- click empty space to add a point, drag a
    point to move it, double-click (or right-click) a point to remove it.
    Only one channel is interactive at a time (the active one); the others
    are drawn dim in the background as a reference if they've been touched.
    """

    pointsChanged = Signal(list)  # emitted with the *active* channel's new points

    # Generous hit tolerance -- a precise click on a 4px dot is an
    # unreasonable ask, and (per this app's own past gradient-tool bug) a
    # miss should never feel like "nothing happened" or do the wrong thing.
    HIT_RADIUS_PX = 10
    MAX_POINTS = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self._curves = {key: [[0.0, 0.0], [1.0, 1.0]] for key, _, _ in CURVE_CHANNELS}
        self._active = "rgb"
        self._drag_index = None
        self._hover_index = None
        self._pad = 8.0
        self._histograms = None  # {"rgb"/"red"/"green"/"blue": raw log-scaled counts}, see set_histograms()

    # -- geometry: 0..1 curve space <-> widget pixels, y flipped (up = brighter)
    def _plot_rect(self) -> QRect:
        p = int(self._pad)
        return QRect(p, p, max(1, self.width() - 2 * p), max(1, self.height() - 2 * p))

    def _to_widget(self, x: float, y: float) -> QPointF:
        r = self._plot_rect()
        return QPointF(r.left() + x * r.width(), r.bottom() - y * r.height())

    def _to_curve(self, pos) -> tuple[float, float]:
        r = self._plot_rect()
        x = (pos.x() - r.left()) / max(1, r.width())
        y = (r.bottom() - pos.y()) / max(1, r.height())
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    # -- data
    def set_curves(self, curves: dict):
        for key, _, _ in CURVE_CHANNELS:
            pts = curves.get(key) or [[0.0, 0.0], [1.0, 1.0]]
            self._curves[key] = [[float(x), float(y)] for x, y in pts]
        self._drag_index = None
        self._hover_index = None
        self.update()

    def curves(self) -> dict:
        return {k: [list(p) for p in v] for k, v in self._curves.items()}

    def set_histograms(self, histograms: dict | None):
        """histograms: compute_log_histograms()'s output, or None to clear
        (e.g. no photo open). Each channel's curve is drawn against *that
        channel's own* histogram -- so switching to Red shows where red is
        actually clipping/bunched, not the overall exposure -- normalized
        to its own peak (unlike HistogramWidget's shared-peak R/G/B
        chart), since only one channel's histogram is ever shown here at a
        time and should fill the plot regardless of which channel it is."""
        self._histograms = histograms
        self.update()

    def active_channel(self) -> str:
        return self._active

    def set_active(self, key: str):
        if key not in self._curves:
            return
        self._active = key
        self._drag_index = None
        self._hover_index = None
        self.update()

    def reset_active(self):
        identity = [[0.0, 0.0], [1.0, 1.0]]
        self._curves[self._active] = identity
        self._drag_index = None
        self.update()
        self.pointsChanged.emit([list(p) for p in identity])

    # -- painting
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#181818"))

        r = self._plot_rect()
        painter.setPen(QColor("#262626"))
        painter.setBrush(QColor("#101010"))
        painter.drawRect(r)

        if self._histograms is not None:
            channel_colors = {key: color for key, _, color in CURVE_CHANNELS}
            counts = self._histograms.get(self._active)
            if counts is not None and counts.max() > 0:
                self._draw_histogram_bg(painter, r, counts / counts.max(), channel_colors[self._active])

        painter.setPen(QPen(QColor("#242424"), 1))
        for i in (1, 2):
            gx = r.left() + r.width() * i / 3
            gy = r.top() + r.height() * i / 3
            painter.drawLine(QPointF(gx, r.top()), QPointF(gx, r.bottom()))
            painter.drawLine(QPointF(r.left(), gy), QPointF(r.right(), gy))

        painter.setPen(QPen(QColor("#3a3a3a"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(self._to_widget(0.0, 0.0), self._to_widget(1.0, 1.0))

        by_key = {key: (label, color) for key, label, color in CURVE_CHANNELS}
        order = [k for k in by_key if k != self._active] + [self._active]
        for key in order:
            points = self._curves[key]
            is_active = key == self._active
            if not is_active and processing.is_identity_curve(points):
                continue  # don't clutter the plot with untouched reference channels
            _, base_color = by_key[key]
            color = QColor(base_color)
            if not is_active:
                color.setAlpha(90)
            painter.setPen(QPen(color, 2 if is_active else 1))
            lut = processing.curve_lut(points, size=64)
            poly = QPolygonF([self._to_widget(i / (len(lut) - 1), float(v)) for i, v in enumerate(lut)])
            painter.drawPolyline(poly)

            if is_active:
                for i, (x, y) in enumerate(points):
                    pos = self._to_widget(x, y)
                    hovered = i == self._hover_index or i == self._drag_index
                    painter.setPen(QPen(QColor("#000000"), 1))
                    painter.setBrush(QColor("#ffffff") if hovered else base_color)
                    rad = 5.5 if hovered else 4.0
                    painter.drawEllipse(pos, rad, rad)

    @staticmethod
    def _draw_histogram_bg(painter, rect: QRect, values: np.ndarray, color: QColor):
        """Filled area chart of `values` (0..1, already normalized) behind
        the curve -- same drawing approach as HistogramWidget's own charts,
        just dim enough to stay a backdrop rather than compete with the
        curve line and control points drawn on top of it."""
        dim = QColor(color)
        dim.setAlpha(70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dim)
        n = len(values)
        x0, y0, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        points = [QPointF(x0 + i / (n - 1) * w, y0 + h - float(v) * h) for i, v in enumerate(values)]
        points.append(QPointF(x0 + w, y0 + h))
        points.append(QPointF(x0, y0 + h))
        painter.drawPolygon(QPolygonF(points))

    # -- mouse interaction
    def _hit_test(self, pos):
        points = self._curves[self._active]
        best_i, best_d2 = None, self.HIT_RADIUS_PX ** 2
        for i, (x, y) in enumerate(points):
            p = self._to_widget(x, y)
            d2 = (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2
            if d2 <= best_d2:
                best_d2, best_i = d2, i
        return best_i

    def _insert_point(self, pos):
        points = self._curves[self._active]
        if len(points) >= self.MAX_POINTS:
            return None
        x, y = self._to_curve(pos)
        x = min(max(x, 1e-4), 1 - 1e-4)  # those slots belong to the fixed endpoints
        idx = len(points)
        for i, (px, _py) in enumerate(points):
            if px > x:
                idx = i
                break
        points.insert(idx, [x, y])
        return idx

    def _drag_point(self, pos):
        points = self._curves[self._active]
        i = self._drag_index
        if i is None or i >= len(points):
            return
        x, y = self._to_curve(pos)
        if i == 0:
            x = 0.0
        elif i == len(points) - 1:
            x = 1.0
        else:
            x = min(max(x, points[i - 1][0] + 1e-4), points[i + 1][0] - 1e-4)
        points[i][0], points[i][1] = x, y
        self.update()
        self.pointsChanged.emit([list(p) for p in points])

    def _remove_point(self, i):
        points = self._curves[self._active]
        if i is None or i == 0 or i == len(points) - 1 or len(points) <= 2:
            return  # endpoints are permanent; a curve always keeps >= 2 points
        points.pop(i)
        self._drag_index = None
        self._hover_index = None
        self.update()
        self.pointsChanged.emit([list(p) for p in points])

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.RightButton:
            self._remove_point(self._hit_test(pos))
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        i = self._hit_test(pos)
        if i is None:
            i = self._insert_point(pos)
        if i is not None:
            self._drag_index = i
            self._drag_point(pos)

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._drag_index is not None:
            self._drag_point(pos)
            return
        new_hover = self._hit_test(pos)
        if new_hover != self._hover_index:
            self._hover_index = new_hover
            self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_index is not None:
            self._drag_index = None
            self.update()

    def mouseDoubleClickEvent(self, event):
        self._remove_point(self._hit_test(event.position()))

    def leaveEvent(self, event):
        if self._hover_index is not None:
            self._hover_index = None
            self.update()


class CurveEditor(QWidget):
    """Tone/color curve editor: draggable control points mapping input
    brightness to output brightness, one curve per channel (master "RGB"
    plus individual Red/Green/Blue for color grading). Complements the
    Light/Color sliders with the same finer control curves give in any
    other photo editor -- e.g. an S-curve that pinches contrast only in the
    midtones while leaving the extreme shadows/highlights alone, which no
    single slider here can express, or lifting blue specifically in the
    shadows for a cooler look without touching the rest of the tonal range.
    """

    curveChanged = Signal(str, list)  # (channel key, new [[x, y], ...] points)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        button_row = QHBoxLayout()
        button_row.setSpacing(2)
        self._buttons = {}
        for key, label, color in CURVE_CHANNELS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setStyleSheet(_curve_channel_button_style(color))
            btn.clicked.connect(lambda _checked, k=key: self._set_active(k))
            button_row.addWidget(btn)
            self._buttons[key] = btn
        self._buttons["rgb"].setChecked(True)
        button_row.addStretch(1)
        reset_btn = QPushButton("Reset")
        reset_btn.setFixedHeight(22)
        reset_btn.clicked.connect(self._canvas_reset)
        button_row.addWidget(reset_btn)
        layout.addLayout(button_row)

        self._canvas = _CurveCanvas()
        self._canvas.pointsChanged.connect(self._on_points_changed)
        layout.addWidget(self._canvas)

    def _set_active(self, key: str):
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self._canvas.set_active(key)

    def _canvas_reset(self):
        self._canvas.reset_active()

    def _on_points_changed(self, points):
        self.curveChanged.emit(self._canvas.active_channel(), points)

    def set_curves(self, curves: dict):
        self._canvas.set_curves(curves)

    def curves(self) -> dict:
        return self._canvas.curves()

    def set_histograms(self, histograms: dict | None):
        self._canvas.set_histograms(histograms)
