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
BACKUPS_TO_KEEP = 7


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
            self._log_malformed(exc, "startup")
            self._recover_from_malformed()
            self._init_schema()
        self._daily_backup()

    def _daily_backup(self, keep: int = BACKUPS_TO_KEEP):
        """At most once a day, a consistent copy of the catalog (SQLite's
        own online-backup API, safe while in use) into catalog_backups/,
        keeping the newest `keep`. Ratings and edits exist nowhere but this
        file, and it has hit trouble three times. Skipped if the catalog
        fails a quick integrity check, so a damaged copy never rotates the
        good ones out. Never allowed to stop the app from starting."""
        try:
            backup_dir = self.db_path.parent / "catalog_backups"
            dest = backup_dir / f"catalog-{time.strftime('%Y%m%d')}.db"
            if dest.exists():
                return
            with self._lock:
                if self.conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    print("[catalog] quick_check failed -- skipping today's backup to keep older good ones")
                    return
                backup_dir.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                target = sqlite3.connect(str(tmp))
                try:
                    self.conn.backup(target)
                finally:
                    target.close()
                tmp.replace(dest)
            for old in sorted(backup_dir.glob("catalog-*.db"))[:-keep]:
                old.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 -- a failed backup must never block startup
            print(f"[catalog] daily backup failed: {exc}")

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
        # Rollback journal, not WAL. WAL mode kept hitting "database disk
        # image is malformed" on real interactive launches (never in scripted
        # replays of the same scan on a copy of the same file, which always
        # passed integrity_check) -- the signature of a damaged WAL index,
        # the memory-mapped -shm file, most likely disturbed by something
        # outside the app. A rollback journal has no -shm at all. Measured on
        # the real 21k-photo catalog: the bulk folder-scan transaction is the
        # same speed (46ms vs 50ms); a single rating commit goes from ~0ms to
        # ~5ms, still imperceptible. Switching an existing WAL database
        # checkpoints any pending WAL content into the main file first, so
        # nothing is lost on upgrade. If another connection (e.g. a second
        # instance) still holds the file in WAL mode the switch fails with
        # "database is locked" -- that must not stop the app starting, so it
        # carries on in WAL until the next start instead.
        self.conn.execute("PRAGMA busy_timeout=2000")
        try:
            mode = self.conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        except sqlite3.OperationalError as exc:
            mode = f"unchanged ({exc})"
        if mode.lower() != "delete":
            print(f"[catalog] couldn't switch to journal_mode=DELETE: {mode} -- database in use elsewhere? continuing")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def _recover_from_malformed(self):
        """Reconnect after a "database disk image is malformed" error and let
        the caller retry once.

        This used to delete the -wal and -shm files before reconnecting.
        That could silently throw away committed changes (ratings, flags,
        edits): in WAL mode a commit lives only in the -wal file until it's
        checkpointed into the main file. No sidecar file is ever deleted here
        now -- in rollback-journal mode a leftover -journal file is what
        SQLite needs to undo a half-finished write, so deleting it would
        risk real corruption.
        """
        try:
            self.conn.close()
        except Exception:
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

        "database disk image is malformed" is retried with a fresh
        connection, backing off over ~8s. It has hit this exact call on real
        launches (twice on 2026-09-30, once already in rollback-journal mode)
        while every check of the file itself -- before, straight after, and
        replaying the same scan on copies -- found nothing wrong: the file
        looks damaged for a moment, then doesn't. A single immediate retry
        wasn't enough. Every occurrence is written to logs/catalog.log with
        what the file looked like at that instant, since the cause is still
        unknown and the app otherwise has nowhere to report it (pythonw has
        no console).
        """
        delays = (0.5, 1.0, 2.0, 4.0)
        for attempt in range(len(delays) + 1):
            try:
                result = self._get_or_create_photos_bulk_once(paths)
                if attempt:
                    self._log_event(f"folder scan succeeded on retry {attempt}")
                return result
            except Exception as exc:
                if not self._is_malformed_error(exc):
                    raise
                self._log_malformed(exc, f"folder scan, attempt {attempt + 1} of {len(delays) + 1}")
                if attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
                with self._lock:
                    self._recover_from_malformed()

    def _log_event(self, message: str):
        """Appends to logs/catalog.log next to the catalog, keeping it under
        ~1MB. Best-effort: logging must never itself break the catalog."""
        try:
            log_dir = self.db_path.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "catalog.log"
            if log_path.exists() and log_path.stat().st_size > 1_000_000:
                log_path.replace(log_dir / "catalog.log.old")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {message}\n")
        except Exception:  # noqa: BLE001
            pass
        print(f"[catalog] {message}")

    def _log_malformed(self, exc: Exception, context: str):
        """Records a "malformed" error with a snapshot of the file at that
        instant: whether it checks out through the live connection and
        through a brand-new one, its size/mtime, and any sidecar files."""
        import traceback

        lines = [f"'database disk image is malformed' during {context}: {exc}", traceback.format_exc().rstrip()]
        live = getattr(self, "conn", None)  # may not exist yet if connecting itself failed
        for label, conn in ((("live connection", live),) if live is not None else ()) + (("fresh connection", None),):
            try:
                c = conn if conn is not None else sqlite3.connect(str(self.db_path), timeout=5)
                try:
                    result = [tuple(r) for r in c.execute("PRAGMA quick_check(5)").fetchall()]
                    lines.append(f"  quick_check via {label}: {result}")
                finally:
                    if conn is None:
                        c.close()
            except Exception as e2:  # noqa: BLE001
                lines.append(f"  quick_check via {label} raised {type(e2).__name__}: {e2}")
        try:
            st = self.db_path.stat()
            lines.append(f"  file: {st.st_size} bytes, modified {time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}")
            sidecars = [p.name for p in self.db_path.parent.glob(self.db_path.name + "-*")]
            lines.append(f"  sidecar files: {sidecars or 'none'}")
        except OSError as e3:
            lines.append(f"  stat failed: {e3}")
        self._log_event("\n".join(lines))

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
