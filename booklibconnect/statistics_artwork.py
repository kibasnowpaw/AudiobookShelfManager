"""Bounded, lazy cover loading. Media files are opened only by the worker."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt, QSize, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader, QIcon, QPixmap, QPainter, QPalette

from .covers import find_track_sidecar_cover, find_cover_in_folder, get_embedded_cover_bytes


def read_thumbnail(path):
    path = Path(path)
    if not path.is_file():
        return QImage()
    sidecar = find_track_sidecar_cover(path)
    buffer = None
    if sidecar is not None:
        reader = QImageReader(str(sidecar))
    else:
        embedded = get_embedded_cover_bytes(path)
        if embedded:
            buffer = QBuffer()
            buffer.setData(QByteArray(embedded))
            buffer.open(QIODevice.ReadOnly)
            reader = QImageReader(buffer)
        else:
            cover = find_cover_in_folder(path.parent, ['cover', 'folder', 'front', 'album'])
            if cover is None:
                return QImage()
            reader = QImageReader(str(cover))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(QSize(200, 200), Qt.KeepAspectRatio))
    image = reader.read()
    if not image.isNull():
        image = image.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


class _CoverJob(QThread):
    def __init__(self, key, parent):
        super().__init__(parent)
        self.key = key
        self.image = QImage()

    def run(self):
        try:
            self.image = read_thumbnail(self.key[0])
        except Exception:
            # Missing, unreadable or removed artwork uses the visible placeholder.
            self.image = QImage()


class StatisticsArtwork(QObject):
    changed = Signal(object)
    idle = Signal()
    CACHE_LIMIT = 192

    def __init__(self, parent):
        super().__init__(parent)
        self.cache = OrderedDict()
        self.pending = OrderedDict()
        self._job = None
        self._paused = True
        canvas = QPixmap(200, 200)
        canvas.fill(parent.palette().color(QPalette.AlternateBase))
        painter = QPainter(canvas)
        painter.setPen(parent.palette().color(QPalette.Text))
        painter.drawText(canvas.rect(), Qt.AlignCenter, 'Cover unavailable')
        painter.end()
        self.placeholder = QIcon(canvas)

    @property
    def busy(self):
        return self._job is not None

    def icon(self, path, signature=''):
        key = (str(path), str(signature))
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if path and not self._paused and (self._job is None or self._job.key != key):
            if key not in self.pending:
                self.pending[key] = None
                QTimer.singleShot(0, self._pump)
        return self.placeholder

    def pause(self):
        self._paused = True
        self.pending.clear()
        return not self.busy

    def resume(self):
        self._paused = False

    def refresh(self):
        """Discard pending work; signature-keyed cached covers remain reusable."""
        self.pending.clear()

    @Slot()
    def _pump(self):
        if self._paused or self._job is not None or not self.pending:
            return
        key, _ = self.pending.popitem(last=False)
        if key in self.cache:
            QTimer.singleShot(0, self._pump)
            return
        self._job = _CoverJob(key, self)
        self._job.finished.connect(self._finished, Qt.QueuedConnection)
        self._job.start()

    @Slot()
    def _finished(self):
        job = self._job
        if job is None:
            return
        self._job = None
        key = job.key
        icon = self.placeholder if job.image.isNull() else QIcon(QPixmap.fromImage(job.image))
        self.cache[key] = icon
        self.cache.move_to_end(key)
        while len(self.cache) > self.CACHE_LIMIT:
            self.cache.popitem(last=False)
        job.deleteLater()
        self.changed.emit(key)
        if self._paused or not self.pending:
            self.idle.emit()
        else:
            QTimer.singleShot(0, self._pump)
