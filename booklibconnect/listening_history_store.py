from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .debug_tools import dbg, dbg_exc, debug_call
from .models import TrackMeta
from .portable_track_state import ensure_track_id
from .audio_formats import supported_audio
from .rating_values import normalize_rating, rating_value
from .purchase_info import validate_purchase


SCHEMA_VERSION = 5
DEFAULT_BACKUP_LIMIT = 10
DATA_DIRECTORY_ENV = "BOOKLIBCONNECT_DATA_DIR"


def _completed_event_predicate(alias: str = "") -> str:
    """SQL condition for a real completed listen with a From/To period."""
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}started_on IS NOT NULL "
        f"AND TRIM({prefix}started_on) <> '' "
        f"AND {prefix}finished_on IS NOT NULL "
        f"AND TRIM({prefix}finished_on) <> ''"
    )


def persistent_data_dir() -> Path:
    """
    Return BookLibConnect's permanent host-data directory.

    This deliberately does not use XDG_DATA_HOME. Flatpak changes that
    environment variable to the app-specific ~/.var/app directory, which can
    be deleted by ``flatpak uninstall --delete-data``. With the matching
    Flatpak filesystem permission, this path remains outside the sandbox's
    disposable app-data directory.

    BOOKLIBCONNECT_DATA_DIR can be used for tests or a deliberately selected
    custom storage location.
    """
    override = os.environ.get(DATA_DIRECTORY_ENV, "").strip()
    if override:
        return Path(override).expanduser()

    return Path.home() / ".local" / "share" / "booklibconnect"


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _duration_or_none(value: object) -> float | None:
    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Duration must be a number of seconds or None.") from None

    if not math.isfinite(number) or number < 0.0:
        raise ValueError("Duration must be a finite, non-negative number.")

    return number


def _date_text(
    value: date | str | None,
    *,
    field_name: str,
    required: bool,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None

    if isinstance(value, datetime):
        value = value.date()

    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None

    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{field_name} must use a real YYYY-MM-DD date."
        ) from None

    # Reject accepted but non-canonical input such as years without four
    # digits. The stored text can therefore be sorted and filtered directly.
    canonical = parsed.isoformat()
    if text != canonical:
        raise ValueError(f"{field_name} must use YYYY-MM-DD.")
    return canonical


def _inclusive_listening_days(
    started_on: object,
    finished_on: object,
) -> int | None:
    """Return elapsed calendar days, counting both start and finish dates."""
    try:
        started = date.fromisoformat(_clean_text(started_on))
        finished = date.fromisoformat(_clean_text(finished_on))
    except ValueError:
        return None

    day_count = (finished - started).days + 1
    return day_count if day_count > 0 else None


@dataclass(frozen=True)
class BookRecord:
    track_id: str
    last_path: str
    title: str
    author: str
    series: str
    narrator: str
    translator: str
    duration_seconds: float | None
    last_seen_at: str
    rating: float
    favorite: bool
    reviewed: bool = False
    purchased_on: str = ''
    purchase_source: str = ''


@dataclass(frozen=True)
class ListeningEvent:
    event_id: int
    track_id: str
    started_on: str | None
    finished_on: str
    duration_seconds: float | None
    note: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ListeningSummary:
    year: int | None
    completed_listens: int
    unique_books: int
    relistens: int
    total_seconds: float
    longest_track_id: str | None
    longest_title: str | None
    longest_seconds: float | None


