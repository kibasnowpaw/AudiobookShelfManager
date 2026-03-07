from __future__ import annotations

import json
import os
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call


class ReviewStore:
    """
    Stores review flags:
      - Goodreads: per-track (counted)
      - Audible: per-track (NOT counted anywhere, just a personal checkbox)

    File format (reviews.json):
    {
      "goodreads_tracks": { "<track_path>": true, ... },
      "audible_tracks": { "<track_path>": true, ... },

      // legacy keys may exist; we preserve them but don't use them:
      "audible_albums_legacy": { "<album_dir>": true, ... }
    }
    """

    @debug_call()
    def __init__(self) -> None:
        self._gr_tracks: dict[str, bool] = {}
        self._aud_tracks: dict[str, bool] = {}
        self._aud_albums_legacy: dict[str, bool] = {}
        self._load()

    @debug_call()
    def config_dir(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        d = base / "booklibconnect"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @debug_call()
    def file_path(self) -> Path:
        return self.config_dir() / "reviews.json"

    @debug_call()
    def _load(self) -> None:
        p = self.file_path()
        if not p.exists():
            dbg("reviews.json missing: %s", p)
            return

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return

            gr = data.get("goodreads_tracks") or data.get("goodreads") or {}
            aud = data.get("audible_tracks") or {}
            aud_legacy = data.get("audible_albums_legacy") or data.get("audible_albums") or {}

            if isinstance(gr, dict):
                self._gr_tracks = {str(k): bool(v) for k, v in gr.items() if v}
            elif isinstance(gr, list):
                self._gr_tracks = {str(x): True for x in gr}

            if isinstance(aud, dict):
                self._aud_tracks = {str(k): bool(v) for k, v in aud.items() if v}
            elif isinstance(aud, list):
                self._aud_tracks = {str(x): True for x in aud}

            # preserve old album-level audible flags if they existed
            if isinstance(aud_legacy, dict):
                self._aud_albums_legacy = {str(k): bool(v) for k, v in aud_legacy.items() if v}
            elif isinstance(aud_legacy, list):
                self._aud_albums_legacy = {str(x): True for x in aud_legacy}

            dbg("Loaded reviews.json (GR=%d, Audible(track)=%d, Audible(legacy albums)=%d)",
                len(self._gr_tracks), len(self._aud_tracks), len(self._aud_albums_legacy))

        except Exception:
            dbg_exc(f"Failed loading reviews from {p}")

    @debug_call()
    def save(self) -> None:
        p = self.file_path()
        data = {
            "goodreads_tracks": self._gr_tracks,
            "audible_tracks": self._aud_tracks,
            "audible_albums_legacy": self._aud_albums_legacy,
        }
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        dbg("Saved reviews.json (GR=%d, Audible(track)=%d)", len(self._gr_tracks), len(self._aud_tracks))

    # -------- Goodreads (counted) --------

    @debug_call()
    def goodreads_done(self, track_path: Path) -> bool:
        return bool(self._gr_tracks.get(str(track_path), False))

    @debug_call()
    def set_goodreads_done(self, track_path: Path, done: bool) -> None:
        k = str(track_path)
        if done:
            self._gr_tracks[k] = True
        else:
            self._gr_tracks.pop(k, None)
        self.save()

    @debug_call()
    def count_goodreads(self, tracks: list[Path]) -> int:
        return sum(1 for p in tracks if self.goodreads_done(p))

    # -------- Audible (per track, NOT counted) --------

    @debug_call()
    def audible_done(self, track_path: Path) -> bool:
        return bool(self._aud_tracks.get(str(track_path), False))

    @debug_call()
    def set_audible_done(self, track_path: Path, done: bool) -> None:
        k = str(track_path)
        if done:
            self._aud_tracks[k] = True
        else:
            self._aud_tracks.pop(k, None)
        self.save()

    # -------- moves on rename --------

    @debug_call()
    def move_track(self, old_path: Path, new_path: Path) -> None:
        o = str(old_path)
        n = str(new_path)

        changed = False
        if o in self._gr_tracks:
            self._gr_tracks.pop(o, None)
            self._gr_tracks[n] = True
            changed = True

        if o in self._aud_tracks:
            self._aud_tracks.pop(o, None)
            self._aud_tracks[n] = True
            changed = True

        if changed:
            self.save()
