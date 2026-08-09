"""One-off script: procedurally render the app icon (a camera aperture iris)
and export it as a multi-resolution .ico. Not part of the app itself."""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).parent
SIZE = 1024
SUPERSAMPLE = 4  # render larger then downsample for antialiasing -- cleans up
# hairline rasterization seams where adjacent aperture blade polygons meet

BG_TOP = (30, 33, 38, 255)
BG_BOTTOM = (18, 19, 22, 255)
RING_COLOR = (51, 153, 255, 255)  # matches the app's selection-blue accent
BLADE_COLORS = [(245, 179, 1, 255), (219, 158, 0, 255)]  # matches the star-rating amber, alternating shade


def vertical_gradient_rounded_square(size, top, bottom, radius):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, size), (0, 0, 0, 0))
    for y in range(size):
        t = y / (size - 1)
        r = round(top[0] + (bottom[0] - top[0]) * t)
        g = round(top[1] + (bottom[1] - top[1]) * t)
        b = round(top[2] + (bottom[2] - top[2]) * t)
        grad.putpixel((0, y), (r, g, b, 255))
    grad = grad.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    img.paste(grad, (0, 0), mask)
    return img


def draw_aperture(draw, cx, cy, r_out, r_in, blades, twist_deg, colors):
    sector = 360.0 / blades
    twist = math.radians(twist_deg)
    for i in range(blades):
        a0 = math.radians(i * sector)
        a1 = math.radians((i + 1) * sector)
        p1 = (cx + r_out * math.cos(a0), cy + r_out * math.sin(a0))
        p2 = (cx + r_out * math.cos(a1), cy + r_out * math.sin(a1))
        p3 = (cx + r_in * math.cos(a1 + twist), cy + r_in * math.sin(a1 + twist))
        p4 = (cx + r_in * math.cos(a0 + twist), cy + r_in * math.sin(a0 + twist))
        draw.polygon([p1, p2, p3, p4], fill=colors[i % len(colors)])


def make_icon():
    render_size = SIZE * SUPERSAMPLE
    img = vertical_gradient_rounded_square(render_size, BG_TOP, BG_BOTTOM, radius=int(render_size * 0.19))
    draw = ImageDraw.Draw(img)

    cx = cy = render_size / 2
    r_out = render_size * 0.34
    r_in = render_size * 0.115

    # thin outer ring accent, behind the blades
    ring_w = render_size * 0.018
    draw.ellipse(
        [cx - r_out - ring_w * 2, cy - r_out - ring_w * 2, cx + r_out + ring_w * 2, cy + r_out + ring_w * 2],
        outline=RING_COLOR,
        width=int(ring_w),
    )

    draw_aperture(draw, cx, cy, r_out, r_in, blades=6, twist_deg=26, colors=BLADE_COLORS)

    # small dark center hole, like a real iris opening
    hole_r = r_in * 0.62
    draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], fill=(16, 17, 19, 255))

    return img.resize((SIZE, SIZE), Image.LANCZOS)


if __name__ == "__main__":
    icon = make_icon()
    png_path = OUT_DIR / "icon_preview.png"
    icon.save(png_path)
    print(f"Saved preview: {png_path}")

    ico_path = OUT_DIR / "app_icon.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(ico_path, sizes=sizes)
    print(f"Saved icon: {ico_path}")
