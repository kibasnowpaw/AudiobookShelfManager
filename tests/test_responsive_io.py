"""Regression checks using real MP3s and the offscreen Qt event loop."""
import json
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication, QMessageBox
from mutagen.id3 import ID3

from booklibconnect import ui_mainwindow as ui
from booklibconnect.models import TrackMeta
from booklibconnect.scan_index_store import CachedTrack, ScanIndexStore
from booklibconnect.album_expected_store import AlbumExpectedStore
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.review_store import ReviewStore


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def wait_until(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for the Qt operation')
        time.sleep(.003)
    app.processEvents()


@pytest.fixture
def library(tmp_path, monkeypatch, app):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'config'))
    monkeypatch.setenv('BOOKLIBCONNECT_DATA_DIR', str(tmp_path/'history'))
    root = tmp_path/'Audiobooks'
    album = root/'Example Author'/'Example Series'
    album.mkdir(parents=True)
    cover = tmp_path/'chosen.png'
    image = QImage(200,200,QImage.Format_RGB32)
    image.fill(QColor('#287fb5'))
    assert image.save(str(cover))
    base = tmp_path/'base.mp3'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=44100:cl=mono',
                    '-t','0.6','-c:a','libmp3lame',str(base)], check=True)
    paths=[]
    for index in range(1,4):
        path=album/f'{index:02} - Book {index} - Example Author.mp3'
        shutil.copyfile(base,path)
        meta=TrackMeta(title=f'Book {index}',artist='Example Author',album='Example Series',
                       genre='Fantasy;LitRPG;Adventure;Progression;Magic;Action;GameLit;Epic;Humor;Fiction',
                       narrator='A Narrator',trackno=index,totaltracks=3)
        ui.write_metadata_to_file(path,meta,cover_path=cover)
        paths.append(path)
    return root, paths, cover


def scan(root, index=None):
    index = index or ScanIndexStore()
    results=[]
    worker=ui.ScanWorker([root],AlbumExpectedStore(),index,{})
    worker.finished.connect(results.append)
    worker.run()
    assert results and not results[0]['error'], results
    return index,results[0]


@pytest.fixture
def window(app, library, monkeypatch):
    root, paths, cover = library
    messages=[]
    for kind in ['warning','critical','information']:
        monkeypatch.setattr(QMessageBox,kind,lambda *args: messages.append(args[1:]))
    monkeypatch.setattr(ui.MainWindow,'_show_scan_file_warnings',lambda *args: None)
    monkeypatch.setattr(ui.MainWindow,'_default_library_root',lambda self: root)
    w=ui.MainWindow()
    w.show()
    wait_until(app,lambda: w._file_job is None and w._scan_thread is None and w._cache_ready)
    w._cancel_artwork_updates()
    w.current_artist_key=next(iter(w._artist_groups_cache))
    w.current_album_dir=paths[0].parent
    w.load_albums(w.current_artist_key)
    w.load_tracks(w.current_album_dir)
    w._cancel_artwork_updates()
    yield w,messages
    wait_until(app,lambda: w._file_job is None and w._scan_thread is None)
    w.close()
    app.processEvents()


def open_book(w, path, app, edit=False):
    w.open_book_details(path,edit=edit)
    wait_until(app,lambda:w._file_job is None)
    assert w._selected_track_path==path
    assert w._book_track_id


def test_unchanged_scan_reuses_cover_validation(library,monkeypatch):
    root, paths, cover=library
    index, first=scan(root)
    assert first['refreshed']==3
    assert all(r.cover_checked for r in first['track_records'].values())
    reloaded=ScanIndexStore()
    def forbidden(*args):
        raise AssertionError('Unchanged MP3 cover was read again')
    monkeypatch.setattr(ui,'get_embedded_cover_bytes',forbidden)
    _, second=scan(root,reloaded)
    assert second['reused']==3
    assert second['cover_issues']==[]
    assert all(r.cover_checked for r in second['track_records'].values())


