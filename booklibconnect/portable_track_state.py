from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import Lock
from uuid import uuid4

from mutagen.id3 import ID3, ID3NoHeaderError, TXXX

from .debug_tools import dbg, dbg_exc
from .audio_formats import supported_audio
from .mp4_metadata import load_m4b, freeform_values, set_freeform

APP_TRACK_ID = "ASM_TRACK_ID"
APP_WATCHED = "ASM_WATCHED"
APP_GOODREADS = "ASM_GOODREADS"
APP_AUDIBLE = "ASM_AUDIBLE"

# Cache custom values after one ID3/MP4 read. The signature makes the cache
# refresh automatically if another program changes the audiobook. The size limit
# prevents an unusually large library from growing memory use forever.
_CACHE_LIMIT = 4096
_cache_lock = Lock()
_FileStamp = tuple[int, int, int, int, int]
_TXXX_CACHE: OrderedDict[
    str,
    tuple[_FileStamp, dict[str, str]],
] = OrderedDict()


def _file_signature(track_file: Path) -> _FileStamp | None:
    try:
        stat = track_file.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def _invalidate_cache(track_file: Path) -> None:
    with _cache_lock:
        _TXXX_CACHE.pop(str(track_file), None)


def _cache_values(
    track_file: Path,
    signature: _FileStamp,
    values: dict[str, str],
) -> None:
    key = str(track_file)
    with _cache_lock:
        _TXXX_CACHE[key] = (signature, values)
        _TXXX_CACHE.move_to_end(key)
        while len(_TXXX_CACHE) > _CACHE_LIMIT:
            _TXXX_CACHE.popitem(last=False)


def _frame_text(frame: TXXX) -> str | None:
    text = getattr(frame, "text", None)

    if isinstance(text, str):
        value = text.strip()
        return value or None

    if isinstance(text, (list, tuple)) and text:
        value = str(text[0]).strip()
        return value or None

    return None


def _txxx_values_from_id3(id3: ID3) -> dict[str, str]:
    values: dict[str, str] = {}

    for frame in id3.getall("TXXX"):
        desc = str(getattr(frame, "desc", "") or "").strip()
        if not desc or desc in values:
            continue

        value = _frame_text(frame)
        if value is not None:
            values[desc] = value

    return values


def _read_all_txxx_text(track_file: Path) -> dict[str, str]:
    if not supported_audio(track_file):
        return {}

    signature = _file_signature(track_file)
    if signature is None:
        _invalidate_cache(track_file)
        return {}

    key = str(track_file)
    with _cache_lock:
        cached = _TXXX_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            _TXXX_CACHE.move_to_end(key)
            return cached[1]

    try:
        _, values = _load_state_tags(track_file)
    except ID3NoHeaderError:
        values = {}
    except Exception:
        # Store an empty result for this exact file version. This prevents the
        # same unreadable ID3 tag from producing a traceback for every flag.
        dbg_exc(f"_read_all_txxx_text failed for {track_file}")
        values = {}

    _cache_values(track_file, signature, values)
    return values


def _load_state_tags(track_file):
    if track_file.suffix.lower() == ".m4b":
        tags = load_m4b(track_file).tags or {}
        return tags, _m4b_state_values(tags)
    tags = ID3(track_file)
    return tags, _txxx_values_from_id3(tags)


def read_tags_for_book_cover(track_file):
    """Share this one audio read with the following identity and review lookups."""
    before = _file_signature(track_file)
    tags, values = _load_state_tags(track_file)
    if before is not None and before == _file_signature(track_file):
        _cache_values(track_file, before, values)  # Only small text values outlive the open.
    return tags


def _m4b_state_values(tags):
    return {key.upper(): value for key, value in freeform_values(tags).items()
            if key.upper().startswith('ASM_')}


def _write_m4b_state(track_file, desc, value, *, ensure_identity=False):
    audio = load_m4b(track_file)
    if audio.tags is None:
        audio.add_tags()
    if ensure_identity and not _m4b_state_values(audio.tags).get(APP_TRACK_ID):
        set_freeform(audio.tags, APP_TRACK_ID, uuid4().hex)
    set_freeform(audio.tags, desc, value)
    audio.save()
    signature = _file_signature(track_file)
    if signature is None:
        _invalidate_cache(track_file)
    else:
        _cache_values(track_file, signature, _m4b_state_values(audio.tags))
    return True


