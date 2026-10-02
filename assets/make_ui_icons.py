"""Generates the small monochrome UI icons the stylesheet uses (app/ui/theme.py).

Drawn at 8x and downsampled, so edges stay smooth; each icon is written at
1x and @2x (Qt picks the @2x file automatically on high-DPI screens).

    python assets/make_ui_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
COLOR = (176, 176, 176, 255)
SS = 8  # supersampling factor


def _save(name: str, size: tuple[int, int], draw_fn):
    for scale, suffix in ((1, ""), (2, "@2x")):
        w, h = size[0] * scale, size[1] * scale
        big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(big), w * SS, h * SS)
        big.resize((w, h), Image.LANCZOS).save(HERE / f"{name}{suffix}.png")


def chevron_down(d, w, h):
    stroke = max(1, round(h * 0.26))
    pts = [(stroke * 0.6, stroke * 0.6), (w / 2, h - stroke * 0.7), (w - stroke * 0.6, stroke * 0.6)]
    d.line(pts, fill=COLOR, width=stroke, joint="curve")


def chevron_right(d, w, h):
    stroke = max(1, round(w * 0.26))
    pts = [(stroke * 0.6, stroke * 0.6), (w - stroke * 0.7, h / 2), (stroke * 0.6, h - stroke * 0.6)]
    d.line(pts, fill=COLOR, width=stroke, joint="curve")


def arrow_up(d, w, h):
    stroke = max(1, round(w * 0.14))
    cx = w / 2
    d.line((cx, stroke, cx, h - stroke * 0.5), fill=COLOR, width=stroke)
    d.line([(w * 0.18, h * 0.42), (cx, stroke * 0.7), (w * 0.82, h * 0.42)], fill=COLOR, width=stroke, joint="curve")


def arrow_down(d, w, h):
    stroke = max(1, round(w * 0.14))
    cx = w / 2
    d.line((cx, stroke * 0.5, cx, h - stroke), fill=COLOR, width=stroke)
    d.line([(w * 0.18, h * 0.58), (cx, h - stroke * 0.7), (w * 0.82, h * 0.58)], fill=COLOR, width=stroke, joint="curve")


def search(d, w, h):
    stroke = max(1, round(w * 0.12))
    r = w * 0.32
    cx, cy = w * 0.42, h * 0.42
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=COLOR, width=stroke)
    d.line((cx + r * 0.72, cy + r * 0.72, w - stroke * 0.5, h - stroke * 0.5), fill=COLOR, width=stroke)


if __name__ == "__main__":
    _save("chevron_down", (10, 6), chevron_down)
    _save("chevron_right", (6, 10), chevron_right)
    _save("arrow_up", (12, 14), arrow_up)
    _save("arrow_down", (12, 14), arrow_down)
    _save("search", (14, 14), search)
    print("wrote chevron_down, chevron_right, arrow_up, arrow_down, search (1x + @2x)")
