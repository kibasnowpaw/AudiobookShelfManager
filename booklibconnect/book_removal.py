"""Explicit book removal with optional, independently opted-in audiobook file trashing."""
from dataclasses import dataclass
from pathlib import Path
import os
import stat

from PySide6.QtCore import QFile, Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QCheckBox, QDialogButtonBox

from .portable_track_state import APP_TRACK_ID, _read_txxx_text
from .audio_formats import supported_audio


def _file_signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not supported_audio(path):
        raise ValueError('Only a regular MP3 or M4B file can be moved to Trash.')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _portal_trash(path, signature):
    """Trash document-portal files through the desktop, using the verified FD."""
    try:
        from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage, QDBusUnixFileDescriptor
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False, ''
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if current != signature:
                raise RuntimeError('The audiobook file changed after confirmation. Nothing was removed.')
            message = QDBusMessage.createMethodCall(
                'org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
                'org.freedesktop.portal.Trash', 'TrashFile')
            message.setArguments([QDBusUnixFileDescriptor(fd)])
            reply = bus.call(message, QDBus.Block, 25000)
            # TrashFile has one FD argument and returns 1 on success (not 0).
            ok = reply.type() == QDBusMessage.ReplyMessage and reply.arguments() == [1]
            return ok, ''  # The portal does not return a recoverable path.
        finally:
            os.close(fd)
    except (ImportError, OSError):
        return False, ''


def _trash_file(path, signature):
    # Flatpak document mounts need the host's Trash portal. On a normal local
    # filesystem, QFile also supplies a path that lets us undo a failed commit.
    flatpak = bool(os.environ.get('FLATPAK_ID')) or Path('/.flatpak-info').exists()
    if flatpak:
        ok, destination = _portal_trash(path, signature)
        if ok:
            return ok, destination
    if _file_signature(Path(path)) != signature:
        raise RuntimeError('The audiobook file changed after confirmation. Nothing was removed.')
    file = QFile(path)
    if file.moveToTrash():
        return True, file.fileName()
    if not flatpak:
        return _portal_trash(path, signature)
    return False, ''


@dataclass(frozen=True)
class RemovalTarget:
    track_id: str
    path: str
    title: str
    state: dict
    signature: tuple | None
    file_note: str

    @property
    def can_delete_file(self):
        return self.signature is not None


def prepare_removal(store, row):
    state = store.removal_snapshot(row.track_id)
    saved = state['book']
    if saved is not None and saved['last_path'] != row.path:
        raise RuntimeError('The saved file path changed. Refresh and try again.')
    signature = None
    note = 'Only this audio file; folder artwork and other files are kept.'
    try:
        path = Path(row.path)
        before = _file_signature(path)
        if _read_txxx_text(path, APP_TRACK_ID) != row.track_id:
            note = 'File deletion is unavailable: the audiobook file identity does not match this saved book.'
        elif before != _file_signature(path):
            note = 'The file changed during checking. Refresh and try again.'
        else:
            signature = before
    except FileNotFoundError:
        note = 'The file is already missing. You can remove its saved data.'
    except (OSError, ValueError):
        note = 'The file cannot be verified for deletion. You can still remove saved data.'
    return RemovalTarget(row.track_id, row.path, row.title, state, signature, note)


def remove_book(store, target, delete_file=False, progress=lambda text: None, trash=None):
    """Run on a worker. Never unlink media or silently fall back from Trash."""
    if delete_file and not target.can_delete_file:
        raise ValueError('This file was not verified for deletion.')
    if not delete_file and target.state['book'] is None:
        raise ValueError('This book has no saved data to remove.')
    progress('Backing up saved library data...')
    backup = store.create_backup(label='before-book-removal', prune=False)
    moved = False
    destination = ''

    def move_file():
        nonlocal moved, destination
        path = Path(target.path)
        if _file_signature(path) != target.signature or _read_txxx_text(path, APP_TRACK_ID) != target.track_id:
            raise RuntimeError('The audiobook file changed after confirmation. Nothing was removed.')
        progress('Moving the audiobook file to Trash...')
        ok, destination = trash(target.path) if trash else _trash_file(target.path, target.signature)
        if not ok:
            raise OSError('The audiobook file could not be moved to Trash. Saved data was not removed.')
        moved = True

    try:
        progress('Removing saved book data...')
        store.remove_book(target.track_id, target.state, move_file if delete_file else None)
    except Exception as error:
        if moved:
            restored = bool(destination and QFile(destination).rename(target.path))
            if not restored:
                raise RuntimeError('Saved data could not be removed. The audiobook file is in Trash; restore it there. '
                                   f'Database backup: {backup}') from error
        raise
    return {'target': target, 'file_deleted': moved, 'backup': str(backup)}


class RemoveBookDialog(QDialog):
    def __init__(self, target, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Remove book')
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        count = len(target.state['events'])
        text = (f'Remove saved data for "{target.title}"?\n\n'
                f'This removes {count} listening {"entry" if count == 1 else "entries"}, '
                'the personal rating, favorite, review marker and purchase details.')
        if target.state['book'] is None:
            text = f'"{target.title}" has no saved listening data. You can choose to delete its audiobook file.'
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.delete_file = QCheckBox('Also delete the audiobook file (move to Trash)')
        self.delete_file.setChecked(False)  # Never persist this choice.
        self.delete_file.setEnabled(target.can_delete_file)
        layout.addWidget(self.delete_file)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.explanation.setTextFormat(Qt.PlainText)
        layout.addWidget(self.explanation)
        path = QLabel(target.path)
        path.setTextFormat(Qt.PlainText)
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path.setWordWrap(True)
        layout.addWidget(path)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.remove_button = buttons.addButton('Remove saved data', QDialogButtonBox.AcceptRole)
        self.remove_button.setAutoDefault(False)
        buttons.button(QDialogButtonBox.Cancel).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        def update_choice():
            checked = self.delete_file.isChecked()
            self.remove_button.setText('Remove data and delete audiobook file' if checked else 'Remove saved data')
            self.remove_button.setEnabled(target.state['book'] is not None or checked)
            self.explanation.setText(target.file_note if checked or not target.can_delete_file else
                                     'The audiobook file stays in your collection. A database backup is created before removal.')

        self.delete_file.toggled.connect(update_choice)
        update_choice()
