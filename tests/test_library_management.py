"""Explicit removal, writer ratings, exact tags and full-page navigation."""
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import os
import sqlite3

import pytest
from PySide6.QtCore import QFile, QRect, Qt, QThread, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from booklibconnect import book_removal as removal, statistics_data, ui_mainwindow as ui
from booklibconnect.genre_tags import split_genre_tags, genre_tag_issue
from booklibconnect.listening_history_store import ListeningHistoryStore
from test_responsive_io import app, library, window, open_book, wait_until
from test_statistics_missing_files import save_listen


@pytest.fixture
def removal_case(library):
    _, paths, _ = library
    store = ListeningHistoryStore()
    rows = []
    for index, path in enumerate(paths[:2]):
        track_id = ui.ensure_track_id(path)
        store.upsert_book(track_id=track_id, last_path=str(path), title=f'Book {index}', duration_seconds=7200)
        store.add_completed_listen(track_id, started_on='2025-01-01', finished_on='2025-01-02')
        store.add_completed_listen(track_id, started_on='2026-01-01', finished_on='2026-01-02')
        store.set_rating(track_id, index + 4)
        store.set_favorite(track_id, True)
        rows.append(SimpleNamespace(track_id=track_id, path=str(path), title=f'Book {index}'))
    return store, rows


def test_remove_missing_record_preserves_other_books_and_backs_up_all_data(removal_case):
    store, rows = removal_case
    Path(rows[0].path).unlink()
    target = removal.prepare_removal(store, rows[0])
    other = store.removal_snapshot(rows[1].track_id)
    assert not target.can_delete_file
    result = removal.remove_book(store, target)
    assert store.removal_snapshot(rows[0].track_id) == {'book': None, 'events': []}
    assert store.removal_snapshot(rows[1].track_id) == other
    assert not result['file_deleted']
    with sqlite3.connect(result['backup']) as connection:
        assert connection.execute('SELECT rating, favorite FROM books WHERE track_id=?',
                                  (rows[0].track_id,)).fetchone() == (4, 1)
        assert connection.execute('SELECT COUNT(*) FROM listening_events WHERE track_id=?',
                                  (rows[0].track_id,)).fetchone()[0] == 2


def test_checkbox_never_remembers_opt_in_and_cancel_is_default(removal_case, app):
    store, rows = removal_case
    target = removal.prepare_removal(store, rows[0])
    before = store.statistics_snapshot()
    first = removal.RemoveBookDialog(target)
    assert first.delete_file.isEnabled() and not first.delete_file.isChecked()
    assert first.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel).isDefault()
    first.delete_file.setChecked(True)
    first.reject()
    second = removal.RemoveBookDialog(target)
    assert not second.delete_file.isChecked()
    second.reject()
    Path(rows[0].path).unlink()
    missing = removal.RemoveBookDialog(removal.prepare_removal(store, rows[0]))
    assert not missing.delete_file.isEnabled() and not missing.delete_file.isChecked()
    assert missing.remove_button.isEnabled()
    missing.reject()
    assert store.statistics_snapshot() == before


def test_default_removal_never_touches_mp3_or_artwork(removal_case, library):
    store, rows = removal_case
    root, paths, cover = library
    before = {path: path.read_bytes() for path in paths + [cover]}
    result = removal.remove_book(store, removal.prepare_removal(store, rows[0]),
                                 trash=lambda path: pytest.fail('File option was not selected'))
    assert not result['file_deleted']
    assert before == {path: path.read_bytes() for path in paths + [cover]}


def test_optional_trash_removes_only_selected_mp3(removal_case, library, tmp_path, monkeypatch):
    store, rows = removal_case
    _, paths, cover = library
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path/'trash-data'))
    (tmp_path/'trash-data').mkdir()
    monkeypatch.delenv('FLATPAK_ID', raising=False)
    sidecar = paths[0].with_suffix('.png')
    sidecar.write_bytes(cover.read_bytes())
    original = paths[0].read_bytes()
    neighbors = {path: path.read_bytes() for path in paths[1:] + [sidecar, cover]}
    result = removal.remove_book(store, removal.prepare_removal(store, rows[0]), delete_file=True)
    assert result['file_deleted'] and not paths[0].exists()
    assert store.book(rows[0].track_id) is None
    assert neighbors == {path: path.read_bytes() for path in neighbors}
    trashed = list((tmp_path/'trash-data'/'Trash'/'files').glob('*.mp3'))
    assert len(trashed) == 1 and trashed[0].read_bytes() == original


