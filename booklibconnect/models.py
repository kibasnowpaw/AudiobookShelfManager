from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TrackMeta:
    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    year: str = ""
    genre: str = ""

    # New fields
    comment: str = ""     # ID3 COMM (good for audiobook "description")
    composer: str = ""    # ID3 TCOM
    discno: int = 0       # ID3 TPOS

    trackno: int = 0
    totaltracks: int = 0