class ListeningHistoryStore:
    """
    Permanent audiobook identity, duration and listening-history storage.

    The scan index remains a rebuildable performance cache. This SQLite file
    is user data and must never be removed merely because a scan is rebuilt.
    Each completed listen keeps a duration snapshot so historical totals do
    not change if an audiobook file is later replaced or edited.
    """

    @debug_call()
    def __init__(
        self,
        data_directory: Path | None = None,
        *,
        backup_limit: int = DEFAULT_BACKUP_LIMIT,
        automatic_daily_backup: bool = True,
    ) -> None:
        self._data_directory = (
            Path(data_directory).expanduser()
            if data_directory is not None
            else persistent_data_dir()
        )
        self._backup_limit = max(1, int(backup_limit))

        self._data_directory.mkdir(parents=True, exist_ok=True)
        self.backup_dir().mkdir(parents=True, exist_ok=True)

        self._prepare_database()

        if automatic_daily_backup:
            self._maybe_create_daily_backup()

    def data_dir(self) -> Path:
        return self._data_directory

    def database_path(self) -> Path:
        return self.data_dir() / "library_state.sqlite3"

    def backup_dir(self) -> Path:
        return self.data_dir() / "backups"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path(),
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self):
        # sqlite3.Connection.__exit__ commits/rolls back; it does not close.
        # In particular, long-running GUI processes must not wait for cyclic GC.
        with closing(self._connect()) as connection, connection:
            yield connection

    def _prepare_database(self) -> None:
        database_existed = self.database_path().exists()

        def version(connection):
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError("The listening-history database was created by a newer "
                                   "Audiobook Shelf Manager version. Upgrade the app before opening it.")
            return current

        with self._transaction() as connection:
            current_version = version(connection)
            if current_version == SCHEMA_VERSION and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_books_last_path_seen'"
            ).fetchone():
                return  # A normal launch need not acquire a migration write lock.
            has_books = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books'"
            ).fetchone()

        if database_existed and has_books and current_version < SCHEMA_VERSION:
            self.create_backup(label=f"before-v{current_version}-to-v{SCHEMA_VERSION}", prune=False)

        with self._transaction() as connection:
            # DDL and user_version must commit together, including on older
            # sqlite3 versions that don't start a transaction for ALTER TABLE.
            connection.execute("BEGIN IMMEDIATE")
            original_version = current_version = version(connection)
            if current_version == 0:
                self._create_schema_v1(connection)
                current_version = 1

            if current_version < 2:
                self._migrate_schema_v1_to_v2(connection)
                current_version = 2

            if current_version < 3:
                # SQLite's non-STRICT INTEGER affinity already preserves
                # fractional values as REAL. No book or history table rebuild
                # is needed. The version guard prevents older integer-only
                # builds from opening the database and truncating ratings.
                current_version = 3

            if current_version < 4:
                connection.execute('ALTER TABLE books ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1))')
                current_version = 4

            if current_version < 5:
                connection.execute("ALTER TABLE books ADD COLUMN purchased_on TEXT NOT NULL DEFAULT ''")
                connection.execute("ALTER TABLE books ADD COLUMN purchase_source TEXT NOT NULL DEFAULT ''")
                current_version = 5

            connection.execute("CREATE INDEX IF NOT EXISTS idx_books_last_path_seen "
                               "ON books(last_path, last_seen_at DESC)")
            if current_version != original_version:
                connection.execute(f"PRAGMA user_version = {current_version}")


        dbg(
            "Listening history database ready: %s (schema %d)",
            self.database_path(),
            SCHEMA_VERSION,
        )

    def _create_schema_v1(self, connection: sqlite3.Connection) -> None:
        for statement in (
            """CREATE TABLE IF NOT EXISTS books (
                track_id TEXT PRIMARY KEY,
                last_path TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                series TEXT NOT NULL DEFAULT '',
                narrator TEXT NOT NULL DEFAULT '',
                translator TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                last_seen_at TEXT NOT NULL,
                CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
            )""",
            """CREATE TABLE IF NOT EXISTS listening_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id TEXT NOT NULL,
                started_on TEXT,
                finished_on TEXT NOT NULL,
                duration_seconds REAL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (track_id) REFERENCES books(track_id)
                    ON UPDATE CASCADE
                    ON DELETE RESTRICT,
                CHECK (started_on IS NULL OR started_on <= finished_on),
                CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_listening_events_track
                ON listening_events(track_id, finished_on, id)""",
            """CREATE INDEX IF NOT EXISTS idx_listening_events_finished
                ON listening_events(finished_on, id)""",
        ):
            connection.execute(statement)

    def _migrate_schema_v1_to_v2(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Add durable personal rating and favorite state to each book."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(books)")
        }

        if "rating" not in columns:
            connection.execute(
                """
                ALTER TABLE books
                ADD COLUMN rating INTEGER NOT NULL DEFAULT 0
                    CHECK (rating BETWEEN 0 AND 5)
                """
            )

        if "favorite" not in columns:
            connection.execute(
                """
                ALTER TABLE books
                ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0
                    CHECK (favorite IN (0, 1))
                """
            )

    @debug_call()
    def register_track(
        self,
        track_path: Path,
        metadata: TrackMeta,
        duration_seconds: float | None,
    ) -> str:
        """Ensure a permanent ID and refresh the saved book snapshot."""
        values = self._track_registration(track_path, metadata, duration_seconds)
        self.upsert_book(**values)
        return values["track_id"]

    @staticmethod
    def _track_registration(track_path, metadata, duration_seconds):
        """Resolve the identity from the audio file, never from the old database path."""
        track_path = Path(track_path)
        if not track_path.exists() or not supported_audio(track_path):
            raise ValueError(f"Audiobook file does not exist: {track_path}")

        track_id = ensure_track_id(track_path)
        if not track_id:
            raise RuntimeError(
                "Could not create or read the audiobook's permanent track ID."
            )

        author = _clean_text(getattr(metadata, "albumartist", ""))
        if not author:
            author = _clean_text(getattr(metadata, "artist", ""))

        title = _clean_text(getattr(metadata, "title", "")) or track_path.stem
        narrator = _clean_text(getattr(metadata, "narrator", ""))
        if not narrator:
            # Older audiobook libraries commonly stored the narrator in TCOM.
            narrator = _clean_text(getattr(metadata, "composer", ""))

        return dict(
            track_id=_clean_text(track_id), last_path=_clean_text(track_path),
            title=title, author=author, series=_clean_text(getattr(metadata, "album", "")),
            narrator=narrator, translator=_clean_text(getattr(metadata, "translator", "")),
            duration_seconds=_duration_or_none(duration_seconds),
        )

    def details_for_file(self, path, metadata, duration_seconds):
        """Read current preferences and listens together. Only changed books need a write."""
        values = self._track_registration(path, metadata, duration_seconds)
        track_id = values["track_id"]
        length = values["duration_seconds"]
        with self._transaction() as connection:
            connection.execute("BEGIN")  # Both reads see the same SQLite snapshot.
            rows = connection.execute("SELECT * FROM books WHERE last_path = ? LIMIT 2",
                                      (values["last_path"],)).fetchall()
            if (len(rows) == 1
                    and all(rows[0][k] == values[k] for k in
                            ("track_id", "title", "author", "series", "narrator", "translator"))
                    and (length is None or length == rows[0]["duration_seconds"])):
                events = self._listens_on(connection, track_id)
                if length is None or all(e.duration_seconds is not None for e in events):
                    return self._book_from_row(rows[0]), events, False

            # Release the read snapshot before taking the writer lock. The usual
            # upsert then re-reads replacement identities under that lock.
            connection.rollback()
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_on(connection, **values)
            row = connection.execute("SELECT * FROM books WHERE track_id = ?", (track_id,)).fetchone()
            return self._book_from_row(row), self._listens_on(connection, track_id), True

    @debug_call()
    def upsert_book(
        self,
        *,
        track_id: str,
        last_path: Path | str,
        title: str = "",
        author: str = "",
        series: str = "",
        narrator: str = "",
        translator: str = "",
        duration_seconds: float | None = None,
    ) -> None:
        with self._transaction() as connection:
            self._upsert_on(connection, track_id=track_id, last_path=last_path, title=title,
                            author=author, series=series, narrator=narrator, translator=translator,
                            duration_seconds=duration_seconds)

    def _upsert_on(self, connection, *, track_id, last_path, title, author, series,
                   narrator, translator, duration_seconds):
        clean_track_id = _clean_text(track_id)
        if not clean_track_id:
            raise ValueError("track_id is required.")

        clean_path = _clean_text(last_path)
        if not clean_path:
            raise ValueError("last_path is required.")

        duration = _duration_or_none(duration_seconds)
        now = _utc_now_text()

        replacement_rows = connection.execute(
            """
            SELECT track_id, duration_seconds, rating, favorite, reviewed, purchased_on, purchase_source
            FROM books
            WHERE last_path = ?
              AND track_id <> ?
            ORDER BY last_seen_at DESC
            """,
            (clean_path, clean_track_id),
        ).fetchall()

        # A replacement can supply a new duration for an older Unknown
        # event. If the replacement itself has no readable duration, keep
        # the newest known value instead of erasing it.
        effective_duration = duration
        if effective_duration is None:
            for replacement_row in replacement_rows:
                previous_duration = replacement_row["duration_seconds"]
                if previous_duration is not None:
                    effective_duration = float(previous_duration)
                    break

        connection.execute(
            """
            INSERT INTO books (
                track_id,
                last_path,
                title,
                author,
                series,
                narrator,
                translator,
                duration_seconds,
                last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                last_path = excluded.last_path,
                title = excluded.title,
                author = excluded.author,
                series = excluded.series,
                narrator = excluded.narrator,
                translator = excluded.translator,
                duration_seconds = COALESCE(
                    excluded.duration_seconds,
                    books.duration_seconds
                ),
                last_seen_at = excluded.last_seen_at
            """,
            (
                clean_track_id,
                clean_path,
                _clean_text(title),
                _clean_text(author),
                _clean_text(series),
                _clean_text(narrator),
                _clean_text(translator),
                effective_duration,
                now,
            ),
        )

        # When a redownloaded MP3 replaces an older file at the same
        # path, keep the user's rating and favorite choice with the book.
        inherited_rating = max(
            (
                rating_value(row["rating"])
                for row in replacement_rows
            ),
            default=0,
        )
        inherited_favorite = any(
            bool(row["favorite"])
            for row in replacement_rows
        )
        inherited_reviewed = any(bool(row['reviewed']) for row in replacement_rows)
        if inherited_rating or inherited_favorite or inherited_reviewed:
            connection.execute(
                """
                UPDATE books
                SET rating = CASE
                        WHEN rating = 0 THEN ?
                        ELSE rating
                    END,
                    favorite = CASE
                        WHEN favorite = 0 THEN ?
                        ELSE favorite
                    END,
                    reviewed = MAX(reviewed, ?)
                WHERE track_id = ?
                """,
                (
                    inherited_rating,
                    1 if inherited_favorite else 0,
                    1 if inherited_reviewed else 0,
                    clean_track_id,
                ),
            )

        # Adopt purchase details as a pair from the newest replaced record.
        # An identity that already has purchase information keeps that pair.
        inherited_purchase = next((row for row in replacement_rows
                                   if row['purchased_on'] or row['purchase_source']), None)
        if inherited_purchase is not None:
            connection.execute(
                "UPDATE books SET purchased_on = ?, purchase_source = ? "
                "WHERE track_id = ? AND purchased_on = '' AND purchase_source = ''",
                (inherited_purchase['purchased_on'], inherited_purchase['purchase_source'], clean_track_id))

        migrated_events = 0
        for replacement_row in replacement_rows:
            old_track_id = str(replacement_row["track_id"])
            cursor = connection.execute(
                """
                UPDATE listening_events
                SET track_id = ?,
                    duration_seconds = COALESCE(
                        duration_seconds,
                        ?
                    ),
                    updated_at = ?
                WHERE track_id = ?
                """,
                (
                    clean_track_id,
                    effective_duration,
                    now,
                    old_track_id,
                ),
            )
            migrated_events += max(0, int(cursor.rowcount))
            connection.execute(
                "DELETE FROM books WHERE track_id = ?",
                (old_track_id,),
            )

        if effective_duration is not None:
            connection.execute(
                """
                UPDATE listening_events
                SET duration_seconds = ?,
                    updated_at = ?
                WHERE track_id = ?
                  AND duration_seconds IS NULL
                """,
                (effective_duration, now, clean_track_id),
            )

        if replacement_rows:
            dbg(
                "Adopted replacement audiobook at %s: "
                "%d old ID(s), %d listening event(s) moved to %s",
                clean_path,
                len(replacement_rows),
                migrated_events,
                clean_track_id,
            )

    @debug_call()
    def book(self, track_id: str) -> BookRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM books WHERE track_id = ?",
                (_clean_text(track_id),),
            ).fetchone()

        return self._book_from_row(row) if row is not None else None

    @debug_call()
    def book_for_path(self, track_path: Path | str) -> BookRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM books
                WHERE last_path = ?
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (_clean_text(track_path),),
            ).fetchone()

        return self._book_from_row(row) if row is not None else None

    def _book_from_row(self, row: sqlite3.Row) -> BookRecord:
        raw_duration = row["duration_seconds"]
        return BookRecord(
            track_id=str(row["track_id"]),
            last_path=str(row["last_path"]),
            title=str(row["title"]),
            author=str(row["author"]),
            series=str(row["series"]),
            narrator=str(row["narrator"]),
            translator=str(row["translator"]),
            duration_seconds=(
                float(raw_duration)
                if raw_duration is not None
                else None
            ),
            last_seen_at=str(row["last_seen_at"]),
            rating=rating_value(row["rating"]),
            favorite=bool(row["favorite"]),
            reviewed=bool(row["reviewed"]),
            purchased_on=str(row["purchased_on"]),
            purchase_source=str(row["purchase_source"]),
        )

    @debug_call()
    def book_preferences(self, track_id: str) -> tuple[float, bool]:
        clean_track_id = _clean_text(track_id)
        if not clean_track_id:
            return 0, False

        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT rating, favorite
                FROM books
                WHERE track_id = ?
                """,
                (clean_track_id,),
            ).fetchone()

        if row is None:
            return 0, False
        return rating_value(row["rating"]), bool(row["favorite"])

    @debug_call()
    def book_ratings_by_path(self) -> dict[str, float]:
        """Return all saved ratings in one query for fast library cards."""
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT last_path, MAX(rating) AS rating
                FROM books
                WHERE rating > 0 AND rating <= 5
                  AND TRIM(last_path) <> ''
                GROUP BY last_path
                """
            ).fetchall()

        return {
            path: rating
            for row in rows
            if (path := _clean_text(row["last_path"]))
            and 0 < (rating := rating_value(row["rating"])) <= 5
        }

    def set_purchase(self, track_id: str, purchased_on='', source='') -> None:
        purchased_on, source = validate_purchase(purchased_on, source)
        with self._transaction() as connection:
            cursor = connection.execute(
                'UPDATE books SET purchased_on = ?, purchase_source = ? WHERE track_id = ?',
                (purchased_on, source, _clean_text(track_id)))
            if not cursor.rowcount:
                raise ValueError('Open the audiobook before recording its purchase.')

    def reviewed_paths(self) -> set[str]:
        """Bulk personal review state, scoped to this library."""
        with self._transaction() as connection:
            return {str(row[0]) for row in connection.execute(
                'SELECT last_path FROM books WHERE reviewed = 1')}

    def set_reviewed(self, track_id: str, reviewed: bool) -> None:
        with self._transaction() as connection:
            cursor = connection.execute('UPDATE books SET reviewed = ? WHERE track_id = ?',
                                        (int(bool(reviewed)), _clean_text(track_id)))
            if not cursor.rowcount:
                raise ValueError('Open the audiobook before marking it reviewed.')

    @debug_call()
    def set_rating(self, track_id: str, rating: float) -> None:
        clean_track_id = _clean_text(track_id)
        clean_rating = normalize_rating(rating)
        if not clean_track_id:
            raise ValueError("track_id is required.")
        if clean_rating < 0 or clean_rating > 5:
            raise ValueError("Rating must be between 0 and 5 stars.")

        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE books SET rating = ? WHERE track_id = ?",
                (clean_rating, clean_track_id),
            )
            if cursor.rowcount <= 0:
                raise ValueError(
                    "The audiobook must be registered before rating it."
                )

    @debug_call()
    def set_favorite(self, track_id: str, favorite: bool) -> None:
        clean_track_id = _clean_text(track_id)
        if not clean_track_id:
            raise ValueError("track_id is required.")

        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE books SET favorite = ? WHERE track_id = ?",
                (1 if favorite else 0, clean_track_id),
            )
            if cursor.rowcount <= 0:
                raise ValueError(
                    "The audiobook must be registered before favoriting it."
                )

    @debug_call()
    def add_completed_listen(
        self,
        track_id: str,
        *,
        finished_on: date | str,
        started_on: date | str | None = None,
        duration_seconds: float | None = None,
        note: str = "",
    ) -> int:
        clean_track_id = _clean_text(track_id)
        start_text = _date_text(
            started_on,
            field_name="Started date",
            required=True,
        )
        finish_text = _date_text(
            finished_on,
            field_name="Finished date",
            required=True,
        )
        assert start_text is not None
        assert finish_text is not None

        if start_text is not None and start_text > finish_text:
            raise ValueError("Started date cannot be after finished date.")

        with self._transaction() as connection:
            book_row = connection.execute(
                "SELECT duration_seconds FROM books WHERE track_id = ?",
                (clean_track_id,),
            ).fetchone()
            if book_row is None:
                raise KeyError(
                    "The audiobook must be registered before adding history."
                )

            if duration_seconds is None:
                raw_duration = book_row["duration_seconds"]
                duration = (
                    float(raw_duration)
                    if raw_duration is not None
                    else None
                )
            else:
                duration = _duration_or_none(duration_seconds)

            now = _utc_now_text()
            cursor = connection.execute(
                """
                INSERT INTO listening_events (
                    track_id,
                    started_on,
                    finished_on,
                    duration_seconds,
                    note,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_track_id,
                    start_text,
                    finish_text,
                    duration,
                    _clean_text(note),
                    now,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)

        return event_id

    @debug_call()
    def update_listen(
        self,
        event_id: int,
        *,
        finished_on: date | str,
        started_on: date | str | None = None,
        duration_seconds: float | None = None,
        note: str = "",
    ) -> None:
        start_text = _date_text(
            started_on,
            field_name="Started date",
            required=True,
        )
        finish_text = _date_text(
            finished_on,
            field_name="Finished date",
            required=True,
        )
        assert start_text is not None
        assert finish_text is not None

        if start_text is not None and start_text > finish_text:
            raise ValueError("Started date cannot be after finished date.")

        duration = _duration_or_none(duration_seconds)

        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE listening_events
                SET started_on = ?,
                    finished_on = ?,
                    duration_seconds = ?,
                    note = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    start_text,
                    finish_text,
                    duration,
                    _clean_text(note),
                    _utc_now_text(),
                    int(event_id),
                ),
            )

            if cursor.rowcount != 1:
                raise KeyError(f"Listening entry {event_id} was not found.")

    @debug_call()
    def delete_listen(self, event_id: int) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM listening_events WHERE id = ?",
                (int(event_id),),
            )
        return cursor.rowcount == 1

    @debug_call()
    def listens_for_book(self, track_id: str) -> list[ListeningEvent]:
        with self._transaction() as connection:
            return self._listens_on(connection, _clean_text(track_id))

    def _listens_on(self, connection, track_id):
        rows = connection.execute(
            "SELECT * FROM listening_events WHERE track_id = ? ORDER BY finished_on DESC, id DESC",
            (track_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _event_from_row(self, row: sqlite3.Row) -> ListeningEvent:
        raw_duration = row["duration_seconds"]
        return ListeningEvent(
            event_id=int(row["id"]),
            track_id=str(row["track_id"]),
            started_on=(
                str(row["started_on"])
                if row["started_on"] is not None
                else None
            ),
            finished_on=str(row["finished_on"]),
            duration_seconds=(
                float(raw_duration)
                if raw_duration is not None
                else None
            ),
            note=str(row["note"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @debug_call()
    def listen_count(
        self,
        track_id: str,
        *,
        year: int | None = None,
    ) -> int:
        parameters: list[object] = [_clean_text(track_id)]
        where = (
            "track_id = ? AND "
            f"{_completed_event_predicate()}"
        )

        if year is not None:
            clean_year = self._validate_year(year)
            where += " AND finished_on >= ? AND finished_on < ?"
            parameters.extend(
                [
                    f"{clean_year:04d}-01-01",
                    f"{clean_year + 1:04d}-01-01",
                ]
            )

        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM listening_events WHERE {where}",
                parameters,
            ).fetchone()

        return int(row["count"])

    @debug_call()
    def last_listened_on(self, track_id: str) -> str | None:
        with self._transaction() as connection:
            row = connection.execute(
                f"""
                SELECT MAX(finished_on) AS finished_on
                FROM listening_events
                WHERE track_id = ?
                  AND {_completed_event_predicate()}
                """,
                (_clean_text(track_id),),
            ).fetchone()

        value = row["finished_on"]
        return str(value) if value is not None else None

    @debug_call()
    def has_completed_listen(self, track_id: str) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                f"""
                SELECT 1
                FROM listening_events
                WHERE track_id = ?
                  AND {_completed_event_predicate()}
                LIMIT 1
                """,
                (_clean_text(track_id),),
            ).fetchone()
        return row is not None

    @debug_call()
    def completed_book_last_listened(self) -> dict[str, str]:
        """Return the latest valid From/To completion date for each book."""
        return {
            path: finished_on
            for path, (finished_on, _day_count) in (
                self.completed_book_latest_listens().items()
            )
        }

    @debug_call()
    def completed_book_latest_listens(
        self,
    ) -> dict[str, tuple[str, int]]:
        """
        Return the latest completed listen date and inclusive day count.

        The newest valid From/To entry is used for each audiobook. A listen
        started and finished on the same date therefore has a duration of one
        calendar day.
        """
        with self._transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    b.last_path,
                    e.started_on,
                    e.finished_on
                FROM books AS b
                JOIN listening_events AS e
                  ON e.track_id = b.track_id
                WHERE {_completed_event_predicate("e")}
                ORDER BY
                    b.last_path,
                    e.finished_on DESC,
                    e.id DESC
                """
            ).fetchall()

        latest: dict[str, tuple[str, int]] = {}
        for row in rows:
            path = _clean_text(row["last_path"])
            if not path or path in latest:
                continue

            finished_on = _clean_text(row["finished_on"])
            day_count = _inclusive_listening_days(
                row["started_on"],
                row["finished_on"],
            )
            if not finished_on or day_count is None:
                continue

            latest[path] = (finished_on, day_count)

        return latest

    @debug_call()
    def latest_listening_days(self, track_id: str) -> int | None:
        """Return inclusive calendar days for the newest completed listen."""
        with self._transaction() as connection:
            row = connection.execute(
                f"""
                SELECT started_on, finished_on
                FROM listening_events
                WHERE track_id = ?
                  AND {_completed_event_predicate()}
                ORDER BY finished_on DESC, id DESC
                LIMIT 1
                """,
                (_clean_text(track_id),),
            ).fetchone()

        if row is None:
            return None

        return _inclusive_listening_days(
            row["started_on"],
            row["finished_on"],
        )

    @debug_call()
    def completed_book_listen_counts(self) -> dict[str, int]:
        """Return the number of valid completed listens for each book path."""
        with self._transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    b.last_path,
                    COUNT(*) AS completed_listens
                FROM books AS b
                JOIN listening_events AS e
                  ON e.track_id = b.track_id
                WHERE {_completed_event_predicate("e")}
                GROUP BY b.track_id, b.last_path
                """
            ).fetchall()

        return {
            path: count
            for row in rows
            if (path := _clean_text(row["last_path"]))
            and (count := int(row["completed_listens"] or 0)) > 0
        }

    @debug_call()
    def completed_book_paths(self) -> set[str]:
        """Return paths for books with at least one dated From/To listen."""
        return set(self.completed_book_last_listened())

    @debug_call()
    def summary(self, year: int | None = None) -> ListeningSummary:
        parameters: list[object] = []
        conditions = [_completed_event_predicate("e")]

        if year is not None:
            clean_year = self._validate_year(year)
            conditions.append("e.finished_on >= ? AND e.finished_on < ?")
            parameters.extend(
                [
                    f"{clean_year:04d}-01-01",
                    f"{clean_year + 1:04d}-01-01",
                ]
            )

        where = "WHERE " + " AND ".join(conditions)

        with self._transaction() as connection:
            totals = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS completed_listens,
                    COUNT(DISTINCT e.track_id) AS unique_books,
                    COALESCE(SUM(e.duration_seconds), 0.0) AS total_seconds
                FROM listening_events AS e
                {where}
                """,
                parameters,
            ).fetchone()

            relistens = connection.execute(
                f"""
                SELECT COUNT(*) AS relistens
                FROM listening_events AS e
                {where}
                AND EXISTS (
                    SELECT 1
                    FROM listening_events AS earlier
                    WHERE earlier.track_id = e.track_id
                      AND {_completed_event_predicate("earlier")}
                      AND (
                          earlier.finished_on < e.finished_on
                          OR (
                              earlier.finished_on = e.finished_on
                              AND earlier.id < e.id
                          )
                      )
                )
                """,
                parameters,
            ).fetchone()

            longest = connection.execute(
                f"""
                SELECT
                    e.track_id,
                    b.title,
                    e.duration_seconds
                FROM listening_events AS e
                JOIN books AS b ON b.track_id = e.track_id
                {where}
                AND e.duration_seconds IS NOT NULL
                ORDER BY e.duration_seconds DESC, e.finished_on DESC, e.id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()

        return ListeningSummary(
            year=year,
            completed_listens=int(totals["completed_listens"]),
            unique_books=int(totals["unique_books"]),
            relistens=int(relistens["relistens"]),
            total_seconds=float(totals["total_seconds"] or 0.0),
            longest_track_id=(
                str(longest["track_id"])
                if longest is not None
                else None
            ),
            longest_title=(
                str(longest["title"])
                if longest is not None
                else None
            ),
            longest_seconds=(
                float(longest["duration_seconds"])
                if longest is not None
                else None
            ),
        )

    @staticmethod
    def _removal_state(connection, track_id):
        book = connection.execute('SELECT * FROM books WHERE track_id = ?', (track_id,)).fetchone()
        events = connection.execute('SELECT * FROM listening_events WHERE track_id = ? ORDER BY id', (track_id,)).fetchall()
        return {'book': dict(book) if book is not None else None,
                'events': [dict(event) for event in events]}

    def removal_snapshot(self, track_id):
        with self._transaction() as connection:
            connection.execute('BEGIN')
            return self._removal_state(connection, track_id)

    def remove_book(self, track_id, expected_state, before_commit=None):
        """Remove only the explicitly reviewed book, in one transaction.

        The optional file action runs after SQL validation/deletion but before
        commit. A failed file action rolls the saved-data deletion back.
        """
        with self._transaction() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if self._removal_state(connection, track_id) != expected_state:
                raise RuntimeError('This book changed while the removal dialog was open. Refresh and try again.')
            connection.execute('DELETE FROM listening_events WHERE track_id = ?', (track_id,))
            connection.execute('DELETE FROM books WHERE track_id = ?', (track_id,))
            if before_commit is not None:
                before_commit()

    def reconcile_scanned_paths(self, records) -> int:
        """Follow an existing identity after a rename, without merging books.

        Called by the completed scan worker with validated, cached IDs. A
        deleted file never deletes history. Duplicate IDs or a destination
        already assigned to another book are deliberately left unchanged.
        """
        candidates = {}
        for path, record in records.items():
            track_id = str(record.track_id or '').strip()
            if track_id:
                candidates.setdefault(track_id, set()).add(str(path))
        changed = 0
        with self._transaction() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for book in connection.execute('SELECT track_id, last_path FROM books').fetchall():
                paths = candidates.get(book['track_id'], set())
                if len(paths) != 1 or book['last_path'] in paths:
                    continue
                path = next(iter(paths))
                conflict = connection.execute(
                    'SELECT 1 FROM books WHERE last_path = ? AND track_id <> ?',
                    (path, book['track_id']),
                ).fetchone()
                if conflict is not None:
                    continue
                cursor = connection.execute(
                    'UPDATE books SET last_path = ? WHERE track_id = ? AND last_path = ?',
                    (path, book['track_id'], book['last_path']),
                )
                changed += max(0, cursor.rowcount)
        return changed

    def gallery_snapshot(self):
        """One consistent read for progress, relistens, decimal ratings and reviews."""
        latest, counts, ratings, reviewed = {}, {}, {}, set()
        with self._transaction() as connection:
            connection.execute("BEGIN")
            for row in connection.execute(
                "SELECT last_path, MAX(CASE WHEN rating > 0 AND rating <= 5 THEN rating "
                "ELSE 0 END) AS rating, MAX(reviewed) AS reviewed FROM books GROUP BY last_path"
            ):
                path = _clean_text(row["last_path"])
                if row["reviewed"]:
                    reviewed.add(str(row["last_path"]))
                if path and (value := rating_value(row["rating"])):
                    ratings[path] = value
            for row in connection.execute(f"""
                SELECT b.last_path, e.started_on, e.finished_on
                FROM books b JOIN listening_events e ON e.track_id = b.track_id
                WHERE {_completed_event_predicate('e')}
                ORDER BY b.last_path, e.finished_on DESC, e.id DESC
            """):
                path = _clean_text(row["last_path"])
                if not path:
                    continue
                counts[path] = counts.get(path, 0) + 1
                if path not in latest:
                    days = _inclusive_listening_days(row["started_on"], row["finished_on"])
                    if days is not None:
                        latest[path] = (_clean_text(row["finished_on"]), days)
        return latest, counts, ratings, reviewed

    def statistics_snapshot(self) -> dict[str, list[dict]]:
        """Read books and history in one consistent, read-only transaction.

        This may run on a worker thread. It never opens media files, creates
        identities, or substitutes a book's current length for an event snapshot.
        """
        uri = self.database_path().absolute().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            books = connection.execute(
                "SELECT track_id, last_path, title, author, series, narrator, "
                "rating, favorite, reviewed, purchased_on, purchase_source FROM books"
            ).fetchall()
            events = connection.execute(
                "SELECT id, track_id, started_on, finished_on, "
                "duration_seconds, note FROM listening_events"
            ).fetchall()
            return {
                "books": [dict(row) for row in books],
                "events": [dict(row) for row in events],
            }
        finally:
            connection.close()

    def _validate_year(self, year: int) -> int:
        try:
            value = int(year)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Year must be a number.") from None

        if value < 1 or value > 9998:
            raise ValueError("Year must be between 1 and 9998.")
        return value

    @debug_call()
    def integrity_check(self) -> bool:
        try:
            with self._transaction() as connection:
                row = connection.execute("PRAGMA integrity_check").fetchone()
            return row is not None and str(row[0]).casefold() == "ok"
        except Exception:
            dbg_exc(
                f"Listening database integrity check failed for "
                f"{self.database_path()}"
            )
            return False

    @debug_call()
    def create_backup(
        self,
        *,
        label: str = "automatic",
        prune: bool = True,
    ) -> Path:
        self.backup_dir().mkdir(parents=True, exist_ok=True)

        safe_label = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in _clean_text(label)
        ).strip("-") or "backup"

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = self.backup_dir() / (
            f"library_state-{timestamp}-{safe_label}.sqlite3"
        )

        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(destination)) as backup:
                source.backup(backup)
                backup.commit()
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        if prune:
            self._prune_backups()

        dbg("Created listening history backup: %s", destination)
        return destination

    def _maybe_create_daily_backup(self) -> None:
        day_prefix = f"library_state-{date.today():%Y%m%d}-"
        try:
            already_created = any(
                path.name.startswith(day_prefix)
                for path in self.backup_dir().glob("library_state-*.sqlite3")
            )
            if not already_created:
                self.create_backup(label="daily")
        except Exception:
            # The working database remains usable even if a backup disk or
            # directory is temporarily unavailable.
            dbg_exc(
                f"Automatic listening history backup failed in "
                f"{self.backup_dir()}"
            )

    def _prune_backups(self) -> None:
        backups = sorted(
            self.backup_dir().glob("library_state-*.sqlite3"),
            key=lambda path: path.name,
            reverse=True,
        )

        for old_backup in backups[self._backup_limit :]:
            try:
                old_backup.unlink()
            except OSError:
                dbg_exc(f"Could not remove old backup {old_backup}")
