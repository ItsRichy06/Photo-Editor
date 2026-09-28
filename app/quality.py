"""Fast, classical (non-AI) technical-quality triage: a rough 1-5 star
rating from sharpness (out-of-focus/motion-blur detection) and exposure
(clipped shadows/highlights), meant to run across an entire library in one
pass to flag obvious misses in a big unrated backlog.

Deliberately NOT the AI Auto Enhance model (see app/auto_enhance.py) -- that
takes a minute or more per photo, fine one at a time but not for thousands
at once. This is sub-millisecond-per-photo classical CV instead, reusing
the same disk-cached thumbnail every photo already has (or gets) from
browsing the library, so a bulk pass over an already-browsed library needs
no extra decoding at all.

Long exposures are exempted from the sharpness judgment: shutter speeds at
or slower than LONG_EXPOSURE_THRESHOLD usually carry deliberate motion blur
(light trails, waterfalls, star trails, panning shots) that this metric
can't tell apart from a focus miss, so those are rated on exposure alone.

The thresholds below are a starting point, not a calibrated ground truth --
"rough rating" is the explicit goal, not pixel-perfect judgment. They're
named constants specifically so they're easy to retune after trying this
against a real library.
"""

import numpy as np
from PIL import Image

from . import image_io, thumbnails

LONG_EXPOSURE_THRESHOLD = 1.0  # seconds

# Tiled-Laplacian-variance thresholds, computed at thumbnail resolution (see
# thumbnails.THUMB_SIZE) -- calibrated against a random sample of 150 real
# photos from this app's own library, including verified shallow-depth-of-
# field shots (f/2.8-f/6.3) that score low on a naive whole-frame average
# but correctly land well above SHARPNESS_DECENT here once only the
# sharpest region of the frame is considered.
SHARPNESS_BLURRY = 50.0  # below this: visibly out of focus or heavily motion-blurred
SHARPNESS_SOFT = 200.0  # below this: noticeably soft, might not hold up at full size
SHARPNESS_DECENT = 800.0  # below this: acceptably sharp for typical viewing
SHARPNESS_SHARP = 2500.0  # at/above this: crisp

# Per-pixel luminance beyond which a pixel counts as "near-black"/"near-
# white" for clipping purposes -- deliberately NOT close to literal 0/1.
# Resizing down to thumbnail resolution (see thumbnails.THUMB_SIZE) blends
# truly-clipped pixels with their neighbors, pulling a genuinely blown-out
# region (0.99+ at full resolution) down into roughly the 0.85-0.95 range
# by the time it's a 320px thumbnail -- checking for literal near-0/near-1
# on a *resized* image just never fires. Confirmed live: a real flash-
# blown photo (faces/jackets visibly pure white) measured 0% pixels above
# 254/255 but 31.57% above this threshold.
CLIP_PIXEL_THRESHOLD = 0.85

# Tier percentages -- separate for highlights and shadows, NOT shared.
# Confirmed live that sharing one set was a real bug: dark tones (shadows,
# hair, dark clothing, depth) are just normal in most photography, so
# "% of pixels below the shadow threshold" runs vastly higher than "% above
# the highlight threshold" even in completely ordinary photos -- a 150-photo
# sample of this library measured median shadow%=27.9 vs median
# highlight%=0.5, and using the same cutoff for both meant 67% of NORMAL
# photos tripped the shadow side alone. Each set below is calibrated
# against that same sample's own distribution (only the tail should ever
# trigger MODERATE, not the majority).
CLIP_HIGHLIGHT_MODERATE = 15.0  # highlight% distribution: p90=6.2 p95=10.5 max=17.7
CLIP_HIGHLIGHT_MILD = 6.0
CLIP_HIGHLIGHT_SLIGHT = 2.0

CLIP_SHADOW_MODERATE = 85.0  # shadow% distribution: median=27.9 p75=45.4 p90=70.1 p95=83.7
CLIP_SHADOW_MILD = 70.0
CLIP_SHADOW_SLIGHT = 45.0


