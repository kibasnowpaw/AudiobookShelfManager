from __future__ import annotations

import json
import os
from threading import RLock
from .atomic_json import write_json_atomic as _write_json_atomic
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call


class AlbumExpectedStore:
    @debug_call()
    def __init__(self, config_directory: Path | None = None) -> None:
        self._config_directory = Path(config_directory) if config_directory is not None else None
        self._lock = RLock()
        self._data: dict[str, int] = self._load()

    @property
    def data(self) -> dict[str, int]:
        return dict(self._data)

    @debug_call()
    def config_dir(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        d = self._config_directory if self._config_directory is not None else base / "booklibconnect"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @debug_call()
    def file_path(self) -> Path:
        return self.config_dir() / "album_expected.json"

    @debug_call()
    def _load(self) -> dict[str, int]:
        p = self.file_path()
        if not p.exists():
            dbg("album_expected.json missing: %s", p)
            return {}

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: dict[str, int] = {}
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except Exception:
                        dbg("Skipping invalid expected entry: %r=%r", k, v)
                dbg("Loaded album_expected (%d entries)", len(out))
                return out
        except Exception:
            dbg_exc(f"Failed loading album_expected from {p}")

        return {}

    @debug_call()
    def save(self) -> None:
        with self._lock:
            _write_json_atomic(self.file_path(), self._data)

    @debug_call()
    def get(self, album_dir: Path) -> int:
        v = int(self._data.get(str(album_dir), 0) or 0)
        dbg("AlbumExpectedStore.get(%s) -> %d", album_dir, v)
        return v

    @debug_call()
    def set(self, album_dir: Path, total: int) -> None:
        with self._lock:
            updated = dict(self._data)
            if total > 0:
                updated[str(album_dir)] = int(total)
            else:
                updated.pop(str(album_dir), None)
            self._commit(updated)

    def fill_missing(self, totals: dict[str, int]) -> None:
        """Commit scan discoveries once, preserving any manually set totals."""
        with self._lock:
            updated = dict(self._data)
            for path, total in totals.items():
                if total > 0 and updated.get(path, 0) <= 0:
                    updated[path] = int(total)
            self._commit(updated)

    def _commit(self, updated: dict[str, int]) -> None:
        if updated != self._data:
            _write_json_atomic(self.file_path(), updated)
            self._data = updated  # Publish only after the atomic write succeeds.

    @debug_call()
    def clear(self, album_dir: Path) -> None:
        self.set(album_dir, 0)
