from __future__ import annotations

import hashlib
import json
import math
import os
from .atomic_json import write_bytes_atomic
from dataclasses import dataclass, fields
from pathlib import Path

from .debug_tools import dbg, dbg_exc
from .models import TrackMeta


INDEX_VERSION = 5
_META_FIELDS = tuple(field.name for field in fields(TrackMeta))
_INTEGER_META_FIELDS = {"discno", "trackno", "totaltracks"}


def _config_dir() -> Path:
    base = Path(
        os.environ.get(
            "XDG_CONFIG_HOME",
            str(Path.home() / ".config"),
        )
    )
    directory = base / "booklibconnect"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _optional_finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def _metadata_from_json(raw: object) -> TrackMeta | None:
    if not isinstance(raw, dict):
        return None

    values: dict[str, object] = {}
    for name in _META_FIELDS:
        value = raw.get(name, 0 if name in _INTEGER_META_FIELDS else "")

        if name in _INTEGER_META_FIELDS:
            try:
                values[name] = max(0, int(value or 0))
            except (TypeError, ValueError, OverflowError):
                values[name] = 0
        else:
            values[name] = str(value or "")

    try:
        return TrackMeta(**values)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CachedTrack:
    """Everything the UI needs for one unchanged audiobook file."""

    size: int
    mtime_ns: int
    ctime_ns: int
    metadata: TrackMeta
    length_seconds: float | None
    watched: bool
    goodreads: bool
    audible: bool
    portable_id_ready: bool
    cover_checked: bool = False
    cover_signature: str = ""
    cover_issue: dict[str, str] | None = None
    track_id: str = ""

    @classmethod
    def from_file(
        cls,
        path: Path,
        metadata: TrackMeta,
        length_seconds: float | None,
        *,
        watched: bool,
        goodreads: bool,
        audible: bool,
        portable_id_ready: bool,
        track_id: str = "",
    ) -> "CachedTrack":
        stat_result = path.stat()
        return cls(
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
            ctime_ns=int(stat_result.st_ctime_ns),
            metadata=metadata,
            length_seconds=_optional_finite_float(length_seconds),
            watched=bool(watched),
            goodreads=bool(goodreads),
            audible=bool(audible),
            portable_id_ready=bool(portable_id_ready),
            track_id=str(track_id or ""),
        )

    def matches_stat(self, stat_result: os.stat_result) -> bool:
        return (
            self.size == int(stat_result.st_size)
            and self.mtime_ns == int(stat_result.st_mtime_ns)
            and self.ctime_ns == int(stat_result.st_ctime_ns)
        )

    def matches_file(self, path: Path) -> bool:
        try:
            return self.matches_stat(path.stat())
        except OSError:
            return False

    def to_json(self) -> dict[str, object]:
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "metadata": {name: getattr(self.metadata, name) for name in _META_FIELDS},
            "length_seconds": self.length_seconds,
            "watched": self.watched,
            "goodreads": self.goodreads,
            "audible": self.audible,
            "portable_id_ready": self.portable_id_ready,
            "track_id": self.track_id,
            "cover_checked": self.cover_checked,
            "cover_signature": self.cover_signature,
            "cover_issue": self.cover_issue,
        }

    @classmethod
    def from_json(cls, raw: object) -> "CachedTrack | None":
        if not isinstance(raw, dict):
            return None

        metadata = _metadata_from_json(raw.get("metadata"))
        if metadata is None:
            return None

        try:
            size = int(raw.get("size", -1))
            mtime_ns = int(raw.get("mtime_ns", -1))
            ctime_ns = int(raw.get("ctime_ns", -1))
        except (TypeError, ValueError, OverflowError):
            return None

        watched = _optional_bool(raw.get("watched"))
        goodreads = _optional_bool(raw.get("goodreads"))
        audible = _optional_bool(raw.get("audible"))
        portable_id_ready = _optional_bool(raw.get("portable_id_ready"))

        if (
            size < 0
            or mtime_ns < 0
            or ctime_ns < 0
            or watched is None
            or goodreads is None
            or audible is None
            or portable_id_ready is None
        ):
            return None

        return cls(
            size=size,
            mtime_ns=mtime_ns,
            ctime_ns=ctime_ns,
            metadata=metadata,
            length_seconds=_optional_finite_float(raw.get("length_seconds")),
            watched=watched,
            goodreads=goodreads,
            audible=audible,
            portable_id_ready=portable_id_ready,
            track_id=str(raw.get("track_id") or ""),
            cover_checked=(raw.get("cover_checked") is True),
            cover_signature=str(raw.get("cover_signature") or ""),
            cover_issue=(
                {str(key): str(value) for key, value in raw["cover_issue"].items()}
                if isinstance(raw.get("cover_issue"), dict)
                and isinstance(raw["cover_issue"].get("reason"), str)
                else None
            ),
        )


