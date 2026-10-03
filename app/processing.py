"""Non-destructive adjustment pipeline.

A "recipe" is a plain dict of adjustment values. apply_recipe() takes the
original decoded image (float32 RGB, [0,1]) and the recipe and returns a new
processed image -- the source array is never mutated, so the same original
can be re-rendered at any time (live preview or full-res export).
"""

import base64
import io
import math

import cv2
import numpy as np
from PIL import Image


def default_recipe() -> dict:
    return {
        "crop": None,  # [x0, y0, x1, y1] normalized 0..1, in the rotated/flipped frame
        "rotate90": 0,  # discrete quarter-turns clockwise, 0..3
        "rotate": 0.0,  # arbitrary straighten angle, degrees
        "flip_h": False,
        "flip_v": False,
        "exposure": 0.0,  # EV stops, -5..5
        "contrast": 0.0,  # -100..100
        "highlights": 0.0,  # -100..100
        "shadows": 0.0,  # -100..100
        "whites": 0.0,  # -100..100
        "blacks": 0.0,  # -100..100
        "temp": 0.0,  # -100..100, blue <-> yellow
        "tint": 0.0,  # -100..100, green <-> magenta
        "saturation": 0.0,  # -100..100
        "vibrance": 0.0,  # -100..100
        "red": 0.0,  # -100..100, per-channel gain
        "green": 0.0,  # -100..100, per-channel gain
        "blue": 0.0,  # -100..100, per-channel gain
        "sharpen": 0.0,  # 0..100
        "denoise": 0.0,  # 0..100
        # Tone/color curves: {"rgb"/"red"/"green"/"blue": [[x, y], ...]},
        # control points in [0,1] (input, output), always sorted by x with
        # the first/last point anchored at x=0/x=1. See apply_curves().
        "curves": default_curves(),
        # AI-selection-driven local adjustments: list of {"mask": <base64 PNG>,
        # "adjustments": {...}}. Masks are captured relative to the fully
        # geometry-processed (rotate/straighten/flip/crop) frame, so they
        # rescale consistently between the live preview and full-res export.
        "local_adjustments": [],
    }


def default_curves() -> dict:
    """Identity curve (output == input) for each channel -- a straight line
    from (0,0) to (1,1) is a no-op once turned into a lookup table."""
    return {"rgb": [[0.0, 0.0], [1.0, 1.0]], "red": [[0.0, 0.0], [1.0, 1.0]],
            "green": [[0.0, 0.0], [1.0, 1.0]], "blue": [[0.0, 0.0], [1.0, 1.0]]}


def default_local_adjustments() -> dict:
    return {
        "exposure": 0.0,
        "contrast": 0.0,
        "highlights": 0.0,
        "shadows": 0.0,
        "whites": 0.0,
        "blacks": 0.0,
        "temp": 0.0,
        "tint": 0.0,
        "hue": 0.0,
        "saturation": 0.0,
        "vibrance": 0.0,
        "sharpen": 0.0,
    }


