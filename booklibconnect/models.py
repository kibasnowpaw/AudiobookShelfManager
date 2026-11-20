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

    # Additional audiobook metadata fields
    comment: str = ""     # ID3 COMM (description/comment)
    composer: str = ""    # ID3 TCOM
    narrator: str = ""    # ID3 TIPL role "narrator" + TXXX fallback
    translator: str = ""  # ID3 TIPL role "translator" + TXXX fallback
    discno: int = 0        # ID3 TPOS

    trackno: int = 0
    totaltracks: int = 0
