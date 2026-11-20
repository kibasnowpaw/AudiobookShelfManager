"""New statistics: multi-tag counts, series uncertainty, drill-down and cover IO."""
from dataclasses import replace
from datetime import date
from pathlib import Path
import time

import pytest
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest

from booklibconnect.statistics_data import build_statistics, scan_snapshot, filter_listens
from booklibconnect.statistics_page import StatisticsPage
from booklibconnect import statistics_artwork
from test_statistics import snapshot
from test_responsive_io import app, library, window, wait_until, open_book


def enriched(snapshot):
    return dict(paths=['/library/a.mp3','/library/b.mp3','/library/d.mp3'], series=2, authors=2,
        expected={'/library/series-one':3}, tracks=[
        dict(path='/library/a.mp3', title='Book A', author='Author A; Author B', authors=('Author A','Author B'),
             narrator='Voice A', genre='Fantasy; LitRPG; fantasy', series='Series One', series_path='/library/series-one',
             trackno=1, totaltracks=3, seconds=777777, issues=(), artwork_key='a1'),
        dict(path='/library/b.mp3', title='Book B', author='Author A', authors=('Author A',), narrator='Voice B',
             genre='LitRPG | Adventure', series='Series One', series_path='/library/series-one', trackno=3, totaltracks=3, issues=('Cover problem',)),
        dict(path='/library/d.mp3', title='Unread Book', author='Author B', genre='', series='Series Two',
             series_path='/library/series-two', trackno=1, totaltracks=0, issues=('Missing genre','Missing narrator'))])


def test_genres_credits_and_filters_do_not_duplicate_books(snapshot):
    result=build_statistics(snapshot,2026,enriched(snapshot))
    assert {x.name:x.books for x in result.genres} == {'Fantasy':1,'LitRPG':2,'Adventure':1,'':1}
    assert result.unique_books == 3 and len(result.listens) == 4
    assert next(p for p in result.authors if p.name=='Author B').books == 1
    assert len(filter_listens(result,genre='litrpg')) == 3
    assert len(filter_listens(result,genre='litrpg',distinct=True)) == 2
    assert [row.track_id for row in filter_listens(result,genre='Fantasy',author='Author B',period=1,distinct=True)] == ['a']
    assert [row.track_id for row in filter_listens(result,genre='',rating=0)] == ['c']
    assert not filter_listens(result,genre='Adventure',narrator='Voice A')
    assert result.audio_seconds == 26*3600  # Current scan duration never replaces event snapshots.


def test_series_gaps_ratings_and_current_collection(snapshot):
    result=build_statistics(snapshot,2025,enriched(snapshot))
    series=result.series[0]
    assert (series.owned,series.completed,series.expected,series.missing)==(2,2,3,1)
    assert series.missing_numbers == (2,)
    assert series.average_rating == 4 and series.rated == 2
    assert not series.ownership_complete and not series.listening_complete
    assert result.collection.completed == 2  # Lifetime completion regardless of year.
    assert result.collection.missing == 1 and result.collection.unknown_totals == 1
    assert result.collection.attention == 2
    assert {x.name:x.books for x in result.issues} == {'Cover problem':1,'Missing genre':1,'Missing narrator':1}
    assert not next(b for b in result.collection_books if b.track_id=='d').completed


@pytest.mark.parametrize('change', ['duplicate','out_of_range','conflicting_totals','no_numbers'])
def test_series_never_claims_exact_gaps_from_bad_numbers(snapshot,change):
    collection=enriched(snapshot)
    first,second=collection['tracks'][:2]
    if change=='duplicate':
        second['trackno']=1
    elif change=='out_of_range':
        second['trackno']=4
    elif change=='conflicting_totals':
        collection['expected']={}
        second['totaltracks']=4
    else:
        first['trackno']=second['trackno']=0
    result=build_statistics(snapshot,2026,collection)
    series=result.series[0]
    assert series.missing_numbers is None
    assert not series.ownership_complete
    if change=='no_numbers':
        assert series.missing == 1
    else:
        assert series.missing is None


