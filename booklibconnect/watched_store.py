from __future__ import annotations

import json
import os
from .atomic_json import write_json_atomic as _write_json_atomic
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call
from .portable_track_state import APP_WATCHED, read_bool_flag, write_bool_flag


class WatchedStore:
    @debug_call()
    def __init__(self) -> None:
        self._legacy: dict[str, bool] = self._load_legacy()
        self._cache: dict[str, bool] = {}

    @debug_call()
    def config_dir(self) -> Path:
        base = Path(
            os.environ.get(
                "XDG_CONFIG_HOME",
                str(Path.home() / ".config"),
            )
        )
        directory = base / "booklibconnect"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @debug_call()
    def file_path(self) -> Path:
        return self.config_dir() / "watched.json"

    @debug_call()
    def _load_legacy(self) -> dict[str, bool]:
        path = self.file_path()

        if not path.exists():
            dbg("watched.json missing: %s", path)
            return {}

        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )

            if (
                isinstance(data, dict)
                and "watched" in data
                and isinstance(data["watched"], dict)
            ):
                data = data["watched"]

            output: dict[str, bool] = {}

            if isinstance(data, dict):
                for key, value in data.items():
                    output[str(key)] = bool(value)

            dbg(
                "Loaded watched.json legacy entries: %d",
                len(output),
            )
            return output

        except Exception:
            dbg_exc(
                f"Failed loading watched.json from {path}"
            )
            return {}

    @debug_call()
    def _save_legacy(self) -> None:
        path = self.file_path()

        try:
            _write_json_atomic(
                path,
                self._legacy,
            )
        except Exception:
            dbg_exc(
                f"Failed saving watched.json to {path}"
            )

    def legacy_snapshot(self) -> dict[str, bool]:
        """
        Return a safe copy of the legacy Watched values.

        The background scanner can use this copy without accessing
        WatchedStore data directly from another thread.
        """
        return dict(self._legacy)

    def prime_scan_cache(
        self,
        values: dict[str, bool],
    ) -> None:
        """
        Replace the Watched RAM cache with values from a successfully
        completed library scan.
        """
        self._cache = {
            str(path): bool(value)
            for path, value in values.items()
        }

    @debug_call()
    def is_watched(
        self,
        track_path: Path,
    ) -> bool:
        if not track_path.exists():
            return False

        key = str(track_path)

        if key in self._cache:
            return self._cache[key]

        embedded = read_bool_flag(
            track_path,
            APP_WATCHED,
        )

        if embedded is not None:
            self._cache[key] = embedded
            return embedded

        legacy = bool(
            self._legacy.get(
                key,
                False,
            )
        )
        self._cache[key] = legacy

        if legacy:
            try:
                write_bool_flag(
                    track_path,
                    APP_WATCHED,
                    True,
                )
            except Exception:
                dbg_exc(
                    "Legacy watched migration failed "
                    f"for {track_path}"
                )

        return legacy

    @debug_call()
    def set_watched(
        self,
        track_path: Path,
        watched: bool,
    ) -> None:
        if not track_path.exists():
            return

        key = str(track_path)
        watched = bool(watched)

        try:
            success = write_bool_flag(
                track_path,
                APP_WATCHED,
                watched,
            )
            dbg(
                "Embedded watched=%s for %s ok=%s",
                watched,
                track_path,
                success,
            )

        except Exception:
            dbg_exc(
                "set_watched embedded write failed "
                f"for {track_path}"
            )

        self._cache[key] = watched
        self._legacy[key] = watched
        self._save_legacy()

    @debug_call()
    def refresh_track(
        self,
        track_path: Path,
    ) -> bool:
        """
        Remove a possibly outdated cached Watched value and reload
        the current value from the MP3 metadata.
        """
        self._cache.pop(
            str(track_path),
            None,
        )
        return self.is_watched(track_path)

    @debug_call()
    def count_watched(
        self,
        paths: list[Path],
    ) -> int:
        count = 0

        for path in paths:
            if self.is_watched(path):
                count += 1

        return count

    @debug_call()
    def move(
        self,
        old_path: Path,
        new_path: Path,
    ) -> None:
        old_key = str(old_path)
        new_key = str(new_path)

        if old_key in self._cache:
            self._cache[new_key] = self._cache.pop(
                old_key
            )

        if old_key in self._legacy:
            self._legacy[new_key] = self._legacy.pop(
                old_key
            )
            self._save_legacy()