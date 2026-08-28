"""Tests app/updater.py: version comparison, the happy path (newer version
found, downloaded, hash-verified), and every failure mode this module is
specifically designed to survive without disrupting the app -- including
the exact "manifest names a file that isn't there" bug that silently broke
updates in the app this was modeled on, and the real cross-app manifest
collision discovered while first setting this up (see CHANGELOG.md 1.9.0
and app/updater.py's own comments for the full story).

Not wired into any CI (this project doesn't have any) -- run directly:
    .venv\\Scripts\\python.exe tests\\test_updater.py
"""
import sys, os
from pathlib import Path

# FOTOEDITOR_SRC lets this run against an installed copy's own app/ tree
# (e.g. C:\Users\...\Programs\PhotoEditor) instead of this source checkout --
# used throughout this project's testing to verify the actual installed
# code, not just "does the interpreter agree with the checkout".
sys.path.insert(0, os.environ.get("FOTOEDITOR_SRC", str(Path(__file__).resolve().parent.parent)))

import json
import hashlib
import tempfile
import unittest.mock as mock

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app import updater

# ------------------------------------------------------------- version parsing
print("=== _parse_version ===")
assert updater._parse_version("1.9.0") == (1, 9, 0)
assert updater._parse_version("1.10.0") > updater._parse_version("1.9.0"), (
    "numeric comparison required -- lexicographic would rank 1.9.0 above 1.10.0"
)
assert updater._parse_version("1.9.0") == updater._parse_version("1.9.0")
print("version parsing OK")

# ------------------------------------------------------------- packaged detection
print("=== _is_packaged_install ===")
with mock.patch("sys.executable", r"C:\Users\Richard\AppData\Local\Programs\PhotoEditor\runtime\pythonw.exe"):
    assert updater._is_packaged_install() is True
with mock.patch("sys.executable", r"C:\Users\Richard\Documents\Foto Editor\.venv\Scripts\python.exe"):
    assert updater._is_packaged_install() is False
print("packaged-install detection OK")


# ------------------------------------------------------------- test harness
def make_worker_and_feed(tmp_dir):
    feed_dir = Path(tmp_dir) / "feed"
    feed_dir.mkdir()
    updater.UPDATE_FEED_DIR = feed_dir
    worker = updater._UpdateCheckWorker()
    events = {"available": None}
    worker.updateAvailable.connect(lambda v, p, n: events.update(available=(v, p, n)))
    return worker, feed_dir, events


def write_manifest(feed_dir, version, installer_name, sha256, notes="test notes"):
    # Must track the real MANIFEST_NAME, not a hardcoded guess -- a stale
    # hardcoded name here would make _check() silently find no manifest at
    # all (the "no manifest present" case), which happens to also satisfy
    # several other assertions for the wrong reason. Exactly this drifted
    # out of sync with app/updater.py once already while writing this file.
    (feed_dir / updater.MANIFEST_NAME).write_text(json.dumps({
        "version": version, "installer": installer_name, "sha256": sha256, "notes": notes,
    }), encoding="utf-8")


def write_fake_installer(feed_dir, name, content=b"fake installer bytes"):
    path = feed_dir / name
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


# ------------------------------------------------------------- no manifest at all
print("=== no manifest present ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    worker._check()  # must not raise
    assert events["available"] is None
print("OK: no manifest -> silent no-op")

# ------------------------------------------------------------- already current / older
print("=== manifest advertises current or older version ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    write_manifest(feed_dir, updater.__version__, "PhotoEditor-Setup.exe", "0" * 64)
    worker._check()
    assert events["available"] is None
print("OK: same version -> no update offered")

# ---------------------------------------- cross-app collision (real, not hypothetical)
# This actually happened while setting this up: the feed directory turned
# out to be shared with an unrelated app (Video Editor), whose manifest
# uses a different schema entirely ("url" instead of "installer"). Confirm
# reading someone else's manifest degrades safely rather than crashing or,
# worse, offering to install the wrong app.
print("=== a different app's manifest entirely sits at the same feed dir ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    (feed_dir / "latest.json").write_text(json.dumps({
        "version": "1.3.0", "url": "VideoEditorSetup-1.3.0.exe",
        "sha256": "3d8cc4b5" * 8, "size": 214354089, "releaseDate": "2026-08-28T13:54:21Z",
    }), encoding="utf-8")
    updater.MANIFEST_NAME = "latest.json"  # simulate reading the wrong file
    try:
        finished = {"fired": False}
        worker.finished.connect(lambda: finished.update(fired=True))
        worker.run()  # the real entry point -- must not crash
    finally:
        updater.MANIFEST_NAME = "photoeditor-latest.json"
    assert events["available"] is None, "must never offer to install a different app's installer"
    assert finished["fired"]
