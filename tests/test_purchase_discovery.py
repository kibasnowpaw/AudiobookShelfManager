"""Purchase retention and failure handling, publication years and library discovery."""
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import sqlite3

import pytest
from PySide6.QtCore import Qt, QDate, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from booklibconnect.purchase_info import validate_purchase, owned_for, release_year
from booklibconnect.listening_history_store import ListeningHistoryStore
from booklibconnect.statistics_data import build_statistics, scan_snapshot, filter_collection
from booklibconnect.statistics_page import StatisticsPage
from booklibconnect.genre_filter_dialog import GenreFilterDialog
from booklibconnect import ui_mainwindow as ui
from test_responsive_io import app, library, window, open_book, wait_until
from test_library_profiles import other_library, switch, stats
from test_statistics import snapshot
from test_statistics_explorer import enriched


@pytest.mark.parametrize('text,expected', [('1984',1984),('2020-02',2020),('2020-02-29',2020),
    ('0001',1),(2026,2026),('',None),('Unknown',None),('2020/2021',None),('2021-02-29',None),
    ('0',None),('0000',None),('release 1999',None),('2019-13',None)])
def test_release_year_parsing_does_not_guess(text, expected):
    assert release_year(text) == expected


@pytest.mark.parametrize('purchased,today,expected', [
    ('',date(2026,9,30),'Not recorded'),
    ('2026-09-30',date(2026,9,30),'0 days (bought today)'),
    ('2026-09-29',date(2026,9,30),'1 day'),
    ('2024-02-29',date(2025,2,28),'1 year (365 days)'),
    ('2026-01-31',date(2026,2,28),'1 month (28 days)'),
    ('2017-09-30',date(2026,9,30),'9 years (3,287 days)'),
])
def test_owned_for_calendar_age(purchased, today, expected):
    assert owned_for(purchased, today=today) == expected


def test_optional_purchase_validation():
    assert validate_purchase('', ' Audible ') == ('','Audible')
    assert validate_purchase('2020-02-29','') == ('2020-02-29','')
    for invalid in ('2021-02-29', '20260930', '30/09/2026', (date.today()+timedelta(days=1)).isoformat()):
        with pytest.raises(ValueError):
            validate_purchase(invalid,'Author website')
    with pytest.raises(ValueError):
        validate_purchase('', 'x'*201)


