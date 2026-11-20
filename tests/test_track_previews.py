"""Series cards must become useful even when the user never selects a book."""
import threading

from PySide6.QtCore import QThread
from PySide6.QtGui import QColor

from booklibconnect import diagnostics as diag, ui_mainwindow as ui
from test_book_open import paint
from test_library_profiles import other_library
from test_m4b_support import make_m4b, metadata

from booklibconnect.gallery_cards import cover_rect
from test_responsive_io import app, library, window, wait_until, open_book


def card_color(window, item):
    size = window.tracks.iconSize()
    image = item.icon().pixmap(size).toImage()
    point = cover_rect(size).center()
    if image.isNull() or not image.rect().contains(point):
        return None
    return image.pixelColor(point).name()


def open_series(w):
    w.open_artist(w.artists.item(0))
    w.open_album(w.albums.item(0))


def test_series_displays_all_book_covers_without_a_book_click(window, library, app):
    w, _ = window
    _, paths, _ = library
    open_series(w)
    assert w.stack.currentIndex() == w.PAGE_TRACKS
    wait_until(app, lambda: all(card_color(w, w._book_item_by_path[str(p)]) ==
                                QColor('#287fb5').name() for p in paths), timeout=2)
    assert w._file_job is None and w._book_track_id is None


def test_mixed_series_loads_correct_images_without_writing_history_or_audio(
        window, library, make_m4b, app, tmp_path):
    w, messages = window
    _, paths, _ = library
    m4b = make_m4b(paths[0].parent / 'Chaptered.m4b')
    image = tmp_path / 'chapter.png'
    paint(image, '#316326')
    ui.write_metadata_to_file(m4b, metadata(4), cover_path=image)
    paint(paths[1].with_suffix('.png'), '#61289c')
    record = w._track_records[str(paths[0])]
    store = w.listening_history_store
    ident = store.register_track(paths[0], record.metadata, record.length_seconds)
    store.set_rating(ident, 3.61)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None)
    before = store.statistics_snapshot()
    books = [*paths, m4b]
    stamps = [p.stat().st_mtime_ns for p in books]
    open_series(w)
    colors = ['#287fb5', '#61289c', '#287fb5', '#316326']
    wait_until(app, lambda: [card_color(w, w._book_item_by_path[str(p)]) for p in books] == colors)
    assert w._file_job is None and w._book_track_id is None and not messages
    assert '3.61' in w._book_item_by_path[str(paths[0])].toolTip()
    assert 'METADATA ERROR' in w._book_item_by_path[str(paths[1])].toolTip()
    assert store.statistics_snapshot() == before
    assert [p.stat().st_mtime_ns for p in books] == stamps
    assert store.book_for_path(m4b) is None


def test_prepared_cover_is_reused_but_ratings_and_history_are_live(window, library, app, monkeypatch, tmp_path):
    w, _ = window
    _, paths, _ = library
    path = paths[0]
    journal = diag.Journal(tmp_path / 'support' / 'sessions.sqlite3')
    monkeypatch.setattr(diag, '_active', journal)
    open_series(w)
    wait_until(app, lambda: all(w._series_previews.preview(p) is not None for p in paths))
    record = w._track_records[str(path)]
    ident = w.listening_history_store.register_track(path, record.metadata, record.length_seconds)
    w.listening_history_store.set_rating(ident, 3.61)
    w.listening_history_store.add_completed_listen(ident, started_on='2026-09-01', finished_on='2026-09-01')
    def no_second_read(*a, **kw):
        raise AssertionError('Opened artwork again after preloading')
    monkeypatch.setattr(ui, '_read_book_cover', no_second_read)
    open_book(w, path, app)
    assert w._book_rating == 3.61 and w._completed_listen_count_by_path[str(path)] == 1
    assert w._book_cover_image.pixelColor(4, 4).name() == '#287fb5'
    text = diag.report(journal.path)
    assert 'series.covers  ok' in text and 'prepared=3' in text
    assert 'preview_cached=1' in next(line for line in text.splitlines() if 'book.open  ok' in line)
    assert 'Example Author' not in text and str(path) not in text
    journal.close()


def test_sidecar_replaced_after_preview_shows_current_image_on_first_open(window, library, app):
    w, _ = window
    _, paths, _ = library
    path = paths[1]
    open_series(w)
    wait_until(app, lambda: w._series_previews.preview(path) is not None)
    paint(path.with_suffix('.png'), '#a62f37')
    open_book(w, path, app, edit=True)
    assert w._book_cover_image.pixelColor(5, 5).name() == '#a62f37'
    assert 'does not match' in w._track_records[str(path)].cover_issue['reason']
    w.cancel_book_editing()
    w.go_back()
    wait_until(app, lambda: card_color(w, w._book_item_by_path[str(path)]) == '#a62f37')


