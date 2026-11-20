"""Per-library and per-series settings, real card painting and review persistence."""
from dataclasses import replace
from pathlib import Path
import json
import sqlite3

import pytest
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QIcon, QColor
from PySide6.QtTest import QTest

from booklibconnect.gallery_options import effective_options, validate_rule
from booklibconnect.genre_tags import genre_tag_issue
from booklibconnect.gallery_cards import render_card, cover_rect, rating_rect
from booklibconnect.settings_dialog import SettingsDialog
from booklibconnect.library_settings_store import LibrarySettingsStore
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect import ui_mainwindow as ui, statistics_data
from test_responsive_io import app, library, window, open_book, wait_until
from test_library_profiles import other_library, switch, stats


def rule(mode, count=10, maximum=10):
    return dict(mode=mode, count=count, maximum=maximum)


def tags(count):
    return ';'.join(f'Tag {i}' for i in range(count))


@pytest.mark.parametrize('setting,valid,invalid', [
    (rule('off'), [0, 1, 10, 30], []),
    (rule('exact'), [10], [0, 9, 11]),
    (rule('exact', 0), [0], [1]),
    (rule('minimum', 3), [3, 10, 20], [0, 2]),
    (rule('maximum', 5), [0, 1, 5], [6, 10]),
    (rule('range', 3, 7), [3, 5, 7], [0, 2, 8]),
])
def test_rule_boundaries(setting, valid, invalid):
    for count in valid:
        assert genre_tag_issue(tags(count), setting) is None
    for count in invalid:
        assert genre_tag_issue(tags(count), setting)
    if valid and valid[-1] > 0:
        count = valid[-1]
        assert genre_tag_issue(tags(count) + ';TAG 0|Tag 0', setting) is None


@pytest.mark.parametrize('setting', [rule('bad'), rule('exact', -1), rule('minimum', 2.5),
                                      rule('range', 10, 9), rule('maximum', True)])
def test_invalid_rule_rejected(setting):
    with pytest.raises(ValueError):
        validate_rule(setting)


def test_old_library_catalog_migrates_and_keeps_main_exactly_ten(tmp_path):
    path = tmp_path / 'library_settings.json'
    old = dict(version=2, active_library='main', startup_library='last', preferences={'playback_speed': 1.75},
               libraries=[dict(id='main', name='Main', roots=[str(tmp_path/'Books')], sort={'writers':'rating_high'})])
    path.write_text(json.dumps(old))
    settings = LibrarySettingsStore(config_directory=tmp_path)
    assert settings.options()['tag_rule'] == rule('exact')
    assert settings.profile()['sort']['writers'] == 'rating_high'
    assert settings.preferences()['playback_speed'] == 1.75
    assert json.loads((tmp_path/'library_settings.before-gallery-rules.json').read_text()) == old
    assert LibrarySettingsStore(config_directory=tmp_path).snapshot() == settings.snapshot()


def test_native_settings_inherit_reset_cancel_and_keyboard_entry(window, library, app):
    w, _ = window
    _, paths, _ = library
    before = w.library_settings_store.snapshot()
    album = paths[0].parent
    dialog = SettingsDialog(w.library_settings_store, w, series_paths=[album])
    dialog.show()
    dialog.activateWindow()
    dialog.nav.setCurrentRow(3)
    dialog.rule_mode.setCurrentIndex(dialog.rule_mode.findData('range'))
    dialog.rule_count.setFocus()
    dialog.rule_count.selectAll()
    QTest.keyClicks(dialog.rule_count, '3')
    dialog.rule_maximum.setFocus()
    dialog.rule_maximum.selectAll()
    QTest.keyClicks(dialog.rule_maximum, '12')
    assert dialog.rule_maximum.value() == 12
    dialog.scope_choice.setCurrentIndex(dialog.scope_choice.findData(str(album)))
    assert dialog.rule_count.value() == 3
    assert not dialog.rule_mode.isEnabled()
    dialog._overrides['tag_rule'].setChecked(True)
    dialog.rule_mode.setCurrentIndex(dialog.rule_mode.findData('maximum'))
    dialog.rule_count.setValue(5)
    dialog.nav.setCurrentRow(4)
    dialog._overrides['rating_corner'].setChecked(True)
    dialog.rating_corner.setCurrentIndex(dialog.rating_corner.findData('bottom_right'))
    draft = w.library_settings_store.clone(dialog.values())
    assert draft.options()['tag_rule'] == rule('range', 3, 12)
    assert draft.options(album)['tag_rule']['count'] == 5
    assert draft.options(album)['rating_corner'] == 'bottom_right'
    assert draft.options(album)['show_completed']
    dialog.reset_series.click()
    assert w.library_settings_store.clone(dialog.values()).options(album) == draft.options()
    dialog.reject()
    assert w.library_settings_store.snapshot() == before


