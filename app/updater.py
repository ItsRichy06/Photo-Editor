"""Best-effort auto-update check against a version manifest on the NAS.

Modeled on the update mechanism in Richard's other desktop app (Stock
Signals, Electron + electron-updater against a "latest.yml"-style feed) --
same shape, adapted for this app's Python/Inno Setup stack instead of
Electron/NSIS:
  - check once per launch, never on a timer
  - never fatal: any failure (share unreachable, malformed manifest,
    missing/mismatched file, hash mismatch) is logged and silently
    swallowed -- the app must work exactly as if this file didn't exist
  - the whole new installer is copied to local disk and hash-verified
    BEFORE the user is ever told about it, so there's nothing to wait for
    once they say yes
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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox

from .version import __version__

log = logging.getLogger(__name__)

# UNC path, not a drive letter -- Z: is how this particular machine happens
# to have \\NAS\FamilyStorage mapped right now, but a drive mapping is a
# per-machine/per-session thing, not guaranteed to exist (or use the same
# letter) on every machine this app runs on.
UPDATE_FEED_DIR = Path(r"\\NAS\FamilyStorage\shares\Personal\fotoeditor")
# Namespaced, not the generic "latest.json" it started as: this folder
# turned out to NOT be exclusively this app's -- confirmed live, a
# completely unrelated app (Richard's separate Video Editor project) is
# also publishing into it, and its own manifest was sitting right here
# under that exact generic name. A hash check alone doesn't save you from
# this: it only proves "the downloaded file matches what the manifest
# said", not "this manifest is actually for this app" -- if this app ever
# read the other one's feed, a version number that happened to compare as
# "newer" would make it hash-verify and offer to install a stranger's
# installer as if it were its own update. Namespacing the filename is what
# actually prevents that, not coordinating with whatever else happens to
# be writing here.
MANIFEST_NAME = "photoeditor-latest.json"


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
        manifest_path = UPDATE_FEED_DIR / MANIFEST_NAME
        if not manifest_path.exists():
            log.info("update check: no manifest at %s, skipping", manifest_path)
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        remote_version = manifest["version"]
        installer_name = manifest["installer"]
        expected_sha256 = manifest["sha256"].lower()
        notes = manifest.get("notes", "")

        # Belt and suspenders on top of the namespaced manifest filename
        # above: confirmed live that this feed directory isn't exclusively
        # this app's, so if a manifest ever somehow ends up here under this
        # name that isn't actually this app's (a future third app reusing
        # the name, a copy-paste mistake, anything), this is what stops it
        # from being trusted rather than a hash check that would happily
        # "verify" a stranger's installer against its own (correct, but
        # irrelevant) hash.
        if "photoeditor" not in installer_name.lower():
            log.error(
                "update check: manifest %s names installer %r, which doesn't look like this "
                "app's -- refusing to trust it (this feed directory is shared with other apps)",
                manifest_path, installer_name,
            )
            return

        if _parse_version(remote_version) <= _parse_version(__version__):
            log.info("update check: already on %s (feed has %s)", __version__, remote_version)
            return

        installer_path = UPDATE_FEED_DIR / installer_name
        if not installer_path.exists():
            # Exactly the failure mode that bit the trading app once already:
            # the manifest advertises a version whose installer isn't
            # actually there (name drift between build and publish). There's
            # no dialog for a background check, so a loud log line is the
            # only thing standing between this and silently never updating
            # again for good.
            log.error(
                "update check: manifest %s advertises %s but %s does not exist -- "
                "publish.ps1's copy and this manifest have drifted out of sync",
                manifest_path, remote_version, installer_path,
            )
            return

        # Copy to local disk and verify BEFORE telling the user anything --
        # a share hiccup mid-copy must never surface as "an update is ready"
        # when it isn't.
        tmp_dir = Path(tempfile.gettempdir()) / "PhotoEditor-update"
        tmp_dir.mkdir(exist_ok=True)
        local_path = tmp_dir / installer_name
        shutil.copyfile(installer_path, local_path)

        actual_sha256 = hashlib.sha256(local_path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            log.error(
                "update check: downloaded installer hash mismatch (expected %s, got %s) -- "
                "discarding, treating as no update available",
                expected_sha256, actual_sha256,
            )
            local_path.unlink(missing_ok=True)
            return

        log.info("update check: %s -> %s downloaded and verified", __version__, remote_version)
        self.updateAvailable.emit(remote_version, str(local_path), notes)


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
