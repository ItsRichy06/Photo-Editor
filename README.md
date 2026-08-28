# Photo Editor

A local photo library + non-destructive editor for JPG/JPEG and Sony ARW (RAW) files.
Everything runs on-device; nothing is uploaded anywhere.

The app has its own explicit dark theme (`app/ui/theme.py`, applied once in
`main.py`), independent of whatever light/dark mode Windows itself is set
to -- it uses Qt's Fusion style with a custom palette, which (unlike the
native Windows style) paints purely from that palette rather than partly
following the OS theme. Without this, the app used to just inherit the
system theme by accident, which meant it looked completely different (plain
white, unstyled default controls) on a machine with Windows set to light
mode.

## Setup

Already done once in this project (kept here for reference / re-setup on another machine):

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```bash
run.bat
```
or
```bash
.venv\Scripts\python.exe main.py
```

Or install it properly: run `installer_output\PhotoEditor-Setup.exe` (built from
`installer\PhotoEditor.iss` via Inno Setup). It's a **universal installer** --
self-contained, works on any Windows 10/11 x64 PC, does not require Python (or
any particular version of it) to be installed first. It bundles the official
Python embeddable distribution with every dependency (PySide6, rawpy, OpenCV,
CUDA-enabled PyTorch, ultralytics/SAM2) installed directly into it, plus the
Visual C++ Redistributable, so nothing needs to be downloaded during install.
An NVIDIA GPU accelerates AI Select when present; without one, everything
still works, just slower (CPU fallback). Installs to
`%LOCALAPPDATA%\Programs\PhotoEditor` (no admin rights needed), with Start Menu
and optional desktop shortcuts using the app icon, and a normal uninstaller.
It's ~2.7GB; that's expected, the bulk is PyTorch + CUDA.

An installed copy checks once per launch for a newer version on the home
NAS and, if one's there and hash-verified, asks before installing it --
never without confirming, and it's silently a no-op if the NAS isn't
reachable. See `HANDOFF.md`'s "Releasing a new version" section for how a
new release gets published.

To rebuild the runtime + installer after changing code:
```bash
# one-time: download the embeddable Python and set it up (see runtime_build/runtime)
runtime_build\runtime\python.exe -m pip install -r requirements.txt --no-warn-script-location

# requirements.txt installs PySide6-Essentials (not the full PySide6 meta-package,
# which pulls in ~430MB of Addons -- WebEngine/Qt3D/QtQuick/Charts/etc -- this app
# never uses) and ultralytics, which always drags in `polars` (~185MB) as a hard
# dependency even though it's never touched by our inference-only SAM2 usage.
# After the install above, strip it back out:
runtime_build\runtime\python.exe -m pip uninstall -y polars polars-runtime-32
# That uninstall can also delete PySide6/__init__.py if you ever uninstall the full
# PySide6 meta-package afterwards (it's a file physically shared between the
# meta-package and PySide6-Essentials -- pip doesn't reference-count shared files
# across packages). If `import PySide6` ever breaks after an uninstall, restore it:
runtime_build\runtime\python.exe -m pip install --force-reinstall --no-deps PySide6-Essentials shiboken6

# then, any time:
"C:\Users\Richard\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\PhotoEditor.iss
```
(`runtime_build/` holds the self-contained Python the installer bundles --
separate from `.venv`, which is only for local development.)

## Using it

**Library**: click "Open Folder…", pick a folder of photos (subfolders are scanned
too, and it remembers this folder next launch). Thumbnails load in parallel across
several CPU threads, so even a folder with several thousand photos comes in quickly.
Use the "Show" dropdown to filter to JPEG-only, RAW-only, or all; "Min rating" and
"Sort" filter/order the grid. Click a photo to select it, then:
- `1`-`5` sets its star rating (also clickable directly on the stars in each thumbnail), `0` clears it
- `P` flags it as a pick, `X` flags it as a reject, `U` clears the flag
- Double-click opens it in the editor

Ratings, flags, and edits are stored in a local catalog database at
`%LOCALAPPDATA%\PhotoEditor\catalog.db` — your original photo files are never
modified.