def test_series_overrides_and_library_options_stay_separate(window, library, tmp_path, app):
    w, _ = window
    _, paths, _ = library
    adult, adult_path = other_library(w, library, tmp_path)
    dialog = SettingsDialog(w.library_settings_store, w, series_paths=[paths[0].parent, adult_path.parent])
    dialog.rule_count.setValue(8)
    dialog.scope_choice.setCurrentIndex(dialog.scope_choice.findData(str(paths[0].parent)))
    dialog._overrides['show_rating'].setChecked(True)
    dialog.show_rating.setChecked(False)
    dialog.library_choice.setCurrentIndex(dialog.library_choice.findData(adult))
    dialog.rule_mode.setCurrentIndex(dialog.rule_mode.findData('off'))
    dialog._controls['show_goodreads'].setChecked(False)
    dialog._controls['show_reviewed'].setChecked(True)
    draft = w.library_settings_store.clone(dialog.values())
    draft.save()
    reopened = LibrarySettingsStore()
    assert reopened.options(ident='main')['tag_rule']['count'] == 8
    assert not reopened.options(paths[0].parent, 'main')['show_rating']
    assert reopened.options(ident=adult)['tag_rule']['mode'] == 'off'
    assert reopened.options(ident=adult)['show_reviewed']
    assert not reopened.options(ident=adult)['show_goodreads']
    assert reopened.options(ident=adult)['show_rating']
    dialog.reject()


def test_rule_change_updates_cards_editor_statistics_without_mp3_reads(window, library, app, monkeypatch):
    w, messages = window
    _, paths, _ = library
    path = paths[0]
    open_book(w, path, app)
    record = w._track_records[str(path)]
    w._track_records[str(path)] = replace(record, metadata=replace(record.metadata, genre=tags(4)))
    assert w._album_metadata_error_count(path.parent) == 1
    w.ed_genre.setText(tags(4))
    before = w.listening_history_store.statistics_snapshot()
    def forbidden(*args, **kwargs):
        raise AssertionError('Settings must not reread MP3 metadata')
    monkeypatch.setattr(ui, 'read_meta_and_length', forbidden)
    draft = w.library_settings_store.snapshot()
    draft['libraries'][0]['options']['tag_rule'] = rule('range', 3, 6)
    assert w._queue_library_change(w.library_settings_store.clone(draft))
    wait_until(app, lambda: w._file_job is None)
    assert w._album_metadata_error_count(path.parent) == 2  # the other two still have 10 tags
    assert not w._track_metadata_issues(path)
    assert w.editor_genre_warning_widget.isHidden()
    assert '3-6' in w.book_genre_caption_label.text()
    result = stats(w, app)
    book = next(row for row in result.collection_books if row.path == str(path))
    assert not any('genre' in text.casefold() for text in book.issues)
    assert w.listening_history_store.statistics_snapshot() == before
    assert not messages


