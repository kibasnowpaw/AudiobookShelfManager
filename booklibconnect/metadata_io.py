from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.id3 import (
    ID3,
    ID3NoHeaderError,
    APIC,
    COMM,
    TPE1,
    TPE2,
    TALB,
    TIT2,
    TCON,
    TDRC,
    TYER,
    TRCK,
    TPOS,
    TCOM,
)
from mutagen.mp3 import MP3

from .debug_tools import dbg, dbg_exc, debug_call
from .helpers import first_str, parse_track_no
from .models import TrackMeta


# ---------------- low-level helpers ----------------

def _synchsafe_to_int(b4: bytes) -> int:
    if len(b4) != 4:
        return 0
    return ((b4[0] & 0x7F) << 21) | ((b4[1] & 0x7F) << 14) | ((b4[2] & 0x7F) << 7) | (b4[3] & 0x7F)


def _find_mp3_frame_start(buf: bytes) -> int | None:
    """
    Find a likely MPEG frame sync. This is a heuristic.
    We search for 0xFF followed by a byte with top 3 bits set (111xxxxx).
    """
    n = len(buf)
    for i in range(0, n - 2):
        if buf[i] != 0xFF:
            continue
        b2 = buf[i + 1]
        if (b2 & 0xE0) != 0xE0:
            continue
        # Avoid obvious false positives: layer bits should not be 00
        # (bits 1-2 of b2). If they are 00, skip.
        if (b2 & 0x06) == 0x00:
            continue
        return i
    return None


def _repair_broken_id3_prefix(path: Path) -> bool:
    """
    If the file begins with a corrupt ID3v2 tag (common when tag size is wrong),
    strip everything before the first MP3 audio frame and rewrite the file.

    Returns True if we modified the file.
    """
    try:
        st = path.stat()
        if st.st_size < 16:
            return False

        with path.open("rb") as f:
            head = f.read(10)
            if len(head) < 10:
                return False

            if head[:3] != b"ID3":
                return False

            tag_size = _synchsafe_to_int(head[6:10])
            tag_len = 10 + tag_size

            # If tag_len fits file size, it's probably not the classic "size too large" corruption.
            # We still might be corrupt, but don’t rewrite unless we have to.
            if tag_len <= st.st_size:
                return False

            dbg("ID3 tag looks corrupt (tag_len=%d > file_size=%d) -> attempting repair: %s", tag_len, st.st_size, path)

            # Read a chunk and find first MP3 frame
            f.seek(0)
            chunk = f.read(min(st.st_size, 4 * 1024 * 1024))  # scan up to 4MB
            start = _find_mp3_frame_start(chunk)
            if start is None:
                dbg("Repair failed: couldn't find MP3 frame sync in first chunk: %s", path)
                return False

        tmp = path.with_suffix(path.suffix + ".repaired.tmp")
        with path.open("rb") as src, tmp.open("wb") as dst:
            src.seek(start)
            shutil.copyfileobj(src, dst, length=1024 * 1024)

        tmp.replace(path)
        dbg("Repaired broken ID3 prefix by stripping %d bytes: %s", start, path)
        return True

    except Exception:
        dbg_exc(f"_repair_broken_id3_prefix failed for {path}")
        return False


def _try_load_id3(path: Path) -> ID3 | None:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        return None
    except Exception:
        return None


def _parse_slash_number(s: str | None) -> tuple[int, int]:
    """
    Parse "1/12" -> (1, 12), "1" -> (1, 0)
    """
    if not s:
        return (0, 0)
    s = s.strip()
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            return (int(a.strip() or "0"), int(b.strip() or "0"))
        except Exception:
            return (0, 0)
    try:
        return (int(s), 0)
    except Exception:
        return (0, 0)


def _first_text_frame(id3: ID3, key: str) -> str:
    try:
        fr = id3.get(key)
        if fr and getattr(fr, "text", None):
            t = fr.text[0]
            return str(t).strip() if t is not None else ""
    except Exception:
        pass
    return ""


def _first_comm(id3: ID3) -> str:
    try:
        comms = id3.getall("COMM")
        for c in comms:
            # COMM.text is a list
            if getattr(c, "text", None):
                txt = str(c.text[0]).strip()
                if txt:
                    return txt
    except Exception:
        pass
    return ""


# ---------------- public API ----------------

