from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

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
    TRCK,
    TPOS,
    TCOM,
    TIPL,
    TXXX,
)
from mutagen.mp3 import MP3

from .debug_tools import dbg, dbg_exc, debug_call
from .models import TrackMeta
from .mp4_metadata import read_m4b, write_m4b, embed_m4b_cover


# ---------------- low-level helpers ----------------

_REPAIR_SCAN_LIMIT = 16 * 1024 * 1024
_REQUIRED_CONSECUTIVE_FRAMES = 3

# Bitrates in kbit/s, indexed by the MPEG header bitrate index (1-14).
_BITRATES_MPEG1 = {
    1: (
        32,
        32,
        32,
        32,
        40,
        48,
        56,
        64,
        80,
        96,
        112,
        128,
        144,
        160,
    ),
    2: (
        32,
        48,
        56,
        64,
        80,
        96,
        112,
        128,
        160,
        192,
        224,
        256,
        320,
        384,
    ),
    3: (
        32,
        40,
        48,
        56,
        64,
        80,
        96,
        112,
        128,
        160,
        192,
        224,
        256,
        320,
    ),
}

_BITRATES_MPEG2 = {
    1: (
        32,
        48,
        56,
        64,
        80,
        96,
        112,
        128,
        144,
        160,
        176,
        192,
        224,
        256,
    ),
    2: (
        8,
        16,
        24,
        32,
        40,
        48,
        56,
        64,
        80,
        96,
        112,
        128,
        144,
        160,
    ),
    3: (
        8,
        16,
        24,
        32,
        40,
        48,
        56,
        64,
        80,
        96,
        112,
        128,
        144,
        160,
    ),
}

_SAMPLE_RATES = {
    1: (44100, 48000, 32000),   # MPEG-1
    2: (22050, 24000, 16000),   # MPEG-2
    25: (11025, 12000, 8000),   # MPEG-2.5
}


def _synchsafe_to_int(b4: bytes) -> int:
    if len(b4) != 4:
        return 0

    return (
        ((b4[0] & 0x7F) << 21)
        | ((b4[1] & 0x7F) << 14)
        | ((b4[2] & 0x7F) << 7)
        | (b4[3] & 0x7F)
    )


def _mpeg_frame_info(
    buf: bytes,
    offset: int,
) -> tuple[int, int, int] | None:
    """
    Return (frame length, MPEG version, layer) for a valid MPEG
    audio header.

    Free-format and reserved values are rejected because their
    frame length cannot be established safely enough for
    automatic repair.
    """
    if offset < 0 or offset + 4 > len(buf):
        return None

    b1, b2, b3, _b4 = buf[offset : offset + 4]

    if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
        return None

    version_bits = (b2 >> 3) & 0x03
    layer_bits = (b2 >> 1) & 0x03

    if (
        version_bits == 0x01
        or layer_bits == 0x00
    ):
        return None

    version = {
        0x00: 25,
        0x02: 2,
        0x03: 1,
    }[version_bits]

    layer = {
        0x01: 3,
        0x02: 2,
        0x03: 1,
    }[layer_bits]

    bitrate_index = (b3 >> 4) & 0x0F
    sample_rate_index = (b3 >> 2) & 0x03
    padding = (b3 >> 1) & 0x01

    if (
        bitrate_index in {0, 15}
        or sample_rate_index == 3
    ):
        return None

    bitrate_table = (
        _BITRATES_MPEG1
        if version == 1
        else _BITRATES_MPEG2
    )

    bitrate = (
        bitrate_table[layer][bitrate_index - 1]
        * 1000
    )

    sample_rate = (
        _SAMPLE_RATES[version][sample_rate_index]
    )

    if layer == 1:
        frame_length = (
            (12 * bitrate) // sample_rate
            + padding
        ) * 4

    elif layer == 3 and version != 1:
        frame_length = (
            (72 * bitrate) // sample_rate
            + padding
        )

    else:
        frame_length = (
            (144 * bitrate) // sample_rate
            + padding
        )

    if frame_length < 24:
        return None

    return (
        frame_length,
        version,
        layer,
    )


