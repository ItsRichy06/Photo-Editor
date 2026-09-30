# Changelog

All notable changes to Photo Editor, in reverse chronological order.

Versioning: `0.x` covers development before the app was a functioning
product (the original three-phase plan wasn't complete yet); `1.0.0` is the
first version with all three phases done -- library, full editing, and
AI-powered selection. From there, a new tool or capability bumps the minor
version (`1.x.0`); a fix, tweak, or polish pass bumps the patch version
(`1.x.y`).

This project had no version control until 2026-08-09 (see `1.7.3` below),
despite being built up over many sessions before that. Entries before that
date are reconstructed from session history rather than commit history --
the ordering and grouping are accurate, but exact dates for the earliest
entries are approximate. From `1.7.3` onward, entries line up with actual
git commits.

## [Unreleased]

## [1.16.2] - 2026-09-30
### Fixed
- "Scan Failed -- database disk image is malformed" could come back even
  with 1.13.2's automatic recovery, leaving newly added photos out of the
  library. The catalog now uses SQLite's classic rollback journal instead
  of WAL mode, which removes the shared-memory index file this error
  comes from. The catalog file itself was never damaged.
- The old automatic recovery deleted SQLite's -wal file, which could
  silently discard recent ratings or edits that hadn't been written into
  the main file yet. It now just reconnects and retries, deleting nothing.
### Added
- A daily safety backup of the catalog (ratings, flags, edits) in
  `%LOCALAPPDATA%\PhotoEditor\catalog_backups`, keeping the last 7 days.

## [1.16.1] - 2026-09-29
### Fixed
- The photo viewer could crash after a few photos and stuttered while
  browsing a large folder: it restarted a fresh image decode every time
  the library's photo list signaled a change (including every thumbnail
  that finished loading), so hundreds of decodes piled up at once. It now
  decodes one photo at a time and only ever the latest one asked for.
- Switching the viewer (or the library) to RAW only / JPEG only could
  crash the app on a large RAW+JPEG folder. Switching is now instant and
  stays on the same shot -- e.g. viewing DSC0001.ARW and choosing JPEG only
  lands on DSC0001.JPG.
### Changed
- The viewer shows RAW photos using the camera's own embedded full-size
  preview, so stepping through RAWs is several times faster (about 0.1s
  per photo instead of 0.7s). The editor still does its full RAW decode.

## [1.16.0] - 2026-09-29
### Added
- The library now remembers whether a rating was set by hand or by
  Auto-Rate Unrated, and a "Rated by: All / My ratings / Auto-rated"
  filter to tell them apart -- useful for pulling up everything Auto-Rate
  touched for a second look without mixing in ratings you set yourself.
  Existing ratings from before this predate the distinction and show under
  neither specific filter until re-rated.

## [1.15.0] - 2026-09-28
### Added
- A full-size photo viewer ("loupe" view): double-click a photo to open it
  near-fullscreen with prev/next arrow buttons, a draggable scrub bar, and
  a filmstrip along the bottom for fast browsing before deciding whether to
  edit. Arrow keys navigate, Escape returns to the library, and the
  RAW/JPEG format filter is shared live with the library grid.
- The viewer's side panel: an Edit button that routes straight into the
  existing editor, a five-star rating control, and a Delete button that
  sends the file to the Recycle Bin (not a permanent delete) and removes it
  from the catalog and library grid in place.
