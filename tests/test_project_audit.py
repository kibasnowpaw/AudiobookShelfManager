"""Regressions from the 2026-11-20 whole-project review."""
from pathlib import Path
from types import SimpleNamespace
import os
import sqlite3

import pytest
from mutagen.id3 import ID3, TXXX

from booklibconnect import album_expected_store, covers, portable_track_state as portable
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.models import TrackMeta
from booklibconnect.naming import build_track_filename, rename_track_file
from booklibconnect.scan_index_store import CachedTrack, ScanIndexStore
from booklibconnect.statistics_data import build_statistics
from booklibconnect.ui_mainwindow import MainWindow


def test_rename_keeps_zero_and_rejects_actual_control_characters(tmp_path):
    old = tmp_path/'original.m4b'
    old.write_bytes(b'keep audio')
    book = TrackMeta(title='Dungeon Diving 1\x0001 /Part:20', artist='Writer 007', trackno=10)
    expected = '10 - Dungeon Diving 101 Part20 - Writer 007.m4b'
    assert build_track_filename(book, '.m4b') == expected
    final = rename_track_file(old, book)
    assert final.name == expected and final.read_bytes() == b'keep audio'


def test_rename_without_sidecars_does_not_adopt_another_cover(tmp_path):
    old = tmp_path/'Before.mp3'
    old.write_bytes(b'audio')
    meta = TrackMeta(title='After')
    target = old.with_name(build_track_filename(meta, '.mp3'))
    cover = target.with_suffix('.JPG')
    cover.write_bytes(b'saved cover')
    assert rename_track_file(old, meta, rename_sidecar=False).stem == target.stem+' (1)'
    assert cover.read_bytes() == b'saved cover'


def test_cover_directory_reuses_names_and_detects_changes(tmp_path, monkeypatch):
    album = tmp_path/'Series'
    album.mkdir()
    for name in ('Front.png','cover.png','Cover.JPG','book01.PnG','Book01.jpg','bonus.png'):
        (album/name).touch()
    listings = []
    original = covers.os.scandir
    def counted(path):
        listings.append(str(path))
        return original(path)
    monkeypatch.setattr(covers.os, 'scandir', counted)
    lookup = covers.CoverDirectory(album)
    for _ in range(20):
        lookup.refresh()
        assert lookup.for_stem('Book01').name == 'Book01.jpg'
        assert lookup.preferred(covers.ALBUM_COVER_STEMS).name == 'cover.png'
    assert listings == [str(album)]
    (album/'Book01.jpg').unlink()
    (album/'cover.jpg').touch()
    lookup.refresh()
    assert lookup.for_stem('Book01').name == 'book01.PnG'
    assert lookup.preferred(covers.ALBUM_COVER_STEMS).name == 'cover.jpg'
    assert len(listings) == 2


def test_scan_index_rewrites_only_when_necessary(tmp_path):
    p = tmp_path/'audio.m4b'
    p.write_bytes(b'metadata fixture')
    entry = CachedTrack.from_file(p, TrackMeta(title='Before'), 3, watched=False,
                                  goodreads=False, audible=False, portable_id_ready=True)
    index = ScanIndexStore(tmp_path/'state')
    records = {str(p): entry}
    index.save([tmp_path], records)
    disk = index.file_path()
    original = disk.stat()
    index = ScanIndexStore(tmp_path/'state')  # a new process can skip the rewrite, too
    index.save([tmp_path], records)
    assert (disk.stat().st_ino, disk.stat().st_mtime_ns) == (original.st_ino,original.st_mtime_ns)
    entry.metadata.title = 'Changed'
    index.save([tmp_path], records)
    assert ScanIndexStore(tmp_path/'state').entries()[str(p)].metadata.title == 'Changed'
    disk.write_text('unrelated external edit')
    index.save([tmp_path], records)
    assert ScanIndexStore(tmp_path/'state').entries()[str(p)].metadata.title == 'Changed'
    disk.unlink()
    index.save([], {})
    assert disk.is_file() and not ScanIndexStore(tmp_path/'state').entries()


def test_discovered_series_totals_commit_together_and_keep_manual_edits(tmp_path, monkeypatch):
    store = album_expected_store.AlbumExpectedStore(tmp_path)
    store.set(Path('A'), 17)
    written = []
    original = album_expected_store._write_json_atomic
    def capture(path, data):
        written.append(dict(data))
        original(path, data)
    monkeypatch.setattr(album_expected_store, '_write_json_atomic', capture)
    store.fill_missing({'A': 2, 'B': 3, 'C': 4})
    store.fill_missing({'A': 2, 'B': 3, 'C': 4})
    assert written == [{'A': 17, 'B': 3, 'C': 4}]
    def failed(*args):
        raise OSError('simulated disk is full')
    monkeypatch.setattr(album_expected_store, '_write_json_atomic', failed)
    with pytest.raises(OSError):
        store.fill_missing({'D': 9})
    assert album_expected_store.AlbumExpectedStore(tmp_path).data == store.data == written[0]