def _has_consecutive_mp3_frames(
    buf: bytes,
    start: int,
    count: int,
) -> bool:
    first = _mpeg_frame_info(
        buf,
        start,
    )

    if first is None:
        return False

    _length, expected_version, expected_layer = first
    offset = start

    for _ in range(count):
        info = _mpeg_frame_info(
            buf,
            offset,
        )

        if info is None:
            return False

        frame_length, version, layer = info

        if (
            version != expected_version
            or layer != expected_layer
        ):
            return False

        offset += frame_length

        if offset > len(buf):
            return False

    return True


def _find_mp3_frame_start(
    buf: bytes,
) -> int | None:
    """
    Find the first candidate backed by several consecutive
    MPEG frames.
    """
    for offset in range(
        0,
        max(0, len(buf) - 3),
    ):
        if buf[offset] != 0xFF:
            continue

        if _has_consecutive_mp3_frames(
            buf,
            offset,
            _REQUIRED_CONSECUTIVE_FRAMES,
        ):
            return offset

    return None


def _next_repair_backup_path(
    path: Path,
) -> Path:
    base = path.with_name(
        f"{path.name}.pre-id3-repair.bak"
    )

    if not base.exists():
        return base

    index = 1

    while True:
        candidate = path.with_name(
            f"{path.name}.pre-id3-repair."
            f"{index}.bak"
        )

        if not candidate.exists():
            return candidate

        index += 1


def _create_repair_backup(
    path: Path,
) -> Path:
    """
    Create a preserved original, using a hard link when
    possible.
    """
    backup = _next_repair_backup_path(path)

    try:
        os.link(
            path,
            backup,
        )
        return backup

    except OSError:
        pass

    try:
        with (
            path.open("rb") as source,
            backup.open("xb") as destination,
        ):
            shutil.copyfileobj(
                source,
                destination,
                length=1024 * 1024,
            )

            destination.flush()
            os.fsync(destination.fileno())

        shutil.copystat(
            path,
            backup,
        )

        return backup

    except Exception:
        try:
            backup.unlink(
                missing_ok=True,
            )
        except OSError:
            pass

        raise


def _validate_repaired_mp3(
    path: Path,
) -> bool:
    try:
        audio = MP3(path)
        info = getattr(
            audio,
            "info",
            None,
        )

        length = (
            getattr(
                info,
                "length",
                0.0,
            )
            if info is not None
            else 0.0
        )

        return float(
            length or 0.0
        ) > 0.0

    except Exception:
        return False