- Shooting info in the viewer, each on its own row with a small icon:
  camera, lens (including brand -- Sigma, Tamron, Viltrox, Sony, etc. --
  read from the file's LensMake tag when the lens/camera writes one),
  focal length, aperture, shutter speed, and ISO.
### Changed
- Viewer navigation shows the library's already-loaded thumbnail instantly
  while the full-resolution preview decodes in the background, and
  prefetches the next/previous photo so stepping through a set with arrow
  keys is close to instant after the first photo.
### Fixed
- `get_exif_summary()` for RAW files was decoding via rawpy's `raw.other`
  accessor, which triggers a broad metadata parse (~1.1s/file) -- switched
  to the existing fast hand-rolled TIFF/IFD tag reader (~10ms/file), so
  opening the viewer on a RAW photo doesn't stall.

## [1.14.1] - 2026-09-22
### Fixed
- Opening a folder could get silently reverted back to whatever folder was
  open last session: startup schedules a deferred reload of the last-used
  folder so the window can appear before a big scan starts, and if Open
  Folder was used to pick a different folder quickly enough, that
  still-pending reload fired moments later and overwrote the choice with
  no indication anything had changed.

## [1.14.0] - 2026-09-21
### Added
- "Delete Selected..." in the library: select 1+ photos, confirm once, and
  the files go to the Recycle Bin (not a permanent delete -- recoverable
  there, same as deleting in Explorer), with their catalog rating/flag/edit
  history cleaned up alongside.
### Changed
- The library's rating filter now shows an exact star count instead of "N
  or more" -- selecting "2" isolates just the photos still sitting at 2
  stars, instead of also showing every 3/4/5-star photo mixed in.
### Fixed
- Auto-Rate Unrated could completely miss real overexposure (a flash photo
  with blown-out faces/clothing scored 4-5 stars) because the clipping
  check required near-literal pure white, which a downsampled thumbnail
  almost never actually contains even when the full-size photo clearly is
  blown out. Recalibrated the clipping detection (and, since the fix
  uncovered it, gave shadows and highlights separate thresholds -- they
  have very different natural rates, and sharing one threshold was making
  67% of ordinary photos look falsely bad on a test sample before this was
  caught and fixed).

## [1.13.2] - 2026-09-21
### Fixed
- The catalog could fail with "database disk image is malformed" on an
  ordinary write, even though the underlying database file was genuinely
  fine (confirmed with PRAGMA integrity_check both before and after) --
  this was WAL-mode shared-memory-index corruption, not actual data
  corruption, and only ever showed up from a real interactive launch (most
  likely antivirus real-time scanning interfering with SQLite's
  memory-mapped index file at the wrong moment). The catalog now detects
  this specific error and automatically recovers -- clearing and
  regenerating the affected sidecar files and retrying once -- instead of
  leaving the library stuck.

## [1.13.1] - 2026-09-21
### Fixed
- The library could get stuck showing "Scanning..." forever, with no
  thumbnails and no error, if registering the scanned photos in the
  catalog hit any failure (confirmed cause: a transient "database is
  locked" when a second short-lived process briefly wrote to the same
  catalog.db). Raised the database's lock-wait timeout from 5s to 30s, and
  any failure that still occurs now shows "Scan failed -- click Open
  Folder to retry" plus a dialog instead of hanging silently.

## [1.13.0] - 2026-09-21
### Added
- "Auto-Rate Unrated..." in the library: gives every photo with no star
  rating yet a rough 1-5 rating from automatic sharpness and exposure
  detection, so a large unrated backlog can get a fast first pass instead
  of rating thousands of photos by hand. Sharpness is computed per-tile
  and takes the sharpest region of the frame rather than a whole-frame
  average, so a shallow-depth-of-field shot (a sharp subject against a
  deliberately blurred background) isn't misjudged as blurry; long
  exposures (1s or slower) are exempted from the sharpness check entirely,
  since their motion blur is usually intentional. Exposure problems alone
  never rate below 2 ("possibly recoverable") -- clipping is usually
  fixable with this app's own Shadows/Blacks/Highlights sliders and isn't
  necessarily a mistake (a deliberate low-key or night shot can
  legitimately be mostly near-black); only a genuine focus miss, which
  editing can't fix, can earn the bottom rating. When both a RAW and a
  JPEG exist for the same shot, only one gets analyzed and the result
  applies to both. Never touches a photo already rated by hand.

## [1.12.0] - 2026-09-20
### Added
- Search box in the library: filters the grid to filenames containing the
  typed text as you type.
- A "Show:" filter for Focus stacks / HDR merges / Originals only, so a
  merged output from Stack Photos can be found on its own or hidden from
  the regular browsing view instead of mixing in with its source frames.

## [1.11.5] - 2026-09-17
### Added
- The library now recognizes .tif/.tiff files, so a photo exported from
  elsewhere (e.g. another tool's stacking output) can be reopened here for
  further editing, not just this app's own JPEG/RAW originals.

## [1.11.4] - 2026-09-17
### Changed
- Focus-stack alignment now chains each frame onto its already-aligned
  neighbor instead of every frame jumping straight to one shared reference
  frame, so no single alignment step has to bridge more than one frame's
  worth of focus change however far a frame sits from the reference in the
  stack. Confirmed no regression on regions that were already sharp. This
  narrows, but doesn't fully close, one remaining edge case: extreme
  close-up regions with strong reflected highlights near bright macro
  lighting can still show some softness, most likely because the
  highlights themselves shift position between shots (an illumination
  effect that realignment can't correct) rather than a pure geometry
  problem -- tried and ruled out two further approaches (per-region local
  alignment, hard per-pixel frame selection) that didn't hold up under
  real-world testing.

## [1.11.3] - 2026-09-17
### Fixed
- Focus-stacked photos could still show real ghosting (not just normal
  background blur) in parts of the subject at a different depth than
  wherever the alignment reference frame happened to be focused -- e.g. a
  car's rear wing was still visibly doubled after 1.11.2, even though the
  front of the car was clean. Two causes: the subject-detection mask was
  silently failing at the resolution actually used and falling back to
  the old whole-frame alignment with no visible sign anything was wrong,
  and even once fixed, a mask built from only one frame can't see parts of
  the subject that frame doesn't have in focus. Now detects the subject
  from several frames spread across the stack instead of one.

## [1.11.2] - 2026-09-17
### Fixed
- Focus-stacked photos could still come out visibly soft/hazy at their
  true full resolution even after 1.11.1's subject-masked alignment fix --
  that fix was validated against a downsampled preview render, which hides
  the actual problem: alignment searches for the best fit on a small
  downsampled proxy image, then scales the result up to the real photo,
  which amplifies any small error on the proxy into several pixels of real
  misalignment once applied at full resolution. Fixed by using a much
  higher-resolution proxy specifically for focus stacks; confirmed against
  a real 35-frame macro stack at its true full resolution.

## [1.11.1] - 2026-09-17
### Fixed
- Focus stacking: a real 35-frame macro stack was getting silently chopped
  into a 30-frame group plus an orphaned "second stack" of whatever the
  tail of the sequence happened to be -- a hardcoded 30-frame cap on
  focus-stack group size, now raised to 100.
- Focus-stacked photos with a subject that has real depth (not a flat
  scene) could come out visibly soft/hazy even with well-aligned frames
  (0.99+ correlation) -- alignment was fitting the whole frame, which let
  a large plain background dominate the fit at the actual subject's
  expense. Now restricts what focus-stack alignment optimizes against to
  an auto-detected subject region, confirmed to fix real-world haze caused
  by focus breathing (the lens's effective focal length shifting across
  the focus range) on a subject with depth.

## [1.11.0] - 2026-09-16
### Added
- Auto Enhance: a one-click button in the editor that has a local
  multimodal AI model (Qwen2.5-VL-7B-Instruct) look at the open photo and
  suggest exposure, contrast, highlights/shadows/whites/blacks, white
  balance, and saturation/vibrance -- runs entirely on-device, no photo
  data leaves the machine. The one-time ~16GB model download only starts
  after an explicit in-app Yes/No confirmation naming the model, size, and
  source; declining leaves the button a no-op, and nothing about it is
  tied to the auto-updater.
### Fixed
- AI Select's mask now respects the Feather slider. SAM2 only ever returns
  a hard-edged mask, and the Feather slider -- visibly sitting right there
  in the panel while AI Select was active -- silently did nothing to it,
  so a local adjustment (e.g. brightening a selected subject) cut off
  sharply at the selection boundary instead of blending in. The mask is
  now Gaussian-blurred by the Feather value, re-softening live as the
  slider moves.
- Reopening an existing mask layer to tweak its sliders was silently
  hardening its edge back to a binary cutout, discarding any feather
  (Brush/Gradient included) the moment it was re-edited.
- The library grid now loads whatever's actually on screen first instead
  of plain folder-scan order -- with a large library (order of 10,000+
  photos), the thumbnails you're actually looking at used to sit behind
  everything alphabetically/chronologically earlier in the queue. Now
  prioritized on initial open and as you scroll.

## [1.10.0] - 2026-08-29
### Added
- Tone/color curves editor alongside the existing sliders: an interactive
  RGB (plus per-channel red/green/blue) curve, each drawn over its own
  live histogram backdrop so you can see exactly where in the tonal range
  a point lands before dragging it.
- HDR and focus stacking: select a flat batch of photos in the library and
  it auto-groups them into bursts by EXIF (capture time + exposure
  settings), dedupes RAW+JPEG pairs, aligns each group (a downsampled
  proxy for speed), then fuses exposure brackets with Mertens fusion or
  blends focus brackets by Laplacian sharpness. A review dialog lets you
  check and uncheck groups before committing.
- The RGB histogram now explains itself with a caption instead of leaving
  blank space underneath.
- The app now launches maximized instead of at a fixed window size.
### Fixed
- A GUI freeze when reopening a folder mid-scan: `load_folder()` was
  calling `QThread.wait()` on the GUI thread, which could deadlock against
  the scan thread it was waiting on. Rewritten to never block the GUI
  thread.
### Notes
This release reconciles two weeks of independent work: this laptop copy
(curves, stacking, histogram polish, the freeze fix, maximized launch --
commits `22a7819` through `0913b43`, 2026-08-09 through 2026-08-12) had
diverged from the desktop copy (gradient shapes, hue shift, slider colors,
the spinbox-arrow fix, the rebrand, and auto-update -- through `1.9.0`
above) while the desktop copy was in active use during that window.
Reconciled via `git merge`, not an overwrite in either direction; both
sides' commit history is preserved. Verified post-merge: all modules
import cleanly, the full updater test suite passes, and a real
launch-to-graceful-close cycle works correctly.

## [1.9.0] - 2026-08-28
### Added
- Auto-update: on launch, the installed app checks the NAS distribution
  share for a newer version and, if one's there, downloads and hash-
  verifies it in the background before ever mentioning it, then asks
  ("Update Now" / "Later") -- never installs without a click. Modeled on
  the update mechanism in Richard's other desktop app (Electron +
  electron-updater against a version-manifest feed); same shape, hand-
  rolled for this app's Python/Inno Setup stack. Carries over that app's
  central lesson the hard way already learned once there rather than
  relearning it here: a manifest that names a file which doesn't actually
  match what's published fails completely silently (no dialog, no visible
  error, just a swallowed exception no one happens to be watching for) --
  so `installer/publish.ps1` re-hashes the file it just copied and
  refuses to publish the manifest unless it actually matches, and the app
  itself only ever installs a download whose hash matches what the
  manifest promised.
- `app/version.py` as the single source of truth for the app's version
  (this is the first version tracked this way), and `installer/publish.ps1`
  to publish a build to the distribution share -- see its header for the
  release steps. Every release from here needs a `CHANGELOG.md` entry and
  a version bump in that file; `publish.ps1` checks both before it'll
  publish anything.
- `tests/test_updater.py`: this project's first actual test file, covering
  the update-check module's happy path and every failure mode it's
  designed to survive (see below for where that list came from).
### Changed
- The update check is a no-op unless it detects it's running from the
  actual installed copy, so it never fires while developing from `.venv`.
### Fixed (found while first setting this up, not after shipping)
- The NAS distribution share turned out to not be exclusively this app's --
  confirmed live, an unrelated app (Video Editor) is also publishing into
  the same directory, and its own update-feed manifest was already sitting
  there under the same generic filename ("latest.json") this app's first
  draft also used. A hash check alone doesn't protect against reading the
  wrong app's (individually valid, correctly-hashed) feed -- it only
  proves a download matches what its manifest says, not that the manifest
  is actually this app's. Fixed with a namespaced manifest filename
  (`photoeditor-latest.json`) plus a content sanity check that refuses to
  trust a manifest whose named installer doesn't look like this app's.
- The distribution share also turned out to sometimes refuse to overwrite
  a file it had happily created moments earlier, in the same session --
  the same ownership quirk this project has hit repeatedly on this share.
  `publish.ps1` now falls back to a timestamped filename when the plain
  one is blocked, rather than failing the whole publish.

## [1.8.0] - 2026-08-28
### Changed
- Rebranded "Foto Editor" to "Photo Editor" throughout: window title,
  installer (name, install directory, Start Menu/desktop shortcuts, output
  filename), and all documentation. Gave the installer a fresh AppId --
  it's a distinct product identity now, not an in-place upgrade of the old
  one, so the old install needs a separate uninstall rather than being
  silently replaced.
- Settings storage moved with it: the QSettings org/app name
  (`FotoEditor`/`FotoEditor` -> `PhotoEditor`/`PhotoEditor`, a different
  Windows registry key) and the appdata folder
  (`%LOCALAPPDATA%\FotoEditor` -> `...\PhotoEditor`, where the catalog
  database and thumbnail cache live). Existing data was migrated (copied,
  not moved) to the new locations; the old ones were left in place
  untouched as a backup.

## [1.7.3] - 2026-08-09
### Added
- Version control. The project had none before this -- initialized git,
  added a `.gitignore` (excluding the dev venv, the embeddable-Python
  runtime build, installer output, and the downloaded AI model weights),
  and made the first two commits.
- `HANDOFF.md`: setup gotchas, an architecture map, and a running log of
  bugs that shipped and got fixed (so they don't get relearned from
  scratch), written for continuing work from a second machine.
- A full portable copy of the project (source + a working installer,
  deliberately not the dev environment or runtime build -- see
  `HANDOFF.md` for why) for working from a laptop while traveling.

## [1.7.2] - 2026-08-05
### Fixed
- Spinbox up/down arrows across every adjustment slider had gone invisible
  (rendering as blank boxes) as a side effect of 1.7.1's theme -- Qt drops
  its own native arrow rendering the moment any stylesheet rule touches a
  spinbox, and the usual CSS border-triangle trick for redrawing them
  doesn't actually render as a triangle in Qt's stylesheet engine (renders
  as a filled rectangle instead, confirmed by pixel-sampling). Fixed with
  real tiny arrow-icon assets instead.

## [1.7.1] - 2026-08-04
### Fixed
- The app had never set its own theme, so it just inherited whatever
  light/dark mode Windows itself was in -- looked fine on a machine with
  Windows in dark mode (masking the bug for a long time), but rendered as
  plain white unstyled controls on a machine in light mode. Gave it an
  explicit dark theme (palette + stylesheet) so it looks the same
  regardless of the OS setting.

## [1.7.0] - 2026-08-04
### Added
- Every adjustment slider's track is now color-coded to what it controls
  instead of plain gray: Temp (blue<->yellow), Tint (green<->magenta), Hue
  (full color wheel), Saturation/Vibrance (gray<->vivid), Red/Green/Blue
  (their own channel color), and black<->white for the exposure/tone
  sliders. Everything else gets a neutral dark-to-accent gradient. Cleaner
  handle styling throughout too.

## [1.6.1] - 2026-08-04
### Fixed
- RAW (.ARW) photos rendered roughly 75% darker than they should --
  `no_auto_bright=True` in the RAW decode disabled LibRaw's exposure
  compensation entirely, so every RAW opened notably darker/flatter than
  the camera's own JPEG rendering. Confirmed against a real library file
  and fixed by enabling LibRaw's own auto-exposure; preview and full-
  resolution export now render within ~1% of each other, so there's no
  brightness jump on export.
- The editor's interactive preview was capped at 1400px on the long edge,
  which looked visibly soft once zoomed in to inspect detail. Bumped to
  2400px (roughly 3x the pixel count) while staying on the fast decode
  path; export was never affected, it always renders at full resolution.

## [1.6.0] - 2026-08-03
### Added
- Hue slider for local (masked) adjustments -- rotates the selected area's
  existing colors around the color wheel (e.g. a red shirt to orange, a
  blue sky to purple) without touching anything outside the mask.

## [1.5.1] - 2026-08-03
### Fixed
- A click that missed a placed Gradient shape's (too-strict) hit-test
  region silently discarded it and replaced it with an invisible,
  zero-effect point -- no feedback that anything had happened, which made
  the tool feel like it randomly did nothing about half the time. Misses
  are now a genuine no-op; only "Clear Selection" discards a placed shape.
  Also widened the hit-test tolerances (Radial now uses its whole bounding
  box rather than the exact ellipse curve; handle targets grew), and fixed
  "Clear Selection" itself not actually resetting a shape's geometry
  (it reset the mask/sliders but silently left the old shape in place).

## [1.5.0] - 2026-08-03
### Added
- Radial and Polygon shapes for the Gradient tool, alongside the existing
  Linear one -- Radial drags out an oval fading from its center; Polygon
  clicks out an arbitrary shape's corners. Both share a Feather amount
  with Brush and an Invert toggle.
- Placed Gradient shapes can be moved (drag the body) and rotated (drag a
  handle on Radial/Polygon; Linear's two end points can be dragged
  individually instead, which naturally re-angles and resizes the fade).

## [1.4.0] - 2026-08-02
### Fixed
- The Gradient tool's live preview didn't reflect pending adjustments
  until after committing the layer -- the render path that applies an
  in-progress mask's adjustments only checked for AI Select/Brush mode,
  Gradient was never added to the condition.
- The gradient's on-screen indicator was a single line drawn along the
  drag direction, which reads as a hard split down the image -- the
  actual fade runs perpendicular to that line. Redrawn the way Lightroom/
  Capture One do it: a solid guide line at each end of the fade and a
  dashed one at the midpoint.

## [1.3.0] - 2026-08-01/02
### Added
- Zoom in/out on the editor canvas (scroll wheel, centered on the cursor,
  or toolbar controls), with middle-click-drag panning once zoomed in.
### Fixed
- RAW camera/lens info showing "unknown" for cameras (confirmed: a Sony
  ZV-E10) whose files Pillow can't even identify as an image, since the
  pixel data lives in a SubIFD rather than IFD0 -- EXIF is now read by
  walking the TIFF tag structure directly instead of through Pillow.
### Changed
- Installer trimmed from ~2.8GB to ~2.6GB: switched to PySide6-Essentials
  instead of the full PySide6 meta-package (drops ~430MB of unused
  WebEngine/Qt3D/QtQuick/Charts), and removed `polars` (~185MB, pulled in
  by `ultralytics` but never actually used by this app's inference-only
  SAM2 usage).

## [1.2.0] - 2026-08-01
### Added
- Gradient tool (linear): drag across the photo for a smooth fade, for
  edits a hard-edged selection isn't suited to (e.g. darkening a sky
  toward the horizon).