def test_generic_reviewed_is_independent_persistent_and_scoped(window, library, tmp_path, app):
    w, messages = window
    _, paths, _ = library
    path = paths[0]
    adult, adult_path = other_library(w, library, tmp_path)
    open_book(w, path, app)
    track_id = w._book_track_id
    draft = w.library_settings_store.snapshot()
    draft['libraries'][0]['options'].update(show_goodreads=False, show_audible=True, show_reviewed=True)
    assert w._queue_library_change(w.library_settings_store.clone(draft))
    wait_until(app, lambda: w._file_job is None)
    w.show_book_editor()
    assert w.chk_goodreads.isHidden()
    assert not w.chk_audible.isHidden()
    assert not w.chk_reviewed.isHidden()
    w.chk_reviewed.setChecked(True)
    wait_until(app, lambda: w._file_job is None)
    assert w.listening_history_store.book(track_id).reviewed
    assert not w.review_store.goodreads_done(path)
    assert not w.review_store.audible_done(path)
    assert 'Reviewed: Yes' in [text for text, _ in w._book_card_lines(path)]
    w.chk_audible.setChecked(True)
    assert 'Audible reviewed: Yes' in [text for text, _ in w._book_card_lines(path)]
    assert any(text.startswith('Audible 1/') for text, _ in w._series_card_lines(path.parent))
    w._book_editing = False
    switch(w, adult, app)
    assert str(path) not in w._reviewed_paths
    open_book(w, adult_path, app)
    assert not w.listening_history_store.book(w._book_track_id).reviewed
    switch(w, 'main', app)
    assert str(path) in w._reviewed_paths
    assert not messages


def test_reviewed_v3_migration_backup_replacement_rename(tmp_path):
    store = ListeningHistoryStore(data_directory=tmp_path)
    store.upsert_book(track_id='old', last_path='/books/old.mp3', title='Old')
    store.set_rating('old', 3.61)
    store.add_completed_listen('old', started_on='2026-01-01', finished_on='2026-01-02')
    with sqlite3.connect(store.database_path()) as connection:
        connection.execute('ALTER TABLE books DROP COLUMN purchased_on')
        connection.execute('ALTER TABLE books DROP COLUMN purchase_source')
        connection.execute('ALTER TABLE books DROP COLUMN reviewed')
        connection.execute('PRAGMA user_version=3')
    store = ListeningHistoryStore(data_directory=tmp_path)
    assert store.book('old').rating == 3.61
    assert not store.book('old').reviewed
    assert list(store.backup_dir().glob('*before-v3-to-v5.sqlite3'))
    store.set_reviewed('old', True)
    store.upsert_book(track_id='new', last_path='/books/old.mp3', title='Replacement')
    assert store.book('old') is None
    assert store.book('new').reviewed
    store.upsert_book(track_id='new', last_path='/books/renamed.mp3', title='Renamed')
    reopened = ListeningHistoryStore(data_directory=tmp_path)
    assert reopened.reviewed_paths() == {'/books/renamed.mp3'}
    assert reopened.book('new').rating == 3.61
    assert len(reopened.listens_for_book('new')) == 1


@pytest.mark.parametrize('corner', ['top_left', 'top_right', 'bottom_left', 'bottom_right'])
def test_card_rating_at_each_corner_and_off(app, corner):
    cover = QPixmap(200, 200)
    cover.fill(QColor('#386577'))
    size = QSize(216, 300)
    plain = render_card(QIcon(cover), size, [], rating=3.61,
                        options={'show_rating': False, 'rating_corner': corner}).pixmap(size).toImage()
    visible = render_card(QIcon(cover), size, [], rating=3.61,
                          options={'show_rating': True, 'rating_corner': corner}).pixmap(size).toImage()
    area = rating_rect(size, corner)
    assert cover_rect(size).size() == QSize(200, 200)
    assert cover_rect(size).contains(area)
    inside, outside = 0, 0
    for y in range(size.height()):
        for x in range(size.width()):
            if plain.pixel(x, y) != visible.pixel(x, y):
                if area.adjusted(-1, -1, 1, 1).contains(x, y):
                    inside += 1
                else:
                    outside += 1
    assert inside > 1000 and outside == 0