print("OK: a schema-mismatched other app's manifest is safely ignored")

# -------------------------- content sanity check (same schema, still not us)
# A hash check alone would happily "verify" a same-schema manifest for a
# totally different app -- it only proves the download matches what the
# manifest says, not that the manifest is actually this app's. This is
# the check that catches that specific case.
print("=== same-schema manifest, but the installer name isn't this app's ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    sha = write_fake_installer(feed_dir, "SomeOtherApp-Setup.exe")
    write_manifest(feed_dir, "99.0.0", "SomeOtherApp-Setup.exe", sha)
    worker._check()
    assert events["available"] is None, "must refuse a same-schema manifest for a different app"
print("OK: content sanity check refuses a non-PhotoEditor installer name")

# ------------------------------------------------------------- the happy path
print("=== newer version, installer present, hash matches ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    sha = write_fake_installer(feed_dir, "PhotoEditor-Setup.exe")
    write_manifest(feed_dir, "99.0.0", "PhotoEditor-Setup.exe", sha, notes="Big fake release")
    worker._check()
    assert events["available"] is not None, "expected updateAvailable to fire"
    version, local_path, notes = events["available"]
    assert version == "99.0.0"
    assert notes == "Big fake release"
    assert Path(local_path).exists(), "verified installer should be copied to local disk"
    assert Path(local_path).read_bytes() == b"fake installer bytes"
print("OK: happy path fires updateAvailable with a verified local copy")

# ------------------------------------------------------------- THE bug this is modeled to survive
print("=== manifest names an installer that doesn't actually exist (the trading-app bug) ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    write_manifest(feed_dir, "99.0.0", "PhotoEditor-Setup.exe", "0" * 64)  # never wrote the file
    worker._check()  # must not raise
    assert events["available"] is None, "must NOT offer an update it can't actually deliver"
print("OK: drifted manifest -> no crash, no false update offer (would need the log line to notice)")

# ------------------------------------------------------------- hash mismatch (corrupted/wrong file)
print("=== installer present but hash doesn't match the manifest ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    write_fake_installer(feed_dir, "PhotoEditor-Setup.exe", content=b"corrupted or wrong file")
    write_manifest(feed_dir, "99.0.0", "PhotoEditor-Setup.exe", "0" * 64)  # hash won't match
    worker._check()
    assert events["available"] is None, "must NOT offer an update whose hash doesn't verify"
print("OK: hash mismatch -> no update offered")

# ------------------------------------------------------------- malformed manifest
print("=== malformed manifest JSON ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    (feed_dir / updater.MANIFEST_NAME).write_text("{not valid json", encoding="utf-8")
    try:
        worker._check()
    except Exception:
        pass  # _check() itself may raise; run() (tested below) is what catches it
    assert events["available"] is None
print("OK: malformed manifest doesn't produce a false update offer")

# ------------------------------------------------------------- run() never raises, always finishes
print("=== run() swallows everything and always emits finished ===")
with tempfile.TemporaryDirectory() as tmp:
    worker, feed_dir, events = make_worker_and_feed(tmp)
    (feed_dir / updater.MANIFEST_NAME).write_text("{not valid json", encoding="utf-8")
    finished = {"fired": False}
    worker.finished.connect(lambda: finished.update(fired=True))
    worker.run()  # must not raise, no matter how broken the feed is
    assert finished["fired"], "finished must fire even when the check itself blew up"
print("OK: run() is fully exception-safe")

print("\nALL UPDATER TESTS PASSED")
