from __future__ import annotations

import json
import os
from .atomic_json import write_json_atomic as _write_json_atomic
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call
from .portable_track_state import APP_AUDIBLE, APP_GOODREADS, read_bool_flag, write_bool_flag


class ReviewStore:
    @debug_call()
    def __init__(self, config_directory: Path | None = None) -> None:
        self._config_directory = Path(config_directory) if config_directory is not None else None
        self._legacy: dict[str, dict[str, bool]] = self._load_legacy()
        self._cache_goodreads: dict[str, bool] = {}
        self._cache_audible: dict[str, bool] = {}

    @debug_call()
    def config_dir(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        d = self._config_directory if self._config_directory is not None else base / "booklibconnect"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @debug_call()
    def file_path(self) -> Path:
        return self.config_dir() / "reviews.json"

    @debug_call()
    def _load_legacy(self) -> dict[str, dict[str, bool]]:
        p = self.file_path()
        if not p.exists():
            dbg("reviews.json missing: %s", p)
            return {}

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out: dict[str, dict[str, bool]] = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    gr = False
                    audible = False

                    if isinstance(v, dict):
                        gr = bool(v.get("goodreads", v.get("goodreads_done", False)))
                        audible = bool(v.get("audible", v.get("audible_done", False)))
                    else:
                        gr = bool(v)

                    out[str(k)] = {"goodreads": gr, "audible": audible}

            dbg("Loaded reviews.json legacy entries: %d", len(out))
            return out
        except Exception:
            dbg_exc(f"Failed loading reviews.json from {p}")
            return {}

    @debug_call()
    def _save_legacy(self) -> None:
        p = self.file_path()
        try:
            _write_json_atomic(p, self._legacy)
        except Exception:
            dbg_exc(f"Failed saving reviews.json to {p}")

    def legacy_snapshot(self) -> dict[str, dict[str, bool]]:
        """Return stable fallback values for the background scan."""
        return {
            path: {
                "goodreads": bool(values.get("goodreads", False)),
                "audible": bool(values.get("audible", False)),
            }
            for path, values in self._legacy.items()
        }

    def legacy_flags(self, path: Path) -> tuple[bool, bool]:
        values = self._legacy.get(str(path), {})
        return bool(values.get("goodreads")), bool(values.get("audible"))

    def prime_scan_cache(
        self,
        goodreads: dict[str, bool],
        audible: dict[str, bool],
    ) -> None:
        """Replace the RAM caches with values from a completed scan."""
        self._cache_goodreads = {
            str(path): bool(value)
            for path, value in goodreads.items()
        }
        self._cache_audible = {
            str(path): bool(value)
            for path, value in audible.items()
        }

    def prime_track_cache(self, track_path: Path, *, goodreads: bool, audible: bool) -> None:
        """Refresh one inspected book without resetting other unsaved scan values."""
        self._cache_goodreads[str(track_path)] = bool(goodreads)
        self._cache_audible[str(track_path)] = bool(audible)

    def cached_done(self, track_path: Path, kind: str) -> bool:
        """Paint review markers without opening MP3s or querying file status."""
        cache = {'goodreads': self._cache_goodreads, 'audible': self._cache_audible}[kind]
        return bool(cache.get(str(track_path), False))

    def _legacy_goodreads(self, track_path: Path) -> bool:
        return bool(self._legacy.get(str(track_path), {}).get("goodreads", False))

    def _legacy_audible(self, track_path: Path) -> bool:
        return bool(self._legacy.get(str(track_path), {}).get("audible", False))

    def _set_legacy(self, track_path: Path, *, goodreads: bool | None = None, audible: bool | None = None) -> None:
        k = str(track_path)
        cur = dict(self._legacy.get(k, {"goodreads": False, "audible": False}))
        if goodreads is not None:
            cur["goodreads"] = bool(goodreads)
        if audible is not None:
            cur["audible"] = bool(audible)
        self._legacy[k] = cur
        self._save_legacy()

    @debug_call()
    def goodreads_done(self, track_path: Path) -> bool:
        if not track_path.exists():
            return False

        k = str(track_path)
        if k in self._cache_goodreads:
            return self._cache_goodreads[k]

        embedded = read_bool_flag(track_path, APP_GOODREADS)
        if embedded is not None:
            self._cache_goodreads[k] = embedded
            return embedded

        legacy = self._legacy_goodreads(track_path)
        self._cache_goodreads[k] = legacy

        if legacy:
            try:
                write_bool_flag(track_path, APP_GOODREADS, True)
            except Exception:
                dbg_exc(f"Legacy Goodreads migration failed for {track_path}")

        return legacy

    @debug_call()
    def audible_done(self, track_path: Path) -> bool:
        if not track_path.exists():
            return False

        k = str(track_path)
        if k in self._cache_audible:
            return self._cache_audible[k]

        embedded = read_bool_flag(track_path, APP_AUDIBLE)
        if embedded is not None:
            self._cache_audible[k] = embedded
            return embedded

        legacy = self._legacy_audible(track_path)
        self._cache_audible[k] = legacy

        if legacy:
            try:
                write_bool_flag(track_path, APP_AUDIBLE, True)
            except Exception:
                dbg_exc(f"Legacy Audible migration failed for {track_path}")

        return legacy

    @debug_call()
    def set_goodreads_done(self, track_path: Path, done: bool) -> None:
        if not track_path.exists():
            return

        k = str(track_path)
        done = bool(done)

        try:
            ok = write_bool_flag(track_path, APP_GOODREADS, done)
            dbg("Embedded goodreads=%s for %s ok=%s", done, track_path, ok)
        except Exception:
            dbg_exc(f"set_goodreads_done embedded write failed for {track_path}")

        self._cache_goodreads[k] = done
        self._set_legacy(track_path, goodreads=done)

    @debug_call()
    def set_audible_done(self, track_path: Path, done: bool) -> None:
        if not track_path.exists():
            return

        k = str(track_path)
        done = bool(done)

        try:
            ok = write_bool_flag(track_path, APP_AUDIBLE, done)
            dbg("Embedded audible=%s for %s ok=%s", done, track_path, ok)
        except Exception:
            dbg_exc(f"set_audible_done embedded write failed for {track_path}")

        self._cache_audible[k] = done
        self._set_legacy(track_path, audible=done)

    @debug_call()
    def count_goodreads(self, paths: list[Path]) -> int:
        n = 0
        for p in paths:
            if self.goodreads_done(p):
                n += 1
        return n

    @debug_call()
    def move_track(self, old_path: Path, new_path: Path) -> None:
        old_k = str(old_path)
        new_k = str(new_path)

        if old_k in self._cache_goodreads:
            self._cache_goodreads[new_k] = self._cache_goodreads.pop(old_k)
        if old_k in self._cache_audible:
            self._cache_audible[new_k] = self._cache_audible.pop(old_k)

        if old_k in self._legacy:
            self._legacy[new_k] = self._legacy.pop(old_k)
            self._save_legacy()
