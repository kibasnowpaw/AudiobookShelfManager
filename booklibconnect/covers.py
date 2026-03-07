from __future__ import annotations

from pathlib import Path
from typing import Optional

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def find_cover_in_folder(folder: Path, preferred_stems: list[str]) -> Path | None:
    """
    Search for an image file inside `folder` by preferred filename stems, then any image.
    Example: preferred_stems ["cover", "folder", "front"] will match cover.jpg, folder.png, etc.
    """
    if not folder.exists() or not folder.is_dir():
        return None

    # 1) Preferred names first
    for stem in preferred_stems:
        stem_l = stem.lower()

        # Fast exact checks first
        for ext in IMG_EXTS:
            p = folder / f"{stem}{ext}"
            if p.exists() and p.is_file():
                return p

        # Case-insensitive scan for this stem
        for p in folder.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in IMG_EXTS:
                continue
            if p.stem.lower() == stem_l:
                return p

    # 2) Fallback: first image in folder (stable order)
    imgs = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS),
        key=lambda x: x.name.lower(),
    )
    return imgs[0] if imgs else None


def find_track_sidecar_cover(track_file: Path) -> Path | None:
    """
    Look for a sidecar image next to the track file with the same basename.
    Example: "01 - Track.mp3" -> "01 - Track.jpg/png/webp"
    """
    folder = track_file.parent
    base = track_file.stem

    for ext in IMG_EXTS:
        p = folder / f"{base}{ext}"
        if p.exists() and p.is_file():
            return p

    # Case-insensitive scan
    base_l = base.lower()
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMG_EXTS and p.stem.lower() == base_l:
            return p

    return None


def get_embedded_cover_bytes(track_file: Path) -> Optional[bytes]:
    """
    Return embedded cover bytes from a supported audio file if present.
    Supports MP3 (ID3/APIC), M4A/M4B (MP4 covr), and FLAC pictures.
    Returns None if no embedded art is found.
    """
    ext = track_file.suffix.lower()

    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3

            id3 = ID3(track_file)
            for frame in id3.values():
                # APIC frame(s) hold embedded artwork
                if getattr(frame, "FrameID", "") == "APIC":
                    data = getattr(frame, "data", None)
                    if isinstance(data, (bytes, bytearray)) and data:
                        return bytes(data)
            return None

        if ext in {".m4a", ".m4b"}:
            from mutagen.mp4 import MP4

            mp4 = MP4(track_file)
            tags = mp4.tags or {}
            covr = tags.get("covr")
            if isinstance(covr, list) and covr:
                first = covr[0]
                if first:
                    return bytes(first)
            return None

        if ext == ".flac":
            from mutagen.flac import FLAC

            flac = FLAC(track_file)
            pics = getattr(flac, "pictures", None) or []
            if pics:
                data = getattr(pics[0], "data", None)
                if isinstance(data, (bytes, bytearray)) and data:
                    return bytes(data)
            return None

    except Exception:
        # Keep this helper quiet; app.py does the user-facing logging.
        return None

    return None
