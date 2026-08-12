"""UI plumbing for the Library's "Stack Photos" feature: a background
thread for the (fast) EXIF-analysis pass, a confirmation dialog shown
between analysis and processing (detection is a best-effort heuristic --
see stacking.py's module docstring -- so this review step is the real
safety net, not optional polish), and a background thread for the (slow,
per-group several-second-to-tens-of-seconds) align+merge batch itself.

Kept out of library_view.py to keep that file focused on the folder/grid
UI -- this is wired in from one button and a handful of signal handlers
there.
"""

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .. import image_io, stacking


class StackAnalysisThread(QThread):
    """Reads EXIF (no full image decode) for every candidate path and runs
    the grouping algorithm. Fast in practice (well under a second per
    photo), but still real file I/O across potentially hundreds of files,
    so it stays off the GUI thread rather than risk a visible stutter."""

    progress = Signal(int, int)  # done, total
    finished_analysis = Signal(list, list)  # candidates, leftover_paths

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        def on_progress(done, total):
            if not self._stop:
                self.progress.emit(done, total)

        infos = stacking.gather_info(self.paths, progress_cb=on_progress)
        if self._stop:
            self.finished_analysis.emit([], [])
            return
        candidates, leftover = stacking.detect_stacks(infos)
        self.finished_analysis.emit(candidates, leftover)


class StackProcessingThread(QThread):
    """Runs align+merge for each confirmed group, one at a time (not all
    loaded simultaneously -- each full-resolution group can be a fair
    amount of memory on its own), writes the result next to its source
    photos, and registers it with the catalog so it shows up in the
    library without needing a manual re-scan."""

    progress = Signal(int, int, str)  # done, total, current group's label
    group_done = Signal(object, object, object)  # candidate, output_path|None, error|None
    finished_processing = Signal()

    def __init__(self, candidates, catalog, parent=None):
        super().__init__(parent)
        self.candidates = candidates
        self.catalog = catalog
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.candidates)
        for i, candidate in enumerate(self.candidates):
            if self._stop:
                break
            self.progress.emit(i, total, candidate.label)
            try:
                merged = stacking.merge_stack(candidate)
                out_path = stacking.output_path_for(candidate)
                image_io.save_image(merged, out_path, quality=92)
                self.catalog.get_or_create_photo(str(out_path))
                self.group_done.emit(candidate, out_path, None)
            except Exception as exc:  # noqa: BLE001 -- keep going past one bad group
                self.group_done.emit(candidate, None, str(exc))
        self.progress.emit(total, total, "")
        self.finished_processing.emit()


class StackPreviewDialog(QDialog):
    """Shows the detected HDR/focus groups -- and how many selected photos
    won't be touched -- for the user to review, and uncheck anything that
    looks wrong, before anything is written to disk."""

    def __init__(self, candidates, leftover, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stack Photos")
        self.setMinimumSize(500, 380)
        self.candidates = candidates

        layout = QVBoxLayout(self)
        hdr_count = sum(1 for c in candidates if c.kind == "hdr")
        focus_count = sum(1 for c in candidates if c.kind == "focus")
        summary = QLabel(
            f"Detected {hdr_count} HDR stack{'s' if hdr_count != 1 else ''} and "
            f"{focus_count} focus stack{'s' if focus_count != 1 else ''} "
            f"({len(candidates)} new photo{'s' if len(candidates) != 1 else ''} total). "
            f"{len(leftover)} selected photo{'s' if len(leftover) != 1 else ''} will be left untouched."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for c in candidates:
            item = QListWidgetItem(f"[{'HDR' if c.kind == 'hdr' else 'Focus'}]  {c.label}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, stretch=1)

        hint = QLabel(
            "Detection is a best guess from timing and exposure alone -- uncheck any group that "
            "shouldn't be merged (e.g. a fast action sequence mistaken for a same-exposure focus "
            "stack). Each result is saved as a new photo next to its sources; nothing selected is "
            "modified or deleted."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999999; font-size: 11px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.process_btn = buttons.addButton("Process", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self.process_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        if not candidates:
            self.process_btn.setEnabled(False)

    def selected_candidates(self) -> list:
        return [
            c for i, c in enumerate(self.candidates)
            if self.list_widget.item(i).checkState() == Qt.CheckState.Checked
        ]