@debug_call()
def read_meta_and_length(p: Path) -> tuple[TrackMeta, float | None]:
    """
    MP3 only.
    Reads ID3 frames directly (more reliable than EasyID3 when custom fields are used).
    If ID3 header is corrupt, attempts a safe repair.
    """
    meta = TrackMeta()
    length: float | None = None

    dbg("Reading metadata (MP3): %s", p)

    # length (try MP3 parser first; if it fails, fall back to MutagenFile)
    try:
        mp3 = MP3(p)
        if getattr(mp3, "info", None) and getattr(mp3.info, "length", None):
            length = float(mp3.info.length)
    except Exception:
        try:
            mf = MutagenFile(p)
            if mf and getattr(mf, "info", None) and getattr(mf.info, "length", None):
                length = float(mf.info.length)
        except Exception:
            length = None

    id3 = _try_load_id3(p)
    if id3 is None:
        # If broken ID3 header, try repair once
        repaired = _repair_broken_id3_prefix(p)
        if repaired:
            id3 = _try_load_id3(p)

    if id3 is None:
        # no tags or unreadable; leave defaults
        dbg("No readable ID3 tags: %s", p)
        return meta, length

    try:
        meta.title = _first_text_frame(id3, "TIT2")
        meta.artist = _first_text_frame(id3, "TPE1")
        meta.album = _first_text_frame(id3, "TALB")
        meta.albumartist = _first_text_frame(id3, "TPE2")
        meta.genre = _first_text_frame(id3, "TCON")

        # year can be TDRC or TYER
        y = _first_text_frame(id3, "TDRC") or _first_text_frame(id3, "TYER")
        meta.year = y

        # composer
        meta.composer = _first_text_frame(id3, "TCOM")

        # comment (COMM)
        meta.comment = _first_comm(id3)

        # track/disc
        trck = _first_text_frame(id3, "TRCK")
        tno, ttot = _parse_slash_number(trck)
        meta.trackno = tno
        meta.totaltracks = ttot

        tpos = _first_text_frame(id3, "TPOS")
        dno, _dtot = _parse_slash_number(tpos)
        meta.discno = dno

    except Exception:
        dbg_exc(f"Metadata parse failed for {p}")

    dbg("Metadata read OK: %s -> %s", p.name, meta)
    return meta, length


def _set_text_frame(id3: ID3, key: str, frame_ctor, value: str) -> None:
    value = (value or "").strip()
    try:
        if value:
            id3.setall(key, [frame_ctor(encoding=3, text=[value])])
        else:
            id3.delall(key)
    except Exception:
        dbg_exc(f"_set_text_frame failed key={key!r}")


def _set_number_slash(id3: ID3, key: str, frame_ctor, a: int, b: int) -> None:
    try:
        if a > 0 and b > 0:
            id3.setall(key, [frame_ctor(encoding=3, text=[f"{a}/{b}"])])
        elif a > 0:
            id3.setall(key, [frame_ctor(encoding=3, text=[str(a)])])
        else:
            id3.delall(key)
    except Exception:
        dbg_exc(f"_set_number_slash failed key={key!r}")


@debug_call()
def write_metadata_to_file(path: Path, meta: TrackMeta) -> None:
    """
    MP3 only.
    Repairs broken ID3 headers if needed, then writes frames.
    """
    if path.suffix.lower() != ".mp3":
        raise RuntimeError("This build supports MP3 only.")

    dbg("write_metadata_to_file path=%s meta=%s", path, meta)

    # If ID3 is broken, repair first
    id3 = _try_load_id3(path)
    if id3 is None:
        _repair_broken_id3_prefix(path)
        id3 = _try_load_id3(path)

    # If still no ID3, create a fresh one
    if id3 is None:
        id3 = ID3()

    try:
        _set_text_frame(id3, "TIT2", TIT2, meta.title)
        _set_text_frame(id3, "TPE1", TPE1, meta.artist)
        _set_text_frame(id3, "TALB", TALB, meta.album)
        _set_text_frame(id3, "TPE2", TPE2, meta.albumartist)
        _set_text_frame(id3, "TCON", TCON, meta.genre)

        # year: write to TDRC (modern)
        _set_text_frame(id3, "TDRC", TDRC, meta.year)

        # composer
        _set_text_frame(id3, "TCOM", TCOM, getattr(meta, "composer", ""))

        # comment (COMM)
        comment = (getattr(meta, "comment", "") or "").strip()
        try:
            id3.delall("COMM")
            if comment:
                id3.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
        except Exception:
            dbg_exc("COMM write failed")

        # track/disc
        _set_number_slash(id3, "TRCK", TRCK, int(getattr(meta, "trackno", 0) or 0), int(getattr(meta, "totaltracks", 0) or 0))
        _set_number_slash(id3, "TPOS", TPOS, int(getattr(meta, "discno", 0) or 0), 0)

        id3.save(path, v2_version=3)
        dbg("MP3 metadata saved: %s", path)

    except Exception:
        dbg_exc(f"write_metadata_to_file failed for {path}")
        raise


@debug_call()
def embed_cover_into_file(path: Path, image_path: Path) -> None:
    """
    MP3 only. Replaces APIC frames with the new image.
    Repairs broken ID3 headers if needed.
    """
    if path.suffix.lower() != ".mp3":
        raise RuntimeError("This build supports MP3 only.")

    img_ext = image_path.suffix.lower()
    mime = "image/jpeg"
    if img_ext == ".png":
        mime = "image/png"
    elif img_ext in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif img_ext == ".webp":
        mime = "image/webp"

    dbg("embed_cover_into_file audio=%s image=%s mime=%s", path, image_path, mime)

    # repair if needed
    id3 = _try_load_id3(path)
    if id3 is None:
        _repair_broken_id3_prefix(path)
        id3 = _try_load_id3(path)

    if id3 is None:
        id3 = ID3()

    data = image_path.read_bytes()

    try:
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        id3.save(path, v2_version=3)
        dbg("Embedded cover into MP3: %s", path)
    except Exception:
        dbg_exc(f"embed_cover_into_file failed for {path}")
        raise
