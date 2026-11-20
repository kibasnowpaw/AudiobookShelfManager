"""Decimal persistence, upgrades, pending edits, fractional paint and statistics."""
from datetime import date
from pathlib import Path
import sqlite3

import pytest
from PySide6.QtCore import Qt, QRectF, QThread, QPoint
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtTest import QTest

from booklibconnect import ui_mainwindow as ui
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.rating_values import normalize_rating, format_rating, mean_rating, rating_text
from booklibconnect.rating_widgets import draw_stars
from booklibconnect.statistics_data import build_statistics, filter_listens
from booklibconnect.statistics_page import StatisticsPage
from test_responsive_io import app, library, window, open_book, wait_until


def saved_book(directory):
    store = ListeningHistoryStore(data_directory=directory)
    store.upsert_book(track_id='book', last_path='/sample/book.mp3', title='Book', duration_seconds=7200)
    store.add_completed_listen('book', started_on='2026-09-01', finished_on='2026-09-02')
    store.set_rating('book', 4)
    store.set_favorite('book', True)
    return store


def test_v2_upgrade_preserves_history_and_makes_backup(tmp_path):
    store = saved_book(tmp_path)
    before = store.statistics_snapshot()
    # The previous schema uses the same non-STRICT INTEGER column, with only
    # whole-number ratings. Reproduce its version and exact saved contents.
    with sqlite3.connect(store.database_path()) as connection:
        assert dict((row[1], row[2]) for row in connection.execute('PRAGMA table_info(books)'))['rating'] == 'INTEGER'
        connection.execute('ALTER TABLE books DROP COLUMN purchased_on')
        connection.execute('ALTER TABLE books DROP COLUMN purchase_source')
        connection.execute('ALTER TABLE books DROP COLUMN reviewed')
        connection.execute('PRAGMA user_version=2')
    upgraded = ListeningHistoryStore(data_directory=tmp_path)
    assert upgraded.statistics_snapshot() == before
    backups = list(upgraded.backup_dir().glob('*before-v2-to-v5.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        assert connection.execute('SELECT rating, favorite FROM books').fetchone() == (4, 1)
        assert connection.execute('SELECT COUNT(*) FROM listening_events').fetchone()[0] == 1
    upgraded.set_rating('book', 3.61)
    reopened = ListeningHistoryStore(data_directory=tmp_path)
    assert reopened.book('book').rating == 3.61
    assert reopened.book('book').favorite
    assert reopened.statistics_snapshot()['events'] == before['events']
    with sqlite3.connect(reopened.database_path()) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 5
        assert connection.execute('SELECT typeof(rating) FROM books').fetchone()[0] == 'real'


@pytest.mark.parametrize('value', [0, 0.01, 0.99, 3.61, 3.99, 4.99, 5])
def test_every_saved_rating_path_preserves_hundredths(tmp_path, value):
    store = saved_book(tmp_path)
    store.set_rating('book', value)
    store = ListeningHistoryStore(data_directory=tmp_path)
    assert store.book('book').rating == value
    assert store.book_preferences('book') == (value, True)
    assert store.book_ratings_by_path() == ({'/sample/book.mp3': value} if value else {})
    result = build_statistics(store.statistics_snapshot(), 2026)
    assert result.listens[0].rating == value
    assert result.average_rating == (value if value else None)
    assert sum(result.rating_counts) == bool(value)


@pytest.mark.parametrize('value', [-0.01, 5.01, 'NaN', 'Infinity', 'bad', None])
def test_invalid_rating_cannot_change_saved_state(tmp_path, value):
    store = saved_book(tmp_path)
    before = store.statistics_snapshot()
    with pytest.raises(ValueError, match='0 to 5'):
        store.set_rating('book', value)
    assert store.statistics_snapshot() == before


def test_replacement_and_rename_keep_decimal_preferences(tmp_path):
    store = saved_book(tmp_path)
    store.set_rating('book', 3.61)
    store.upsert_book(track_id='replacement', last_path='/sample/book.mp3', title='Replacement', duration_seconds=7200)
    assert store.book('book') is None
    assert store.book_preferences('replacement') == (3.61, True)
    assert {event.track_id for event in store.listens_for_book('replacement')} == {'replacement'}
    store.upsert_book(track_id='replacement', last_path='/sample/renamed.mp3', title='Replacement', duration_seconds=7200)
    assert store.book_ratings_by_path() == {'/sample/renamed.mp3': 3.61}


@pytest.mark.parametrize('typed', ['3.61', '3,61'])
def test_editor_saves_decimal_after_preview_and_rename(window, library, app, typed):
    w, messages = window
    _, paths, _ = library
    open_book(w, paths[0], app)
    w._set_book_rating(4)
    w._set_book_favorite(True)
    track_id = w._book_track_id
    w.show_book_editor()
    before = paths[0].read_bytes()
    w.ed_rating.setFocus()
    w.ed_rating.selectAll()
    QTest.keyClicks(w.ed_rating, typed)
    QTest.keyClick(w.ed_rating, Qt.Key_Tab)
    assert w.ed_rating.value() == 3.61
    assert w.editor_rating_stars.value == 3.61
    assert w.editor_rating_text.text() == '3.61 out of 5'
    assert w.listening_history_store.book(track_id).rating == 4
    assert paths[0].read_bytes() == before
    w.ed_title.setText('Decimal Rating Book')
    w.chk_autorename.setChecked(True)
    w.save_metadata_for_selected()
    wait_until(app, lambda: w._file_job is None)
    assert w._selected_track_path != paths[0] and w._selected_track_path.exists()
    assert not w._book_editing and w._book_rating == 3.61 and w._book_favorite
    assert w.book_rating_text_label.text() == '3.61 out of 5'
    assert [button.fraction for button in w.book_rating_buttons] == pytest.approx([1, 1, 1, .61, 0])
    assert w._album_rating_summary(w.current_album_dir) == (3.61, 1)
    assert w._artist_rating_summary(w.current_artist_key) == (3.61, 1)
    assert '3.61 out of 5' in w._artist_item_by_dir[w.current_artist_key].toolTip()
    assert w.listening_history_store.book(track_id).last_path == str(w._selected_track_path)
    assert w.listening_history_store.book(track_id).rating == 3.61
    open_book(w, w._selected_track_path, app, edit=True)
    assert w.ed_rating.value() == 3.61 and not messages


def test_cancel_discards_pending_decimal_and_clear_saves_unrated(window, library, app):
    w, messages = window
    _, paths, _ = library
    open_book(w, paths[0], app)
    w._set_book_rating(4)
    w.show_book_editor()
    w.ed_rating.setValue(3.61)
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    w.go_back()
    assert w.ed_rating.value() == 3.61 and w._book_editing
    w.cancel_book_editing()
    wait_until(app, lambda: w._file_job is None)
    assert w._book_rating == 4
    w.show_book_editor()
    assert w.ed_rating.value() == 4
    w.editor_rating_clear.click()
    assert w.ed_rating.value() == 0 and w.editor_rating_text.text() == 'Unrated'
    assert w._book_rating == 4
    w.save_metadata_for_selected()
    wait_until(app, lambda: w._file_job is None)
    assert w._book_rating == 0 and not w._book_rating_for_path(w._selected_track_path)
    assert w._album_rating_summary(w.current_album_dir) == (0, 0)
    assert not messages


@pytest.mark.parametrize('failure', ['mp3', 'rating'])
def test_failed_save_keeps_decimal_pending_and_retry_uses_real_path(window, library, app, monkeypatch, failure):
    w, messages = window
    _, paths, _ = library
    open_book(w, paths[0], app)
    w._set_book_rating(4)
    track_id = w._book_track_id
    w.show_book_editor()
    w.ed_rating.setValue(3.61)
    w.ed_title.setText('Renamed With Decimal')
    def fail(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        raise OSError('Simulated write failure')
    with monkeypatch.context() as patch:
        if failure == 'mp3':
            patch.setattr(ui, 'write_metadata_to_file', fail)
        else:
            patch.setattr(w.listening_history_store, 'set_rating', fail)
        w.save_metadata_for_selected()
        wait_until(app, lambda: w._file_job is None)
    assert messages
    assert w._book_editing and w.ed_rating.value() == 3.61
    assert w._selected_track_path.exists()
    assert w.listening_history_store.book(track_id).rating == 4
    if failure == 'rating':
        assert not paths[0].exists() and w._selected_track_path != paths[0]
        assert 'rating not saved' in messages[-1][0].lower()
    messages.clear()
    w.save_metadata_for_selected()
    wait_until(app, lambda: w._file_job is None)
    assert w._book_rating == 3.61 and not w._book_editing and not messages


def decimal_snapshot():
    values = [0, .5, 3.61, 3.62, 5]
    books = [dict(track_id=str(i), last_path=f'/sample/{i}.mp3', title=f'Book {i}', rating=value)
             for i, value in enumerate(values)]
    events = [dict(id=i, track_id=str(i), started_on='2026-09-01', finished_on='2026-09-01', duration_seconds=7200)
              for i in range(len(values))]
    events.append(dict(id=8, track_id='2', started_on='2026-09-02', finished_on='2026-09-02', duration_seconds=7200))
    return dict(books=books, events=events)


def test_statistics_counts_decimal_bands_and_exact_filters(app):
    result = build_statistics(decimal_snapshot(), 2026)
    assert result.rating_counts == (1, 0, 0, 2, 1)
    assert result.average_rating == pytest.approx((.5+3.61+3.62+5)/4)
    assert len(filter_listens(result, rating=3.61)) == 2
    assert len(filter_listens(result, rating=3.61, distinct=True)) == 1
    assert len(filter_listens(result, rating='band:4', distinct=True)) == 2
    page = StatisticsPage()
    page.show_result(result)
    page._choose_rating(3)
    assert {row.rating for row in page.history_model.rows} == {3.61, 3.62}
    combo = page.history_filters['rating']
    combo.setCurrentIndex(combo.findData(3.61))
    assert len(page.history_model.rows) == 1 and page.history_model.rows[0].rating == 3.61
    assert page.history_model.index(0, 8).data() == '3.61 out of 5'
    assert page.history_model.index(0, 8).data(Qt.UserRole) == 3.61
    page._clear_history_filters()
    page.history_sort.sort(8, Qt.AscendingOrder)
    ratings = [page.history_sort.index(i, 8).data(Qt.UserRole) for i in range(page.history_sort.rowCount())]
    assert ratings == sorted(ratings)
    page.close()
    wait_until(app, lambda: not page.artwork.busy)


def test_averages_keep_precision_until_display():
    series = mean_rating([3.61, 3.62, 0])
    assert series == 3.615 and format_rating(series) == '3.62'
    writer = mean_rating([series, 4])
    assert writer == 3.8075 and format_rating(writer) == '3.81'
    assert normalize_rating('3,61') == 3.61
    assert normalize_rating('3.615') == 3.62
    assert rating_text(3.61) == '3.61 out of 5'


def test_fractional_star_paint_has_three_full_stars_and_61_percent_of_fourth(app):
    def rendered(value):
        image = QImage(500, 100, QImage.Format_RGB32)
        image.fill(QColor('#20262e'))
        painter = QPainter(image)
        # A caller's active brush must not paint over the gold fill.
        painter.setBrush(QColor('#20262e'))
        draw_stars(painter, QRectF(0, 0, 500, 100), value)
        painter.end()
        return image
    full, partial = rendered(5), rendered(3.61)
    assert full.copy(0, 0, 300, 100) == partial.copy(0, 0, 300, 100)
    gold = QColor('#f5c451')
    xs = [x for x in range(300, 400) for y in range(100) if partial.pixelColor(x, y) == gold]
    assert xs and max(xs) in range(358, 362)
    assert not any(partial.pixelColor(x, y) == gold for x in range(400, 500) for y in range(100))


def test_edit_rating_button_focuses_visible_number_field_and_saves_typing(window, library, app):
    w, messages = window
    _, paths, _ = library
    w.resize(900, 600)
    open_book(w, paths[0], app)
    # Offscreen mouse events do not activate a window as a real desktop click does.
    w.activateWindow()
    app.processEvents()
    QTest.mouseClick(w.book_edit_rating_button, Qt.LeftButton)
    wait_until(app, lambda: w._book_editing and w.ed_rating.hasFocus())
    viewport = w.editor_scroll.viewport()
    center = w.ed_rating.mapTo(viewport, w.ed_rating.rect().center())
    assert viewport.rect().contains(center)
    assert w.editor_rating_label.text() == 'Rating (0-5)'
    assert w.editor_rating_label.mapTo(viewport, QPoint(0, 0)).y() > w.ed_title.mapTo(viewport, QPoint(0, 0)).y()
    QTest.keyClicks(w.ed_rating, '3.61')
    QTest.keyClick(w.ed_rating, Qt.Key_Tab)
    assert w.ed_rating.value() == 3.61
    assert w.editor_rating_text.text() == '3.61 out of 5'
    assert w._book_rating == 0
    w.editor_scroll.ensureWidgetVisible(w.btn_save)
    app.processEvents()
    QTest.mouseClick(w.btn_save, Qt.LeftButton)
    wait_until(app, lambda: w._file_job is None)
    assert not w._book_editing and w._book_rating == 3.61
    assert w.book_rating_text_label.text() == '3.61 out of 5' and not messages


def test_edit_metadata_reveals_rating_after_previous_scroll_position(window, library, app):
    w, messages = window
    _, paths, _ = library
    w.resize(900, 600)
    open_book(w, paths[0], app, edit=True)
    scroll = w.editor_scroll.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    app.processEvents()
    w.cancel_book_editing()
    wait_until(app, lambda: w._file_job is None)
    open_book(w, paths[1], app)
    w.act_edit_book.trigger()
    app.processEvents()
    viewport = w.editor_scroll.viewport()
    assert w._book_editing and w.ed_rating.isEnabled()
    center = w.ed_rating.mapTo(viewport, w.ed_rating.rect().center())
    assert viewport.rect().contains(center)
    assert scroll.value() < scroll.maximum()
    assert not messages
