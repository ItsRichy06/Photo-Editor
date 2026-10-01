"""Best-effort auto-update check against the app's release feed on the portal.

Modeled on the update mechanism in Richard's other desktop app (Stock
Signals, Electron + electron-updater against a "latest.yml"-style feed) --
same shape, adapted for this app's Python/Inno Setup stack instead of
Electron/NSIS:
  - check once per launch, never on a timer
  - never fatal: any failure (portal unreachable, malformed manifest,
    missing/mismatched file, hash mismatch) is logged and silently
    swallowed -- the app must work exactly as if this file didn't exist
  - the whole new installer is downloaded to local disk and hash-verified
    BEFORE the user is ever told about it, so there's nothing to wait for
    once they say yes
  - the small "update only" package (app code, ~2 MB) is used when the
    installed runtime's fingerprint matches the one it was built for (see
    installer/runtime_id.py); otherwise the full ~2.7 GB installer
  - the user decides when to install -- never forced, never silent
    without a click

One lesson carried over word-for-word from that app's own history: its
update feed once pointed at a filename that didn't match what was actually
uploaded, and updates silently stopped working for months with zero
visible error anywhere -- just a swallowed exception in a background
thread, the same shape of bug this module is equally capable of hiding.
installer/publish.ps1 cross-checks the manifest against the real file it
just copied for exactly that reason; the loud log line below on a mismatch
is this module's own side of that same guard.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox

from .version import __version__

log = logging.getLogger(__name__)

# The portal (Tailscale Funnel, public HTTPS) rather than the
# \\NAS\FamilyStorage share this used to read: that UNC path only resolves
# on the home LAN, so a remote install never saw an update -- a friend
# testing the app downloaded the full installer by hand for every release.
# The portal serves exactly what installer/publish.ps1 puts in
# \\NAS\SSD\FrontEnd\releases\photo-editor: the manifest from a declared
# whitelist (feed/), installers only by the names the manifest gives
# (download/), with HTTP range support.
PORTAL_URL = "https://nas.taila13a0a.ts.net/apps/photo-editor"
# Namespaced, not a generic "latest.json": the old feed folder turned out
# to be shared with Richard's Video Editor, whose own manifest sat there
# under exactly that name. Kept, along with the installer-name check below,
# so a manifest that isn't this app's can never be trusted.
MANIFEST_NAME = "photoeditor-latest.json"
FEED_URL = f"{PORTAL_URL}/feed/{MANIFEST_NAME}"
DOWNLOAD_URL = f"{PORTAL_URL}/download/"
HTTP_TIMEOUT = 30  # seconds per connect/read, not for the whole download
CHUNK = 1 << 20
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "PhotoEditor-update"


def _is_packaged_install() -> bool:
    """True only when running from the installed app's bundled runtime, not
    a dev .venv -- the same guard electron-updater's app.isPackaged gives
    the trading app. Checking for updates from a source checkout would just
    fail every time (no reason a dev tree should shadow the same path an
    install uses), so skip it entirely rather than let it become log noise
    every debug run."""
    exe = Path(sys.executable).resolve()
    return exe.parent.name.lower() == "runtime" and exe.parent.parent.name.lower() == "photoeditor"


def _parse_version(v: str) -> tuple:
    """'1.10.2' -> (1, 10, 2), so comparison is numeric, not lexicographic
    (lexicographic would rank '1.9.0' above '1.10.0')."""
    return tuple(int(p) for p in v.split("."))


class _UpdateCheckWorker(QObject):
    updateAvailable = Signal(str, str, str)  # new_version, local_installer_path, notes
    finished = Signal()

    def run(self):
        try:
            self._check()
        except Exception:
            # Network hiccup, NAS unreachable, malformed manifest, whatever --
            # this must never be allowed to disrupt the app itself. Log it so
            # it's findable, but nothing more.
            log.exception("update check failed (non-fatal)")
        finally:
            self.finished.emit()

    def _check(self):
        manifest = _fetch_manifest()
        if manifest is None:
            return

        remote_version = manifest["version"]
        notes = manifest.get("notes", "")
        if _parse_version(remote_version) <= _parse_version(__version__):
            log.info("update check: already on %s (feed has %s)", __version__, remote_version)
            return

        name, expected_sha256 = _choose_package(manifest)
        # Belt and suspenders on top of the namespaced manifest name: a
        # manifest that isn't this app's must never be trusted, and a hash
        # check alone can't tell -- it would happily "verify" a stranger's
        # installer against that stranger's (correct, but irrelevant) hash.
        if not _looks_like_ours(name):
            log.error("update check: manifest names installer %r, which isn't this app's -- refusing it", name)
            return

        local_path = _download_verified(name, expected_sha256)
        if local_path is None:
            return
        log.info("update check: %s -> %s downloaded and verified (%s)", __version__, remote_version, name)
        self.updateAvailable.emit(remote_version, str(local_path), notes)


def _installed_runtime_id() -> str | None:
    """The fingerprint the full installer put into the bundled runtime
    (installer/runtime_id.py). None for installs older than this mechanism,
    which therefore always get the full installer once."""
    try:
        return (Path(sys.executable).resolve().parent / "runtime_id.txt").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def _choose_package(manifest: dict) -> tuple[str, str]:
    """(filename, sha256) of the package to fetch: the small update-only one
    when this install's runtime is the one it was built for, else the full
    installer. PhotoEditor-Update.iss re-checks the same fingerprint itself
    before touching anything, so a wrong choice here can only make an
    update decline, never break an install."""
    small = manifest.get("update_installer")
    small_sha = manifest.get("update_sha256")
    wanted_runtime = manifest.get("runtime_id")
    if small and small_sha and wanted_runtime and wanted_runtime == _installed_runtime_id():
        return small, small_sha.lower()
    return manifest["installer"], manifest["sha256"].lower()


def _looks_like_ours(name: str) -> bool:
    return "photoeditor" in name.lower() and not any(c in name for c in "/\\") and ".." not in name


def _fetch_manifest() -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(FEED_URL, headers={"Cache-Control": "no-cache"}),
                                    timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.info("update check: no manifest at %s, skipping", FEED_URL)
            return None
        raise


def _download_verified(name: str, expected_sha256: str) -> Path | None:
    """Downloads DOWNLOAD_URL/<name> into DOWNLOAD_DIR and returns its path
    only if its sha256 matches -- an interrupted or corrupted download must
    never surface as "an update is ready". Resumes a partial download from
    an earlier launch with an HTTP range request, reuses a finished verified
    one (e.g. the user clicked Later last time), and hashes while streaming
    instead of reading a 2.7 GB file back into memory."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    final = DOWNLOAD_DIR / name
    part = DOWNLOAD_DIR / (name + ".part")
    for stale in DOWNLOAD_DIR.iterdir():
        if stale.name not in (final.name, part.name):
            stale.unlink(missing_ok=True)

    if final.exists():
        if _sha256_of(final) == expected_sha256:
            return final
        final.unlink()

    have = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(DOWNLOAD_URL + urllib.parse.quote(name))
    if have:
        request.add_header("Range", f"bytes={have}-")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
            mode = "ab" if (have and resp.status == 206) else "wb"
            with open(part, mode) as out:
                while chunk := resp.read(CHUNK):
                    out.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # The manifest advertises a file that isn't actually published --
            # name drift between build and publish, the failure that silently
            # broke the trading app's updates for months. A background check
            # has no dialog, so a loud log line is the only signal.
            log.error("update check: manifest names %s but %s%s is 404 -- publish.ps1's upload and "
                      "the manifest have drifted out of sync", name, DOWNLOAD_URL, name)
            return None
        if exc.code == 416 and have:  # .part already complete or longer than the file
            part.unlink(missing_ok=True)
            return None
        raise

    actual = _sha256_of(part)
    if actual != expected_sha256:
        log.error("update check: downloaded %s hash mismatch (expected %s, got %s) -- discarding",
                  name, expected_sha256, actual)
        part.unlink(missing_ok=True)
        return None
    part.replace(final)
    return final


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def check_for_updates(parent_window):
    """Fire-and-forget: kicks off a background check; if a newer, verified
    installer is found, prompts the user once it's ready. Call once at
    startup, after the main window is showing -- never blocks anything."""
    if not _is_packaged_install():
        log.info("update check: running from a dev checkout, skipping")
        return

    thread = QThread(parent_window)
    worker = _UpdateCheckWorker()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.updateAvailable.connect(lambda v, path, notes: _prompt_install(parent_window, v, path, notes))
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    # Keep references alive on the window itself -- otherwise Python (or Qt)
    # garbage-collecting the thread/worker mid-run silently kills the check.
    parent_window._update_thread = thread
    parent_window._update_worker = worker
    thread.start()