def _repair_broken_id3_prefix(
    path: Path,
) -> bool:
    """
    If the file begins with a corrupt ID3v2 tag, build and
    validate a repaired copy before replacing the original.

    A backup is preserved beside the MP3 before any
    replacement occurs.

    Returns True if the file was modified.
    """
    temp_path: Path | None = None

    try:
        if path.is_symlink():
            dbg(
                "Repair skipped for symlink: %s",
                path,
            )
            return False

        file_stat = path.stat()

        if file_stat.st_size < 16:
            return False

        with path.open("rb") as source:
            header = source.read(10)

            if len(header) < 10:
                return False

            if header[:3] != b"ID3":
                return False

            tag_size = _synchsafe_to_int(
                header[6:10]
            )
            tag_length = 10 + tag_size

            # Only repair the recognised corruption where the
            # declared ID3 tag is larger than the entire file.
            if tag_length <= file_stat.st_size:
                return False

            dbg(
                "ID3 tag looks corrupt "
                "(tag_len=%d > file_size=%d) "
                "-> attempting safe repair: %s",
                tag_length,
                file_stat.st_size,
                path,
            )

            # Three consecutive frames are required so bytes
            # inside embedded artwork are not mistaken for the
            # start of the audio stream.
            source.seek(0)

            chunk = source.read(
                min(
                    file_stat.st_size,
                    _REPAIR_SCAN_LIMIT,
                )
            )

            start = _find_mp3_frame_start(
                chunk
            )

            if start is None:
                dbg(
                    "Repair stopped: no validated MP3 "
                    "frame sequence in the first %d "
                    "bytes: %s",
                    len(chunk),
                    path,
                )
                return False

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.repair.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            temp_path = Path(destination.name)

            with path.open("rb") as source:
                source.seek(start)

                shutil.copyfileobj(
                    source,
                    destination,
                    length=1024 * 1024,
                )

            destination.flush()
            os.fsync(
                destination.fileno()
            )

        shutil.copystat(
            path,
            temp_path,
        )

        if not _validate_repaired_mp3(
            temp_path
        ):
            dbg(
                "Repair stopped: Mutagen rejected "
                "repaired copy: %s",
                path,
            )
            return False

        backup_path = _create_repair_backup(
            path
        )

        os.replace(
            temp_path,
            path,
        )
        temp_path = None

        try:
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY,
            )

            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        except OSError:
            pass

        dbg(
            "Safely repaired broken ID3 prefix by "
            "stripping %d bytes: %s (backup: %s)",
            start,
            path,
            backup_path,
        )
        return True

    except Exception:
        dbg_exc(
            f"_repair_broken_id3_prefix failed "
            f"for {path}"
        )
        return False

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(
                    missing_ok=True,
                )
            except OSError:
                pass


def _try_load_id3(
    path: Path,
) -> ID3 | None:
    try:
        return ID3(path)

    except ID3NoHeaderError:
        return None

    except Exception:
        return None


def _parse_slash_number(
    value: str | None,
) -> tuple[int, int]:
    """
    Parse "1/12" as (1, 12), or "1" as (1, 0).
    """
    if not value:
        return (0, 0)

    value = value.strip()

    if "/" in value:
        first, second = value.split(
            "/",
            1,
        )

        try:
            return (
                int(first.strip() or "0"),
                int(second.strip() or "0"),
            )

        except Exception:
            return (0, 0)

    try:
        return (
            int(value),
            0,
        )

    except Exception:
        return (0, 0)


def _first_text_frame(
    id3: ID3,
    key: str,
) -> str:
    try:
        frame = id3.get(key)

        if (
            frame
            and getattr(
                frame,
                "text",
                None,
            )
        ):
            text = frame.text[0]

            return (
                str(text).strip()
                if text is not None
                else ""
            )

    except Exception:
        pass

    return ""


def _first_comm(
    id3: ID3,
) -> str:
    try:
        comments = id3.getall("COMM")

        for comment in comments:
            if getattr(
                comment,
                "text",
                None,
            ):
                text = str(
                    comment.text[0]
                ).strip()

                if text:
                    return text

    except Exception:
        pass

    return ""


def _translator_role(role: object) -> bool:
    normalized = re.sub(
        r"\s+",
        " ",
        str(role or "").strip(),
    ).casefold()

    return normalized in {
        "translator",
        "translated by",
        "translation",
    }


def _narrator_role(role: object) -> bool:
    normalized = re.sub(
        r"\s+",
        " ",
        str(role or "").strip(),
    ).casefold()

    return normalized in {
        "narrator",
        "narrated by",
        "read by",
        "reader",
    }


def _split_translator_names(value: object) -> list[str]:
    """Split our UI value without breaking 'Surname, Given name'."""
    text = str(value or "")
    parts = re.split(r"[;\n]+", text)
    names: list[str] = []
    seen: set[str] = set()

    for part in parts:
        name = re.sub(r"\s+", " ", part).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)

    return names


def _split_narrator_names(value: object) -> list[str]:
    """Split our UI value without breaking 'Surname, Given name'."""
    text = str(value or "")
    parts = re.split(r"[;\n]+", text)
    names: list[str] = []
    seen: set[str] = set()

    for part in parts:
        name = re.sub(r"\s+", " ", part).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)

    return names


