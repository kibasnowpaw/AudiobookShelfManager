"""Real M4B round trips, chapter/audio preservation and native mixed-library UI."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import shutil
import subprocess

import pytest
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from PySide6.QtCore import QDate, QThread
from PySide6.QtGui import QImage, QColor

from booklibconnect import ui_mainwindow as ui, mp4_metadata, portable_track_state as portable
from booklibconnect import book_removal as removal
from booklibconnect.models import TrackMeta
from booklibconnect.metadata_io import read_meta_and_length, write_metadata_to_file, embed_cover_into_file
from booklibconnect.covers import get_embedded_cover_bytes
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.purchase_info import release_year
from booklibconnect.statistics_data import build_statistics, scan_snapshot
from test_responsive_io import app, library, window, scan, wait_until, open_book
from test_library_profiles import switch, stats


@pytest.fixture
def make_m4b(tmp_path):
    chapters=tmp_path/'chapters.ffmeta'
    chapters.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=800\ntitle=First chapter\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=800\nEND=1800\ntitle=Andet kapitel: Æøå\n')
    def create(path=None, *, codec='aac', faststart=True, encrypted=False):
        path=path or tmp_path/'chaptered.m4b'
        path.parent.mkdir(parents=True,exist_ok=True)
        command=['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
                 '-f','ffmetadata','-i',str(chapters),'-t','1.8','-map','0:a:0','-map_metadata','1',
                 '-map_chapters','1','-c:a',codec]
        if faststart: command+=['-movflags','+faststart']
        if encrypted:
            command+=['-encryption_scheme','cenc-aes-ctr','-encryption_key','000102030405060708090a0b0c0d0e0f',
                      '-encryption_kid','0f0e0d0c0b0a09080706050403020100']
        subprocess.run(command+['-f','mp4',str(path)],check=True,capture_output=True)
        return path
    return create


def fingerprint(path):
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_chapters','-of','json',str(path)]))
    chapters=[(row['start_time'],row['end_time'],row['tags']['title']) for row in probe['chapters']]
    audio=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map','0:a:0',
                                   '-c:a','pcm_s16le','-f','hash','-hash','sha256','-'])
    return chapters,audio


def cover(tmp_path, suffix='png'):
    path=tmp_path/('replacement.'+suffix)
    image=QImage(240,240,QImage.Format_RGB32)
    image.fill(QColor('#9c2978'))
    assert image.save(str(path))
    return path


def metadata(number=1):
    return TrackMeta(title='A new book: Æøå',artist='Writer One; Writer Two',album='Example Series',
        albumartist='Writer One; Writer Two',year='2020-02-29',
        genre='Fantasy;LitRPG;Adventure;Progression;Magic;Action;GameLit;Epic;Humor;Fiction',
        narrator='Reader One; Reader Two',translator='Translator Æ',composer='Legacy narrator',
        comment=('A detailed description. '*1200).strip(),trackno=number,totaltracks=4,discno=0)


@pytest.mark.parametrize('codec',['aac','alac'])
@pytest.mark.parametrize('faststart',[False,True])
def test_metadata_ids_and_reviews_preserve_audio_chapters_unknown_tags_and_disc(make_m4b,tmp_path,app,codec,faststart):
    path=make_m4b(codec=codec,faststart=faststart)
    original=fingerprint(path)
    assert len(original[0])==2
    audio=MP4(path)
    audio.tags['disk']=[(3,10)]
    audio.tags['----:com.apple.iTunes:KEEP_ME']=[MP4FreeForm(b'unchanged')]
    audio.tags['\xa9too']=['Original encoder']
    audio.save()
    chosen=cover(tmp_path)
    write_metadata_to_file(path,metadata(),cover_path=chosen)
    ident=portable.ensure_track_id(path)
    assert ident and portable.ensure_track_id(path)==ident
    assert portable.write_bool_flag(path,portable.APP_GOODREADS,True)
    assert portable.write_bool_flag(path,portable.APP_AUDIBLE,False)
    assert portable.read_bool_flag(path,portable.APP_GOODREADS) is True
    assert portable.read_bool_flag(path,portable.APP_AUDIBLE) is False
    result,seconds=read_meta_and_length(path)
    assert result==replace(metadata(),discno=3)
    assert 1.7<seconds<2
    tags=MP4(path).tags
    assert tags['disk']==[(3,10)] and tags['trkn']==[(1,4)]
    assert tags['----:com.apple.iTunes:KEEP_ME'][0]==b'unchanged'
    assert tags['\xa9too']==['Original encoder']
    assert tags['stik']==[2]
    assert get_embedded_cover_bytes(path)==chosen.read_bytes()
    assert fingerprint(path)==original
    # Another metadata-only save must retain IDs, review flags and artwork.
    write_metadata_to_file(path,replace(result,title='Second title'))
    assert portable.ensure_track_id(path)==ident
    assert portable.read_bool_flag(path,portable.APP_GOODREADS) is True
    assert get_embedded_cover_bytes(path)==chosen.read_bytes()
    assert fingerprint(path)==original


@pytest.mark.parametrize('suffix',['png','jpg','webp'])
def test_supported_covers_round_trip_and_verify_without_altering_source(make_m4b,tmp_path,app,suffix):
    path=make_m4b()
    chosen=cover(tmp_path,suffix)
    before=chosen.read_bytes()
    embed_cover_into_file(path,chosen)
    data=get_embedded_cover_bytes(path)
    assert ui._images_visually_match(before,data)
    assert MP4(path).tags['covr'][0].imageformat in (MP4Cover.FORMAT_JPEG,MP4Cover.FORMAT_PNG)
    assert chosen.read_bytes()==before


def test_missing_tags_primary_narrator_fallback_and_clearing(make_m4b):
    path=make_m4b()
    audio=MP4(path)
    audio.delete()
    result,length=read_meta_and_length(path)
    assert result==TrackMeta() and length>0
    assert portable.write_bool_flag(path,portable.APP_AUDIBLE,True)
    ident=portable.ensure_track_id(path)
    audio=MP4(path)
    audio.tags['\xa9wrt']=['Composer fallback']
    audio.tags['----:com.apple.iTunes:narrator']=[MP4FreeForm('Narrator Ø'.encode())]
    audio.tags['\xa9ART']=['Author One','Author Two']
    audio.tags['\xa9gen']=['Fantasy','LitRPG']
    audio.tags['desc']=['Short description']
    audio.save()
    result,_=read_meta_and_length(path)
    assert result.narrator=='Narrator Ø' and result.artist=='Author One; Author Two'
    assert result.genre=='Fantasy; LitRPG' and result.comment=='Short description'
    write_metadata_to_file(path,replace(result,narrator='',genre='',comment='',trackno=0,totaltracks=0))
    cleared,_=read_meta_and_length(path)
    assert cleared.narrator=='Composer fallback' and cleared.genre=='' and cleared.comment==''
    assert not any(k.endswith(':narrator') for k in MP4(path).tags)
    assert portable.ensure_track_id(path)==ident
    assert portable.read_bool_flag(path,portable.APP_AUDIBLE) is True


def test_corrupt_and_encrypted_m4b_are_not_repaired_or_modified(make_m4b,tmp_path):
    corrupt=tmp_path/'corrupt.m4b'
    corrupt.write_bytes(b'not an audio file')
    encrypted=make_m4b(tmp_path/'encrypted.m4b',encrypted=True)
    for path in (corrupt,encrypted):
        before=path.read_bytes()
        with pytest.raises(Exception):read_meta_and_length(path)
        with pytest.raises(Exception):write_metadata_to_file(path,metadata())
        assert portable.ensure_track_id(path) is None
        assert path.read_bytes()==before
    assert not list(tmp_path.glob('*.bak'))


def test_readonly_identity_failure_keeps_audio_untouched(make_m4b,monkeypatch):
    path=make_m4b()
    before=path.read_bytes()
    def denied(*a,**kw):raise PermissionError('Read-only file')
    monkeypatch.setattr(MP4,'save',denied)
    assert portable.ensure_track_id(path) is None
    assert portable.read_bool_flag(path,portable.APP_GOODREADS) is None
    assert portable.write_bool_flag(path,portable.APP_GOODREADS,True) is False
    assert path.read_bytes()==before


def test_mixed_scan_rescan_cache_changes_and_broken_m4b(library,make_m4b,monkeypatch):
    root,paths,_=library
    path=make_m4b(paths[0].parent/'04 - Fourth.M4B')
    write_metadata_to_file(path,metadata(4))
    corrupt=paths[0].parent/'broken.m4b'
    corrupt.write_bytes(b'broken')
    _,first=scan(root)
    assert len(first['track_records'])==4 and first['failed']==1
    assert first['broken_files'][0]['path']==str(corrupt)
    assert first['track_records'][str(path)].track_id
    assert not first['identity_failures']
    with monkeypatch.context() as patch:
        patch.setattr(ui,'read_meta_and_length',lambda *a:pytest.fail('Unchanged audio was reread'))
        corrupt.unlink()
        _,again=scan(root)
    assert again['reused']==4 and again['refreshed']==0
    write_metadata_to_file(path,replace(metadata(4),title='Changed M4B'))
    _,changed=scan(root)
    assert changed['refreshed']==1 and changed['reused']==3
    assert changed['track_records'][str(path)].metadata.title=='Changed M4B'
    path.unlink()
    _,removed=scan(root)
    assert len(removed['track_records'])==3 and removed['removed']==1


def test_native_m4b_save_rename_history_purchase_statistics_and_cover_retry(window,library,make_m4b,tmp_path,app,monkeypatch):
    w,messages=window
    root,paths,_=library
    path=make_m4b(paths[0].parent/'04 - New Book.m4b')
    write_metadata_to_file(path,metadata(4))
    # Rescan through the real interface before opening the new format.
    w.act_rescan.trigger()
    wait_until(app,lambda:w._scan_thread is None and w._cache_ready)
    open_book(w,path,app)
    ident=w._book_track_id
    w.listening_history_store.add_completed_listen(ident,started_on='2026-09-01',finished_on='2026-09-02')
    original=fingerprint(path)
    chosen=cover(tmp_path,'webp')
    w.show_book_editor()
    w.ed_title.setText('Saved M4B book')
    w.ed_rating.setValue(3.61)
    w.purchase_date_known.setChecked(True)
    w.ed_purchased_on.setDate(QDate(2020,2,16))
    w.ed_purchase_source.setText('Author shop')
    w._pending_cover_path=chosen
    w._pending_cover_track_path=path
    calls=[]
    original_write=ui.write_metadata_to_file
    def drop_first_cover(audio,meta,*,cover_path=None):
        assert QThread.currentThread()!=app.thread()
        calls.append(audio)
        return original_write(audio,meta,cover_path=None if len(calls)==1 else cover_path)
    monkeypatch.setattr(ui,'write_metadata_to_file',drop_first_cover)
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    renamed=w._selected_track_path
    assert renamed.exists() and renamed.suffix=='.m4b' and renamed!=path
    assert len(calls)==2 and w._pending_cover_path is None
    assert w._book_rating==3.61 and w._book_purchase==('2020-02-16','Author shop')
    assert len(w.listening_history_store.listens_for_book(ident))==1
    assert portable.ensure_track_id(renamed)==ident and fingerprint(renamed)==original
    w.find_books()
    wait_until(app,lambda:w._statistics_job is None)
    result=w.statistics_page.result
    assert result.collection.books==4
    row=next(book for book in result.collection_books if book.path==str(renamed))
    assert row.rating==3.61 and row.purchased_on=='2020-02-16' and row.release_year==2020
    w.statistics_page.collection_genres=('LitRPG',)
    w.statistics_page._filter_collection()
    assert any(book.path==str(renamed) for book in w.statistics_page.collection_model.rows)
    other_path=tmp_path/'Adult'/'Writer'/'Example Series'/renamed.name
    other_path.parent.mkdir(parents=True)
    shutil.copy2(renamed,other_path)
    other=w.library_settings_store.add_library('Adult',[tmp_path/'Adult'])
    switch(w,other,app)
    open_book(w,other_path,app)
    assert w._book_track_id==ident and w._book_rating==0 and w._book_purchase==('','')
    assert stats(w,app).collection.books==1
    switch(w,'main',app)
    open_book(w,renamed,app)
    assert w._book_rating==3.61 and w._book_purchase==('2020-02-16','Author shop')
    assert not messages


@pytest.mark.parametrize('delete_file',[False,True])
def test_m4b_removal_requires_opt_in_preserves_other_files_and_backups(make_m4b,tmp_path,app,delete_file):
    path=make_m4b()
    store=ListeningHistoryStore(data_directory=tmp_path/'state')
    meta,seconds=read_meta_and_length(path)
    ident=store.register_track(path,meta,seconds)
    store.set_rating(ident,3.61)
    store.set_purchase(ident,'2020-02-16','Shop')
    store.add_completed_listen(ident,started_on='2026-09-01',finished_on='2026-09-02')
    side=path.with_suffix('.jpg')
    side.write_bytes(b'keep sidecar')
    target=removal.prepare_removal(store,SimpleNamespace(track_id=ident,path=str(path),title='Book'))
    dialog=removal.RemoveBookDialog(target)
    assert target.can_delete_file and not dialog.delete_file.isChecked()
    def trash(filename):
        assert delete_file
        destination=tmp_path/'trash.m4b'
        Path(filename).rename(destination)
        return True,str(destination)
    result=removal.remove_book(store,target,delete_file=delete_file,trash=trash)
    assert result['file_deleted']==delete_file and path.exists()!=delete_file
    assert side.read_bytes()==b'keep sidecar' and store.book(ident) is None
    assert Path(result['backup']).exists()
    dialog.close()


@pytest.mark.parametrize('tag,expected', [('2020-02-29T00:00:00Z',2020),
    ('2018-12-31T23:30:00-03:00',2018), ('2021-02-29T00:00:00Z',None),
    ('2020-01-01T25:00:00Z',None)])
def test_m4b_release_timestamps_use_the_recorded_calendar_year(tag,expected):
    assert release_year(tag)==expected


def test_renaming_mixed_formats_retains_shared_cover_and_avoids_destination_artwork(make_m4b,tmp_path,app):
    from booklibconnect.naming import rename_track_file,build_track_filename
    source=make_m4b(tmp_path/'Original.m4b')
    sibling=tmp_path/'Original.mp3'
    sibling.write_bytes(b'other audiobook remains unchanged')
    side=source.with_suffix('.png')
    shutil.copyfile(cover(tmp_path),side)
    image=side.read_bytes()
    requested=metadata()
    conflict=source.with_name(build_track_filename(requested,'.mp3'))
    conflict.write_bytes(b'destination in another format')
    conflict_cover=conflict.with_suffix('.png')
    conflict_cover.write_bytes(b'other cover')
    before=fingerprint(source)
    renamed=rename_track_file(source,requested)
    assert renamed.suffix=='.m4b' and renamed.stem.endswith('(1)')
    assert side.read_bytes()==image and renamed.with_suffix('.png').read_bytes()==image
    assert conflict_cover.read_bytes()==b'other cover' and sibling.read_bytes()==b'other audiobook remains unchanged'
    assert fingerprint(renamed)==before
