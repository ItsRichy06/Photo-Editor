"""Local SQLite catalog: per-photo ratings, pick/reject flags, and non-destructive
edit recipes. Keyed by absolute file path. Original photo files are never touched --
this database is the only place ratings/edits live."""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_DB_NAME = "catalog.db"


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "PhotoEditor"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Catalog:
    """Not just single-connection-safe: the library view scans folders on a
    background QThread while the UI thread may read/write ratings at the same
    time, so every call is serialized through a lock."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path) if db_path else (app_data_dir() / DEFAULT_DB_NAME)
        self._lock = threading.RLock()
        try:
            self._connect()
            self._init_schema()
        except Exception as exc:
            if not self._is_malformed_error(exc):
                raise
            print(f"[catalog] 'database disk image is malformed' at startup -- recovering and retrying: {exc}")
            self._recover_from_malformed()
            self._init_schema()

    def _connect(self):
        # timeout=30: how long a write waits for another connection's lock to
        # clear before raising "database is locked", instead of Python's
        # sqlite3 default of 5s -- found too short the hard way: a second
        # short-lived process (e.g. a diagnostic script) briefly writing to
        # this same file was enough to make a folder scan's own write throw,
        # which (see ThumbnailLoaderThread.run()'s matching fix) used to kill
        # the whole scan silently, leaving the library stuck on "Scanning..."
        # forever with no error shown and no way to recover short of
        # restarting the app.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # WAL + NORMAL sync: commits no longer force a full fsync-per-row, which
        # is what made scanning a folder of thousands of photos crawl.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def _recover_from_malformed(self):
        """Close, clear the WAL-mode sidecar files (-wal/-shm), and reconnect.

        Confirmed live, repeatedly, on a real installed copy: "database disk
        image is malformed" during an ordinary write, even though
        PRAGMA integrity_check on the same file moments before and after
        always reports "ok" -- this is WAL-mode shared-memory-index
        corruption, not actual page-data corruption (never reproduced from
        any scripted launch, only real interactive ones, which points at
        something like antivirus real-time scanning interfering with
        SQLite's memory-mapped -shm file, though the exact trigger was never
        pinned down). The -wal file only ever holds not-yet-checkpointed
        writes; on a sync=NORMAL/WAL setup those are the most recent
        uncommitted ones, so this can lose a just-in-flight write, but the
        alternative -- the catalog silently refusing to open at all for the
        rest of the session, exactly what this replaced -- is worse. Deleting
        and letting SQLite regenerate both fresh has reliably recovered every
        single time this was hit and fixed by hand before this method
        existed.
        """
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(self.db_path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
        self._connect()

    def _is_malformed_error(self, exc: Exception) -> bool:
        return isinstance(exc, sqlite3.DatabaseError) and "malformed" in str(exc).lower()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                size INTEGER,
                mtime REAL,
                width INTEGER,
                height INTEGER,
                rating INTEGER DEFAULT 0,
                flag TEXT DEFAULT 'none',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS edits (
                photo_id INTEGER PRIMARY KEY,
                recipe_json TEXT,
                updated_at TEXT,
                FOREIGN KEY(photo_id) REFERENCES photos(id)
            );
            CREATE INDEX IF NOT EXISTS idx_photos_path ON photos(path);
            """
        )
        # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in SQLite, and this
        # column was added after the table itself, so real existing catalogs
        # need a real migration rather than just extending the CREATE TABLE
        # above (which only runs for a brand-new database).
        self._add_column_if_missing("photos", "source_kind", "TEXT")
        # NULL for an unrated photo, else "manual" (set by the user) or
        # "auto" (filled in by Auto-Rate Unrated) -- lets the library
        # distinguish/filter the two separately from the rating value itself.
        self._add_column_if_missing("photos", "rating_source", "TEXT")
        self.conn.commit()

    def _add_column_if_missing(self, table: str, column: str, coltype: str):
        cols = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    def get_or_create_photo(self, path: str) -> dict:
        path = str(Path(path).resolve())
        with self._lock:
            row = self.conn.execute("SELECT * FROM photos WHERE path=?", (path,)).fetchone()
            if row:
                return dict(row)
            st = os.stat(path)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute(
                "INSERT INTO photos (path, size, mtime, rating, flag, created_at) "
                "VALUES (?, ?, ?, 0, 'none', ?)",
                (path, st.st_size, st.st_mtime, now),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM photos WHERE path=?", (path,)).fetchone()
            return dict(row)

    def get_or_create_photos_bulk(self, paths: list[str]) -> dict:
        """Register many paths in a single transaction and return {resolved_path: row}.

        Used by the folder scan instead of get_or_create_photo() in a loop --
        committing once per file (with fsync) for thousands of photos is the
        main reason a big library scan used to crawl.

        Retries once, recovering via _recover_from_malformed(), if this hits
        "database disk image is malformed" -- confirmed live as this exact
        call's failure mode; see _recover_from_malformed()'s docstring.
        """
        try:
            return self._get_or_create_photos_bulk_once(paths)
        except Exception as exc:
            if not self._is_malformed_error(exc):
                raise
            print(f"[catalog] 'database disk image is malformed' during folder scan -- recovering and retrying: {exc}")
            with self._lock:
                self._recover_from_malformed()
            return self._get_or_create_photos_bulk_once(paths)

    def _get_or_create_photos_bulk_once(self, paths: list[str]) -> dict:
        resolved = [str(Path(p).resolve()) for p in paths]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        result = {}
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for rp in resolved:
                    try:
                        st = os.stat(rp)
                    except OSError:
                        continue
                    self.conn.execute(
                        "INSERT OR IGNORE INTO photos (path, size, mtime, rating, flag, created_at) "
                        "VALUES (?, ?, ?, 0, 'none', ?)",
                        (rp, st.st_size, st.st_mtime, now),
                    )
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass  # connection itself may be broken (e.g. the malformed-db case) -- let the caller's retry handle it
                raise

            chunk_size = 500
            for i in range(0, len(resolved), chunk_size):
                chunk = resolved[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"SELECT * FROM photos WHERE path IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    result[row["path"]] = dict(row)
        return result

    def get_photo(self, photo_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
            return dict(row) if row else None

    def set_dimensions(self, photo_id: int, width: int, height: int):
        with self._lock:
            self.conn.execute(
                "UPDATE photos SET width=?, height=? WHERE id=?", (width, height, photo_id)
            )
            self.conn.commit()

    def set_rating(self, photo_id: int, rating: int, source: str = "manual"):
        """source: "manual" (the user rated it) or "auto" (Auto-Rate Unrated
        filled it in) -- ignored and stored as NULL when clearing a rating
        back to 0, since an unrated photo has no source to remember."""
        rating = max(0, min(5, int(rating)))
        if source not in ("manual", "auto"):
            raise ValueError(f"invalid rating source: {source}")
        stored_source = source if rating > 0 else None
        with self._lock:
            self.conn.execute(
                "UPDATE photos SET rating=?, rating_source=? WHERE id=?",
                (rating, stored_source, photo_id),
            )
            self.conn.commit()

    def set_flag(self, photo_id: int, flag: str):
        if flag not in ("none", "pick", "reject"):
            raise ValueError(f"invalid flag: {flag}")
        with self._lock:
            self.conn.execute("UPDATE photos SET flag=? WHERE id=?", (flag, photo_id))
            self.conn.commit()

    def delete_photos(self, photo_ids: list[int]):
        """Removes these photos' rows (and their edit recipes) from the
        catalog. Does not touch the actual files -- see LibraryView's
        delete flow, which sends the real files to the Recycle Bin
        separately and only calls this after that succeeds."""
        if not photo_ids:
            return
        with self._lock:
            placeholders = ",".join("?" for _ in photo_ids)
            self.conn.execute(f"DELETE FROM edits WHERE photo_id IN ({placeholders})", photo_ids)
            self.conn.execute(f"DELETE FROM photos WHERE id IN ({placeholders})", photo_ids)
            self.conn.commit()

    def set_source_kind(self, photo_id: int, kind: str | None):
        """kind: None for a normal photo, "focus" or "hdr" for a photo
        produced by the Stack Photos feature -- lets the library filter to
        just the merged outputs instead of every source frame that went
        into them."""
        if kind not in (None, "focus", "hdr"):
            raise ValueError(f"invalid source_kind: {kind}")
        with self._lock:
            self.conn.execute("UPDATE photos SET source_kind=? WHERE id=?", (kind, photo_id))
            self.conn.commit()

    def get_edit_recipe(self, photo_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT recipe_json FROM edits WHERE photo_id=?", (photo_id,)
            ).fetchone()
        if row and row["recipe_json"]:
            return json.loads(row["recipe_json"])
        return None

    def set_edit_recipe(self, photo_id: int, recipe: dict):
        recipe_json = json.dumps(recipe)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO edits (photo_id, recipe_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET
                    recipe_json=excluded.recipe_json,
                    updated_at=excluded.updated_at
                """,
                (photo_id, recipe_json, now),
            )
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()
