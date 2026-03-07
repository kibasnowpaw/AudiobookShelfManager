from __future__ import annotations

import json
import os
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call


class WatchedStore:
    """
    Stores watched/read status per track path.
    JSON file: ~/.config/booklibconnect/watched.json (or XDG_CONFIG_HOME)
    """

    @debug_call()
    def __init__(self) -> None:
        self._data: dict[str, int] = self._load()

    @debug_call()
    def config_dir(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        d = base / "booklibconnect"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @debug_call()
    def file_path(self) -> Path:
        return self.config_dir() / "watched.json"

    @debug_call()
    def _load(self) -> dict[str, int]:
        p = self.file_path()
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out: dict[str, int] = {}
                for k, v in raw.items():
                    try:
                        out[str(k)] = 1 if int(v) else 0
                    except Exception:
                        continue
                return out
        except Exception:
            dbg_exc(f"Failed loading watched from {p}")
        return {}

    @debug_call()
    def save(self) -> None:
        p = self.file_path()
        try:
            p.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            dbg_exc(f"Failed saving watched to {p}")

    @debug_call()
    def is_watched(self, track_path: Path) -> bool:
        return bool(self._data.get(str(track_path), 0))

    @debug_call()
    def set_watched(self, track_path: Path, watched: bool) -> None:
        k = str(track_path)
        if watched:
            self._data[k] = 1
        else:
            self._data.pop(k, None)
        self.save()

    @debug_call()
    def move(self, old_path: Path, new_path: Path) -> None:
        """If a track is renamed, keep watched state."""
        ok = str(old_path)
        nk = str(new_path)
        if ok in self._data:
            self._data[nk] = self._data.get(ok, 1)
            self._data.pop(ok, None)
            self.save()

    @debug_call()
    def count_watched(self, tracks: list[Path]) -> int:
        return sum(1 for p in tracks if self.is_watched(p))
