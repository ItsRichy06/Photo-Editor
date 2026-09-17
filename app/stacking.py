"""Focus and HDR (exposure) stacking.

Given a flat list of selected photos, auto-detect which ones form an
exposure bracket (differing shutter speed, same framing -- classic HDR) or a
focus bracket (same exposure, subtly different focus distance -- focus
stacking), align each detected group, and merge it into one new photo saved
next to the sources.

Detection works purely from EXIF (capture time + exposure value) since focus
distance isn't reliably exposed across cameras/lenses -- there is no way to
be 100% certain from metadata alone that a burst of same-exposure photos is
really a deliberate focus stack and not, say, a fast action sequence. The
caller is expected to show the detected groups for the user to confirm/
reject before anything is written to disk.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import image_io

# Consecutive photos more than this many seconds apart are never considered
# part of the same burst -- generous enough for a deliberate, unhurried
# bracket sequence, tight enough to not lump together unrelated shots.
BURST_GAP_SECONDS = 4.0
# Within a same-exposure burst, focus-stack frames are almost always shot in
# rapid continuous succession (racking focus takes a fraction of a second
# per frame) -- a tighter gap than the general burst window, so a slower,
# deliberate re-composition between two *different* focus stacks doesn't get
# merged into one.
FOCUS_GAP_SECONDS = 2.0
# Two shots count as "the same exposure" if within this many EV of each
# other -- loose enough to absorb normal metering jitter, tight enough not
# to mistake a genuine (if small) bracket step for noise. A burst is only
# treated as an HDR bracket once its spread exceeds 2x this.
EV_JITTER = 0.35
# Granularity EV values are rounded to before comparing for a repeat -- see
# _split_hdr_cycles().
EV_ROUND = 0.2
MIN_GROUP_SIZE = 3
MAX_GROUP_SIZE = 9  # generous upper bound for an HDR bracket, matches real cameras' caps
# Focus stacks run far longer than exposure brackets ever do (a deep macro
# stack can legitimately run to 50-100+ frames), so this is much more
# generous -- mainly a backstop against a long same-exposure continuous-
# shooting burst (sports, wildlife) being swept up as one giant "stack",
# both for plausibility and because aligning+merging that many full-
# resolution frames as one group would be very slow. Previously 30, which
# turned out to be low enough to bite a real deliberate 35-frame macro
# stack -- it got silently chopped into a 30-frame group plus an orphaned
# 5-frame "stack" of whatever the tail of the sequence happened to be,
# which looked like a detection bug rather than a hardcoded cap. The
# per-group review dialog already lets the user reject any candidate that
# doesn't make sense, so there's little cost to being more generous here.
MAX_FOCUS_GROUP_SIZE = 100


@dataclass
class StackCandidate:
    kind: str  # "hdr" | "focus"
    paths: list  # list[Path], time-sorted
    label: str  # human-readable summary for the confirmation UI


def _exposure_value(shutter, aperture, iso) -> float | None:
    """Standard photographic EV (at ISO 100): higher = brighter exposure.
    Used only to compare shots against each other, not as an absolute
    light-meter reading, so a missing ISO (assumed 100) is fine."""
    if not shutter or not aperture:
        return None
    ev = math.log2((aperture * aperture) / shutter)
    if iso:
        ev -= math.log2(iso / 100.0)
    return ev


def _add_subsecond(dt: datetime, sub_tags: dict) -> datetime:
    """A one-second EXIF timestamp resolution is too coarse to correctly
    order a fast burst -- several bracket/focus-stack shots often land in
    the same second. Cameras commonly write a SubSecTime(Original) string
    of digits for the fractional second (e.g. "774" meaning .774s);
    incorporating it fixes the ordering of same-second bursts."""
    raw = sub_tags.get("SubsecTimeOriginal") or sub_tags.get("SubsecTime")
    if not raw:
        return dt
    digits = str(raw).strip()
    if not digits.isdigit():
        return dt
    micros = int(digits.ljust(6, "0")[:6])
    return dt.replace(microsecond=micros)


def _photo_info(path: Path) -> dict:
    """EXIF-only, no full RAW/JPEG decode. For RAW files this deliberately
    bypasses rawpy's own metadata accessors (raw.other.iso_speed etc.) in
    favor of image_io's hand-rolled TIFF/IFD walk, which reads the same
    values directly from the file's IFD0/EXIF-SubIFD in milliseconds
    instead of 1+ second per file (see get_capture_datetime's docstring)
    -- the difference between a few seconds and several minutes to analyze
    a large (e.g. 200-photo) selection."""
    path_str = str(path)
    dt = shutter = aperture = iso = None
    if image_io.is_raw(path_str):
        try:
            ifd0_tags, sub_tags = image_io._read_tiff_exif_tags(path_str)
            raw_dt = ifd0_tags.get("DateTime")
            if raw_dt:
                dt = datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")
                dt = _add_subsecond(dt, sub_tags)
            shutter = image_io._rational_to_float(sub_tags.get("ExposureTime"))
            aperture = image_io._rational_to_float(sub_tags.get("FNumber"))
            iso = sub_tags.get("ISOSpeedRatings") or sub_tags.get("PhotographicSensitivity")
        except (ValueError, OSError):
            pass
    else:
        exif = image_io.get_exif_summary(path_str)
        dt = image_io.get_capture_datetime(path_str)
        shutter, aperture, iso = exif.get("shutter"), exif.get("aperture"), exif.get("iso")

    ev = _exposure_value(shutter, aperture, iso)
    return {"path": path, "datetime": dt, "ev": ev}


def dedupe_pairs(paths) -> list:
    """When both a RAW and a JPEG of the same shot are selected (same
    filename stem -- this app's own export/camera output pairs them that
    way), keep only the RAW. Treating them as two separate photos would
    double-count every real shot and confuse the exposure-cycle/timestamp
    grouping below, since a RAW+JPEG pair share the same timestamp and
    exposure -- confirmed as the actual cause of duplicate/overlapping
    detected groups when tested against a real photo folder with paired
    files."""
    by_stem = {}
    for p in paths:
        p = Path(p)
        existing = by_stem.get(p.stem)
        if existing is None or (image_io.is_raw(p) and not image_io.is_raw(existing)):
            by_stem[p.stem] = p
    return list(by_stem.values())


def gather_info(paths, progress_cb=None) -> list:
    """EXIF-only pass (fast, no full image decode) over each path.
    progress_cb, if given, is called with (done, total) after each photo --
    meant to be run off the GUI thread and polled/reported from there."""
    paths = dedupe_pairs(paths)
    infos = []
    total = len(paths)
    for i, p in enumerate(paths):
        infos.append(_photo_info(Path(p)))
        if progress_cb:
            progress_cb(i + 1, total)
    return infos


def _cluster_bursts(infos: list, gap_seconds: float) -> list:
    """Split time-sorted infos into runs where each consecutive pair is
    within gap_seconds of each other. A photo with no timestamp is always
    its own singleton run -- there's nothing to cluster it on."""
    bursts = []
    current = []
    prev_dt = None
    for info in infos:
        dt = info["datetime"]
        if dt is None:
            if current:
                bursts.append(current)
            bursts.append([info])
            current = []
            prev_dt = None
            continue
        if current and prev_dt is not None and (dt - prev_dt).total_seconds() <= gap_seconds:
            current.append(info)
        else:
            if current:
                bursts.append(current)
            current = [info]
        prev_dt = dt
    if current:
        bursts.append(current)
    return bursts


def _split_hdr_cycles(burst: list) -> list:
    """Within one exposure-varying burst, split into individual bracket
    cycles. A real camera bracket visits each exposure level exactly once
    per cycle (whatever the order -- ascending, descending, "0 first"), so
    an EV value reappearing (within EV_ROUND) marks the start of a new
    cycle. This is what turns e.g. one 200-photo burst of forty repeated
    5-shot brackets back into forty separate 5-photo groups."""
    groups, current, seen = [], [], set()
    for info in burst:
        ev = info["ev"]
        key = None if ev is None else round(ev / EV_ROUND) * EV_ROUND
        if key is not None and key in seen and len(current) >= 2:
            groups.append(current)
            current, seen = [], set()
        current.append(info)
        if key is not None:
            seen.add(key)
        if len(current) >= MAX_GROUP_SIZE:
            groups.append(current)
            current, seen = [], set()
    if current:
        groups.append(current)
    return groups


def _chunk(infos: list, max_size: int) -> list:
    """Split into consecutive chunks of at most max_size."""
    return [infos[i : i + max_size] for i in range(0, len(infos), max_size)]


def _make_candidate(kind: str, infos: list) -> StackCandidate:
    paths = [i["path"] for i in infos]
    names = [p.stem for p in paths]
    span = f"{names[0]} - {names[-1]}" if len(names) > 1 else names[0]
    label = f"{'HDR' if kind == 'hdr' else 'Focus'} stack, {len(paths)} photos ({span})"
    return StackCandidate(kind=kind, paths=paths, label=label)


def detect_stacks(infos: list) -> tuple[list, list]:
    """infos: from gather_info(), any order -- sorted here by capture time.
    Returns (candidates, leftover_paths); leftover_paths are photos that
    aren't part of any detected group (no timestamp, isolated, or a
    same-exposure/exposure-varying run too small to bother stacking) and
    are left untouched."""
    dated = sorted(infos, key=lambda i: (i["datetime"] is None, i["datetime"]))
    bursts = _cluster_bursts(dated, BURST_GAP_SECONDS)

    candidates = []
    leftover = []
    for burst in bursts:
        if len(burst) < MIN_GROUP_SIZE:
            leftover.extend(i["path"] for i in burst)
            continue
        evs = [i["ev"] for i in burst if i["ev"] is not None]
        ev_spread = (max(evs) - min(evs)) if evs else 0.0
        if ev_spread >= EV_JITTER * 2:
            # Clearly varying exposure: an HDR-bracketed burst, possibly
            # several consecutive bracket cycles shot back to back.
            for cycle in _split_hdr_cycles(burst):
                if len(cycle) < MIN_GROUP_SIZE:
                    leftover.extend(i["path"] for i in cycle)
                    continue
                candidates.append(_make_candidate("hdr", cycle))
        else:
            # Near-constant exposure: re-split on the tighter focus-rack
            # cadence, since several separate focus stacks can be shot back
            # to back with only a short pause between them.
            for sub in _cluster_bursts(burst, FOCUS_GAP_SECONDS):
                for chunk in _chunk(sub, MAX_FOCUS_GROUP_SIZE):
                    if len(chunk) < MIN_GROUP_SIZE:
                        leftover.extend(i["path"] for i in chunk)
                        continue
                    candidates.append(_make_candidate("focus", chunk))
    return candidates, leftover


# --------------------------------------------------------------- alignment


# ECC's search is iterative and its per-iteration cost scales with pixel
# count, so running it directly on a full-resolution RAW (tens of megapixels)
# is dramatically slower than the alignment quality needs -- long enough to
# make "select 200 photos" a many-minutes wait. Standard fix: find the warp
# on a small downsampled proxy, then scale that matrix up and apply it once
# to the full-resolution image (a single warp is comparatively cheap; only
# the iterative search benefits from working at full size).
ECC_PROXY_MAX_DIM = 1000


def _downsample_gray(img_float: np.ndarray, max_dim: int) -> tuple:
    """Returns (grayscale proxy, scale) where scale = proxy_size / full_size."""
    h, w = img_float.shape[:2]
    scale = min(1.0, max_dim / max(h, w))
    gray = cv2.cvtColor(img_float, cv2.COLOR_RGB2GRAY)
    if scale < 1.0:
        gray = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    return gray, scale


def _scale_warp(warp: np.ndarray, scale: float) -> np.ndarray:
    """Rescale a warp matrix found on a `scale`x proxy so it applies
    correctly to the full-resolution image instead (translation components
    scale by 1/scale; rotation/perspective components don't)."""
    s = np.array([[1.0 / scale, 0, 0], [0, 1.0 / scale, 0], [0, 0, 1]], dtype=np.float64)
    if warp.shape[0] == 2:
        warp = np.vstack([warp, [0, 0, 1]])
        full = (s @ warp @ np.linalg.inv(s)).astype(np.float32)
        return full[:2, :]
    return (s @ warp.astype(np.float64) @ np.linalg.inv(s)).astype(np.float32)


def _align_ecc(images_float: list, ref_index: int, motion_type) -> list:
    """Align every image in images_float onto images_float[ref_index] via
    ECC (Enhanced Correlation Coefficient). Used for both HDR and focus
    stacks (with a different motion model -- see callers below): ECC is,
    by design, invariant to a global gain/bias difference between the two
    images, which is exactly the kind of difference an HDR bracket's
    exposure steps produce, so it stays reliable across frames that don't
    look equally bright. (cv2's own AlignMTB, built specifically for HDR
    alignment, was tried first and empirically found to silently produce a
    *worse*-aligned result than the unaligned input on this OpenCV build --
    confirmed against a synthetic case with a known exact shift -- so ECC
    is used here instead, verified against the same known-shift case.)
    """
    ref_proxy, scale = _downsample_gray(images_float[ref_index], ECC_PROXY_MAX_DIM)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6)
    h, w = images_float[ref_index].shape[:2]
    warp_size = 3 if motion_type == cv2.MOTION_HOMOGRAPHY else 2
    aligned = []
    for i, img in enumerate(images_float):
        if i == ref_index:
            aligned.append(img)
            continue
        proxy, _ = _downsample_gray(img, ECC_PROXY_MAX_DIM)
        warp = np.eye(warp_size, 3, dtype=np.float32)
        try:
            _, warp = cv2.findTransformECC(ref_proxy, proxy, warp, motion_type, criteria, None, 5)
            warp = _scale_warp(warp, scale)
            if warp_size == 3:
                out = cv2.warpPerspective(
                    img, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                    borderMode=cv2.BORDER_REPLICATE,
                )
            else:
                out = cv2.warpAffine(
                    img, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                    borderMode=cv2.BORDER_REPLICATE,
                )
        except cv2.error:
            out = img  # didn't converge -- fall back to the unaligned frame rather than failing the whole stack
        aligned.append(out)
    return aligned