def test_cover_edit_invalidates_persistent_result(library):
    root, paths, cover=library
    side=paths[0].with_suffix('.png')
    shutil.copyfile(cover,side)
    _, first=scan(root)
    record=first['track_records'][str(paths[0])]
    assert ui.find_track_sidecar_cover(paths[0])==side
    image=QImage(200,200,QImage.Format_RGB32)
    image.fill(QColor('#bf3232'))
    assert image.save(str(side))
    new=ui._validated_cover_record(record,paths[0],record)
    assert new.cover_signature!=record.cover_signature
    assert new.cover_issue is not None
    assert 'match' in new.cover_issue['reason']


def test_old_index_keeps_metadata(library):
    root,paths,cover=library
    index,first=scan(root)
    content=json.loads(index.file_path().read_text())
    content['version']=3
    for r in content['tracks'].values():
        for key in ['cover_checked','cover_signature','cover_issue']:
            r.pop(key,None)
    index.file_path().write_text(json.dumps(content))
    old=ScanIndexStore()
    assert len(old.entries())==3
    assert not next(iter(old.entries().values())).cover_checked
    _,upgraded=scan(root,old)
    assert upgraded['reused']==3
    assert all(r.cover_checked for r in upgraded['track_records'].values())


def test_artist_warning_paint_does_not_read_audio(window,library,monkeypatch):
    w,messages=window
    _,paths,_=library
    w._embedded_cache.clear()
    w._embedded_cache_bytes=0
    monkeypatch.setattr(ui,'get_embedded_cover_bytes',lambda *args:pytest.fail('Painting reread MP3s'))
    assert w._artist_metadata_error_count(w.current_artist_key)==0
    # A cached Genre or Album error still propagates through the hierarchy.
    record=w._track_records[str(paths[0])]
    w._track_records[str(paths[0])]=replace(record,metadata=replace(record.metadata,genre=''))
    assert w._artist_metadata_error_count(w.current_artist_key)==1


def test_open_runs_off_ui_thread_and_keeps_timer_alive(window,library,app,monkeypatch):
    w,messages=window
    _,paths,_=library
    original=ui._load_book_data
    ticks=[]
    timer=QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda:ticks.append(time.monotonic()))
    timer.start()
    def slow(*args):
        assert QThread.currentThread()!=app.thread()
        time.sleep(.25)
        return original(*args)
    monkeypatch.setattr(ui,'_load_book_data',slow)
    before=time.monotonic()
    w.open_book_details(paths[0])
    assert time.monotonic()-before<.15
    assert w._busy_dialog.isVisible()
    wait_until(app,lambda:w._file_job is None)
    timer.stop()
    assert len(ticks)>=10
    assert w._busy_dialog is None
    assert w.book_detail_title_label.text()=='Book 1'
    assert w.book_detail_narrator_label.text()=='A Narrator'
    assert w.book_detail_cover_label.width()==200
    assert not messages


def test_save_dialog_duplicate_guard_and_real_write(window,library,app,monkeypatch):
    w,messages=window
    _,paths,cover=library
    open_book(w,paths[0],app,True)
    w._set_book_rating(4)
    w._set_book_favorite(True)
    w.ed_title.setText('Renamed Book')
    w.chk_autorename.setChecked(True)
    w._pending_cover_path=cover
    w._pending_cover_track_path=paths[0]
    original=ui.write_metadata_to_file
    writes=[]
    def slow(*args,**kwargs):
        assert QThread.currentThread()!=app.thread()
        writes.append(args[0])
        time.sleep(.2)
        return original(*args,**kwargs)
    monkeypatch.setattr(ui,'write_metadata_to_file',slow)
    ticks=[]
    timer=QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda:ticks.append(1))
    timer.start()
    w.save_metadata_for_selected()
    job=w._file_job
    assert 'Saving' in w._busy_dialog.message.text()
    assert w._busy_dialog.isVisible()
    w._busy_dialog.reject()
    assert w._busy_dialog.isVisible()
    w.save_metadata_for_selected()
    assert w._file_job is job
    wait_until(app,lambda:w._file_job is None)
    timer.stop()
    assert len(writes)==1
    assert len(ticks)>=8
    assert w._busy_dialog is None
    assert w._selected_track_path.exists()
    assert w._selected_track_path!=paths[0]
    assert not paths[0].exists()
    assert ID3(w._selected_track_path)['TIT2'].text[0]=='Renamed Book'
    assert w._book_rating==4 and w._book_favorite
    assert w._pending_cover_path is None
    assert not messages,messages


