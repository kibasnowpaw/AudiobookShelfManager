"""Read-only statistics over saved history and a frozen library-scan snapshot."""
from __future__ import annotations

import calendar
import math
import re
import stat
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean

from .genre_tags import genre_tag_issue, split_genre_tags
from .gallery_options import effective_options
from .purchase_info import release_year
from .rating_values import rating_value, rating_band, mean_rating


def _seconds(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _positive(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _rating(value):
    return rating_value(value)


def normalized(value):
    return unicodedata.normalize('NFKC', str(value or '').strip()).casefold()


def split_tags(value):
    """Match the editor's delimiters and case-insensitive duplicate handling."""
    return tuple(split_genre_tags(value))


def split_people(value):
    text = re.sub(r'\s+(?:and|&)\s+', ';', str(value or ''), flags=re.I)
    return split_tags(text)


def probe_files(paths):
    """Check each path once on the Statistics worker, without reading MP3s.

    A disconnected drive or lost Flatpak access is also unavailable; no file
    or saved listening record is ever removed by this check.
    """
    result = {}
    for path in set(paths):
        if not path:
            result[path] = 'File not found'
            continue
        try:
            mode = Path(path).stat().st_mode
            result[path] = 'Available' if stat.S_ISREG(mode) else 'File not found'
        except (FileNotFoundError, NotADirectoryError):
            result[path] = 'File not found'
        except OSError:
            result[path] = 'File unavailable'
    return result


@dataclass(frozen=True)
class ListenRow:
    event_id: int
    track_id: str
    path: str
    title: str
    author: str
    narrator: str
    started_on: str
    finished_on: str
    seconds: float | None
    days: int
    relisten: bool
    rating: float
    favorite: bool
    note: str
    series: str = ''
    genres: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    narrators: tuple[str, ...] = ()
    artwork_key: str = ''
    file_status: str = 'Not checked'

    @property
    def can_open(self):
        return bool(self.path) and self.file_status in ('Available', 'Not checked')


@dataclass(frozen=True)
class PeriodTotal:
    label: str
    listens: int
    relistens: int
    seconds: float
    known_durations: int


@dataclass(frozen=True)
class PersonTotal:
    name: str
    listens: int
    books: int
    seconds: float


@dataclass(frozen=True)
class BreakdownTotal:
    name: str
    books: int


@dataclass(frozen=True)
class CollectionBook:
    track_id: str
    path: str
    title: str
    author: str
    narrator: str
    series: str
    series_path: str
    genres: tuple[str, ...]
    seconds: float | None
    rating: float
    favorite: bool
    completed: bool
    issues: tuple[str, ...]
    artwork_key: str = ''
    release_year: int | None = None
    purchased_on: str = ''
    purchase_source: str = ''


@dataclass(frozen=True)
class SeriesTotal:
    path: str
    name: str
    author: str
    owned: int
    completed: int
    expected: int | None
    missing: int | None
    missing_numbers: tuple[int, ...] | None
    average_rating: float | None
    rated: int
    note: str

    @property
    def ownership_complete(self):
        return self.expected is not None and self.missing == 0

    @property
    def listening_complete(self):
        return self.ownership_complete and self.completed == self.owned


@dataclass(frozen=True)
class CollectionTotal:
    available: bool = False
    books: int = 0
    series: int = 0
    authors: int = 0
    completed: int = 0
    rated: int = 0
    favorites: int = 0
    missing: int = 0
    unknown_totals: int = 0
    uncertain_gaps: int = 0
    ownership_complete: int = 0
    listening_complete: int = 0
    attention: int = 0


@dataclass(frozen=True)
class StatisticsResult:
    year: int | None
    years: tuple[int, ...]
    listens: tuple[ListenRow, ...]
    unique_books: int
    relistens: int
    audio_seconds: float
    known_durations: int
    average_days: float | None
    average_rating: float | None
    rating_counts: tuple[int, ...]
    favorite_books: int
    periods: tuple[PeriodTotal, ...]
    authors: tuple[PersonTotal, ...]
    narrators: tuple[PersonTotal, ...]
    longest: ListenRow | None
    shortest: ListenRow | None
    collection: CollectionTotal
    excluded_entries: int
    genres: tuple[BreakdownTotal, ...] = ()
    collection_books: tuple[CollectionBook, ...] = ()
    series: tuple[SeriesTotal, ...] = ()
    issues: tuple[BreakdownTotal, ...] = ()
    release_years: tuple[BreakdownTotal, ...] = ()


def scan_snapshot(records, expected=None, author_count=0, *, profile=None):
    """Copy cached metadata only. This function never stats or opens a file."""
    tracks = []
    rules = {}
    for path, record in records.items():
        meta = record.metadata
        location = Path(path)
        folder = location.parent
        if folder not in rules:
            rules[folder] = effective_options(profile, folder)['tag_rule']
        narrator = str(meta.narrator or meta.composer or '').strip()
        issues = []
        issue = genre_tag_issue(meta.genre, rules[folder])
        if issue:
            issues.append(issue['kind'])
        if not narrator:
            issues.append('Missing narrator')
        if unicodedata.normalize('NFC', meta.album.strip()) != unicodedata.normalize('NFC', folder.name.strip()):
            issues.append('Album / folder mismatch')
        if record.cover_issue:
            issues.append('Cover problem')
        elif not record.cover_checked:
            issues.append('Cover not checked')
        authors = {}
        for raw in (meta.artist, meta.albumartist):
            for person in split_people(raw):
                authors.setdefault(normalized(person), person)
        tracks.append(dict(
            path=str(path), track_id=record.track_id,
            title=meta.title.strip() or location.stem,
            author=meta.artist.strip() or meta.albumartist.strip(),
            authors=tuple(authors.values()), narrator=narrator,
            series=meta.album.strip() or folder.name,
            series_path=str(folder), genre=meta.genre, release_year=release_year(meta.year),
            trackno=_positive(meta.trackno), totaltracks=_positive(meta.totaltracks),
            seconds=record.length_seconds, issues=tuple(issues),
            artwork_key=str(record.cover_signature or f'{record.mtime_ns}:{record.size}'),
        ))
    return dict(paths=tuple(records), tracks=tuple(tracks),
                series=len({track['series_path'] for track in tracks}),
                authors=author_count, expected=dict(expected or {}))


def unique_listens(rows):
    """Return the latest listen for each identity, preserving input order."""
    latest = {}
    for row in rows:
        previous = latest.get(row.track_id)
        if previous is None or (row.finished_on, row.event_id) > (previous.finished_on, previous.event_id):
            latest[row.track_id] = row
    return tuple(sorted(latest.values(), key=lambda row: (row.finished_on, row.event_id), reverse=True))


def filter_listens(result, *, genre=None, author=None, narrator=None, rating=None,
                   period=None, distinct=False, availability=None):
    """All supplied filters intersect; missing metadata has a distinct empty key."""
    rows = []
    for row in result.listens:
        if availability == 'available' and not row.can_open:
            continue
        if availability == 'unavailable' and row.can_open:
            continue
        if genre is not None and not (not row.genres if genre == '' else normalized(genre) in {normalized(x) for x in row.genres}):
            continue
        if author is not None and not (not row.authors if author == '' else normalized(author) in {normalized(x) for x in row.authors}):
            continue
        if narrator is not None and not (not row.narrators if narrator == '' else normalized(narrator) in {normalized(x) for x in row.narrators}):
            continue
        if rating is not None:
            if isinstance(rating, str) and rating.startswith('band:'):
                if rating_band(row.rating) != int(rating.split(':')[1]):
                    continue
            elif row.rating != rating:
                continue
        if period is not None:
            key = int(row.finished_on[5:7]) if result.year is not None else int(row.finished_on[:4])
            if key != period:
                continue
        rows.append(row)
    return unique_listens(rows) if distinct else tuple(rows)


def _collection_details(collection, books, all_rows):
    if collection is None:
        return CollectionTotal(), (), (), ()
    paths = set(collection.get('paths', ()))
    by_path = {str(book.get('last_path') or ''): book for book in books.values()}
    finished_ids = {row.track_id for row in all_rows}
    tracks = {str(track['path']): track for track in collection.get('tracks', ())}
    current_books = []
    groups = defaultdict(list)
    for path in sorted(paths):
        saved = by_path.get(path, {})
        track = tracks.get(path, {})
        if track.get('track_id'):
            identified = books.get(track['track_id'], {})
            saved = identified if identified.get('last_path') == path else {}
        group_path = str(track.get('series_path') or Path(path).parent)
        entry = CollectionBook(
            track_id=str(saved.get('track_id') or track.get('track_id') or 'path:' + path), path=path,
            title=str(track.get('title') or saved.get('title') or Path(path).stem),
            author=str(track.get('author', saved.get('author')) or 'Unknown author'),
            narrator=str(track.get('narrator', saved.get('narrator')) or 'Unknown narrator'),
            series=str(track.get('series') or saved.get('series') or Path(group_path).name),
            series_path=group_path, genres=split_tags(track.get('genre')),
            seconds=_seconds(track.get('seconds')), rating=_rating(saved.get('rating')),
            favorite=bool(saved.get('favorite')),
            completed=saved.get('track_id') in finished_ids,
            issues=tuple(track.get('issues', ())), artwork_key=str(track.get('artwork_key') or ''),
            release_year=release_year(track.get('release_year')),
            purchased_on=str(saved.get('purchased_on') or ''),
            purchase_source=str(saved.get('purchase_source') or ''),
        )
        current_books.append(entry)
        if tracks:
            groups[group_path].append((entry, track))
    series_rows = []
    for path, entries in groups.items():
        manual = _positive(collection.get('expected', {}).get(path))
        tagged = {_positive(track.get('totaltracks')) for book, track in entries} - {0}
        expected = manual or (next(iter(tagged)) if len(tagged) == 1 else None)
        count = len(entries)
        note = 'Recorded series total' if expected else 'Expected total unknown'
        if manual:
            note = 'Saved expected total'
        elif len(tagged) > 1:
            note = 'Conflicting expected totals in book metadata'
        missing, missing_numbers = None, None
        if expected:
            numbers = [_positive(track.get('trackno')) for book, track in entries]
            positive = [number for number in numbers if number]
            if len(positive) != len(set(positive)) or any(number > expected for number in positive) or count > expected:
                note = 'Check book numbering / expected total'
            else:
                missing = expected - count
                if all(numbers) and expected <= 10000:
                    missing_numbers = tuple(sorted(set(range(1, expected + 1)) - set(numbers)))
                elif missing:
                    note += '; exact missing numbers unknown'
        ratings = [book.rating for book, track in entries if book.rating]
        names = sorted({book.author for book, track in entries}, key=str.casefold)
        series_rows.append(SeriesTotal(
            path=path, name=Path(path).name, author='; '.join(names),
            owned=count, completed=sum(book.completed for book, track in entries),
            expected=expected, missing=missing, missing_numbers=missing_numbers,
            average_rating=mean_rating(ratings), rated=len(ratings), note=note))
    series_rows.sort(key=lambda series: series.name.casefold())
    issue_counts = Counter(issue for book in current_books for issue in set(book.issues))
    totals = CollectionTotal(
        available=True, books=len(paths), series=int(collection.get('series', len(series_rows))),
        authors=int(collection.get('authors', 0)), completed=sum(book.completed for book in current_books),
        rated=sum(bool(book.rating) for book in current_books), favorites=sum(book.favorite for book in current_books),
        missing=sum(series.missing or 0 for series in series_rows),
        unknown_totals=sum(series.expected is None for series in series_rows),
        uncertain_gaps=sum(series.missing is None for series in series_rows),
        ownership_complete=sum(series.ownership_complete for series in series_rows),
        listening_complete=sum(series.listening_complete for series in series_rows),
        attention=sum(bool(book.issues) for book in current_books))
    issues = tuple(BreakdownTotal(name, count) for name, count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0])))
    return totals, tuple(current_books), tuple(series_rows), issues