def _align_translation(images_float: list, ref_index: int) -> list:
    """Translation + rotation alignment for an HDR bracket -- handheld
    shake between bracket shots is almost always just that, no perspective
    change, so the cheaper/more stable Euclidean motion model is enough."""
    return _align_ecc(images_float, ref_index, cv2.MOTION_EUCLIDEAN)


def _align_homography(images_float: list, ref_index: int) -> list:
    """Full-homography alignment for focus stacks -- focus breathing (a
    lens's field of view subtly changing across its focus range) means a
    simpler motion model isn't always enough."""
    return _align_ecc(images_float, ref_index, cv2.MOTION_HOMOGRAPHY)


# ------------------------------------------------------------------ merge


def _check_same_size(images: list):
    shapes = {img.shape[:2] for img in images}
    if len(shapes) > 1:
        raise ValueError(f"photos in this stack have different pixel dimensions: {shapes}")


def _to_u8_bgr(img_float: np.ndarray) -> np.ndarray:
    rgb8 = np.clip(img_float, 0.0, 1.0) * 255.0
    return cv2.cvtColor(rgb8.astype(np.uint8), cv2.COLOR_RGB2BGR)


def merge_hdr(paths: list) -> np.ndarray:
    images = [image_io.load_image_float(p) for p in paths]
    _check_same_size(images)
    ref_index = len(images) // 2
    aligned = _align_translation(images, ref_index)
    merged_bgr = cv2.createMergeMertens().process([_to_u8_bgr(img) for img in aligned])
    merged_rgb = cv2.cvtColor(merged_bgr, cv2.COLOR_BGR2RGB)
    return np.clip(merged_rgb, 0.0, 1.0).astype(np.float32)


