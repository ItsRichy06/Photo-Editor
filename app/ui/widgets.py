"""Small reusable widgets: image conversion helpers, star rating, labeled slider."""

import math

import numpy as np
from PIL import Image
from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QSlider, QWidget


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


class HistogramWidget(QWidget):
    """Live histogram of the currently rendered preview image, drawn as two
    separate charts (each normalized to its own peak) rather than one
    overlaid chart, so the exposure curve doesn't get visually buried under
    the more saturated R/G/B fills: overall exposure/luminance on top,
    R/G/B channels below."""

    LABEL_H = 14
    GAP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self._channels = None  # (r, g, b) each a length-256 float array normalized 0..1
        self._luminance = None  # length-256 float array normalized 0..1

    def set_image(self, arr: np.ndarray):
        img8 = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
        r = np.bincount(img8[..., 0].ravel(), minlength=256).astype(np.float32)
        g = np.bincount(img8[..., 1].ravel(), minlength=256).astype(np.float32)
        b = np.bincount(img8[..., 2].ravel(), minlength=256).astype(np.float32)
        luminance = (
            0.299 * img8[..., 0].astype(np.float32)
            + 0.587 * img8[..., 1].astype(np.float32)
            + 0.114 * img8[..., 2].astype(np.float32)
        ).astype(np.uint8)
        lum_counts = np.bincount(luminance.ravel(), minlength=256).astype(np.float32)
        # log-scale so a tall spike (e.g. a solid sky) doesn't flatten the rest
        r, g, b, lum_counts = np.log1p(r), np.log1p(g), np.log1p(b), np.log1p(lum_counts)
        # Each chart normalizes against its own peak -- otherwise a much
        # taller luminance peak would flatten the RGB curves or vice versa.
        rgb_peak = max(r.max(), g.max(), b.max(), 1e-6)
        self._channels = (r / rgb_peak, g / rgb_peak, b / rgb_peak)
        self._luminance = lum_counts / max(lum_counts.max(), 1e-6)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#181818"))
        if self._channels is None:
            return

        w = self.width()
        chart_h = max(10, (self.height() - 2 * self.LABEL_H - self.GAP) // 2)

        top_label = QRect(2, 0, w - 4, self.LABEL_H)
        top_chart = QRect(0, self.LABEL_H, w, chart_h)
        bottom_label = QRect(2, self.LABEL_H + chart_h + self.GAP, w - 4, self.LABEL_H)
        bottom_chart = QRect(0, 2 * self.LABEL_H + chart_h + self.GAP, w, chart_h)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
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
