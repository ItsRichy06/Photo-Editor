# Handoff notes

Written 2026-08-09, before a 2-week trip, so work on Photo Editor can continue
from a laptop. This file covers project status, environment setup, the
project's shape, and things that broke once already so they don't get
relearned the hard way on a fresh machine. **README.md** is the user-facing
doc (features, how to use the app) -- read that too, it's not duplicated here.

## Status

Feature-complete and in active polish. All three original phases are done:
library + ratings, the full non-destructive editing toolkit, and AI-powered
selection (SAM2) plus manual brush/gradient masking. Nothing is mid-flight or
half-implemented as of this commit -- the last stretch of work was bug fixes
and UI polish (see "Recent history" below), not new unstarted features.

This is the **first commit** in this repo's history -- there was no version
control before now, despite the project having gone through a long build-out
across many sessions. Keep committing as you go from here; there's no
history before this point to fall back on.

## Quick start on a new machine

Two separate things you might want to do:

**Just run the app** (no code changes): install `installer_output` isn't
included in this copy (see "What's not copied" below) -- rebuild it, or
grab the latest `PhotoEditor-Setup-*.exe` from the distribution share if
you have network access to it (`Z:\shares\Personal\fotoeditor\`, see
"Where things live"). It's a universal installer, no Python required on
the target machine.

**Make code changes**: follow README.md's "Setup" section
(`python -m venv .venv` + `pip install -r requirements.txt`), then
`run.bat` or `.venv\Scripts\python.exe main.py`. Gotchas not obvious from
that section alone:

- **`torch`/`torchvision` need the CUDA wheel index, not plain PyPI.**
  `requirements.txt` already pins `--extra-index-url .../cu128` and exact
  `+cu128` versions for this -- don't loosen those pins, a bare
  `torch>=2.11` silently resolves to a CPU-only build from PyPI even with
  the extra index present.
- **After installing requirements.txt, `polars` (~185MB) is dragged in by
  `ultralytics` but never actually used** by this app's inference-only
  SAM2 usage. `pip uninstall -y polars polars-runtime-32` after the main
  install. Verified safe via a real GPU segmentation call with polars
  absent.
- **Don't `pip uninstall PySide6` (the full meta-package) after installing
  `PySide6-Essentials`** (what requirements.txt actually specifies) --
  they share one physical file (`PySide6/__init__.py`) at the same path,
  and pip doesn't reference-count files shared across packages. Uninstalling
  the meta-package deletes that file out from under Essentials and breaks
  `import PySide6` for everything. If this happens:
  `pip install --force-reinstall --no-deps PySide6-Essentials shiboken6`.
- **No NVIDIA GPU on the laptop?** Everything still works -- AI Select
  falls back to CPU automatically (a few seconds per selection instead of
  near-instant). Nothing else in the app touches CUDA.
- **The AI Select model (`sam2_b.pt`, ~150MB) downloads on first use**,
  cached in the project root afterward. Needs internet the first time only.

## Where things live

- **This copy**: `X:\Foto Editor` (laptop-portable copy, made 2026-08-09)
- **Original**: `C:\Users\Richard\Documents\Foto Editor` (desktop)
- **Catalog** (ratings/flags/edit recipes -- NOT in the project folder):
  `%LOCALAPPDATA%\PhotoEditor\catalog.db` on whichever machine you're using
  (was `%LOCALAPPDATA%\FotoEditor\catalog.db` before the 2026-08-09 Foto->Photo
  rebrand -- the old data was migrated to the new path at that point, and the
  old folder was left in place untouched as a backup, not deleted).
  This does not travel with the project copy -- the laptop starts with an
  empty catalog unless you copy that file over too. Since it stores absolute
  photo paths, it's only useful if the same photos are reachable at the same
  path on the laptop.
- **Photo library** (on the desktop): `C:\Users\Richard\Pictures\Kamera bilder`
  -- not copied here (thousands of RAW/JPEG files); the laptop needs its own
  set of photos to actually edit, or access back to this folder.
- **Distribution share**: `Z:\shares\Personal\fotoeditor\` -- where built
  installers get dropped for other machines/people to grab. Quirk: this share
  denies overwrite/delete on files once written under certain conditions
  (permission ownership oddity, not fully understood) -- new builds go out
  under a new filename each time rather than overwriting the old one. Always
  hash-verify (`Get-FileHash`) after copying anything here; a copy has
  silently corrupted (matching size, wrong content) at least once before.

## Architecture map

```
main.py                 entry point, QApplication bootstrap, dark theme applied here
app/
  catalog.py             SQLite: photos, ratings, flags, edit recipes (WAL mode)
  image_io.py             JPEG/ARW load & save, EXIF (including hand-rolled TIFF
                           walking for RAW files Pillow can't identify), preview
                           downsampling
  processing.py            the adjustment pipeline: exposure/contrast/tone regions/
                            white balance/hue/saturation/sharpen/denoise, crop/
                            rotate, local (masked) layer compositing
  thumbnails.py             cached thumbnail generation for the library grid
  segmentation.py            SAM2 wrapper: device selection (GPU probe + CPU
                              fallback), point-prompted mask generation
  ui/
    main_window.py          Library <-> Editor mode switch
    library_view.py          folder scan, thumbnail grid, ratings/flags UI
    editor_view.py            far and away the biggest file -- canvas (zoom/pan/
                               crop/AI-select/brush/gradient), the whole adjustment
                               panel, layers, export. If you're orienting in this
                               codebase, start here.
    widgets.py                 star rating, LabeledSlider (the color-gradient
                                sliders), histogram
    theme.py                    explicit dark QPalette + QSS (see "Recent history")
assets/                app icon, spinbox arrow icons (see "Recent history")
installer/             Inno Setup script + VC++ redistributable
runtime_build/         NOT in this copy -- the embeddable Python the installer
                        bundles. See README's "rebuild the runtime + installer".
```

## Recent history (things that broke once, fixed, worth knowing)

Roughly newest-first. Each of these was a real bug that shipped and got
fixed within this same project -- the pattern/lesson is worth carrying
forward rather than rediscovering:

- **2026-08-29: reconciled two weeks of independent desktop/laptop work.**
  This laptop copy (`X:\Foto Editor`) had gone on to build a tone/color
  curves editor, HDR/focus stacking, histogram polish, a GUI-freeze fix,
  and maximized launch (commits `22a7819`..`0913b43`, 2026-08-09 through
  2026-08-12) while the desktop copy was simultaneously getting gradient
  shapes, the hue slider, colored sliders, the spinbox-arrow fix, the
  Foto->Photo rebrand, and auto-update -- two genuinely diverged histories
  neither side knew about until the desktop copy pulled from a `laptop`
  git remote pointed at this folder. Reconciled with `git merge` (not an
  overwrite either direction) on a `merge-laptop-work` branch; only
  `main.py` conflicted (`showMaximized()` vs `check_for_updates()` both
  wanted the last lines of `main()`) and was resolved by keeping both.
  Shipped as `1.10.0` -- see `CHANGELOG.md`. Two false alarms surfaced
  while verifying the merge before trusting it, worth knowing about if
  they come up again: (1) `python -c "EditorView(cat)"` (construct then
  let the bare interpreter tear it down) crashes with a genuine
  `STATUS_STACK_BUFFER_OVERRUN` on teardown -- reproduces on the laptop
  commits alone, so it predates and is unrelated to the merge, and doesn't
  happen in real usage (only when a Qt widget is torn down by interpreter
  exit without `app.exec()` ever having run); (2) testing a real launch's
  graceful-close cycle via PowerShell's `$proc.CloseMainWindow()` first
  appeared to hang the process forever -- turned out to be testing the
  wrong PID, since the venv's `pythonw.exe` **launcher stub re-execs a
  child `pythonw.exe` process** that owns the actual window; the parent
  stub PID has no window at all (`MainWindowHandle` 0). Check
  `Get-CimInstance Win32_Process -Filter "ParentProcessId=<stub pid>"` to
  find the real child before concluding a launch/close test failed.
- **2026-08-28: auto-update added** (`app/updater.py`, `installer/publish.ps1`,
  `app/version.py`, `CHANGELOG.md`) -- see "Releasing a new version" above
  for the mechanics. Worth knowing if it ever seems to have stopped working:
  it fails completely silently by design (only a log line, no dialog) on
  anything from the share being unreachable to a manifest/file mismatch --
  check the log before assuming it's broken versus just not able to reach
  `\\NAS\FamilyStorage` from wherever the app is currently running. Two
  things found live while first setting this up, both fixed before this
  ever shipped: (1) the feed directory turned out to NOT be exclusive to
  this app -- a separate app (Video Editor) publishes into the exact same
  directory, and had already claimed the generic manifest filename
  ("latest.json") this app's first draft also picked -- hence the
  `photoeditor-` prefix, plus a content check refusing to trust a manifest
  that doesn't look like this app's even if the filename ever collides
  again some other way; (2) the share can refuse to overwrite a file it
  happily created moments earlier in the same session (the same ownership
  quirk documented elsewhere in this file) -- `publish.ps1` falls back to
  a timestamped filename rather than failing the whole publish over it.
- **2026-08-28: rebranded "Foto Editor" -> "Photo Editor"** (source, window
  title, installer, docs -- everything except the actual project folder
  path, which is still `Photo Editor` on disk on both the desktop and this
  laptop copy; renaming a folder a live session is running out of is asking
  for trouble, so that was deliberately left alone). Two things about this
  that aren't obvious from a plain find-replace: the QSettings org/app name
  changed (`FotoEditor` -> `PhotoEditor`, i.e. a different registry key,
  `HKCU\Software\PhotoEditor\PhotoEditor` now), and the appdata folder name
  changed too (`%LOCALAPPDATA%\FotoEditor` -> `...\PhotoEditor`, where
  `catalog.db` and the thumbnail cache live). Both were migrated (copied,
  not moved -- the old `FotoEditor` registry key and appdata folder are
  still there as an untouched backup) as part of the rebrand; if a *third*
  machine ever needs the same migration, check `git log` around this commit
  for the exact commands used. The installer also got a new AppId GUID
  (it's a distinct product identity now, not an in-place upgrade of the old
  one) -- meaning the old "Foto Editor" install has to be uninstalled
  separately, a new install won't find/replace it automatically.
- **Qt stylesheets silently drop spinbox arrows.** The instant any QSS rule
  touches `QDoubleSpinBox`/`QSpinBox`, Qt stops drawing its own native
  up/down arrow glyphs for it -- and the classic CSS "zero-size box +
  transparent side borders + colored top/bottom border" triangle trick does
  **not** render as a triangle in Qt's QSS engine (it renders as a filled
  rectangle, confirmed by pixel-sampling). The only fix that reliably works
  is real tiny image assets referenced via `image: url(...)`
  (`assets/spin_up_arrow.png` / `spin_down_arrow.png`, generated via PIL).
  If you add more custom-styled widgets with subcontrols (QComboBox dropdown
  arrows are the next likely victim), expect the same issue.
- **A QSS rule change needs the installer's `[Files]` section to actually
  bundle new asset files.** `installer/PhotoEditor.iss` (named `FotoEditor.iss`
  at the time) used to reference
  `assets\app_icon.ico` by exact filename; adding the two arrow PNGs above
  required switching that to a wildcard (`assets\*`). If you add new asset
  types again, double check they're covered by a wildcard, not an exact
  filename, or they silently won't ship.
- **A "click missed the shape = discard it" UX trap.** When move/rotate was
  added to the Gradient tool's placed shapes, any click that missed the
  (too-strict) hit-test region silently reset the shape to an invisible,
  zero-effect point -- with no feedback that anything had happened. Felt
  like the whole tool was "randomly" doing nothing. Fixed by making misses
  a genuine no-op (only "Clear Selection" discards a placed shape) plus
  generous hit-test tolerances. The general lesson: any interaction that
  can silently discard user work on an imprecise click needs a safety net,
  not just better luck with the tolerance value.
- **RAW files rendered ~75% darker than they should.** `rawpy.postprocess()`
  had `no_auto_bright=True`, i.e. no exposure compensation at all, so every
  RAW opened notably darker/flatter than the camera's own JPEG rendering.
  Fixed by enabling LibRaw's own auto-exposure (`no_auto_bright=False`);
  confirmed consistent between the preview decode and the full-res export
  decode (~1% apart) so there's no brightness jump on export.
- **The editor's working preview is capped at 2400px on the long edge**
  (was 1400 -- bumped after zooming in looked visibly soft). This is
  deliberate for interactive performance; export always re-decodes and
  renders at full resolution regardless of what the preview showed.
- **PySide6-Essentials vs the full PySide6 meta-package**, and the
  shared-`__init__.py` uninstall trap -- covered above under Quick Start,
  repeating here because it's the single easiest thing to break by
  instinctively running `pip uninstall PySide6`.

## Rebuilding the installer

Needs `runtime_build/runtime` to exist (not copied here -- see README's
"rebuild the runtime + installer" section for the one-time setup), then:

```bash
"C:\Users\Richard\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\PhotoEditor.iss
```

Inno Setup itself also isn't copied here -- reinstall it on whichever
machine builds the installer (free, from jrsoftware.org).

After rebuilding, if updating an existing local install, uninstall-then-
reinstall rather than just running the new installer over the old one --
confirmed Inno Setup doesn't clean up files that a newer build no longer
references, so an in-place "upgrade" can leave stale/removed dependencies
sitting around at full size.

## Releasing a new version (`CHANGELOG.md` + auto-update)

Every release needs, in order:

1. Bump `__version__` in `app/version.py`.
2. Add a matching top entry to `CHANGELOG.md` (see existing entries for the
   format -- `0.x` was pre-functioning-product development, `1.0.0` is the
   first version with all three original phases done, a new tool/capability
   bumps the minor version, a fix/tweak/polish pass bumps the patch version).
3. Rebuild the installer (above).
4. `powershell -ExecutionPolicy Bypass -File installer\publish.ps1 -Version X.Y.Z`
   -- copies the installer to the distribution share and publishes the
   auto-update feed manifest (`latest.json`) alongside it. Refuses to run at
   all if step 1 or 2 was skipped (checks both files actually mention the
   version being published), and refuses to publish the manifest if the
   copied installer's hash doesn't match what it's about to advertise --
   see the script's own header for why that specific check exists.

Already-installed copies check that share once per launch (`app/updater.py`)
and, if a newer verified version is there, ask before installing it -- never
without a click, never mid-session, only once fully downloaded and hash-
checked. Only runs against an actual installed copy, never `.venv`. If the
share isn't reachable (e.g. away from home without VPN/Tailscale to the NAS),
the check just silently does nothing -- same as if this feature didn't
exist, by design, not a bug to fix.

## What's not copied here, and why

- **`.venv/`** (~4.7GB) -- the desktop's dev environment. Not copied because
  it's tied to that machine's exact setup, and a laptop with different
  hardware (especially GPU-vs-no-GPU) is better served by a fresh
  `pip install -r requirements.txt` than an inherited environment that might
  not match. Regenerate per "Quick start" above.
- **`runtime_build/`** (~4.6GB) -- the embeddable Python bundled into the
  installer. Only needed if you're rebuilding the installer itself, not for
  day-to-day code changes. See README if you need it.
- **`installer_output/`** -- previously built installer .exe, regenerate
  via "Rebuilding the installer" above, or pull the latest from the
  distribution share if reachable.
- **The photo catalog and library folder** -- see "Where things live".

Everything else -- all source code, the full edit history from this commit
forward, assets, the installer script itself -- is in this copy.