def test_native_chart_clicks_filters_and_sorted_book_activation(snapshot,app):
    page=StatisticsPage()
    page.resize(1080,850)
    page.show_result(build_statistics(snapshot,2026,enriched(snapshot)))
    page.show()
    app.processEvents()
    region=page.timeline._regions[0][0]
    QTest.mouseClick(page.timeline,Qt.LeftButton,pos=region.center().toPoint())
    assert page.tabs.currentIndex()==1 and len(page.history_model.rows)==2
    page._choose_profile('LitRPG')
    assert page.history_mode.currentData() is True and len(page.history_model.rows)==2
    page.history_filters['author'].setCurrentIndex(page.history_filters['author'].findData('Author B'))
    assert [row.track_id for row in page.history_model.rows]==['a']
    page._choose_rating(4)
    assert [row.track_id for row in page.history_model.rows]==['a']
    page._choose_rating(5)
    assert [row.track_id for row in page.history_model.rows]==['c']
    page._clear_history_filters()
    page.history_sort.sort(2,Qt.AscendingOrder)
    opened=[]
    page.open_book_requested.connect(opened.append)
    page._open_history_book(page.history_sort.index(0,2))
    assert opened==['/library/a.mp3']
    page.speed.setValue(2)
    assert page.estimated_time.text().startswith('13 h 00 m') and '1 unknown' in page.estimated_time.text()
    page.tabs.setCurrentIndex(2)
    assert not page.period.isEnabled()
    page._choose_issue('Missing genre')
    assert [b.track_id for b in page.collection_model.rows]==['d']
    page._clear_collection()
    page.collection_status.setCurrentIndex(page.collection_status.findData('unfinished'))
    assert [b.track_id for b in page.collection_model.rows]==['d']
    page.tabs.setCurrentIndex(3)
    assert page.period.isEnabled()
    assert [r.track_id for r in page.recap_model.rows]==['a']
    page.recap_selector.setCurrentIndex(page.recap_selector.findData('all'))
    assert len(page.recap_model.rows)==3
    page.close()
    wait_until(app,lambda:not page.artwork.busy)


def test_lazy_cover_cache_uses_worker_and_refresh_signature(app,tmp_path,monkeypatch):
    page=StatisticsPage()
    page.show()
    original=statistics_artwork.read_thumbnail
    calls=[]
    def slow(path):
        assert QThread.currentThread()!=app.thread()
        calls.append(path)
        time.sleep(.07)
        image=QImage(200,200,QImage.Format_RGB32)
        image.fill(QColor('blue'))
        return image
    monkeypatch.setattr(statistics_artwork,'read_thumbnail',slow)
    ticks=[]
    timer=QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda:ticks.append(1))
    timer.start()
    assert not page.artwork.busy
    for _ in range(4):
        page.artwork.icon('/example/book.mp3','v1')
    wait_until(app,lambda:('/example/book.mp3','v1') in page.artwork.cache)
    assert len(calls)==1 and len(ticks)>=5
    page.artwork.icon('/example/book.mp3','v1')
    assert len(calls)==1
    page.artwork.icon('/example/book.mp3','v2')
    wait_until(app,lambda:page.artwork.busy)
    page.close()
    assert page.isVisible()  # Closing waits asynchronously for the single active read.
    wait_until(app,lambda:not page.artwork.busy and not page.isVisible())
    timer.stop()
    assert len(calls)==2


def test_real_embedded_cover_is_scaled_without_writing(library):
    root,paths,cover=library
    path=paths[0]
    before=path.read_bytes()
    image=statistics_artwork.read_thumbnail(str(path))
    assert not image.isNull() and image.width()==200 and image.height()==200
    assert before==path.read_bytes()


