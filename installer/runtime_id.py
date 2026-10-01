"""Fingerprint of the bundled Python runtime (runtime_build/runtime).

The small "update only" installer (PhotoEditor-Update.iss) ships app code
without the ~4.8 GB runtime, so it may only be applied on top of exactly the
runtime it was built against. This computes a fingerprint from what is
actually bundled -- the relative path and size of every file, so installing,
removing or upgrading any package changes it -- and writes it into the
runtime as runtime_id.txt, which the full installer then carries onto every
machine. The update installer compares the installed copy of that file
against the value it was built with and refuses to run on a mismatch.

Derived, never typed by hand: a fingerprint that wrongly said "unchanged"
would put new app code on an old runtime.

    python installer/runtime_id.py            # print it, (re)write runtime_id.txt
    python installer/runtime_id.py --check    # print it, exit 1 if runtime_id.txt is stale
"""

import hashlib
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime_build" / "runtime"
ID_FILE = "runtime_id.txt"


def compute(runtime: Path = RUNTIME) -> str:
    entries = []
    for p in runtime.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(runtime)
        # Same exclusions as the installer's [Files] entry, plus the id file itself.
        if "__pycache__" in rel.parts or p.suffix == ".pyc" or rel.as_posix() == ID_FILE:
            continue
        entries.append(f"{rel.as_posix().lower()}\t{p.stat().st_size}")
    if not entries:
        raise SystemExit(f"no runtime found at {runtime}")
    digest = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()
    return digest[:16]


def main():
    runtime_id = compute()
    id_path = RUNTIME / ID_FILE
    if "--check" in sys.argv:
        current = id_path.read_text(encoding="ascii").strip() if id_path.exists() else None
        print(runtime_id)
        sys.exit(0 if current == runtime_id else 1)
    id_path.write_text(runtime_id + "\n", encoding="ascii")
    print(runtime_id)


if __name__ == "__main__":
    main()