def encode_mask(mask: np.ndarray) -> str:
    """bool/float HxW mask -> base64-encoded PNG, for storing in a recipe.
    Binary masks compress to a few KB even at multi-megapixel resolution."""
    mask_u8 = (np.clip(mask, 0.0, 1.0) * 255.0).astype(np.uint8) if mask.dtype != np.uint8 else mask
    img = Image.fromarray(mask_u8, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def decode_mask(encoded: str, target_shape) -> np.ndarray:
    """base64 PNG -> float32 HxW mask in [0,1], resized (bilinear, smooth
    edges) to target_shape=(h,w) -- the mask may have been captured at a
    different resolution (preview) than it's being applied at (full export)."""
    data = base64.b64decode(encoded)
    img = Image.open(io.BytesIO(data)).convert("L")
    target_h, target_w = target_shape
    if img.size != (target_w, target_h):
        img = img.resize((target_w, target_h), Image.BILINEAR)
    return np.asarray(img).astype(np.float32) / 255.0


def largest_inscribed_rect(w: float, h: float, angle_rad: float) -> tuple[float, float]:
    """Largest axis-aligned same-position rectangle with no rotated-in border
    pixels, for a w x h image rotated by angle_rad about its center."""
    if w <= 0 or h <= 0:
        return w, h
    sin_a, cos_a = abs(math.sin(angle_rad)), abs(math.cos(angle_rad))
    if sin_a < 1e-9:
        return w, h
    width_is_longer = w >= h
    side_long, side_short = (w, h) if width_is_longer else (h, w)
    if side_short <= 2.0 * sin_a * cos_a * side_long or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * side_short
        if width_is_longer:
            wr, hr = x / sin_a, x / cos_a
        else:
            wr, hr = x / cos_a, x / sin_a
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a
    return wr, hr


def apply_geometry(img: np.ndarray, recipe: dict) -> np.ndarray:
    out = img
    rotate90 = int(recipe.get("rotate90", 0)) % 4
    if rotate90:
        out = np.rot90(out, k=-rotate90)

    angle = recipe.get("rotate", 0.0)
    if angle:
        h, w = out.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
        out = cv2.warpAffine(
            np.ascontiguousarray(out, dtype=np.float32),
            m,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        # Straightening a rotated rectangle leaves no real data in its
        # corners; auto-crop to the largest rect fully inside the source
        # instead of showing the reflected/fabricated border.
        crop_w, crop_h = largest_inscribed_rect(w, h, math.radians(angle))
        crop_w, crop_h = min(int(crop_w), w), min(int(crop_h), h)
        x0 = max(0, (w - crop_w) // 2)
        y0 = max(0, (h - crop_h) // 2)
        out = out[y0 : y0 + crop_h, x0 : x0 + crop_w]

    if recipe.get("flip_h"):
        out = np.fliplr(out)
    if recipe.get("flip_v"):
        out = np.flipud(out)

    crop = recipe.get("crop")
    if crop:
        h, w = out.shape[:2]
        x0, y0, x1, y1 = crop
        xi0, xi1 = sorted((int(round(x0 * w)), int(round(x1 * w))))
        yi0, yi1 = sorted((int(round(y0 * h)), int(round(y1 * h))))
        xi0, xi1 = max(0, xi0), min(w, xi1)
        yi0, yi1 = max(0, yi0), min(h, yi1)
        if xi1 > xi0 and yi1 > yi0:
            out = out[yi0:yi1, xi0:xi1]

    return np.ascontiguousarray(out, dtype=np.float32)


def apply_tone_regions(img, highlights, shadows, whites, blacks):
    out = img
    if highlights or shadows:
        luminance = (out[..., 0] * 0.299 + out[..., 1] * 0.587 + out[..., 2] * 0.114)[..., None]
        shadow_weight = np.clip(1.0 - luminance / 0.5, 0.0, 1.0) ** 1.5
        highlight_weight = np.clip((luminance - 0.5) / 0.5, 0.0, 1.0) ** 1.5
        out = out + (shadows / 100.0) * 0.35 * shadow_weight
        out = out + (highlights / 100.0) * 0.35 * highlight_weight
    if whites or blacks:
        k = 0.15
        black_point = -k * (blacks / 100.0)
        white_point = 1.0 - k * (whites / 100.0)
        denom = max(white_point - black_point, 1e-3)
        out = (out - black_point) / denom
    return out


def apply_white_balance(img, temp, tint):
    if not temp and not tint:
        return img
    out = img.copy()
    r_gain = 1.0 + (temp / 100.0) * 0.3
    b_gain = 1.0 - (temp / 100.0) * 0.3
    g_gain = 1.0 - (tint / 100.0) * 0.2
    out[..., 0] *= r_gain
    out[..., 1] *= g_gain
    out[..., 2] *= b_gain
    return out


def apply_channel_gains(img, red, green, blue):
    if not red and not green and not blue:
        return img
    out = img.copy()
    out[..., 0] *= 1.0 + red / 100.0
    out[..., 1] *= 1.0 + green / 100.0
    out[..., 2] *= 1.0 + blue / 100.0
    return out


def apply_saturation_vibrance(img, saturation, vibrance):
    if not saturation and not vibrance:
        return img
    src = np.ascontiguousarray(np.clip(img, 0.0, 1.0), dtype=np.float32)
    hsv = cv2.cvtColor(src, cv2.COLOR_RGB2HSV)
    s = hsv[..., 1]
    if saturation:
        s = s * (1.0 + saturation / 100.0)
    if vibrance:
        s = s + (vibrance / 100.0) * (1.0 - s) * 0.8
    hsv[..., 1] = np.clip(s, 0.0, 1.0)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def apply_hue_shift(img, hue_degrees):
    """Rotates every pixel's hue around the color wheel by hue_degrees
    (-180..180), leaving saturation/brightness untouched -- e.g. +120 turns
    red into green, a blue sky into purple/magenta, and so on. This is the
    per-mask "change the color" control (local adjustments only; there's no
    global Hue slider since a whole-image hue rotation is rarely wanted)."""
    if not hue_degrees:
        return img
    src = np.ascontiguousarray(np.clip(img, 0.0, 1.0), dtype=np.float32)
    hsv = cv2.cvtColor(src, cv2.COLOR_RGB2HSV)
    hsv[..., 0] = (hsv[..., 0] + hue_degrees) % 360.0
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def apply_sharpen(img, amount):
    if amount <= 0:
        return img
    a = amount / 100.0
    src = np.ascontiguousarray(img, dtype=np.float32)
    blurred = cv2.GaussianBlur(src, (0, 0), sigmaX=1.5)
    return src + a * 1.5 * (src - blurred)


DENOISE_REF_DIM = 2400  # long edge denoise is tuned at -- the editor preview's size


def _noise_sigma(ch):
    """Robust noise std of one channel: Immerkaer's Laplacian-difference
    kernel cancels smooth gradients, and the median keeps real edges and
    texture from inflating the estimate."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    return float(np.median(np.abs(cv2.filter2D(ch, -1, k)))) / 0.6745 / 6.0


def _guided_filter(guide, src, r, eps):
    """He et al.'s guided filter: smooths `src` over a (2r+1)^2 box but only
    where `guide` is flat, so chroma can't bleed across a luminance edge."""
    k = (2 * r + 1, 2 * r + 1)
    m_i = cv2.boxFilter(guide, -1, k)
    m_p = cv2.boxFilter(src, -1, k)
    a = (cv2.boxFilter(guide * src, -1, k) - m_i * m_p) / (cv2.boxFilter(guide * guide, -1, k) - m_i * m_i + eps)
    b = m_p - a * m_i
    return cv2.boxFilter(a, -1, k) * guide + cv2.boxFilter(b, -1, k)


def _denoise_lab(lab, t, scale):
    """Denoises a float Lab image at (roughly) preview resolution. Returns
    the result and the chroma weight used, so the full-res path can fade
    the fine chroma band by the same amount."""
    L, a, b = (np.ascontiguousarray(lab[..., i]) for i in range(3))
    sig_l = _noise_sigma(L)
    sig_c = max(_noise_sigma(a), _noise_sigma(b))
    # Luma: NLM, its strength set from the measured grain so 100 means
    # "remove about all of it" whether the photo is ISO 100 or ISO 6400.
    h = t * min(max(sig_l * 2.55 * 1.6, 3.0), 30.0)
    l8 = np.clip(L * 2.55, 0, 255).astype(np.uint8)
    L = cv2.fastNlMeansDenoising(l8, None, h, 7, 21).astype(np.float32) / 2.55
    # Chroma: high-ISO colour noise is coarse red/blue blotching that a 7px
    # NLM patch can't see, so smooth a/b over a wide radius instead, guided
    # by luma. Scaled by how much colour noise there actually is, so a clean
    # photo's faint colour detail (thin pink clouds...) is left alone.
    w = t * min(max((sig_c - 0.5) / 3.0, 0.0), 1.0)
    if w > 0:
        r = max(1, int(round((2 + 14 * t) * scale)))
        eps = (max(sig_l, 0.5) * 2.0) ** 2
        a = a + w * (_guided_filter(L, a, r, eps) - a)
        b = b + w * (_guided_filter(L, b, r, eps) - b)
    return cv2.merge([L, a, b]), w


def apply_denoise(img, amount, scale=None):
    """Luma/chroma-split denoise. `scale` is the SOURCE photo's long edge
    over DENOISE_REF_DIM (before any crop), so a full-res export lands where
    the preview did: a 24MP RAW's noise is coarser per pixel than the
    downsized preview's, so above preview size the image is split in two
    bands -- the coarse band denoised at preview scale (exactly what the
    preview showed), the fine band soft-thresholded."""
    if amount <= 0:
        return img
    t = min(amount, 100.0) / 100.0
    if scale is None:
        scale = max(img.shape[:2]) / DENOISE_REF_DIM
    lab = cv2.cvtColor(np.clip(img, 0.0, 1.0).astype(np.float32), cv2.COLOR_RGB2Lab)
    if scale <= 1.2:
        out, _ = _denoise_lab(lab, t, scale)
        return cv2.cvtColor(out, cv2.COLOR_Lab2RGB)

    h0, w0 = lab.shape[:2]
    size = (max(1, round(w0 / scale)), max(1, round(h0 / scale)))
    small = cv2.resize(lab, size, interpolation=cv2.INTER_AREA)
    small_d, w = _denoise_lab(small, t, 1.0)
    detail = lab
    detail -= cv2.resize(small, (w0, h0), interpolation=cv2.INTER_LINEAR)
    del small
    # Median first: isolated hot/cold pixels sit far above any threshold
    # that keeps real texture, and a 3x3 median removes them but not edges.
    d_l = np.ascontiguousarray(detail[..., 0])
    d_l += t * (cv2.medianBlur(d_l, 3) - d_l)
    thr = t * 1.5 * float(np.median(np.abs(d_l))) / 0.6745
    detail[..., 0] = np.sign(d_l) * np.maximum(np.abs(d_l) - thr, 0.0)
    detail[..., 1:] *= 1.0 - w
    detail += cv2.resize(small_d, (w0, h0), interpolation=cv2.INTER_LINEAR)
    return cv2.cvtColor(detail, cv2.COLOR_Lab2RGB)


def is_identity_curve(points) -> bool:
    """True for a 2-point straight line from (0,0) to (1,1) -- i.e. a curve
    that hasn't been touched yet, so callers can skip it entirely."""
    return len(points) <= 2 and all(abs(x - y) < 1e-6 for x, y in points)


def curve_lut(points, size: int = 256) -> np.ndarray:
    """Sample a curve through `points` (each an (x, y) pair in [0,1], sorted
    by x) into a `size`-entry lookup table, via monotone cubic (Fritsch-
    Carlson) interpolation. Monotone rather than a plain natural cubic
    spline so the curve never overshoots *between* control points -- an
    overshoot on a tone curve would crush/blow out pixels the user never
    asked to clip. Also used by the curve-editor widget to draw the line
    it shows, so the drawn curve and the one actually applied always match.
    """
    pts = sorted((float(x), float(y)) for x, y in points)
    xs = np.array([p[0] for p in pts], dtype=np.float64)
    ys = np.array([p[1] for p in pts], dtype=np.float64)
    for i in range(1, len(xs)):
        if xs[i] <= xs[i - 1]:
            xs[i] = xs[i - 1] + 1e-6  # duplicate/out-of-order x: nudge apart

    sample_x = np.linspace(0.0, 1.0, size)
    if len(xs) < 2:
        return np.clip(sample_x, 0.0, 1.0).astype(np.float32)
    if len(xs) == 2:
        return np.interp(sample_x, xs, ys).astype(np.float32)

    h = np.diff(xs)
    delta = np.diff(ys) / h
    n = len(xs)
    m = np.zeros(n)
    m[0], m[-1] = delta[0], delta[-1]
    for i in range(1, n - 1):
        m[i] = 0.0 if delta[i - 1] * delta[i] <= 0 else (delta[i - 1] + delta[i]) / 2.0
    for i in range(n - 1):
        if delta[i] == 0:
            m[i] = m[i + 1] = 0.0
        else:
            a, b = m[i] / delta[i], m[i + 1] / delta[i]
            s = a * a + b * b
            if s > 9.0:
                t = 3.0 / math.sqrt(s)
                m[i], m[i + 1] = t * a * delta[i], t * b * delta[i]

    seg = np.clip(np.searchsorted(xs, sample_x, side="right") - 1, 0, n - 2)
    x0, x1, y0, y1 = xs[seg], xs[seg + 1], ys[seg], ys[seg + 1]
    m0, m1 = m[seg], m[seg + 1]
    hseg = x1 - x0
    t = np.clip((sample_x - x0) / hseg, 0.0, 1.0)
    t2, t3 = t * t, t * t * t
    h00, h10 = 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t
    h01, h11 = -2 * t3 + 3 * t2, t3 - t2
    lut = h00 * y0 + h10 * hseg * m0 + h01 * y1 + h11 * hseg * m1
    return lut.astype(np.float32)


def _apply_lut(values: np.ndarray, lut: np.ndarray) -> np.ndarray:
    xs = np.linspace(0.0, 1.0, len(lut))
    return np.interp(values.ravel(), xs, lut).reshape(values.shape).astype(np.float32)


def apply_curves(img: np.ndarray, curves: dict) -> np.ndarray:
    """curves: {"rgb"/"red"/"green"/"blue": [[x, y], ...]}. The "rgb" master
    curve applies first and identically to all three channels (classic
    S-curve contrast, lifted black point, ...); any per-channel curve then
    layers on top for color grading (e.g. lifting blue in the shadows)."""
    if not curves:
        return img
    out = img
    master = curves.get("rgb")
    if master and not is_identity_curve(master):
        out = _apply_lut(out, curve_lut(master))

    active = [
        (idx, curves[name])
        for name, idx in (("red", 0), ("green", 1), ("blue", 2))
        if curves.get(name) and not is_identity_curve(curves[name])
    ]
    if active:
        if out is img:
            out = out.copy()
        for idx, points in active:
            out[..., idx] = _apply_lut(out[..., idx], curve_lut(points))
    return out


def apply_local_layer(img: np.ndarray, mask: np.ndarray, adjustments: dict) -> np.ndarray:
    """Apply a scoped set of adjustments only within mask (0..1 per-pixel
    blend weight), leaving everything outside it untouched."""
    adjusted = img.copy()

    exposure = adjustments.get("exposure", 0.0)
    if exposure:
        adjusted = adjusted * (2.0 ** exposure)

    contrast = adjustments.get("contrast", 0.0)
    if contrast:
        factor = 1.0 + contrast / 100.0
        adjusted = (adjusted - 0.5) * factor + 0.5

    adjusted = apply_tone_regions(
        adjusted,
        adjustments.get("highlights", 0.0),
        adjustments.get("shadows", 0.0),
        adjustments.get("whites", 0.0),
        adjustments.get("blacks", 0.0),
    )
    adjusted = apply_white_balance(adjusted, adjustments.get("temp", 0.0), adjustments.get("tint", 0.0))
    adjusted = apply_hue_shift(adjusted, adjustments.get("hue", 0.0))
    adjusted = apply_saturation_vibrance(adjusted, adjustments.get("saturation", 0.0), adjustments.get("vibrance", 0.0))
    adjusted = apply_sharpen(adjusted, adjustments.get("sharpen", 0.0))
    adjusted = np.clip(adjusted, 0.0, 1.0)

    m = mask[..., None]
    return img * (1.0 - m) + adjusted * m


def apply_local_adjustments(img: np.ndarray, local_layers: list) -> np.ndarray:
    out = img
    for layer in local_layers:
        mask = decode_mask(layer["mask"], out.shape[:2])
        out = apply_local_layer(out, mask, layer.get("adjustments", {}))
    return out


def apply_recipe(img: np.ndarray, recipe: dict) -> np.ndarray:
    out = apply_geometry(img, recipe)

    exposure = recipe.get("exposure", 0.0)
    if exposure:
        out = out * (2.0 ** exposure)

    contrast = recipe.get("contrast", 0.0)
    if contrast:
        factor = 1.0 + contrast / 100.0
        out = (out - 0.5) * factor + 0.5

    out = apply_tone_regions(
        out,
        recipe.get("highlights", 0.0),
        recipe.get("shadows", 0.0),
        recipe.get("whites", 0.0),
        recipe.get("blacks", 0.0),
    )
    out = apply_white_balance(out, recipe.get("temp", 0.0), recipe.get("tint", 0.0))
    out = apply_channel_gains(out, recipe.get("red", 0.0), recipe.get("green", 0.0), recipe.get("blue", 0.0))
    out = apply_curves(out, recipe.get("curves"))
    out = apply_saturation_vibrance(out, recipe.get("saturation", 0.0), recipe.get("vibrance", 0.0))
    # Denoise before sharpen, or sharpening amplifies the grain first.
    out = apply_denoise(out, recipe.get("denoise", 0.0), scale=max(img.shape[:2]) / DENOISE_REF_DIM)
    out = apply_sharpen(out, recipe.get("sharpen", 0.0))
    out = np.clip(out, 0.0, 1.0).astype(np.float32)

    local_layers = recipe.get("local_adjustments")
    if local_layers:
        out = apply_local_adjustments(out, local_layers)
        out = np.clip(out, 0.0, 1.0).astype(np.float32)

    return out
