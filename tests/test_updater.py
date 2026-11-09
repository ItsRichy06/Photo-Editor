"""Tests app/updater.py against a local HTTP server that behaves like the
portal: /feed/<manifest> and /download/<file> (with HTTP range support).

Covers version comparison, the happy path (newer version found, downloaded,
hash-verified), choosing the small update-only package vs the full
installer by runtime fingerprint, resuming an interrupted download, and
every failure mode this module is designed to survive without disrupting
the app -- including "manifest names a file that isn't there" (the bug that
silently broke updates in the app this was modeled on) and a manifest that
isn't this app's at all (a real collision, see CHANGELOG.md 1.9.0).

Not wired into any CI (this project doesn't have any) -- run directly:
    .venv\\Scripts\\python.exe tests\\test_updater.py
"""
import sys, os
from pathlib import Path

# FOTOEDITOR_SRC lets this run against an installed copy's own app/ tree
# (e.g. C:\Users\...\Programs\PhotoEditor) instead of this source checkout.
sys.path.insert(0, os.environ.get("FOTOEDITOR_SRC", str(Path(__file__).resolve().parent.parent)))

import hashlib
import json
import tempfile
import threading
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app import updater

# ------------------------------------------------------------- version parsing
print("=== _parse_version ===")
assert updater._parse_version("1.9.0") == (1, 9, 0)
assert updater._parse_version("1.10.0") > updater._parse_version("1.9.0"), (
    "numeric comparison required -- lexicographic would rank 1.9.0 above 1.10.0"
)
print("version parsing OK")

# ------------------------------------------------------------- packaged detection
print("=== _is_packaged_install ===")
with mock.patch("sys.executable", r"C:\Users\Richard\AppData\Local\Programs\PhotoEditor\runtime\pythonw.exe"):
    assert updater._is_packaged_install() is True
with mock.patch("sys.executable", r"/\.venv\Scripts\python.exe"):
    assert updater._is_packaged_install() is False
print("packaged-install detection OK")