def _translator_from_id3(id3: ID3) -> str:
    """
    Read translators from the official involved-people frame.

    TXXX:TRANSLATOR is accepted as a compatibility fallback for
    tag editors and older files that do not use TIPL/IPLS.
    """
    names: list[str] = []
    seen: set[str] = set()

    def add_name(value: object) -> None:
        for name in _split_translator_names(value):
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)

    try:
        for frame in id3.getall("TIPL"):
            for person in getattr(frame, "people", []) or []:
                if not isinstance(person, (list, tuple)) or len(person) < 2:
                    continue
                role, person_name = person[0], person[1]
                if _translator_role(role):
                    add_name(person_name)
    except Exception:
        dbg_exc("TIPL translator read failed")

    try:
        for frame in id3.getall("TXXX"):
            if str(getattr(frame, "desc", "")).strip().casefold() != "translator":
                continue

            text_values = getattr(frame, "text", None) or []
            if isinstance(text_values, str):
                text_values = [text_values]

            for value in text_values:
                add_name(value)
    except Exception:
        dbg_exc("TXXX translator read failed")

    return "; ".join(names)


def _narrator_from_id3(id3: ID3) -> str:
    """
    Read narrators from the official involved-people frame.

    TXXX:NARRATOR is accepted as a compatibility fallback for
    audiobook players and tag editors that do not expose TIPL/IPLS.
    """
    names: list[str] = []
    seen: set[str] = set()

    def add_name(value: object) -> None:
        for name in _split_narrator_names(value):
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)

    try:
        for frame in id3.getall("TIPL"):
            for person in getattr(frame, "people", []) or []:
                if not isinstance(person, (list, tuple)) or len(person) < 2:
                    continue
                role, person_name = person[0], person[1]
                if _narrator_role(role):
                    add_name(person_name)
    except Exception:
        dbg_exc("TIPL narrator read failed")

    try:
        for frame in id3.getall("TXXX"):
            if str(getattr(frame, "desc", "")).strip().casefold() != "narrator":
                continue

            text_values = getattr(frame, "text", None) or []
            if isinstance(text_values, str):
                text_values = [text_values]

            for value in text_values:
                add_name(value)
    except Exception:
        dbg_exc("TXXX narrator read failed")

    return "; ".join(names)


def _set_translator(id3: ID3, value: str) -> None:
    """
    Store translators in TIPL while preserving all other people roles.

    A matching TXXX value is also written because many audiobook and
    general-purpose tag editors expose custom text more reliably than TIPL.
    """
    translator_names = _split_translator_names(value)
    preserved_people: list[list[str]] = []

    try:
        for frame in id3.getall("TIPL"):
            for person in getattr(frame, "people", []) or []:
                if not isinstance(person, (list, tuple)) or len(person) < 2:
                    continue

                role = str(person[0] or "").strip()
                person_name = str(person[1] or "").strip()
                if not role or not person_name or _translator_role(role):
                    continue
                preserved_people.append([role, person_name])

        people = preserved_people + [
            ["translator", name]
            for name in translator_names
        ]

        if people:
            id3.setall(
                "TIPL",
                [
                    TIPL(
                        encoding=3,
                        people=people,
                    )
                ],
            )
        else:
            id3.delall("TIPL")
    except Exception:
        dbg_exc("TIPL translator write failed")
        raise

    try:
        for frame in list(id3.getall("TXXX")):
            if str(getattr(frame, "desc", "")).strip().casefold() != "translator":
                continue
            id3.delall(getattr(frame, "HashKey", "TXXX:TRANSLATOR"))

        if translator_names:
            id3.add(
                TXXX(
                    encoding=3,
                    desc="TRANSLATOR",
                    text=["; ".join(translator_names)],
                )
            )
    except Exception:
        dbg_exc("TXXX translator write failed")
        raise