@pytest.mark.parametrize('mode, expected', [
    ('az', 'ABC'), ('most_owned','BAC'), ('missing_most','CBA'),
    ('completed_first','ABC'), ('incomplete_first','BCA'),
    ('most_progress','ABC'), ('least_progress','CBA'),
])
def test_sort_books_by_progress_without_accessing_disk(mode, expected):
    albums = list(map(Path, 'CAB'))
    ids = {'A':['a'], 'B':['b1','b2'], 'C':[]}
    total = {'A':1, 'B':3, 'C':4}
    options = SimpleNamespace(_series_sort_mode=lambda:mode, _album_mp3s=lambda p:ids[p.name],
                              _album_denominator=lambda p,_:total[p.name],
                              _is_book_completed=lambda p:p in ('a','b1'))
    # No date, review store, or filesystem access exists on this view.
    assert ''.join(p.name for p in MainWindow._sorted_album_dirs(options,albums)) == expected


def test_book_replaced_at_same_path_and_mtime_does_not_reuse_old_identity(tmp_path):
    path, replacement = tmp_path/'book.mp3', tmp_path/'replacement.mp3'
    for f, ident in ((path,'a'*32),(replacement,'b'*32)):
        tag = ID3()
        tag.add(TXXX(encoding=3, desc=portable.APP_TRACK_ID, text=[ident]))
        tag.save(f)
    st = path.stat()
    assert replacement.stat().st_size == st.st_size
    assert portable.ensure_track_id(path) == 'a'*32
    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_mtime_ns == st.st_mtime_ns
    assert portable.ensure_track_id(path) == 'b'*32


def test_gallery_snapshot_and_sqlite_rollback(tmp_path):
    store = ListeningHistoryStore(tmp_path, automatic_daily_backup=False)
    store.upsert_book(track_id='one',last_path='/one.m4b',title='One')
    store.add_completed_listen('one',started_on='2025-01-10',finished_on='2025-01-14')
    store.add_completed_listen('one',started_on='2025-02-01',finished_on='2025-02-01')
    store.set_rating('one',3.61)
    store.set_reviewed('one',True)
    assert store.gallery_snapshot() == ({'/one.m4b':('2025-02-01',1)}, {'/one.m4b':2},
                                        {'/one.m4b':3.61}, {'/one.m4b'})
    with pytest.raises(ValueError):
        with store._transaction() as conn:
            conn.execute("UPDATE books SET rating=1")
            raise ValueError('do not commit')
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    assert store.book('one').rating == 3.61


def test_history_at_reused_filename_does_not_complete_the_replacement():
    path='/books/101.m4b'
    for books in ([{'track_id':'new','rating':3.61}, {'track_id':'old','rating':1}],
                  [{'track_id':'old','rating':1}, {'track_id':'new','rating':3.61}]):
        snapshot={'books':[dict(b,last_path=path) for b in books],
                  'events':[dict(id=1, track_id='old',started_on='2024-01-01',finished_on='2024-01-02')]}
        collection={'paths':(path,), 'tracks':({'path':path,'track_id':'new','title':'New book'},)}
        stats=build_statistics(snapshot,None,collection,files={path:'Available'})
        assert stats.collection_books[0].rating == 3.61
        assert stats.collection.completed == 0
        assert len(stats.listens) == 1 and not stats.listens[0].can_open


def test_interrupted_schema_upgrade_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    from contextlib import closing
    db = tmp_path/'library_state.sqlite3'
    with closing(sqlite3.connect(db)) as conn, conn:
        ListeningHistoryStore._create_schema_v1(None, conn)
        conn.execute("INSERT INTO books(track_id,last_path,last_seen_at) VALUES('old','/old.mp3','then')")
        conn.execute('PRAGMA user_version=1')
    real = ListeningHistoryStore._migrate_schema_v1_to_v2
    def fail_after_alter(self, conn):
        real(self, conn)
        raise OSError('simulated interrupted upgrade')
    with monkeypatch.context() as m:
        m.setattr(ListeningHistoryStore, '_migrate_schema_v1_to_v2', fail_after_alter)
        with pytest.raises(OSError):
            ListeningHistoryStore(tmp_path, automatic_daily_backup=False)
    backup, = (tmp_path/'backups').glob('*before-v1-to-v5.sqlite3')
    for path in (db, backup):
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
            assert 'rating' not in {row[1] for row in conn.execute('PRAGMA table_info(books)')}
            assert conn.execute('SELECT last_path FROM books').fetchone()[0] == '/old.mp3'
    assert ListeningHistoryStore(tmp_path, automatic_daily_backup=False).book('old').rating == 0