**Editor**: the "Photo Info" panel on the left shows the shooting data read
straight from the file's EXIF -- camera, lens, aperture, shutter speed, ISO,
focal length, date taken, and dimensions (works for both JPEG and RAW; some
cameras write RAW files Pillow can't even identify as an image, so RAW EXIF
is read by walking the TIFF tag structure directly rather than going through
Pillow's image loader).

RAW (.ARW) decoding uses LibRaw's own auto-exposure so the editor's default
rendering matches what the camera's own JPEG/preview looks like, rather than
the flat, notably-darker linear rendering a RAW decoder gives you with no
tone-mapping at all. The interactive preview (what you see while editing) is
decoded at up to 2400px on the long edge -- plenty for judging an edit, but
zoom past 100% and it'll look softer than the original; export always
re-decodes and renders at full resolution regardless of preview zoom.

Scroll the mouse wheel over the canvas to zoom in/out (centered on the
cursor), or use the −/+/Fit/100% controls in the toolbar. Middle-click-drag
pans around when zoomed in. Every tool (crop, AI Select, brush, gradient)
keeps working correctly at any zoom level. Opening a new photo resets the
view back to Fit.

Adjust Light/Color/Channels/Detail sliders (exposure, contrast,
highlights, shadows, whites, blacks, white balance, saturation, vibrance,
per-channel red/green/blue, sharpen, denoise) while watching the live RGB
histogram update above them. Each slider's track is colored to match what it
does instead of plain gray -- Temp fades blue-to-yellow, Tint green-to-
magenta, Exposure/tonal sliders black-to-white, Saturation/Vibrance gray-to-
vivid, and the Red/Green/Blue channel sliders their own color -- so the
midpoint of a -N..N slider's colored track always lines up with "no change."
Rendering happens on a background thread (only
the latest change is ever computed, so dragging a slider stays smooth even
though the underlying adjustments -- especially denoise -- aren't cheap).
Denoise uses Non-Local Means rather than a bilateral filter, since it
actually removes sensor grain instead of just softening detail around it.
Straighten auto-crops away the rotated edges instead of showing fabricated
border pixels. The Crop tool shows a persistent, draggable rectangle
(corner/edge handles to resize, drag the middle to move, rule-of-thirds
guide) with a live width×height + ratio readout and quick aspect-ratio
presets, both landscape and portrait (Free/Original/1:1/4:5/4:3/3:2/16:9 and
their inverses). Edits are non-destructive and auto-saved to the catalog as
you go. "Show Original" toggles a before/after view. "Export…" renders your
edits at full resolution to a new JPEG/PNG/TIFF file, remembering the last
folder you exported to.

**Selection & Masking**: three tools for applying adjustments to just part
of a photo, all feeding the same Add Layer / layer list below.

- **AI Select**: click any object in the photo -- a person, animal, car,
  building, anything -- and it's selected with a mask that hugs its real
  edges (powered by Meta's SAM2, running locally on your GPU). Shift-click
  adds another point to grow the selection; Ctrl-click removes an area from
  it. The first use downloads a ~150MB model file (one-time, cached
  locally) and may take a few seconds; after that, selection is
  near-instant on a CUDA GPU (a couple of seconds on CPU if no compatible
  GPU is found -- the status line under the buttons says which one is
  active).
- **Brush**: drag to paint a mask by hand, for anything AI Select can't
  pick out on its own. Adjust brush size with the slider (a live circle
  follows your cursor showing the current size, with a dashed inner ring
  marking where the soft edge begins); "Feather" controls how hard or
  gradual that edge is; check "Erase" to subtract from the mask instead of
  adding to it.
- **Gradient**: for smooth, gradual transitions rather than a hard-edged
  selection, in three shapes (pick one from the "Gradient Shape" dropdown):
  - *Linear*: drag across the photo -- full effect where you start, fading
    to nothing where you release (drag the other direction to flip it).
    Good for darkening a sky toward the horizon.
  - *Radial*: drag out an oval -- full effect at the center, fading out
    toward the edge. Good for a spotlight/vignette look on a subject.
  - *Polygon*: click to place corners, then close the shape by double-clicking
    or clicking the first corner again. Good for an arbitrary hand-drawn
    area (Escape cancels a shape you're still placing).

  Radial and Polygon share the "Feather" slider with Brush (0% is a hard
  edge, 100% fades deep into the shape) and an "Invert" button to apply the
  effect outside the shape instead of inside it.

  Once a shape is placed, it stays editable rather than locking in place:
  drag inside it to move it, or drag its rotate handle (the small circle on
  a stalk above the shape) to turn it. Linear's two end dots can also be
  dragged individually to reposition either end (which naturally re-angles
  and resizes the fade too). A click that misses the shape/handles does
  nothing to it (rather than silently discarding it) -- hit Clear Selection
  to deliberately start a new one.

Either way, the selected area is shown as a thin outline rather than a
filled tint, so the actual pixels stay visible while you adjust exposure,
contrast, highlights/shadows, white balance, hue, saturation, or sharpen for
just that area, live, before hitting "Add Layer" to commit it. "Hue" rotates
the selection's existing colors around the color wheel (e.g. turn a red
shirt orange, or a blue sky more purple) without touching anything outside
the mask -- there's no global Hue slider, only this per-selection one.
Layers show up in the list below and can be re-selected to tweak their
adjustments, or deleted; add as many as you like.

## Status

All three planned phases are built: library + ratings, the full editing
toolkit, and AI-powered object selection plus manual brush masking. See
`CHANGELOG.md` for the version history.