def _set_narrator(id3: ID3, value: str) -> None:
    """
    Store narrators in TIPL while preserving all other people roles.

    A matching TXXX value is also written for broad audiobook-player and
    tag-editor compatibility.
    """
    narrator_names = _split_narrator_names(value)
    preserved_people: list[list[str]] = []

    try:
        for frame in id3.getall("TIPL"):
            for person in getattr(frame, "people", []) or []:
                if not isinstance(person, (list, tuple)) or len(person) < 2:
                    continue

                role = str(person[0] or "").strip()
                person_name = str(person[1] or "").strip()
                if not role or not person_name or _narrator_role(role):
                    continue
                preserved_people.append([role, person_name])

        people = preserved_people + [
            ["narrator", name]
            for name in narrator_names
        ]

        if people:
            id3.setall(
                "TIPL",
                [
                    TIPL(
                        encoding=3,
                        people=people,
                    )
                ],
            )
        else:
            id3.delall("TIPL")
    except Exception:
        dbg_exc("TIPL narrator write failed")
        raise

    try:
        for frame in list(id3.getall("TXXX")):
            if str(getattr(frame, "desc", "")).strip().casefold() != "narrator":
                continue
            id3.delall(getattr(frame, "HashKey", "TXXX:NARRATOR"))

        if narrator_names:
            id3.add(
                TXXX(
                    encoding=3,
                    desc="NARRATOR",
                    text=["; ".join(narrator_names)],
                )
            )
    except Exception:
        dbg_exc("TXXX narrator write failed")
        raise


# ---------------- public API ----------------

@debug_call()
def read_meta_and_length(
    path: Path,
) -> tuple[TrackMeta, float | None]:
    """
    Read metadata and duration from an MP3 or M4B file.

    If the ID3 header has the recognised corruption, attempt a
    safe repair with validation and backup.
    """
    if path.suffix.lower() == ".m4b":
        return read_m4b(path)
    if path.suffix.lower() != ".mp3":
        raise ValueError("Supported audiobook formats are MP3 and M4B.")
    metadata = TrackMeta()
    length: float | None = None

    dbg(
        "Reading metadata (MP3): %s",
        path,
    )

    # Try the dedicated MP3 parser first.
    try:
        mp3 = MP3(path)

        if (
            getattr(
                mp3,
                "info",
                None,
            )
            and getattr(
                mp3.info,
                "length",
                None,
            )
        ):
            length = float(
                mp3.info.length
            )

    except Exception:
        # Fall back to Mutagen's generic file loader.
        try:
            audio = MutagenFile(path)

            if (
                audio
                and getattr(
                    audio,
                    "info",
                    None,
                )
                and getattr(
                    audio.info,
                    "length",
                    None,
                )
            ):
                length = float(
                    audio.info.length
                )

        except Exception:
            length = None

    id3 = _try_load_id3(path)

    if id3 is None:
        repaired = _repair_broken_id3_prefix(
            path
        )

        if repaired:
            id3 = _try_load_id3(path)

            # The first duration attempt may have failed because
            # of the damaged ID3 header. Retry after repair.
            if length is None:
                try:
                    mp3 = MP3(path)

                    if (
                        getattr(
                            mp3,
                            "info",
                            None,
                        )
                        and getattr(
                            mp3.info,
                            "length",
                            None,
                        )
                    ):
                        length = float(
                            mp3.info.length
                        )

                except Exception:
                    length = None

    if id3 is None:
        dbg(
            "No readable ID3 tags: %s",
            path,
        )
        return (
            metadata,
            length,
        )

    try:
        metadata.title = _first_text_frame(
            id3,
            "TIT2",
        )

        metadata.artist = _first_text_frame(
            id3,
            "TPE1",
        )

        metadata.album = _first_text_frame(
            id3,
            "TALB",
        )

        metadata.albumartist = _first_text_frame(
            id3,
            "TPE2",
        )

        metadata.genre = _first_text_frame(
            id3,
            "TCON",
        )

        metadata.year = (
            _first_text_frame(
                id3,
                "TDRC",
            )
            or _first_text_frame(
                id3,
                "TYER",
            )
        )

        metadata.composer = _first_text_frame(
            id3,
            "TCOM",
        )

        metadata.narrator = _narrator_from_id3(
            id3
        )
        if not metadata.narrator:
            # Composer (TCOM) was widely used as a narrator fallback by
            # older audiobook tools. Present it as Narrator in this app,
            # while preferring the real narrator roles whenever available.
            metadata.narrator = metadata.composer

        metadata.translator = _translator_from_id3(
            id3
        )

        metadata.comment = _first_comm(
            id3
        )

        track_value = _first_text_frame(
            id3,
            "TRCK",
        )

        track_number, total_tracks = (
            _parse_slash_number(
                track_value
            )
        )

        metadata.trackno = track_number
        metadata.totaltracks = total_tracks

        disc_value = _first_text_frame(
            id3,
            "TPOS",
        )

        disc_number, _total_discs = (
            _parse_slash_number(
                disc_value
            )
        )

        metadata.discno = disc_number

    except Exception:
        dbg_exc(
            f"Metadata parse failed for {path}"
        )

    dbg(
        "Metadata read OK: %s -> %s",
        path.name,
        metadata,
    )

    return (
        metadata,
        length,
    )


