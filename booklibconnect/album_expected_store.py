from __future__ import annotations

import json
import os
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call


class AlbumExpectedStore:
    @debug_call()
    def __init__(self) -> None:
        self._data: dict[str, int] = self._load()

    @property
    def data(self) -> dict[str, int]:
        return self._data

    @debug_call()
    def config_dir(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        d = base / "booklibconnect"
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
        p = self.file_path()
        p.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        dbg("Saved album_expected.json (%d entries) to %s", len(self._data), p)

    @debug_call()
    def get(self, album_dir: Path) -> int:
        v = int(self._data.get(str(album_dir), 0) or 0)
        dbg("AlbumExpectedStore.get(%s) -> %d", album_dir, v)
        return v

    @debug_call()
    def set(self, album_dir: Path, total: int) -> None:
        if total <= 0:
            self._data.pop(str(album_dir), None)
        else:
            self._data[str(album_dir)] = int(total)
        self.save()

    @debug_call()
    def clear(self, album_dir: Path) -> None:
        self._data.pop(str(album_dir), None)
        self.save()
