from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .debug_tools import dbg_exc, debug_call

AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac"}


@debug_call()
def fmt_size_mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f}"


@debug_call()
def fmt_mtime(mtime_unix: int) -> str:
    return datetime.fromtimestamp(mtime_unix).strftime("%Y-%m-%d %H:%M")


@debug_call()
def fmt_len(seconds: float | None) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


@debug_call()
def is_audio(p: Path) -> bool:
    try:
        return p.is_file() and p.suffix.lower() in AUDIO_EXTS
    except Exception:
        dbg_exc(f"is_audio failed for {p!r}")
        return False


def _visible_directories(folder: Path) -> list[Path]:
    with os.scandir(folder) as entries:
        paths = [folder / e.name for e in entries if not e.name.startswith(".") and e.is_dir()]
    return sorted(paths, key=lambda path: path.name.lower())


@debug_call()
def list_artist_dirs(library_root: Path) -> list[Path]:
    try:
        return _visible_directories(library_root)
    except FileNotFoundError:
        return []


@debug_call()
def list_album_dirs(artist_dir: Path) -> list[Path]:
    return _visible_directories(artist_dir)


@debug_call()
def list_tracks(album_dir: Path, extensions=AUDIO_EXTS) -> list[Path]:
    with os.scandir(album_dir) as entries:
        tracks = [album_dir / e.name for e in entries
                  if os.path.splitext(e.name)[1].lower() in extensions and e.is_file()]
    return sorted(tracks, key=lambda path: path.name.lower())


@debug_call()
def first_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and v:
        v = v[0]
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", "ignore")
        except Exception:
            return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return None


@debug_call()
def parse_track_no(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"^\s*(\d+)", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        dbg_exc(f"parse_track_no failed for {s!r}")
        return None
