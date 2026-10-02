"""Moving photos into folders on disk ("Group into Folder").

Real moves, not an in-app album: the new folder shows up in Explorer too.
Each selected photo takes its same-named siblings along (DSC0412.ARW,
DSC0412.JPG, DSC0412.xmp...), so a RAW+JPEG pair never gets split. Nothing
is ever overwritten: a file whose name is already taken in the destination
is skipped and reported. The catalog is updated for every moved photo, so
ratings, flags and edits follow the file.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import thumbnails

_INVALID_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def folder_name_problem(name: str) -> str | None:
    """Why `name` can't be a Windows folder name, or None if it's fine."""
    if not name or not name.strip():
        return "Enter a folder name."
    if name != name.strip() or name.endswith("."):
        return "A folder name can't start or end with a space, or end with a dot."
    if any(c in _INVALID_CHARS for c in name):
        return 'A folder name can\'t contain any of  < > : " / \\ | ? *'
    if name.split(".")[0].upper() in _RESERVED:
        return f'"{name}" is reserved by Windows.'
    if len(name) > 120:
        return "That name is too long."
    return None


@dataclass
class MoveResult:
    moved: list = field(default_factory=list)     # [(old_path, new_path)] files actually moved
    skipped: list = field(default_factory=list)   # [(path, reason)]

    @property
    def photo_count(self) -> int:
        return len(self.moved)


def plan_moves(photo_paths, dest_dir) -> list[tuple[Path, Path]]:
    """(src, dst) for every file to move: each photo plus same-stem siblings
    in its own folder. Files already in dest_dir are left out."""
    dest_dir = Path(dest_dir)
    plan, seen = [], set()
    for p in photo_paths:
        p = Path(p)
        if p.parent.resolve() == dest_dir.resolve():
            continue
        stem = p.stem.lower()
        try:
            siblings = [s for s in p.parent.iterdir() if s.is_file() and s.stem.lower() == stem]
        except OSError:
            siblings = [p]
        for s in siblings or [p]:
            key = str(s.resolve()).lower()
            if key not in seen:
                seen.add(key)
                plan.append((s, dest_dir / s.name))
    return plan


def move_into_folder(photo_paths, dest_dir, catalog) -> MoveResult:
    """Creates dest_dir if needed, moves the photos (and siblings) into it,
    and re-points their catalog rows. Never overwrites anything."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = MoveResult()
    for src, dst in plan_moves(photo_paths, dest_dir):
        if dst.exists():
            result.skipped.append((str(src), f"a file named {dst.name} is already in {dest_dir.name}"))
            continue
        try:
            st = src.stat()
            os.rename(src, dst)  # same volume (a subfolder) -> a fast rename; never overwrites on Windows
        except OSError as exc:
            result.skipped.append((str(src), str(exc)))
            continue
        result.moved.append((str(src.resolve()), str(dst.resolve())))
        thumbnails.move_cached(str(src), str(dst), st.st_mtime)
    if result.moved:
        catalog.move_photo_paths(result.moved)
    return result
