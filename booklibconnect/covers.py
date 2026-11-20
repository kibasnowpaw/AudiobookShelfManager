from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from .portable_track_state import read_tags_for_book_cover

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


ALBUM_COVER_STEMS = ("cover", "folder", "front", "album")


class CoverDirectory:
    """A short-lived index of image names for one series or author directory.

    Names are reused only while the directory is unchanged. File contents and
    image timestamps are never cached here. The caller owns its lifetime.
    """
    def __init__(self, folder):
        self.folder = Path(folder)
        self._stamp = None

    @staticmethod
    def _directory_stamp(st):
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def _list_entries(self, stamp, audio_extensions=()):
        by_name, by_stem = {}, {}
        first = None
        tracks = []
        with os.scandir(self.folder) as entries:
            for entry in entries:
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in audio_extensions:
                    if entry.is_file():
                        tracks.append(self.folder / entry.name)
                elif ext in IMG_EXTS:
                    try:
                        if not entry.is_file():
                            continue
                    except OSError:
                        if not audio_extensions:
                            raise
                        # An unreadable picture must not prevent audio discovery.
                        # Refresh the artwork independently during validation.
                        stamp = None
                        continue
                    path = self.folder / entry.name
                    by_name[entry.name] = path
                    by_stem.setdefault(path.stem.lower(), path)
                    if first is None or entry.name.lower() < first.name.lower():
                        first = path
        self._by_name, self._by_stem, self._first = by_name, by_stem, first
        self._stamp = stamp
        return tracks

    def discover_tracks(self, extensions):
        """One directory listing provides both audio files and cover names."""
        try:
            stamp = self._directory_stamp(self.folder.stat())
        except OSError:
            stamp = None
        tracks = self._list_entries(stamp, extensions)
        return sorted(tracks, key=lambda path: path.name.lower())

    def refresh(self):
        st = self.folder.stat()
        stamp = self._directory_stamp(st)
        if stamp != self._stamp:
            self._list_entries(stamp)
        return st

    def _by_basename(self, stem):
        for ext in IMG_EXTS:
            path = self._by_name.get(stem + ext)
            if path is not None:
                return path
        return self._by_stem.get(stem.lower())

    def _choose(self, stems, fallback):
        for stem in stems:
            path = self._by_basename(stem)
            if path is not None:
                return path
        return self._first if fallback else None

    def _select(self, stems, fallback):
        path = self._choose(stems, fallback)
        if path is not None and not path.is_file():
            # Also handle deletions on coarse-timestamp filesystems.
            self._stamp = None
            self.refresh()
            path = self._choose(stems, fallback)
        return path

    def signature_sources(self, track_stem):
        """Read each selected image's current stamp once, even for a shared path."""
        for attempt in range(2):
            directory_stat = self.refresh()
            sidecar = self._choose((track_stem,), False)
            folder_cover = self._choose(ALBUM_COVER_STEMS, True)
            stats = {}
            try:
                for path in (sidecar, folder_cover):
                    if path is not None and path not in stats:
                        st = path.stat()
                        if not stat.S_ISREG(st.st_mode):
                            raise FileNotFoundError
                        stats[path] = st
            except (FileNotFoundError, NotADirectoryError):
                if attempt:
                    raise
                self._stamp = None
                continue
            return directory_stat, [(p, stats.get(p)) for p in (sidecar, folder_cover)]

    def for_stem(self, stem):
        return self._select((stem,), False)

    def preferred(self, stems):
        return self._select(stems, True)


def find_cover_in_folder(folder: Path, preferred_stems: list[str]) -> Path | None:
    if preferred_stems:
        for ext in IMG_EXTS:
            exact = folder / (preferred_stems[0] + ext)
            if exact.is_file():
                return exact
    index = CoverDirectory(folder)
    try:
        index.refresh()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return index.preferred(preferred_stems)


def find_track_sidecar_cover(track_file: Path) -> Path | None:
    for ext in IMG_EXTS:
        exact = track_file.with_suffix(ext)
        if exact.is_file():
            return exact
    index = CoverDirectory(track_file.parent)
    try:
        index.refresh()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return index.for_stem(track_file.stem)


def get_embedded_cover_bytes(track_file: Path, *, prime_portable=False) -> Optional[bytes]:
    """
    Return embedded cover bytes from a supported audio file if present.
    Supports MP3 (ID3/APIC), M4A/M4B (MP4 covr), and FLAC pictures.
    Returns None if no embedded art is found.
    """
    ext = track_file.suffix.lower()

    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3

            id3 = read_tags_for_book_cover(track_file) if prime_portable else ID3(track_file)
            for frame in id3.values():
                # APIC frame(s) hold embedded artwork
                if getattr(frame, "FrameID", "") == "APIC":
                    data = getattr(frame, "data", None)
                    if isinstance(data, (bytes, bytearray)) and data:
                        return bytes(data)
            return None

        if ext in {".m4a", ".m4b"}:
            from mutagen.mp4 import MP4

            tags = (read_tags_for_book_cover(track_file) if prime_portable and ext == ".m4b"
                    else (MP4(track_file).tags or {}))
            covr = tags.get("covr")
            if isinstance(covr, list) and covr:
                first = covr[0]
                if first:
                    return bytes(first)
            return None

        if ext == ".flac":
            from mutagen.flac import FLAC

            flac = FLAC(track_file)
            pics = getattr(flac, "pictures", None) or []
            if pics:
                data = getattr(pics[0], "data", None)
                if isinstance(data, (bytes, bytearray)) and data:
                    return bytes(data)
            return None

    except Exception:
        # Keep this helper quiet; app.py does the user-facing logging.
        return None

    return None
