from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QPoint, QObject, Signal, QThread
from PySide6.QtGui import QIcon, QColor, QPixmap
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
    QSplitter,
    QVBoxLayout,
    QHBoxLayout,
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
)

from .album_expected_store import AlbumExpectedStore
from .covers import find_cover_in_folder, find_track_sidecar_cover, get_embedded_cover_bytes
from .debug_tools import dbg_exc, debug_call
from .helpers import fmt_len, fmt_mtime, fmt_size_mb, list_album_dirs, list_artist_dirs, list_tracks
from .metadata_io import read_meta_and_length, write_metadata_to_file, embed_cover_into_file
from .models import TrackMeta
from .naming import rename_track_file
from .qt_compat import signal_blocker
from .review_store import ReviewStore
from .ui_icon_helpers import (
    theme_icon_or_fallback,
    qicon_from_bytes,
    album_tile_icon_with_strip,
    icon_with_badge_text_at,
)
from .watched_store import WatchedStore


class ScanWorker(QObject):
    """
    Background scanner:
    - Builds caches for artist dirs, album dirs, and mp3 track lists.
    - Learns expected totaltracks from first mp3 tag (optional, fast-ish).
    """
    progress = Signal(int, str)  # percent, message
    finished = Signal(object)    # payload dict

    def __init__(self, root: Path, expected: AlbumExpectedStore) -> None:
        super().__init__()
        self.root = root
        self.expected = expected
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            artists = list_artist_dirs(self.root)
            albums_all: list[Path] = []
            albums_by_artist: dict[str, list[Path]] = {}

            for ad in artists:
                als = list_album_dirs(ad)
                albums_by_artist[str(ad)] = als
                albums_all.extend(als)

            total = max(1, len(albums_all))
            mp3s_by_album: dict[str, list[Path]] = {}

            for i, al in enumerate(albums_all):
                if self._cancel:
                    return

                mp3s = [p for p in list_tracks(al) if p.suffix.lower() == ".mp3"]
                mp3s_by_album[str(al)] = mp3s

                try:
                    if self.expected.get(al) <= 0 and mp3s:
                        meta, _ = read_meta_and_length(mp3s[0])
                        if getattr(meta, "totaltracks", 0) > 0:
                            self.expected.set(al, int(meta.totaltracks))
                except Exception:
                    pass

                pct = int(((i + 1) * 100) / total)
                self.progress.emit(pct, f"Scanning albums {i+1}/{total}")

            payload = {"artist_dirs": artists, "albums_by_artist": albums_by_artist, "mp3s_by_album": mp3s_by_album}
            self.finished.emit(payload)

        except Exception:
            dbg_exc("ScanWorker.run failed")
            self.finished.emit({"artist_dirs": [], "albums_by_artist": {}, "mp3s_by_album": {}})


