from __future__ import annotations

import re
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call
from .models import TrackMeta

_BAD_CHARS = r'<>:"/\|?*\0'


@debug_call()
def sanitize_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(rf"[{re.escape(_BAD_CHARS)}]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    return s


@debug_call()
def build_track_filename(meta: TrackMeta, ext: str) -> str:
    artist = meta.artist or meta.albumartist
    title = meta.title or "Track"
    n = meta.trackno if meta.trackno > 0 else 0

    artist = sanitize_filename(artist)
    title = sanitize_filename(title)

    prefix = f"{n:02d}" if n > 0 else "00"

    # Pattern: "NN - Title - Artist.ext"
    if artist:
        return f"{prefix} - {title} - {artist}{ext}"
    return f"{prefix} - {title}{ext}"


@debug_call()
def rename_track_file(old_path: Path, meta: TrackMeta, rename_sidecar: bool = True) -> Path:
    target = old_path.with_name(build_track_filename(meta, old_path.suffix))
    dbg("rename_track_file old=%s target=%s rename_sidecar=%s", old_path, target, rename_sidecar)

    if target == old_path:
        dbg("rename_track_file: target same as old, no rename")
        return old_path

    if target.exists():
        stem = target.stem
        ext = target.suffix
        i = 1
        while True:
            cand = target.with_name(f"{stem} ({i}){ext}")
            if not cand.exists():
                target = cand
                break
            i += 1
        dbg("rename_track_file collision -> %s", target)

    if rename_sidecar:
        for img_ext in [".jpg", ".jpeg", ".png", ".webp"]:
            side = old_path.with_suffix(img_ext)
            if side.exists():
                side_target = target.with_suffix(img_ext)
                try:
                    side.rename(side_target)
                    dbg("Renamed sidecar %s -> %s", side, side_target)
                except Exception:
                    dbg_exc(f"Failed renaming sidecar {side} -> {side_target}")

    old_path.rename(target)
    dbg("Renamed track %s -> %s", old_path, target)
    return target
