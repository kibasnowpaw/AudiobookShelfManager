"""Library migration, independent state, settings and asynchronous switching."""
from datetime import date
from pathlib import Path
import json
import shutil
import sqlite3
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QFileDialog, QInputDialog

from booklibconnect import ui_mainwindow as ui, statistics_data, statistics_artwork
from booklibconnect import library_settings_store as settings_module
from booklibconnect.library_settings_store import LibrarySettingsStore
from booklibconnect.settings_dialog import SettingsDialog
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.models import TrackMeta
from test_responsive_io import app, library, window, open_book, wait_until


def other_library(w, library, tmp_path):
    root, paths, _ = library
    other = tmp_path / 'Adult'
    path = other / 'Adult Writer' / 'Adult Series' / '01 - Adult Book - Adult Writer.mp3'
    path.parent.mkdir(parents=True)
    shutil.copy2(paths[0], path)
    cover = tmp_path / 'adult-cover.png'
    image = QImage(200, 200, QImage.Format_RGB32)
    image.fill(QColor('#bb4262'))
    image.save(str(cover))
    ui.write_metadata_to_file(path, TrackMeta(title='Adult Book', artist='Adult Writer',
        album='Adult Series', narrator='Adult Narrator', trackno=1, totaltracks=1,
        genre='Fantasy;Romance;Magic;Adventure;Action;Drama;Humor;Fiction;Adult;Epic'), cover_path=cover)
    ident = w.library_settings_store.add_library('Adult', [other])
    w._refresh_library_switchers()
    return ident, path


def switch(w, ident, app):
    assert w.switch_library(ident)
    wait_until(app, lambda: w._pending_library_change is None and w._file_job is None and w._scan_thread is None)
    assert w.library_settings_store.active_library_id() == ident


def stats(w, app):
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None)
    return w.statistics_page.result