def _to_float01(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0


SHARPNESS_GRID = 6  # tiles per side


def compute_sharpness(image_float: np.ndarray) -> float:
    """Variance of the Laplacian -- a standard, cheap focus-blur metric: a
    sharp image has lots of edges/high-frequency detail, which the
    Laplacian picks up strongly; blur smooths that away.

    Computed per-tile across a grid and taking the sharpest tiles, not one
    whole-frame average -- a whole-frame average rates a shallow-depth-of-
    field shot (a sharp subject against a deliberately blurred background,
    e.g. a bird or a portrait) as blurry, since most of the frame really is
    soft. Taking the sharpest tiles instead asks "is any meaningful part of
    this photo actually in focus", which is what a focus-*miss* check
    should ask -- plenty of great photos are not uniformly sharp edge to
    edge, and shouldn't be penalized for that."""
    import cv2

    gray = cv2.cvtColor((np.clip(image_float, 0.0, 1.0) * 255.0).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    tile_h, tile_w = max(1, h // SHARPNESS_GRID), max(1, w // SHARPNESS_GRID)
    tile_variances = [
        cv2.Laplacian(gray[ty : ty + tile_h, tx : tx + tile_w], cv2.CV_64F).var()
        for ty in range(0, h - tile_h + 1, tile_h)
        for tx in range(0, w - tile_w + 1, tile_w)
    ]
    if not tile_variances:
        return 0.0
    tile_variances.sort()
    # Average of the sharpest ~15% of tiles -- robust to one noisy/edge-
    # artifact tile while still requiring a real sharp region, not just a
    # single sharp pixel.
    top_n = max(1, len(tile_variances) // 7)
    return float(np.mean(tile_variances[-top_n:]))


def compute_clipping(image_float: np.ndarray) -> tuple[float, float]:
    """Returns (shadow_clip_pct, highlight_clip_pct) -- % of pixels beyond
    CLIP_PIXEL_THRESHOLD from black/white respectively."""
    img = np.clip(image_float, 0.0, 1.0)
    luminance = 0.2126 * img[..., 0] + 0.7152 * img[..., 1] + 0.0722 * img[..., 2]
    shadow_pct = float((luminance < (1.0 - CLIP_PIXEL_THRESHOLD)).mean() * 100.0)
    highlight_pct = float((luminance > CLIP_PIXEL_THRESHOLD).mean() * 100.0)
    return shadow_pct, highlight_pct


def _sharpness_tier(sharpness: float) -> int:
    if sharpness < SHARPNESS_BLURRY:
        return 1
    if sharpness < SHARPNESS_SOFT:
        return 2
    if sharpness < SHARPNESS_DECENT:
        return 3
    if sharpness < SHARPNESS_SHARP:
        return 4
    return 5


def _clip_tier(clip_pct: float, moderate: float, mild: float, slight: float) -> int:
    """Floors at 2 ("possibly recoverable"), never 1 ("useless") -- unlike
    blur, clipped shadows/highlights in a RAW file are usually recoverable
    with the Shadows/Blacks/Highlights sliders already in this app, and
    severe clipping alone doesn't mean a mistake: a deliberate low-key/
    night shot or a silhouette can legitimately have most of the frame
    near-black. Reserve "useless" for the one thing editing can never fix:
    a genuine focus miss."""
    if clip_pct >= moderate:
        return 2
    if clip_pct >= mild:
        return 3
    if clip_pct >= slight:
        return 4
    return 5


def _exposure_tier(shadow_pct: float, highlight_pct: float) -> int:
    shadow_tier = _clip_tier(shadow_pct, CLIP_SHADOW_MODERATE, CLIP_SHADOW_MILD, CLIP_SHADOW_SLIGHT)
    highlight_tier = _clip_tier(highlight_pct, CLIP_HIGHLIGHT_MODERATE, CLIP_HIGHLIGHT_MILD, CLIP_HIGHLIGHT_SLIGHT)
    return min(shadow_tier, highlight_tier)


def _get_shutter_seconds(path_str: str):
    """EXIF-only, no full decode -- same fast IFD-walk pattern as
    stacking._photo_info, just returning the raw shutter value that
    function doesn't expose (it only keeps the derived EV)."""
    try:
        if image_io.is_raw(path_str):
            _, sub_tags = image_io._read_tiff_exif_tags(path_str)
            return image_io._rational_to_float(sub_tags.get("ExposureTime"))
        return image_io.get_exif_summary(path_str).get("shutter")
    except (ValueError, OSError):
        return None


def assess_quality(image_float: np.ndarray, shutter_seconds) -> int:
    """Returns a 1-5 star rating. The worse of the two axes (sharpness,
    exposure) caps the result -- a photo ruined by either alone shouldn't
    score well just because the other axis is fine."""
    shadow_pct, highlight_pct = compute_clipping(image_float)
    exposure_tier = _exposure_tier(shadow_pct, highlight_pct)

    is_long_exposure = shutter_seconds is not None and shutter_seconds >= LONG_EXPOSURE_THRESHOLD
    if is_long_exposure:
        return exposure_tier

    sharpness_tier = _sharpness_tier(compute_sharpness(image_float))
    return min(sharpness_tier, exposure_tier)


def assess_photo(path_str: str, thumb_size: int = 320) -> int:
    """Convenience wrapper: loads (or reuses the disk-cached) thumbnail and
    the photo's shutter speed, returns a 1-5 rating. This is the function
    the bulk auto-rate pass actually calls."""
    thumb = thumbnails.get_thumbnail(path_str, thumb_size)
    image_float = _to_float01(thumb)
    shutter = _get_shutter_seconds(path_str)
    return assess_quality(image_float, shutter)