class MainWindow(QMainWindow):
    PAGE_ARTISTS = 0
    PAGE_ALBUMS = 1
    PAGE_TRACKS = 2

    COL_NO = 0
    COL_TITLE = 1
    COL_ARTIST = 2
    COL_ALBUM = 3
    COL_ALBUMARTIST = 4
    COL_YEAR = 5
    COL_GENRE = 6
    COL_LEN = 7
    COL_SIZE = 8
    COL_MOD = 9

    @debug_call()
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BookLibConnect (MP3)")
        self.resize(1700, 920)

        self.library_root: Path | None = None
        self.current_artist_dir: Path | None = None
        self.current_album_dir: Path | None = None

        self.current_track_count = 0
        self.current_album_expected = 0

        self._loading_tracks = False
        self._selected_track_path: Path | None = None
        self._suppress_track_selection_handler = False

        self.expected_store = AlbumExpectedStore()
        self.watched_store = WatchedStore()
        self.review_store = ReviewStore()

        self._embedded_cache: dict[str, bytes] = {}

        self._cache_ready = False
        self._artist_dirs_cache: list[Path] = []
        self._albums_by_artist_cache: dict[str, list[Path]] = {}
        self._mp3s_by_album_cache: dict[str, list[Path]] = {}

        self._album_meta_cache: dict[str, TrackMeta] = {}

        self._artist_item_by_dir: dict[str, QListWidgetItem] = {}
        self._album_item_by_dir: dict[str, QListWidgetItem] = {}

        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None

        self.artists = QListWidget()
        self._configure_grid(self.artists, icon=196, cell_w=240, cell_h=260)
        self.artists.itemDoubleClicked.connect(self.open_artist)

        self.albums = QListWidget()
        self._configure_grid(self.albums, icon=220, cell_w=250, cell_h=340, icon_h=300)
        self.albums.itemDoubleClicked.connect(self.open_album)
        self.albums.setContextMenuPolicy(Qt.CustomContextMenu)
        self.albums.customContextMenuRequested.connect(self._albums_context_menu)

        self.tracks = QTableWidget(0, 10)
        self.tracks.setHorizontalHeaderLabels(
            ["#", "Title", "Artist", "Album", "Album Artist", "Year", "Genre", "Length", "Size (MB)", "Modified"]
        )
        self.tracks.horizontalHeader().setStretchLastSection(True)
        self.tracks.setAlternatingRowColors(True)
        self.tracks.setSelectionBehavior(self.tracks.SelectionBehavior.SelectRows)
        self.tracks.setSelectionMode(self.tracks.SelectionMode.SingleSelection)
        self.tracks.setIconSize(QSize(110, 110))
        self.tracks.verticalHeader().setDefaultSectionSize(118)
        self.tracks.currentCellChanged.connect(self.on_current_cell_changed)

        self.editor_widget = self._build_track_editor()
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QScrollArea.NoFrame)
        self.editor_scroll.setWidget(self.editor_widget)

        self.tracks_split = QSplitter()
        self.tracks_split.setOrientation(Qt.Horizontal)
        self.tracks_split.addWidget(self.editor_scroll)
        self.tracks_split.addWidget(self.tracks)
        self.tracks_split.setStretchFactor(0, 0)
        self.tracks_split.setStretchFactor(1, 1)
        self.tracks_split.setSizes([600, 1200])

        self.tracks_page = QWidget()
        tracks_layout = QHBoxLayout(self.tracks_page)
        tracks_layout.setContentsMargins(0, 0, 0, 0)
        tracks_layout.addWidget(self.tracks_split)

        self.artists_page = QWidget()
        artists_layout = QVBoxLayout(self.artists_page)
        artists_layout.setContentsMargins(18, 14, 18, 18)
        artists_layout.setSpacing(0)
        artists_layout.addWidget(self.artists)

        self.albums_page = QWidget()
        albums_layout = QVBoxLayout(self.albums_page)
        albums_layout.setContentsMargins(18, 14, 18, 18)
        albums_layout.setSpacing(0)
        albums_layout.addWidget(self.albums)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.artists_page)
        self.stack.addWidget(self.albums_page)
        self.stack.addWidget(self.tracks_page)
        self.setCentralWidget(self.stack)

        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_set_root = tb.addAction("Set Library Root")
        self.act_set_root.triggered.connect(self.pick_library_root)

        self.act_back = tb.addAction("Back")
        self.act_back.triggered.connect(self.go_back)
        self.act_back.setEnabled(False)

        self.act_rescan = tb.addAction("Rescan")
        self.act_rescan.triggered.connect(self.start_scan)

        self.act_set_album_total = tb.addAction("Set Album Total…")
        self.act_set_album_total.triggered.connect(self.set_expected_for_selected_album)
        self.act_set_album_total.setEnabled(False)

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

        self.status("Starting…")
        self._auto_load_default_root()

    def status(self, msg: str) -> None:
        platform = os.environ.get("QT_QPA_PLATFORM", "auto")
        self.statusBar().showMessage(f"{msg} (Qt: {platform})")

    @debug_call()
    def _auto_load_default_root(self) -> None:
        default = Path.home() / "Audiobook"
        default.mkdir(parents=True, exist_ok=True)
        self.library_root = default
        self.current_artist_dir = None
        self.current_album_dir = None
        self._selected_track_path = None

        self._go_to(self.PAGE_ARTISTS)
        self.start_scan()

    @debug_call()
    def start_scan(self) -> None:
        if not self.library_root:
            self.status("No library root selected.")
            return

        if self._scan_worker is not None:
            try:
                self._scan_worker.cancel()
            except Exception:
                pass

        self._cache_ready = False
        self._artist_dirs_cache = []
        self._albums_by_artist_cache = {}
        self._mp3s_by_album_cache = {}
        self._artist_item_by_dir = {}
        self._album_item_by_dir = {}

        self.artists.clear()
        self.albums.clear()
        self.tracks.setRowCount(0)

        self.scan_bar.setValue(0)
        self.scan_bar.show()
        self.status("Scanning… 0%")

        self._scan_thread = QThread(self)
        self._scan_worker = ScanWorker(self.library_root, self.expected_store)
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)

        self._scan_thread.start()

    def _on_scan_progress(self, pct: int, msg: str) -> None:
        self.scan_bar.setValue(int(pct))
        self.status(f"{msg} — {pct}%")

    def _on_scan_finished(self, payload: object) -> None:
        try:
            data = payload if isinstance(payload, dict) else {}
            self._artist_dirs_cache = data.get("artist_dirs", []) or []
            self._albums_by_artist_cache = data.get("albums_by_artist", {}) or {}
            self._mp3s_by_album_cache = data.get("mp3s_by_album", {}) or {}
            self._cache_ready = True
        except Exception:
            dbg_exc("scan finished parse failed")
            self._cache_ready = False

        self.scan_bar.hide()
        self.status("Scan complete.")
        self.load_artists()
        self._go_to(self.PAGE_ARTISTS)

        self._scan_worker = None
        self._scan_thread = None

    def closeEvent(self, event) -> None:
        try:
            if self._scan_worker is not None:
                self._scan_worker.cancel()
        except Exception:
            pass
        try:
            if self._scan_thread is not None:
                self._scan_thread.quit()
                self._scan_thread.wait(1500)
        except Exception:
            pass
        super().closeEvent(event)

    def _trim_lineedit(self, le: QLineEdit) -> None:
        t = le.text()
        s = t.strip()
        if s != t:
            le.setText(s)

    def _build_track_editor(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.cover_label = QLabel("Click to add cover")
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setMinimumSize(420, 420)
        self.cover_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.cover_label.setStyleSheet("QLabel { background: #111; border: 1px solid #333; }")
        self.cover_label.setCursor(Qt.PointingHandCursor)
        self.cover_label.mousePressEvent = self._cover_clicked
        layout.addWidget(self.cover_label)

        self.album_progress_label = QLabel("")
        self.album_progress_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.album_progress_label)

        self.chk_watched = QCheckBox("Mark track as read/watched")
        self.chk_watched.stateChanged.connect(self._watched_toggled)
        layout.addWidget(self.chk_watched)

        self.chk_goodreads = QCheckBox("Goodreads updated for this track (main)")
        self.chk_goodreads.stateChanged.connect(self._goodreads_toggled)
        layout.addWidget(self.chk_goodreads)

        self.chk_audible = QCheckBox("Audible review done for this track (not counted)")
        self.chk_audible.stateChanged.connect(self._audible_toggled)
        layout.addWidget(self.chk_audible)

        box = QGroupBox("Metadata")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.ed_title = QLineEdit()
        self.ed_artist = QLineEdit()
        self.ed_album = QLineEdit()
        self.ed_albumartist = QLineEdit()
        self.ed_year = QLineEdit()
        self.ed_genre = QLineEdit()
        self.ed_composer = QLineEdit()

        for le in (
            self.ed_title,
            self.ed_artist,
            self.ed_album,
            self.ed_albumartist,
            self.ed_year,
            self.ed_genre,
            self.ed_composer,
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

        form.addRow("Title", self.ed_title)
        form.addRow("Artist", self.ed_artist)
        form.addRow("Album", self.ed_album)
        form.addRow("Album Artist", self.ed_albumartist)
        form.addRow("Composer", self.ed_composer)
        form.addRow("Year", self.ed_year)
        form.addRow("Track #", self.sp_trackno)
        form.addRow("Total Tracks", self.sp_totaltracks)
        form.addRow("Disc #", self.sp_discno)
        form.addRow("Genre", self.ed_genre)
        form.addRow("Comment", self.ed_comment)
        form.addRow("Directory", self.ed_directory)

        layout.addWidget(box)

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
        self.btn_reload.clicked.connect(self.reload_selected_into_editor)

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
        w.setIconSize(QSize(icon, icon_h if icon_h is not None else icon))
        w.setGridSize(QSize(cell_w, cell_h))
        w.setUniformItemSizes(True)
        w.setWordWrap(True)
        w.setSpacing(12)

    def _watched_toggled(self, _state: int) -> None:
        if not self._selected_track_path:
            return
        self.watched_store.set_watched(self._selected_track_path, self.chk_watched.isChecked())
        self._update_after_flag_change()

    def _goodreads_toggled(self, _state: int) -> None:
        if not self._selected_track_path:
            return
        self.review_store.set_goodreads_done(self._selected_track_path, self.chk_goodreads.isChecked())
        self._update_after_flag_change()

    def _audible_toggled(self, _state: int) -> None:
        if not self._selected_track_path:
            return
        self.review_store.set_audible_done(self._selected_track_path, self.chk_audible.isChecked())
        self.status("Saved Audible flag for this track.")

    def _update_after_flag_change(self) -> None:
        if self.current_album_dir:
            self._update_single_album_item(self.current_album_dir)
        if self.current_artist_dir:
            self._update_single_artist_item(self.current_artist_dir)

    def _album_mp3s(self, album_dir: Path) -> list[Path]:
        if self._cache_ready:
            cached = self._mp3s_by_album_cache.get(str(album_dir))
            if cached is not None:
                cached = [p for p in cached if p.exists() and p.suffix.lower() == ".mp3"]
                self._mp3s_by_album_cache[str(album_dir)] = cached
                return cached

        fresh = [p for p in list_tracks(album_dir) if p.suffix.lower() == ".mp3"]
        if self._cache_ready:
            self._mp3s_by_album_cache[str(album_dir)] = fresh
        return fresh

    def _refresh_album_cache(self, album_dir: Path) -> None:
        fresh = [p for p in list_tracks(album_dir) if p.suffix.lower() == ".mp3"]
        self._mp3s_by_album_cache[str(album_dir)] = fresh

    def _album_denominator(self, album_dir: Path, mp3s: list[Path]) -> int:
        expected = self.expected_store.get(album_dir)
        return expected if expected > 0 else len(mp3s)

    def _album_counts(self, album_dir: Path) -> tuple[int, int, int, int]:
        mp3s = self._album_mp3s(album_dir)
        owned = len(mp3s)
        denom = self._album_denominator(album_dir, mp3s)
        read_count = self.watched_store.count_watched(mp3s)
        gr_count = self.review_store.count_goodreads(mp3s)
        return owned, denom, read_count, gr_count

    def _theme_icon_or_fallback(self, theme_name: str, fallback_sp: QStyle.StandardPixmap) -> QIcon:
        return theme_icon_or_fallback(self.style(), theme_name, fallback_sp)

    def _album_tile_icon_with_strip(
        self,
        cover_icon: QIcon,
        size: QSize,
        *,
        read_line: str,
        read_color: QColor,
        own_line: str,
        own_color: QColor,
        gr_line: str,
        gr_color: QColor,
    ) -> QIcon:
        return album_tile_icon_with_strip(
            self.style(),
            cover_icon,
            size,
            read_line=read_line,
            read_color=read_color,
            own_line=own_line,
            own_color=own_color,
            gr_line=gr_line,
            gr_color=gr_color,
        )

    def _icon_with_badge_text_at(
        self, base_icon: QIcon, text: str, size: QSize, badge_color: QColor, side: str = "right"
    ) -> QIcon:
        return icon_with_badge_text_at(
            self.style(),
            base_icon,
            text,
            size,
            badge_color,
            side=side,
        )

    def _embedded_bytes(self, track_path: Path) -> bytes | None:
        k = str(track_path)
        if k in self._embedded_cache:
            return self._embedded_cache[k]
        blob = get_embedded_cover_bytes(track_path)
        if blob:
            self._embedded_cache[k] = blob
        return blob

    def _qicon_from_bytes(self, data: bytes, size: QSize) -> QIcon | None:
        return qicon_from_bytes(data, size)

    def _album_icon(self, album_dir: Path, artist_cover: Path | None) -> QIcon:
        cover = find_cover_in_folder(album_dir, preferred_stems=["cover", "folder", "front", "album"])
        if cover and cover.exists():
            return QIcon(str(cover))

        mp3s = self._album_mp3s(album_dir)
        if mp3s:
            blob = self._embedded_bytes(mp3s[0])
            if blob:
                ico = self._qicon_from_bytes(blob, self.albums.iconSize())
                if ico:
                    return ico

        if artist_cover and artist_cover.exists():
            return QIcon(str(artist_cover))

        return self._theme_icon_or_fallback("media-optical", QStyle.SP_DirClosedIcon)

    def _artist_icon(self, artist_dir: Path) -> QIcon:
        cover = find_cover_in_folder(artist_dir, preferred_stems=["artist", "artist-cover", "cover", "folder", "front"])
        if cover and cover.exists():
            return QIcon(str(cover))

        albums = (
            self._albums_by_artist_cache.get(str(artist_dir), [])
            if self._cache_ready
            else list_album_dirs(artist_dir)
        )
        if albums:
            return self._album_icon(albums[0], artist_cover=None)

        return self._theme_icon_or_fallback("user-identity", QStyle.SP_DirIcon)

    def _track_icon(self, track_path: Path, album_cover: Path | None, artist_cover: Path | None) -> QIcon:
        side = find_track_sidecar_cover(track_path)
        if side and side.exists():
            return QIcon(str(side))

        blob = self._embedded_bytes(track_path)
        if blob:
            ico = self._qicon_from_bytes(blob, self.tracks.iconSize())
            if ico:
                return ico

        if album_cover and album_cover.exists():
            return QIcon(str(album_cover))
        if artist_cover and artist_cover.exists():
            return QIcon(str(artist_cover))

        return self._theme_icon_or_fallback("audio-x-generic", QStyle.SP_FileIcon)

    def load_artists(self) -> None:
        if not self.library_root:
            return

        self.artists.clear()
        self._artist_item_by_dir = {}

        artist_dirs = self._artist_dirs_cache if self._cache_ready else list_artist_dirs(self.library_root)
        for ad in artist_dirs:
            it = QListWidgetItem(ad.name)
            it.setData(Qt.ItemDataRole.UserRole, str(ad))
            it.setTextAlignment(Qt.AlignHCenter)
            self.artists.addItem(it)
            self._artist_item_by_dir[str(ad)] = it

        for ad in artist_dirs:
            self._update_single_artist_item(ad)

    def load_albums(self, artist_dir: Path) -> None:
        self.albums.clear()
        self._album_item_by_dir = {}

        album_dirs = (
            self._albums_by_artist_cache.get(str(artist_dir), [])
            if self._cache_ready
            else list_album_dirs(artist_dir)
        )

        for al in album_dirs:
            it = QListWidgetItem(al.name)
            it.setData(Qt.ItemDataRole.UserRole, str(al))
            it.setTextAlignment(Qt.AlignHCenter)
            self.albums.addItem(it)
            self._album_item_by_dir[str(al)] = it

        for al in album_dirs:
            self._update_single_album_item(al)

    def _update_single_artist_item(self, artist_dir: Path) -> None:
        it = self._artist_item_by_dir.get(str(artist_dir))
        if not it:
            return

        album_dirs = (
            self._albums_by_artist_cache.get(str(artist_dir), [])
            if self._cache_ready
            else list_album_dirs(artist_dir)
        )
        total = len(album_dirs)
        completed = 0

        for al in album_dirs:
            _owned, denom, read_count, _gr = self._album_counts(al)
            if denom > 0 and read_count >= denom:
                completed += 1

        left_text = f"{completed}/{total}" if total > 0 else "0/0"
        left_color = (
            QColor(46, 204, 113) if (total > 0 and completed >= total)
            else (QColor(243, 156, 18) if completed > 0 else QColor(127, 140, 141))
        )

        right_text = str(total) if total <= 99 else "99+"
        right_color = QColor(52, 152, 219)

        base = self._artist_icon(artist_dir)
        icon = self._icon_with_badge_text_at(base, left_text, self.artists.iconSize(), left_color, side="left")
        icon = self._icon_with_badge_text_at(icon, right_text, self.artists.iconSize(), right_color, side="right")
        it.setIcon(icon)
        it.setToolTip(f"{artist_dir}\nCompleted albums: {left_text}\nAlbums: {right_text}")

    def _update_single_album_item(self, album_dir: Path) -> None:
        it = self._album_item_by_dir.get(str(album_dir))
        if not it:
            return

        owned, denom, read_count, gr_count = self._album_counts(album_dir)

        read_line = f"Read {read_count}/{denom}" if denom > 0 else "Read 0/0"
        own_line = f"Own/in Album {owned}/{denom}" if denom > 0 else "Own/in Album 0/0"
        gr_line = f"Goodread Review {gr_count}/{denom}" if denom > 0 else "Goodread Review 0/0"

        read_color = (
            QColor(46, 204, 113) if (denom > 0 and read_count >= denom)
            else (QColor(243, 156, 18) if read_count > 0 else QColor(127, 140, 141))
        )

        expected_known = self.expected_store.get(album_dir) > 0
        own_color = (
            QColor(46, 204, 113) if (expected_known and denom > 0 and owned >= denom)
            else (QColor(52, 152, 219) if owned > 0 else QColor(127, 140, 141))
        )

        gr_color = (
            QColor(46, 204, 113) if (denom > 0 and gr_count >= denom)
            else (QColor(243, 156, 18) if gr_count > 0 else QColor(127, 140, 141))
        )

        artist_cover = None
        try:
            artist_cover = find_cover_in_folder(
                album_dir.parent,
                preferred_stems=["artist", "artist-cover", "cover", "folder", "front"],
            )
        except Exception:
            artist_cover = None

        cover_icon = self._album_icon(album_dir, artist_cover=artist_cover)
        icon = self._album_tile_icon_with_strip(
            cover_icon,
            self.albums.iconSize(),
            read_line=read_line,
            read_color=read_color,
            own_line=own_line,
            own_color=own_color,
            gr_line=gr_line,
            gr_color=gr_color,
        )

        it.setIcon(icon)
        it.setToolTip(f"{album_dir}\n{own_line}\n{read_line}\n{gr_line}")

    def open_artist(self, item: QListWidgetItem) -> None:
        artist_dir = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        if not artist_dir.exists():
            return
        self.current_artist_dir = artist_dir
        self.current_album_dir = None
        self.load_albums(artist_dir)
        self._go_to(self.PAGE_ALBUMS)

    def open_album(self, item: QListWidgetItem) -> None:
        album_dir = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        if not album_dir.exists():
            return
        self.current_album_dir = album_dir
        self.load_tracks(album_dir)
        self._go_to(self.PAGE_TRACKS)

    def _row_path(self, row: int) -> Path | None:
        it = self.tracks.item(row, self.COL_NO)
        if not it:
            return None
        p = it.data(Qt.UserRole)
        return Path(str(p)) if p else None

    def _find_row_by_path(self, target: Path) -> int | None:
        for row in range(self.tracks.rowCount()):
            rp = self._row_path(row)
            if rp and rp == target:
                return row
        return None

    def _select_row_and_load(self, row: int) -> None:
        if row < 0 or row >= self.tracks.rowCount():
            return
        p = self._row_path(row)
        if not p:
            return

        self._selected_track_path = p
        self._suppress_track_selection_handler = True
        try:
            with signal_blocker(self.tracks):
                self.tracks.setCurrentCell(row, self.COL_TITLE)
                self.tracks.selectRow(row)
        finally:
            self._suppress_track_selection_handler = False

        self.reload_selected_into_editor()

    def _refresh_current_album_after_file_change(self, new_path: Path) -> None:
        if not self.current_album_dir or not self.current_album_dir.exists():
            self._selected_track_path = new_path
            self.reload_selected_into_editor()
            return

        self._refresh_album_cache(self.current_album_dir)
        self._selected_track_path = new_path
        self.load_tracks(self.current_album_dir, select_path=new_path)

        self._update_single_album_item(self.current_album_dir)
        if self.current_artist_dir:
            self._update_single_artist_item(self.current_artist_dir)

    def load_tracks(self, album_dir: Path, select_path: Path | None = None) -> None:
        self._loading_tracks = True
        try:
            tracks = self._album_mp3s(album_dir)
            self._album_meta_cache = {}

            cur_expected = self.expected_store.get(album_dir)
            found_total = 0

            decorated: list[tuple[int, int, str, Path, TrackMeta, float | None]] = []
            for p in tracks:
                meta, length_sec = read_meta_and_length(p)
                self._album_meta_cache[str(p)] = meta

                if cur_expected <= 0 and getattr(meta, "totaltracks", 0) > 0:
                    found_total = max(found_total, int(meta.totaltracks))

                discno = int(getattr(meta, "discno", 0) or 0)
                disc_key = discno if discno > 0 else 1
                trackno = int(getattr(meta, "trackno", 0) or 0)
                track_key = trackno if trackno > 0 else 10_000

                decorated.append((disc_key, track_key, p.name.lower(), p, meta, length_sec))

            if cur_expected <= 0 and found_total > 0:
                self.expected_store.set(album_dir, found_total)
                cur_expected = found_total

            decorated.sort(key=lambda t: (t[0], t[1], t[2]))

            self.current_track_count = len(decorated)
            self.current_album_expected = cur_expected

            self.album_progress_label.setText(
                f"Tracks: {self.current_track_count} | Expected: {self.current_album_expected or '-'}"
            )

            album_cover = find_cover_in_folder(album_dir, preferred_stems=["cover", "folder", "front", "album"])
            artist_cover = None
            if self.current_artist_dir:
                artist_cover = find_cover_in_folder(
                    self.current_artist_dir, preferred_stems=["artist", "artist-cover", "cover", "folder", "front"]
                )

            self.tracks.setRowCount(len(decorated))
            for row_i, (_disc_k, _trk_k, _n, p, meta, length_sec) in enumerate(decorated):
                st = p.stat()

                item_no = QTableWidgetItem()
                item_no.setData(Qt.EditRole, int(meta.trackno if getattr(meta, "trackno", 0) > 0 else row_i + 1))
                item_no.setData(Qt.UserRole, str(p))
                item_no.setFlags(item_no.flags() & ~Qt.ItemIsEditable)
                self.tracks.setItem(row_i, self.COL_NO, item_no)

                title = meta.title.strip() if meta.title.strip() else p.stem
                it_title = QTableWidgetItem(title)
                it_title.setFlags(it_title.flags() & ~Qt.ItemIsEditable)
                it_title.setToolTip(str(p))
                it_title.setIcon(self._track_icon(p, album_cover, artist_cover))
                self.tracks.setItem(row_i, self.COL_TITLE, it_title)

                def ro(text: str) -> QTableWidgetItem:
                    itx = QTableWidgetItem(text)
                    itx.setFlags(itx.flags() & ~Qt.ItemIsEditable)
                    return itx

                self.tracks.setItem(row_i, self.COL_ARTIST, ro(meta.artist))
                self.tracks.setItem(row_i, self.COL_ALBUM, ro(meta.album))
                self.tracks.setItem(row_i, self.COL_ALBUMARTIST, ro(meta.albumartist))
                self.tracks.setItem(row_i, self.COL_YEAR, ro(meta.year))
                self.tracks.setItem(row_i, self.COL_GENRE, ro(meta.genre))
                self.tracks.setItem(row_i, self.COL_LEN, ro(fmt_len(length_sec)))
                self.tracks.setItem(row_i, self.COL_SIZE, ro(fmt_size_mb(int(st.st_size))))
                self.tracks.setItem(row_i, self.COL_MOD, ro(fmt_mtime(int(st.st_mtime))))

            self.tracks.resizeColumnsToContents()

        finally:
            self._loading_tracks = False

        if self.tracks.rowCount() <= 0:
            self._selected_track_path = None
            return

        target_row = 0
        if select_path is not None:
            found_row = self._find_row_by_path(select_path)
            if found_row is not None:
                target_row = found_row

        self._select_row_and_load(target_row)

    def on_current_cell_changed(self, currentRow: int, *_args) -> None:
        if self._loading_tracks or self._suppress_track_selection_handler:
            return
        if currentRow < 0:
            return
        self._select_row_and_load(currentRow)

    def _validate_unique_trackno(self, path: Path, meta: TrackMeta) -> bool:
        tno = int(getattr(meta, "trackno", 0) or 0)
        if tno <= 0:
            return True

        disc = int(getattr(meta, "discno", 0) or 0)

        for p_str, m in self._album_meta_cache.items():
            if Path(p_str) == path:
                continue
            other_tno = int(getattr(m, "trackno", 0) or 0)
            other_disc = int(getattr(m, "discno", 0) or 0)
            if other_tno == tno and other_disc == disc:
                QMessageBox.warning(
                    self,
                    "Duplicate track number",
                    f"Track #{tno} on disc {disc or 1} is already used by:\n{Path(p_str).name}\n\n"
                    "Choose a different track number.",
                )
                return False

        return True

    def reload_selected_into_editor(self) -> None:
        if not self._selected_track_path or not self._selected_track_path.exists():
            return

        meta, _len = read_meta_and_length(self._selected_track_path)

        self.ed_title.setText((meta.title or "").strip())
        self.ed_artist.setText((meta.artist or "").strip())
        self.ed_album.setText((meta.album or "").strip())
        self.ed_albumartist.setText((meta.albumartist or "").strip())
        self.ed_year.setText((meta.year or "").strip())
        self.ed_genre.setText((meta.genre or "").strip())
        self.ed_composer.setText((getattr(meta, "composer", "") or "").strip())
        self.ed_comment.setPlainText(getattr(meta, "comment", "") or "")

        self.sp_trackno.setValue(int(getattr(meta, "trackno", 0) or 0))
        self.sp_totaltracks.setValue(int(getattr(meta, "totaltracks", 0) or self.current_album_expected or 0))
        self.sp_discno.setValue(int(getattr(meta, "discno", 0) or 0))

        self.ed_directory.setText(str(self._selected_track_path))
        self.ed_directory.setToolTip(str(self._selected_track_path))

        self.chk_watched.blockSignals(True)
        self.chk_goodreads.blockSignals(True)
        self.chk_audible.blockSignals(True)
        try:
            self.chk_watched.setChecked(self.watched_store.is_watched(self._selected_track_path))
            self.chk_goodreads.setChecked(self.review_store.goodreads_done(self._selected_track_path))
            self.chk_audible.setChecked(self.review_store.audible_done(self._selected_track_path))
        finally:
            self.chk_watched.blockSignals(False)
            self.chk_goodreads.blockSignals(False)
            self.chk_audible.blockSignals(False)

        self._refresh_cover_for_track(self._selected_track_path)

    def _editor_meta(self) -> TrackMeta:
        return TrackMeta(
            title=self.ed_title.text().strip(),
            artist=self.ed_artist.text().strip(),
            album=self.ed_album.text().strip(),
            albumartist=self.ed_albumartist.text().strip(),
            year=self.ed_year.text().strip(),
            genre=self.ed_genre.text().strip(),
            composer=self.ed_composer.text().strip(),
            comment=self.ed_comment.toPlainText().strip(),
            discno=int(self.sp_discno.value()),
            trackno=int(self.sp_trackno.value()),
            totaltracks=int(self.sp_totaltracks.value()),
        )

    def save_metadata_for_selected(self) -> None:
        if not self._selected_track_path:
            return

        p = self._selected_track_path
        if not p.exists():
            QMessageBox.warning(self, "Missing", "Selected track no longer exists.")
            return

        meta = self._editor_meta()

        if not self._validate_unique_trackno(p, meta):
            return

        if getattr(meta, "totaltracks", 0) <= 0 and self.current_album_expected > 0:
            meta.totaltracks = self.current_album_expected  # type: ignore[attr-defined]

        try:
            write_metadata_to_file(p, meta)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return

        new_path = p
        if self.chk_autorename.isChecked():
            try:
                new_path = rename_track_file(p, meta, rename_sidecar=self.chk_renamesidecar.isChecked())
                if new_path != p:
                    self.watched_store.move(p, new_path)
                    self.review_store.move_track(p, new_path)

                    if str(p) in self._embedded_cache:
                        self._embedded_cache[str(new_path)] = self._embedded_cache.pop(str(p))
            except Exception as e:
                QMessageBox.warning(self, "Rename failed", str(e))
                new_path = p

        self._refresh_current_album_after_file_change(new_path)
        self.status(f"Saved tags{' + renamed' if new_path != p else ''}: {new_path.name}")

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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._selected_track_path:
            self._refresh_cover_for_track(self._selected_track_path)

    def _refresh_cover_for_track(self, track_path: Path) -> None:
        side = find_track_sidecar_cover(track_path)
        if side and side.exists():
            self._set_cover_preview_path(side)
            return

        blob = self._embedded_bytes(track_path)
        if blob:
            self._set_cover_preview_bytes(blob)
            return

        album_dir = track_path.parent
        album_cover = find_cover_in_folder(album_dir, preferred_stems=["cover", "folder", "front", "album"])
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

        try:
            embed_cover_into_file(p, Path(img))
        except Exception as e:
            QMessageBox.critical(self, "Cover failed", str(e))
            return

        self._embedded_cache.pop(str(p), None)
        self._refresh_cover_for_track(p)
        if self.current_album_dir:
            self._update_single_album_item(self.current_album_dir)
        if self.current_artist_dir:
            self._update_single_artist_item(self.current_artist_dir)

        self.status("Embedded cover into MP3.")

    def pick_library_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select your library root (Artist/Album/Tracks)")
        if not folder:
            return
        root = Path(folder).expanduser().resolve()
        if not root.exists():
            QMessageBox.warning(self, "Not found", "Folder does not exist.")
            return

        self.library_root = root
        self.current_artist_dir = None
        self.current_album_dir = None
        self._selected_track_path = None

        self._go_to(self.PAGE_ARTISTS)
        self.start_scan()

    def go_back(self) -> None:
        page = self.stack.currentIndex()
        if page == self.PAGE_TRACKS:
            self._go_to(self.PAGE_ALBUMS)
        elif page == self.PAGE_ALBUMS:
            self._go_to(self.PAGE_ARTISTS)

    def _go_to(self, page: int) -> None:
        self.stack.setCurrentIndex(page)
        self.act_back.setEnabled(page != self.PAGE_ARTISTS)
        self.act_set_album_total.setEnabled(page == self.PAGE_ALBUMS)

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
        a_set = menu.addAction("Set expected total tracks…")
        a_clear = menu.addAction("Clear expected total")
        menu.addSeparator()
        menu.addAction(f"Owned tracks in folder: {owned}")

        act = menu.exec(self.albums.mapToGlobal(pos))
        if act == a_set:
            self._prompt_set_expected(album_dir, cur, owned)
        elif act == a_clear:
            self.expected_store.clear(album_dir)
            self._update_single_album_item(album_dir)
            if self.current_artist_dir:
                self._update_single_artist_item(self.current_artist_dir)

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
        if self.current_artist_dir:
            self._update_single_artist_item(self.current_artist_dir)
        self.status(f"Expected total set to {val} for {album_dir.name}")