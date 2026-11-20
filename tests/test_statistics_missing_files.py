"""Deletion, rename and identity safety using real MP3s and the Qt event loop."""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtTest import QTest

from booklibconnect import statistics_data, ui_mainwindow as ui
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.scan_index_store import ScanIndexStore
from test_responsive_io import app, library, window, open_book, wait_until, scan


def save_listen(w, path, app):
    open_book(w, path, app)
    store = w.listening_history_store
    book = store.book_for_path(path)
    store.add_completed_listen(book.track_id, started_on=date.today(), finished_on=date.today())
    store.set_rating(book.track_id, 4)
    store.set_favorite(book.track_id, True)
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    return book.track_id


def test_refresh_hides_missing_files_without_erasing_history(window, library, app, monkeypatch):
    w, messages = window
    _, paths, _ = library
    track_id = save_listen(w, paths[0], app)
    page = w.statistics_page
    before = w.listening_history_store.statistics_snapshot()
    seconds = page.result.audio_seconds
    paths[0].unlink()
    original = statistics_data.probe_files

    def background_only(paths):
        assert QThread.currentThread() != app.thread()
        return original(paths)

    monkeypatch.setattr(statistics_data, 'probe_files', background_only)
    page.refresh_button.click()
    wait_until(app, lambda: w._statistics_job is None)
    assert w.listening_history_store.statistics_snapshot() == before
    assert page.result.unique_books == 1 and page.result.audio_seconds == seconds
    assert page.result.average_rating == 4 and page.result.favorite_books == 1
    assert page.result.collection.books == 2
    assert not page.history_model.rows
    assert all(row.path != str(paths[0]) for row in page.result.collection_books)
    page.tabs.setCurrentIndex(1)
    files = page.history_filters['availability']
    files.setCurrentIndex(files.findData('unavailable'))
    assert len(page.history_model.rows) == 1
    assert page.history_model.rows[0].track_id == track_id
    assert page.history_model.rows[0].file_status == 'File not found'
    assert page.history_sort.index(0, 2).data().startswith('[File not found]')
    selected = page.history_sort.index(0, 2)
    page.history_table.setCurrentIndex(selected)
    assert not page.history_open.isEnabled()
    opened = []
    page.open_book_requested.connect(opened.append)
    page._open_history_book(selected)
    QTest.keyClick(page.history_table, Qt.Key_Return)
    page.history_open.click()
    assert not opened and not messages
    # Historical highlights still describe what was listened to, but cannot
    # launch the removed file or request artwork for it.
    highlight = page.recap_highlight_model.index(0)
    page._open_recap_book(page.recap_highlight_model, highlight)
    assert page.recap_highlight_model.data(highlight, Qt.DecorationRole).cacheKey() == page.artwork.placeholder.cacheKey()
    page.recap_selector.setCurrentIndex(page.recap_selector.findData('all'))
    assert not page.recap_model.rows and '1 unavailable' in page.recap_count.text()
    assert not opened
    # Chart counts include the preserved history, so a drilldown must include
    # its clearly marked unavailable row as well.
    page._choose_rating(3)
    assert files.currentData() is None and len(page.history_model.rows) == 1
    page._clear_history_filters()
    assert files.currentData() == 'available' and not page.history_model.rows


def test_file_deleted_after_refresh_is_retired_on_first_open(window, library, app):
    w, messages = window
    _, paths, _ = library
    save_listen(w, paths[0], app)
    page = w.statistics_page
    assert page.history_model.rows[0].can_open
    paths[0].unlink()
    page._open_history_book(page.history_sort.index(0, 2))
    wait_until(app, lambda: w._file_job is None and w._statistics_job is None)
    assert w.stack.currentIndex() == w.PAGE_STATISTICS
    assert not page.history_model.rows and page.result.collection.books == 2
    assert len(page.result.listens) == 1 and not page.result.listens[0].can_open
    assert not messages  # No repeated missing-file warning dialog.