def test_legacy_folders_migrate_once_without_moving_database(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('BOOKLIBCONNECT_DATA_DIR', str(tmp_path / 'data'))
    config = tmp_path / 'config' / 'booklibconnect'
    config.mkdir(parents=True)
    root = tmp_path / 'offline-audiobooks'
    original = json.dumps({'library_roots': [str(root)]})
    (config / 'library_settings.json').write_text(original)
    history = ListeningHistoryStore()
    history.upsert_book(track_id='legacy', last_path=root / 'Book.mp3', title='Existing book')
    history.set_rating('legacy', 3.61)
    history.add_completed_listen('legacy', started_on='2025-01-01', finished_on='2025-01-02')
    before = history.statistics_snapshot()
    settings = LibrarySettingsStore()
    assert settings.profile()['name'] == 'Main'
    assert settings.library_roots() == [root]
    assert settings.startup_library_id() == 'main'
    assert settings.profile_data_dir() == history.data_dir()
    assert (config / 'library_settings.before-separate-libraries.json').read_text() == original
    assert LibrarySettingsStore().snapshot() == settings.snapshot()
    assert ListeningHistoryStore().statistics_snapshot() == before


@pytest.mark.parametrize('kind', ['same', 'parent', 'child', 'symlink'])
def test_overlapping_roots_cannot_mix_libraries(tmp_path, kind):
    root = tmp_path / 'Main'
    root.mkdir()
    settings = LibrarySettingsStore(config_directory=tmp_path / 'config')
    settings.set_library_roots([root])
    if kind == 'same':
        other = root
    elif kind == 'parent':
        other = tmp_path
    elif kind == 'child':
        other = root / 'Adult'
    else:
        other = tmp_path / 'alias'
        other.symlink_to(root, target_is_directory=True)
    before = settings.file_path().read_bytes()
    with pytest.raises(ValueError, match='cannot share'):
        settings.add_library('Adult', [other])
    assert settings.file_path().read_bytes() == before
    assert len(settings.libraries()) == 1


def test_corrupt_catalog_is_not_replaced(tmp_path):
    folder = tmp_path / 'settings'
    folder.mkdir()
    path = folder / 'library_settings.json'
    path.write_text('{broken')
    with pytest.raises(ValueError, match='file has been kept'):
        LibrarySettingsStore(config_directory=folder)
    assert path.read_text() == '{broken'


def test_library_catalog_survives_disposable_config_removal(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('BOOKLIBCONNECT_DATA_DIR', str(tmp_path / 'permanent'))
    settings = LibrarySettingsStore()
    ident = settings.add_library('Adult', [tmp_path / 'offline-adult'])
    settings.set_active_library(ident)
    before = settings.snapshot()
    assert settings.file_path().is_relative_to(tmp_path / 'permanent')
    shutil.rmtree(tmp_path / 'config')
    restored = LibrarySettingsStore()
    assert restored.snapshot() == before
    assert restored.profile()['name'] == 'Adult'
    assert restored.library_roots() == [tmp_path / 'offline-adult']


def test_renaming_library_keeps_storage_identity(tmp_path):
    settings = LibrarySettingsStore(config_directory=tmp_path / 'config')
    ident = settings.add_library('Adult')
    original_config = settings.profile_config_dir(ident)
    original_data = settings.profile_data_dir(ident)
    draft = settings.snapshot()
    draft['libraries'][1]['name'] = 'Private audio'
    renamed = settings.clone(draft)
    renamed.save()
    restored = LibrarySettingsStore(config_directory=tmp_path / 'config')
    assert restored.profile_config_dir(ident) == original_config
    assert restored.profile_data_dir(ident) == original_data
    assert restored.profile(ident)['name'] == 'Private audio'


def test_main_and_adult_keep_separate_ratings_history_statistics_and_caches(window, library, tmp_path, app):
    w, messages = window
    _, paths, _ = library
    open_book(w, paths[0], app)
    main_store = w.listening_history_store
    main_id = w._book_track_id
    main_store.set_rating(main_id, 3.61)
    main_store.set_favorite(main_id, True)
    main_store.add_completed_listen(main_id, started_on=date.today(), finished_on=date.today())
    main_store_snapshot = main_store.statistics_snapshot()
    main_index = w.scan_index_store.file_path()
    adult, path = other_library(w, library, tmp_path)
    assert stats(w, app).average_rating == 3.61
    switch(w, adult, app)
    assert w.statistics_page is None
    assert set(w._track_records) == {str(path)}
    assert set(w._embedded_cache).issubset({str(path)}) and not w._book_rating_by_path
    assert w.listening_history_store.database_path() != main_store.database_path()
    assert w.scan_index_store.file_path() != main_index
    assert w.expected_store.get(paths[0].parent) == 0
    assert w.review_store.legacy_snapshot() == {}
    open_book(w, path, app)
    # The copied MP3 deliberately has the same permanent ID. State is still isolated.
    assert w._book_track_id == main_id and w._book_rating == 0 and not w._book_favorite
    w.listening_history_store.set_rating(main_id, 4.72)
    w.listening_history_store.add_completed_listen(main_id, started_on=date.today(), finished_on=date.today())
    adult_store = w.listening_history_store
    result = stats(w, app)
    assert result.average_rating == 4.72 and result.unique_books == 1
    assert result.collection.books == 1
    assert all(row.path == str(path) for row in result.listens)
    assert all(row.path == str(path) for row in result.collection_books)
    assert w.statistics_library_switcher.currentData() == adult
    assert main_store.statistics_snapshot() == main_store_snapshot
    switch(w, 'main', app)
    assert w.library_switcher.currentText() == 'Main'
    assert set(w._track_records) == {str(p) for p in paths}
    assert stats(w, app).average_rating == 3.61
    assert w.statistics_page.result.collection.books == 3
    open_book(w, paths[0], app)
    assert w._book_rating == 3.61 and w._book_favorite
    assert adult_store.book_preferences(main_id) == (4.72, False)
    assert not messages


def test_unsaved_rating_stays_in_editor_when_switch_is_requested(window, library, tmp_path, app):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    open_book(w, library[1][0], app, edit=True)
    w.ed_rating.setValue(3.61)
    assert not w.switch_library(adult)
    assert w._book_editing and w.ed_rating.value() == 3.61
    assert w.library_switcher.currentData() == 'main'
    assert w.library_settings_store.active_library_id() == 'main'
    w.cancel_book_editing()
    wait_until(app, lambda: w._file_job is None)
    switch(w, adult, app)
    open_book(w, path, app)
    assert w._book_rating == 0 and not messages


def test_scan_is_cancelled_before_switch_and_cannot_publish_old_books(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = ui.read_meta_and_length
    w.scan_index_store._entries.clear()
    def delayed(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(ui, 'read_meta_and_length', delayed)
    w.start_scan()
    wait_until(app, entered.is_set)
    try:
        assert w.switch_library(adult)
        assert w._pending_library_change is not None
        assert w.library_settings_store.active_library_id() == 'main'
    finally:
        gate.set()
    wait_until(app, lambda: w._pending_library_change is None and w._scan_thread is None and w._file_job is None)
    assert w.library_settings_store.active_library_id() == adult
    assert set(w._track_records) == {str(path)}
    assert not messages


def test_close_during_pending_switch_does_not_activate_another_library(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    adult, _ = other_library(w, library, tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = ui.read_meta_and_length
    w.scan_index_store._entries.clear()
    def delayed(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(ui, 'read_meta_and_length', delayed)
    w.start_scan()
    wait_until(app, entered.is_set)
    try:
        assert w.switch_library(adult)
        w.close()
        assert w._closing and w._pending_library_change is None
    finally:
        gate.set()
    wait_until(app, lambda: w._scan_thread is None and w._file_job is None)
    assert LibrarySettingsStore().active_library_id() == 'main'
    assert not messages


def test_inflight_statistics_are_discarded_before_new_library_opens(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = statistics_data.build_statistics
    def delayed(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(statistics_data, 'build_statistics', delayed)
    w.show_statistics()
    wait_until(app, entered.is_set)
    try:
        assert w.switch_library(adult)
        assert w._pending_library_change is not None
    finally:
        gate.set()
    wait_until(app, lambda: w._pending_library_change is None and w._file_job is None and w._scan_thread is None)
    assert w.statistics_page is None
    assert stats(w, app).collection.books == 1
    assert all(row.path == str(path) for row in w.statistics_page.result.collection_books)
    assert not messages


def test_inflight_thumbnail_finishes_before_old_gallery_is_disposed(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = statistics_artwork.read_thumbnail
    def delayed(*args):
        entered.set()
        assert gate.wait(5)
        return original(*args)
    monkeypatch.setattr(statistics_artwork, 'read_thumbnail', delayed)
    stats(w, app)
    w.statistics_page.artwork.icon(str(library[1][0]), 'test')
    wait_until(app, entered.is_set)
    try:
        assert w.switch_library(adult)
        assert w._pending_library_change is not None
    finally:
        gate.set()
    wait_until(app, lambda: w._pending_library_change is None and w._file_job is None and w._scan_thread is None)
    assert w.statistics_page is None
    assert set(w._track_records) == {str(path)}
    assert not messages


def test_saving_cannot_switch_database_under_the_worker(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = ui._save_book_data
    def delayed(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(ui, '_save_book_data', delayed)
    open_book(w, library[1][0], app, edit=True)
    w.ed_rating.setValue(3.61)
    w.save_metadata_for_selected()
    wait_until(app, entered.is_set)
    try:
        assert not w.switch_library(adult)
        assert w.library_settings_store.active_library_id() == 'main'
    finally:
        gate.set()
    wait_until(app, lambda: w._file_job is None)
    assert w._book_rating == 3.61
    switch(w, adult, app)
    open_book(w, path, app)
    assert w._book_rating == 0 and not messages


def test_old_book_click_cannot_register_in_new_library(window, library, tmp_path, app):
    w, _ = window
    adult, _ = other_library(w, library, tmp_path)
    switch(w, adult, app)
    before = w.listening_history_store.statistics_snapshot()
    w.open_book_details(library[1][0])
    app.processEvents()
    assert w._file_job is None and w._selected_track_path is None
    assert w.listening_history_store.statistics_snapshot() == before


def test_settings_cancel_keeps_folders_preferences_and_files(window, library, tmp_path, app, monkeypatch):
    w, _ = window
    settings = w.library_settings_store
    before = settings.file_path().read_bytes()
    folder = tmp_path / 'New adult collection'
    folder.mkdir()
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: ('Adult', True))
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a, **k: str(folder))
    dialog = SettingsDialog(settings, w)
    dialog.new_library.click()
    dialog.btn_add.click()
    dialog.auto_rename.setChecked(False)
    dialog.speed.setValue(1.75)
    values = dialog.values()
    assert len(values['libraries']) == 2
    assert values['libraries'][1]['roots'] == [str(folder)]
    assert values['preferences']['playback_speed'] == 1.75
    dialog.reject()
    assert settings.file_path().read_bytes() == before
    assert all(path.exists() for path in library[1]) and folder.is_dir()
    assert not (settings.profile_data_dir() / 'libraries').exists()


def test_settings_defaults_reach_editor_and_statistics_and_sort_is_per_library(window, library, tmp_path, app):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    w.cmb_artist_sort.setCurrentIndex(w.cmb_artist_sort.findData('rating_high'))
    draft = w.library_settings_store.snapshot()
    draft['preferences'].update(auto_rename=False, rename_sidecar=False,
                                playback_speed=1.75, scan_warning_dialog=False)
    w._queue_library_change(w.library_settings_store.clone(draft))
    wait_until(app, lambda: w._file_job is None)
    assert not w.chk_autorename.isChecked() and not w.chk_renamesidecar.isChecked()
    stats(w, app)
    assert w.statistics_page.speed.value() == 1.75
    switch(w, adult, app)
    assert w.cmb_artist_sort.currentData() == 'az'
    open_book(w, path, app, edit=True)
    w.ed_title.setText('Changed title')
    w.save_metadata_for_selected()
    wait_until(app, lambda: w._file_job is None)
    assert w._selected_track_path == path and path.exists()
    switch(w, 'main', app)
    assert w.cmb_artist_sort.currentData() == 'rating_high'
    assert not messages


def test_new_empty_library_never_falls_back_to_main_folder(window, app):
    w, messages = window
    ident = w.library_settings_store.add_library('Empty')
    switch(w, ident, app)
    assert w.library_roots == [] and not w._track_records and w.artists.count() == 0
    result = stats(w, app)
    assert result.collection.books == 0 and result.unique_books == 0
    assert not messages


def test_rescan_reconnects_unavailable_folder_without_restart(window, library, tmp_path, app):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    switch(w, adult, app)
    open_book(w, path, app)
    store = w.listening_history_store
    store.set_rating(w._book_track_id, 3.61)
    before = store.statistics_snapshot()
    root = w.library_settings_store.library_roots()[0]
    offline = root.with_name('Disconnected drive')
    root.rename(offline)
    try:
        w.start_scan()
        assert w._scan_thread is None and not w._track_records and w.artists.count() == 0
        assert w.library_settings_store.library_roots() == [root]
        assert store.statistics_snapshot() == before
    finally:
        offline.rename(root)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None)
    assert set(w._track_records) == {str(path)}
    assert store.statistics_snapshot() == before
    assert w.library_settings_store.active_library_id() == adult
    assert not messages


def test_failed_settings_write_keeps_active_library_and_catalog(window, library, tmp_path, app, monkeypatch):
    w, messages = window
    adult, _ = other_library(w, library, tmp_path)
    before = w.library_settings_store.file_path().read_bytes()
    def failing(*args):
        raise OSError('Read-only settings folder')
    monkeypatch.setattr(settings_module, '_write_json_atomic', failing)
    assert w.switch_library(adult)
    wait_until(app, lambda: w._file_job is None and w._pending_library_change is None)
    assert w.library_settings_store.active_library_id() == 'main'
    assert w.library_switcher.currentData() == 'main'
    assert w.library_settings_store.file_path().read_bytes() == before
    assert set(w._track_records) == {str(path) for path in library[1]}
    assert messages and 'Read-only' in str(messages)


def test_manual_backup_contains_only_selected_library(window, library, tmp_path, app):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    open_book(w, library[1][0], app)
    w.listening_history_store.set_rating(w._book_track_id, 3.61)
    switch(w, adult, app)
    open_book(w, path, app)
    w.listening_history_store.set_rating(w._book_track_id, 4.72)
    w._library_backup_action('backup')
    wait_until(app, lambda: w._file_job is None)
    backups = list(w.listening_history_store.backup_dir().glob('*manual.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute('SELECT title, rating FROM books').fetchall() == [('Adult Book', 4.72)]
    assert messages[-1][0] == 'Backup created'


def test_startup_choice_and_last_used_survive_restart(window, library, tmp_path, app):
    w, messages = window
    adult, path = other_library(w, library, tmp_path)
    switch(w, adult, app)
    # The default startup library is Main, even after visiting Adult.
    w.close()
    restarted = ui.MainWindow()
    restarted.show()
    try:
        wait_until(app, lambda: restarted._file_job is None and restarted._scan_thread is None and restarted._cache_ready)
        assert restarted.library_settings_store.active_library_id() == 'main'
        draft = restarted.library_settings_store.snapshot()
        draft['startup_library'] = 'last'
        draft['active_library'] = adult
        restarted.library_settings_store.clone(draft).save()
    finally:
        restarted.close()
        app.processEvents()
    last = ui.MainWindow()
    last.show()
    try:
        wait_until(app, lambda: last._file_job is None and last._scan_thread is None and last._cache_ready)
        assert last.library_settings_store.active_library_id() == adult
        assert set(last._track_records) == {str(path)}
    finally:
        last.close()
        app.processEvents()
    assert not messages