def merge_focus(paths: list) -> np.ndarray:
    images = [image_io.load_image_float(p) for p in paths]
    _check_same_size(images)
    ref_index = len(images) // 2
    aligned = _align_homography(images, ref_index)

    sharpness = []
    for img in aligned:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=5)
        sharpness.append(cv2.GaussianBlur(np.abs(lap), (0, 0), sigmaX=4.0))
    stack = np.stack(sharpness, axis=0)  # NxHxW

    # Soft per-pixel "which frame is sharpest here" selection -- a hard
    # argmax leaves visible seams at the focus-plane boundaries between
    # frames, so this blends smoothly instead, weighted heavily toward
    # whichever frame wins at each pixel.
    weights = stack - stack.max(axis=0, keepdims=True)
    weights = np.exp(weights * 6.0)
    weights /= weights.sum(axis=0, keepdims=True) + 1e-8

    out = np.zeros_like(aligned[0])
    for i, img in enumerate(aligned):
        out += img * weights[i][..., None]
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def merge_stack(candidate: StackCandidate) -> np.ndarray:
    if candidate.kind == "hdr":
        return merge_hdr(candidate.paths)
    return merge_focus(candidate.paths)


def output_path_for(candidate: StackCandidate) -> Path:
    first = Path(candidate.paths[0])
    suffix = "HDR" if candidate.kind == "hdr" else "Focus"
    out = first.with_name(f"{first.stem}_{suffix}.jpg")
    n = 2
    while out.exists():
        out = first.with_name(f"{first.stem}_{suffix}_{n}.jpg")
        n += 1
    return out