def test_failed_trash_rolls_back_database(removal_case):
    store, rows = removal_case
    before = store.statistics_snapshot()
    path = Path(rows[0].path)
    original = path.read_bytes()
    with pytest.raises(OSError, match='could not be moved to Trash'):
        removal.remove_book(store, removal.prepare_removal(store, rows[0]), True,
                            trash=lambda path: (False, ''))
    assert store.statistics_snapshot() == before and path.read_bytes() == original


@pytest.mark.parametrize('change', ['rating', 'new_listen', 'file'])
def test_changed_confirmation_cannot_remove_new_data_or_replacement_file(removal_case, change):
    store, rows = removal_case
    target = removal.prepare_removal(store, rows[0])
    if change == 'rating':
        store.set_rating(target.track_id, 1)
    elif change == 'new_listen':
        store.add_completed_listen(target.track_id, started_on='2026-02-01', finished_on='2026-02-01')
    else:
        with open(target.path, 'ab') as file:
            file.write(b'changed')
    before = store.statistics_snapshot()
    original = Path(target.path).read_bytes()
    with pytest.raises(RuntimeError, match='changed'):
        removal.remove_book(store, target, True, trash=lambda path: pytest.fail('Unsafe file action'))
    assert store.statistics_snapshot() == before and Path(target.path).read_bytes() == original


def test_database_failure_after_move_restores_mp3(removal_case, tmp_path, monkeypatch):
    store, rows = removal_case
    target = removal.prepare_removal(store, rows[0])
    before = store.statistics_snapshot()
    original = Path(target.path).read_bytes()
    destination = tmp_path/'generated-trash.mp3'
    def move(path):
        assert QFile(path).rename(str(destination))
        return True, str(destination)
    def fail_after_action(track_id, state, action):
        with store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM listening_events WHERE track_id=?', (track_id,))
            connection.execute('DELETE FROM books WHERE track_id=?', (track_id,))
            action()
            raise sqlite3.OperationalError('Simulated commit failure')
    monkeypatch.setattr(store, 'remove_book', fail_after_action)
    with pytest.raises(sqlite3.OperationalError):
        removal.remove_book(store, target, True, trash=move)
    assert store.statistics_snapshot() == before
    assert Path(target.path).read_bytes() == original and not destination.exists()


@pytest.mark.parametrize('result', [0, 1])
def test_flatpak_portal_protocol_uses_verified_descriptor(removal_case, monkeypatch, result):
    from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusUnixFileDescriptor
    store, rows = removal_case
    target = removal.prepare_removal(store, rows[0])
    calls = []
    def call(message, mode, timeout):
        assert message.service() == 'org.freedesktop.portal.Desktop'
        assert message.interface() == 'org.freedesktop.portal.Trash'
        assert message.member() == 'TrashFile'
        assert len(message.arguments()) == 1
        descriptor = message.arguments()[0]
        assert isinstance(descriptor, QDBusUnixFileDescriptor)
        assert os.fstat(descriptor.fileDescriptor()).st_ino == target.signature[1]
        calls.append(message)
        return SimpleNamespace(type=lambda: QDBusMessage.ReplyMessage, arguments=lambda: [result])
    bus = SimpleNamespace(isConnected=lambda: True, call=call)
    monkeypatch.setattr(QDBusConnection, 'sessionBus', lambda: bus)
    assert removal._portal_trash(target.path, target.signature) == (result == 1, '')
    assert len(calls) == 1 and Path(target.path).exists()