def build_statistics(snapshot: dict, year: int | None, collection: dict | None = None,
                     *, files: dict | None = None) -> StatisticsResult:
    """Finish dates and duration snapshots are authoritative for listening totals."""
    if year is not None:
        year = int(year)
        if not 1 <= year <= 9999:
            raise ValueError('Choose a valid year.')
    books = {str(book['track_id']): book for book in snapshot.get('books', [])}
    metadata = {str(track['path']): track for track in (collection or {}).get('tracks', ())}
    # File availability changes which current books can be opened, never the
    # historical totals. Keep build_statistics itself free of file operations.
    files = files or {}
    if collection is not None:
        paths = tuple(path for path in collection.get('paths', ())
                      if files.get(path, 'Available') == 'Available')
        all_paths = collection.get('paths', ())
        if len(paths) != len(all_paths):
            available = set(paths)
            tracks = tuple(track for track in collection.get('tracks', ()) if track['path'] in available)
            authors = {normalized(name) for track in tracks
                       for name in track.get('authors') or split_people(track.get('author'))}
            collection = dict(collection, paths=paths, tracks=tracks,
                              series=len({str(Path(path).parent) for path in paths}),
                              authors=len(authors))
    dated = []
    excluded = 0
    for event in snapshot.get('events', []):
        try:
            started_text = str(event.get('started_on') or '').strip()
            finished_text = str(event.get('finished_on') or '').strip()
            started = date.fromisoformat(started_text)
            finished = date.fromisoformat(finished_text)
            if started > finished or started.isoformat() != started_text or finished.isoformat() != finished_text:
                raise ValueError('Invalid listening dates')
            track_id = str(event['track_id'])
            if track_id not in books:
                raise ValueError('Missing book identity')
            event_id = int(event['id'])
        except (ValueError, TypeError, KeyError):
            excluded += 1
            continue
        dated.append((finished, event_id, started, track_id, event))
    dated.sort(key=lambda entry: (entry[0], entry[1]))
    seen = set()
    all_rows = []
    for finished, event_id, started, track_id, event in dated:
        book = books[track_id]
        path = str(book.get('last_path') or '')
        track = metadata.get(path, {})
        file_status = files.get(path, 'Not checked')
        if track.get('track_id') and track['track_id'] != track_id:
            # Do not open a different identity just because it occupies an old
            # filename. The normal replacement workflow owns any history merge.
            file_status = 'Different book at saved path'
            track = {}
        author = str(track.get('author', book.get('author')) or '').strip()
        narrator = str(track.get('narrator', book.get('narrator')) or '').strip()
        all_rows.append(ListenRow(
            event_id=event_id, track_id=track_id, path=path,
            title=str(track.get('title') or book.get('title') or 'Untitled audiobook'),
            author=author or 'Unknown author', narrator=narrator or 'Unknown narrator',
            started_on=started.isoformat(), finished_on=finished.isoformat(),
            seconds=_seconds(event.get('duration_seconds')), days=(finished-started).days+1,
            relisten=track_id in seen, rating=_rating(book.get('rating')), favorite=bool(book.get('favorite')),
            note=str(event.get('note') or ''), series=str(track.get('series') or book.get('series') or ''),
            genres=split_tags(track.get('genre', book.get('genre'))),
            authors=tuple(track.get('authors') or split_people(author)), narrators=split_people(narrator),
            artwork_key=str(track.get('artwork_key') or ''), file_status=file_status))
        seen.add(track_id)
    years = tuple(sorted({int(row.finished_on[:4]) for row in all_rows}, reverse=True))
    selected = [row for row in all_rows if year is None or int(row.finished_on[:4]) == year]
    unique = unique_listens(selected)
    ratings = Counter(rating_band(row.rating) for row in unique if row.rating)
    known = [row for row in selected if row.seconds is not None]
    totals = defaultdict(lambda: [0, 0, 0.0, 0])
    for row in selected:
        key = int(row.finished_on[5:7]) if year is not None else int(row.finished_on[:4])
        entry = totals[key]
        entry[0] += 1
        entry[1] += int(row.relisten)
        if row.seconds is not None:
            entry[2] += row.seconds
            entry[3] += 1
    keys = range(1, 13) if year is not None else sorted(totals)
    periods = tuple(PeriodTotal(calendar.month_abbr[key] if year is not None else str(key), *totals[key]) for key in keys)

    def people(field, unknown):
        groups = defaultdict(list)
        names = {}
        for row in selected:
            for name in getattr(row, field) or (unknown,):
                key = normalized(name)
                names.setdefault(key, name)
                groups[key].append(row)
        result = [PersonTotal(names[key], len(rows), len({row.track_id for row in rows}), sum(row.seconds or 0 for row in rows)) for key, rows in groups.items()]
        return tuple(sorted(result, key=lambda person: (-person.listens, -person.books, person.name.casefold())))

    genres = Counter()
    genre_names = {}
    for row in unique:
        for tag in row.genres or ('',):
            key = normalized(tag)
            genre_names.setdefault(key, tag)
            genres[key] += 1
    genre_totals = tuple(BreakdownTotal(genre_names[key], count) for key, count in sorted(genres.items(), key=lambda item: (-item[1], item[0])))
    current, current_books, series, issues = _collection_details(collection, books, all_rows)
    return StatisticsResult(
        year=year, years=years, listens=tuple(reversed(selected)), unique_books=len(unique),
        relistens=sum(row.relisten for row in selected), audio_seconds=sum(row.seconds for row in known),
        known_durations=len(known), average_days=mean(row.days for row in selected) if selected else None,
        average_rating=mean_rating(row.rating for row in unique if row.rating),
        rating_counts=tuple(ratings.get(star, 0) for star in range(1,6)),
        favorite_books=sum(row.favorite for row in unique), periods=periods,
        authors=people('authors', 'Unknown author'), narrators=people('narrators', 'Unknown narrator'),
        longest=max(known, key=lambda row:(row.seconds,row.finished_on,row.event_id)) if known else None,
        shortest=min(known, key=lambda row:(row.seconds,-row.event_id)) if known else None,
        collection=current, excluded_entries=excluded, genres=genre_totals,
        collection_books=current_books, series=series, issues=issues,
        release_years=tuple(BreakdownTotal(str(key) if key else 'Unknown', count)
                           for key, count in sorted(Counter(book.release_year for book in current_books).items(),
                                                    key=lambda pair: pair[0] if pair[0] else 10000)))


def filter_collection(result, *, mode='all', series=None, issue=None, query='', genres=(),
                      genre_match='all', year=None, source=None):
    """Intersect current-library filters; no date or genre is guessed for a book."""
    wanted = {normalized(tag) for tag in genres}
    words = normalized(query).split()
    output = []
    for book in result.collection_books:
        if series is not None and book.series_path != series:
            continue
        if issue is not None and issue not in book.issues:
            continue
        if year is not None and (book.release_year or 0) != year:
            continue
        if source is not None and normalized(book.purchase_source) != normalized(source):
            continue
        tags = {normalized(tag) for tag in book.genres} or {''}
        if wanted and not (wanted <= tags if genre_match == 'all' else bool(wanted & tags)):
            continue
        searchable = normalized(' '.join((book.title, book.author, book.series, book.narrator, book.purchase_source)))
        if not all(word in searchable for word in words):
            continue
        matches = {
            'all': True, 'unfinished': not book.completed, 'finished': book.completed,
            'unrated': not book.rating, 'favorites': book.favorite, 'issues': bool(book.issues),
            'missing_purchase_date': not book.purchased_on,
            'missing_purchase_source': not book.purchase_source,
        }
        if matches.get(mode, False):
            output.append(book)
    return tuple(output)
