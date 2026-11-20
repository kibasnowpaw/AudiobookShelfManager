"""M4B metadata, using MP4 atoms while preserving audio and chapter tracks."""
import math
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, AtomDataType

from .models import TrackMeta

FREEFORM_PREFIX = '----:com.apple.iTunes:'
NARRATOR_NAMES = {'narrator', 'narrated by', 'read by', 'reader'}
TRANSLATOR_NAMES = {'translator', 'translated by', 'translation'}


def load_m4b(path):
    audio = MP4(path)
    if audio.info.codec in {'drms', 'enca'}:
        raise ValueError('This M4B contains encrypted audio. Use an unencrypted M4B file.')
    length = float(audio.info.length)
    if not math.isfinite(length) or length <= 0:
        raise ValueError('No readable M4B audio stream with a positive duration was found.')
    return audio


def _texts(values):
    if isinstance(values, (str, bytes)):
        values = [values]
    result = []
    for value in values or ():
        if isinstance(value, bytes):
            kind = getattr(value, 'dataformat', AtomDataType.UTF8)
            codec = 'utf-8'
            if kind == AtomDataType.UTF16:
                codec = 'utf-16' if value[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-16-be'
            value = value.decode(codec, errors='replace')
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _text(tags, key):
    return '; '.join(_texts(tags.get(key)))


def freeform_values(tags):
    return {key[len(FREEFORM_PREFIX):]: '; '.join(_texts(value))
            for key, value in (tags or {}).items() if key.startswith(FREEFORM_PREFIX)}


def set_freeform(tags, name, value):
    # Canonicalize case variants only within our iTunes freeform namespace.
    for key in list(tags):
        if key.startswith(FREEFORM_PREFIX) and key[len(FREEFORM_PREFIX):].casefold() == name.casefold():
            del tags[key]
    if value:
        tags[FREEFORM_PREFIX + name] = [MP4FreeForm(str(value).encode('utf-8'), dataformat=AtomDataType.UTF8)]


def _role(tags, names):
    parts = [value for key, value in freeform_values(tags).items() if key.casefold() in names and value]
    return '; '.join(dict.fromkeys(parts))


def _set_role(tags, name, aliases, value):
    for key in list(tags):
        if key.startswith(FREEFORM_PREFIX) and key[len(FREEFORM_PREFIX):].casefold() in aliases:
            del tags[key]
    set_freeform(tags, name, (value or '').strip())


def _numbers(tags, key):
    try:
        pair = tags[key][0]
        return max(0, int(pair[0])), max(0, int(pair[1]))
    except (KeyError, IndexError, TypeError, ValueError):
        return 0, 0


def read_m4b(path):
    audio = load_m4b(path)
    tags = audio.tags or {}
    track, total = _numbers(tags, 'trkn')
    disc, _ = _numbers(tags, 'disk')
    composer = _text(tags, '\xa9wrt')
    return TrackMeta(
        title=_text(tags, '\xa9nam'), artist=_text(tags, '\xa9ART'),
        album=_text(tags, '\xa9alb'), albumartist=_text(tags, 'aART'),
        year=_text(tags, '\xa9day'), genre=_text(tags, '\xa9gen'),
        comment=_text(tags, 'ldes') or _text(tags, 'desc') or _text(tags, '\xa9cmt'),
        composer=composer, narrator=_role(tags, NARRATOR_NAMES) or composer,
        translator=_role(tags, TRANSLATOR_NAMES), trackno=track,
        totaltracks=total, discno=disc), float(audio.info.length)


def _set_text(tags, key, value):
    value = (value or '').strip()
    if value:
        tags[key] = [value]
    else:
        tags.pop(key, None)


def cover_atom(path):
    # covr supports JPEG and PNG. Convert other images accepted by the editor
    # to PNG in memory, without changing the selected source image.
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage
    data = Path(path).read_bytes()
    image = QImage.fromData(data)
    if image.isNull():
        raise ValueError('The selected cover could not be read as an image.')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        kind = MP4Cover.FORMAT_PNG
    elif data.startswith(b'\xff\xd8\xff'):
        kind = MP4Cover.FORMAT_JPEG
    else:
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        if not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, 'PNG'):
            raise ValueError('The selected cover could not be converted to PNG for M4B.')
        data = bytes(encoded)
        kind = MP4Cover.FORMAT_PNG
    return MP4Cover(data, imageformat=kind)


def write_m4b(path, metadata, cover_path=None):
    audio = load_m4b(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for key, field in (('\xa9nam','title'), ('\xa9ART','artist'), ('\xa9alb','album'),
                       ('aART','albumartist'), ('\xa9day','year'), ('\xa9gen','genre'),
                       ('\xa9wrt','composer')):
        _set_text(tags, key, getattr(metadata, field, ''))
    if hasattr(metadata, 'narrator'):
        _set_role(tags, 'NARRATOR', NARRATOR_NAMES, metadata.narrator)
    if hasattr(metadata, 'translator'):
        _set_role(tags, 'TRANSLATOR', TRANSLATOR_NAMES, metadata.translator)
    comment = (getattr(metadata, 'comment', '') or '').strip()
    _set_text(tags, 'ldes', comment)
    _set_text(tags, '\xa9cmt', comment)
    _set_text(tags, 'desc', comment[:255])
    number, total = int(metadata.trackno or 0), int(metadata.totaltracks or 0)
    if number > 0 or total > 0:
        tags['trkn'] = [(number, total)]
    else:
        tags.pop('trkn', None)
    # Disc is hidden in the audiobook UI. Preserve both its number and total,
    # including values that cannot be represented by the old single field.
    tags.setdefault('stik', [2])  # Audiobook media kind for previously untyped files.
    if cover_path is not None:
        tags['covr'] = [cover_atom(cover_path)]
    audio.save()


def embed_m4b_cover(path, image_path):
    audio = load_m4b(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags['covr'] = [cover_atom(image_path)]
    audio.save()