def _prompt_install(parent_window, new_version: str, installer_path: str, notes: str):
    box = QMessageBox(parent_window)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Update available")
    box.setText(f"Photo Editor {new_version} is available (you have {__version__}).")
    detail = "Update now to install it and relaunch, or keep working -- nothing changes until you choose to update."
    if notes:
        detail = f"{notes}\n\n{detail}"
    box.setInformativeText(detail)
    update_btn = box.addButton("Update Now", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is update_btn:
        _install_and_relaunch(installer_path)


def _install_and_relaunch(installer_path: str):
    # Inno Setup's own [Run] "launch now" entry is Flags: skipifsilent --
    # deliberately skipped on a silent install (see installer/PhotoEditor.iss),
    # so a silent auto-update needs to relaunch the app itself rather than
    # relying on that. Spawn a detached helper that waits for the (silent)
    # installer to finish, then launches the freshly-installed app -- this
    # process exits immediately after, rather than blocking the UI for
    # however long the install takes.
    install_dir = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "PhotoEditor"
    new_pythonw = install_dir / "runtime" / "pythonw.exe"
    new_main = install_dir / "main.py"
    ps_command = (
        f'Start-Process -FilePath "{installer_path}" '
        f'-ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait; '
        f'Start-Process -FilePath "{new_pythonw}" -ArgumentList \'"{new_main}"\' '
        f'-WorkingDirectory "{install_dir}"'
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_command],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sys.exit(0)
