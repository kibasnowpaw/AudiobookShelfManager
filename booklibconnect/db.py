from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from booklibconnect.paths import app_data_dir


@dataclass(frozen=True)
class Library:
    id: int
    path: str


@dataclass(frozen=True)
class ArtistRow:
    id: int
    name: str
    albums: int
    tracks: int


@dataclass(frozen=True)
class AlbumRow:
    id: int
    title: str
    year: int | None
    cover_path: str | None
    tracks: int


def db_path() -> Path:
    return app_data_dir() / "booklibconnect.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA journal_mode = WAL;")
    con.execute("PRAGMA synchronous = NORMAL;")
    return con


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS libraries (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            path      TEXT NOT NULL UNIQUE,
            added_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS artists (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS albums (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id   INTEGER NOT NULL,
            title       TEXT NOT NULL,
            year        INTEGER,
            cover_path  TEXT,
            UNIQUE(artist_id, title),
            FOREIGN KEY(artist_id) REFERENCES artists(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id   INTEGER NOT NULL,
            album_id     INTEGER NOT NULL,
            rel_path     TEXT NOT NULL,
            title        TEXT NOT NULL,
            track_no     INTEGER,
            disc_no      INTEGER,
            duration_sec INTEGER,
            size_bytes   INTEGER NOT NULL,
            mtime_unix   INTEGER NOT NULL,
            UNIQUE(library_id, rel_path),
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
            FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE CASCADE
        );
        """
    )
    con.commit()


def upsert_library(con: sqlite3.Connection, path: str) -> int:
    con.execute("INSERT OR IGNORE INTO libraries(path) VALUES(?)", (path,))
    con.commit()
    row = con.execute("SELECT id FROM libraries WHERE path=?", (path,)).fetchone()
    return int(row[0])


def list_libraries(con: sqlite3.Connection) -> list[Library]:
    rows = con.execute("SELECT id, path FROM libraries ORDER BY path COLLATE NOCASE").fetchall()
    return [Library(int(r[0]), str(r[1])) for r in rows]


def delete_library(con: sqlite3.Connection, library_id: int) -> None:
    con.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    con.commit()


def _upsert_artist(con: sqlite3.Connection, name: str) -> int:
    name = name.strip() or "Unknown"
    con.execute("INSERT OR IGNORE INTO artists(name) VALUES(?)", (name,))
    row = con.execute("SELECT id FROM artists WHERE name=?", (name,)).fetchone()
    return int(row[0])


def _upsert_album(con: sqlite3.Connection, artist_id: int, title: str, year: int | None) -> int:
    title = title.strip() or "Unknown"
    con.execute(
        "INSERT OR IGNORE INTO albums(artist_id, title, year) VALUES(?,?,?)",
        (artist_id, title, year),
    )
    row = con.execute(
        "SELECT id FROM albums WHERE artist_id=? AND title=?",
        (artist_id, title),
    ).fetchone()
    return int(row[0])


def set_album_cover_if_missing(con: sqlite3.Connection, album_id: int, cover_path: str | None) -> None:
    if not cover_path:
        return
    row = con.execute("SELECT cover_path FROM albums WHERE id=?", (album_id,)).fetchone()
    if not row:
        return
    if row[0]:
        return
    con.execute("UPDATE albums SET cover_path=? WHERE id=?", (cover_path, album_id))


def replace_tracks_for_library(con: sqlite3.Connection, library_id: int, track_rows: list[tuple]) -> None:
    # track_rows: (album_id, rel_path, title, track_no, disc_no, duration_sec, size_bytes, mtime_unix)
    con.execute("DELETE FROM tracks WHERE library_id=?", (library_id,))
    con.executemany(
        """
        INSERT INTO tracks(
          library_id, album_id, rel_path, title, track_no, disc_no, duration_sec, size_bytes, mtime_unix
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        [(library_id, *r) for r in track_rows],
    )
    con.commit()


def list_artists(con: sqlite3.Connection) -> list[ArtistRow]:
    rows = con.execute(
        """
        SELECT a.id, a.name,
               COUNT(DISTINCT al.id) AS albums,
               COUNT(t.id) AS tracks
        FROM artists a
        LEFT JOIN albums al ON al.artist_id = a.id
        LEFT JOIN tracks t ON t.album_id = al.id
        GROUP BY a.id, a.name
        ORDER BY a.name COLLATE NOCASE
        """
    ).fetchall()
    return [ArtistRow(int(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in rows]


def list_albums_for_artist(con: sqlite3.Connection, artist_id: int) -> list[AlbumRow]:
    rows = con.execute(
        """
        SELECT al.id, al.title, al.year, al.cover_path, COUNT(t.id) AS tracks
        FROM albums al
        LEFT JOIN tracks t ON t.album_id = al.id
        WHERE al.artist_id=?
        GROUP BY al.id, al.title, al.year, al.cover_path
        ORDER BY al.title COLLATE NOCASE
        """,
        (artist_id,),
    ).fetchall()
    return [AlbumRow(int(r[0]), str(r[1]), (int(r[2]) if r[2] is not None else None), (str(r[3]) if r[3] else None), int(r[4])) for r in rows]


def list_tracks_for_album(con: sqlite3.Connection, album_id: int) -> list[tuple]:
    return con.execute(
        """
        SELECT track_no, title, rel_path, duration_sec
        FROM tracks
        WHERE album_id=?
        ORDER BY COALESCE(disc_no,0), COALESCE(track_no,0), rel_path COLLATE NOCASE
        """,
        (album_id,),
    ).fetchall()


def upsert_artist_album(con: sqlite3.Connection, artist_name: str, album_title: str, year: int | None) -> tuple[int, int]:
    artist_id = _upsert_artist(con, artist_name)
    album_id = _upsert_album(con, artist_id, album_title, year)
    return artist_id, album_id