"""Formats supported throughout the audiobook manager."""
from pathlib import Path

SUPPORTED_AUDIO_EXTENSIONS = frozenset({'.mp3', '.m4b'})


def supported_audio(path):
    return Path(path).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
