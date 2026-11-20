from __future__ import annotations

import re
import shutil
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call
from .models import TrackMeta
from .audio_formats import supported_audio
from .covers import IMG_EXTS

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


@debug_call()
def sanitize_filename(s: str) -> str:
    s = (s or "").strip()
    s = _BAD_CHARS.sub("", s)
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

    entries = list(old_path.parent.iterdir())
    sidecars = [entry for entry in entries if rename_sidecar and entry.is_file()
                and entry.suffix.lower() in IMG_EXTS and entry.stem.casefold() == old_path.stem.casefold()]
    shared_cover = any(entry != old_path and supported_audio(entry)
                       and entry.stem.casefold() == old_path.stem.casefold() for entry in entries)

    # Exclude our own sidecars, but reserve all other artwork even when the
    # user chooses not to rename covers. A new name must not adopt another cover.
    ours = set(sidecars)
    reserved = {entry.stem.casefold() for entry in entries
                if entry != old_path and entry not in ours
                and (supported_audio(entry) or entry.suffix.lower() in IMG_EXTS)}

    def occupied(candidate):
        return candidate.stem.casefold() in reserved or candidate.exists()

    if occupied(target):
        stem, extension = target.stem, target.suffix
        number = 1
        while occupied(target.with_name(f"{stem} ({number}){extension}")):
            number += 1
        target = target.with_name(f"{stem} ({number}){extension}")
        dbg("rename_track_file collision -> %s", target)

    # Do not move an image away from the original file if the audio rename fails.
    old_path.rename(target)
    for side in sidecars:
        side_target = target.with_suffix(side.suffix)
        if side_target == side:
            continue
        try:
            if shared_cover:
                shutil.copy2(side, side_target)
            else:
                side.rename(side_target)
            dbg("Updated sidecar %s -> %s", side, side_target)
        except Exception:
            dbg_exc(f"Failed updating sidecar {side} -> {side_target}")

    dbg("Renamed track %s -> %s", old_path, target)
    return target
