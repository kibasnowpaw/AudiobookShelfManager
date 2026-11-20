from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import unicodedata
import math
import time
from dataclasses import dataclass, replace
from datetime import date
from collections import OrderedDict, deque
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QPoint, QObject, Signal, Slot, QThread, QTimer, QDate, QSettings, QUrl
from PySide6.QtGui import QBrush, QIcon, QImage, QImageReader, QPixmap, QActionGroup, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QWidget,
    QSizePolicy,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QPushButton,
    QCheckBox,
    QGroupBox,
    QMenu,
    QInputDialog,
    QPlainTextEdit,
    QScrollArea,
    QProgressBar,
    QStyle,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QAbstractItemView,
)

from . import diagnostics as diag
from .album_expected_store import AlbumExpectedStore
from .audio_formats import SUPPORTED_AUDIO_EXTENSIONS
from .covers import (ALBUM_COVER_STEMS, CoverDirectory, find_cover_in_folder,
                     find_track_sidecar_cover, get_embedded_cover_bytes)
from .debug_tools import dbg_exc, debug_call
from .helpers import fmt_len, list_album_dirs, list_artist_dirs, list_tracks
from .library_settings_store import LibrarySettingsStore
from .listening_history_store import ListeningEvent, ListeningHistoryStore
from .metadata_io import read_meta_and_length, write_metadata_to_file
from .models import TrackMeta
from .purchase_info import validate_purchase, owned_for
from .genre_tags import REQUIRED_GENRE_TAGS, split_genre_tags, genre_tag_issue, rule_description
from .gallery_options import effective_options
from .gallery_cards import render_card, card_height, status_color, MUTED, GREEN
from .rating_values import normalize_rating, rating_value, rating_text, mean_rating
from .rating_widgets import RatingSpinBox, RatingStars, RatingStarButton
from .naming import rename_track_file
from .portable_track_state import (
    APP_AUDIBLE,
    APP_GOODREADS,
    ensure_track_id,
    read_bool_flag,
    read_review_flags,
)
from .qt_compat import signal_blocker
from .review_store import ReviewStore
from .scan_index_store import CachedTrack, ScanIndexStore
from .settings_dialog import SettingsDialog, show_diagnostics_dialog
from .series_previews import SeriesPreviews
from .ui_icon_helpers import (
    theme_icon_or_fallback,
    qicon_from_bytes,
)


def _load_library_storage(settings, progress, *, strict=False):
    """Keep every store bound to its library, including while a worker runs."""
    main = settings.active_library_id() == 'main'
    config = {} if main else {'config_directory': settings.profile_config_dir()}
    data = {} if main else {'data_directory': settings.profile_data_dir()}
    expected = AlbumExpectedStore(**config)
    reviews = ReviewStore(**config)
    index = ScanIndexStore(**config)
    history = None
    latest, counts, ratings, reviewed = {}, {}, {}, set()
    history_error = None
    try:
        progress('Opening listening history and checking backups...')
        history = ListeningHistoryStore(**data)
        latest, counts, ratings, reviewed = history.gallery_snapshot()
    except Exception as exc:
        if strict:
            raise
        history_error = str(exc)
        dbg_exc('Listening history database could not be opened')
    return (expected, reviews, settings, index, history, latest, counts, ratings, reviewed, history_error)


def _format_listening_days(day_count: int | None) -> str:
    if day_count is None or day_count <= 0:
        return ""
    unit = "day" if day_count == 1 else "days"
    return f"{day_count} {unit}"


def _narrator_for_metadata(metadata: TrackMeta) -> str:
    """Prefer audiobook narrator tags, with Composer as a legacy fallback."""
    narrator = str(getattr(metadata, "narrator", "") or "").strip()
    if narrator:
        return narrator
    return str(getattr(metadata, "composer", "") or "").strip()


MAX_VISIBLE_BOOK_TAGS = 6
MAX_ALLOWED_GENRE_TAGS = REQUIRED_GENRE_TAGS
COVER_FINGERPRINT_SIZE = 24
COVER_MEAN_RGB_DIFFERENCE_LIMIT = 14.0


def _split_book_tags(raw_genre: object) -> list[str]:
    return split_genre_tags(raw_genre)


def _album_folder_mismatch(
    track_path: Path,
    album_value: object,
) -> dict[str, str] | None:
    """Describe an exact Album-tag/folder mismatch, ignoring outer spaces."""
    album = str(album_value or "").strip()
    folder = Path(track_path).parent.name.strip()
    normalized_album = unicodedata.normalize("NFC", album)
    normalized_folder = unicodedata.normalize("NFC", folder)
    if normalized_album == normalized_folder:
        return None
    return {
        "path": str(track_path),
        "album": album,
        "folder": folder,
    }


def _genre_tag_issue(raw_genre: object, rule=None) -> dict[str, str] | None:
    return genre_tag_issue(raw_genre, rule)


def _read_cover_file_bytes(image_path: Path) -> bytes | None:
    try:
        data = Path(image_path).read_bytes()
    except OSError:
        return None
    return data if data else None


def _image_visual_fingerprint(
    data: bytes,
) -> tuple[float, tuple[int, ...]] | None:
    """Return a small RGB fingerprint that survives normal re-encoding."""
    image = QImage.fromData(data)
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return None

    aspect_ratio = float(image.width()) / float(image.height())
    image = image.convertToFormat(QImage.Format.Format_RGB32)
    image = image.scaled(
        COVER_FINGERPRINT_SIZE,
        COVER_FINGERPRINT_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

    pixels: list[int] = []
    for y in range(image.height()):
        for x in range(image.width()):
            rgb = int(image.pixel(x, y))
            pixels.extend(
                (
                    (rgb >> 16) & 0xFF,
                    (rgb >> 8) & 0xFF,
                    rgb & 0xFF,
                )
            )
    return aspect_ratio, tuple(pixels)


def _images_visually_match(first: bytes, second: bytes) -> bool:
    """Compare image content rather than requiring identical file bytes."""
    if first == second:
        return True

    first_fingerprint = _image_visual_fingerprint(first)
    second_fingerprint = _image_visual_fingerprint(second)
    if first_fingerprint is None or second_fingerprint is None:
        return False

    first_ratio, first_pixels = first_fingerprint
    second_ratio, second_pixels = second_fingerprint
    ratio_limit = max(first_ratio, second_ratio) * 0.03
    if abs(first_ratio - second_ratio) > ratio_limit:
        return False

    if not first_pixels or len(first_pixels) != len(second_pixels):
        return False
    total_difference = sum(
        abs(first_value - second_value)
        for first_value, second_value in zip(
            first_pixels,
            second_pixels,
        )
    )
    mean_difference = total_difference / len(first_pixels)
    return mean_difference <= COVER_MEAN_RGB_DIFFERENCE_LIMIT


_NOT_READ = object()


def _cover_consistency_issue_from_sources(
    track_path: Path,
    sidecar_path: Path | None,
    embedded_bytes: bytes | None,
    folder_cover_path: Path | None,
    *, displayed_bytes=_NOT_READ,
) -> dict[str, str] | None:
    """Check that the cover shown for a book is also embedded in its audio file."""
    embedded_fingerprint = (
        _image_visual_fingerprint(embedded_bytes)
        if embedded_bytes
        else None
    )
    if embedded_bytes and embedded_fingerprint is None:
        return {
            "path": str(track_path),
            "program_cover": "Embedded audiobook artwork",
            "reason": "The embedded cover cannot be read as an image.",
        }

    displayed_path: Path | None = None
    displayed_source = ""
    if sidecar_path is not None:
        displayed_path = sidecar_path
        displayed_source = "Track sidecar cover"
    elif not embedded_bytes and folder_cover_path is not None:
        displayed_path = folder_cover_path
        displayed_source = "Album-folder cover"

    # With no external fallback, a readable embedded cover is exactly what
    # the book view uses. No cover anywhere is not treated as a mismatch.
    if displayed_path is None:
        return None

    if displayed_bytes is _NOT_READ:
        displayed_bytes = _read_cover_file_bytes(displayed_path)
    if displayed_bytes is None or _image_visual_fingerprint(displayed_bytes) is None:
        return {
            "path": str(track_path),
            "program_cover": str(displayed_path),
            "reason": f"{displayed_source} cannot be read as an image.",
        }

    if not embedded_bytes:
        return {
            "path": str(track_path),
            "program_cover": str(displayed_path),
            "reason": (
                f"The program uses {displayed_source.lower()}, but the audiobook file "
                "does not contain an embedded cover."
            ),
        }

    if not _images_visually_match(displayed_bytes, embedded_bytes):
        return {
            "path": str(track_path),
            "program_cover": str(displayed_path),
            "reason": (
                f"The {displayed_source.lower()} shown by the program does "
                "not match the cover embedded in the audiobook file."
            ),
        }
    return None


def _scan_cover_consistency_issue(
    track_path: Path, lookup: CoverDirectory | None = None,
) -> dict[str, str] | None:
    try:
        if lookup is None:
            lookup = CoverDirectory(track_path.parent)
        lookup.refresh()
        sidecar_path = lookup.for_stem(track_path.stem)
        embedded_bytes = get_embedded_cover_bytes(track_path)
        folder_cover_path = (lookup.preferred(ALBUM_COVER_STEMS)
                             if sidecar_path is None and not embedded_bytes else None)
        return _cover_consistency_issue_from_sources(
            track_path,
            sidecar_path,
            embedded_bytes,
            folder_cover_path,
        )
    except Exception as exc:
        return {
            "path": str(track_path),
            "program_cover": "<cover check failed>",
            "reason": (
                "Cover consistency could not be checked: "
                f"{type(exc).__name__}: {exc}"
            ),
        }


def _track_metadata_issue_messages(
    track_path: Path,
    metadata: TrackMeta,
    rule=None,
) -> list[str]:
    issues: list[str] = []
    mismatch = _album_folder_mismatch(track_path, metadata.album)
    if mismatch is not None:
        album = mismatch["album"] or "<empty>"
        issues.append(
            f"Album tag '{album}' does not match folder "
            f"'{mismatch['folder']}'."
        )

    genre_issue = _genre_tag_issue(metadata.genre, rule)
    if genre_issue is not None:
        issues.append(genre_issue["reason"])
    return issues


def _cover_sources(track_path, lookup, track_stat=None):
    """Return the v5 signature and the image paths used to build it."""
    try:
        folder_stat, images = lookup.signature_sources(track_path.stem)
        sources = [(track_path, track_stat if track_stat is not None else track_path.stat()),
                   (track_path.parent, folder_stat), *images]
        values = [(str(p), st.st_size, st.st_mtime_ns, st.st_ctime_ns) if p is not None else None
                  for p, st in sources]
        return repr((1, values)), images[0][0], images[1][0]
    except OSError:
        return "", None, None


def _cover_source_signature(track_path: Path, lookup: CoverDirectory | None = None, *, track_stat=None) -> str:
    return _cover_sources(track_path, lookup or CoverDirectory(track_path.parent), track_stat)[0]


def _validated_cover_record(record: CachedTrack, path: Path, previous=None, lookup=None, *,
                            track_stat=None, counters=None) -> CachedTrack:
    if lookup is None:
        lookup = CoverDirectory(path.parent)
    signature = _cover_source_signature(path, lookup, track_stat=track_stat)
    hit = bool(previous is not None and previous.cover_checked and signature
               and signature == previous.cover_signature)
    if counters is not None:
        counters['cover_cached' if hit else 'cover_rechecked'] += 1
    if hit:
        if record is previous:
            return record
        return replace(record, cover_checked=True, cover_signature=signature, cover_issue=previous.cover_issue)
    issue = _scan_cover_consistency_issue(path, lookup)
    # A new or changed cover must still match an independent, fresh signature
    # after it was read; never pass the initial audio stat into this second check.
    stable = bool(signature and signature == _cover_source_signature(path, lookup))
    return replace(record, cover_checked=stable, cover_signature=signature, cover_issue=issue)


class FileTask(QThread):
    """Run file/database work without touching any widgets in this thread."""
    progress = Signal(str)

    def __init__(self, work, parent=None, *, operation, details=None):
        super().__init__(parent)
        self.work = work
        self.operation = operation
        self.details = details or {}
        self.result = None
        self.error = None

    def run(self):
        started = time.perf_counter()
        diag.record(self.operation, 'start', **self.details)
        try:
            self.result = self.work(self.progress.emit)
        except Exception as exc:
            self.error = exc
            diag.record(self.operation, 'error', exc=exc, ms=diag.elapsed_ms(started), **self.details)
            dbg_exc("Background file operation failed")
        else:
            flags = {}
            if self.operation == 'book.save' and isinstance(self.result, dict):
                flags = {k: bool(self.result.get(k)) for k in
                         ('rename_error', 'refresh_error', 'rating_error', 'purchase_error')}
            elif self.operation == 'book.open' and isinstance(self.result, dict):
                flags = {'history_error': bool(self.result.get('history_error'))}
            elif self.operation == 'library.open' and self.result:
                flags = {'history_error': bool(self.result[-1])}
            state = 'warning' if any(flags.values()) else 'ok'
            if self.operation == 'book.open' and isinstance(self.result, dict):
                flags['cached'] = bool(self.result.get('metadata_cached'))
                flags['cover_cached'] = bool(self.result.get('cover_cached'))
                flags['preview_cached'] = bool(self.result.get('preview_cached'))
                if 'history_written' in self.result:
                    flags['history_written'] = self.result['history_written']
                flags.update((k, self.result.get(k)) for k in ('metadata_ms', 'history_ms', 'artwork_ms'))
            diag.record(self.operation, state, ms=diag.elapsed_ms(started), **self.details, **flags)


class BusyDialog(QDialog):
    """A visible wait indicator; Escape cannot abandon an in-flight write."""
    def __init__(self, title, message, parent):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModal)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        self.message = QLabel(message, self)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)

    def reject(self):
        pass

    def closeEvent(self, event):
        event.ignore()


@dataclass
class _BookCover:
    image: QImage
    embedded: bytes | None
    embedded_read: bool
    signature: str
    checked: bool
    issue: dict | None
    cached: bool


def _read_book_cover(path, record):
    """Read the image for display. Reuse validation only for identical sources."""
    lookup = CoverDirectory(path.parent)
    for attempt in range(2):
        signature, sidecar, folder = _cover_sources(path, lookup)
        if not signature:
            sidecar = find_track_sidecar_cover(path)
        hit = bool(record is not None and record.cover_checked and signature
                   and signature == record.cover_signature
                   and (record.cover_issue or {}).get("program_cover") != "<cover check failed>")
        displayed = _read_cover_file_bytes(sidecar) if sidecar is not None else None
        image = QImage.fromData(displayed) if displayed else QImage()
        if sidecar is not None and image.isNull():
            hit = False  # Always surface a present-day read failure.
        embedded_read = not (hit and sidecar is not None)
        embedded = get_embedded_cover_bytes(path, prime_portable=True) if embedded_read else None
        if sidecar is None:
            if embedded:
                displayed = embedded
            else:
                if not signature:
                    folder = find_cover_in_folder(path.parent, ALBUM_COVER_STEMS)
                displayed = _read_cover_file_bytes(folder) if folder is not None else None
            image = QImage.fromData(displayed) if displayed else QImage()
            if image.isNull() and (displayed or (not embedded and folder is not None)):
                hit = False
        issue = record.cover_issue if hit else _cover_consistency_issue_from_sources(
            path, sidecar, embedded, folder, displayed_bytes=displayed)
        stable = bool(signature and signature == _cover_source_signature(path, lookup))
        if signature and not stable and attempt == 0:
            record = None  # A source changed mid-read: reselect it and verify afresh.
            continue
        if not image.isNull():
            image = image.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return _BookCover(image, embedded, embedded_read, signature, stable, issue, hit and stable)


def _prepare_series_cover(path, record, artist_dirs):
    """Prepare a small preview without opening a detail page or writing to the book or DB."""
    if not path.is_file():
        return None
    art = _read_book_cover(path, record)
    # Retain the decoded preview, never an entire embedded cover for each card.
    art = replace(art, embedded=None, embedded_read=False)
    image = art.image
    if image.isNull():
        # Folder/author artwork can decorate a card. Never pass author art to the editor.
        locations = [(path.parent, ALBUM_COVER_STEMS)]
        artist_dir = next((d for d in artist_dirs if d.is_dir()), None)
        if artist_dir is not None:
            locations.append((artist_dir, ('artist', 'artist-cover', 'cover', 'folder', 'front')))
        for directory, stems in locations:
            candidate = find_cover_in_folder(directory, stems)
            if candidate is not None:
                reader = QImageReader(str(candidate))
                size = reader.size()
                if size.isValid():
                    reader.setScaledSize(size.scaled(QSize(200, 200), Qt.KeepAspectRatio))
                image = reader.read()
                if not image.isNull():
                    break
    if image.width() > 200 or image.height() > 200:
        image = image.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return art, image


class _BookChangedWhileOpening(RuntimeError):
    pass


def _load_book_data(path, record, history_store, progress, review_store=None, preview=None):
    """Read fresh data off the UI thread; retry one concurrent file replacement."""
    try:
        return _load_book_once(path, record, history_store, progress, review_store, preview)
    except _BookChangedWhileOpening:
        return _load_book_once(path, None, history_store, progress, review_store)