def test_all_card_switches_hide_labels_and_expand_layout_when_enabled(window, library, app):
    w, _ = window
    _, paths, _ = library
    path = paths[0]
    open_book(w, path, app)
    w._set_book_rating(4)
    old = w.listening_history_store.statistics_snapshot()
    draft = w.library_settings_store.snapshot()
    options = draft['libraries'][0]['options']
    for key in list(options):
        if key.startswith('show_'):
            options[key] = False
    assert w._queue_library_change(w.library_settings_store.clone(draft))
    wait_until(app, lambda: w._file_job is None and not w._artwork_queue)
    assert w._book_card_lines(path) == []
    assert w._series_card_lines(path.parent) == []
    assert w.tracks.iconSize() == QSize(216, 216)
    assert 'rating' not in w._book_item_by_path[str(path)].toolTip().casefold()
    assert 'completed' not in w._book_item_by_path[str(path)].toolTip().casefold()
    assert w.listening_history_store.statistics_snapshot() == old
    for key in list(options):
        if key.startswith('show_'):
            options[key] = True
    assert w._queue_library_change(w.library_settings_store.clone(draft))
    wait_until(app, lambda: w._file_job is None and not w._artwork_queue)
    w.load_albums(w.current_artist_key)
    w._go_to(w.PAGE_ALBUMS)
    wait_until(app, lambda: not w._artwork_queue)
    app.processEvents()
    assert w.albums.visualItemRect(w.albums.item(0)).height() >= w.albums.iconSize().height()
    w.load_tracks(path.parent)
    w._go_to(w.PAGE_TRACKS)
    wait_until(app, lambda: not w._artwork_queue)
    app.processEvents()
    assert w.tracks.visualItemRect(w.tracks.item(0)).height() >= w.tracks.iconSize().height()
    assert w.tracks.visualItemRect(w.tracks.item(0)).width() >= 216


def test_scanner_uses_series_rule_even_with_unchanged_index(window, library, app):
    w, messages = window
    _, paths, _ = library
    draft = w.library_settings_store.snapshot()
    profile = draft['libraries'][0]
    profile['options']['tag_rule'] = rule('exact', 2)
    profile['series_overrides'][str(paths[0].parent)] = {'tag_rule': rule('minimum', 2)}
    assert w._queue_library_change(w.library_settings_store.clone(draft))
    wait_until(app, lambda: w._file_job is None)
    worker = ui.ScanWorker(w.library_roots, w.expected_store, w.scan_index_store,
                           w.review_store.legacy_snapshot(), w.listening_history_store, profile=profile)
    payloads = []
    worker.finished.connect(payloads.append)
    worker.run()
    assert payloads and not payloads[0]['genre_issues']
    assert not messages


def test_generic_review_survives_real_metadata_rename(window, library, app):
    w, messages = window
    _, paths, _ = library
    path = paths[0]
    open_book(w, path, app)
    ident = w._book_track_id
    w.listening_history_store.set_reviewed(ident, True)
    w._reload_completed_history_paths()
    w.show_book_editor()
    w.ed_title.setText('New Reviewed Title')
    w.ed_rating.setValue(3.61)
    w.chk_autorename.setChecked(True)
    w.save_metadata_for_selected()
    wait_until(app, lambda: w._file_job is None)
    new_path = w._selected_track_path
    assert new_path != path and new_path.exists() and not path.exists()
    assert str(path) not in w._reviewed_paths
    assert str(new_path) in w._reviewed_paths
    assert w.listening_history_store.book(ident).reviewed
    assert w.listening_history_store.book(ident).rating == 3.61
    assert not messages