def delay_first_read(w, app, monkeypatch):
    started, release = threading.Event(), threading.Event()
    read = w._series_previews.read
    calls = []
    def delayed(path, record, artist_dirs):
        assert QThread.currentThread() != app.thread()
        calls.append(path)
        if len(calls) == 1:
            started.set()
            assert release.wait(5), 'Preview was not released'
        return read(path, record, artist_dirs)
    monkeypatch.setattr(w._series_previews, 'read', delayed)
    return started, release, calls


def test_click_does_not_wait_for_another_book_cover(window, library, app, monkeypatch):
    w, messages = window
    _, paths, _ = library
    started, release, calls = delay_first_read(w, app, monkeypatch)
    try:
        open_series(w)
        wait_until(app, started.is_set)
        other = next(p for p in paths if p != calls[0])
        open_book(w, other, app)
        assert w.stack.currentIndex() == w.PAGE_BOOK and not messages
        assert w._series_previews.job is not None and not release.is_set()
    finally:
        release.set()
    wait_until(app, lambda: w._series_previews.job is None)
    w.go_back()
    wait_until(app, lambda: all(card_color(w, w._book_item_by_path[str(p)]) == '#287fb5' for p in paths))


def test_gallery_switch_discards_the_old_cover_in_flight(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    _, paths, _ = library
    other, other_path = other_library(w, library, tmp_path)
    started, release, _ = delay_first_read(w, app, monkeypatch)
    try:
        open_series(w)
        wait_until(app, started.is_set)
        assert w.switch_library(other)
        assert w._pending_library_change is not None
    finally:
        release.set()
    wait_until(app, lambda: w._pending_library_change is None and w._scan_thread is None
               and w._file_job is None and set(w._track_records) == {str(other_path)})
    open_series(w)
    wait_until(app, lambda: card_color(w, w._book_item_by_path[str(other_path)]) == '#bb4262')
    assert all(w._series_previews.preview(p) is None for p in paths)
    assert set(w._book_item_by_path) == {str(other_path)} and not messages


def test_window_waits_for_read_on_close(window, app, monkeypatch):
    w, _ = window
    started, release, _ = delay_first_read(w, app, monkeypatch)
    try:
        open_series(w)
        wait_until(app, started.is_set)
        assert not w.close() and w._closing
    finally:
        release.set()
    wait_until(app, lambda: not w.isVisible() and w._series_previews.job is None)


def test_scrolling_moves_newly_visible_books_ahead_of_hidden_ones(window, library, app, tmp_path, monkeypatch):
    import shutil
    from PySide6.QtWidgets import QAbstractItemView
    from booklibconnect.models import TrackMeta
    w, _ = window
    _, paths, cover = library
    for number in range(4, 27):
        path = paths[0].parent / f'{number:02} - Book {number}.mp3'
        shutil.copyfile(tmp_path / 'base.mp3', path)
        ui.write_metadata_to_file(path, TrackMeta(title=f'Book {number}', artist='Example Author',
            album='Example Series', trackno=number, totaltracks=26), cover_path=cover)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None)
    w.resize(720, 540)
    started, release, calls = delay_first_read(w, app, monkeypatch)
    try:
        open_series(w)
        wait_until(app, started.is_set)
        last = w.tracks.item(w.tracks.count() - 1)
        w.tracks.scrollToItem(last, QAbstractItemView.PositionAtBottom)
        last_key = str(w._book_item_path(last))
        wait_until(app, lambda: last_key in w._series_previews.visible)
        visible = set(w._series_previews.visible)
        assert 1 <= len(visible) < w.tracks.count() - 1
    finally:
        release.set()
    wait_until(app, lambda: len(calls) >= 2)
    assert str(calls[1]) in visible
    assert w._file_job is None


def test_artist_picture_is_only_a_fallback_for_the_card(window, library, app):
    from mutagen.id3 import ID3
    w, _ = window
    _, paths, _ = library
    path = paths[0]
    tags = ID3(path)
    tags.delall('APIC')
    tags.save(path)
    paint(path.parent.parent / 'artist.png', '#da9a18')
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None)
    open_series(w)
    wait_until(app, lambda: card_color(w, w._book_item_by_path[str(path)]) == '#da9a18')
    open_book(w, path, app)
    assert w._book_cover_image.isNull()