def _set_text_frame(
    id3: ID3,
    key: str,
    frame_constructor,
    value: str,
) -> None:
    value = (
        value or ""
    ).strip()

    try:
        if value:
            id3.setall(
                key,
                [
                    frame_constructor(
                        encoding=3,
                        text=[value],
                    )
                ],
            )

        else:
            id3.delall(key)

    except Exception:
        dbg_exc(
            f"_set_text_frame failed key={key!r}"
        )


def _set_number_slash(
    id3: ID3,
    key: str,
    frame_constructor,
    first: int,
    second: int,
) -> None:
    try:
        if first > 0 and second > 0:
            id3.setall(
                key,
                [
                    frame_constructor(
                        encoding=3,
                        text=[
                            f"{first}/{second}"
                        ],
                    )
                ],
            )

        elif first > 0:
            id3.setall(
                key,
                [
                    frame_constructor(
                        encoding=3,
                        text=[str(first)],
                    )
                ],
            )

        else:
            id3.delall(key)

    except Exception:
        dbg_exc(
            f"_set_number_slash failed key={key!r}"
        )


def _replace_cover_frame(
    id3: ID3,
    image_path: Path,
) -> None:
    """Replace APIC in memory so the caller can save the tag once."""
    image_extension = image_path.suffix.lower()

    if image_extension == ".png":
        mime = "image/png"
    elif image_extension in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif image_extension == ".webp":
        mime = "image/webp"
    else:
        raise RuntimeError(
            "Cover must be a JPG, JPEG, PNG, or WebP image."
        )

    image_data = image_path.read_bytes()
    if not image_data:
        raise RuntimeError("The selected cover image is empty.")

    id3.delall("APIC")
    id3.add(
        APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="Cover",
            data=image_data,
        )
    )