def _load_book_once(path, record, history_store, progress, review_store, preview=None):
    path = Path(path)
    try:
        original_stat = path.stat()
        if not stat.S_ISREG(original_stat.st_mode):
            raise FileNotFoundError
    except (FileNotFoundError, NotADirectoryError):
        raise FileNotFoundError(f"The audiobook file no longer exists: {path}") from None
    began = time.perf_counter()
    progress("Loading audiobook metadata...")
    valid_record = record if record is not None and record.matches_stat(original_stat) else None
    if valid_record is not None:
        metadata, length = valid_record.metadata, valid_record.length_seconds
    else:
        metadata, length = read_meta_and_length(path)
    data = {"path": path, "metadata": metadata, "length": length, "metadata_cached": valid_record is not None,
            "track_id": None, "history_error": False, "events": [],
            "preferences": (0, False), "reviewed": False, "purchase": ('', ''), "listen_count": 0,
            "last_listened": None, "listening_days": None}
    data["metadata_ms"] = diag.elapsed_ms(began)
    began = time.perf_counter()
    progress("Loading and checking cover...")
    # The series preview is only a candidate. External sidecars, folder art,
    # and the audio file itself may all have changed since the grid was shown.
    use_preview = (preview is not None and preview.checked and bool(preview.signature)
                   and preview.signature == _cover_source_signature(path))
    art = replace(preview, cached=True) if use_preview else _read_book_cover(path, valid_record)
    data["preview_cached"] = use_preview
    # A different file could have landed at this path while its picture was
    # loading. Never register its identity with the previous file's title.
    after_art = path.stat()
    if any(getattr(original_stat, k) != getattr(after_art, k) for k in
           ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
        raise _BookChangedWhileOpening("The audiobook changed while opening. Please open it again.")
    data["artwork_ms"] = diag.elapsed_ms(began)
    began = time.perf_counter()
    if history_store is not None:
        try:
            progress("Loading listening history...")
            saved_book, events, written = history_store.details_for_file(path, metadata, length)
            data["track_id"] = saved_book.track_id
            data["history_written"] = written
            data["preferences"] = (saved_book.rating, saved_book.favorite)
            data["reviewed"] = saved_book.reviewed
            data["purchase"] = (saved_book.purchased_on, saved_book.purchase_source)
            data["events"] = events
            completed = [e for e in events if str(e.started_on or "").strip()
                         and str(e.finished_on or "").strip()]
            data["listen_count"] = len(completed)
            if completed:
                latest = max(completed, key=lambda e: (e.finished_on, e.event_id))
                data["last_listened"] = latest.finished_on
                data["listening_days"] = max(1, (date.fromisoformat(latest.finished_on)
                    - date.fromisoformat(latest.started_on)).days + 1)
        except Exception:
            data["history_error"] = True
            dbg_exc(f"Could not load listening history for {path}")

    data["history_ms"] = diag.elapsed_ms(began)
    embedded_goodreads, embedded_audible = read_review_flags(path)
    if record is not None:
        legacy_gr, legacy_au = record.goodreads, record.audible
    elif review_store is not None and (embedded_goodreads is None or embedded_audible is None):
        legacy_gr, legacy_au = review_store.legacy_flags(path)
    else:
        legacy_gr, legacy_au = False, False
    goodreads = legacy_gr if embedded_goodreads is None else embedded_goodreads
    audible = legacy_au if embedded_audible is None else embedded_audible
    new_record = CachedTrack.from_file(path, metadata, length, watched=False,
        goodreads=goodreads, audible=audible,
        portable_id_ready=bool(data["track_id"] or (valid_record and valid_record.portable_id_ready)),
        track_id=data["track_id"] or (valid_record.track_id if valid_record else ""))
    if not new_record.matches_stat(original_stat):
        # Never attach metadata read before a concurrent edit to the new file stamp.
        new_record = replace(new_record, size=original_stat.st_size, mtime_ns=original_stat.st_mtime_ns,
                             ctime_ns=original_stat.st_ctime_ns)
        art.checked = art.cached = False
    data["record"] = replace(new_record, cover_checked=art.checked,
                             cover_signature=art.signature, cover_issue=art.issue)
    data.update(cover_image=art.image, embedded=art.embedded, embedded_read=art.embedded_read,
                cover_cached=art.cached)
    return data


class CoverVerificationError(RuntimeError):
    pass


def _save_book_data(path, metadata, cover, autorename, rename_sidecar,
                    review_store, history_store, progress, rating=None, purchase=None):
    """Write, verify, rename, then read the finished book before reporting success."""
    rating = normalize_rating(rating) if rating is not None else None
    purchase = validate_purchase(*purchase) if purchase is not None else None
    progress("Saving, please wait...\nWriting metadata and artwork.")
    selected = _read_cover_file_bytes(cover) if cover is not None else None
    if cover is not None and not selected:
        raise ValueError("The selected cover could not be read. Select it again before saving.")
    for attempt in range(2):
        try:
            write_metadata_to_file(path, metadata, cover_path=cover)
        except Exception as exc:
            if attempt:
                raise CoverVerificationError(f"The automatic cover retry failed: {exc}") from exc
            raise
        if cover is None:
            break
        progress("Saving, please wait...\nVerifying the cover inside the audiobook file.")
        actual = get_embedded_cover_bytes(path)
        if actual and _images_visually_match(selected, actual):
            break
        if attempt:
            raise CoverVerificationError(
                "The cover still does not match after two write attempts. "
                "The file was not renamed and the selected cover remains pending.")
        diag.record('book.cover', 'retry', format=diag.audio_kind(path), attempt=2)
        progress("Saving, please wait...\nRetrying the cover write.")

    new_path = path
    rename_error = None
    if autorename:
        progress("Saving, please wait...\nUpdating the filename.")
        try:
            new_path = rename_track_file(path, metadata, rename_sidecar=rename_sidecar)
        except Exception as exc:
            rename_error = str(exc)
        if new_path != path:
            try:
                review_store.move_track(path, new_path)
            except Exception as exc:
                rename_error = f"The file was renamed, but review-state migration failed: {exc}"
    progress("Saving, please wait...\nRefreshing saved book information.")
    try:
        book = _load_book_data(new_path, None, history_store, lambda _message: None, review_store)
    except Exception as exc:
        # A completed write/rename is never reported as if the old path still exists.
        return {"old_path": path, "new_path": new_path, "book": None,
                "refresh_error": str(exc), "rename_error": rename_error,
                "cover_saved": cover is not None, "requested_rating": rating,
                "rating_error": 'The new rating was not saved because the book could not be reloaded.' if rating is not None else None,
                'requested_purchase': purchase,
                'purchase_error': 'Purchase details were not saved because the book could not be reloaded.' if purchase is not None else None}
    rating_error = None
    if rating is not None:
        try:
            if history_store is None or book['track_id'] is None:
                raise RuntimeError('The listening database is unavailable.')
            progress('Saving, please wait...\nSaving your rating.')
            history_store.set_rating(book['track_id'], rating)
            book['preferences'] = (rating, book['preferences'][1])
        except Exception as exc:
            rating_error = str(exc)
    purchase_error = None
    if purchase is not None:
        try:
            if history_store is None or book['track_id'] is None:
                raise RuntimeError('The listening database is unavailable.')
            progress('Saving, please wait...\nSaving purchase details.')
            history_store.set_purchase(book['track_id'], *purchase)
            book['purchase'] = purchase
        except Exception as exc:
            purchase_error = str(exc)
    return {"old_path": path, "new_path": new_path, "book": book,
            'requested_purchase': purchase, 'purchase_error': purchase_error,
            "refresh_error": None, "rename_error": rename_error,
            "cover_saved": cover is not None, "requested_rating": rating,
            "rating_error": rating_error}


class ScanWorker(QObject):
    """
    Background scanner:
    - Builds caches for artist dirs, album dirs, and mp3 track lists.
    - Reuses metadata and flags for files whose signature has not changed.
    - Refreshes only new or modified files.
    - Assigns portable track IDs to playable files in the worker thread.
    - Reports unreadable audiobook files instead of silently accepting them.
    - Learns expected totaltracks from cached or freshly read metadata.
    - Scans multiple library roots into one combined cache.
    """
    progress = Signal(int, str)  # percent, message
    finished = Signal(object)    # payload dict

    def __init__(
        self,
        roots: list[Path],
        expected: AlbumExpectedStore,
        scan_index: ScanIndexStore,
        review_legacy: dict[str, dict[str, bool]],
        history_store: ListeningHistoryStore | None = None,
        profile=None,
    ) -> None:
        super().__init__()
        self.roots = [Path(p) for p in roots]
        self.expected = expected
        self.scan_index = scan_index
        self.history_store = history_store
        self.profile = profile
        self.review_legacy = {
            str(path): dict(values)
            for path, values in review_legacy.items()
        }
        self._cancel = False
        self._last_progress_at = 0.0

    def _emit_progress(self, percent: int, message: str) -> None:
        now = time.monotonic()
        if percent in (0, 100) or now - self._last_progress_at >= 0.08:
            self._last_progress_at = now
            self.progress.emit(percent, message)

    def cancel(self) -> None:
        self._cancel = True

    def _resolved_flag(
        self,
        track_path: Path,
        description: str,
        fallback: bool,
    ) -> bool:
        try:
            embedded = read_bool_flag(track_path, description)
        except Exception:
            dbg_exc(
                f"Incremental scan flag read failed for "
                f"{track_path} desc={description}"
            )
            embedded = None

        return bool(fallback if embedded is None else embedded)

    def run(self) -> None:
        scan_started = time.perf_counter()
        diag.record('scan', 'start', roots=len(self.roots),
                    portal_roots=sum(diag.is_document_portal(path) for path in self.roots))
        payload: dict[str, object] = {
            "cancelled": True,
            "error": False,
            "artist_dirs": [],
            "albums_by_artist": {},
            "mp3s_by_album": {},
            "track_records": {},
            "reused": 0,
            "refreshed": 0,
            "removed": 0,
            "failed": 0,
            "broken_files": [],
            "identity_failures": [],
            "album_mismatches": [],
            "genre_issues": [],
            "cover_issues": [],
            "index_saved": False,
        }

        try:
            self._emit_progress(0, "Discovering library folders")
            artists: list[Path] = []
            albums_all: list[Path] = []
            albums_by_artist: dict[str, list[Path]] = {}
            seen_artists: set[str] = set()

            for root in self.roots:
                if self._cancel:
                    return
                if not root.is_dir():
                    continue

                for artist_dir in list_artist_dirs(root):
                    if self._cancel:
                        return

                    key = str(artist_dir)
                    if key in seen_artists:
                        continue
                    seen_artists.add(key)

                    artists.append(artist_dir)

                    als = list_album_dirs(artist_dir)
                    albums_by_artist[key] = als
                    albums_all.extend(als)

            discovered_by_album: dict[Path, tuple[CoverDirectory, list[Path]]] = {}
            total_files = 0

            m4b_count = 0
            for album_dir in albums_all:
                if self._cancel:
                    return

                covers = CoverDirectory(album_dir)
                mp3s = covers.discover_tracks(SUPPORTED_AUDIO_EXTENSIONS)
                discovered_by_album[album_dir] = covers, mp3s
                total_files += len(mp3s)
                m4b_count += sum(1 for path in mp3s if path.suffix.lower() == '.m4b')
            diag.record('scan.discovery', 'ok', ms=diag.elapsed_ms(scan_started), series=len(albums_all),
                        mp3=total_files - m4b_count, m4b=m4b_count)

            validation_started = time.perf_counter()
            cover_seconds = 0.
            cover_counts = {'cover_cached': 0, 'cover_rechecked': 0}
            mp3s_by_album: dict[str, list[Path]] = {}
            track_records: dict[str, CachedTrack] = {}
            previous_paths = set(self.scan_index.entries())
            discovered_totals: dict[str, int] = {}
            processed = 0
            reused = 0
            refreshed = 0
            failed = 0
            broken_files: list[dict[str, str]] = []
            identity_failures: list[str] = []
            album_mismatches: list[dict[str, str]] = []
            genre_issues: list[dict[str, str]] = []
            cover_issues: list[dict[str, str]] = []

            for album_dir in albums_all:
                if self._cancel:
                    return

                covers, discovered_tracks = discovered_by_album.pop(album_dir)
                tag_rule = effective_options(self.profile, album_dir)["tag_rule"]
                valid_mp3s: list[Path] = []
                album_total = 0

                for track_path in discovered_tracks:
                    if self._cancel:
                        return

                    track_key = str(track_path)

                    try:
                        stat_result = track_path.stat()
                    except OSError as exc:
                        failed += 1
                        broken_files.append(
                            {
                                "path": track_key,
                                "reason": f"File could not be accessed: {exc}",
                            }
                        )
                        processed += 1
                        percent = (
                            int((processed * 100) / total_files)
                            if total_files > 0
                            else 100
                        )
                        self._emit_progress(
                            percent,
                            f"Indexing files {processed}/{total_files}",
                        )
                        continue

                    record = self.scan_index.unchanged(track_path, stat_result)
                    previous_record = record

                    if (
                        record is not None
                        and record.length_seconds is not None
                        and math.isfinite(float(record.length_seconds))
                        and float(record.length_seconds) > 0.0
                    ):
                        metadata = record.metadata
                        length_seconds = float(record.length_seconds)
                        goodreads = record.goodreads
                        audible = record.audible
                        reused += 1
                    else:
                        record = None
                        try:
                            metadata, length_seconds = read_meta_and_length(track_path)

                            if (
                                length_seconds is None
                                or not math.isfinite(float(length_seconds))
                                or float(length_seconds) <= 0.0
                            ):
                                failed += 1
                                broken_files.append(
                                    {
                                        "path": track_key,
                                        "reason": (
                                            "No readable audio stream or "
                                            "positive duration was found."
                                        ),
                                    }
                                )
                                processed += 1
                                percent = (
                                    int((processed * 100) / total_files)
                                    if total_files > 0
                                    else 100
                                )
                                self._emit_progress(
                                    percent,
                                    f"Indexing files {processed}/{total_files}",
                                )
                                continue

                            review_fallback = self.review_legacy.get(track_key, {})
                            goodreads = self._resolved_flag(
                                track_path,
                                APP_GOODREADS,
                                bool(review_fallback.get("goodreads", False)),
                            )
                            audible = self._resolved_flag(
                                track_path,
                                APP_AUDIBLE,
                                bool(review_fallback.get("audible", False)),
                            )

                            refreshed += 1

                        except Exception as exc:
                            failed += 1
                            dbg_exc(f"Incremental scan failed for {track_path}")
                            broken_files.append(
                                {
                                    "path": track_key,
                                    "reason": (
                                        f"Audiobook scan failed: "
                                        f"{type(exc).__name__}: {exc}"
                                    ),
                                }
                            )
                            processed += 1
                            percent = (
                                int((processed * 100) / total_files)
                                if total_files > 0
                                else 100
                            )
                            self._emit_progress(
                                percent,
                                f"Indexing files {processed}/{total_files}",
                            )
                            continue

                    if self._cancel:
                        return

                    track_id = record.track_id if record is not None else ""
                    portable_id_ready = bool(record is not None
                                             and record.portable_id_ready and track_id)
                    reuse_audio_stat = portable_id_ready
                    if not portable_id_ready:
                        try:
                            before_identity_stat = track_path.stat()
                            before_identity_signature = (
                                int(before_identity_stat.st_size),
                                int(before_identity_stat.st_mtime_ns),
                                int(before_identity_stat.st_ctime_ns),
                            )
                            current_percent = (
                                int((processed * 100) / total_files)
                                if total_files > 0
                                else 100
                            )
                            self._emit_progress(
                                current_percent,
                                f"Preparing audiobook ID {processed + 1}/{total_files}",
                            )
                            track_id = ensure_track_id(track_path) or ""
                            portable_id_ready = bool(track_id)

                            after_identity_stat = track_path.stat()
                            after_identity_signature = (
                                int(after_identity_stat.st_size),
                                int(after_identity_stat.st_mtime_ns),
                                int(after_identity_stat.st_ctime_ns),
                            )
                            if (
                                portable_id_ready
                                and after_identity_signature
                                != before_identity_signature
                            ):
                                metadata, length_seconds = read_meta_and_length(
                                    track_path
                                )
                                if (
                                    length_seconds is None
                                    or not math.isfinite(float(length_seconds))
                                    or float(length_seconds) <= 0.0
                                ):
                                    failed += 1
                                    broken_files.append(
                                        {
                                            "path": track_key,
                                            "reason": (
                                                "The audiobook file became unreadable after "
                                                "its portable ID was saved."
                                            ),
                                        }
                                    )
                                    processed += 1
                                    percent = (
                                        int((processed * 100) / total_files)
                                        if total_files > 0
                                        else 100
                                    )
                                    self._emit_progress(
                                        percent,
                                        f"Indexing files {processed}/{total_files}",
                                    )
                                    continue
                        except Exception:
                            portable_id_ready = False
                            dbg_exc(
                                f"Background track ID failed for {track_path}"
                            )

                        if not portable_id_ready:
                            identity_failures.append(track_key)

                    try:
                        if reuse_audio_stat:
                            # This immutable entry already matched this file, and no
                            # identity write was needed. Keep its original file stamp.
                            if record.watched:
                                record = replace(record, watched=False)
                        else:
                            record = CachedTrack.from_file(
                                track_path,
                                metadata,
                                length_seconds,
                                watched=False,
                                goodreads=goodreads,
                                audible=audible,
                                portable_id_ready=portable_id_ready,
                                track_id=track_id,
                            )
                    except Exception as exc:
                        failed += 1
                        dbg_exc(f"Could not cache scanned audiobook file {track_path}")
                        broken_files.append(
                            {
                                "path": track_key,
                                "reason": (
                                    f"Could not cache the scanned audiobook: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            }
                        )
                        record = None

                    processed += 1

                    if record is not None:
                        valid_mp3s.append(track_path)
                        mismatch = _album_folder_mismatch(
                            track_path,
                            record.metadata.album,
                        )
                        if mismatch is not None:
                            album_mismatches.append(mismatch)
                        genre_issue = _genre_tag_issue(
                            record.metadata.genre, tag_rule
                        )
                        if genre_issue is not None:
                            genre_issues.append(
                                {
                                    "path": track_key,
                                    **genre_issue,
                                }
                            )
                        cover_started = time.perf_counter()
                        record = _validated_cover_record(record, track_path, previous_record, covers,
                            track_stat=stat_result if reuse_audio_stat else None, counters=cover_counts)
                        cover_seconds += time.perf_counter() - cover_started
                        track_records[track_key] = record
                        cover_issue = record.cover_issue
                        if cover_issue is not None:
                            cover_issues.append(cover_issue)
                        album_total = max(
                            album_total,
                            int(getattr(record.metadata, "totaltracks", 0) or 0),
                        )

                    percent = (
                        int((processed * 100) / total_files)
                        if total_files > 0
                        else 100
                    )
                    self._emit_progress(
                        percent,
                        f"Indexing files {processed}/{total_files}",
                    )

                mp3s_by_album[str(album_dir)] = valid_mp3s

                if album_total > 0:
                    discovered_totals[str(album_dir)] = album_total

            if self._cancel:
                return

            diag.record('scan.checks', 'ok', ms=diag.elapsed_ms(validation_started),
                        cover_ms=round(cover_seconds * 1000), **cover_counts)
            index_started = time.perf_counter()
            self.expected.fill_missing(discovered_totals)
            index_saved = True
            index_written = None
            try:
                index_written = self.scan_index.save(self.roots, track_records)
            except Exception:
                index_saved = False
                dbg_exc("Failed saving the persistent incremental scan index")

            diag.record('scan.index', 'ok' if index_saved else 'error', ms=diag.elapsed_ms(index_started),
                        index_written=index_written)
            removed = len(previous_paths.difference(track_records))
            history_path_updates = 0
            history_path_error = False
            if self.history_store is not None:
                history_started = time.perf_counter()
                try:
                    history_path_updates = self.history_store.reconcile_scanned_paths(track_records)
                except Exception:
                    history_path_error = True
                    dbg_exc("Could not update saved audiobook paths after scanning")
                diag.record('scan.history', 'error' if history_path_error else 'ok',
                            ms=diag.elapsed_ms(history_started), paths_updated=history_path_updates)

            payload = {
                "cancelled": False,
                "error": False,
                "artist_dirs": artists,
                "albums_by_artist": albums_by_artist,
                "mp3s_by_album": mp3s_by_album,
                "track_records": track_records,
                "reused": reused,
                "refreshed": refreshed,
                "removed": removed,
                "history_path_updates": history_path_updates,
                "history_path_error": history_path_error,
                "failed": failed,
                "broken_files": broken_files,
                "identity_failures": identity_failures,
                "album_mismatches": album_mismatches,
                "genre_issues": genre_issues,
                "validation_profile": self.profile,
                "cover_issues": cover_issues,
                "index_saved": index_saved,
            }

        except Exception:
            dbg_exc("ScanWorker.run failed")
            payload = {
                "cancelled": False,
                "error": True,
                "artist_dirs": [],
                "albums_by_artist": {},
                "mp3s_by_album": {},
                "track_records": {},
                "reused": 0,
                "refreshed": 0,
                "removed": 0,
                "failed": 0,
                "broken_files": [],
                "identity_failures": [],
                "album_mismatches": [],
                "genre_issues": [],
                "cover_issues": [],
                "index_saved": False,
            }
        finally:
            # Only counts enter the journal; the payload contains private paths and metadata.
            try:
                if payload['error'] or payload['cancelled']:
                    diag.record('scan', 'error' if payload['error'] else 'cancelled', ms=diag.elapsed_ms(scan_started))
                else:
                    diag.record('scan', 'ok', ms=diag.elapsed_ms(scan_started),
                                books=len(payload['track_records']), reused=payload['reused'],
                                refreshed=payload['refreshed'], removed=payload['removed'], failed=payload['failed'],
                                id_warnings=len(payload['identity_failures']), album_warnings=len(payload['album_mismatches']),
                                genre_warnings=len(payload['genre_issues']), cover_warnings=len(payload['cover_issues']),
                                index_saved=payload['index_saved'], history_path_error=payload['history_path_error'])
            finally:
                self.finished.emit(payload)


class ListeningEventDialog(QDialog):
    """Collect and validate one completed audiobook listening period."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        event: ListeningEvent | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            "Edit Completed Listen"
            if event is not None
            else "Add Completed Listen"
        )
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        explanation = QLabel(
            "A completed listen requires both a start date and a finish "
            "date. The audiobook's current duration is saved automatically "
            "with the entry."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate.currentDate())

        self.finish_date = QDateEdit()
        self.finish_date.setCalendarPopup(True)
        self.finish_date.setDisplayFormat("yyyy-MM-dd")
        self.finish_date.setDate(QDate.currentDate())

        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Optional note")
        self.note.setFixedHeight(90)

        form.addRow("Started", self.start_date)
        form.addRow("Finished", self.finish_date)
        form.addRow("Note", self.note)
        layout.addLayout(form)

        if event is not None:
            finish = QDate.fromString(
                event.finished_on,
                "yyyy-MM-dd",
            )
            if finish.isValid():
                self.finish_date.setDate(finish)

            if event.started_on:
                start = QDate.fromString(
                    event.started_on,
                    "yyyy-MM-dd",
                )
                if start.isValid():
                    self.start_date.setDate(start)
            else:
                # A legacy entry without a start date is not completed under
                # the From/To rule. Default it to the finish date so saving
                # the edit can turn it into a valid completed listen.
                self.start_date.setDate(self.finish_date.date())

            self.note.setPlainText(event.note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if self.start_date.date() > self.finish_date.date():
            QMessageBox.warning(
                self,
                "Invalid dates",
                "The start date cannot be after the finish date.",
            )
            return

        self.accept()

    def values(self) -> tuple[str, str, str]:
        started_on = self.start_date.date().toString("yyyy-MM-dd")
        finished_on = self.finish_date.date().toString("yyyy-MM-dd")
        note = self.note.toPlainText().strip()
        return started_on, finished_on, note


class MainWindow(QMainWindow):
    EMBEDDED_CACHE_MAX_ENTRIES = 128
    EMBEDDED_CACHE_MAX_BYTES = 64 * 1024 * 1024
    PATH_ICON_CACHE_MAX_ENTRIES = 256
    DECODED_ICON_CACHE_MAX_ENTRIES = 192
    COVER_LOOKUP_CACHE_MAX_ENTRIES = 1024
    ARTWORK_BATCH_SIZE = 2
    BOOK_COVER_ROLE = int(Qt.ItemDataRole.UserRole) + 1
    ENGLISH_ARTIST_INDEX = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    DANISH_ARTIST_INDEX = ("\u00C6", "\u00D8", "\u00C5")

    PAGE_ARTISTS = 0
    PAGE_ALBUMS = 1
    PAGE_TRACKS = 2
    PAGE_BOOK = 3
    PAGE_STATISTICS = 4

    ARTIST_SORTS: list[tuple[str, str]] = [
        ("A-Z", "az"),
        ("Highest rated writer", "rating_high"),
        ("Lowest rated writer", "rating_low"),
        ("Newest updated", "newest"),
        ("Oldest updated", "oldest"),
        ("Most completed series", "most_completed"),
        ("Least completed series", "least_completed"),
        ("Most series", "most_series"),
        ("Fewest series", "fewest_series"),
    ]

    SERIES_SORTS: list[tuple[str, str]] = [
        ("A-Z", "az"),
        ("Newest updated", "newest"),
        ("Oldest updated", "oldest"),
        ("Completed first", "completed_first"),
        ("Incomplete first", "incomplete_first"),
        ("Most progress", "most_progress"),
        ("Least progress", "least_progress"),
        ("Most owned", "most_owned"),
        ("Missing most", "missing_most"),
    ]

    @debug_call()
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Audiobook Shelf Manager")
        self.resize(1700, 920)

        self.library_roots: list[Path] = []
        self.library_root: Path | None = None  # kept for compatibility
        self.current_artist_key: str | None = None
        self.current_artist_dir: Path | None = None  # legacy compatibility
        self.current_album_dir: Path | None = None

        self.current_track_count = 0
        self.current_album_expected = 0

        self._loading_tracks = False
        self._selected_track_path: Path | None = None
        self._suppress_track_selection_handler = False
        self._book_track_id: str | None = None
        self._book_title_text = ""
        self._book_editing = False
        self._book_rating = 0
        self._book_favorite = False
        self._book_purchase = ('', '')
        self._listening_events_by_id: dict[int, ListeningEvent] = {}
        self._pending_cover_path: Path | None = None
        self._pending_cover_track_path: Path | None = None

        self.expected_store = None
        self.review_store = None
        self.library_settings_store = None
        self._library_switchers = []
        self._pending_library_change = None
        self.scan_index_store = None
        self.listening_history_store = None
        self._completed_history_paths: set[str] = set()
        self._last_listened_history_by_path: dict[str, str] = {}
        self._listening_days_history_by_path: dict[str, int] = {}
        self._completed_listen_count_by_path: dict[str, int] = {}
        self._book_rating_by_path: dict[str, float] = {}
        self._reviewed_paths = set()
        self._file_job = None
        self._file_job_callback = None
        self._busy_dialog = None
        self._book_cover_image = QImage()
        self._book_cover_path = None
        self._book_artwork_generation = 0
        self.statistics_page = None
        self._statistics_job = None
        self._statistics_request = 0
        self._statistics_pending = False
        self._statistics_return_page = self.PAGE_ARTISTS
        self._book_return_to_statistics = False

        self._embedded_cache: OrderedDict[
            str,
            tuple[tuple[int, int], bytes | None],
        ] = OrderedDict()
        self._embedded_cache_bytes = 0
        self._path_icon_cache: OrderedDict[
            tuple[str, int, int],
            QIcon,
        ] = OrderedDict()
        self._decoded_icon_cache: OrderedDict[
            tuple[str, int, int, int, int],
            QIcon,
        ] = OrderedDict()
        self._cover_lookup_cache: OrderedDict[
            tuple[str, str, tuple[str, ...], int],
            Path | None,
        ] = OrderedDict()
        self._track_records: dict[str, CachedTrack] = {}

        self._cache_ready = False
        self._artist_dirs_cache: list[Path] = []
        self._albums_by_artist_cache: dict[str, list[Path]] = {}
        self._mp3s_by_album_cache: dict[str, list[Path]] = {}

        self._artist_groups_cache: dict[str, list[Path]] = {}
        self._artist_names_cache: dict[str, str] = {}
        self._artist_folders_by_key_cache: dict[str, list[Path]] = {}
        self._album_artist_alias_cache: dict[str, list[str]] = {}

        self._album_meta_cache: dict[str, TrackMeta] = {}

        self._artist_item_by_dir: dict[str, QListWidgetItem] = {}
        self._album_item_by_dir: dict[str, QListWidgetItem] = {}
        self._book_item_by_path: dict[str, QListWidgetItem] = {}
        self._artist_index_targets: dict[str, QListWidgetItem] = {}

        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._scan_pending = False
        self._closing = False

        self._artwork_queue: deque[tuple[str, int, object]] = deque()
        self._artist_artwork_generation = 0
        self._album_artwork_generation = 0
        self._held_parent_jobs = {'artist': {}, 'album': {}}
        self._series_previews = SeriesPreviews(self, _prepare_series_cover)
        self._series_previews.ready.connect(self._on_series_cover)
        self._series_previews.idle.connect(self._artwork_idle)
        self._book_viewport_timer = QTimer(self)
        self._book_viewport_timer.setSingleShot(True)
        self._book_viewport_timer.timeout.connect(self._prioritize_visible_books)
        self._artwork_timer = QTimer(self)
        self._artwork_timer.setInterval(0)
        self._artwork_timer.timeout.connect(self._process_artwork_queue)

        self.artists = QListWidget()
        self._configure_grid(self.artists, icon=196, cell_w=228, cell_h=300, icon_h=260)
        self.artists.itemActivated.connect(self.open_artist)
        self.artists.setContextMenuPolicy(Qt.CustomContextMenu)
        self.artists.customContextMenuRequested.connect(self._artists_context_menu)

        self.albums = QListWidget()
        self._configure_grid(self.albums, icon=220, cell_w=250, cell_h=340, icon_h=300)
        self.albums.itemDoubleClicked.connect(self.open_album)
        self.albums.setContextMenuPolicy(Qt.CustomContextMenu)
        self.albums.customContextMenuRequested.connect(self._albums_context_menu)
        self.albums.itemSelectionChanged.connect(self._on_album_selection_changed)

        self.cmb_artist_sort = QComboBox()
        self.cmb_artist_sort.setMinimumWidth(210)
        self.cmb_artist_sort.setToolTip("Choose how artist cards are sorted")
        self.cmb_artist_sort.setStyleSheet("QComboBox { padding: 4px 10px; min-height: 30px; }")
        for label, value in self.ARTIST_SORTS:
            self.cmb_artist_sort.addItem(label, value)
        self.cmb_artist_sort.currentIndexChanged.connect(self._on_artist_sort_changed)

        self.cmb_series_sort = QComboBox()
        self.cmb_series_sort.setMinimumWidth(220)
        self.cmb_series_sort.setToolTip("Choose how series cards are sorted")
        self.cmb_series_sort.setStyleSheet("QComboBox { padding: 4px 10px; min-height: 30px; }")
        for label, value in self.SERIES_SORTS:
            self.cmb_series_sort.addItem(label, value)
        self.cmb_series_sort.currentIndexChanged.connect(self._on_series_sort_changed)

        self.btn_set_album_total = QPushButton("Set Album Total\u2026")
        self.btn_set_album_total.setToolTip("Set expected total tracks for the selected album")
        self.btn_set_album_total.setEnabled(False)
        self.btn_set_album_total.clicked.connect(self.set_expected_for_selected_album)

        self.tracks = QListWidget()
        self._configure_grid(
            self.tracks,
            icon=216,
            cell_w=246,
            cell_h=340,
            icon_h=300,
        )
        self.tracks.itemSelectionChanged.connect(
            self._on_book_selection_changed
        )
        self.tracks.itemDoubleClicked.connect(self._open_book_item)
        self.tracks.verticalScrollBar().valueChanged.connect(self._schedule_visible_books)

        self.tracks_progress_label = QLabel("")
        self.tracks_progress_label.setAlignment(Qt.AlignCenter)

        self.editor_widget = self._build_track_editor()
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QScrollArea.NoFrame)
        self.editor_scroll.setWidget(self.editor_widget)

        self.tracks_page = QWidget()
        tracks_layout = QVBoxLayout(self.tracks_page)
        tracks_layout.setContentsMargins(12, 10, 12, 12)
        tracks_layout.setSpacing(8)
        tracks_layout.addWidget(self.tracks_progress_label)
        tracks_layout.addWidget(self.tracks, 1)

        self.book_details_widget = self._build_book_details_view()
        self.book_details_scroll = QScrollArea()
        self.book_details_scroll.setWidgetResizable(True)
        self.book_details_scroll.setFrameShape(QScrollArea.NoFrame)
        self.book_details_scroll.setWidget(self.book_details_widget)

        self.book_mode_stack = QStackedWidget()
        self.book_mode_stack.addWidget(self.book_details_scroll)
        self.book_mode_stack.addWidget(self.editor_scroll)

        self.book_page = QWidget()
        book_page_layout = QVBoxLayout(self.book_page)
        book_page_layout.setContentsMargins(0, 0, 0, 0)
        book_page_layout.addWidget(self.book_mode_stack)

        self.artists_page = QWidget()
        artists_layout = QVBoxLayout(self.artists_page)
        artists_layout.setContentsMargins(18, 14, 18, 18)
        artists_layout.setSpacing(8)

        artist_sort_row = QHBoxLayout()
        artist_sort_row.setContentsMargins(0, 0, 0, 0)
        artist_sort_row.addStretch(1)
        self.artist_sort_title = QLabel("Writer sort:")
        self.artist_sort_title.setStyleSheet("font-weight: 600;")
        artist_sort_row.addWidget(self.artist_sort_title)
        artist_sort_row.addSpacing(8)
        artist_sort_row.addWidget(self.cmb_artist_sort)

        artists_layout.addLayout(artist_sort_row)

        artist_content_row = QHBoxLayout()
        artist_content_row.setContentsMargins(0, 0, 0, 0)
        artist_content_row.setSpacing(6)
        artist_content_row.addWidget(self.artists, 1)

        self.artist_index_widget = QWidget()
        self.artist_index_widget.setFixedWidth(34)
        self.artist_index_widget.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Expanding,
        )
        self.artist_index_layout = QVBoxLayout(self.artist_index_widget)
        self.artist_index_layout.setContentsMargins(2, 0, 2, 0)
        self.artist_index_layout.setSpacing(0)
        self.artist_index_widget.setVisible(False)
        artist_content_row.addWidget(self.artist_index_widget)

        artists_layout.addLayout(artist_content_row, 1)

        self.albums_page = QWidget()
        albums_layout = QVBoxLayout(self.albums_page)
        albums_layout.setContentsMargins(18, 14, 18, 18)
        albums_layout.setSpacing(8)

        series_sort_row = QHBoxLayout()
        series_sort_row.setContentsMargins(0, 0, 0, 0)
        series_sort_row.addStretch(1)
        series_sort_row.addWidget(self.btn_set_album_total)
        series_sort_row.addSpacing(14)
        self.series_sort_title = QLabel("Series Sort:")
        self.series_sort_title.setStyleSheet("font-weight: 600;")
        series_sort_row.addWidget(self.series_sort_title)
        series_sort_row.addSpacing(8)
        series_sort_row.addWidget(self.cmb_series_sort)

        albums_layout.addLayout(series_sort_row)
        albums_layout.addWidget(self.albums, 1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.artists_page)
        self.stack.addWidget(self.albums_page)
        self.stack.addWidget(self.tracks_page)
        self.stack.addWidget(self.book_page)
        self.setCentralWidget(self.stack)

        settings_menu = self.menuBar().addMenu("&Settings")

        self.act_open_settings = settings_menu.addAction("Open Settings\u2026")
        self.act_open_settings.setShortcut("Ctrl+,")
        self.act_open_settings.triggered.connect(self.open_settings_dialog)
        self.act_diagnostics = settings_menu.addAction("Diagnostics…")
        self.act_diagnostics.triggered.connect(lambda: show_diagnostics_dialog(self))

        settings_menu.addSeparator()

        self.act_rescan_menu = settings_menu.addAction("Rescan Current Library")
        self.act_rescan_menu.triggered.connect(self._rescan_or_cancel)

        tb = QToolBar("Main")
        self.main_toolbar = tb
        tb.setMovable(False)
        self.addToolBar(tb)

        self.library_switcher = self._add_library_switcher(tb)
        tb.addSeparator()

        self.act_back = tb.addAction("Back")
        self.act_back.triggered.connect(self.go_back)
        self.act_back.setEnabled(False)

        self.act_edit_book = tb.addAction("Edit Metadata")
        self.act_edit_book.setShortcut("Ctrl+E")
        self.act_edit_book.setToolTip("Edit metadata for the open audiobook")
        self.act_edit_book.triggered.connect(self.show_book_editor)
        self.act_edit_book.setEnabled(False)
        self.act_edit_book.setVisible(False)

        self.act_rescan = tb.addAction("Rescan")
        self.act_rescan.triggered.connect(self._rescan_or_cancel)

        self.act_statistics = tb.addAction("Statistics")
        self.act_statistics.setShortcut("Ctrl+Shift+S")
        self.act_statistics.setToolTip("Listening history, ratings and collection statistics")
        self.act_statistics.setCheckable(True)
        self.act_statistics.triggered.connect(self.show_statistics)
        self.act_find_books = tb.addAction('Find books')
        self.act_find_books.setShortcut('Ctrl+F')
        self.act_find_books.setToolTip('Search this library by title, genre, release year or purchase source')
        self.act_find_books.triggered.connect(self.find_books)

        tb.addSeparator()

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.breadcrumb = QLabel("")
        self.breadcrumb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tb.addWidget(self.breadcrumb)

        self.setStatusBar(QStatusBar())
        self.scan_bar = QProgressBar()
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setValue(0)
        self.scan_bar.setFixedWidth(220)
        self.scan_bar.hide()
        self.statusBar().addPermanentWidget(self.scan_bar)

        self.status("Loading library data...")
        self.centralWidget().setEnabled(False)
        self.menuBar().setEnabled(False)
        for toolbar in self.findChildren(QToolBar):
            toolbar.setEnabled(False)
        QTimer.singleShot(0, self._initialize_storage)

    def show_statistics(self):
        if self._file_job is not None or self._closing or self._pending_library_change is not None:
            return
        if self.statistics_page is None:
            began = time.perf_counter()
            diag.record('statistics.open', 'start')
            # Import and construct only when requested, keeping startup light.
            from .statistics_page import StatisticsPage
            self.statistics_page = StatisticsPage(self)
            self.statistics_page.speed.setValue(self.library_settings_store.preferences()['playback_speed'])
            self.statistics_page.speed.valueChanged.connect(self._save_listening_speed)
            self.stack.addWidget(self.statistics_page)
            self.statistics_page.refresh_requested.connect(self._refresh_statistics)
            self.statistics_page.open_book_requested.connect(self._open_statistics_book)
            self.statistics_page.remove_book_requested.connect(self._remove_statistics_book)
            self.statistics_page.artwork_idle.connect(self._artwork_idle)
            self._build_statistics_navigation()
            diag.record('statistics.open', 'ok', ms=diag.elapsed_ms(began))
        if self.stack.currentIndex() != self.PAGE_STATISTICS:
            if not self._book_return_to_statistics:
                self._statistics_return_page = self.stack.currentIndex()
        self._go_to(self.PAGE_STATISTICS)
        self._refresh_statistics()

    def find_books(self):
        if self._file_job is not None or self._closing or self._pending_library_change is not None:
            return
        self.show_statistics()
        self.statistics_page.show_collection_browser()

    def _build_statistics_navigation(self):
        page = self.statistics_page
        toolbar = QToolBar('Statistics', self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.statistics_toolbar = toolbar
        back = toolbar.addAction('Back')
        back.triggered.connect(self.go_back)
        toolbar.addSeparator()
        self.statistics_library_switcher = self._add_library_switcher(toolbar)
        self._refresh_library_switchers()
        toolbar.addSeparator()
        group = QActionGroup(toolbar)
        group.setExclusive(True)
        self.statistics_actions = []
        for index, title in enumerate(('Listening', 'History', 'Collection', 'Year recap')):
            action = toolbar.addAction(title)
            action.setCheckable(True)
            action.setActionGroup(group)
            action.triggered.connect(lambda checked=False, value=index: page.tabs.setCurrentIndex(value))
            self.statistics_actions.append(action)
        toolbar.addAction(self.act_find_books)
        page.tabs.currentChanged.connect(self._sync_statistics_navigation)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        page.period.setMinimumWidth(145)
        toolbar.addWidget(page.period)
        toolbar.addWidget(page.refresh_button)
        page.use_external_navigation()
        self._sync_statistics_navigation(page.tabs.currentIndex())

    def _sync_statistics_navigation(self, index):
        self.statistics_actions[index].setChecked(True)

    def _remove_statistics_book(self, row):
        if self._file_job is not None or self._closing:
            return
        if self._scan_thread is not None:
            self.status('Wait for the scan to finish before removing a book.')
            return
        if self._book_editing:
            self._go_to(self.PAGE_BOOK)
            self.status('Finish or cancel your metadata edit before removing a book.')
            return
        from .book_removal import prepare_removal
        store = self.listening_history_store
        self._run_file_job('Remove book', 'Checking saved book data...',
                           lambda progress: prepare_removal(store, row), self._confirm_book_removal, operation='book.remove.check')

    def _confirm_book_removal(self, target, error):
        if self._closing:
            return
        if error is not None:
            QMessageBox.warning(self, 'Could not prepare removal', str(error))
            return
        from .book_removal import RemoveBookDialog, remove_book
        dialog = RemoveBookDialog(target, self)
        accepted = dialog.exec() == QDialog.Accepted
        delete_file = dialog.delete_file.isChecked()
        dialog.deleteLater()
        if not accepted:
            self.status('Removal cancelled.')
            return
        store = self.listening_history_store
        self._run_file_job('Removing book', 'Removing saved book data...',
                           lambda progress: remove_book(store, target, delete_file, progress),
                           self._on_book_removed, operation='book.remove')

    def _on_book_removed(self, data, error):
        if error is not None:
            QMessageBox.warning(self, 'Book removal failed', str(error))
            self.status('Book removal failed.')
            return
        self._statistics_return_page = self.PAGE_ARTISTS
        self._book_return_to_statistics = False
        self._book_track_id = None
        self._reload_completed_history_paths()
        self._refresh_statistics()
        if data['file_deleted'] and not self._closing:
            self.start_scan()
        else:
            self.load_artists()
        self.status('Saved book data removed.' + (' Audiobook file moved to Trash.' if data['file_deleted'] else ''))

    def _refresh_statistics(self):
        if self.statistics_page is None or self._closing or self._pending_library_change is not None:
            return
        self._statistics_request += 1
        self.statistics_page.set_loading()
        if self._statistics_job is not None:
            self._statistics_pending = True
            return
        self._start_statistics_job()

    def _start_statistics_job(self):
        from .statistics_data import build_statistics, scan_snapshot, probe_files
        history = self.listening_history_store
        if history is None:
            self.statistics_page.show_error("The listening database is not available yet.")
            return
        year = self.statistics_page.selected_year()
        records = ({path: replace(record, metadata=replace(record.metadata))
                    for path, record in self._track_records.items()}
                   if self._cache_ready else None)
        expected = dict(self.expected_store.data) if self.expected_store is not None else {}
        author_count = len(self._artist_groups_cache)
        profile = self.library_settings_store.profile()

        def calculate(progress):
            collection = scan_snapshot(records, expected, author_count, profile=profile) if records is not None else None
            snapshot = history.statistics_snapshot()
            paths = set((collection or {}).get('paths', ()))
            paths.update(str(book.get('last_path') or '') for book in snapshot['books'])
            # Only file status is checked here. Metadata/artwork is still cached
            # and every disk operation stays on this worker thread.
            files = probe_files(paths)
            return build_statistics(snapshot, year, collection, files=files)

        job = FileTask(calculate, self, operation='statistics.read', details={'books': len(records or {})})
        job.request_id = self._statistics_request
        self._statistics_job = job
        job.finished.connect(self._on_statistics_finished, Qt.QueuedConnection)
        job.start()

    @Slot()
    def _on_statistics_finished(self):
        job = self._statistics_job
        if job is None:
            return
        self._statistics_job = None
        job.deleteLater()
        if self._closing:
            self._statistics_pending = False
            self.close()
            return
        if self._pending_library_change is not None:
            self._statistics_pending = False
            self._finish_pending_library_change()
            return
        if self._statistics_pending or job.request_id != self._statistics_request:
            self._statistics_pending = False
            self._start_statistics_job()
            return
        if job.error is not None:
            self.statistics_page.show_error(str(job.error))
        else:
            began = time.perf_counter()
            diag.record('statistics.render', 'start')
            self.statistics_page.show_result(job.result)
            diag.record('statistics.render', 'ok', ms=diag.elapsed_ms(began), listens=len(job.result.listens))

    def _open_statistics_book(self, path):
        if self._file_job is not None or self._closing:
            return
        if self._book_editing:
            self._go_to(self.PAGE_BOOK)
            self.status("Finish or cancel your current metadata edit before opening another book.")
            return
        if path:
            self.open_book_details(Path(path), from_statistics=True)

    def _artwork_idle(self):
        if self._closing:
            self.close()
        elif self._pending_library_change is not None:
            self._finish_pending_library_change()

    def _run_file_job(self, title, message, work, callback, *, operation, details=None):
        if self._file_job is not None or self._closing or self._pending_library_change is not None:
            return
        self._artwork_timer.stop()
        self._series_previews.set_active(False)
        self._file_job_callback = callback
        self._busy_dialog = BusyDialog(title, message, self)
        self._busy_dialog.show()
        self.status(message.split("\n")[0])
        job = FileTask(work, self, operation=operation, details=details)
        self._file_job = job
        job.progress.connect(self._on_file_job_progress, Qt.QueuedConnection)
        job.finished.connect(self._on_file_job_finished, Qt.QueuedConnection)
        job.start()

    @Slot(str)
    def _on_file_job_progress(self, message):
        if self._busy_dialog is not None:
            self._busy_dialog.message.setText(message)

    @Slot()
    def _on_file_job_finished(self):
        job = self._file_job
        if job is None:
            return
        callback = self._file_job_callback
        dialog = self._busy_dialog
        self._file_job = None
        self._file_job_callback = None
        self._busy_dialog = None
        if dialog is not None and job.error is not None:
            dialog.hide()
        try:
            if callback is not None:
                callback(job.result, job.error)
        except Exception as exc:
            dbg_exc("Could not display the completed file operation")
            QMessageBox.critical(self, "Could not refresh the window", str(exc))
        finally:
            if dialog is not None:
                dialog.hide()
                dialog.deleteLater()
            job.deleteLater()
            if self._closing:
                self.close()
            elif self._artwork_queue and self._file_job is None and self.stack.currentIndex() != self.PAGE_STATISTICS:
                self._artwork_timer.start()
            self._sync_series_previews()

    def _initialize_storage(self):
        def work(progress):
            progress("Loading saved library data...")
            settings = LibrarySettingsStore()
            if settings.needs_default_root:
                settings.set_library_roots([self._default_library_root()], 'main')
            if settings.migrated_legacy:
                try:
                    speed = float(QSettings('KibaSnowpaw', 'AudiobookShelfManager').value('statistics/playback_speed', 2.0))
                    if math.isfinite(speed) and .5 <= speed <= 4:
                        settings.set_preference('playback_speed', speed)
                except (TypeError, ValueError):
                    pass
            settings.set_active_library(settings.startup_library_id())
            return _load_library_storage(settings, progress)
        self._run_file_job("Loading library", "Loading saved library data...", work,
                           self._on_storage_initialized, operation='library.open')

    def _on_storage_initialized(self, data, error):
        if error is not None:
            QMessageBox.critical(self, "Could not load library data", str(error))
            self._closing = True
            return
        (self.expected_store, self.review_store, self.library_settings_store,
         self.scan_index_store, self.listening_history_store, latest, counts,
         ratings, reviewed, history_error) = data
        self._last_listened_history_by_path = {p: value[0] for p, value in latest.items()}
        self._listening_days_history_by_path = {p: value[1] for p, value in latest.items()}
        self._completed_history_paths = set(latest)
        self._completed_listen_count_by_path = counts
        self._book_rating_by_path = ratings
        self._reviewed_paths = reviewed
        self.centralWidget().setEnabled(True)
        self.menuBar().setEnabled(True)
        for toolbar in self.findChildren(QToolBar):
            toolbar.setEnabled(True)
        if self._closing:
            return
        self._refresh_library_switchers()
        self._apply_preferences()
        self._restore_library_roots()
        if history_error:
            QMessageBox.warning(self, "Listening history unavailable", history_error)

    def _accept_book_data(self, data):
        path = data["path"]
        record = data["record"]
        key = str(path)
        previous = self._track_records.get(key)
        self._book_purchase = data.get('purchase', ('', ''))
        self._track_records[str(path)] = record
        self.review_store.prime_track_cache(path, goodreads=record.goodreads, audible=record.audible)
        self._book_cover_path = path
        self._book_cover_image = data["cover_image"]
        self._invalidate_embedded_artwork(path)
        blob = data["embedded"]
        # When a verified sidecar supplied the preview, embedded art was not read.
        # Leave the entry absent so a later embedded-only request can load it.
        if data["embedded_read"] and (blob is None or len(blob) <= self.EMBEDDED_CACHE_MAX_BYTES):
            self._embedded_cache[str(path)] = ((record.mtime_ns, record.size), blob)
            self._embedded_cache_bytes += len(blob) if blob else 0
            while (len(self._embedded_cache) > self.EMBEDDED_CACHE_MAX_ENTRIES
                   or self._embedded_cache_bytes > self.EMBEDDED_CACHE_MAX_BYTES):
                self._remove_embedded_cache_entry(next(iter(self._embedded_cache)))
        self._invalidate_cover_lookups_for_folder(path.parent)
        item = self._book_item_by_path.get(key)
        if item is not None:
            prepared = self._series_previews.preview(path)
            changed = ((prepared is not None and prepared.signature != record.cover_signature)
                       or (previous is not None and previous.cover_signature != record.cover_signature))
            if changed:
                self._reset_series_previews()
                item.setData(self.BOOK_COVER_ROLE, None)
            if not data.get("preview_cached") and not self._book_cover_image.isNull():
                image = self._book_cover_image.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                item.setData(self.BOOK_COVER_ROLE, QIcon(QPixmap.fromImage(image)))
        if not data["history_error"]:
            if data.get('reviewed'):
                self._reviewed_paths.add(key)
            else:
                self._reviewed_paths.discard(key)
            count = data["listen_count"]
            self._completed_listen_count_by_path[key] = count
            if count:
                self._completed_history_paths.add(key)
                self._last_listened_history_by_path[key] = data["last_listened"]
                self._listening_days_history_by_path[key] = data["listening_days"]
            else:
                self._completed_history_paths.discard(key)
                self._last_listened_history_by_path.pop(key, None)
                self._listening_days_history_by_path.pop(key, None)
            rating = data["preferences"][0]
            if rating:
                self._book_rating_by_path[key] = rating
            else:
                self._book_rating_by_path.pop(key, None)

    def _queue_book_card_updates(self, paths):
        self._book_artwork_generation += 1
        generation = self._book_artwork_generation
        # Draw titles, ratings and status for this series ahead of hidden parent cards.
        parents = (job for job in self._artwork_queue if job[0] != "book")
        updated = deque(("book", generation, str(path)) for path in paths)
        updated.extend(parents)
        self._artwork_queue = updated
        if updated and self._file_job is None:
            self._artwork_timer.start()

    def _reset_series_previews(self):
        self._series_previews.reset(((key, self._track_records.get(key)) for key in self._book_item_by_path),
                                    self._artist_folders_by_key_cache.get(self.current_artist_key, ()))

    def _sync_series_previews(self):
        active = (self.stack.currentIndex() == self.PAGE_TRACKS and self._file_job is None
                  and self._scan_thread is None and not self._closing and self._pending_library_change is None)
        self._series_previews.set_active(active)
        if active:
            self._prioritize_visible_books()

    def _schedule_visible_books(self, *_):
        if self._series_previews.active and self._series_previews.pending and not self._book_viewport_timer.isActive():
            self._book_viewport_timer.start(16)

    def _prioritize_visible_books(self):
        if not self._series_previews.active or not self._series_previews.pending:
            return
        area = self.tracks.viewport().rect()
        self._series_previews.prioritize(key for key, item in self._book_item_by_path.items()
                                         if self.tracks.visualItemRect(item).intersects(area))

    def _on_series_cover(self, key, image):
        item = self._book_item_by_path.get(key)
        if item is not None:
            if not image.isNull():
                item.setData(self.BOOK_COVER_ROLE, QIcon(QPixmap.fromImage(image)))
            self._update_single_book_item(Path(key))

    def status(self, msg: str) -> None:
        platform = os.environ.get("QT_QPA_PLATFORM", "auto")
        self.statusBar().showMessage(f"{msg} (Qt: {platform})")

    def _default_library_root(self) -> Path:
        default = Path.home() / "Audiobook"
        default.mkdir(parents=True, exist_ok=True)
        return default

    def _normalize_library_roots(self, roots: list[Path]) -> list[Path]:
        out: list[Path] = []
        seen: set[str] = set()

        for root in roots:
            try:
                p = root.expanduser().resolve()
            except Exception:
                p = root.expanduser()

            if not p.exists() or not p.is_dir():
                continue

            key = str(p)
            if key in seen:
                continue

            seen.add(key)
            out.append(p)

        return out

    def _reset_navigation_state(self) -> None:
        self._cancel_artwork_updates()
        self.current_artist_key = None
        self.current_artist_dir = None
        self.current_album_dir = None
        self._selected_track_path = None
        self._book_track_id = None
        self._book_title_text = ""
        self._book_editing = False
        self._book_rating = 0
        self._book_favorite = False
        self._listening_events_by_id = {}
        self._album_meta_cache = {}
        self.current_track_count = 0
        self.current_album_expected = 0
        self._artist_groups_cache = {}
        self._artist_names_cache = {}
        self._album_artist_alias_cache = {}
        self._book_item_by_path = {}
        self._clear_artist_index()
        self.artist_index_widget.setVisible(False)
        self.album_progress_label.setText("")
        self.tracks_progress_label.setText("")
        self.book_mode_stack.setCurrentIndex(0)
        self.cover_label.setPixmap(QPixmap())
        self.cover_label.setText("Click to add cover")
        self.book_detail_cover_label.setPixmap(QPixmap())
        self.book_detail_cover_label.setText("No cover")
        self.book_detail_cover_label.setStyleSheet(
            "QLabel { background: #111; border: 1px solid #343a40; "
            "border-radius: 6px; }"
        )
        self.book_detail_title_label.setText("Select an audiobook")
        self.book_detail_author_heading.setText("")
        self._refresh_book_tag_chips("", validate=False)
        self.book_album_warning_label.clear()
        self.book_album_warning_label.hide()
        self.book_detail_series_label.setStyleSheet("font-size: 15px;")
        self._refresh_book_preference_controls()
        self.editor_album_warning_widget.hide()
        self.editor_genre_warning_widget.hide()
        self.editor_cover_warning_widget.hide()
        self.cover_label.setStyleSheet(
            "QLabel { background: #111; border: 1px solid #333; }"
        )
        self.book_history_table.setRowCount(0)
        self.btn_add_completed_listen.setEnabled(False)
        self.btn_edit_listen.setEnabled(False)
        self.btn_delete_listen.setEnabled(False)
        self.tracks.clear()

    def _library_summary_text(self) -> str:
        count = len(self.library_roots)
        name = self.library_settings_store.profile()['name'] if self.library_settings_store else 'Main'
        if count <= 0:
            return name + ' | No folders selected'
        if count == 1:
            if self.library_roots[0].name.casefold() == name.casefold():
                return f"{name} | 1 folder"
            return f"{name} | {self.library_roots[0].name}"
        return f"{name} | {count} folders"

    def _update_breadcrumb(self) -> None:
        if self.stack.currentIndex() == self.PAGE_STATISTICS:
            self.breadcrumb.setText("Statistics")
            return
        parts = [self._library_summary_text()]

        if self.current_artist_key:
            parts.append(self._artist_display_name(self.current_artist_key))

        if self.current_album_dir:
            parts.append(self.current_album_dir.name)

        if (
            self.stack.currentIndex() == self.PAGE_BOOK
            and self._selected_track_path is not None
        ):
            parts.append(
                self._book_title_text
                or self._selected_track_path.stem
            )

            if self._book_editing:
                parts.append("Edit")

        self.breadcrumb.setText("  /  ".join(parts))

    @debug_call()
    def _restore_library_roots(self) -> None:
        roots = self.library_settings_store.library_roots()
        self.library_roots = self._normalize_library_roots(roots)
        self.library_root = self.library_roots[0] if self.library_roots else None
        self._reset_navigation_state()
        self._go_to(self.PAGE_ARTISTS)
        self.start_scan()

    def _all_artist_dirs(self) -> list[Path]:
        out: list[Path] = []
        seen: set[str] = set()

        for root in self.library_roots:
            for artist_dir in list_artist_dirs(root):
                key = str(artist_dir)
                if key in seen:
                    continue
                seen.add(key)
                out.append(artist_dir)

        return out

    def _apply_library_roots(self, roots: list[Path], *, save: bool, rescan: bool) -> None:
        normalized = self._normalize_library_roots(roots)

        self.library_roots = normalized
        self.library_root = normalized[0] if normalized else None

        if save:
            self.library_settings_store.set_library_roots(normalized)

        self._reset_navigation_state()
        self._update_breadcrumb()

        if rescan:
            self._go_to(self.PAGE_ARTISTS)
            if self._scan_worker is not None:
                self._scan_pending = True
                self._scan_worker.cancel()
                self._set_scan_action_state(running=True, enabled=False)
                self.status("Library folders changed. Restarting scan\u2026")
            else:
                self.start_scan()

    def _add_library_switcher(self, toolbar):
        combo = QComboBox()
        combo.setMinimumWidth(140)
        combo.setMaximumWidth(220)
        combo.setAccessibleName('Active audiobook library')
        combo.setToolTip('Switch library. Add or rename libraries in Settings.')
        combo.currentIndexChanged.connect(lambda _index: self.switch_library(combo.currentData()))
        toolbar.addWidget(combo)
        self._library_switchers.append(combo)
        return combo

    def _refresh_library_switchers(self):
        settings = self.library_settings_store
        if settings is None:
            return
        for combo in self._library_switchers:
            with signal_blocker(combo):
                combo.clear()
                for profile in settings.libraries():
                    combo.addItem(profile['name'], profile['id'])
                combo.setCurrentIndex(combo.findData(settings.active_library_id()))
                combo.setEnabled(self._pending_library_change is None)
        self.setWindowTitle('Audiobook Shelf Manager - ' + settings.profile()['name'])
        self._update_breadcrumb()

    def _gallery_options(self, series=None):
        if self.library_settings_store is None:
            return effective_options()
        return self.library_settings_store.options(series)

    def _review_card_lines(self, paths, denominator, options):
        lines = []
        for key, label in (('show_goodreads', 'Goodreads'), ('show_audible', 'Audible'),
                           ('show_reviewed', 'Reviewed')):
            if not options[key]:
                continue
            if key == 'show_reviewed':
                count = sum(str(path) in self._reviewed_paths for path in paths)
            else:
                # The scan/load primes these caches. Never reopen audio files while painting cards.
                kind = 'goodreads' if key == 'show_goodreads' else 'audible'
                count = sum(self.review_store.cached_done(path, kind) for path in paths)
            text = f'{label} {count}/{denominator}'
            lines.append((text, status_color(count, denominator)))
        return lines

    def _series_card_lines(self, album):
        options = self._gallery_options(album)
        paths = self._album_mp3s(album)
        total = self._album_denominator(album, paths)
        lines = []
        if options['show_owned']:
            lines.append((f'Owned {len(paths)}/{total}', status_color(len(paths), total)))
        if options['show_completed']:
            completed = sum(self._is_book_completed(path) for path in paths)
            lines.append((f'Completed {completed}/{total}', status_color(completed, total)))
        return lines + self._review_card_lines(paths, total, options)

    def _book_card_lines(self, path):
        options = self._gallery_options(path.parent)
        lines = []
        if options['show_owned']:
            lines.append(('Owned', GREEN))
        if options['show_completed']:
            lines.append((self._book_completion_text(path), GREEN if self._is_book_completed(path) else MUTED))
            if options['show_listening_dates']:
                finished = self._book_last_listened(path)
                days = _format_listening_days(self._book_listening_days(path))
                if finished:
                    lines.append(('Last listened ' + finished, MUTED))
                if days:
                    lines.append(('Took ' + days, MUTED))
        for text, color in self._review_card_lines([path], 1, options):
            label, count = text.rsplit(' ', 1)
            lines.append((label + (' reviewed' if label != 'Reviewed' else '') + (': Yes' if count == '1/1' else ': No'), color))
        return lines

    def _size_gallery_cards(self, album=None):
        options = self._gallery_options()
        def row_count(values):
            return sum(values[key] for key in ('show_owned', 'show_completed', 'show_goodreads', 'show_audible', 'show_reviewed'))
        series_options = [self._gallery_options(Path(path)) for path in self._mp3s_by_album_cache]
        album_options = self._gallery_options(album or self.current_album_dir)
        book_rows = row_count(album_options) + (2 if album_options['show_completed'] and album_options['show_listening_dates'] else 0)
        for widget, rows in ((self.artists, row_count(options)),
                             (self.albums, max([row_count(options)] + [row_count(value) for value in series_options])),
                             (self.tracks, book_rows)):
            height = card_height(rows)
            icon_size, grid_size = QSize(216, height), QSize(246, height + 44)
            if widget.iconSize() == icon_size and widget.gridSize() == grid_size:
                continue
            widget.setIconSize(icon_size)
            widget.setGridSize(grid_size)
            for index in range(widget.count()):
                widget.item(index).setSizeHint(QSize(230, height + 36))
            widget.doItemsLayout()

    def _refresh_gallery_views(self):
        self._size_gallery_cards()
        if self.stack.currentIndex() == self.PAGE_ARTISTS:
            self.load_artists()
        else:
            self._queue_artist_card_updates(list(self._artist_item_by_dir))
        if self.stack.currentIndex() == self.PAGE_ALBUMS and self.current_artist_key:
            self.load_albums(self.current_artist_key)
        else:
            self._queue_album_card_updates([Path(path) for path in self._album_item_by_dir])
        self._queue_book_card_updates([Path(path) for path in self._book_item_by_path])
        self._update_current_book_progress_label()
        if self._selected_track_path is not None:
            record = self._track_records.get(str(self._selected_track_path))
            if record is not None:
                self._refresh_book_tag_chips(record.metadata.genre)
                self._refresh_book_album_warning(self._selected_track_path, record.metadata)
                self._refresh_editor_genre_warning()
            self._apply_book_display_options()
        if self.statistics_page is not None:
            self._refresh_statistics()

    def _apply_book_display_options(self):
        options = self._gallery_options(self._selected_track_path.parent if self._selected_track_path else None)
        for key, editor, value in (('show_goodreads', self.chk_goodreads, self.book_detail_goodreads_label),
                                    ('show_audible', self.chk_audible, self.book_detail_audible_label),
                                    ('show_reviewed', self.chk_reviewed, self.book_detail_reviewed_label)):
            editor.setVisible(options[key])
            value.setVisible(options[key])
            value._caption.setVisible(options[key])
        for value in (self.book_detail_status_label, self.book_detail_listens_label,
                      self.book_detail_last_listened_label, self.book_detail_listening_days_label):
            visible = options['show_completed']
            if value in (self.book_detail_last_listened_label, self.book_detail_listening_days_label):
                visible = visible and options['show_listening_dates']
            value.setVisible(visible)
            value._caption.setVisible(visible)
        self.book_status_box.setVisible(any(options[key] for key in ('show_completed', 'show_goodreads', 'show_audible', 'show_reviewed')))

    def _reviewed_toggled(self, checked):
        if self._book_track_id is None or self._selected_track_path is None:
            return
        path, track_id, store = self._selected_track_path, self._book_track_id, self.listening_history_store
        def done(_result, error):
            if error is not None:
                with signal_blocker(self.chk_reviewed):
                    self.chk_reviewed.setChecked(str(path) in self._reviewed_paths)
                QMessageBox.warning(self, 'Could not save review', str(error))
                return
            if checked:
                self._reviewed_paths.add(str(path))
            else:
                self._reviewed_paths.discard(str(path))
            self._refresh_book_detail_status()
            self._update_after_flag_change()
            self.status('Review marker saved.')
        self._run_file_job('Saving review', 'Saving review marker...',
                           lambda progress: store.set_reviewed(track_id, checked), done, operation='book.review')

    def _apply_preferences(self):
        settings = self.library_settings_store
        preferences = settings.preferences()
        self.chk_autorename.setChecked(preferences['auto_rename'])
        self.chk_renamesidecar.setChecked(preferences['rename_sidecar'])
        modes = settings.profile()['sort'] if preferences['remember_sort'] else {}
        for key, combo in (('writers', self.cmb_artist_sort), ('series', self.cmb_series_sort)):
            with signal_blocker(combo):
                combo.setCurrentIndex(max(0, combo.findData(modes.get(key, 'az'))))
        if self.statistics_page is not None:
            with signal_blocker(self.statistics_page.speed):
                self.statistics_page.speed.setValue(preferences['playback_speed'])
            self.statistics_page._update_estimate()

    def _save_listening_speed(self, value):
        if self.library_settings_store is not None:
            try:
                self.library_settings_store.set_preference('playback_speed', value)
            except (OSError, ValueError) as exc:
                self.status('Could not remember listening speed: ' + str(exc))

    def _remember_sort(self, category, mode):
        settings = self.library_settings_store
        if settings is not None and settings.preferences()['remember_sort']:
            try:
                settings.set_sort(category, mode)
            except (OSError, ValueError) as exc:
                self.status('Could not remember sorting: ' + str(exc))

    def switch_library(self, ident):
        settings = self.library_settings_store
        if settings is None or ident is None or ident == settings.active_library_id():
            return False
        self._refresh_library_switchers()
        try:
            candidate = settings.clone()
            candidate.set_active_library(ident, save=False)
            return self._queue_library_change(candidate)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Could not switch library', str(exc))
            return False

    def _queue_library_change(self, candidate, action=None):
        if self._closing or self._file_job is not None or self._pending_library_change is not None:
            return False
        if self._book_editing:
            self._go_to(self.PAGE_BOOK)
            self.status('Save or cancel your metadata edit before switching libraries.')
            return False
        previous = self.library_settings_store
        reload_library = (candidate.active_library_id() != previous.active_library_id()
                          or candidate.library_roots() != previous.library_roots())
        if not reload_library:
            self._commit_library_change(candidate, False, action)
            return True
        self._pending_library_change = (candidate, action)
        self._series_previews.set_active(False)
        self._series_previews.reset()
        self.centralWidget().setEnabled(False)
        self._refresh_library_switchers()
        self._statistics_request += 1
        self._statistics_pending = False
        self._scan_pending = False
        if self.statistics_page is not None:
            self.statistics_page.artwork.pause()
        if self._scan_worker is not None:
            self._scan_worker.cancel()
            self._set_scan_action_state(running=True, enabled=False)
        self.status('Switching to ' + candidate.profile()['name'] + '...')
        self._finish_pending_library_change()
        return True

    def _finish_pending_library_change(self):
        if self._pending_library_change is None or self._closing:
            return
        if self._scan_thread is not None or self._statistics_job is not None or self._file_job is not None:
            return
        if self._series_previews.job is not None or (self.statistics_page is not None and self.statistics_page.artwork.busy):
            return
        candidate, action = self._pending_library_change
        self._pending_library_change = None
        self._commit_library_change(candidate, True, action)

    def _commit_library_change(self, candidate, reload_library, action):
        def work(progress):
            data = _load_library_storage(candidate, progress, strict=True) if reload_library else None
            candidate.save()
            return data
        self._run_file_job('Library settings', 'Opening ' + candidate.profile()['name'] + '...', work,
            partial(self._on_library_changed, settings=candidate, reload_library=reload_library, action=action),
            operation='library.settings')

    def _discard_library_views(self):
        # No scan, statistics or thumbnail worker remains when this is called.
        if self.statistics_page is not None:
            self.statistics_page.shutdown_artwork()
            self.stack.removeWidget(self.statistics_page)
            self.statistics_page.deleteLater()
            self.statistics_page = None
            self._library_switchers.remove(self.statistics_library_switcher)
            self.removeToolBar(self.statistics_toolbar)
            self.statistics_toolbar.deleteLater()
            del self.statistics_toolbar
            del self.statistics_library_switcher
        self._statistics_return_page = self.PAGE_ARTISTS
        self._book_return_to_statistics = False
        self._clear_pending_cover()
        self._reset_navigation_state()
        self.artists.clear()
        self.albums.clear()
        self._artist_dirs_cache = []
        self._albums_by_artist_cache = {}
        self._mp3s_by_album_cache = {}
        self._track_records = {}
        self._artist_item_by_dir = {}
        self._album_item_by_dir = {}
        self._artist_folders_by_key_cache = {}
        self._embedded_cache.clear()
        self._embedded_cache_bytes = 0
        self._path_icon_cache.clear()
        self._decoded_icon_cache.clear()
        self._cover_lookup_cache.clear()
        self._book_cover_image = QImage()
        self._book_cover_path = None
        self._cache_ready = False

    def _on_library_changed(self, data, error, *, settings, reload_library, action):
        self.centralWidget().setEnabled(True)
        if error is not None:
            self._refresh_library_switchers()
            if self.statistics_page is not None and self.stack.currentIndex() == self.PAGE_STATISTICS:
                self.statistics_page.artwork.resume()
            QMessageBox.warning(self, 'Could not open library', str(error))
            self.status('The previous library is still open.')
            if not self._cache_ready and self.library_roots and not self._closing:
                self.start_scan()
            return
        if reload_library:
            self._discard_library_views()
            self._on_storage_initialized(data, None)
        else:
            self.library_settings_store = settings
            self._apply_preferences()
            self._refresh_library_switchers()
            if self._cache_ready:
                self._refresh_gallery_views()
            self.status('Settings saved.')
        if not self._closing and action:
            QTimer.singleShot(0, partial(self._library_backup_action, action))

    def _library_backup_action(self, action):
        store = self.listening_history_store
        if store is None:
            return
        if action == 'open_backups':
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(store.backup_dir()))):
                QMessageBox.information(self, 'Backup folder', str(store.backup_dir()))
            return
        def done(path, error):
            if error is not None:
                QMessageBox.warning(self, 'Backup failed', str(error))
            else:
                QMessageBox.information(self, 'Backup created', str(path))
        self._run_file_job('Backing up library', 'Saving listening history, ratings and favorites...',
                           lambda progress: store.create_backup(label='manual', prune=False), done, operation='library.backup')

    def open_settings_dialog(self, *, series=None) -> None:
        if self.library_settings_store is None or self._file_job is not None or self._pending_library_change is not None:
            return
        if self._book_editing:
            self._go_to(self.PAGE_BOOK)
            self.status('Save or cancel your metadata edit before changing libraries or settings.')
            return
        dlg = SettingsDialog(self.library_settings_store, self,
                             series_paths=self._mp3s_by_album_cache.keys(), selected_series=series)
        if not dlg.exec():
            return
        self._queue_library_change(self.library_settings_store.clone(dlg.values()), dlg.requested_action)

    def _clean_artist_name(self, text: str) -> str:
        text = re.sub(r"\s+", " ", (text or "").strip())
        return text.strip(" ,;|")

    def _artist_key_for_name(self, text: str) -> str:
        return self._clean_artist_name(text).casefold()

    def _split_artist_names(self, raw: str) -> list[str]:
        raw = self._clean_artist_name(raw)
        if not raw:
            return []

        tmp = raw
        tmp = re.sub(r"\s+(?:and|&)\s+", ";", tmp, flags=re.IGNORECASE)
        tmp = tmp.replace(",", ";")
        tmp = tmp.replace("|", ";")

        out: list[str] = []
        seen: set[str] = set()

        for part in tmp.split(";"):
            name = self._clean_artist_name(part)
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)

        return out

    def _album_artist_aliases(self, album_dir: Path) -> list[str]:
        cache_key = str(album_dir)
        cached = self._album_artist_alias_cache.get(cache_key)
        if cached is not None:
            return cached

        aliases: list[str] = []

        try:
            mp3s = self._album_mp3s(album_dir)
            if mp3s:
                meta, _ = self._meta_and_length_for_track(mp3s[0])
                raw = (meta.albumartist or "").strip() or (meta.artist or "").strip()
                aliases = self._split_artist_names(raw)
        except Exception:
            aliases = []

        if not aliases:
            fallback = self._clean_artist_name(album_dir.parent.name or "Unknown")
            aliases = [fallback] if fallback else ["Unknown"]

        self._album_artist_alias_cache[cache_key] = aliases
        return aliases

    def _rebuild_artist_groups(self) -> None:
        groups: dict[str, list[Path]] = {}
        names: dict[str, str] = {}
        all_albums: list[Path] = []
        artist_dirs: list[Path] = []

        if self._cache_ready:
            artist_dirs = list(self._artist_dirs_cache)
            for album_list in self._albums_by_artist_cache.values():
                all_albums.extend(album_list)
        else:
            seen_artist_dirs: set[str] = set()
            for root in self.library_roots:
                for artist_dir in list_artist_dirs(root):
                    artist_dir_key = str(artist_dir)
                    if artist_dir_key in seen_artist_dirs:
                        continue
                    seen_artist_dirs.add(artist_dir_key)
                    artist_dirs.append(artist_dir)
                    all_albums.extend(list_album_dirs(artist_dir))

        folders_by_key: dict[str, list[Path]] = {}
        for artist_dir in artist_dirs:
            folder_key = self._artist_key_for_name(artist_dir.name)
            if folder_key:
                folders_by_key.setdefault(folder_key, []).append(artist_dir)

        seen_albums: set[str] = set()

        for album_dir in all_albums:
            album_key = str(album_dir)
            if album_key in seen_albums:
                continue
            seen_albums.add(album_key)

            aliases = self._album_artist_aliases(album_dir)
            for alias in aliases:
                key = self._artist_key_for_name(alias)
                if not key:
                    continue

                names.setdefault(key, alias)
                groups.setdefault(key, []).append(album_dir)

        for key, albums in list(groups.items()):
            deduped: list[Path] = []
            seen_paths: set[str] = set()
            for album_dir in albums:
                s = str(album_dir)
                if s in seen_paths:
                    continue
                seen_paths.add(s)
                deduped.append(album_dir)
            groups[key] = deduped

        self._artist_groups_cache = groups
        self._artist_names_cache = names
        self._artist_folders_by_key_cache = folders_by_key

    def _artist_display_name(self, artist_key: str) -> str:
        return self._artist_names_cache.get(artist_key, artist_key)

    def _artist_album_dirs(self, artist_key: str) -> list[Path]:
        if not self._artist_groups_cache:
            self._rebuild_artist_groups()
        return list(self._artist_groups_cache.get(artist_key, []))

    def _safe_artist_folder_name(self, artist_name: str) -> str:
        name = re.sub(r"[/\\\x00]", "-", artist_name)
        name = re.sub(r"\s+", " ", name).strip().strip(".")
        if not name or name in {".", ".."}:
            raise ValueError("The artist name cannot be used as a folder name.")
        return name

    def _preferred_library_root_for_artist(self, artist_key: str) -> Path:
        for album_dir in self._artist_album_dirs(artist_key):
            for root in self.library_roots:
                try:
                    album_dir.relative_to(root)
                    return root
                except ValueError:
                    continue

        if self.library_roots:
            return self.library_roots[0]

        raise RuntimeError("No library folder is available.")

    def _artist_folder_for_key(self, artist_key: str, *, create: bool) -> Path | None:
        existing = self._artist_folders_by_key_cache.get(artist_key, [])
        for artist_dir in existing:
            if artist_dir.is_dir():
                return artist_dir

        if not create:
            return None

        # Catch a folder created manually after the last scan before making
        # another directory with a slightly different spelling or case.
        for root in self.library_roots:
            try:
                artist_dirs = list_artist_dirs(root)
            except Exception:
                artist_dirs = []

            for artist_dir in artist_dirs:
                if self._artist_key_for_name(artist_dir.name) != artist_key:
                    continue

                self._artist_folders_by_key_cache.setdefault(artist_key, []).append(artist_dir)
                if artist_dir not in self._artist_dirs_cache:
                    self._artist_dirs_cache.append(artist_dir)
                if self._cache_ready:
                    self._albums_by_artist_cache.setdefault(str(artist_dir), [])
                return artist_dir

        artist_name = self._artist_display_name(artist_key)
        folder_name = self._safe_artist_folder_name(artist_name)
        library_root = self._preferred_library_root_for_artist(artist_key)
        artist_dir = library_root / folder_name
        artist_dir.mkdir(parents=False, exist_ok=True)

        self._artist_folders_by_key_cache.setdefault(artist_key, []).append(artist_dir)

        if artist_dir not in self._artist_dirs_cache:
            self._artist_dirs_cache.append(artist_dir)
        if self._cache_ready:
            self._albums_by_artist_cache.setdefault(str(artist_dir), [])

        return artist_dir

    def _artist_artwork_files(self, artist_dir: Path) -> list[Path]:
        supported = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        try:
            return sorted(
                (
                    path
                    for path in artist_dir.iterdir()
                    if path.is_file()
                    and path.stem.casefold() == "artist"
                    and path.suffix.lower() in supported
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            return []

    def _save_artist_artwork(self, artist_key: str, source: Path) -> Path:
        source = Path(source).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Artwork file does not exist: {source}")

        supported = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        suffix = source.suffix.lower()
        if suffix not in supported:
            raise ValueError("Artwork must be JPG, JPEG, PNG, WEBP, or BMP.")

        artist_dir = self._artist_folder_for_key(artist_key, create=True)
        if artist_dir is None:
            raise RuntimeError("The artist folder could not be created.")

        destination = artist_dir / f"artist{suffix}"
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artist.",
            suffix=".tmp",
            dir=artist_dir,
        )
        temporary = Path(temporary_name)

        try:
            with os.fdopen(file_descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())

            os.replace(temporary, destination)

            for old_artwork in self._artist_artwork_files(artist_dir):
                if old_artwork != destination:
                    old_artwork.unlink(missing_ok=True)

            self._invalidate_artwork_path(destination)
            self._invalidate_cover_lookups_for_folder(artist_dir)
            return destination
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _remove_saved_artist_artwork(self, artist_key: str) -> bool:
        artist_dir = self._artist_folder_for_key(artist_key, create=False)
        if artist_dir is None:
            return False

        removed = False
        for artwork in self._artist_artwork_files(artist_dir):
            self._invalidate_artwork_path(artwork)
            artwork.unlink()
            removed = True

        if removed:
            self._invalidate_cover_lookups_for_folder(artist_dir)
        return removed

    def _artist_cover_for_key(self, artist_key: str | None) -> Path | None:
        if not artist_key:
            return None

        artist_dir = self._artist_folder_for_key(artist_key, create=False)
        if artist_dir is None:
            return None

        try:
            cover = self._folder_cover(
                artist_dir,
                preferred_stems=["artist", "artist-cover", "cover", "folder", "front"],
            )
            if cover and cover.exists():
                return cover
        except Exception:
            pass
        return None

    def _select_item_by_user_data(self, widget: QListWidget, value: str | None) -> None:
        if value is None:
            return

        for i in range(widget.count()):
            item = widget.item(i)
            if str(item.data(Qt.ItemDataRole.UserRole)) == value:
                widget.setCurrentItem(item)
                return

    def _artist_index_section(self, artist_name: str) -> str:
        first = next(
            (
                character
                for character in (artist_name or "").strip()
                if character.isalnum()
            ),
            "",
        )
        if not first:
            return "#"

        upper = first.upper()
        if upper in self.DANISH_ARTIST_INDEX:
            return upper

        decomposed = unicodedata.normalize("NFKD", upper)
        latin_base = next(
            (
                character
                for character in decomposed
                if "A" <= character <= "Z"
            ),
            "",
        )
        if latin_base:
            return latin_base

        if upper.isalpha():
            return upper
        return "#"

    def _artist_alphabetical_sort_key(self, artist_key: str) -> tuple[int, str, str]:
        name = self._artist_display_name(artist_key)
        section = self._artist_index_section(name)

        if section == "#":
            section_rank = -1
        elif section in self.ENGLISH_ARTIST_INDEX:
            section_rank = self.ENGLISH_ARTIST_INDEX.index(section)
        elif section in self.DANISH_ARTIST_INDEX:
            section_rank = (
                len(self.ENGLISH_ARTIST_INDEX)
                + self.DANISH_ARTIST_INDEX.index(section)
            )
        else:
            section_rank = len(self.ENGLISH_ARTIST_INDEX) + len(
                self.DANISH_ARTIST_INDEX
            )

        normalized_name = unicodedata.normalize("NFKD", name).casefold()
        return section_rank, section.casefold(), normalized_name

    def _clear_artist_index(self) -> None:
        self._artist_index_targets = {}
        while self.artist_index_layout.count() > 0:
            layout_item = self.artist_index_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_artist_index(self, artist_keys: list[str]) -> None:
        self._clear_artist_index()

        if self._artist_sort_mode() != "az" or not artist_keys:
            self.artist_index_widget.setVisible(False)
            return

        for artist_key in artist_keys:
            item = self._artist_item_by_dir.get(artist_key)
            if item is None:
                continue
            section = self._artist_index_section(
                self._artist_display_name(artist_key)
            )
            self._artist_index_targets.setdefault(section, item)

        sections: list[str] = []
        if "#" in self._artist_index_targets:
            sections.append("#")
        sections.extend(self.ENGLISH_ARTIST_INDEX)

        if any(
            section in self._artist_index_targets
            for section in self.DANISH_ARTIST_INDEX
        ):
            sections.extend(self.DANISH_ARTIST_INDEX)

        standard_sections = {
            "#",
            *self.ENGLISH_ARTIST_INDEX,
            *self.DANISH_ARTIST_INDEX,
        }
        extra_sections = sorted(
            (
                section
                for section in self._artist_index_targets
                if section not in standard_sections
            ),
            key=str.casefold,
        )
        sections.extend(extra_sections)

        for section in sections:
            button = QPushButton(section)
            button.setFlat(True)
            button.setFixedSize(30, 22)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setEnabled(section in self._artist_index_targets)
            button.setToolTip(
                f"Jump to artists beginning with {section}"
                if button.isEnabled()
                else f"No artists beginning with {section}"
            )
            button.setStyleSheet(
                "QPushButton { border: 0; padding: 0; font-weight: 600; }"
                "QPushButton:hover { color: #42bff5; }"
                "QPushButton:disabled { color: #4f555b; }"
            )
            button.clicked.connect(
                partial(self._jump_to_artist_section, section)
            )
            self.artist_index_layout.addWidget(button)

        self.artist_index_layout.addStretch(1)
        self.artist_index_widget.setVisible(True)

    def _jump_to_artist_section(self, section: str) -> None:
        item = self._artist_index_targets.get(section)
        if item is None:
            return
        self.artists.setCurrentItem(item)
        self.artists.scrollToItem(
            item,
            QAbstractItemView.ScrollHint.PositionAtTop,
        )
        self.artists.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _artist_sort_mode(self) -> str:
        return str(self.cmb_artist_sort.currentData() or "az")

    def _series_sort_mode(self) -> str:
        return str(self.cmb_series_sort.currentData() or "az")

    def _on_artist_sort_changed(self, _index: int) -> None:
        self._remember_sort('writers', self._artist_sort_mode())
        if self.library_roots:
            self.load_artists()
            self.status(f"Artist sort: {self.cmb_artist_sort.currentText()}")

    def _on_series_sort_changed(self, _index: int) -> None:
        self._remember_sort('series', self._series_sort_mode())
        if self.current_artist_key:
            self.load_albums(self.current_artist_key)
            self.status(f"Series sort: {self.cmb_series_sort.currentText()}")

    def _on_album_selection_changed(self) -> None:
        item = self.albums.currentItem()
        enabled = False
        if item is not None:
            try:
                album_dir = Path(str(item.data(Qt.ItemDataRole.UserRole)))
                enabled = album_dir.exists()
            except Exception:
                enabled = False
        self.btn_set_album_total.setEnabled(enabled)

    @debug_call()
    def start_scan(self) -> None:
        if self._file_job is not None or self._pending_library_change is not None:
            return
        if self._book_editing:
            self.status('Save or cancel your metadata edit before rescanning.')
            return

        if self._scan_worker is not None or self._scan_thread is not None:
            self.status("A library scan is already running\u2026")
            return

        configured = self.library_settings_store.library_roots() if self.library_settings_store else self.library_roots
        self.library_roots = self._normalize_library_roots(configured)
        self.library_root = self.library_roots[0] if self.library_roots else None

        self._scan_pending = False
        self._set_scan_action_state(running=True, enabled=True)
        self._cancel_artwork_updates()

        self._cache_ready = False
        self._artist_dirs_cache = []
        self._albums_by_artist_cache = {}
        self._mp3s_by_album_cache = {}
        self._track_records = {}
        self._artist_item_by_dir = {}
        self._album_item_by_dir = {}
        self._book_item_by_path = {}
        self._artist_groups_cache = {}
        self._artist_names_cache = {}
        self._artist_folders_by_key_cache = {}
        self._album_artist_alias_cache = {}

        self.artists.clear()
        self.albums.clear()
        self.tracks.clear()
        self._clear_artist_index()
        self.artist_index_widget.setVisible(False)
        self.btn_set_album_total.setEnabled(False)

        if not self.library_roots:
            diag.record('scan', 'skipped', configured=len(configured), available=0)
            self._reset_navigation_state()
            self._cache_ready = True
            self._set_scan_action_state(running=False, enabled=True)
            self.scan_bar.hide()
            if self.stack.currentIndex() == self.PAGE_STATISTICS:
                self._statistics_return_page = self.PAGE_ARTISTS
                self._refresh_statistics()
            else:
                self._go_to(self.PAGE_ARTISTS)
            self.status('Library folders are unavailable. Reconnect the drive and use Rescan.' if configured
                        else 'This library is empty. Open Settings to add its folders.')
            self._update_breadcrumb()
            return

        self.scan_bar.setValue(0)
        self.scan_bar.show()
        self.status(f"Scanning {len(self.library_roots)} folder(s)\u2026 0%")
        self._update_breadcrumb()

        thread = QThread(self)
        worker = ScanWorker(
            self.library_roots,
            self.expected_store,
            self.scan_index_store,
            self.review_store.legacy_snapshot(),
            self.listening_history_store,
            profile=self.library_settings_store.profile(),
        )
        self._scan_thread = thread
        self._scan_worker = worker
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_scan_progress)
        worker.finished.connect(self._on_scan_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(partial(self._on_scan_thread_finished, thread))
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _rescan_or_cancel(self) -> None:
        worker = self._scan_worker
        thread = self._scan_thread

        if worker is not None and thread is not None:
            self._scan_pending = False
            worker.cancel()
            self._set_scan_action_state(running=True, enabled=False)
            self.status("Cancelling the library scan\u2026")
            return

        self.start_scan()

    def _set_scan_action_state(self, *, running: bool, enabled: bool) -> None:
        if running:
            toolbar_text = "Cancel Scan" if enabled else "Stopping Scan\u2026"
            menu_text = "Cancel Library Scan" if enabled else "Stopping Library Scan\u2026"
        else:
            toolbar_text = "Rescan"
            menu_text = "Rescan Current Library"

        self.act_rescan.setText(toolbar_text)
        self.act_rescan_menu.setText(menu_text)
        self.act_rescan.setEnabled(enabled)
        self.act_rescan_menu.setEnabled(enabled)

    def _on_scan_progress(self, pct: int, msg: str) -> None:
        self.scan_bar.setValue(int(pct))
        self.status(f"{msg} \u2014 {pct}%")

    def _show_scan_file_warnings(
        self,
        broken_files: list[dict[str, str]],
        identity_failures: list[str],
        album_mismatches: list[dict[str, str]],
        genre_issues: list[dict[str, str]],
        cover_issues: list[dict[str, str]],
    ) -> None:
        """Show every file that is broken or needs metadata attention."""
        if self._pending_library_change is not None or self._file_job is not None:
            return
        if self.library_settings_store is not None and not self.library_settings_store.preferences()['scan_warning_dialog']:
            return
        if (
            not broken_files
            and not identity_failures
            and not album_mismatches
            and not genre_issues
            and not cover_issues
        ):
            return

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Audiobook scan warnings")

        summary_parts: list[str] = []
        information_parts: list[str] = []
        if broken_files:
            summary_parts.append(
                f"{len(broken_files)} broken or unreadable audiobook file(s)"
            )
            information_parts.append(
                "Broken files were skipped and are not included as normal "
                "audiobooks."
            )
        if album_mismatches:
            summary_parts.append(
                f"{len(album_mismatches)} Album tag/folder mismatch(es)"
            )
            information_parts.append(
                "Album mismatches can make media players place books in the "
                "wrong collection. Open the book, choose Edit Metadata, and "
                "use the highlighted Album warning."
            )
        if genre_issues:
            summary_parts.append(
                f"{len(genre_issues)} Genre metadata error(s)"
            )
            information_parts.append(
                "Genre tags must match the library or series rule in Settings. "
                "Open the book and choose Edit Metadata to use the "
                "highlighted Genre warning."
            )
        if cover_issues:
            summary_parts.append(
                f"{len(cover_issues)} cover consistency error(s)"
            )
            information_parts.append(
                "The cover shown for a book must match the cover embedded "
                "in its audio file. Open the book and choose Edit Metadata to use "
                "the highlighted Cover warning."
            )
        if identity_failures:
            summary_parts.append(
                f"{len(identity_failures)} permanent track ID warning(s)"
            )
            information_parts.append(
                "Files without a saved permanent track ID remain playable, "
                "but opening them for the first time may still be slower."
            )

        message.setText("Scan found " + ", ".join(summary_parts) + ".")
        information = "\n\n".join(information_parts)
        information += "\n\nSelect Show Details to see every full file path."

        details: list[str] = []
        for item in broken_files:
            path = str(item.get("path", "")).strip()
            reason = str(item.get("reason", "")).strip()
            details.append(
                f"BROKEN OR UNREADABLE:\n{path}\nReason: {reason}"
            )

        for path in identity_failures:
            details.append(
                f"TRACK ID COULD NOT BE SAVED:\n{path}"
            )

        for item in album_mismatches:
            path = str(item.get("path", "")).strip()
            album = str(item.get("album", "")).strip() or "<empty>"
            folder = str(item.get("folder", "")).strip()
            details.append(
                "ALBUM TAG DOES NOT MATCH FOLDER:\n"
                f"{path}\nAlbum tag: {album}\nFolder: {folder}"
            )

        for item in genre_issues:
            path = str(item.get("path", "")).strip()
            genre = str(item.get("genre", "")).strip() or "<empty>"
            count = str(item.get("count", "0")).strip() or "0"
            reason = str(item.get("reason", "")).strip()
            details.append(
                "GENRE METADATA ERROR:\n"
                f"{path}\nGenre: {genre}\nTag count: {count}\n"
                f"Reason: {reason}"
            )

        for item in cover_issues:
            path = str(item.get("path", "")).strip()
            program_cover = (
                str(item.get("program_cover", "")).strip() or "<none>"
            )
            reason = str(item.get("reason", "")).strip()
            details.append(
                "COVER CONSISTENCY ERROR:\n"
                f"{path}\nProgram cover: {program_cover}\n"
                f"Reason: {reason}"
            )

        message.setInformativeText(information)
        message.setDetailedText("\n\n".join(details))
        message.setStandardButtons(QMessageBox.StandardButton.Ok)
        message.exec()

    def _on_scan_finished(self, payload: object) -> None:
        if self._pending_library_change is not None:
            self.scan_bar.hide()
            return
        data = payload if isinstance(payload, dict) else {}

        if bool(data.get("cancelled", False)):
            self.scan_bar.hide()
            if self._scan_pending:
                self.status("Scan stopped. Restarting with the new library folders\u2026")
            elif self._closing:
                self.status("Stopping scan\u2026")
            else:
                self.status("Scan cancelled.")
            return

        if bool(data.get("error", False)):
            self._cache_ready = False
            self.scan_bar.hide()
            self.status("Scan failed. See the error log for details.")
            return

        render_started = time.perf_counter()
        diag.record('scan.display', 'start')
        try:
            self._artist_dirs_cache = data.get("artist_dirs", []) or []
            self._albums_by_artist_cache = data.get("albums_by_artist", {}) or {}
            self._mp3s_by_album_cache = data.get("mp3s_by_album", {}) or {}
            raw_records = data.get("track_records", {}) or {}
            if not isinstance(raw_records, dict):
                raise TypeError("track_records is not a dictionary")
            self._track_records = {
                str(path): record
                for path, record in raw_records.items()
                if isinstance(record, CachedTrack)
            }

            raw_broken_files = data.get("broken_files", []) or []
            broken_files = [
                {
                    "path": str(item.get("path", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                }
                for item in raw_broken_files
                if (
                    isinstance(item, dict)
                    and str(item.get("path", "")).strip()
                )
            ]

            raw_identity_failures = data.get("identity_failures", []) or []
            identity_failures = [
                str(path).strip()
                for path in raw_identity_failures
                if str(path).strip()
            ]

            raw_album_mismatches = data.get("album_mismatches", []) or []
            album_mismatches = [
                {
                    "path": str(item.get("path", "")).strip(),
                    "album": str(item.get("album", "")).strip(),
                    "folder": str(item.get("folder", "")).strip(),
                }
                for item in raw_album_mismatches
                if (
                    isinstance(item, dict)
                    and str(item.get("path", "")).strip()
                )
            ]

            if ('genre_issues' in data and data.get('validation_profile')
                    == self.library_settings_store.profile()):
                genre_issues = data['genre_issues']
            else:
                # A user may have edited the rules while this worker was scanning.
                genre_issues = []
                for path, record in self._track_records.items():
                    rule = self._gallery_options(Path(path).parent)['tag_rule']
                    issue = _genre_tag_issue(record.metadata.genre, rule)
                    if issue:
                        genre_issues.append({'path': path, **issue})

            raw_cover_issues = data.get("cover_issues", []) or []
            cover_issues = [
                {
                    "path": str(item.get("path", "")).strip(),
                    "program_cover": str(
                        item.get("program_cover", "")
                    ).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                }
                for item in raw_cover_issues
                if (
                    isinstance(item, dict)
                    and str(item.get("path", "")).strip()
                )
            ]
            self._cache_ready = True
        except Exception:
            dbg_exc("scan finished parse failed")
            self._cache_ready = False
            self.scan_bar.hide()
            self.status("Scan result could not be loaded. See the error log for details.")
            return

        goodreads_values = {
            path: record.goodreads
            for path, record in self._track_records.items()
        }
        audible_values = {
            path: record.audible
            for path, record in self._track_records.items()
        }
        self.review_store.prime_scan_cache(goodreads_values, audible_values)

        self.scan_bar.hide()
        self._album_artist_alias_cache = {}
        self._rebuild_artist_groups()
        self._reload_completed_history_paths()

        reused = int(data.get("reused", 0) or 0)
        refreshed = int(data.get("refreshed", 0) or 0)
        removed = int(data.get("removed", 0) or 0)
        failed = int(data.get("failed", 0) or 0)
        index_saved = bool(data.get("index_saved", False))

        details = f"{reused} reused, {refreshed} refreshed"
        if data.get("history_path_updates"):
            details += f", {data['history_path_updates']} saved paths updated"
        if data.get("history_path_error"):
            details += ", saved paths could not be updated"
        if removed > 0:
            details += f", {removed} removed"
        if failed > 0:
            details += f", {failed} failed"
        if broken_files:
            details += f", {len(broken_files)} broken"
        if identity_failures:
            details += f", {len(identity_failures)} track ID warnings"
        if album_mismatches:
            details += f", {len(album_mismatches)} album mismatches"
        if genre_issues:
            details += f", {len(genre_issues)} genre errors"
        if cover_issues:
            details += f", {len(cover_issues)} cover errors"
        if not index_saved:
            details += ", index was not saved"

        self.status(
            f"Scan complete. {self._library_summary_text()}. {details}."
        )
        showing_statistics = self.stack.currentIndex() == self.PAGE_STATISTICS
        self.load_artists()
        if showing_statistics:
            self._statistics_return_page = self.PAGE_ARTISTS
            self._go_to(self.PAGE_STATISTICS)
            self._refresh_statistics()
        else:
            self._go_to(self.PAGE_ARTISTS)

        diag.record('scan.display', 'ok', ms=diag.elapsed_ms(render_started), books=len(self._track_records))
        if self.library_settings_store.preferences()['scan_warning_dialog'] and (
            broken_files
            or identity_failures
            or album_mismatches
            or genre_issues
            or cover_issues
        ):
            QTimer.singleShot(
                0,
                partial(
                    self._show_scan_file_warnings,
                    broken_files,
                    identity_failures,
                    album_mismatches,
                    genre_issues,
                    cover_issues,
                ),
            )

    def _on_scan_thread_finished(self, thread: QThread) -> None:
        if thread is not self._scan_thread:
            return

        self._scan_worker = None
        self._scan_thread = None
        self._set_scan_action_state(running=False, enabled=True)

        if self._closing:
            self.close()
            return

        if self._pending_library_change is not None:
            self._finish_pending_library_change()
            return

        if self._scan_pending:
            self._scan_pending = False
            self.start_scan()

    def closeEvent(self, event) -> None:
        self._pending_library_change = None
        if self._file_job is not None:
            self._closing = True
            self.status("Finishing the current file operation before closing...")
            event.ignore()
            return
        if self._statistics_job is not None:
            self._closing = True
            self.status("Finishing the statistics read before closing...")
            event.ignore()
            return
        if self.statistics_page is not None and not self.statistics_page.shutdown_artwork():
            self._closing = True
            self.status("Finishing the current cover read before closing...")
            event.ignore()
            return
        self._cancel_artwork_updates()
        if self._series_previews.job is not None:
            self._closing = True
            self.status("Finishing the current cover read before closing...")
            event.ignore()
            return
        thread = self._scan_thread
        worker = self._scan_worker

        if thread is not None and thread.isRunning():
            self._closing = True
            self._scan_pending = False
            self._set_scan_action_state(running=True, enabled=False)
            if worker is not None:
                worker.cancel()
            self.status("Stopping the library scan before closing\u2026")
            event.ignore()
            return

        super().closeEvent(event)

    def _trim_lineedit(self, le: QLineEdit) -> None:
        t = le.text()
        s = t.strip()
        if s != t:
            le.setText(s)

    def _detail_value_label(
        self,
        *,
        word_wrap: bool = True,
    ) -> QLabel:
        label = QLabel("\u2014")
        label.setWordWrap(word_wrap)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet("font-size: 15px;")
        return label

    def _build_book_details_view(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(20)

        header = QHBoxLayout()
        header.setSpacing(28)

        self.book_detail_cover_label = QLabel("No cover")
        self.book_detail_cover_label.setAlignment(Qt.AlignCenter)
        self.book_detail_cover_label.setFixedSize(200, 200)
        self.book_detail_cover_label.setSizePolicy(
            QSizePolicy.Preferred,
            QSizePolicy.Preferred,
        )
        self.book_detail_cover_label.setStyleSheet(
            "QLabel { background: #111; border: 1px solid #343a40; "
            "border-radius: 6px; }"
        )
        header.addWidget(self.book_detail_cover_label, 0, Qt.AlignTop)

        summary_widget = QWidget()
        summary = QVBoxLayout(summary_widget)
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(11)

        self.book_detail_title_label = QLabel("Select an audiobook")
        self.book_detail_title_label.setWordWrap(True)
        self.book_detail_title_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.book_detail_title_label.setStyleSheet(
            "font-size: 28px; font-weight: 700;"
        )
        summary.addWidget(self.book_detail_title_label)

        self.book_detail_author_heading = QLabel("")
        self.book_detail_author_heading.setWordWrap(True)
        self.book_detail_author_heading.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.book_detail_author_heading.setStyleSheet(
            "font-size: 18px; color: #9fb7c9;"
        )
        summary.addWidget(self.book_detail_author_heading)

        rating_row = QHBoxLayout()
        rating_row.setContentsMargins(0, 2, 0, 2)
        rating_row.setSpacing(3)
        self.book_edit_rating_button = QPushButton('Edit rating...')
        self.book_edit_rating_button.setMinimumHeight(32)
        self.book_edit_rating_button.setToolTip('Type a precise number, such as 3.61 out of 5.')
        self.book_edit_rating_button.clicked.connect(self.edit_book_rating)
        rating_row.addWidget(self.book_edit_rating_button)

        self.book_rating_buttons: list[QPushButton] = []
        for star_number in range(1, 6):
            star_button = RatingStarButton()
            star_button.setToolTip(
                f"Set your rating to {star_number} star"
                f"{'s' if star_number != 1 else ''}"
            )
            star_button.setAccessibleName(
                f"Rate this audiobook {star_number} out of 5"
            )
            star_button.clicked.connect(
                partial(self._set_book_rating, star_number)
            )
            rating_row.addWidget(star_button)
            self.book_rating_buttons.append(star_button)

        self.book_rating_text_label = QLabel("Not rated")
        self.book_rating_text_label.setStyleSheet(
            "font-size: 13px; color: #96a0a8; margin-left: 7px;"
        )
        rating_row.addWidget(self.book_rating_text_label)
        rating_row.addStretch(1)

        self.book_favorite_button = QPushButton("\u2661 Favorite")
        self.book_favorite_button.setCheckable(True)
        self.book_favorite_button.setCursor(Qt.PointingHandCursor)
        self.book_favorite_button.setMinimumHeight(36)
        self.book_favorite_button.setStyleSheet(
            "QPushButton { border: 1px solid #4b5660; border-radius: 6px; "
            "padding: 6px 15px; font-weight: 600; color: #cbd4db; "
            "background: #20252a; }"
            "QPushButton:hover { border-color: #df6f86; color: #f3a0b2; }"
            "QPushButton:checked { border-color: #df6f86; color: #ffffff; "
            "background: #8f3549; }"
        )
        self.book_favorite_button.clicked.connect(
            self._set_book_favorite
        )
        rating_row.addWidget(self.book_favorite_button)
        summary.addLayout(rating_row)

        self.book_genre_caption_label = QLabel(
            "Genre tags"
        )
        self.book_genre_caption_label.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #84909a;"
        )
        summary.addWidget(self.book_genre_caption_label)

        self.book_tags_widget = QWidget()
        self.book_tags_layout = QHBoxLayout(self.book_tags_widget)
        self.book_tags_layout.setContentsMargins(0, 0, 0, 0)
        self.book_tags_layout.setSpacing(6)
        summary.addWidget(self.book_tags_widget)

        self.book_album_warning_label = QLabel("")
        self.book_album_warning_label.setWordWrap(True)
        self.book_album_warning_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.book_album_warning_label.setStyleSheet(
            "QLabel { background: #4a2817; border: 1px solid #e58b3f; "
            "border-radius: 6px; color: #ffd8b4; padding: 10px 12px; "
            "font-size: 14px; font-weight: 600; }"
        )
        self.book_album_warning_label.hide()
        summary.addWidget(self.book_album_warning_label)

        self.book_detail_author_label = self._detail_value_label()
        self.book_detail_albumartist_label = self._detail_value_label()
        self.book_detail_series_label = self._detail_value_label()
        self.book_detail_narrator_label = self._detail_value_label()
        self.book_detail_translator_label = self._detail_value_label()
        self.book_detail_year_label = self._detail_value_label()
        self.book_detail_purchased_label = self._detail_value_label()
        self.book_detail_source_label = self._detail_value_label()
        self.book_detail_owned_for_label = self._detail_value_label()
        for label in (self.book_detail_purchased_label, self.book_detail_source_label, self.book_detail_owned_for_label):
            label.setTextFormat(Qt.PlainText)
        self.book_detail_duration_label = self._detail_value_label()
        self.book_detail_track_label = self._detail_value_label()
        self.book_detail_goodreads_label = self._detail_value_label()
        self.book_detail_audible_label = self._detail_value_label()
        self.book_detail_reviewed_label = self._detail_value_label()
        self.book_detail_status_label = self._detail_value_label()
        self.book_detail_status_label.setStyleSheet(
            "font-size: 15px; font-weight: 600;"
        )
        self.book_detail_listens_label = self._detail_value_label()
        self.book_detail_last_listened_label = self._detail_value_label()
        self.book_detail_listening_days_label = self._detail_value_label()

        def add_grid_field(
            grid: QGridLayout,
            row: int,
            column_pair: int,
            caption: str,
            value_label: QLabel,
        ) -> None:
            label = QLabel(caption)
            value_label._caption = label
            label.setStyleSheet(
                "font-size: 12px; font-weight: 600; color: #84909a;"
            )
            label.setAlignment(Qt.AlignTop | Qt.AlignRight)
            base_column = column_pair * 2
            grid.addWidget(label, row, base_column)
            grid.addWidget(value_label, row, base_column + 1)

        status_box = QGroupBox("Listening & status")
        status_grid = QGridLayout(status_box)
        status_grid.setHorizontalSpacing(14)
        status_grid.setVerticalSpacing(8)
        status_grid.setColumnStretch(1, 1)
        status_grid.setColumnStretch(3, 1)
        add_grid_field(
            status_grid, 0, 0, "Status", self.book_detail_status_label
        )
        add_grid_field(
            status_grid, 0, 1, "Completed listens", self.book_detail_listens_label
        )
        add_grid_field(
            status_grid, 1, 0, "Last listened", self.book_detail_last_listened_label
        )
        add_grid_field(
            status_grid, 1, 1, "Time to complete", self.book_detail_listening_days_label
        )
        add_grid_field(
            status_grid, 2, 0, "Goodreads reviewed", self.book_detail_goodreads_label
        )
        add_grid_field(
            status_grid, 2, 1, "Audible review", self.book_detail_audible_label
        )
        add_grid_field(status_grid, 3, 0, 'Reviewed', self.book_detail_reviewed_label)
        self.book_status_box = status_box
        summary.addWidget(status_box)

        metadata_box = QGroupBox("Book metadata")
        metadata_grid = QGridLayout(metadata_box)
        metadata_grid.setHorizontalSpacing(14)
        metadata_grid.setVerticalSpacing(8)
        metadata_grid.setColumnStretch(1, 1)
        metadata_grid.setColumnStretch(3, 1)
        add_grid_field(
            metadata_grid, 0, 0, "Series / Album", self.book_detail_series_label
        )
        add_grid_field(
            metadata_grid, 0, 1, "Series position", self.book_detail_track_label
        )
        add_grid_field(
            metadata_grid, 1, 0, "Author / Writer", self.book_detail_author_label
        )
        add_grid_field(
            metadata_grid, 1, 1, "Album Artist", self.book_detail_albumartist_label
        )
        add_grid_field(
            metadata_grid, 2, 0, "Narrator", self.book_detail_narrator_label
        )
        add_grid_field(
            metadata_grid, 2, 1, "Translator", self.book_detail_translator_label
        )
        add_grid_field(
            metadata_grid, 3, 0, "Release year", self.book_detail_year_label
        )
        add_grid_field(
            metadata_grid, 3, 1, "Duration", self.book_detail_duration_label
        )
        add_grid_field(metadata_grid, 4, 0, 'Purchased on', self.book_detail_purchased_label)
        add_grid_field(metadata_grid, 4, 1, 'Bought from', self.book_detail_source_label)
        add_grid_field(metadata_grid, 5, 0, 'Owned for', self.book_detail_owned_for_label)
        summary.addWidget(metadata_box)
        summary.addStretch(1)

        header.addWidget(summary_widget, 1)
        outer.addLayout(header)

        description_box = QGroupBox("Description / Comment")
        description_layout = QVBoxLayout(description_box)
        self.book_detail_description_label = self._detail_value_label()
        self.book_detail_description_label.setMinimumHeight(80)
        self.book_detail_description_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        description_layout.addWidget(self.book_detail_description_label)
        outer.addWidget(description_box)

        outer.addStretch(1)
        return widget

    def _refresh_book_tag_chips(
        self,
        raw_genre: object,
        *,
        validate: bool = True,
    ) -> None:
        while self.book_tags_layout.count():
            item = self.book_tags_layout.takeAt(0)
            child = item.widget()
            if child is not None:
                child.deleteLater()

        tags = _split_book_tags(raw_genre)
        rule = self._gallery_options(self._selected_track_path.parent if self._selected_track_path else None)['tag_rule']
        genre_issue = _genre_tag_issue(raw_genre, rule) if validate else None
        if genre_issue is None:
            self.book_genre_caption_label.setText(
                f"Genre tags ({rule_description(rule)})"
            )
            self.book_genre_caption_label.setStyleSheet(
                "font-size: 12px; font-weight: 600; color: #84909a;"
            )
        else:
            self.book_genre_caption_label.setText(
                "\u26A0 Genre metadata error"
            )
            self.book_genre_caption_label.setStyleSheet(
                "font-size: 13px; font-weight: 700; color: #facc15;"
            )
        visible_tags = tags[:MAX_VISIBLE_BOOK_TAGS]
        for tag in visible_tags:
            chip = QLabel(tag)
            chip.setStyleSheet(
                "QLabel { background: #3c4248; border: 1px solid #505960; "
                "border-radius: 10px; color: #eef2f5; padding: 3px 9px; "
                "font-size: 12px; font-weight: 600; }"
            )
            chip.setToolTip(tag)
            self.book_tags_layout.addWidget(chip)

        hidden_count = len(tags) - len(visible_tags)
        if hidden_count > 0:
            more_chip = QLabel(f"+{hidden_count}")
            if genre_issue is not None:
                more_chip.setStyleSheet(
                    "QLabel { background: #4a3a0b; border: 1px solid #d8ad20; "
                    "border-radius: 10px; color: #ffe583; padding: 3px 8px; "
                    "font-size: 12px; font-weight: 700; }"
                )
            else:
                more_chip.setStyleSheet(
                    "QLabel { color: #b9c3ca; padding: 3px 5px; "
                    "font-size: 12px; font-weight: 700; }"
                )
            more_chip.setToolTip(
                "More tags: " + ", ".join(tags[MAX_VISIBLE_BOOK_TAGS:])
            )
            self.book_tags_layout.addWidget(more_chip)

        if not tags:
            no_tags = QLabel(
                "\u26A0 Genre is missing" if genre_issue else "No genre tags"
            )
            if genre_issue:
                no_tags.setStyleSheet(
                    "QLabel { background: #4a3a0b; border: 1px solid #d8ad20; "
                    "border-radius: 10px; color: #ffe583; padding: 3px 9px; "
                    "font-size: 12px; font-weight: 700; }"
                )
            else:
                no_tags.setStyleSheet(
                    "font-size: 12px; color: #6f7a82; font-style: italic;"
                )
            self.book_tags_layout.addWidget(no_tags)

        self.book_tags_layout.addStretch(1)

    def _refresh_book_album_warning(
        self,
        track_path: Path,
        metadata: TrackMeta,
    ) -> None:
        mismatch = _album_folder_mismatch(track_path, metadata.album)
        genre_issue = _genre_tag_issue(metadata.genre, self._gallery_options(track_path.parent)['tag_rule'])
        cover_issue = self._cover_consistency_issue_for_track(track_path)
        warnings: list[str] = []

        if mismatch is None:
            self.book_detail_series_label.setStyleSheet("font-size: 15px;")
        else:
            album_text = mismatch["album"] or "<empty>"
            self.book_detail_series_label.setStyleSheet(
                "QLabel { font-size: 15px; font-weight: 700; "
                "color: #ffe583; background: #4a3a0b; "
                "border: 1px solid #d8ad20; border-radius: 4px; "
                "padding: 3px 6px; }"
            )
            warnings.append(
                "ALBUM TAG DOES NOT MATCH THE FOLDER\n"
                f"Album tag: {album_text}    Folder: {mismatch['folder']}\n"
                "Media players may place this audiobook in the wrong "
                "collection."
            )

        if genre_issue is not None:
            warnings.append(
                "GENRE METADATA ERROR\n"
                f"{genre_issue['reason']}"
            )

        if cover_issue is None:
            self.book_detail_cover_label.setStyleSheet(
                "QLabel { background: #111; border: 1px solid #343a40; "
                "border-radius: 6px; }"
            )
        else:
            self.book_detail_cover_label.setStyleSheet(
                "QLabel { background: #111; border: 3px solid #d8ad20; "
                "border-radius: 6px; }"
            )
            program_cover = cover_issue["program_cover"]
            warnings.append(
                "COVER CONSISTENCY ERROR\n"
                f"{cover_issue['reason']}\n"
                f"Program cover: {program_cover}"
            )

        if not warnings:
            self.book_album_warning_label.clear()
            self.book_album_warning_label.hide()
            return

        self.book_album_warning_label.setText(
            "\u26A0 METADATA NEEDS ATTENTION\n\n"
            + "\n\n".join(warnings)
            + "\n\nChoose Edit Metadata to fix the highlighted field(s)."
        )
        self.book_album_warning_label.show()

    def _refresh_editor_album_warning(self, _text: str = "") -> None:
        track_path = self._selected_track_path
        if track_path is None:
            self.editor_album_warning_widget.hide()
            return

        mismatch = _album_folder_mismatch(track_path, self.ed_album.text())
        if mismatch is None:
            self.editor_album_warning_widget.hide()
            self.ed_album.setStyleSheet("")
            return

        album_text = mismatch["album"] or "<empty>"
        self.editor_album_warning_label.setText(
            "\u26A0 Album tag mismatch: "
            f"'{album_text}' does not exactly match folder "
            f"'{mismatch['folder']}'."
        )
        self.ed_album.setStyleSheet(
            "QLineEdit { border: 2px solid #e58b3f; }"
        )
        self.editor_album_warning_widget.show()

    def _refresh_editor_genre_warning(self, _text: str = "") -> None:
        rule = self._gallery_options(self._selected_track_path.parent if self._selected_track_path else None)['tag_rule']
        self.ed_genre.setToolTip('Unique tags: ' + rule_description(rule) + '. Separate with commas, semicolons or |.')
        genre_issue = _genre_tag_issue(self.ed_genre.text(), rule)
        if genre_issue is None:
            self.editor_genre_warning_widget.hide()
            self.ed_genre.setStyleSheet("")
            return

        self.editor_genre_warning_label.setText(
            "\u26A0 " + genre_issue["reason"]
        )
        self.ed_genre.setStyleSheet(
            "QLineEdit { border: 2px solid #d8ad20; }"
        )
        self.editor_genre_warning_widget.show()

    def _refresh_editor_cover_warning(self) -> None:
        track_path = self._selected_track_path
        pending_cover = (
            track_path is not None
            and self._pending_cover_track_path == track_path
            and self._pending_cover_path is not None
        )
        if track_path is None or pending_cover:
            self.editor_cover_warning_widget.hide()
            self.cover_label.setStyleSheet(
                "QLabel { background: #111; border: 1px solid #333; }"
            )
            return

        cover_issue = self._cover_consistency_issue_for_track(track_path)
        if cover_issue is None:
            self.editor_cover_warning_widget.hide()
            self.cover_label.setStyleSheet(
                "QLabel { background: #111; border: 1px solid #333; }"
            )
            return

        self.editor_cover_warning_label.setText(
            "\u26A0 Cover error: "
            + cover_issue["reason"]
            + " Program cover: "
            + cover_issue["program_cover"]
            + ". Click the cover to choose the correct picture, then save."
        )
        self.cover_label.setStyleSheet(
            "QLabel { background: #111; border: 3px solid #d8ad20; }"
        )
        self.editor_cover_warning_widget.show()

    def _use_folder_name_for_album(self) -> None:
        if self._selected_track_path is None:
            return
        self.ed_album.setText(self._selected_track_path.parent.name)
        self.ed_album.setFocus()

    def _refresh_book_preference_controls(self, preferences=None) -> None:
        rating = 0
        favorite = False
        available = (
            self.listening_history_store is not None
            and self._book_track_id is not None
        )

        if available:
            try:
                assert self.listening_history_store is not None
                assert self._book_track_id is not None
                rating, favorite = (preferences if preferences is not None else
                    self.listening_history_store.book_preferences(self._book_track_id))
            except Exception:
                dbg_exc(
                    f"Could not read book preferences for "
                    f"{self._book_track_id}"
                )
                available = False
                rating = 0
                favorite = False

        self._book_rating = rating
        self._book_favorite = favorite
        self.book_edit_rating_button.setEnabled(available)
        for star_number, button in enumerate(
            self.book_rating_buttons,
            start=1,
        ):
            button.setFraction(rating - (star_number - 1))
            button.setEnabled(available)

        self.book_rating_text_label.setText(
            rating_text(rating)
        )
        self.book_favorite_button.blockSignals(True)
        try:
            self.book_favorite_button.setChecked(favorite)
            self.book_favorite_button.setText(
                "\u2665 Favorite" if favorite else "\u2661 Favorite"
            )
            self.book_favorite_button.setEnabled(available)
        finally:
            self.book_favorite_button.blockSignals(False)

    def _set_book_rating(
        self,
        star_number: int,
        _checked: bool = False,
    ) -> None:
        if (
            self.listening_history_store is None
            or self._book_track_id is None
        ):
            return

        new_rating = 0 if self._book_rating == star_number else star_number
        try:
            self.listening_history_store.set_rating(
                self._book_track_id,
                new_rating,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not save rating",
                str(exc),
            )
            self._refresh_book_preference_controls()
            return

        self._refresh_book_preference_controls()
        if self._book_editing:
            self.ed_rating.setValue(new_rating)
        if self._selected_track_path is not None:
            rating_path = str(self._selected_track_path)
            if new_rating > 0:
                self._book_rating_by_path[rating_path] = new_rating
            else:
                self._book_rating_by_path.pop(rating_path, None)
            self._update_after_flag_change()
        if new_rating > 0:
            self.status(f"Saved {rating_text(new_rating)}.")
        else:
            self.status("Cleared audiobook rating.")

    def _set_book_favorite(self, checked: bool) -> None:
        if (
            self.listening_history_store is None
            or self._book_track_id is None
        ):
            return

        try:
            self.listening_history_store.set_favorite(
                self._book_track_id,
                checked,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not save favorite",
                str(exc),
            )
            self._refresh_book_preference_controls()
            return

        self._refresh_book_preference_controls()
        self.status(
            "Added audiobook to Favorites."
            if checked
            else "Removed audiobook from Favorites."
        )

    def _build_track_editor(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.editor_rating_row = QWidget()
        rating_row = QHBoxLayout(self.editor_rating_row)
        rating_row.setContentsMargins(0, 0, 0, 0)
        rating_row.setSpacing(10)
        self.ed_rating = RatingSpinBox()
        self.ed_rating.setMinimumSize(140, 36)
        self.ed_rating.setStyleSheet('QDoubleSpinBox {font-size: 16px; padding: 4px 8px;}')
        rating_row.addWidget(self.ed_rating)
        self.editor_rating_stars = RatingStars()
        rating_row.addWidget(self.editor_rating_stars)
        self.editor_rating_text = QLabel('Unrated')
        rating_row.addWidget(self.editor_rating_text)
        self.editor_rating_clear = QPushButton('Clear rating')
        self.editor_rating_clear.clicked.connect(lambda: self.ed_rating.setValue(0))
        rating_row.addWidget(self.editor_rating_clear)
        rating_row.addStretch()
        self.ed_rating.valueChanged.connect(self._preview_editor_rating)

        self.cover_label = QLabel("Click to add cover")
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setMinimumSize(420, 420)
        self.cover_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.cover_label.setStyleSheet("QLabel { background: #111; border: 1px solid #333; }")
        self.cover_label.setCursor(Qt.PointingHandCursor)
        self.cover_label.mousePressEvent = self._cover_clicked
        layout.addWidget(self.cover_label)

        self.editor_cover_warning_widget = QWidget()
        editor_cover_warning_layout = QHBoxLayout(
            self.editor_cover_warning_widget
        )
        editor_cover_warning_layout.setContentsMargins(0, 0, 0, 0)
        editor_cover_warning_layout.setSpacing(8)
        self.editor_cover_warning_label = QLabel("")
        self.editor_cover_warning_label.setWordWrap(True)
        self.editor_cover_warning_label.setStyleSheet(
            "color: #facc15; font-weight: 600;"
        )
        editor_cover_warning_layout.addWidget(
            self.editor_cover_warning_label,
            1,
        )
        self.editor_cover_warning_widget.hide()
        layout.addWidget(self.editor_cover_warning_widget)

        self.album_progress_label = QLabel("")
        self.album_progress_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.album_progress_label)

        self.chk_goodreads = QCheckBox("Goodreads reviewed")
        self.chk_goodreads.stateChanged.connect(self._goodreads_toggled)
        layout.addWidget(self.chk_goodreads)

        self.chk_audible = QCheckBox("Audible reviewed")
        self.chk_audible.stateChanged.connect(self._audible_toggled)
        layout.addWidget(self.chk_audible)
        self.chk_reviewed = QCheckBox('Reviewed')
        self.chk_reviewed.setToolTip('Your general review marker, independent of Goodreads and Audible. Saved immediately.')
        self.chk_reviewed.toggled.connect(self._reviewed_toggled)
        layout.addWidget(self.chk_reviewed)

        box = QGroupBox("Metadata")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.ed_title = QLineEdit()
        self.ed_artist = QLineEdit()
        self.ed_album = QLineEdit()
        self.ed_album.textChanged.connect(
            self._refresh_editor_album_warning
        )
        self.ed_albumartist = QLineEdit()
        self.ed_year = QLineEdit()
        self.ed_year.setToolTip('Release/publication year used for collection statistics. Use YYYY or an ISO date.')
        self.ed_genre = QLineEdit()
        self.ed_genre.textChanged.connect(
            self._refresh_editor_genre_warning
        )
        self.ed_genre.setPlaceholderText(
            "LitRPG; Progression; Fantasy"
        )
        self.ed_genre.setToolTip(
            "Separate tags with semicolons, commas, or vertical bars. "
            "The library or series tag rule applies. Book Details shows up "
            "to six clean tag chips."
        )
        self.ed_composer = QLineEdit()
        self.ed_narrator = QLineEdit()
        self.ed_narrator.setPlaceholderText(
            "Separate multiple narrators with semicolons"
        )
        self.ed_narrator.setToolTip(
            "Narrator names are stored separately and do not create author cards."
        )
        self.ed_translator = QLineEdit()
        self.ed_translator.setPlaceholderText(
            "Separate multiple translators with semicolons"
        )
        self.ed_translator.setToolTip(
            "Translator names are stored separately and do not create author cards."
        )

        for le in (
            self.ed_title,
            self.ed_artist,
            self.ed_album,
            self.ed_albumartist,
            self.ed_year,
            self.ed_genre,
            self.ed_composer,
            self.ed_narrator,
            self.ed_translator,
        ):
            le.editingFinished.connect(partial(self._trim_lineedit, le))

        self.sp_discno = QSpinBox()
        self.sp_discno.setRange(0, 999)

        self.sp_trackno = QSpinBox()
        self.sp_trackno.setRange(0, 999)

        self.sp_totaltracks = QSpinBox()
        self.sp_totaltracks.setRange(0, 9999)

        self.ed_comment = QPlainTextEdit()
        self.ed_comment.setPlaceholderText("Description / Comment (audiobook players often show this)")
        self.ed_comment.setFixedHeight(90)
        self.ed_comment.textChanged.connect(self._autosize_comment)

        self.ed_directory = QLineEdit()
        self.ed_directory.setReadOnly(True)

        self.editor_album_warning_widget = QWidget()
        editor_album_warning_layout = QHBoxLayout(
            self.editor_album_warning_widget
        )
        editor_album_warning_layout.setContentsMargins(0, 0, 0, 0)
        editor_album_warning_layout.setSpacing(8)
        self.editor_album_warning_label = QLabel("")
        self.editor_album_warning_label.setWordWrap(True)
        self.editor_album_warning_label.setStyleSheet(
            "color: #f0a25f; font-weight: 600;"
        )
        self.btn_use_folder_album = QPushButton("Use Folder Name")
        self.btn_use_folder_album.setToolTip(
            "Copy the containing folder name into the Album field"
        )
        self.btn_use_folder_album.clicked.connect(
            self._use_folder_name_for_album
        )
        editor_album_warning_layout.addWidget(
            self.editor_album_warning_label,
            1,
        )
        editor_album_warning_layout.addWidget(self.btn_use_folder_album)
        self.editor_album_warning_widget.hide()

        self.editor_genre_warning_widget = QWidget()
        editor_genre_warning_layout = QHBoxLayout(
            self.editor_genre_warning_widget
        )
        editor_genre_warning_layout.setContentsMargins(0, 0, 0, 0)
        editor_genre_warning_layout.setSpacing(8)
        self.editor_genre_warning_label = QLabel("")
        self.editor_genre_warning_label.setWordWrap(True)
        self.editor_genre_warning_label.setStyleSheet(
            "color: #facc15; font-weight: 600;"
        )
        editor_genre_warning_layout.addWidget(
            self.editor_genre_warning_label,
            1,
        )
        self.editor_genre_warning_widget.hide()

        form.addRow("Title", self.ed_title)
        self.editor_rating_label = QLabel('Rating (0-5)')
        self.editor_rating_label.setBuddy(self.ed_rating)
        form.addRow(self.editor_rating_label, self.editor_rating_row)
        self.purchase_date_row = QWidget()
        purchase_layout = QHBoxLayout(self.purchase_date_row)
        purchase_layout.setContentsMargins(0, 0, 0, 0)
        self.purchase_date_known = QCheckBox('Date known')
        self.ed_purchased_on = QDateEdit(QDate.currentDate())
        self.ed_purchased_on.setDisplayFormat('yyyy-MM-dd')
        self.ed_purchased_on.setCalendarPopup(True)
        self.ed_purchased_on.setDateRange(QDate(1, 1, 1), QDate.currentDate())
        self.ed_purchased_on.setEnabled(False)
        self.ed_purchased_on.setAccessibleName('Purchase date')
        self.purchase_date_known.toggled.connect(self.ed_purchased_on.setEnabled)
        purchase_layout.addWidget(self.purchase_date_known)
        purchase_layout.addWidget(self.ed_purchased_on)
        purchase_layout.addStretch()
        self.purchase_clear = QPushButton('Clear purchase details')
        self.purchase_clear.clicked.connect(lambda: self._set_purchase_inputs(('', '')))
        purchase_layout.addWidget(self.purchase_clear)
        self.ed_purchase_source = QLineEdit()
        self.ed_purchase_source.setMaxLength(200)
        self.ed_purchase_source.setPlaceholderText('Optional: Audible, author website, bookshop...')
        self.ed_purchase_source.setToolTip('Saved with your personal library data when you click Save Metadata.')
        form.addRow('Purchased on', self.purchase_date_row)
        form.addRow('Bought from', self.ed_purchase_source)
        form.addRow("Artist", self.ed_artist)
        form.addRow("Album", self.ed_album)
        form.addRow("", self.editor_album_warning_widget)
        form.addRow("Album Artist", self.ed_albumartist)
        form.addRow("Narrator", self.ed_narrator)
        form.addRow("Translator", self.ed_translator)
        form.addRow("Release year", self.ed_year)
        form.addRow("Track #", self.sp_trackno)
        form.addRow("Total Tracks", self.sp_totaltracks)
        form.addRow("Genre", self.ed_genre)
        form.addRow("", self.editor_genre_warning_widget)
        form.addRow("Comment", self.ed_comment)
        form.addRow("Directory", self.ed_directory)

        layout.addWidget(box)

        history_list_box = QGroupBox("Listening history")
        history_list_layout = QVBoxLayout(history_list_box)
        history_list_layout.setSpacing(8)

        self.book_history_table = QTableWidget(0, 4)
        self.book_history_table.setHorizontalHeaderLabels(
            ["Started", "Finished", "Duration", "Note"]
        )
        self.book_history_table.horizontalHeader().setStretchLastSection(True)
        self.book_history_table.setAlternatingRowColors(True)
        self.book_history_table.setSelectionBehavior(
            self.book_history_table.SelectionBehavior.SelectRows
        )
        self.book_history_table.setSelectionMode(
            self.book_history_table.SelectionMode.SingleSelection
        )
        self.book_history_table.verticalHeader().setVisible(False)
        self.book_history_table.setMinimumHeight(190)
        self.book_history_table.itemSelectionChanged.connect(
            self._on_listening_history_selection_changed
        )
        self.book_history_table.cellDoubleClicked.connect(
            self._edit_listening_history_from_row
        )
        history_list_layout.addWidget(self.book_history_table)

        history_actions = QHBoxLayout()
        self.btn_add_completed_listen = QPushButton("Add Completed Listen")
        self.btn_add_completed_listen.clicked.connect(
            self.add_completed_listen
        )
        self.btn_edit_listen = QPushButton("Edit Selected Listen")
        self.btn_edit_listen.setEnabled(False)
        self.btn_edit_listen.clicked.connect(self.edit_selected_listen)
        self.btn_delete_listen = QPushButton("Delete Selected Listen")
        self.btn_delete_listen.setEnabled(False)
        self.btn_delete_listen.clicked.connect(self.delete_selected_listen)

        history_actions.addWidget(self.btn_add_completed_listen)
        history_actions.addWidget(self.btn_edit_listen)
        history_actions.addWidget(self.btn_delete_listen)
        history_actions.addStretch(1)
        history_list_layout.addLayout(history_actions)
        layout.addWidget(history_list_box)

        self.chk_autorename = QCheckBox("Auto-rename file after saving")
        self.chk_autorename.setChecked(True)
        self.chk_renamesidecar = QCheckBox("Also rename sidecar cover (track.jpg/png) if present")
        self.chk_renamesidecar.setChecked(True)
        layout.addWidget(self.chk_autorename)
        layout.addWidget(self.chk_renamesidecar)

        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("Save Metadata")
        self.btn_reload = QPushButton("Reload From File")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_reload)
        layout.addLayout(btn_row)

        self.btn_save.clicked.connect(self.save_metadata_for_selected)
        self.btn_reload.clicked.connect(self._reload_book_from_file)

        layout.addStretch(1)
        return w

    def _autosize_comment(self) -> None:
        doc_h = int(self.ed_comment.document().size().height()) + 16
        self.ed_comment.setFixedHeight(max(90, min(doc_h, 320)))

    def _configure_grid(self, w: QListWidget, icon: int, cell_w: int, cell_h: int, *, icon_h: int | None = None) -> None:
        w.setViewMode(QListWidget.IconMode)
        w.setFlow(QListWidget.LeftToRight)
        w.setWrapping(True)
        w.setResizeMode(QListWidget.Adjust)
        w.setMovement(QListWidget.Static)
        w.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        w.verticalScrollBar().setSingleStep(36)
        w.setIconSize(QSize(icon, icon_h if icon_h is not None else icon))
        w.setGridSize(QSize(cell_w, cell_h))
        w.setUniformItemSizes(True)
        w.setWordWrap(True)
        w.setSpacing(12)

    def _select_item_by_path(self, widget: QListWidget, path: Path | None) -> None:
        if path is None:
            return

        target = str(path)
        for i in range(widget.count()):
            item = widget.item(i)
            if str(item.data(Qt.ItemDataRole.UserRole)) == target:
                widget.setCurrentItem(item)
                return

    def _goodreads_toggled(self, _state: int) -> None:
        if not self._selected_track_path:
            return
        self.review_store.set_goodreads_done(self._selected_track_path, self.chk_goodreads.isChecked())
        if self.stack.currentIndex() == self.PAGE_BOOK:
            self._refresh_book_detail_status()
        self._update_after_flag_change()

    def _audible_toggled(self, _state: int) -> None:
        if not self._selected_track_path:
            return
        self.review_store.set_audible_done(self._selected_track_path, self.chk_audible.isChecked())
        if self.stack.currentIndex() == self.PAGE_BOOK:
            self._refresh_book_detail_status()
        self._update_after_flag_change()
        self.status("Saved Audible flag for this track.")

    def _update_after_flag_change(self) -> None:
        if self._selected_track_path is not None:
            self._update_single_book_item(self._selected_track_path)
        if self.current_album_dir:
            self._update_current_book_progress_label()
            self._update_single_album_item(self.current_album_dir)
        affected = [key for key, albums in self._artist_groups_cache.items()
                    if self.current_album_dir in albums]
        for key in affected:
            self._update_single_artist_item(key)
        if self._artist_sort_mode() in ('rating_high', 'rating_low'):
            self.load_artists()

    def _meta_and_length_for_track(
        self,
        track_path: Path,
    ) -> tuple[TrackMeta, float | None]:
        """Render indexed metadata; opening/reloading a book checks freshness."""
        key = str(track_path)
        record = self._track_records.get(key)

        if record is not None:
            return record.metadata, record.length_seconds

        return read_meta_and_length(track_path)

    def _album_mp3s(self, album_dir: Path) -> list[Path]:
        if self._cache_ready:
            cached = self._mp3s_by_album_cache.get(str(album_dir))
            if cached is not None:
                # The scanner filters missing/broken files. Opening a book
                # checks its current file again in the background.
                return cached

        fresh = list_tracks(album_dir, SUPPORTED_AUDIO_EXTENSIONS)
        if self._cache_ready:
            self._mp3s_by_album_cache[str(album_dir)] = fresh
        return fresh

    def _refresh_album_cache(self, album_dir: Path) -> None:
        fresh = list_tracks(album_dir, SUPPORTED_AUDIO_EXTENSIONS)
        self._mp3s_by_album_cache[str(album_dir)] = fresh

    def _album_denominator(self, album_dir: Path, mp3s: list[Path]) -> int:
        expected = self.expected_store.get(album_dir)
        return expected if expected > 0 else len(mp3s)

    def _reload_completed_history_paths(self) -> None:
        latest, counts, ratings, reviewed = {}, {}, {}, set()
        if self.listening_history_store is not None:
            try:
                latest, counts, ratings, reviewed = self.listening_history_store.gallery_snapshot()
            except Exception:
                dbg_exc("Could not refresh audiobook progress")
                return  # Retain the last good snapshot if the database is temporarily busy.
        self._last_listened_history_by_path = {p: value[0] for p, value in latest.items()}
        self._listening_days_history_by_path = {p: value[1] for p, value in latest.items()}
        self._completed_listen_count_by_path = counts
        self._reviewed_paths = reviewed
        self._book_rating_by_path = ratings
        self._completed_history_paths = set(latest)

    def _is_book_completed(self, track_path: Path) -> bool:
        return str(track_path) in self._completed_history_paths

    def _book_last_listened(self, track_path: Path) -> str | None:
        return self._last_listened_history_by_path.get(str(track_path))

    def _book_listening_days(self, track_path: Path) -> int | None:
        return self._listening_days_history_by_path.get(str(track_path))

    def _book_completed_listens(self, track_path: Path) -> int:
        return int(
            self._completed_listen_count_by_path.get(
                str(track_path),
                0,
            )
            or 0
        )

    def _book_rating_for_path(self, track_path: Path) -> float:
        return rating_value(self._book_rating_by_path.get(str(track_path), 0))

    def _album_rating_summary(self, album_dir: Path) -> tuple[float, int]:
        ratings = [
            self._book_rating_for_path(track_path)
            for track_path in self._album_mp3s(album_dir)
        ]
        ratings = [rating for rating in ratings if rating > 0]
        if not ratings:
            return 0.0, 0
        return mean_rating(ratings), len(ratings)

    def _artist_rating_summary(self, artist_key: str) -> tuple[float, int]:
        """Each rated series contributes once, with equal weight."""
        ratings = [self._album_rating_summary(album) for album in set(self._artist_album_dirs(artist_key))]
        averages = [average for average, count in ratings if count]
        return (mean_rating(averages), len(averages)) if averages else (0.0, 0)

    def _book_completion_text(self, track_path: Path) -> str:
        count = self._book_completed_listens(track_path)
        if count <= 0:
            if not self._is_book_completed(track_path):
                return "Not completed"
            count = 1
        unit = "time" if count == 1 else "times"
        return f"Completed {count} {unit}"

    def _update_current_book_progress_label(self):
        if self.current_album_dir is None:
            self.tracks_progress_label.clear()
            return
        lines = self._series_card_lines(self.current_album_dir)
        self.tracks_progress_label.setText(' | '.join(text for text, color in lines))
        self.album_progress_label.setText(self.tracks_progress_label.text())

    def _artist_series_metrics(self, artist_key: str) -> tuple[int, int]:
        album_dirs = self._artist_album_dirs(artist_key)
        total_series = len(album_dirs)
        completed_series = 0

        for al in album_dirs:
            paths = self._album_mp3s(al)
            denom = self._album_denominator(al, paths)
            if denom > 0 and sum(self._is_book_completed(path) for path in paths) >= denom:
                completed_series += 1

        return total_series, completed_series

    def _album_updated_mtime(self, album_dir: Path) -> float:
        latest = 0.0
        try:
            latest = float(album_dir.stat().st_mtime)
        except Exception:
            latest = 0.0

        for p in self._album_mp3s(album_dir):
            try:
                latest = max(latest, float(p.stat().st_mtime))
            except Exception:
                pass

        return latest

    def _artist_updated_mtime(self, artist_key: str) -> float:
        latest = 0.0

        for album_dir in self._artist_album_dirs(artist_key):
            latest = max(latest, self._album_updated_mtime(album_dir))

        return latest

    def _sorted_artist_keys(self, artist_keys: list[str]) -> list[str]:
        mode = self._artist_sort_mode()
        if mode == "az":
            return sorted(artist_keys, key=self._artist_alphabetical_sort_key)

        def key(artist):
            name = self._artist_display_name(artist).lower()
            if mode in ("rating_high", "rating_low"):
                rating, rated = self._artist_rating_summary(artist)
                return (not rated, -rating if mode == "rating_high" else rating,
                        self._artist_display_name(artist).casefold())
            if mode in ("newest", "oldest"):
                mtime = self._artist_updated_mtime(artist)
                return (-mtime if mode == "newest" else mtime, name)
            if mode in ("most_completed", "least_completed"):
                total, completed = self._artist_series_metrics(artist)
                sign = -1 if mode == "most_completed" else 1
                return (sign * completed, sign * total, name)
            if mode in ("most_series", "fewest_series"):
                total = len(self._artist_album_dirs(artist))
                return (-total if mode == "most_series" else total, name)
            return (name,)

        return sorted(artist_keys, key=key)

    def _sorted_album_dirs(self, album_dirs: list[Path]) -> list[Path]:
        mode = self._series_sort_mode()

        def key(album):
            name = album.name.lower()
            if mode in ("newest", "oldest"):
                mtime = self._album_updated_mtime(album)
                return (-mtime if mode == "newest" else mtime, name)
            if mode not in ("completed_first", "incomplete_first", "most_progress",
                            "least_progress", "most_owned", "missing_most"):
                return (name,)
            paths = self._album_mp3s(album)
            if mode == "most_owned":
                return (-len(paths), name)
            denom = self._album_denominator(album, paths)
            if mode == "missing_most":
                return (-max(0, denom - len(paths)), name)
            finished = sum(self._is_book_completed(path) for path in paths)
            progress = finished / denom if denom else 0
            if mode in ("completed_first", "incomplete_first"):
                complete = denom > 0 and finished >= denom
                return (not complete if mode == "completed_first" else complete, -progress, name)
            sign = -1 if mode == "most_progress" else 1
            return (sign * progress, sign * finished, name)

        return sorted(album_dirs, key=key)

    def _theme_icon_or_fallback(self, theme_name: str, fallback_sp: QStyle.StandardPixmap) -> QIcon:
        return theme_icon_or_fallback(self.style(), theme_name, fallback_sp)

    def _track_metadata_issues(
        self,
        track_path: Path,
        metadata: TrackMeta | None = None,
    ) -> list[str]:
        if metadata is None:
            metadata, _length_seconds = self._meta_and_length_for_track(
                track_path
            )
        issues = _track_metadata_issue_messages(track_path, metadata, self._gallery_options(track_path.parent)['tag_rule'])
        cover_issue = self._cover_consistency_issue_for_track(track_path)
        if cover_issue is not None:
            issues.append(cover_issue["reason"])
        return issues

    def _cover_consistency_issue_for_track(self, track_path: Path) -> dict[str, str] | None:
        # Warning badges consume the scanner/book-load result. Painting a
        # parent card must never open every audiobook file beneath that parent.
        record = self._track_records.get(str(track_path))
        return record.cover_issue if record is not None else None

    def _album_metadata_error_count(self, album_dir: Path) -> int:
        invalid_tracks = 0
        for track_path in self._album_mp3s(album_dir):
            record = self._track_records.get(str(track_path))
            if record is not None:
                invalid = bool(_track_metadata_issue_messages(track_path, record.metadata, self._gallery_options(track_path.parent)['tag_rule'])
                               or record.cover_issue)
            else:
                invalid = bool(self._track_metadata_issues(track_path))
            if invalid:
                invalid_tracks += 1
        return invalid_tracks

    def _artist_metadata_error_count(self, artist_key: str) -> int:
        return sum(
            self._album_metadata_error_count(album_dir)
            for album_dir in self._artist_album_dirs(artist_key)
        )

    @staticmethod
    def _artwork_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat_result = path.stat()
            return int(stat_result.st_mtime_ns), int(stat_result.st_size)
        except OSError:
            return None

    def _invalidate_artwork_path(self, image_path: Path) -> None:
        path_key = str(image_path)
        for cache_key in tuple(self._path_icon_cache):
            if cache_key[0] == path_key:
                self._path_icon_cache.pop(cache_key, None)

    def _invalidate_cover_lookups_for_folder(self, folder: Path) -> None:
        folder_key = str(folder)
        for cache_key in tuple(self._cover_lookup_cache):
            cached_target = cache_key[1]
            cached_folder = (
                str(Path(cached_target).parent)
                if cache_key[0] == "sidecar"
                else cached_target
            )
            if cached_folder == folder_key:
                self._cover_lookup_cache.pop(cache_key, None)

    def _folder_cover(
        self,
        folder: Path,
        preferred_stems: list[str],
    ) -> Path | None:
        try:
            directory_mtime = int(folder.stat().st_mtime_ns)
        except OSError:
            return None

        folder_key = str(folder)
        stems = tuple(stem.casefold() for stem in preferred_stems)
        cache_key = ("folder", folder_key, stems, directory_mtime)

        if cache_key in self._cover_lookup_cache:
            cached = self._cover_lookup_cache[cache_key]
            if cached is None or cached.exists():
                self._cover_lookup_cache.move_to_end(cache_key)
                return cached
            self._cover_lookup_cache.pop(cache_key, None)

        for stale_key in tuple(self._cover_lookup_cache):
            if stale_key[0] == "folder" and stale_key[1] == folder_key:
                self._cover_lookup_cache.pop(stale_key, None)

        cover = find_cover_in_folder(
            folder,
            preferred_stems=list(preferred_stems),
        )
        self._cover_lookup_cache[cache_key] = cover
        self._cover_lookup_cache.move_to_end(cache_key)

        while len(self._cover_lookup_cache) > self.COVER_LOOKUP_CACHE_MAX_ENTRIES:
            self._cover_lookup_cache.popitem(last=False)

        return cover

    def _track_sidecar_cover(self, track_path: Path) -> Path | None:
        try:
            directory_mtime = int(track_path.parent.stat().st_mtime_ns)
        except OSError:
            return None

        track_key = str(track_path)
        cache_key = ("sidecar", track_key, (), directory_mtime)

        if cache_key in self._cover_lookup_cache:
            cached = self._cover_lookup_cache[cache_key]
            if cached is None or cached.exists():
                self._cover_lookup_cache.move_to_end(cache_key)
                return cached
            self._cover_lookup_cache.pop(cache_key, None)

        for stale_key in tuple(self._cover_lookup_cache):
            if stale_key[0] == "sidecar" and stale_key[1] == track_key:
                self._cover_lookup_cache.pop(stale_key, None)

        cover = find_track_sidecar_cover(track_path)
        self._cover_lookup_cache[cache_key] = cover
        self._cover_lookup_cache.move_to_end(cache_key)

        while len(self._cover_lookup_cache) > self.COVER_LOOKUP_CACHE_MAX_ENTRIES:
            self._cover_lookup_cache.popitem(last=False)

        return cover

    def _path_icon(self, image_path: Path) -> QIcon:
        signature = self._artwork_signature(image_path)
        if signature is None:
            return QIcon()

        path_key = str(image_path)
        cache_key = (path_key, signature[0], signature[1])
        cached = self._path_icon_cache.get(cache_key)

        if cached is not None:
            self._path_icon_cache.move_to_end(cache_key)
            return cached

        for stale_key in tuple(self._path_icon_cache):
            if stale_key[0] == path_key:
                self._path_icon_cache.pop(stale_key, None)

        icon = QIcon(path_key)
        if icon.isNull():
            return icon

        self._path_icon_cache[cache_key] = icon
        self._path_icon_cache.move_to_end(cache_key)

        while len(self._path_icon_cache) > self.PATH_ICON_CACHE_MAX_ENTRIES:
            self._path_icon_cache.popitem(last=False)

        return icon

    def _remove_embedded_cache_entry(self, track_key: str) -> None:
        cached = self._embedded_cache.pop(track_key, None)
        if cached is None:
            return

        blob = cached[1]
        if blob is not None:
            self._embedded_cache_bytes = max(
                0,
                self._embedded_cache_bytes - len(blob),
            )

    def _invalidate_embedded_artwork(self, track_path: Path) -> None:
        track_key = str(track_path)
        self._remove_embedded_cache_entry(track_key)

        for cache_key in tuple(self._decoded_icon_cache):
            if cache_key[0] == track_key:
                self._decoded_icon_cache.pop(cache_key, None)

    def _embedded_bytes(self, track_path: Path) -> bytes | None:
        signature = self._artwork_signature(track_path)
        track_key = str(track_path)

        if signature is None:
            self._invalidate_embedded_artwork(track_path)
            return None

        cached = self._embedded_cache.get(track_key)
        if cached is not None and cached[0] == signature:
            self._embedded_cache.move_to_end(track_key)
            return cached[1]

        self._remove_embedded_cache_entry(track_key)
        blob = get_embedded_cover_bytes(track_path)
        self._embedded_cache[track_key] = (signature, blob)
        self._embedded_cache.move_to_end(track_key)

        if blob is not None:
            self._embedded_cache_bytes += len(blob)

        while (
            len(self._embedded_cache) > self.EMBEDDED_CACHE_MAX_ENTRIES
            or self._embedded_cache_bytes > self.EMBEDDED_CACHE_MAX_BYTES
        ):
            oldest_key = next(iter(self._embedded_cache))
            self._remove_embedded_cache_entry(oldest_key)

        return blob

    def _embedded_icon(
        self,
        track_path: Path,
        size: QSize,
    ) -> QIcon | None:
        signature = self._artwork_signature(track_path)
        if signature is None:
            self._invalidate_embedded_artwork(track_path)
            return None

        cache_key = (
            str(track_path),
            signature[0],
            signature[1],
            int(size.width()),
            int(size.height()),
        )
        cached = self._decoded_icon_cache.get(cache_key)

        if cached is not None:
            self._decoded_icon_cache.move_to_end(cache_key)
            return cached

        blob = self._embedded_bytes(track_path)
        if not blob:
            return None

        icon = qicon_from_bytes(blob, size)
        if icon is None or icon.isNull():
            return None

        self._decoded_icon_cache[cache_key] = icon
        self._decoded_icon_cache.move_to_end(cache_key)

        while len(self._decoded_icon_cache) > self.DECODED_ICON_CACHE_MAX_ENTRIES:
            self._decoded_icon_cache.popitem(last=False)

        return icon

    def _move_embedded_artwork_cache(
        self,
        old_path: Path,
        new_path: Path,
    ) -> None:
        old_key = str(old_path)
        new_key = str(new_path)
        cached = self._embedded_cache.pop(old_key, None)

        if cached is not None:
            old_blob = cached[1]
            if old_blob is not None:
                self._embedded_cache_bytes = max(
                    0,
                    self._embedded_cache_bytes - len(old_blob),
                )

            signature = self._artwork_signature(new_path)
            if signature is not None:
                blob = cached[1]
                self._embedded_cache[new_key] = (signature, blob)
                self._embedded_cache.move_to_end(new_key)
                if blob is not None:
                    self._embedded_cache_bytes += len(blob)

        for cache_key in tuple(self._decoded_icon_cache):
            if cache_key[0] == old_key or cache_key[0] == new_key:
                self._decoded_icon_cache.pop(cache_key, None)

        self._invalidate_cover_lookups_for_folder(new_path.parent)

    def _queue_artist_card_updates(self, artist_keys: list[str]) -> None:
        self._held_parent_jobs["artist"].clear()
        self._artist_artwork_generation += 1
        generation = self._artist_artwork_generation
        self._artwork_queue = deque(
            job for job in self._artwork_queue
            if job[0] != "artist"
        )

        for artist_key in artist_keys:
            self._artwork_queue.append(("artist", generation, artist_key))

        if self._artwork_queue and not self._artwork_timer.isActive():
            self._artwork_timer.start()

    def _queue_album_card_updates(self, album_dirs: list[Path]) -> None:
        self._held_parent_jobs["album"].clear()
        self._album_artwork_generation += 1
        generation = self._album_artwork_generation
        self._artwork_queue = deque(
            job for job in self._artwork_queue
            if job[0] != "album"
        )

        for album_dir in album_dirs:
            self._artwork_queue.append(("album", generation, str(album_dir)))

        if self._artwork_queue and not self._artwork_timer.isActive():
            self._artwork_timer.start()

    def _cancel_artwork_updates(self) -> None:
        self._series_previews.set_active(False)
        self._series_previews.reset()
        self._book_viewport_timer.stop()
        for jobs in self._held_parent_jobs.values():
            jobs.clear()
        self._artist_artwork_generation += 1
        self._album_artwork_generation += 1
        self._book_artwork_generation += 1
        self._artwork_queue.clear()
        self._artwork_timer.stop()

    def _process_artwork_queue(self) -> None:
        if self._file_job is not None:
            return
        processed = 0

        while self._artwork_queue and processed < self.ARTWORK_BATCH_SIZE:
            kind, generation, value = self._artwork_queue.popleft()
            processed += 1
            if kind in self._held_parent_jobs:
                target = self.PAGE_ARTISTS if kind == "artist" else self.PAGE_ALBUMS
                if self.stack.currentIndex() != target:
                    self._held_parent_jobs[kind][value] = (kind, generation, value)
                    continue
            try:
                if kind == "artist":
                    artist_key = str(value)
                    if (
                        generation == self._artist_artwork_generation
                        and artist_key in self._artist_item_by_dir
                    ):
                        self._update_single_artist_item(artist_key)

                elif kind == "album":
                    album_dir = Path(str(value))
                    if (
                        generation == self._album_artwork_generation
                        and str(album_dir) in self._album_item_by_dir
                    ):
                        self._update_single_album_item(album_dir)

                elif kind == "book":
                    if generation == self._book_artwork_generation:
                        self._update_single_book_item(Path(str(value)))

            except Exception:
                dbg_exc(f"Artwork card update failed for {kind}: {value}")

        if not self._artwork_queue:
            self._artwork_timer.stop()

    def _album_icon(self, album_dir: Path, artist_cover: Path | None) -> QIcon:
        cover = self._folder_cover(
            album_dir,
            preferred_stems=["cover", "folder", "front", "album"],
        )
        if cover and cover.exists():
            return self._path_icon(cover)

        mp3s = self._album_mp3s(album_dir)
        if mp3s:
            icon = self._embedded_icon(mp3s[0], self.albums.iconSize())
            if icon is not None:
                return icon

        if artist_cover and artist_cover.exists():
            return self._path_icon(artist_cover)

        return self._theme_icon_or_fallback("media-optical", QStyle.SP_DirClosedIcon)

    def _artist_icon(self, artist_key: str) -> QIcon:
        cover = self._artist_cover_for_key(artist_key)
        if cover and cover.exists():
            return self._path_icon(cover)

        albums = self._artist_album_dirs(artist_key)
        if albums:
            return self._album_icon(albums[0], artist_cover=None)

        return self._theme_icon_or_fallback("user-identity", QStyle.SP_DirIcon)

    def _update_single_book_item(self, track_path: Path) -> None:
        key = str(track_path)
        item = self._book_item_by_path.get(key)
        if item is None:
            return
        record = self._track_records.get(key)
        metadata = record.metadata if record is not None else self._album_meta_cache.get(key)
        if metadata is None:
            return
        base = item.data(self.BOOK_COVER_ROLE)
        if base is None:
            base = self._theme_icon_or_fallback("audio-x-generic", QStyle.SP_FileIcon)
        options = self._gallery_options(track_path.parent)
        title = (metadata.title or "").strip() or track_path.stem
        lines = self._book_card_lines(track_path)
        issues = self._track_metadata_issues(track_path, metadata)
        rating = self._book_rating_for_path(track_path)
        tooltip = [title] + [text for text, _ in lines] + [f'METADATA ERROR: {issue}' for issue in issues]
        if rating > 0 and options['show_rating']:
            tooltip.append(f'Your rating: {rating_text(rating)}')
        item.setText(title)
        item.setForeground(QBrush())
        item.setIcon(render_card(base, self.tracks.iconSize(), lines, rating=rating,
                                 options=options, warning=bool(issues)))
        item.setToolTip("\n".join(tooltip))

    def load_artists(self) -> None:
        self._size_gallery_cards()
        if not self.library_roots:
            self._queue_artist_card_updates([])
            self.artists.clear()
            self._artist_item_by_dir = {}
            self._rebuild_artist_index([])
            self._update_breadcrumb()
            return

        selected_artist_key = self.current_artist_key

        self.artists.clear()
        self._artist_item_by_dir = {}

        self._rebuild_artist_groups()
        artist_keys = self._sorted_artist_keys(list(self._artist_groups_cache.keys()))
        placeholder = self._theme_icon_or_fallback(
            "user-identity",
            QStyle.SP_DirIcon,
        )

        for artist_key in artist_keys:
            label = self._artist_display_name(artist_key)
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, artist_key)
            it.setTextAlignment(Qt.AlignHCenter)
            it.setIcon(placeholder)
            it.setToolTip(f"{label}\nLoading artwork and progress\u2026")
            it.setSizeHint(QSize(230, self.artists.iconSize().height() + 36))
            self.artists.addItem(it)
            self._artist_item_by_dir[artist_key] = it

        self._rebuild_artist_index(artist_keys)
        self._queue_artist_card_updates(artist_keys)

        self._select_item_by_user_data(self.artists, selected_artist_key)
        self._update_breadcrumb()

    def load_albums(self, artist_key: str) -> None:
        self._size_gallery_cards()
        selected_album = self.current_album_dir

        self.albums.clear()
        self._album_item_by_dir = {}

        album_dirs = self._artist_album_dirs(artist_key)
        album_dirs = self._sorted_album_dirs(album_dirs)
        placeholder = self._theme_icon_or_fallback(
            "media-optical",
            QStyle.SP_DirClosedIcon,
        )

        for al in album_dirs:
            it = QListWidgetItem(al.name)
            it.setData(Qt.ItemDataRole.UserRole, str(al))
            it.setTextAlignment(Qt.AlignHCenter)
            it.setIcon(placeholder)
            it.setToolTip(f"{al}\nLoading artwork and progress\u2026")
            it.setSizeHint(QSize(230, self.albums.iconSize().height() + 36))
            self.albums.addItem(it)
            self._album_item_by_dir[str(al)] = it

        self._queue_album_card_updates(album_dirs)

        self._select_item_by_path(self.albums, selected_album)

        if self.albums.currentItem() is None and self.albums.count() > 0:
            self.albums.setCurrentRow(0)

        self._on_album_selection_changed()

    def _update_single_artist_item(self, artist_key):
        item = self._artist_item_by_dir.get(artist_key)
        if item is None:
            return
        options = self._gallery_options()
        albums = self._artist_album_dirs(artist_key)
        books = [path for album in albums for path in self._album_mp3s(album)]
        total = len(albums)
        lines = []
        if options['show_owned']:
            owned = sum(len(self._album_mp3s(album)) >= self.expected_store.get(album) > 0 for album in albums)
            lines.append((f'Series owned {owned}/{total}', status_color(owned, total)))
        if options['show_completed']:
            _, completed = self._artist_series_metrics(artist_key)
            lines.append((f'Series completed {completed}/{total}', status_color(completed, total)))
        lines += self._review_card_lines(books, len(books), options)
        rating, rated = self._artist_rating_summary(artist_key)
        errors = self._artist_metadata_error_count(artist_key)
        item.setIcon(render_card(self._artist_icon(artist_key), self.artists.iconSize(), lines,
                                 rating=rating, options=options, warning=errors > 0))
        label = self._artist_display_name(artist_key)
        item.setText(label)
        item.setTextAlignment(Qt.AlignHCenter)
        tooltip = [label] + [line for line, color in lines]
        if options['show_rating'] and rated:
            tooltip.append(f'Writer rating: {rating_text(rating)} from {rated} rated series')
            tooltip.append('Each series average has equal weight; unrated series are excluded.')
        if errors:
            tooltip.append(f'METADATA ERROR: {errors} audiobook(s) need attention')
        item.setToolTip('\n'.join(tooltip))

    def _update_single_album_item(self, album_dir):
        item = self._album_item_by_dir.get(str(album_dir))
        if item is None:
            return
        options = self._gallery_options(album_dir)
        lines = self._series_card_lines(album_dir)
        artist_cover = self._artist_cover_for_key(self.current_artist_key) if self.current_artist_key else None
        rating, rated = self._album_rating_summary(album_dir)
        errors = self._album_metadata_error_count(album_dir)
        item.setIcon(render_card(self._album_icon(album_dir, artist_cover), self.albums.iconSize(), lines,
                                 rating=rating, options=options, warning=errors > 0))
        tooltip = [str(album_dir)] + [line for line, color in lines]
        if options['show_rating'] and rated:
            tooltip.append(f'Series rating: {rating_text(rating)} from {rated} rated audiobooks')
        if errors:
            tooltip.append(f'METADATA ERROR: {errors} audiobook(s) need attention')
        item.setToolTip('\n'.join(tooltip))

    def open_artist(self, item: QListWidgetItem) -> None:
        artist_key = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not artist_key:
            return

        self.current_artist_key = artist_key
        self.current_artist_dir = self._artist_folder_for_key(artist_key, create=False)
        self.current_album_dir = None
        self.load_albums(artist_key)
        self._go_to(self.PAGE_ALBUMS)

    def open_album(self, item: QListWidgetItem) -> None:
        album_dir = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        if not album_dir.exists():
            return
        self.current_album_dir = album_dir
        self.load_tracks(album_dir)
        self._go_to(self.PAGE_TRACKS)

    def _open_book_item(self, item: QListWidgetItem) -> None:
        self._select_book_item(item)
        self.open_selected_book_details()

    def open_selected_book_details(self) -> None:
        if self._selected_track_path is None:
            return
        self.open_book_details(self._selected_track_path)

    def edit_selected_book(self) -> None:
        if self._selected_track_path is None:
            return
        self.open_book_details(
            self._selected_track_path,
            edit=True,
        )

    def _display_book_value(self, value: object) -> str:
        text = str(value or "").strip()
        return text or "\u2014"

    def _set_book_information_value(
        self,
        field: QLabel,
        value: object,
    ) -> None:
        """Set one compact detail value without leaving a blank field."""
        field.setText(self._display_book_value(value))
        field.setVisible(True)

    def _book_position_text(self, metadata: TrackMeta) -> str:
        track_number = int(getattr(metadata, "trackno", 0) or 0)
        total_tracks = int(getattr(metadata, "totaltracks", 0) or 0)

        if track_number > 0 and total_tracks > 0:
            return f"{track_number}/{total_tracks}"
        if track_number > 0:
            return str(track_number)
        return ""

    def open_book_details(self, track_path: Path, *, edit: bool = False, from_statistics=None) -> None:
        if self._file_job is not None or self._closing or self._pending_library_change is not None:
            return
        if self._scan_thread is not None:
            self.status("Please wait for the library scan to finish.")
            return
        path = Path(track_path)
        # Queued clicks from an old view must never register a book in another
        # library after switching. Historical paths removed from this library's
        # folders can still open unless a different library now owns that path.
        if self.library_settings_store is not None:
            resolved = path.resolve()
            active = self.library_settings_store.active_library_id()
            for profile in self.library_settings_store.libraries():
                if profile['id'] == active:
                    continue
                if any(Path(root) == resolved or Path(root) in resolved.parents for root in profile['roots']):
                    self.status('That audiobook belongs to ' + profile['name'] + '. Switch libraries to open it.')
                    return
        if from_statistics is None:
            from_statistics = self._book_return_to_statistics and self.stack.currentIndex() == self.PAGE_BOOK
        record = self._track_records.get(str(path))
        history, reviews = self.listening_history_store, self.review_store
        preview = self._series_previews.preview(path)
        self._run_file_job("Loading audiobook", "Loading audiobook...",
            lambda progress: _load_book_data(path, record, history, progress, reviews, preview),
            partial(self._on_book_loaded, edit=edit, from_statistics=from_statistics),
            operation='book.open', details={'format': diag.audio_kind(path)})

    def _on_book_loaded(self, data, error, *, edit=False, from_statistics=False):
        if error is not None:
            if from_statistics and isinstance(error, FileNotFoundError):
                # The file may have disappeared after the last Statistics
                # refresh. Recheck once and retire its open action immediately.
                self.status("That file is no longer available. Refreshing Statistics; saved history is kept.")
                self._refresh_statistics()
                return
            QMessageBox.warning(self, "Could not open audiobook", str(error))
            self.status("Audiobook could not be opened.")
            return
        if not self._closing:
            self._book_return_to_statistics = bool(from_statistics)
            if from_statistics:
                self.current_album_dir = data["path"].parent
                for artist_key, albums in self._artist_groups_cache.items():
                    if self.current_album_dir in albums:
                        self.current_artist_key = artist_key
                        break
                # A direct book jump changes the library context. After the
                # return to Statistics, Back goes to the library root instead
                # of exposing an older album grid with a different context.
                self._statistics_return_page = self.PAGE_ARTISTS
            self._show_loaded_book(data, edit=edit)

    def _show_loaded_book(self, data, *, edit=False):
        began = time.perf_counter()
        diag.record('book.render', 'start')
        self._accept_book_data(data)
        track_path = data["path"]
        self._selected_track_path = track_path
        metadata, length_seconds = data["metadata"], data["length"]
        title = (metadata.title or "").strip() or track_path.stem
        author = (metadata.artist or metadata.albumartist or "").strip()
        self._book_title_text = title
        self._book_track_id = data["track_id"]
        history_error = data["history_error"]

        self.book_detail_title_label.setText(title)
        self.book_detail_author_heading.setText(author)
        purchased_on, purchase_source = self._book_purchase
        self.book_detail_purchased_label.setText(purchased_on or 'Not recorded')
        self.book_detail_source_label.setText(purchase_source or 'Not recorded')
        self.book_detail_owned_for_label.setText(owned_for(purchased_on))
        self._set_book_information_value(
            self.book_detail_author_label,
            metadata.artist,
        )
        self._set_book_information_value(
            self.book_detail_albumartist_label,
            metadata.albumartist,
        )
        self._set_book_information_value(
            self.book_detail_series_label,
            metadata.album,
        )
        self._set_book_information_value(
            self.book_detail_narrator_label,
            _narrator_for_metadata(metadata),
        )
        self._set_book_information_value(
            self.book_detail_translator_label,
            getattr(metadata, "translator", ""),
        )
        self._set_book_information_value(
            self.book_detail_year_label,
            metadata.year,
        )
        self._refresh_book_tag_chips(metadata.genre)
        self._refresh_book_album_warning(track_path, metadata)
        self._set_book_information_value(
            self.book_detail_duration_label,
            fmt_len(length_seconds)
            if length_seconds is not None
            else "",
        )
        self._set_book_information_value(
            self.book_detail_track_label,
            self._book_position_text(metadata),
        )
        self.book_detail_description_label.setText(
            self._display_book_value(getattr(metadata, "comment", ""))
        )

        self._refresh_book_detail_status(data)
        self._refresh_book_preference_controls(data["preferences"])
        self._refresh_listening_history_table(data["events"])
        self._refresh_book_detail_cover(track_path)

        self._book_editing = False
        self.book_mode_stack.setCurrentIndex(0)
        self._go_to(self.PAGE_BOOK)

        # Refresh just this book and its parents after an external change or
        # replacement identity was discovered, without delaying the page.
        self._artwork_queue.append(("book", self._book_artwork_generation, str(track_path)))
        self._artwork_queue.append(("album", self._album_artwork_generation, str(track_path.parent)))
        for artist_key, albums in self._artist_groups_cache.items():
            if track_path.parent in albums:
                self._artwork_queue.append(("artist", self._artist_artwork_generation, artist_key))

        if edit:
            self.show_book_editor()
        elif history_error:
            self.status(
                "Book details opened, but its listening-history identity "
                "could not be registered."
            )
        else:
            self.status(f"Book details: {title}")
        diag.record('book.render', 'ok', ms=diag.elapsed_ms(began))

    def _refresh_book_detail_status(self, prepared=None) -> None:
        track_path = self._selected_track_path
        if track_path is None or not track_path.exists():
            return

        goodreads = self.review_store.goodreads_done(track_path)
        audible = self.review_store.audible_done(track_path)

        self.book_detail_goodreads_label.setText("Yes" if goodreads else "No")
        self.book_detail_audible_label.setText("Yes" if audible else "No")
        self.book_detail_reviewed_label.setText('Yes' if str(track_path) in self._reviewed_paths else 'No')
        self._apply_book_display_options()

        listen_count = 0
        last_listened: str | None = None
        if (
            self.listening_history_store is not None
            and self._book_track_id is not None
        ):
            try:
                if prepared is not None:
                    listen_count = prepared["listen_count"]
                    last_listened = prepared["last_listened"]
                else:
                    listen_count = self.listening_history_store.listen_count(self._book_track_id)
                    last_listened = self.listening_history_store.last_listened_on(self._book_track_id)
            except Exception:
                dbg_exc(
                    f"Could not read listening summary for "
                    f"{self._book_track_id}"
                )

        completed = listen_count > 0
        if completed:
            self.book_detail_status_label.setText("Completed")
            self.book_detail_status_label.setStyleSheet(
                "font-size: 16px; font-weight: 600; color: #2ecc71;"
            )
        else:
            self.book_detail_status_label.setText("Not completed")
            self.book_detail_status_label.setStyleSheet(
                "font-size: 16px; font-weight: 600; color: #95a5a6;"
            )

        self.book_detail_listens_label.setText(str(listen_count))
        self.book_detail_last_listened_label.setText(
            last_listened or "Never"
        )
        listening_days = _format_listening_days(
            self._book_listening_days(track_path)
        )
        self.book_detail_listening_days_label.setText(
            listening_days or "\u2014"
        )

    def _refresh_listening_history_table(self, events=None) -> None:
        self._listening_events_by_id = {}
        self.btn_edit_listen.setEnabled(False)
        self.btn_delete_listen.setEnabled(False)

        history_available = (
            self.listening_history_store is not None
            and self._book_track_id is not None
        )
        self.btn_add_completed_listen.setEnabled(history_available)

        if not history_available:
            self.book_history_table.setRowCount(0)
            return

        try:
            assert self.listening_history_store is not None
            assert self._book_track_id is not None
            if events is None:
                events = self.listening_history_store.listens_for_book(self._book_track_id)
        except Exception:
            dbg_exc(
                f"Could not load listening history for "
                f"{self._book_track_id}"
            )
            self.book_history_table.setRowCount(0)
            self.btn_add_completed_listen.setEnabled(False)
            return

        self._listening_events_by_id = {
            event.event_id: event
            for event in events
        }

        def read_only_item(text: str) -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            return item

        with signal_blocker(self.book_history_table):
            self.book_history_table.setRowCount(len(events))

            for row, event in enumerate(events):
                started_item = read_only_item(event.started_on or "Unknown")
                started_item.setData(
                    Qt.ItemDataRole.UserRole,
                    event.event_id,
                )
                self.book_history_table.setItem(row, 0, started_item)
                self.book_history_table.setItem(
                    row,
                    1,
                    read_only_item(event.finished_on),
                )
                self.book_history_table.setItem(
                    row,
                    2,
                    read_only_item(
                        fmt_len(event.duration_seconds)
                        if event.duration_seconds is not None
                        else "Unknown"
                    ),
                )
                note_item = read_only_item(event.note)
                note_item.setToolTip(event.note)
                self.book_history_table.setItem(row, 3, note_item)

            self.book_history_table.clearSelection()

        self.book_history_table.resizeColumnsToContents()
        self.book_history_table.horizontalHeader().setStretchLastSection(True)

    def _selected_listening_event(self) -> ListeningEvent | None:
        row = self.book_history_table.currentRow()
        if row < 0:
            return None

        item = self.book_history_table.item(row, 0)
        if item is None:
            return None

        try:
            event_id = int(
                item.data(Qt.ItemDataRole.UserRole)
            )
        except (TypeError, ValueError, OverflowError):
            return None

        return self._listening_events_by_id.get(event_id)

    def _on_listening_history_selection_changed(self) -> None:
        selected = self._selected_listening_event() is not None
        self.btn_edit_listen.setEnabled(selected)
        self.btn_delete_listen.setEnabled(selected)

    def _edit_listening_history_from_row(
        self,
        row: int,
        _column: int,
    ) -> None:
        if row < 0 or row >= self.book_history_table.rowCount():
            return
        self.book_history_table.selectRow(row)
        self.edit_selected_listen()

    def add_completed_listen(self) -> None:
        if (
            self.listening_history_store is None
            or self._book_track_id is None
            or self._selected_track_path is None
        ):
            QMessageBox.warning(
                self,
                "Listening history unavailable",
                "This audiobook could not be connected to the permanent "
                "listening-history database.",
            )
            return

        dialog = ListeningEventDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        started_on, finished_on, note = dialog.values()

        try:
            self.listening_history_store.add_completed_listen(
                self._book_track_id,
                started_on=started_on,
                finished_on=finished_on,
                note=note,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not save listening history",
                str(exc),
            )
            return

        self._reload_completed_history_paths()
        self._refresh_book_detail_status()
        self._refresh_listening_history_table()
        self._update_after_flag_change()
        self.status(f"Added completed listen ending {finished_on}.")

    def edit_selected_listen(self) -> None:
        event = self._selected_listening_event()
        if event is None or self.listening_history_store is None:
            return

        dialog = ListeningEventDialog(
            self,
            event=event,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        started_on, finished_on, note = dialog.values()
        try:
            self.listening_history_store.update_listen(
                event.event_id,
                started_on=started_on,
                finished_on=finished_on,
                duration_seconds=event.duration_seconds,
                note=note,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not update listening history",
                str(exc),
            )
            return

        self._reload_completed_history_paths()
        self._refresh_book_detail_status()
        self._refresh_listening_history_table()
        self._update_after_flag_change()
        self.status(f"Updated completed listen ending {finished_on}.")

    def delete_selected_listen(self) -> None:
        event = self._selected_listening_event()
        if event is None or self.listening_history_store is None:
            return

        answer = QMessageBox.question(
            self,
            "Delete listening entry",
            "Delete the completed listen ending "
            f"{event.finished_on}?\n\n"
            "If this is the final valid From/To entry, the audiobook will "
            "become not completed.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted = self.listening_history_store.delete_listen(
                event.event_id
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not delete listening history",
                str(exc),
            )
            return

        if not deleted:
            QMessageBox.warning(
                self,
                "Listening entry missing",
                "The selected listening entry no longer exists.",
            )
            self._refresh_book_detail_status()
            self._refresh_listening_history_table()
            return

        self._reload_completed_history_paths()
        self._refresh_book_detail_status()
        self._refresh_listening_history_table()
        self._update_after_flag_change()
        self.status("Deleted listening-history entry.")

    def show_book_editor(self) -> None:
        if self._pending_library_change is not None or self._file_job is not None:
            return
        if self._selected_track_path is None:
            return

        self.reload_selected_into_editor()
        self._refresh_listening_history_table()
        self._book_editing = True
        self.book_mode_stack.setCurrentIndex(1)
        self._go_to(self.PAGE_BOOK)
        QTimer.singleShot(0, self._reveal_rating_input)
        self.status(f"Editing metadata: {self._book_title_text}")

    def _reveal_rating_input(self):
        if not self._book_editing or self.stack.currentIndex() != self.PAGE_BOOK:
            return
        self.editor_widget.layout().activate()
        # Start at the metadata form, even if the previous editor was scrolled
        # to its cover or Save buttons. Title and the numeric rating stay together.
        top = self.ed_title.mapTo(self.editor_widget, QPoint(0, 0)).y()
        self.editor_scroll.verticalScrollBar().setValue(max(0, top - 36))
        self.editor_scroll.ensureWidgetVisible(self.ed_rating, 20, 24)

    def _focus_rating_input(self):
        self._reveal_rating_input()
        if self._book_editing and self.stack.currentIndex() == self.PAGE_BOOK:
            self.ed_rating.setFocus(Qt.OtherFocusReason)
            self.ed_rating.selectAll()

    def edit_book_rating(self):
        if self._file_job is not None or self._book_track_id is None:
            return
        self.show_book_editor()
        QTimer.singleShot(0, self._focus_rating_input)

    def cancel_book_editing(self) -> None:
        if self._selected_track_path is None:
            return
        self._clear_pending_cover()
        self.open_book_details(self._selected_track_path)

    def _back_to_book_list(self) -> None:
        self._book_editing = False
        self.book_mode_stack.setCurrentIndex(0)
        if self._book_return_to_statistics:
            self._book_return_to_statistics = False
            self._go_to(self.PAGE_STATISTICS)
            self._refresh_statistics()
            return
        self._go_to(self.PAGE_TRACKS)

    def _book_item_path(self, item: QListWidgetItem | None) -> Path | None:
        if item is None:
            return None
        raw_path = item.data(Qt.ItemDataRole.UserRole)
        return Path(str(raw_path)) if raw_path else None

    def _select_book_item(self, item: QListWidgetItem | None) -> None:
        p = self._book_item_path(item)
        if not p:
            return

        self._selected_track_path = p
        if self.tracks.currentItem() is not item:
            self._suppress_track_selection_handler = True
            try:
                with signal_blocker(self.tracks):
                    self.tracks.setCurrentItem(item)
            finally:
                self._suppress_track_selection_handler = False

    def _refresh_current_album_after_file_change(self, new_path: Path) -> None:
        if not self.current_album_dir or not self.current_album_dir.exists():
            self._selected_track_path = new_path
            self.reload_selected_into_editor()
            return

        self._refresh_album_cache(self.current_album_dir)
        self._album_artist_alias_cache.pop(str(self.current_album_dir), None)
        self._rebuild_artist_groups()

        if self.current_artist_key and self.current_artist_key not in self._artist_groups_cache:
            aliases = self._album_artist_aliases(self.current_album_dir)
            if aliases:
                self.current_artist_key = self._artist_key_for_name(aliases[0])
            else:
                self.current_artist_key = None

        self._selected_track_path = new_path
        self.load_tracks(self.current_album_dir, select_path=new_path)

        self._update_single_album_item(self.current_album_dir)
        if self.current_artist_key:
            self._update_single_artist_item(self.current_artist_key)

    def load_tracks(self, album_dir: Path, select_path: Path | None = None) -> None:
        self._size_gallery_cards(album_dir)
        self._loading_tracks = True
        self._book_item_by_path = {}
        self.tracks.clear()
        try:
            tracks = self._album_mp3s(album_dir)
            self._album_meta_cache = {}

            cur_expected = self.expected_store.get(album_dir)
            found_total = 0

            decorated: list[tuple[int, str, Path, TrackMeta]] = []
            for p in tracks:
                meta, _ = self._meta_and_length_for_track(p)
                self._album_meta_cache[str(p)] = meta

                if cur_expected <= 0 and getattr(meta, "totaltracks", 0) > 0:
                    found_total = max(found_total, int(meta.totaltracks))

                trackno = int(getattr(meta, "trackno", 0) or 0)
                track_key = trackno if trackno > 0 else 10_000

                decorated.append((track_key, p.name.lower(), p, meta))

            if cur_expected <= 0 and found_total > 0:
                self.expected_store.set(album_dir, found_total)
                cur_expected = found_total

            decorated.sort(key=lambda t: (t[0], t[1]))

            self.current_track_count = len(decorated)
            self.current_album_expected = cur_expected

            self.album_progress_label.setText(
                f"Tracks: {self.current_track_count} | Expected: {self.current_album_expected or '-'}"
            )
            self._update_current_book_progress_label()

            placeholder = self._theme_icon_or_fallback("audio-x-generic", QStyle.SP_FileIcon)
            for _trk_k, _n, p, meta in decorated:
                title = meta.title.strip() if meta.title.strip() else p.stem
                metadata_issues = self._track_metadata_issues(p, meta)
                tooltip_lines = [title] + [text for text, color in self._book_card_lines(p)]
                tooltip_lines.extend(f'METADATA ERROR: {issue}' for issue in metadata_issues)

                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, str(p))
                item.setTextAlignment(Qt.AlignHCenter)
                item.setIcon(placeholder)
                item.setToolTip("\n".join(tooltip_lines))
                item.setSizeHint(QSize(230, self.tracks.iconSize().height() + 36))
                self.tracks.addItem(item)
                self._book_item_by_path[str(p)] = item

        finally:
            self._loading_tracks = False

        # The keys are the exact paths stored on the rows, in display order.
        # TrackMeta values used as keys leave every row with its placeholder.
        self._queue_book_card_updates(self._book_item_by_path)
        self._reset_series_previews()
        self._sync_series_previews()
        if self.tracks.count() <= 0:
            self._selected_track_path = None
            return

        target_item = (
            self._book_item_by_path.get(str(select_path))
            if select_path is not None
            else None
        )
        if target_item is None:
            target_item = self.tracks.item(0)
        self._select_book_item(target_item)

    def _on_book_selection_changed(self) -> None:
        if self._loading_tracks or self._suppress_track_selection_handler:
            return
        self._select_book_item(self.tracks.currentItem())

    def _validate_unique_trackno(self, path: Path, meta: TrackMeta) -> bool:
        tno = int(getattr(meta, "trackno", 0) or 0)
        if tno <= 0:
            return True

        for p_str, m in self._album_meta_cache.items():
            if Path(p_str) == path:
                continue
            other_tno = int(getattr(m, "trackno", 0) or 0)
            if other_tno == tno:
                QMessageBox.warning(
                    self,
                    "Duplicate track number",
                    f"Track #{tno} is already used by:\n{Path(p_str).name}\n\n"
                    "Choose a different track number.",
                )
                return False

        return True

    def _reload_book_from_file(self) -> None:
        if self._selected_track_path and self._file_job is None:
            self._track_records.pop(str(self._selected_track_path), None)
            self.open_book_details(self._selected_track_path, edit=True)

    def _preview_editor_rating(self, value):
        self.editor_rating_stars.setValue(value)
        self.editor_rating_text.setText(rating_text(value))

    def _set_purchase_inputs(self, purchase):
        purchased_on, source = purchase
        self.ed_purchased_on.setMaximumDate(QDate.currentDate())
        self.purchase_date_known.setChecked(bool(purchased_on))
        self.ed_purchased_on.setDate(QDate.fromString(purchased_on, 'yyyy-MM-dd') if purchased_on else QDate.currentDate())
        self.ed_purchased_on.setEnabled(bool(purchased_on))
        self.ed_purchase_source.setText(source)

    def reload_selected_into_editor(self) -> None:
        if not self._selected_track_path or not self._selected_track_path.exists():
            return

        self._clear_pending_cover()
        meta, _len = self._meta_and_length_for_track(self._selected_track_path)

        rating_available = self.listening_history_store is not None and self._book_track_id is not None
        self.ed_rating.setEnabled(rating_available)
        self.editor_rating_clear.setEnabled(rating_available)
        self.purchase_date_row.setEnabled(rating_available)
        self.ed_purchase_source.setEnabled(rating_available)
        self._set_purchase_inputs(self._book_purchase)
        self.ed_rating.setValue(self._book_rating_for_path(self._selected_track_path))
        self._preview_editor_rating(self.ed_rating.value())

        self.ed_title.setText((meta.title or "").strip())
        self.ed_artist.setText((meta.artist or "").strip())
        self.ed_album.setText((meta.album or "").strip())
        self.ed_albumartist.setText((meta.albumartist or "").strip())
        self.ed_year.setText((meta.year or "").strip())
        self.ed_genre.setText((meta.genre or "").strip())
        self.ed_composer.setText((getattr(meta, "composer", "") or "").strip())
        self.ed_narrator.setText(_narrator_for_metadata(meta))
        self.ed_translator.setText((getattr(meta, "translator", "") or "").strip())
        self.ed_comment.setPlainText(getattr(meta, "comment", "") or "")

        self.sp_trackno.setValue(int(getattr(meta, "trackno", 0) or 0))
        self.sp_totaltracks.setValue(int(getattr(meta, "totaltracks", 0) or self.current_album_expected or 0))
        self.sp_discno.setValue(int(getattr(meta, "discno", 0) or 0))

        self.ed_directory.setText(str(self._selected_track_path))
        self.ed_directory.setToolTip(str(self._selected_track_path))
        self._refresh_editor_album_warning()
        self._refresh_editor_genre_warning()

        self.chk_goodreads.blockSignals(True)
        self.chk_audible.blockSignals(True)
        try:
            self.chk_goodreads.setChecked(self.review_store.goodreads_done(self._selected_track_path))
            self.chk_audible.setChecked(self.review_store.audible_done(self._selected_track_path))
        finally:
            self.chk_goodreads.blockSignals(False)
            self.chk_audible.blockSignals(False)

        with signal_blocker(self.chk_reviewed):
            self.chk_reviewed.setChecked(str(self._selected_track_path) in self._reviewed_paths)
        self.chk_reviewed.setEnabled(self._book_track_id is not None)
        self._apply_book_display_options()
        self._refresh_cover_for_track(self._selected_track_path)
        self._refresh_editor_cover_warning()

    def _editor_meta(self) -> TrackMeta:
        return TrackMeta(
            title=self.ed_title.text().strip(),
            artist=self.ed_artist.text().strip(),
            album=self.ed_album.text().strip(),
            albumartist=self.ed_albumartist.text().strip(),
            year=self.ed_year.text().strip(),
            genre=self.ed_genre.text().strip(),
            composer=self.ed_composer.text().strip(),
            narrator=self.ed_narrator.text().strip(),
            translator=self.ed_translator.text().strip(),
            comment=self.ed_comment.toPlainText().strip(),
            discno=int(self.sp_discno.value()),
            trackno=int(self.sp_trackno.value()),
            totaltracks=int(self.sp_totaltracks.value()),
        )

    def save_metadata_for_selected(self) -> None:
        if not self._selected_track_path or self._file_job is not None:
            return
        if self._scan_thread is not None:
            self.status("Please wait for the library scan to finish before saving.")
            return
        path = self._selected_track_path
        meta = self._editor_meta()
        if not self._validate_unique_trackno(path, meta):
            return
        if getattr(meta, "totaltracks", 0) <= 0 and self.current_album_expected > 0:
            meta.totaltracks = self.current_album_expected
        cover = self._pending_cover_path if self._pending_cover_track_path == path else None
        autorename = self.chk_autorename.isChecked()
        rename_sidecar = self.chk_renamesidecar.isChecked()
        return_to_details = self.stack.currentIndex() == self.PAGE_BOOK
        reviews, history = self.review_store, self.listening_history_store
        self.ed_rating.interpretText()
        rating = self.ed_rating.value() if self.ed_rating.isEnabled() else None
        if rating == self._book_rating_for_path(path):
            rating = None
        purchase = None
        if self.ed_purchase_source.isEnabled():
            self.ed_purchased_on.interpretText()
            try:
                purchase = validate_purchase(
                    self.ed_purchased_on.date().toString('yyyy-MM-dd') if self.purchase_date_known.isChecked() else '',
                    self.ed_purchase_source.text())
            except ValueError as exc:
                QMessageBox.warning(self, 'Check purchase details', str(exc))
                return
            if purchase == self._book_purchase:
                purchase = None
        self._run_file_job("Saving audiobook", "Saving, please wait...",
            lambda progress: _save_book_data(path, meta, cover, autorename,
                rename_sidecar, reviews, history, progress, rating=rating, purchase=purchase),
            partial(self._on_metadata_saved, path=path, return_to_details=return_to_details),
            operation='book.save', details={'format': diag.audio_kind(path), 'cover_changed': cover is not None})

    def _on_metadata_saved(self, data, error, *, path, return_to_details):
        if error is not None:
            self._track_records.pop(str(path), None)
            self._invalidate_embedded_artwork(path)
            if isinstance(error, CoverVerificationError):
                self._show_cover_verification_failure(path, str(error))
            else:
                QMessageBox.critical(self, "Save failed", str(error))
            self.status("Saving failed. Your selected cover remains pending.")
            return

        new_path = data["new_path"]
        rating_error = data.get('rating_error')
        purchase_error = data.get('purchase_error')
        self._selected_track_path = new_path
        self._track_records.pop(str(path), None)
        if new_path != path:
            self._move_embedded_artwork_cache(path, new_path)
            old_key = str(path)
            # A rename can succeed even when the following read fails. Move
            # known path references now so a retry does not collide with its
            # own former filename in duplicate-number validation.
            if old_key in self._album_meta_cache:
                self._album_meta_cache[str(new_path)] = self._album_meta_cache.pop(old_key)
            for album_key, paths in self._mp3s_by_album_cache.items():
                self._mp3s_by_album_cache[album_key] = [new_path if item == path else item for item in paths]
            item = self._book_item_by_path.pop(old_key, None)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, str(new_path))
                self._book_item_by_path[str(new_path)] = item
            self._completed_history_paths.discard(old_key)
            if old_key in self._reviewed_paths:
                self._reviewed_paths.discard(old_key)
                self._reviewed_paths.add(str(new_path))
            for cache in (self._last_listened_history_by_path,
                          self._listening_days_history_by_path,
                          self._completed_listen_count_by_path,
                          self._book_rating_by_path):
                if old_key in cache:
                    cache[str(new_path)] = cache.pop(old_key)
        self._invalidate_cover_lookups_for_folder(new_path.parent)
        if data["cover_saved"]:
            self._clear_pending_cover()
        if data["book"] is not None:
            self._accept_book_data(data["book"])
            self._refresh_current_album_after_file_change(new_path)
            self._update_after_flag_change()
            if return_to_details and not rating_error and not purchase_error:
                self._show_loaded_book(data["book"])
        else:
            # The write succeeded but refresh failed: use the real resulting
            # path and ask for a reload, never silently restore the old name.
            self._book_cover_path = None
            self.ed_directory.setText(str(new_path))
            self.ed_directory.setToolTip(str(new_path))
            QMessageBox.warning(self, "Saved; reload needed",
                "The file was saved, but its details could not be reloaded:\n\n"
                + data["refresh_error"])
        if data["rename_error"]:
            QMessageBox.warning(self, "Saved; filename update needs attention", data["rename_error"])
        if rating_error or purchase_error:
            errors = []
            if rating_error:
                self.ed_rating.setValue(data['requested_rating'])
                errors.append('Rating not saved: ' + rating_error)
            if purchase_error:
                self._set_purchase_inputs(data['requested_purchase'])
                errors.append('Purchase details not saved: ' + purchase_error)
            self._book_editing = True
            self.book_mode_stack.setCurrentIndex(1)
            self._go_to(self.PAGE_BOOK)
            title = 'Metadata saved; rating not saved' if rating_error else 'Metadata saved; purchase details not saved'
            QMessageBox.warning(self, title, '\n\n'.join(errors))
            if rating_error:
                QTimer.singleShot(0, self._focus_rating_input)
            self.status('Metadata saved. Unsaved personal details are still in the editor; try Save Metadata again.')
            return
        saved_parts = ["metadata"]
        if data.get('requested_rating') is not None:
            saved_parts.append('rating')
        if data.get('requested_purchase') is not None:
            saved_parts.append('purchase details')
        if data["cover_saved"]:
            saved_parts.append("cover")
        if new_path != path:
            saved_parts.append("renamed file")
        self.status(f"Saved {' + '.join(saved_parts)}: {new_path.name}")

    def _saved_cover_matches_selection(
        self,
        track_path: Path,
        selected_cover_path: Path,
    ) -> bool:
        selected_bytes = _read_cover_file_bytes(selected_cover_path)
        if selected_bytes is None:
            return False

        self._invalidate_embedded_artwork(track_path)
        embedded_bytes = get_embedded_cover_bytes(track_path)
        if not embedded_bytes:
            return False
        return _images_visually_match(selected_bytes, embedded_bytes)

    def _show_cover_verification_failure(
        self,
        track_path: Path,
        detail: str,
    ) -> None:
        self._track_records.pop(str(track_path), None)
        self._invalidate_embedded_artwork(track_path)
        self.editor_cover_warning_label.setText(
            "\u26A0 The selected cover could not be verified inside the "
            "audiobook file. It remains selected so you can try again."
        )
        self.editor_cover_warning_widget.show()
        self.cover_label.setStyleSheet(
            "QLabel { background: #111; border: 3px solid #d8ad20; }"
        )
        QMessageBox.critical(
            self,
            "Cover was not saved correctly",
            "Metadata may have been saved, but the new cover could not be "
            "verified inside the audiobook file. The file was not renamed and the "
            "selected cover is still ready in the editor.\n\n"
            + detail,
        )

    def _clear_pending_cover(self) -> None:
        self._pending_cover_path = None
        self._pending_cover_track_path = None

    def _set_cover_preview_path(self, img_path: Optional[Path]) -> None:
        if not img_path or not img_path.exists():
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("Click to add cover")
            return
        pm = QPixmap(str(img_path))
        if pm.isNull():
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("Click to add cover")
            return
        self.cover_label.setText("")
        self.cover_label.setPixmap(pm.scaled(self.cover_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _set_cover_preview_bytes(self, data: bytes) -> None:
        pm = QPixmap()
        if not pm.loadFromData(data) or pm.isNull():
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("Click to add cover")
            return
        self.cover_label.setText("")
        self.cover_label.setPixmap(pm.scaled(self.cover_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _set_book_detail_cover_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            self.book_detail_cover_label.setPixmap(QPixmap())
            self.book_detail_cover_label.setText("No cover")
            return

        self.book_detail_cover_label.setText("")
        self.book_detail_cover_label.setPixmap(
            pixmap.scaled(
                self.book_detail_cover_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def _refresh_book_detail_cover(self, track_path: Path) -> None:
        if track_path == self._book_cover_path:
            self._set_book_detail_cover_pixmap(QPixmap.fromImage(self._book_cover_image))
            return
        sidecar = self._track_sidecar_cover(track_path)
        if sidecar and sidecar.exists():
            self._set_book_detail_cover_pixmap(QPixmap(str(sidecar)))
            return

        embedded = self._embedded_bytes(track_path)
        if embedded:
            pixmap = QPixmap()
            if pixmap.loadFromData(embedded):
                self._set_book_detail_cover_pixmap(pixmap)
                return

        album_cover = self._folder_cover(
            track_path.parent,
            preferred_stems=["cover", "folder", "front", "album"],
        )
        if album_cover and album_cover.exists():
            self._set_book_detail_cover_pixmap(QPixmap(str(album_cover)))
            return

        self.book_detail_cover_label.setPixmap(QPixmap())
        self.book_detail_cover_label.setText("No cover")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, '_series_previews'):
            self._schedule_visible_books()

    def _refresh_cover_for_track(self, track_path: Path) -> None:
        if (
            self._pending_cover_track_path == track_path
            and self._pending_cover_path is not None
            and self._pending_cover_path.exists()
        ):
            self._set_cover_preview_path(self._pending_cover_path)
            return

        if track_path == self._book_cover_path:
            pixmap = QPixmap.fromImage(self._book_cover_image)
            self.cover_label.setPixmap(pixmap.scaled(self.cover_label.size(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation) if not pixmap.isNull() else pixmap)
            self.cover_label.setText("" if not pixmap.isNull() else "Click to add cover")
            return
        side = self._track_sidecar_cover(track_path)
        if side and side.exists():
            self._set_cover_preview_path(side)
            return

        blob = self._embedded_bytes(track_path)
        if blob:
            self._set_cover_preview_bytes(blob)
            return

        album_dir = track_path.parent
        album_cover = self._folder_cover(
            album_dir,
            preferred_stems=["cover", "folder", "front", "album"],
        )
        if album_cover and album_cover.exists():
            self._set_cover_preview_path(album_cover)
            return

        self.cover_label.setPixmap(QPixmap())
        self.cover_label.setText("Click to add cover")

    def _cover_clicked(self, event) -> None:
        if not self._selected_track_path:
            return
        p = self._selected_track_path
        if not p.exists():
            QMessageBox.warning(self, "Missing", "Selected track no longer exists.")
            return

        img, _ = QFileDialog.getOpenFileName(self, "Select cover image", str(p.parent), "Images (*.jpg *.jpeg *.png)")
        if not img:
            return

        image_path = Path(img)
        preview = QPixmap(str(image_path))
        if preview.isNull():
            QMessageBox.warning(
                self,
                "Invalid cover",
                "The selected image could not be opened.",
            )
            return

        self._pending_cover_path = image_path
        self._pending_cover_track_path = p
        self._set_cover_preview_path(image_path)
        self._refresh_editor_cover_warning()
        self.status(
            "Cover selected. It will be embedded when Save Metadata is clicked."
        )

    def pick_library_root(self) -> None:
        self.open_settings_dialog()

    def go_back(self) -> None:
        page = self.stack.currentIndex()
        if page == self.PAGE_STATISTICS:
            self._go_to(self._statistics_return_page)
        elif page == self.PAGE_BOOK:
            if self._book_editing:
                self.cancel_book_editing()
            else:
                self._back_to_book_list()
        elif page == self.PAGE_TRACKS:
            self._go_to(self.PAGE_ALBUMS)
        elif page == self.PAGE_ALBUMS:
            self._go_to(self.PAGE_ARTISTS)

    def _go_to(self, page: int) -> None:
        self.stack.setCurrentIndex(page)
        self._sync_series_previews()
        kind = {self.PAGE_ARTISTS: "artist", self.PAGE_ALBUMS: "album"}.get(page)
        if kind is not None:
            held = self._held_parent_jobs[kind]
            self._artwork_queue.extendleft(reversed(held.values()))
            held.clear()
        self.main_toolbar.setVisible(page != self.PAGE_STATISTICS)
        if hasattr(self, 'statistics_toolbar'):
            self.statistics_toolbar.setVisible(page == self.PAGE_STATISTICS)
        self.act_statistics.setChecked(page == self.PAGE_STATISTICS)
        if page == self.PAGE_STATISTICS:
            self._artwork_timer.stop()
        elif self._artwork_queue and self._file_job is None:
            self._artwork_timer.start()
        self.act_back.setEnabled(page != self.PAGE_ARTISTS)
        show_edit = page == self.PAGE_BOOK and not self._book_editing
        self.act_edit_book.setVisible(show_edit)
        self.act_edit_book.setEnabled(
            show_edit
            and self._selected_track_path is not None
            and self._selected_track_path.exists()
        )
        self._update_breadcrumb()

    def _albums_context_menu(self, pos: QPoint) -> None:
        item = self.albums.itemAt(pos)
        if not item:
            return
        album_dir = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        if not album_dir.exists():
            return

        owned = len(self._album_mp3s(album_dir))
        cur = self.expected_store.get(album_dir)

        menu = QMenu(self)
        a_set = menu.addAction("Set expected total tracks\u2026")
        a_clear = menu.addAction("Clear expected total")
        a_settings = menu.addAction("Series settings...")
        menu.addSeparator()
        menu.addAction(f"Owned tracks in folder: {owned}")

        act = menu.exec(self.albums.mapToGlobal(pos))
        if act == a_settings:
            self.open_settings_dialog(series=album_dir)
        elif act == a_set:
            self._prompt_set_expected(album_dir, cur, owned)
        elif act == a_clear:
            self.expected_store.clear(album_dir)
            self._update_single_album_item(album_dir)
            if self.current_artist_key:
                self._update_single_artist_item(self.current_artist_key)

    def _artists_context_menu(self, pos: QPoint) -> None:
        item = self.artists.itemAt(pos)
        if item is None:
            return

        artist_key = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not artist_key:
            return

        artist_name = self._artist_display_name(artist_key)
        artist_dir = self._artist_folder_for_key(artist_key, create=False)
        has_saved_artwork = bool(
            artist_dir is not None
            and self._artist_artwork_files(artist_dir)
        )

        menu = QMenu(self)
        set_action = menu.addAction("Set Artist Artwork\u2026")
        remove_action = menu.addAction("Remove Artist Artwork")
        remove_action.setEnabled(has_saved_artwork)

        selected_action = menu.exec(self.artists.mapToGlobal(pos))

        if selected_action == set_action:
            self._choose_artist_artwork(artist_key, artist_name)
        elif selected_action == remove_action:
            self._remove_artist_artwork(artist_key, artist_name)

    def _choose_artist_artwork(self, artist_key: str, artist_name: str) -> None:
        start_dir = str(Path.home())
        existing_folder = self._artist_folder_for_key(artist_key, create=False)

        if existing_folder is not None:
            start_dir = str(existing_folder)
        else:
            albums = self._artist_album_dirs(artist_key)
            if albums:
                start_dir = str(albums[0].parent)

        image_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            f"Select artwork for {artist_name}",
            start_dir,
            "Images (*.jpg *.jpeg *.png *.webp *.bmp)",
        )
        if not image_path:
            return

        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            QMessageBox.warning(
                self,
                "Invalid artwork",
                "The selected file could not be loaded as an image.",
            )
            return

        try:
            saved_path = self._save_artist_artwork(artist_key, Path(image_path))
        except Exception as exc:
            QMessageBox.critical(self, "Artwork failed", str(exc))
            return

        if self.current_artist_key == artist_key:
            self.current_artist_dir = saved_path.parent
        self._update_single_artist_item(artist_key)
        self.status(f"Saved artwork for {artist_name} in {saved_path.parent}.")

    def _remove_artist_artwork(self, artist_key: str, artist_name: str) -> None:
        try:
            removed = self._remove_saved_artist_artwork(artist_key)
        except Exception as exc:
            QMessageBox.critical(self, "Artwork failed", str(exc))
            return

        if not removed:
            return

        self._update_single_artist_item(artist_key)
        self.status(f"Removed artist artwork for {artist_name}.")

    def set_expected_for_selected_album(self) -> None:
        item = self.albums.currentItem()
        if not item:
            QMessageBox.information(self, "Select album", "Select an album first.")
            return
        album_dir = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        if not album_dir.exists():
            return
        owned = len(self._album_mp3s(album_dir))
        cur = self.expected_store.get(album_dir)
        self._prompt_set_expected(album_dir, cur, owned)

    def _prompt_set_expected(self, album_dir: Path, cur: int, owned: int) -> None:
        default = cur if cur > 0 else max(owned, 1)
        label = f"Set expected total tracks for:\n{album_dir.name}"
        val, ok = QInputDialog.getInt(self, "Expected total tracks", label, default, 0, 9999, 1)
        if not ok:
            return
        self.expected_store.set(album_dir, int(val))
        self._update_single_album_item(album_dir)
        if self.current_artist_key:
            self._update_single_artist_item(self.current_artist_key)
        self.status(f"Expected total set to {val} for {album_dir.name}")