# ------------------------------------------------------------- fake portal
class FakePortal:
    """Serves files from a temp dir the way the portal does; records every
    request (path + Range header) so tests can assert what was fetched."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "releases"
        self.dir.mkdir()
        self.requests = []
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                portal.requests.append((self.path, self.headers.get("Range")))
                parts = self.path.strip("/").split("/")
                if len(parts) != 2 or parts[0] not in ("feed", "download"):
                    return self.send_error(404)
                f = portal.dir / parts[1]
                if not f.is_file():
                    return self.send_error(404)
                data = f.read_bytes()
                rng = self.headers.get("Range")
                if rng and parts[0] == "download":
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(data):
                        return self.send_error(416)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                    data = data[start:]
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.downloads = Path(self.tmp.name) / "downloads"
        self.patches = [
            mock.patch.object(updater, "FEED_URL", f"{base}/feed/{updater.MANIFEST_NAME}"),
            mock.patch.object(updater, "DOWNLOAD_URL", f"{base}/download/"),
            mock.patch.object(updater, "DOWNLOAD_DIR", self.downloads),
            mock.patch.object(updater, "_installed_runtime_id", lambda: self.installed_runtime),
        ]
        self.installed_runtime = None

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.server.shutdown()
        self.tmp.cleanup()

    def publish(self, name, content):
        (self.dir / name).write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def manifest(self, **fields):
        (self.dir / updater.MANIFEST_NAME).write_text(json.dumps(fields), encoding="utf-8")

    def check(self):
        worker = updater._UpdateCheckWorker()
        events = {"available": None, "finished": False}
        worker.updateAvailable.connect(lambda v, p, n: events.update(available=(v, p, n)))
        worker.finished.connect(lambda: events.update(finished=True))
        worker.run()  # the real entry point: must never raise
        assert events["finished"], "finished must always fire"
        return events["available"]

    def downloads_requested(self):
        return [r for r in self.requests if r[0].startswith("/download/")]


FULL = b"full installer bytes " * 1000
SMALL = b"small update bytes " * 50

print("=== no manifest published (404) ===")
with FakePortal() as p:
    assert p.check() is None
print("OK: no manifest -> silent no-op")

print("=== manifest advertises the current version ===")
with FakePortal() as p:
    sha = p.publish("PhotoEditor-Setup-1.0.0.exe", FULL)
    p.manifest(version=updater.__version__, installer="PhotoEditor-Setup-1.0.0.exe", sha256=sha)
    assert p.check() is None
    assert not p.downloads_requested(), "nothing should be downloaded when already current"
print("OK: same version -> no update offered, nothing downloaded")

print("=== a different app's manifest schema (Video Editor's 'url' instead of 'installer') ===")
with FakePortal() as p:
    p.manifest(version="99.0.0", url="VideoEditorSetup-1.3.0.exe", sha256="3d8cc4b5" * 8)
    assert p.check() is None
print("OK: a schema-mismatched other app's manifest is safely ignored")

print("=== same-schema manifest, but the installer name isn't this app's ===")
with FakePortal() as p:
    sha = p.publish("SomeOtherApp-Setup.exe", FULL)
    p.manifest(version="99.0.0", installer="SomeOtherApp-Setup.exe", sha256=sha)
    assert p.check() is None
    assert not p.downloads_requested()
print("OK: refuses a non-PhotoEditor installer name before downloading anything")

print("=== happy path: install without a runtime fingerprint gets the full installer ===")
with FakePortal() as p:
    full_sha = p.publish("PhotoEditor-Setup-99.0.0.exe", FULL)
    small_sha = p.publish("PhotoEditor-Update-99.0.0.exe", SMALL)
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256=full_sha,
               update_installer="PhotoEditor-Update-99.0.0.exe", update_sha256=small_sha,
               runtime_id="abc123", notes="Big fake release")
    p.installed_runtime = None
    version, path, notes = p.check()
    assert (version, notes) == ("99.0.0", "Big fake release")
    assert Path(path).name == "PhotoEditor-Setup-99.0.0.exe" and Path(path).read_bytes() == FULL
print("OK: no installed fingerprint -> full installer, downloaded and verified")

print("=== matching runtime fingerprint gets the small update-only package ===")
with FakePortal() as p:
    full_sha = p.publish("PhotoEditor-Setup-99.0.0.exe", FULL)
    small_sha = p.publish("PhotoEditor-Update-99.0.0.exe", SMALL)
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256=full_sha,
               update_installer="PhotoEditor-Update-99.0.0.exe", update_sha256=small_sha, runtime_id="abc123")
    p.installed_runtime = "abc123"
    _, path, _ = p.check()
    assert Path(path).name == "PhotoEditor-Update-99.0.0.exe" and Path(path).read_bytes() == SMALL
    assert all("Setup" not in r[0] for r in p.downloads_requested()), "must not also fetch the full installer"
print("OK: matching fingerprint -> small package only")

print("=== different runtime fingerprint gets the full installer ===")
with FakePortal() as p:
    full_sha = p.publish("PhotoEditor-Setup-99.0.0.exe", FULL)
    small_sha = p.publish("PhotoEditor-Update-99.0.0.exe", SMALL)
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256=full_sha,
               update_installer="PhotoEditor-Update-99.0.0.exe", update_sha256=small_sha, runtime_id="new-runtime")
    p.installed_runtime = "old-runtime"
    _, path, _ = p.check()
    assert Path(path).name == "PhotoEditor-Setup-99.0.0.exe"
print("OK: runtime changed -> full installer")

print("=== manifest without update-only fields (pre-1.18 publish) gets the full installer ===")
with FakePortal() as p:
    full_sha = p.publish("PhotoEditor-Setup-99.0.0.exe", FULL)
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256=full_sha)
    p.installed_runtime = "abc123"
    _, path, _ = p.check()
    assert Path(path).name == "PhotoEditor-Setup-99.0.0.exe"
print("OK: old-style manifest -> full installer")

print("=== manifest names an installer that isn't published (the trading-app bug) ===")
with FakePortal() as p:
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256="0" * 64)
    assert p.check() is None
print("OK: drifted manifest -> 404 handled, no false update offer")

print("=== downloaded file's hash doesn't match the manifest ===")
with FakePortal() as p:
    p.publish("PhotoEditor-Setup-99.0.0.exe", b"corrupted or wrong file")
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256="0" * 64)
    assert p.check() is None
    assert not any(p.downloads.iterdir()), "a file that failed verification must not be left behind"
print("OK: hash mismatch -> no update offered, download discarded")

print("=== interrupted download resumes with a range request ===")
with FakePortal() as p:
    sha = p.publish("PhotoEditor-Setup-99.0.0.exe", FULL)
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256=sha)
    p.downloads.mkdir()
    (p.downloads / "PhotoEditor-Setup-99.0.0.exe.part").write_bytes(FULL[:5000])  # left by a previous launch
    _, path, _ = p.check()
    assert Path(path).read_bytes() == FULL
    assert p.downloads_requested() == [("/download/PhotoEditor-Setup-99.0.0.exe", "bytes=5000-")]
    assert not (p.downloads / "PhotoEditor-Setup-99.0.0.exe.part").exists()
print("OK: resumed from byte 5000 and verified")

print("=== already downloaded and verified on an earlier launch (user clicked Later) ===")
with FakePortal() as p:
    sha = p.publish("PhotoEditor-Setup-99.0.0.exe", FULL)
    p.manifest(version="99.0.0", installer="PhotoEditor-Setup-99.0.0.exe", sha256=sha)
    p.downloads.mkdir()
    (p.downloads / "PhotoEditor-Setup-99.0.0.exe").write_bytes(FULL)
    (p.downloads / "PhotoEditor-Setup-98.0.0.exe").write_bytes(b"an older download")
    _, path, _ = p.check()
    assert Path(path).read_bytes() == FULL
    assert not p.downloads_requested(), "a verified earlier download must be reused, not fetched again"
    assert not (p.downloads / "PhotoEditor-Setup-98.0.0.exe").exists(), "older downloads should be cleaned up"
print("OK: reused the verified download, removed the stale one")

print("=== malformed manifest JSON ===")
with FakePortal() as p:
    (p.dir / updater.MANIFEST_NAME).write_text("{not valid json", encoding="utf-8")
    assert p.check() is None
print("OK: malformed manifest -> no offer, no crash")

print("=== portal unreachable ===")
with FakePortal() as p:
    port = p.server.server_port
    p.server.shutdown()
    p.server.server_close()
    assert p.check() is None
print("OK: unreachable portal -> no offer, no crash")

print("=== the real feed URL points at the portal, not the old LAN share ===")
assert updater.FEED_URL == "https://nas.taila13a0a.ts.net/apps/photo-editor/feed/photoeditor-latest.json"
assert not hasattr(updater, "UPDATE_FEED_DIR")
print("OK")

print("\nALL UPDATER TESTS PASSED")
