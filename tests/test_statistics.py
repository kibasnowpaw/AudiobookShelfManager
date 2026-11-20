"""Statistics counting, read-only storage, and real Qt event-loop checks."""
import sqlite3
import time
from datetime import date

import pytest
from PySide6.QtCore import Qt, QThread, QTimer

from booklibconnect.statistics_data import build_statistics
from booklibconnect.statistics_page import StatisticsPage
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect import ui_mainwindow as ui
from test_responsive_io import app, library, window, wait_until, open_book


@pytest.fixture
def snapshot():
    return {'books': [
        dict(track_id='a', last_path='/library/a.mp3', title='Book A', author='Author A', narrator='Voice A', rating=5, favorite=1, duration_seconds=999999),
        dict(track_id='b', last_path='/library/b.mp3', title='Book B', author='Author A', narrator='Voice B', rating=3, favorite=0),
        dict(track_id='c', last_path='/removed/c.mp3', title='Book C', author='Author C', narrator='Voice A', rating=0, favorite=1),
        dict(track_id='d', last_path='/library/d.mp3', title='Unread Book', rating=4, favorite=1),
    ], 'events': [
        dict(id=1, track_id='a', started_on='2025-12-20', finished_on='2025-12-22', duration_seconds=36000),
        dict(id=2, track_id='a', started_on='2025-12-31', finished_on='2026-01-02', duration_seconds=43200),
        dict(id=3, track_id='b', started_on='2026-01-02', finished_on='2026-01-02', duration_seconds=7200),
        dict(id=4, track_id='a', started_on='2026-02-01', finished_on='2026-02-04', duration_seconds=43200),
        dict(id=5, track_id='c', started_on='2026-02-01', finished_on='2026-02-05', duration_seconds=None),
    ]}


def test_cross_year_relistens_and_duration_snapshots(snapshot):
    result = build_statistics(snapshot, 2026)
    assert len(result.listens) == 4
    assert result.unique_books == 3 and result.relistens == 2
    assert result.audio_seconds == 26*3600  # Never the book's current 999999 seconds.
    assert result.known_durations == 3
    assert result.average_days == (3+1+4+5)/4
    assert result.average_rating == 4  # Book A's relistens do not add extra votes.
    assert result.rating_counts == (0, 0, 1, 0, 1)
    assert result.favorite_books == 2
    assert result.years == (2026, 2025)
    assert len(result.periods) == 12
    assert result.periods[0].listens == 2 and result.periods[0].seconds == 14*3600
    assert result.periods[1].listens == 2 and result.periods[1].known_durations == 1
    assert all(month.listens == 0 for month in result.periods[2:])
    assert sum(p.seconds for p in result.periods) == result.audio_seconds
    assert result.longest.title == 'Book A' and result.shortest.title == 'Book B'
    assert result.authors[0].name == 'Author A' and result.authors[0].listens == 3
    assert result.authors[0].books == 2
    earlier = build_statistics(snapshot, 2025)
    assert earlier.relistens == 0 and earlier.average_days == 3
    all_time = build_statistics(snapshot, None)
    assert len(all_time.listens) == 5 and all_time.relistens == 2
    assert [p.label for p in all_time.periods] == ['2025', '2026']


def test_collection_is_current_paths_but_history_keeps_removed_books(snapshot):
    current = dict(paths=['/library/a.mp3', '/library/a.mp3', '/library/b.mp3', '/library/d.mp3'], series=2, authors=2)
    result = build_statistics(snapshot, 2025, current)
    assert result.collection.books == 3
    assert result.collection.completed == 2  # All time, even when the selected year is 2025.
    assert result.collection.rated == 3 and result.collection.favorites == 2
    assert build_statistics(snapshot, None, current).unique_books == 3
    assert not build_statistics(snapshot, None).collection.available


def test_invalid_legacy_dates_unknown_lengths_and_tied_finishes(snapshot):
    invalid = [
        dict(id=6, track_id='b', started_on=None, finished_on='2026-03-01'),
        dict(id=7, track_id='b', started_on='2026-03-02', finished_on='2026-03-01'),
        dict(id=8, track_id='b', started_on='bad', finished_on='2026-03-01'),
        dict(id=9, track_id='absent', started_on='2026-03-01', finished_on='2026-03-01'),
    ]
    snapshot['events'].extend(invalid)
    snapshot['events'].append(dict(id=10, track_id='b', started_on='2026-01-02', finished_on='2026-01-02', duration_seconds=float('nan')))
    result = build_statistics(snapshot, 2026)
    assert result.excluded_entries == 4
    assert result.relistens == 3
    assert result.known_durations == 3
    assert next(row for row in result.listens if row.event_id == 10).relisten
    empty = build_statistics(snapshot, 2024)
    assert not empty.listens and empty.average_days is None and empty.average_rating is None
    assert empty.longest is None and all(p.seconds == 0 for p in empty.periods)
    assert build_statistics({'books': [], 'events': []}, None).periods == ()