def test_v4_migration_preserves_all_state_and_original_backup(tmp_path):
    store = ListeningHistoryStore(data_directory=tmp_path)
    store.upsert_book(track_id='book',last_path='/books/book.mp3',title='Book')
    store.set_rating('book',3.61)
    store.set_favorite('book',True)
    store.set_reviewed('book',True)
    store.add_completed_listen('book',started_on='2025-01-01',finished_on='2025-01-02')
    with sqlite3.connect(store.database_path()) as db:
        db.execute('ALTER TABLE books DROP COLUMN purchased_on')
        db.execute('ALTER TABLE books DROP COLUMN purchase_source')
        db.execute('PRAGMA user_version=4')
    upgraded = ListeningHistoryStore(data_directory=tmp_path)
    book = upgraded.book('book')
    assert book.rating == 3.61 and book.favorite and book.reviewed
    assert (book.purchased_on, book.purchase_source) == ('','')
    assert len(upgraded.listens_for_book('book')) == 1
    backups = list(upgraded.backup_dir().glob('*before-v4-to-v5.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert db.execute('SELECT rating,favorite,reviewed FROM books').fetchone()==(3.61,1,1)
        assert 'purchased_on' not in [row[1] for row in db.execute('PRAGMA table_info(books)')]


def test_purchase_follows_replacement_identity_and_rename_without_mixing_pair(tmp_path):
    store = ListeningHistoryStore(data_directory=tmp_path)
    store.upsert_book(track_id='a',last_path='/books/a.mp3',title='Old')
    store.set_purchase('a','2017-02-16','Audible')
    store.upsert_book(track_id='b',last_path='/books/a.mp3',title='New copy')
    assert store.book('a') is None
    assert store.book('b').purchase_source=='Audible'
    assert store.book('b').purchased_on=='2017-02-16'
    store.upsert_book(track_id='b',last_path='/books/renamed.mp3',title='Renamed')
    assert store.book('b').purchased_on=='2017-02-16'
    store.upsert_book(track_id='c',last_path='/books/other.mp3',title='Existing destination')
    store.set_purchase('c','', 'Author website')
    store.upsert_book(track_id='c',last_path='/books/renamed.mp3',title='Replacement with own purchase')
    assert store.book('c').purchase_source=='Author website' and not store.book('c').purchased_on
    assert store.book('b') is None


def enter_purchase(w, purchased='2017-02-16', source='Audible'):
    w.purchase_date_known.setChecked(bool(purchased))
    if purchased:
        w.ed_purchased_on.setDate(QDate.fromString(purchased,'yyyy-MM-dd'))
    w.ed_purchase_source.setText(source)


def test_real_edit_save_display_rename_clear_and_library_isolation(window, library, tmp_path, app):
    w,messages=window
    _,paths,_=library
    adult,adult_path=other_library(w,library,tmp_path)
    open_book(w,paths[0],app)
    ident=w._book_track_id
    w.show_book_editor()
    assert not w.purchase_date_known.isChecked() and not w.ed_purchase_source.text()
    enter_purchase(w)
    w.ed_title.setText('Book With Purchase')
    w.ed_rating.setValue(3.61)
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    renamed=w._selected_track_path
    assert renamed!=paths[0] and renamed.exists()
    assert w.book_detail_purchased_label.text()=='2017-02-16'
    assert w.book_detail_source_label.text()=='Audible'
    assert 'years' in w.book_detail_owned_for_label.text()
    assert w._book_rating==3.61
    w.show_book_editor()
    enter_purchase(w,'2020-01-01','Cancelled shop')
    w.cancel_book_editing()
    wait_until(app,lambda:w._file_job is None)
    assert w.listening_history_store.book(ident).purchase_source=='Audible'
    switch(w,adult,app)
    open_book(w,adult_path,app)
    assert w._book_purchase==('','')
    switch(w,'main',app)
    open_book(w,renamed,app)
    w.show_book_editor()
    assert w.ed_purchase_source.text()=='Audible'
    w.purchase_clear.click()
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    assert w._book_purchase==('','')
    assert w.book_detail_owned_for_label.text()=='Not recorded'
    assert not messages


def test_source_without_date_never_saves_today(window, library, app):
    w,messages=window
    _,paths,_=library
    open_book(w,paths[0],app)
    w.show_book_editor()
    enter_purchase(w,'','Bookshop')
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    assert w._book_purchase==('','Bookshop')
    assert w.book_detail_owned_for_label.text()=='Not recorded'
    assert not messages


@pytest.mark.parametrize('failure',['mp3','purchase','reload'])
def test_failed_save_retains_purchase_inputs_for_retry(window, library, app, monkeypatch, failure):
    w,messages=window
    _,paths,_=library
    open_book(w,paths[0],app)
    ident=w._book_track_id
    w.show_book_editor()
    enter_purchase(w,'2020-01-02','Independent shop')
    w.ed_title.setText('Rename During Save')
    w.ed_rating.setValue(4.23)
    def fail(*args,**kwargs):
        assert QThread.currentThread()!=app.thread()
        raise OSError('Simulated '+failure+' failure')
    with monkeypatch.context() as patch:
        if failure=='mp3':patch.setattr(ui,'write_metadata_to_file',fail)
        elif failure=='purchase':patch.setattr(w.listening_history_store,'set_purchase',fail)
        else:patch.setattr(ui,'_load_book_data',fail)
        w.save_metadata_for_selected()
        wait_until(app,lambda:w._file_job is None)
    assert w._book_editing and w._selected_track_path.exists()
    assert w.ed_purchase_source.text()=='Independent shop'
    assert w.ed_purchased_on.date()==QDate(2020,1,2) and w.purchase_date_known.isChecked()
    assert not w.listening_history_store.book(ident).purchase_source
    assert messages
    messages.clear()
    w.save_metadata_for_selected()
    wait_until(app,lambda:w._file_job is None)
    assert w._book_purchase==('2020-01-02','Independent shop')
    assert w._book_rating==4.23 and not w._book_editing and not messages


def discovery_data(snapshot):
    collection=enriched(snapshot)
    collection['tracks'][0]['release_year']=2018
    collection['tracks'][1]['release_year']=2020
    collection['tracks'][2]['release_year']=2020
    snapshot['books'][0].update(purchased_on='2017-02-16',purchase_source='Audible')
    snapshot['books'][1].update(purchased_on='2024-03-01',purchase_source='audible')
    return build_statistics(snapshot,2026,collection)


def test_release_year_totals_use_owned_books_not_listens(snapshot):
    result=discovery_data(snapshot)
    assert {entry.name:entry.books for entry in result.release_years}=={'2018':1,'2020':2}
    assert sum(entry.books for entry in result.release_years)==result.collection.books==3
    assert len(result.listens)==4  # Different population, including a removed file.
    missing=enriched(snapshot)
    missing['tracks'][0]['release_year']=1984
    unavailable=build_statistics(snapshot,2025,missing,files={'/library/b.mp3':'File not found'})
    assert {entry.name:entry.books for entry in unavailable.release_years}=={'1984':1,'Unknown':1}
    assert sum(entry.books for entry in unavailable.release_years)==2


def test_filters_intersect_and_include_unfinished_books(snapshot):
    result=discovery_data(snapshot)
    assert [x.track_id for x in filter_collection(result,genres=['Fantasy','LITRPG'])]==['a']
    assert [x.track_id for x in filter_collection(result,genres=['Fantasy','Adventure'],genre_match='any')]==['a','b']
    assert [x.track_id for x in filter_collection(result,genres=[''])]==['d']
    assert [x.track_id for x in filter_collection(result,query='voice b',source='AUDIBLE',year=2020)]==['b']
    assert [x.track_id for x in filter_collection(result,mode='unfinished',year=2020)]==['d']
    assert [x.track_id for x in filter_collection(result,mode='missing_purchase_date')]==['d']
    assert [x.track_id for x in filter_collection(result,source='')]==['d']
    assert not filter_collection(result,year=0)


def test_native_filter_selection_year_drilldown_sort_and_open(snapshot, app):
    page=StatisticsPage()
    page.resize(1000,780)
    result=discovery_data(snapshot)
    page.show_result(result)
    page.show()
    page.show_collection_browser()
    app.processEvents()
    assert page.notice.isHidden()
    page.collection_genres=('Adventure',)
    page.collection_source.setCurrentIndex(page.collection_source.findData('Audible'))
    page.collection_search.setText('Book B')
    assert [book.track_id for book in page.collection_model.rows]==['b']
    page._clear_collection()
    page._choose_release_year(1)
    assert {book.track_id for book in page.collection_model.rows}=={'b','d'}
    assert page.collection_year.currentData()==2020
    page._clear_collection()
    page.collection_oldest.click()
    values=[page.collection_sort.index(i,8).data() for i in range(3)]
    assert values==['2017-02-16','2024-03-01','Not recorded']
    opened=[]
    page.open_book_requested.connect(opened.append)
    page._open_collection_book(page.collection_sort.index(0,0))
    assert opened==['/library/a.mp3']
    page.collection_year.setCurrentIndex(page.collection_year.findData(2018))
    page.show_result(result)
    assert len(page.collection_model.rows)==1  # Refresh keeps the selected year.
    assert not page.period.isEnabled()
    page.close()


def test_genre_dialog_search_and_cancel_preserve_original_filter(app):
    selected=('Fantasy',)
    dialog=GenreFilterDialog([('',1),('Fantasy',2),('LitRPG',3)],selected)
    dialog.show()
    dialog.tags.item(2).setCheckState(Qt.Checked)
    dialog.search.setText('litrpg')
    assert dialog.tags.item(1).isHidden() and not dialog.tags.item(2).isHidden()
    assert dialog.selected_genres()==('Fantasy','LitRPG')
    dialog.reject()
    assert selected==('Fantasy',) and dialog.result()==QDialog.Rejected


def test_find_books_shortcut_and_year_tag_changes_use_cached_metadata(window, library, app, monkeypatch):
    w,messages=window
    _,paths,_=library
    record=w._track_records[str(paths[0])]
    w._track_records[str(paths[0])]=replace(record,metadata=replace(record.metadata,year='2018-02-16'))
    def forbidden(*args,**kwargs):raise AssertionError('No MP3 reads for year statistics')
    monkeypatch.setattr(ui,'read_meta_and_length',forbidden)
    w.act_find_books.trigger()
    wait_until(app,lambda:w._statistics_job is None)
    page=w.statistics_page
    assert page.tabs.currentIndex()==2
    assert {entry.name:entry.books for entry in page.result.release_years}=={'2018':1,'Unknown':2}
    page.collection_genres=('fantasy',)
    page._filter_collection()
    assert len(page.collection_model.rows)==3
    assert not messages