class ScanIndexStore:
    """Persistent, validated index used by incremental library scans."""

    def __init__(self, config_directory: Path | None = None) -> None:
        self._config_directory = Path(config_directory) if config_directory is not None else None
        self._disk_stamp = self._digest = None
        self._entries = self._load()

    def file_path(self) -> Path:
        directory = self._config_directory if self._config_directory is not None else _config_dir()
        return directory / "scan_index.json"

    def entries(self) -> dict[str, CachedTrack]:
        return dict(self._entries)

    def unchanged(
        self,
        path: Path,
        stat_result: os.stat_result,
    ) -> CachedTrack | None:
        cached = self._entries.get(str(path))
        if cached is None or not cached.matches_stat(stat_result):
            return None
        return cached

    def _load(self) -> dict[str, CachedTrack]:
        path = self.file_path()
        if not path.exists():
            dbg("scan_index.json missing: %s", path)
            return {}

        try:
            with path.open("rb") as handle:
                contents = handle.read()
                stamp = _stamp(os.fstat(handle.fileno()))
            raw = json.loads(contents)
            # Older metadata/cover checks remain reusable. Missing IDs are
            # read once in the scan worker, then retained in version 5.
            if not isinstance(raw, dict) or raw.get("version") not in (3, 4, INDEX_VERSION):
                dbg("Ignoring unsupported scan index: %s", path)
                return {}

            raw_tracks = raw.get("tracks")
            if not isinstance(raw_tracks, dict):
                return {}

            entries: dict[str, CachedTrack] = {}
            for raw_path, raw_entry in raw_tracks.items():
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                entry = CachedTrack.from_json(raw_entry)
                if entry is not None:
                    entries[raw_path] = entry

            self._disk_stamp, self._digest = stamp, hashlib.sha256(contents).digest()
            dbg("Loaded persistent scan index: %d tracks", len(entries))
            return entries

        except Exception:
            dbg_exc(f"Failed loading persistent scan index from {path}")
            return {}

    def save(
        self,
        roots: list[Path],
        entries: dict[str, CachedTrack],
    ) -> bool:
        """Return whether this call actually rewrote the index file."""
        payload = {
            "version": INDEX_VERSION,
            "library_roots": [str(path) for path in roots],
            "tracks": {
                path: entry.to_json()
                for path, entry in sorted(entries.items())
            },
        }
        # Compare the serialized snapshot, not mutable TrackMeta object identities.
        # If the file disappeared, was replaced, or an old index needs upgrading,
        # always write a fresh complete index.
        path = self.file_path()
        contents = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        digest = hashlib.sha256(contents).digest()
        try:
            unchanged = digest == self._digest and _stamp(path.stat()) == self._disk_stamp
        except OSError:
            unchanged = False
        if not unchanged:
            write_bytes_atomic(path, contents)
            self._disk_stamp, self._digest = _stamp(path.stat()), digest
        self._entries = dict(entries)
        dbg("Saved persistent scan index: %d tracks", len(entries))
        return not unchanged