def test_database_snapshot_is_read_only_and_picks_up_edits(tmp_path, monkeypatch):
    store = ListeningHistoryStore(data_directory=tmp_path/'history')
    store.upsert_book(track_id='a', last_path=tmp_path/'absent.mp3', title='Kept history', author='Author', duration_seconds=7200)
    event_id = store.add_completed_listen('a', started_on='2026-01-01', finished_on='2026-01-01')
    store.set_rating('a', 5)
    before = store.statistics_snapshot()
    calls = []
    original = sqlite3.connect
    def read_only(*args, **kwargs):
        assert args[0].endswith('?mode=ro') and kwargs['uri']
        connection = original(*args, **kwargs)
        connection.set_trace_callback(calls.append)
        return connection
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', read_only)
        after = store.statistics_snapshot()
    assert before == after
    assert not any(sql.lstrip().split()[0] in ('UPDATE', 'INSERT', 'DELETE', 'CREATE', 'ALTER') for sql in calls)
    assert build_statistics(after, 2026).average_rating == 5
    store.set_rating('a', 2)
    assert build_statistics(store.statistics_snapshot(), 2026).average_rating == 2
    store.delete_listen(event_id)
    assert not build_statistics(store.statistics_snapshot(), 2026).listens


def test_table_sort_is_numeric_and_no_rich_text(snapshot, app):
    snapshot['books'][0]['title'] = '<b>Literal book title</b>'
    page = StatisticsPage()
    result = build_statistics(snapshot, 2026)
    page.show_result(result)
    page.history_sort.sort(5, Qt.AscendingOrder)
    values = [page.history_sort.index(i, 5).data(Qt.UserRole) for i in range(4)]
    assert values == [-1, 7200, 43200, 43200]
    page.history_sort.sort(6, Qt.AscendingOrder)
    assert [page.history_sort.index(i, 6).data(Qt.DisplayRole) for i in range(4)] == ['1','3','4','5']
    assert '<b>Literal book title</b>' in page.longest.text()
    assert page.longest.textFormat() == Qt.PlainText
    page.resize(1000, 720)
    page.show()
    app.processEvents()
    assert page._columns == 3 and page.cards[0].width() >= 170
    assert not page.grab().isNull()  # Exercise QWidget's painting/metric virtuals.
    page.metric_selector.setCurrentIndex(1)
    assert page.timeline.unit == 'hours'
    assert sum(item[1] for item in page.timeline.entries) == 26
    page.close()


def test_statistics_background_load_coalesces_year_changes(window, app, monkeypatch):
    w, messages = window
    original = w.listening_history_store.statistics_snapshot
    calls, ticks = [], []
    def slow():
        assert QThread.currentThread() != app.thread()
        calls.append(1)
        time.sleep(.18)
        return original()
    monkeypatch.setattr(w.listening_history_store, 'statistics_snapshot', slow)
    monkeypatch.setattr(ui, 'read_meta_and_length', lambda *args: pytest.fail('Statistics reread an MP3'))
    monkeypatch.setattr(ui, 'get_embedded_cover_bytes', lambda *args: pytest.fail('Statistics read artwork'))
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    w.act_statistics.trigger()
    page = w.statistics_page
    assert page.progress.isVisible() and not page.tabs.isEnabled()
    page.period.setCurrentIndex(page.period.findData(None))
    page.period.setCurrentIndex(page.period.findData(date.today().year))
    page.period.setCurrentIndex(page.period.findData(None))
    wait_until(app, lambda: w._statistics_job is None and page.result is not None)
    timer.stop()
    assert page.result.year is None and len(calls) == 2
    assert len(ticks) >= 12
    assert page.result.collection.books == 3
    assert w.stack.currentIndex() == w.PAGE_STATISTICS and not messages
    assert not page.progress.isVisible() and page.tabs.isEnabled()


def test_statistics_back_keeps_unsaved_editor_and_refreshes_ratings(window, library, app):
    w, messages = window
    _, paths, _ = library
    open_book(w, paths[0], app, True)
    w.ed_title.setText('Unsaved title')
    book = w.listening_history_store.book_for_path(paths[0])
    w.listening_history_store.add_completed_listen(book.track_id, started_on=date.today(), finished_on=date.today())
    w._set_book_rating(4)
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    assert w.statistics_page.result.average_rating == 4
    w.go_back()
    assert w.stack.currentIndex() == w.PAGE_BOOK and w._book_editing
    assert w.ed_title.text() == 'Unsaved title'
    w._set_book_rating(2)
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    assert w.statistics_page.result.average_rating == 2
    assert not messages


def test_statistics_error_retry_and_safe_close(window, app, monkeypatch):
    w, messages = window
    original = w.listening_history_store.statistics_snapshot
    def failed():
        raise OSError('Test database unavailable')
    monkeypatch.setattr(w.listening_history_store, 'statistics_snapshot', failed)
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    assert 'Test database unavailable' in w.statistics_page.notice.text()
    assert not w.statistics_page.tabs.isEnabled()
    def slow():
        time.sleep(.18)
        return original()
    monkeypatch.setattr(w.listening_history_store, 'statistics_snapshot', slow)
    w.statistics_page.refresh_button.click()
    w.close()
    assert w.isVisible() and w._closing
    wait_until(app, lambda: w._statistics_job is None)
    assert not w.isVisible()
    assert not messages


def test_scan_completion_keeps_statistics_open(window, library, app):
    w, messages = window
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None and w._statistics_job is None)
    assert w.stack.currentIndex() == w.PAGE_STATISTICS
    assert w.statistics_page.result.collection.books == 3
    assert not w._artwork_timer.isActive()
    w.go_back()
    assert w.stack.currentIndex() == w.PAGE_ARTISTS