@debug_call()
def write_metadata_to_file(
    path: Path,
    metadata: TrackMeta,
    *,
    cover_path: Path | None = None,
) -> None:
    """
    Write metadata and an optional replacement cover in one format-specific save.

    A recognised damaged ID3 prefix is repaired safely before
    new metadata is written.
    """
    if path.suffix.lower() == ".m4b":
        return write_m4b(path, metadata, cover_path)
    if path.suffix.lower() != ".mp3":
        raise RuntimeError("Supported audiobook formats are MP3 and M4B.")

    dbg(
        "write_metadata_to_file path=%s meta=%s",
        path,
        metadata,
    )

    id3 = _try_load_id3(path)

    if id3 is None:
        _repair_broken_id3_prefix(path)
        id3 = _try_load_id3(path)

    if id3 is None:
        id3 = ID3()

    try:
        _set_text_frame(
            id3,
            "TIT2",
            TIT2,
            metadata.title,
        )

        _set_text_frame(
            id3,
            "TPE1",
            TPE1,
            metadata.artist,
        )

        _set_text_frame(
            id3,
            "TALB",
            TALB,
            metadata.album,
        )

        _set_text_frame(
            id3,
            "TPE2",
            TPE2,
            metadata.albumartist,
        )

        _set_text_frame(
            id3,
            "TCON",
            TCON,
            metadata.genre,
        )

        _set_text_frame(
            id3,
            "TDRC",
            TDRC,
            metadata.year,
        )

        _set_text_frame(
            id3,
            "TCOM",
            TCOM,
            getattr(
                metadata,
                "composer",
                "",
            ),
        )

        # Preserve an existing narrator tag when an older TrackMeta/UI is
        # temporarily used during an upgrade.
        if hasattr(metadata, "narrator"):
            _set_narrator(
                id3,
                getattr(
                    metadata,
                    "narrator",
                    "",
                ),
            )

        # During a staged upgrade, an older TrackMeta/UI may not yet have
        # a translator attribute. Preserve any existing translator tag in
        # that case instead of accidentally clearing it on an ordinary save.
        if hasattr(metadata, "translator"):
            _set_translator(
                id3,
                getattr(
                    metadata,
                    "translator",
                    "",
                ),
            )

        comment = (
            getattr(
                metadata,
                "comment",
                "",
            )
            or ""
        ).strip()

        try:
            id3.delall("COMM")

            if comment:
                id3.add(
                    COMM(
                        encoding=3,
                        lang="eng",
                        desc="",
                        text=[comment],
                    )
                )

        except Exception:
            dbg_exc("COMM write failed")

        _set_number_slash(
            id3,
            "TRCK",
            TRCK,
            int(
                getattr(
                    metadata,
                    "trackno",
                    0,
                )
                or 0
            ),
            int(
                getattr(
                    metadata,
                    "totaltracks",
                    0,
                )
                or 0
            ),
        )

        _set_number_slash(
            id3,
            "TPOS",
            TPOS,
            int(
                getattr(
                    metadata,
                    "discno",
                    0,
                )
                or 0
            ),
            0,
        )

        if cover_path is not None:
            _replace_cover_frame(
                id3,
                Path(cover_path),
            )

        # TIPL is the ID3v2.4 involved-people frame. Mutagen converts it
        # to the v2.3 IPLS equivalent while retaining TXXX:TRANSLATOR.
        id3.update_to_v23()

        id3.save(
            path,
            v2_version=3,
        )

        dbg(
            "MP3 metadata saved: %s",
            path,
        )

    except Exception:
        dbg_exc(
            f"write_metadata_to_file failed "
            f"for {path}"
        )
        raise


@debug_call()
def embed_cover_into_file(
    path: Path,
    image_path: Path,
) -> None:
    """
    Replace the embedded cover in an MP3 or M4B.

    A recognised damaged ID3 prefix is repaired safely before
    the cover is written.
    """
    if path.suffix.lower() == ".m4b":
        return embed_m4b_cover(path, image_path)
    if path.suffix.lower() != ".mp3":
        raise RuntimeError("Supported audiobook formats are MP3 and M4B.")

    dbg(
        "embed_cover_into_file "
        "audio=%s image=%s",
        path,
        image_path,
    )

    id3 = _try_load_id3(path)

    if id3 is None:
        _repair_broken_id3_prefix(path)
        id3 = _try_load_id3(path)

    if id3 is None:
        id3 = ID3()

    try:
        _replace_cover_frame(
            id3,
            image_path,
        )

        id3.save(
            path,
            v2_version=3,
        )

        dbg(
            "Embedded cover into MP3: %s",
            path,
        )

    except Exception:
        dbg_exc(
            f"embed_cover_into_file failed "
            f"for {path}"
        )
        raise
