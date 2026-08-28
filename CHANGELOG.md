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
