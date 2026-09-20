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
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL + NORMAL sync: commits no longer force a full fsync-per-row, which
        # is what made scanning a folder of thousands of photos crawl.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.RLock()
        self._init_schema()

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
        """
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
                self.conn.rollback()
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

    def set_rating(self, photo_id: int, rating: int):
        rating = max(0, min(5, int(rating)))
        with self._lock:
            self.conn.execute("UPDATE photos SET rating=? WHERE id=?", (rating, photo_id))
            self.conn.commit()

    def set_flag(self, photo_id: int, flag: str):
        if flag not in ("none", "pick", "reject"):
            raise ValueError(f"invalid flag: {flag}")
        with self._lock:
            self.conn.execute("UPDATE photos SET flag=? WHERE id=?", (flag, photo_id))
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