def _load_id3(track_file: Path) -> ID3:
    try:
        return ID3(track_file)
    except ID3NoHeaderError:
        return ID3()


def _delete_txxx(id3: ID3, desc: str) -> None:
    try:
        id3.delall(f"TXXX:{desc}")
    except Exception:
        frames = list(id3.getall("TXXX"))
        for frame in frames:
            if getattr(frame, "desc", "") == desc:
                try:
                    id3.delall(
                        getattr(
                            frame,
                            "HashKey",
                            f"TXXX:{desc}",
                        )
                    )
                except Exception:
                    pass


def _refresh_cache_from_id3(track_file: Path, id3: ID3) -> None:
    signature = _file_signature(track_file)
    if signature is None:
        _invalidate_cache(track_file)
        return

    try:
        values = _txxx_values_from_id3(id3)
    except Exception:
        _invalidate_cache(track_file)
        return

    _cache_values(track_file, signature, values)


def _read_txxx_text(
    track_file: Path,
    desc: str,
) -> str | None:
    return _read_all_txxx_text(track_file).get(desc)


def _write_txxx_text(
    track_file: Path,
    desc: str,
    value: str,
) -> bool:
    if (
        not track_file.exists()
        or not supported_audio(track_file)
    ):
        return False

    try:
        if track_file.suffix.lower() == ".m4b":
            return _write_m4b_state(track_file, desc, str(value))
        id3 = _load_id3(track_file)
        _delete_txxx(id3, desc)
        id3.add(
            TXXX(
                encoding=3,
                desc=desc,
                text=[str(value)],
            )
        )
        id3.save(track_file)
        _refresh_cache_from_id3(track_file, id3)
        return True
    except Exception:
        _invalidate_cache(track_file)
        dbg_exc(
            f"_write_txxx_text failed for "
            f"{track_file} desc={desc}"
        )
        return False


def ensure_track_id(track_file: Path) -> str | None:
    existing = _read_txxx_text(
        track_file,
        APP_TRACK_ID,
    )

    if existing:
        return existing

    new_id = uuid4().hex
    ok = _write_txxx_text(
        track_file,
        APP_TRACK_ID,
        new_id,
    )

    if ok:
        dbg(
            "Embedded new track id for %s -> %s",
            track_file,
            new_id,
        )
        return new_id

    return None


def read_bool_flag(
    track_file: Path,
    desc: str,
) -> bool | None:
    return _bool_value(_read_txxx_text(track_file, desc))


def read_review_flags(track_file: Path) -> tuple[bool | None, bool | None]:
    values = _read_all_txxx_text(track_file)
    return _bool_value(values.get(APP_GOODREADS)), _bool_value(values.get(APP_AUDIBLE))


def _bool_value(raw: str | None) -> bool | None:
    if raw is None:
        return None

    value = raw.strip().lower()

    if value in {"1", "true", "yes", "on", "y"}:
        return True

    if value in {"0", "false", "no", "off", "n"}:
        return False

    return None


def write_bool_flag(
    track_file: Path,
    desc: str,
    value: bool,
) -> bool:
    if (
        not track_file.exists()
        or not supported_audio(track_file)
    ):
        return False

    try:
        if track_file.suffix.lower() == ".m4b":
            return _write_m4b_state(track_file, desc, "1" if value else "0", ensure_identity=True)
        # Add the track ID and requested flag in one ID3 save instead of
        # opening and rewriting the MP3 twice when the ID is missing.
        id3 = _load_id3(track_file)
        values = _txxx_values_from_id3(id3)

        if not values.get(APP_TRACK_ID):
            new_id = uuid4().hex
            _delete_txxx(id3, APP_TRACK_ID)
            id3.add(
                TXXX(
                    encoding=3,
                    desc=APP_TRACK_ID,
                    text=[new_id],
                )
            )
            dbg(
                "Embedded new track id for %s -> %s",
                track_file,
                new_id,
            )

        _delete_txxx(id3, desc)
        id3.add(
            TXXX(
                encoding=3,
                desc=desc,
                text=["1" if value else "0"],
            )
        )
        id3.save(track_file)
        _refresh_cache_from_id3(track_file, id3)
        return True
    except Exception:
        _invalidate_cache(track_file)
        dbg_exc(
            f"write_bool_flag failed for "
            f"{track_file} desc={desc}"
        )
        return False