def test_failed_cover_retries_once_keeps_selection_and_no_rename(window,library,app,monkeypatch):
    w,messages=window
    _,paths,cover=library
    open_book(w,paths[0],app,True)
    w.ed_title.setText('Should Not Rename')
    w.chk_autorename.setChecked(True)
    w._pending_cover_path=cover
    w._pending_cover_track_path=paths[0]
    writes=[]
    monkeypatch.setattr(ui,'write_metadata_to_file',lambda *args,**kwargs:writes.append(args[0]))
    monkeypatch.setattr(ui,'_images_visually_match',lambda *args:False)
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    assert len(writes)==2
    assert paths[0].exists()
    assert w._selected_track_path==paths[0]
    assert w._pending_cover_path==cover
    assert w._busy_dialog is None
    assert any('Cover was not saved correctly' in m for m in messages)


def test_retry_success_and_write_exception(window,library,app,monkeypatch):
    w,messages=window
    _,paths,cover=library
    open_book(w,paths[0],app,True)
    w.chk_autorename.setChecked(False)
    w._pending_cover_path=cover
    w._pending_cover_track_path=paths[0]
    original=ui._images_visually_match
    calls=[]
    def match(*args):
        calls.append(1)
        return False if len(calls)==1 else original(*args)
    monkeypatch.setattr(ui,'_images_visually_match',match)
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    assert len(calls)>=2 and w._pending_cover_path is None
    assert not messages
    open_book(w,paths[0],app,True)
    w._pending_cover_path=cover
    w._pending_cover_track_path=paths[0]
    def failed(*args,**kwargs):
        raise PermissionError('Test: file is read-only')
    monkeypatch.setattr(ui,'write_metadata_to_file',failed)
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    assert w._pending_cover_path==cover
    assert w._busy_dialog is None
    assert any('Save failed' in m for m in messages)


def test_close_during_save_waits_for_completion(window,library,app,monkeypatch):
    w,messages=window
    _,paths,_=library
    open_book(w,paths[0],app,True)
    original=ui.write_metadata_to_file
    def slow(*args,**kwargs):
        time.sleep(.15)
        return original(*args,**kwargs)
    monkeypatch.setattr(ui,'write_metadata_to_file',slow)
    w.save_metadata_for_selected()
    job=w._file_job
    w.close()
    assert w.isVisible() and w._closing
    wait_until(app,lambda:w._file_job is None)
    assert not w.isVisible()
    assert not messages


def test_startup_draws_before_slow_index_load(app, library, monkeypatch):
    root, paths, cover = library
    original = ScanIndexStore.__init__
    def slow(store):
        assert QThread.currentThread() != app.thread()
        time.sleep(.3)
        original(store)
    monkeypatch.setattr(ScanIndexStore, '__init__', slow)
    monkeypatch.setattr(ui.MainWindow, '_default_library_root', lambda self: root)
    monkeypatch.setattr(ui.MainWindow, '_show_scan_file_warnings', lambda *args: None)
    ticks=[]
    timer=QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda:ticks.append(1))
    timer.start()
    before=time.monotonic()
    w=ui.MainWindow()
    w.show()
    assert time.monotonic()-before < .2
    wait_until(app,lambda:w._file_job is not None)
    assert w._busy_dialog.isVisible() and w.isVisible()
    wait_until(app,lambda:w._file_job is None and w._scan_thread is None and w._cache_ready)
    timer.stop()
    assert len(ticks)>=15
    assert len(w._track_records)==3
    w.close()


def test_reload_detects_external_metadata_change(window,library,app):
    w,messages=window
    _,paths,_=library
    open_book(w,paths[0],app,True)
    meta, _ = ui.read_meta_and_length(paths[0])
    meta.title='Changed externally'
    ui.write_metadata_to_file(paths[0], meta)
    w._reload_book_from_file()
    wait_until(app,lambda:w._file_job is None)
    assert w.ed_title.text()=='Changed externally'
    assert not messages