### Fixed
- Adjustment slider spinboxes stepped by a full unit per arrow-key press
  regardless of decimal precision, making fine sliders like Exposure hard
  to nudge accurately.

## [1.1.0] - 2026-07-31
### Added
- "Photo Info" panel showing EXIF shooting data (camera, lens, aperture,
  shutter speed, ISO, focal length, date, dimensions) for both JPEG and
  RAW.
- App icon.
- First packaged Windows installer (Inno Setup): a self-contained
  embeddable Python runtime bundled with every dependency, so the target
  machine needs no separate Python install.
### Fixed
- A round of responsiveness, denoise quality, and mask-overlay rendering
  fixes surfaced while dogfooding the app on a real photo library.

## [1.0.0] - 2026-07-30
### Added
- AI-powered object selection: click any object -- a person, animal, car,
  building, anything -- and it's selected with a mask that hugs its real
  edges, powered by Meta's SAM2 running locally (GPU-accelerated when
  available, with an automatic CPU fallback). Shift-click adds to the
  selection, Ctrl-click removes from it.
- Manual brush masking for anything AI Select can't pick out on its own,
  with adjustable size and a soft feather edge.
- Local (masked) adjustment layers: any selection -- AI, brush, or later
  gradient -- can carry its own exposure/contrast/white-balance/
  saturation/sharpen adjustments, scoped only to that area. Layers can be
  re-selected to tweak, or deleted.

This completes the original three-phase plan -- the first version that
counts as a real, functioning product rather than a work in progress.

## [0.2.0] - 2026-07-29
### Added
- The non-destructive editor (Phase 2): Light/Color/Detail adjustment
  sliders (exposure, contrast, highlights/shadows/whites/blacks, white
  balance, saturation/vibrance, per-channel RGB gain, sharpen, denoise),
  live RGB histogram, crop tool with draggable handles and aspect-ratio
  presets, straighten, rotate/flip, before/after compare, and export to
  JPEG/PNG/TIFF. Edits are non-destructive and auto-saved as you go.
- Rendering moved to a background thread so dragging a slider stays
  smooth even though some adjustments (denoise especially) aren't cheap.

## [0.1.0] - 2026-07-28
### Added
- Initial project scaffold and the library browser (Phase 1): folder
  picker with recursive scan, parallel thumbnail loading, star ratings
  (1-5) and pick/reject flags with keyboard shortcuts, filter by rating/
  format, sort options. Unified JPEG/Sony ARW (RAW) loading. A local
  SQLite catalog for ratings/flags/edits, keyed by file path -- original
  photo files are never modified.
