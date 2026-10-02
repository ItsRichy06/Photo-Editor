"""Disk-cached thumbnail generation.

Uses camera-embedded preview JPEGs for RAW files where available (fast: no
demosaic needed) and caches everything on disk keyed by path+mtime+size so
re-browsing a folder is instant.
"""

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

from . import image_io
from .catalog import app_data_dir

THUMB_CACHE_DIR = app_data_dir() / "thumb_cache"
THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Bump whenever thumbnail-generation logic changes (e.g. an orientation fix)
# so previously-cached thumbnails -- keyed only by the *source photo's* path
# and mtime, which don't change when this code does -- get regenerated
# instead of silently serving stale output forever.
CACHE_VERSION = 2


def _cache_path(path: str, mtime: float, size: int) -> Path:
    key = f"v{CACHE_VERSION}|{path}|{mtime}|{size}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return THUMB_CACHE_DIR / f"{digest}.jpg"


def move_cached(old_path: str, new_path: str, mtime: float, size: int = 320):
    """After a file move (same mtime, new path), carry its cached thumbnail
    over so the moved photo shows up instantly. Best-effort."""
    old = _cache_path(old_path, mtime, size)
    if old.exists():
        try:
            old.replace(_cache_path(new_path, mtime, size))
        except OSError:
            pass


def get_thumbnail(path, size: int = 320) -> Image.Image:
    """Return a PIL.Image thumbnail (RGB, long edge <= size)."""
    path = Path(path)
    mtime = path.stat().st_mtime
    cache_file = _cache_path(str(path), mtime, size)

    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGB")
        except Exception:
            pass

    img = image_io.extract_embedded_preview(path)
    if img is None:
        arr = image_io.load_preview_float(path, max_dim=size)
        img = Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8))

    img = img.copy()
    img.thumbnail((size, size), Image.LANCZOS)

    try:
        img.convert("RGB").save(cache_file, format="JPEG", quality=85)
    except Exception:
        pass

    return img