def test_sorted_missing_history_removal_refreshes_statistics(window, library, app, monkeypatch):
    w, messages = window
    _, paths, _ = library
    first_id = save_listen(w, paths[0], app)
    second_id = save_listen(w, paths[1], app)
    paths[0].unlink()
    page = w.statistics_page
    page.refresh_button.click()
    wait_until(app, lambda: w._statistics_job is None)
    page.tabs.setCurrentIndex(1)
    files = page.history_filters['availability']
    files.setCurrentIndex(files.findData(None))
    page.history_sort.sort(2, Qt.DescendingOrder)
    selected = next(page.history_sort.index(index, 2) for index in range(page.history_sort.rowCount())
                    if page.history_model.rows[page.history_sort.mapToSource(page.history_sort.index(index, 2)).row()].track_id == first_id)
    page.history_table.setCurrentIndex(selected)
    assert page.history_remove.isEnabled() and not page.history_open.isEnabled()
    confirmations = []
    def accept(dialog):
        assert not dialog.delete_file.isChecked() and not dialog.delete_file.isEnabled()
        confirmations.append(dialog)
        return QDialog.Accepted
    original = removal.remove_book
    def worker_only(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        return original(*args, **kwargs)
    monkeypatch.setattr(removal.RemoveBookDialog, 'exec', accept)
    monkeypatch.setattr(removal, 'remove_book', worker_only)
    page.history_remove.click()
    wait_until(app, lambda: w._file_job is None and w._statistics_job is None)
    assert len(confirmations) == 1 and not messages
    assert w.listening_history_store.book(first_id) is None
    assert w.listening_history_store.book(second_id).rating == 4
    assert page.result.unique_books == 1 and page.result.favorite_books == 1
    assert all(row.track_id != first_id for row in page.history_model.rows)


def test_collection_remove_opt_in_rescans_and_updates_cards(window, library, app, monkeypatch, tmp_path):
    w, messages = window
    _, paths, _ = library
    track_id = save_listen(w, paths[0], app)
    page = w.statistics_page
    page.tabs.setCurrentIndex(2)
    page.collection_sort.sort(0, Qt.DescendingOrder)
    selected = next(page.collection_sort.index(index, 0) for index in range(page.collection_sort.rowCount())
                    if page.collection_model.rows[page.collection_sort.mapToSource(page.collection_sort.index(index, 0)).row()].path == str(paths[0]))
    page.collection_table.setCurrentIndex(selected)
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path/'trash-data'))
    (tmp_path/'trash-data').mkdir()
    def accept(dialog):
        assert not dialog.delete_file.isChecked() and dialog.delete_file.isEnabled()
        dialog.delete_file.setChecked(True)
        return QDialog.Accepted
    monkeypatch.setattr(removal.RemoveBookDialog, 'exec', accept)
    page.collection_remove.click()
    wait_until(app, lambda: w._file_job is None and w._scan_thread is None and w._statistics_job is None)
    assert not paths[0].exists() and not messages
    assert w.listening_history_store.book(track_id) is None
    assert page.result.collection.books == 2 and page.result.unique_books == 0
    assert str(paths[0]) not in w._track_records
    assert str(paths[0]) not in w.scan_index_store.entries()


def test_writer_ratings_average_series_equally_and_sort_unrated_last(window, monkeypatch):
    w, _ = window
    groups = {'A': [Path('/series/large'), Path('/series/small'), Path('/series/large'), Path('/series/unrated')],
              'B': [Path('/series/high')], 'C': [Path('/series/unrated')]}
    ratings = {'large': [5, 5, 5, 0], 'small': [1], 'high': [4], 'unrated': [0]}
    albums = {Path('/series')/name: [Path('/series')/name/f'{i}.mp3' for i in range(len(values))]
              for name, values in ratings.items()}
    monkeypatch.setattr(w, '_artist_album_dirs', lambda key: groups[key])
    monkeypatch.setattr(w, '_album_mp3s', lambda path: albums[path])
    monkeypatch.setattr(w, '_artist_display_name', lambda key: key)
    w._book_rating_by_path = {str(path): rating for album, paths in albums.items()
                               for path, rating in zip(paths, ratings[album.name])}
    assert w._artist_rating_summary('A') == (3, 2)
    assert w._artist_rating_summary('C') == (0, 0)
    monkeypatch.setattr(w, '_artist_sort_mode', lambda: 'rating_high')
    assert w._sorted_artist_keys(['C', 'A', 'B']) == ['B', 'A', 'C']
    monkeypatch.setattr(w, '_artist_sort_mode', lambda: 'rating_low')
    assert w._sorted_artist_keys(['C', 'B', 'A']) == ['A', 'B', 'C']


