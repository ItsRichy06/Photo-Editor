"""Unified image loading/saving for JPEG/JPG and Sony ARW (RAW).

All editing/export works on float32 RGB arrays in the [0, 1] range so the same
processing pipeline (app/processing.py) applies to both formats. RAW files are
decoded via rawpy (LibRaw); everything else goes through Pillow.
"""

import io
import struct
from datetime import datetime
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ExifTags, ImageOps, TiffImagePlugin

RAW_EXTENSIONS = {".arw"}
# .tif/.tiff included so a file exported from elsewhere (e.g. another
# stacking/editing tool's TIFF output) can be reopened here for further
# editing -- Image.open() already handles TIFF natively via Pillow, same
# as JPEG, so no format-specific loading code was needed, just adding it
# to the set of extensions the library scan/open dialogs recognize.
STANDARD_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff"}
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | STANDARD_EXTENSIONS


def is_supported(path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def is_raw(path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def get_dimensions(path) -> tuple[int, int]:
    """Fast dimension lookup without a full decode."""
    path = str(path)
    if is_raw(path):
        with rawpy.imread(path) as raw:
            w, h = raw.sizes.width, raw.sizes.height
            return (h, w) if raw.sizes.flip in (5, 6) else (w, h)
    with Image.open(path) as img:
        w, h = img.width, img.height
        # EXIF orientations 5-8 include a 90-degree turn, which swaps the
        # displayed dimensions relative to the raw stored pixel grid.
        orientation = img.getexif().get(0x0112, 1)
        return (h, w) if orientation in (5, 6, 7, 8) else (w, h)


def load_image_float(path, half_size: bool = False) -> np.ndarray:
    """Full decode -> HxWx3 float32 array in [0, 1]."""
    path = str(path)
    if is_raw(path):
        with rawpy.imread(path) as raw:
            rgb16 = raw.postprocess(
                use_camera_wb=True,
                # LibRaw's own histogram-based exposure compensation. With this
                # disabled (the old no_auto_bright=True), RAWs rendered notably
                # dark and flat compared to both the camera's own embedded JPEG
                # and any other RAW viewer -- confirmed on a real library file,
                # the old render averaged ~75% darker than the camera's JPEG.
                # half-size preview decodes and full-size export decodes agree
                # to within ~0.6% brightness, so this doesn't introduce a
                # visible jump between the live preview and the exported file.
                no_auto_bright=False,
                output_bps=16,
                half_size=half_size,
            )
        return rgb16.astype(np.float32) / 65535.0
    with Image.open(path) as img:
        # Pillow never auto-rotates on load -- without this, any photo shot in
        # portrait (or with the camera rotated) displays sideways.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        if half_size:
            img = img.resize((max(1, img.width // 2), max(1, img.height // 2)), Image.BILINEAR)
        arr = np.asarray(img).astype(np.float32) / 255.0
        return arr


def load_preview_float(path, max_dim: int = 1600) -> np.ndarray:
    """Decode then downsample to max_dim on the long edge, for fast interactive editing."""
    # RAW: use half_size decode when the source is clearly larger than we need --
    # much faster than a full-res demosaic followed by a resize.
    if is_raw(path):
        w, h = get_dimensions(path)
        use_half = max(w, h) > max_dim * 2.2
        arr = load_image_float(path, half_size=use_half)
    else:
        arr = load_image_float(path)

    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest > max_dim:
        scale = max_dim / longest
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        img = Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8))
        img = img.resize((new_w, new_h), Image.BILINEAR)
        arr = np.asarray(img).astype(np.float32) / 255.0
    return arr


def _apply_libraw_flip(img: Image.Image, flip: int) -> Image.Image:
    """flip: LibRaw's own convention -- 0=none, 3=180, 5=90CCW, 6=90CW."""
    if flip == 3:
        return img.transpose(Image.Transpose.ROTATE_180)
    if flip == 5:
        return img.transpose(Image.Transpose.ROTATE_90)
    if flip == 6:
        return img.transpose(Image.Transpose.ROTATE_270)
    return img


def extract_embedded_preview(path) -> Image.Image | None:
    """Fast preview for thumbnails: camera-embedded JPEG for RAW, or the image itself."""
    path = str(path)
    if is_raw(path):
        try:
            with rawpy.imread(path) as raw:
                thumb = raw.extract_thumb()
                flip = raw.sizes.flip
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawFileUnsupportedError):
            return None
        if thumb.format == rawpy.ThumbFormat.JPEG:
            img = Image.open(io.BytesIO(thumb.data))
            # Deliberately ignore the embedded JPEG's own EXIF orientation tag --
            # not every camera writes a correct one on the *thumbnail* specifically,
            # which is what caused this to stay wrong even after a first fix attempt.
            # raw.sizes.flip is LibRaw's own reading of the file's orientation, the
            # exact same value that makes the main RAW decode (postprocess) correctly
            # oriented, so it's the one source of truth used everywhere here.
            img = _apply_libraw_flip(img, flip)
            return img.convert("RGB")
        elif thumb.format == rawpy.ThumbFormat.BITMAP:
            img = Image.fromarray(thumb.data).convert("RGB")
            return _apply_libraw_flip(img, flip)
        return None
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB").copy()


def _rational_to_float(value):
    """EXIF rational values come back as (numerator, denominator) tuples (or
    occasionally an IFDRational/plain number depending on Pillow version)."""
    if value is None:
        return None
    if isinstance(value, tuple) and len(value) == 2:
        num, den = value
        try:
            return float(num) / float(den) if den else None
        except (TypeError, ZeroDivisionError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _combine_make_model(make, model) -> str | None:
    make = (make or "").strip()
    model = (model or "").strip()
    if not model:
        return make or None
    if not make or model.lower().startswith(make.lower()):
        return model
    return f"{make} {model}"


def _format_exif_datetime(raw) -> str | None:
    if not raw:
        return None
    try:
        from datetime import datetime as _dt

        return _dt.strptime(raw, "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def _read_tiff_exif_tags(path) -> tuple[dict, dict]:
    """Manually walk a TIFF-based file's IFD0 and Exif SubIFD, bypassing
    PIL.Image.open()'s format validation. Some cameras (confirmed: a Sony
    ZV-E10's .ARW files) write an IFD0 with no ImageWidth/ImageLength --
    the actual raw pixel data lives in a SubIFD instead -- which makes
    Pillow refuse to even identify the file, even though that same IFD0 has
    perfectly ordinary Make/Model/DateTime tags and a valid pointer to the
    Exif SubIFD with lens/exposure data. Returns (ifd0_tags, exif_subifd_tags),
    both {tag_name: value} using PIL's tag names; either may be empty."""
    with open(path, "rb") as f:
        header = f.read(8)
        if header[:2] not in (b"II", b"MM") or len(header) < 8:
            return {}, {}
        byteorder = "<" if header[:2] == b"II" else ">"
        ifd_offset = struct.unpack(byteorder + "I", header[4:8])[0]

        f.seek(ifd_offset)
        ifd0 = TiffImagePlugin.ImageFileDirectory_v2(header)
        ifd0.load(f)
        ifd0_tags = {ExifTags.TAGS.get(k, k): v for k, v in ifd0.items()}

        sub_tags = {}
        exif_offset = ifd0.get(0x8769)  # standard "ExifIFD pointer" tag
        if exif_offset:
            f.seek(exif_offset)
            exif_ifd = TiffImagePlugin.ImageFileDirectory_v2(header)
            exif_ifd.load(f)
            sub_tags = {ExifTags.TAGS.get(k, k): v for k, v in exif_ifd.items()}
        return ifd0_tags, sub_tags


def get_capture_datetime(path):
    """Full-precision (down to the second) capture timestamp, for grouping
    photos by *when* they were shot (e.g. detecting a burst/bracket for
    stacking). Unlike get_exif_summary()'s "date" field -- intentionally
    truncated to minute precision for display -- this is precise enough to
    tell consecutive burst shots apart. Returns None if unavailable.

    For RAW files this reads via the existing hand-rolled TIFF/IFD walk
    (_read_tiff_exif_tags) rather than rawpy's raw.other.timestamp --
    measured at ~10ms vs 1+ second per file for the rawpy accessor (it
    appears to trigger a much broader metadata parse under the hood), which
    matters a lot when this runs once per photo across a big selection."""
    path = str(path)
    if is_raw(path):
        try:
            ifd0_tags, sub_tags = _read_tiff_exif_tags(path)
            raw_dt = ifd0_tags.get("DateTime")
            if not raw_dt:
                return None
            dt = datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")
            # One-second resolution is too coarse to order a fast burst --
            # several shots often land in the same second. Fold in the
            # sub-second EXIF field (a digit string for the fractional
            # second, e.g. "774" meaning .774s) when the camera wrote one.
            subsec = str(sub_tags.get("SubsecTimeOriginal") or sub_tags.get("SubsecTime") or "").strip()
            if subsec.isdigit():
                dt = dt.replace(microsecond=int(subsec.ljust(6, "0")[:6]))
            return dt
        except (ValueError, OSError):
            return None
    try:
        with Image.open(path) as img:
            tags = {ExifTags.TAGS.get(k, k): v for k, v in img.getexif().items()}
            raw_dt = tags.get("DateTime")
            if not raw_dt:
                return None
            return datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")
    except (ValueError, OSError):
        return None


def get_exif_summary(path) -> dict:
    """Best-effort shooting info: camera, lens, aperture, shutter speed
    (seconds), ISO, focal length (mm), date taken, pixel dimensions. Missing
    fields are simply absent from the returned dict -- most cameras don't
    populate every tag."""
    path = str(path)
    summary = {}

    try:
        summary["width"], summary["height"] = get_dimensions(path)
    except Exception:
        pass

    if is_raw(path):
        try:
            # Deliberately not rawpy's raw.other (iso_speed/shutter_speed/
            # aperture/focal_length/timestamp) -- measured at 1+ second per
            # call (it appears to trigger a much broader metadata parse
            # under the hood), which stalled the GUI thread for that long
            # every time a RAW photo was opened (here and in the photo
            # viewer). _read_tiff_exif_tags reads the same values straight
            # from the file's IFD0/EXIF-SubIFD in ~10ms -- also already
            # needed below for camera/lens, and for stacking.py's EXIF
            # analysis pass, so this now shares one fast code path with
            # both instead of paying rawpy's slow one here.
            ifd0_tags, sub_tags = _read_tiff_exif_tags(path)
            camera = _combine_make_model(ifd0_tags.get("Make"), ifd0_tags.get("Model"))
            if camera:
                summary["camera"] = camera
            lens = _combine_make_model(sub_tags.get("LensMake"), sub_tags.get("LensModel"))
            if lens:
                summary["lens"] = lens

            date = _format_exif_datetime(ifd0_tags.get("DateTime"))
            if date:
                summary["date"] = date
            shutter = _rational_to_float(sub_tags.get("ExposureTime"))
            if shutter:
                summary["shutter"] = shutter
            aperture = _rational_to_float(sub_tags.get("FNumber"))
            if aperture:
                summary["aperture"] = aperture
            iso = sub_tags.get("ISOSpeedRatings") or sub_tags.get("PhotographicSensitivity")
            if iso:
                summary["iso"] = iso
            focal_length = _rational_to_float(sub_tags.get("FocalLength"))
            if focal_length:
                summary["focal_length"] = focal_length
        except Exception:
            pass
    else:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
                camera = _combine_make_model(tags.get("Make"), tags.get("Model"))
                if camera:
                    summary["camera"] = camera
                date = _format_exif_datetime(tags.get("DateTime"))
                if date:
                    summary["date"] = date

                sub_tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.get_ifd(ExifTags.IFD.Exif).items()}
                shutter = _rational_to_float(sub_tags.get("ExposureTime"))
                if shutter:
                    summary["shutter"] = shutter
                aperture = _rational_to_float(sub_tags.get("FNumber"))
                if aperture:
                    summary["aperture"] = aperture
                iso = sub_tags.get("ISOSpeedRatings") or sub_tags.get("PhotographicSensitivity")
                if iso:
                    summary["iso"] = iso
                focal_length = _rational_to_float(sub_tags.get("FocalLength"))
                if focal_length:
                    summary["focal_length"] = focal_length
                lens = _combine_make_model(sub_tags.get("LensMake"), sub_tags.get("LensModel"))
                if lens:
                    summary["lens"] = lens
        except Exception:
            pass

    return summary


def save_image(image_float: np.ndarray, out_path, quality: int = 92):
    """Write a float32 [0,1] RGB array to disk. Format is inferred from out_path's extension."""
    out_path = Path(out_path)
    arr = np.clip(image_float, 0.0, 1.0)
    arr8 = (arr * 255.0 + 0.5).astype(np.uint8)
    img = Image.fromarray(arr8, mode="RGB")
    ext = out_path.suffix.lower()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if ext in (".jpg", ".jpeg"):
        img.save(out_path, format="JPEG", quality=quality, optimize=True)
    elif ext == ".png":
        img.save(out_path, format="PNG")
    elif ext in (".tif", ".tiff"):
        img.save(out_path, format="TIFF")
    else:
        raise ValueError(f"unsupported export extension: {ext}")


def scan_folder(folder, recursive: bool = True):
    """Yield paths of all supported photo files under folder."""
    folder = Path(folder)
    pattern = "**/*" if recursive else "*"
    for p in sorted(folder.glob(pattern)):
        if p.is_file() and is_supported(p):
            yield p
