"""One read-only series cover job at a time. Widgets and pixmaps stay on the GUI thread."""
from collections import OrderedDict
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt

from . import diagnostics as diag
from .debug_tools import dbg_exc


class _ReadCover(QThread):
    def __init__(self, parent, generation, key, read, record, artist_dirs):
        super().__init__(parent)
        self.generation, self.key = generation, key
        self.read, self.record, self.artist_dirs = read, record, artist_dirs
        self.result = None
        self.ms = 0

    def run(self):
        began = perf_counter()
        try:
            self.result = self.read(Path(self.key), self.record, self.artist_dirs)
        except Exception:
            dbg_exc("Could not prepare a series cover")
        finally:
            self.ms = diag.elapsed_ms(began)


class SeriesPreviews(QObject):
    ready = Signal(str, object)
    idle = Signal()
    CACHE_LIMIT = 32  # At most 32 decoded images, each no larger than 300 x 300.

    def __init__(self, parent, read):
        super().__init__(parent)
        self.read = read
        self.generation = 0
        self.active = False
        self.job = None
        self.pending = OrderedDict()
        self.recent = OrderedDict()
        self.visible = set()
        self.artist_dirs = ()
        self.total = self.prepared = self.failed = self.ms = 0
        self.logged_start = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._pump)

    def reset(self, entries=(), artist_dirs=()):
        # In-flight I/O can finish. Its result must not touch a new series or library.
        if self.logged_start:
            self._report('cancelled')
        self.generation += 1
        self.timer.stop()
        self.pending = OrderedDict(entries)
        self.recent.clear()
        self.visible.clear()
        self.artist_dirs = tuple(artist_dirs)
        self.total, self.prepared, self.failed, self.ms = len(self.pending), 0, 0, 0
        self._schedule()

    def set_active(self, active):
        self.active = active
        if not active:
            self.timer.stop()
        else:
            self._schedule()

    def prioritize(self, keys):
        keys = list(keys)
        self.visible = set(keys[:self.CACHE_LIMIT])
        for key in reversed(keys):
            if key in self.pending:
                self.pending.move_to_end(key, last=False)

    def preview(self, path):
        return self.recent.get(str(path))

    def _schedule(self):
        if self.active and self.pending and self.job is None and not self.timer.isActive():
            self.timer.start(0)

    def _report(self, state):
        diag.record('series.covers', state, books=self.total, prepared=self.prepared,
                    failed=self.failed, ms=self.ms)
        self.logged_start = False

    @Slot()
    def _pump(self):
        if not self.active or not self.pending or self.job is not None:
            return
        if not self.logged_start:
            diag.record('series.covers', 'start', books=self.total)
            self.logged_start = True
        key, record = self.pending.popitem(last=False)
        self.job = _ReadCover(self, self.generation, key, self.read, record, self.artist_dirs)
        self.job.finished.connect(self._finished, Qt.QueuedConnection)
        self.job.start()

    @Slot()
    def _finished(self):
        job, self.job = self.job, None
        if job is None:
            return
        if job.generation == self.generation:
            self.ms += job.ms
            if job.result is None:
                self.failed += 1
            else:
                art, card_image = job.result
                self.prepared += 1
                if art.checked and not art.image.isNull():
                    self.recent[job.key] = art
                    if len(self.recent) > self.CACHE_LIMIT:
                        oldest = next((k for k in self.recent if k not in self.visible),
                                      next(iter(self.recent)))
                        del self.recent[oldest]
                self.ready.emit(job.key, card_image)
            if not self.pending:
                self._report('warning' if self.failed else 'ok')
        job.deleteLater()
        if not self.active or not self.pending:
            self.idle.emit()
        self._schedule()