def test_scan_snapshot_does_no_file_io_and_keeps_unknowns(window,monkeypatch):
    w,messages=window
    key=next(iter(w._track_records))
    record=w._track_records[key]
    records={key:replace(record,metadata=replace(record.metadata,genre='',narrator='',composer=''),cover_checked=False,cover_issue=None)}
    def forbidden(*args,**kwargs):
        raise AssertionError('Snapshot attempted file IO')
    with monkeypatch.context() as patch:
        patch.setattr(Path,'stat',forbidden)
        snapshot=scan_snapshot(records,{},1)
    issues=snapshot['tracks'][0]['issues']
    assert 'Missing genre' in issues and 'Missing narrator' in issues and 'Cover not checked' in issues
    assert not messages


def test_open_from_statistics_returns_to_statistics_and_guards_edits(window,library,app):
    w,messages=window
    root,paths,cover=library
    open_book(w,paths[0],app,True)
    w.ed_title.setText('Keep this unsaved edit')
    w.show_statistics()
    wait_until(app,lambda:w._statistics_job is None)
    w.statistics_page.open_book_requested.emit(str(paths[1]))
    assert w.stack.currentIndex()==w.PAGE_BOOK
    assert w.ed_title.text()=='Keep this unsaved edit' and w._selected_track_path==paths[0]
    w.cancel_book_editing()
    wait_until(app,lambda:w._file_job is None)
    w.show_statistics()
    wait_until(app,lambda:w._statistics_job is None)
    w.statistics_page.open_book_requested.emit(str(paths[1]))
    wait_until(app,lambda:w._file_job is None)
    assert w._selected_track_path==paths[1] and w._book_return_to_statistics
    # An edit begun after a direct Statistics jump must also be protected.
    w.edit_selected_book()
    wait_until(app,lambda:w._file_job is None)
    w.ed_title.setText('Protect an edit opened from Statistics')
    w.show_statistics()
    wait_until(app,lambda:w._statistics_job is None)
    w.statistics_page.open_book_requested.emit(str(paths[2]))
    assert w.stack.currentIndex()==w.PAGE_BOOK and w._selected_track_path==paths[1]
    assert w.ed_title.text()=='Protect an edit opened from Statistics'
    w.cancel_book_editing()
    wait_until(app,lambda:w._file_job is None)
    w.go_back()
    wait_until(app,lambda:w._statistics_job is None)
    assert w.stack.currentIndex()==w.PAGE_STATISTICS
    assert not messages


def test_author_listen_metric_and_future_unknown_chart_labels(snapshot,app):
    page=StatisticsPage()
    page.show_result(build_statistics(snapshot,2026,enriched(snapshot)))
    page.profile_selector.setCurrentIndex(page.profile_selector.findData('author'))
    page.profile_count.setCurrentIndex(page.profile_count.findData('listens'))
    assert next(entry[2] for entry in page.profile_chart.entries if entry[0]=='Author A')==3
    page._choose_profile('Author A')
    assert len(page.history_model.rows)==3 and not page.history_mode.currentData()
    page.profile_selector.setCurrentIndex(page.profile_selector.findData('genre'))
    assert page.profile_count.currentData()=='books' and not page.profile_count.isEnabled()
    only_unknown=dict(books=snapshot['books'],events=[snapshot['events'][-1]])
    page.show_result(build_statistics(only_unknown,2026))
    page.metric_selector.setCurrentIndex(page.metric_selector.findData('hours'))
    assert 1 in page.timeline.unavailable_indices
    assert page.estimated_time.text().startswith('Unknown')
    page.close()


def test_explicitly_missing_current_credits_do_not_restore_stale_saved_names(snapshot):
    collection=enriched(snapshot)
    collection['tracks'][1]['narrator']=''
    collection['tracks'][1]['author']=''
    collection['tracks'][1]['authors']=()
    result=build_statistics(snapshot,2026,collection)
    row=next(row for row in result.listens if row.track_id=='b')
    assert row.narrator=='Unknown narrator' and row.narrators==()
    assert row.author=='Unknown author' and row.authors==()
    assert [row.track_id for row in filter_listens(result,narrator='')]==['b']
    current=next(book for book in result.collection_books if book.track_id=='b')
    assert current.narrator=='Unknown narrator'
