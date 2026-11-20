from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, APIC, ID3NoHeaderError

from booklibconnect.paths import covers_dir


# Renamed to avoid collision with models.TrackMeta
@dataclass(frozen=True)
class IndexedTrackMeta:
    artist: str
    album: str
    title: str
    album_artist: str
    track_no: Optional[int]
    disc_no: Optional[int]
    year: Optional[int]
    duration_sec: Optional[int]
    cover_path: Optional[str]


def _first_text(tags, keys: list[str]) -> Optional[str]:
    if not tags:
        return None

    for k in keys:
        v = tags.get(k)
        if not v:
            continue

        if isinstance(v, (list, tuple)):
            if not v:
                continue
            x = v[0]
            return str(x).strip() if x is not None else None

        return str(v).strip()

    return None


def _parse_int_maybe(v: Optional[str]) -> Optional[int]:
    if not v:
        return None

    s = str(v).strip()
    if "/" in s:
        s = s.split("/", 1)[0].strip()

    try:
        return int(s)
    except Exception:
        return None


def _guess_from_path(p: Path) -> tuple[str, str, str]:
    # artist = parent of album folder
    # album = album folder
    # title = filename
    title = p.stem
    album = p.parent.name
    artist = p.parent.parent.name if p.parent.parent and p.parent.parent.name else "Unknown"
    return artist or "Unknown", album or "Unknown", title or "Unknown"


def _write_cover_bytes(blob: bytes, ext: str) -> str:
    h = hashlib.sha1(blob).hexdigest()
    out = covers_dir() / f"{h}.{ext}"

    if not out.exists():
        out.write_bytes(blob)

    return str(out)


def _extract_cover_mp3(path: Path) -> Optional[str]:
    try:
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            return None

        apics = id3.getall("APIC")
        if not apics:
            return None

        ap: APIC = apics[0]
        mime = (ap.mime or "").lower()

        if "png" in mime:
            ext = "png"
        else:
            ext = "jpg"

        return _write_cover_bytes(ap.data, ext)

    except Exception:
        return None


def read_track_meta(path: Path) -> IndexedTrackMeta:
    if path.suffix.lower() == ".m4b":
        from .mp4_metadata import read_m4b
        from .covers import get_embedded_cover_bytes
        from .purchase_info import release_year
        metadata, seconds = read_m4b(path)
        fallback_artist, fallback_album, fallback_title = _guess_from_path(path)
        blob = get_embedded_cover_bytes(path)
        cover = _write_cover_bytes(blob, 'png' if blob.startswith(b'\x89PNG') else 'jpg') if blob else None
        artist = metadata.artist or fallback_artist
        return IndexedTrackMeta(
            artist=artist, album=metadata.album or fallback_album,
            title=metadata.title or fallback_title,
            album_artist=metadata.albumartist or artist,
            track_no=metadata.trackno or None, disc_no=metadata.discno or None,
            year=release_year(metadata.year), duration_sec=int(round(seconds)) if seconds else None,
            cover_path=cover)
    if path.suffix.lower() != ".mp3":
        raise RuntimeError("Supported audiobook formats are MP3 and M4B.")

    audio = MutagenFile(path)

    if not audio:
        a, al, t = _guess_from_path(path)
        return IndexedTrackMeta(
            artist=a,
            album=al,
            title=t,
            album_artist=a,
            track_no=None,
            disc_no=None,
            year=None,
            duration_sec=None,
            cover_path=None,
        )

    tags = getattr(audio, "tags", None)

    # ID3 keys only
    artist = _first_text(tags, ["TPE1"]) or ""
    album_artist = _first_text(tags, ["TPE2"]) or ""
    album = _first_text(tags, ["TALB"]) or ""
    title = _first_text(tags, ["TIT2"]) or ""

    track_s = _first_text(tags, ["TRCK"])
    disc_s = _first_text(tags, ["TPOS"])
    year_s = _first_text(tags, ["TDRC", "TYER"])

    track_no = _parse_int_maybe(track_s)
    disc_no = _parse_int_maybe(disc_s)
    year = _parse_int_maybe(year_s)

    duration_sec = None
    try:
        if getattr(audio, "info", None) and getattr(audio.info, "length", None):
            duration_sec = int(round(float(audio.info.length)))
    except Exception:
        duration_sec = None

    cover_path = _extract_cover_mp3(path)

    # Fallbacks
    if not (artist and album and title):
        ga, gal, gt = _guess_from_path(path)
        artist = artist or ga
        album_artist = album_artist or artist
        album = album or gal
        title = title or gt

    album_artist = album_artist or artist or "Unknown"
    artist = artist or "Unknown"
    album = album or "Unknown"
    title = title or "Unknown"

    return IndexedTrackMeta(
        artist=artist,
        album=album,
        title=title,
        album_artist=album_artist,
        track_no=track_no,
        disc_no=disc_no,
        year=year,
        duration_sec=duration_sec,
        cover_path=cover_path,
    )