def test_rescan_follows_same_id_after_external_rename(window, library, app, monkeypatch):
    w, messages = window
    _, paths, _ = library
    track_id = save_listen(w, paths[0], app)
    store = w.listening_history_store
    events = store.statistics_snapshot()['events']
    new_path = paths[0].with_name('Renamed outside the app.mp3')
    original_bytes = paths[0].read_bytes()
    paths[0].rename(new_path)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None and w._statistics_job is None)
    assert new_path.read_bytes() == original_bytes
    assert store.book(track_id).last_path == str(new_path)
    assert store.book(track_id).rating == 4 and store.book(track_id).favorite
    assert store.statistics_snapshot()['events'] == events
    assert store.book_for_path(paths[0]) is None
    row = w.statistics_page.history_model.rows[0]
    assert row.path == str(new_path) and row.can_open
    assert w.statistics_page.result.collection.completed == 1
    assert w._track_records[str(new_path)].track_id == track_id
    assert ScanIndexStore().entries()[str(new_path)].track_id == track_id
    assert str(paths[0]) not in w._track_records
    assert w._book_rating_by_path[str(new_path)] == 4
    # Cached IDs avoid an added MP3 read on subsequent unchanged rescans.
    def forbidden(*args):
        pytest.fail('An unchanged MP3 was reopened for metadata or identity')
    monkeypatch.setattr(ui, 'ensure_track_id', forbidden)
    monkeypatch.setattr(ui, 'read_meta_and_length', forbidden)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None and w._statistics_job is None)
    assert store.statistics_snapshot()['events'] == events
    assert store.book(track_id).last_path == str(new_path)
    assert not messages


@pytest.mark.parametrize('case', ['different_id', 'duplicate_id', 'occupied_destination'])
def test_path_reconciliation_never_guesses_or_merges(tmp_path, case):
    store = ListeningHistoryStore(data_directory=tmp_path/'history')
    old = str(tmp_path/'deleted.mp3')
    target = str(tmp_path/'current.mp3')
    store.upsert_book(track_id='saved', last_path=old, title='Same title', duration_seconds=7200)
    store.add_completed_listen('saved', started_on='2026-01-01', finished_on='2026-01-01')
    store.set_rating('saved', 5)
    store.set_favorite('saved', True)
    records = {target: SimpleNamespace(track_id='saved')}
    if case == 'different_id':
        records[target].track_id = 'other'
    elif case == 'duplicate_id':
        records[str(tmp_path/'second-copy.mp3')] = SimpleNamespace(track_id='saved')
    else:
        store.upsert_book(track_id='other', last_path=target, title='Same title')
    before = store.statistics_snapshot()
    assert store.reconcile_scanned_paths(records) == 0
    assert store.statistics_snapshot() == before


def test_old_cache_reads_ids_once_without_rewriting_audio(library, monkeypatch):
    root, paths, _ = library
    index, _ = scan(root)
    before = {path: path.read_bytes() for path in paths}
    data = json.loads(index.file_path().read_text())
    data['version'] = 4
    for entry in data['tracks'].values():
        entry.pop('track_id')
    index.file_path().write_text(json.dumps(data))
    old = ScanIndexStore()
    calls = []
    original = ui.ensure_track_id
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(ui, 'ensure_track_id', counted)
    _, first = scan(root, old)
    assert first['reused'] == 3 and len(calls) == 3
    assert all(record.track_id for record in first['track_records'].values())
    assert before == {path: path.read_bytes() for path in paths}
    calls.clear()
    _, second = scan(root, ScanIndexStore())
    assert second['reused'] == 3 and not calls


def test_inaccessible_path_is_not_mistaken_for_deleted_history(tmp_path, monkeypatch):
    path = str(tmp_path/'offline.mp3')
    def denied(*args, **kwargs):
        raise PermissionError('Access lost')
    monkeypatch.setattr(Path, 'stat', denied)
    files = statistics_data.probe_files([path, path])
    assert files == {path: 'File unavailable'}
    snapshot = dict(books=[dict(track_id='saved', last_path=path, rating=4)], events=[
        dict(id=1, track_id='saved', started_on='2026-01-01', finished_on='2026-01-01', duration_seconds=7200)])
    result = statistics_data.build_statistics(snapshot, 2026, files=files)
    assert result.unique_books == 1 and result.audio_seconds == 7200
    assert not result.listens[0].can_open