def test_writer_badge_updates_after_personal_rating(window, library, app):
    w, messages = window
    _, paths, _ = library
    open_book(w, paths[0], app)
    key = w.current_artist_key
    w._set_book_rating(5)
    assert w._artist_rating_summary(key) == (5, 1)
    assert 'Writer rating: 5 out of 5' in w._artist_item_by_dir[key].toolTip()
    w._set_book_rating(2)
    assert w._artist_rating_summary(key) == (2, 1)
    assert 'Writer rating: 2 out of 5' in w._artist_item_by_dir[key].toolTip()
    assert not messages


@pytest.mark.parametrize('count', [0, 1, 9, 10, 11])
def test_exact_tag_count_matches_card_and_statistics_warnings(window, library, count):
    w, _ = window
    _, paths, _ = library
    genre = ';'.join(f'Tag {i}' for i in range(count))
    issue = genre_tag_issue(genre)
    assert (issue is None) == (count == 10)
    record = w._track_records[str(paths[0])]
    w._track_records[str(paths[0])] = replace(record, metadata=replace(record.metadata, genre=genre))
    assert w._artist_metadata_error_count(w.current_artist_key) == (0 if count == 10 else 1)
    row = next(track for track in statistics_data.scan_snapshot(w._track_records)['tracks'] if track['path'] == str(paths[0]))
    expected = 'Missing genre' if count == 0 else 'Too few genre tags' if count < 10 else 'Too many genre tags'
    assert (expected in row['issues']) == (count != 10)


def test_duplicate_tags_never_fill_required_count():
    value = ';'.join(f'Tag {i}' for i in range(9)) + '|TAG 0, tag 1,\uff34\uff41\uff47 2'
    assert len(split_genre_tags(value)) == 9
    assert '9/10' in genre_tag_issue(value)['reason']
    assert genre_tag_issue(value + ';Unique tenth') is None


def test_full_page_navigation_years_and_unsaved_edit_are_preserved(window, library, app):
    w, messages = window
    _, paths, _ = library
    save_listen(w, paths[0], app)
    page = w.statistics_page
    assert w.statistics_toolbar.isVisible() and not w.main_toolbar.isVisible()
    assert not page.header.isVisible() and not page.tabs.tabBar().isVisible()
    assert page.period.isVisible() and page.refresh_button.isVisible()
    for index, action in enumerate(w.statistics_actions):
        action.trigger()
        app.processEvents()
        assert page.tabs.currentIndex() == index and action.isChecked()
        assert page.period.isEnabled() == (index != 2)
    page.period.setCurrentIndex(page.period.findData(None))
    wait_until(app, lambda: w._statistics_job is None)
    assert page.result.year is None
    w.statistics_toolbar.actions()[0].trigger()
    assert w.main_toolbar.isVisible() and not w.statistics_toolbar.isVisible()
    open_book(w, paths[0], app, edit=True)
    w.ed_title.setText('Unsaved title')
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    w.statistics_toolbar.actions()[0].trigger()
    assert w.stack.currentIndex() == w.PAGE_BOOK
    assert w._book_editing and w.ed_title.text() == 'Unsaved title'
    assert not messages


def test_recap_long_captions_fit_below_200px_covers(window, library, app):
    w, _ = window
    _, paths, _ = library
    save_listen(w, paths[0], app)
    page = w.statistics_page
    long = replace(page.result.listens[0], title='A Long Audiobook Title With Several Words: The Adventure Continues',
                   author='First Writer and Second Writer')
    page.recap_highlight_model.set_rows([long, long], ['Longest | 46 h 57 m', 'Shortest | 0 h 28 m'])
    for width in (1440, 900):
        w.resize(width, 850)
        page.tabs.setCurrentIndex(3)
        app.processEvents()
        page._fit_recap_tiles()
        text = page.recap_highlight_model.index(0).data()
        height = QFontMetrics(page.recap_highlights.font()).boundingRect(QRect(0, 0, 208, 10000), Qt.TextWordWrap, text).height()
        assert page.recap_highlights.iconSize().width() == 200
        assert page.recap_highlight_model.tile_size.height() >= 200 + height + 20
        assert page.recap_highlights.visualRect(page.recap_highlight_model.index(1)).bottom() < page.recap_highlights.height()
