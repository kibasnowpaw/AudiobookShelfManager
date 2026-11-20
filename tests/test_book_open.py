"""Book opens must show live state without repeating a scan or a database write."""
import os
import shutil

import pytest
from mutagen.id3 import ID3
from PySide6.QtGui import QColor, QImage

from booklibconnect import ui_mainwindow as ui
from booklibconnect.listening_history_store import ListeningHistoryStore
from test_responsive_io import app, library, scan, window, open_book, wait_until
from test_m4b_support import make_m4b, metadata


def test_details_read_live_edits_without_writing_or_opening_multiple_connections(library, tmp_path, monkeypatch):
    root, paths, _ = library
    _, scanned = scan(root)
    path = paths[0]
    record = scanned["track_records"][str(path)]
    store = ListeningHistoryStore(tmp_path / "personal", automatic_daily_backup=False)
    book, events, written = store.details_for_file(path, record.metadata, record.length_seconds)
    assert written and not events
    other = ListeningHistoryStore(store.data_dir(), automatic_daily_backup=False)
    other.set_rating(book.track_id, 3.61)
    other.set_favorite(book.track_id, True)
    other.set_reviewed(book.track_id, True)
    other.set_purchase(book.track_id, "2020-02-16", "Bookshop")
    for day in ("2026-01-02", "2026-01-19"):
        other.add_completed_listen(book.track_id, started_on=day, finished_on=day)
    connections = []
    connect = store._connect
    def read_only():
        conn = connect()
        conn.execute("PRAGMA query_only = ON")
        connections.append(conn)
        return conn
    monkeypatch.setattr(store, "_connect", read_only)
    data = ui._load_book_data(path, record, store, lambda _: None)
    assert not data["history_error"] and not data["history_written"] and len(connections) == 1
    assert data["preferences"] == (3.61, True) and data["reviewed"]
    assert data["purchase"] == ("2020-02-16", "Bookshop")
    assert [event.finished_on for event in data["events"]] == ["2026-01-19", "2026-01-02"]
    assert data["listen_count"] == 2 and data["listening_days"] == 1


def test_replacement_and_missing_duration_take_write_path(library, tmp_path):
    root, paths, _ = library
    _, result = scan(root)
    path, replacement = paths[:2]
    first, second = (result["track_records"][str(p)] for p in (path, replacement))
    store = ListeningHistoryStore(tmp_path / "data", automatic_daily_backup=False)
    book, _, _ = store.details_for_file(path, first.metadata, first.length_seconds)
    store.set_rating(book.track_id, 3.61)
    store.set_purchase(book.track_id, "2021-03-10", "Shop")
    event_id = store.add_completed_listen(book.track_id, started_on="2026-01-02", finished_on="2026-01-02")
    with store._transaction() as conn:
        conn.execute("UPDATE listening_events SET duration_seconds=NULL WHERE id=?", (event_id,))
    seen, events, written = store.details_for_file(path, first.metadata, first.length_seconds)
    assert written and seen.track_id == first.track_id and events[0].duration_seconds == first.length_seconds
    # Preserve the old mtime on a different playable file at exactly the same path.
    old_stat = path.stat()
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replacement.replace(path)
    data = ui._load_book_data(path, first, store, lambda _: None)
    assert not data["metadata_cached"] and not data["history_error"] and data["history_written"]
    assert data["track_id"] == second.track_id != first.track_id
    assert data["metadata"].title == second.metadata.title and data["preferences"][0] == 3.61
    assert data["events"][0].event_id == event_id and data["purchase"] == ("2021-03-10", "Shop")
    assert store.book(first.track_id) is None


def paint(path, color):
    image = QImage(160, 160, QImage.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")


@pytest.mark.parametrize("ext", ["mp3", "m4b"])
def test_verified_sidecar_displays_without_parsing_embedded_art_and_changed_cover_is_rechecked(
        library, make_m4b, ext, monkeypatch):
    root, paths, image = library
    path = paths[0] if ext == "mp3" else make_m4b(paths[0].parent / "Chaptered.m4b")
    if ext == "m4b":
        ui.write_metadata_to_file(path, metadata(), cover_path=image)
    sidecar = path.with_suffix(".PnG")
    shutil.copyfile(image, sidecar)
    _, result = scan(root)
    record = result["track_records"][str(path)]
    with monkeypatch.context() as m:
        m.setattr(ui, "get_embedded_cover_bytes", lambda *a, **kw: pytest.fail("read embedded art on verified sidecar"))
        m.setattr(ui, "_image_visual_fingerprint", lambda *_: pytest.fail("compared an unchanged cover"))
        warm = ui._load_book_data(path, record, None, lambda _: None)
    assert warm["cover_cached"] and not warm["embedded_read"]
    assert warm["cover_image"].pixelColor(4, 4).name() == "#287fb5"
    paint(sidecar, "#bf3232")
    changed = ui._load_book_data(path, warm["record"], None, lambda _: None)
    assert not changed["cover_cached"] and changed["embedded_read"] and changed["embedded"]
    assert changed["cover_image"].pixelColor(4, 4).name() == "#bf3232"
    assert changed["record"].cover_checked and "does not match" in changed["record"].cover_issue["reason"]
    # Removing the sidecar switches the preview back to the embedded artwork.
    sidecar.unlink()
    changed = ui._load_book_data(path, changed["record"], None, lambda _: None)
    assert not changed["cover_cached"] and changed["record"].cover_issue is None
    assert changed["cover_image"].pixelColor(4, 4).name() == "#287fb5"


def test_cached_sidecar_does_not_make_embedded_art_disappear(window, library, app):
    w, _ = window
    _, paths, image = library
    path = paths[0]
    shutil.copyfile(image, path.with_suffix(".png"))
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None)
    open_book(w, path, app)
    assert w._embedded_bytes(path) == image.read_bytes()


def test_folder_only_preview_keeps_warning_and_repairs_it_after_embedding(library):
    root, paths, image = library
    path = paths[0]
    tags = ID3(path)
    tags.delall("APIC")
    tags.save(path)
    shutil.copyfile(image, path.parent / "Folder.PNG")
    _, result = scan(root)
    record = result["track_records"][str(path)]
    warm = ui._load_book_data(path, record, None, lambda _: None)
    assert warm["cover_cached"] and not warm["cover_image"].isNull()
    assert "does not contain an embedded cover" in warm["record"].cover_issue["reason"]
    ui.write_metadata_to_file(path, record.metadata, cover_path=image)
    repaired = ui._load_book_data(path, record, None, lambda _: None)
    assert not repaired["cover_cached"] and repaired["record"].cover_issue is None


def test_cover_changed_mid_open_is_reselected_before_caching(library, monkeypatch):
    root, paths, image = library
    path = paths[0]
    sidecar = path.with_suffix(".png")
    shutil.copyfile(image, sidecar)
    _, result = scan(root)
    record = result["track_records"][str(path)]
    read = ui._read_cover_file_bytes
    changed = False
    def change_during_read(p):
        nonlocal changed
        data = read(p)
        if p == sidecar and not changed:
            changed = True
            paint(sidecar, "#334455")
        return data
    monkeypatch.setattr(ui, "_read_cover_file_bytes", change_during_read)
    result = ui._load_book_data(path, record, None, lambda _: None)
    assert changed and not result["cover_cached"] and result["record"].cover_checked
    assert "does not match" in result["record"].cover_issue["reason"]
    assert result["cover_image"].pixelColor(3, 3).name() == "#334455"


@pytest.mark.parametrize("ext", ["mp3", "m4b"])
def test_first_embedded_preview_reads_audio_tags_once_and_uses_real_portable_values(
        library, make_m4b, ext, tmp_path, monkeypatch):
    from booklibconnect import portable_track_state as portable
    root, paths, image = library
    path = paths[0] if ext == "mp3" else make_m4b(paths[0].parent / "No sidecar.m4b")
    if ext == "m4b":
        ui.write_metadata_to_file(path, metadata(), cover_path=image)
    assert portable.write_bool_flag(path, portable.APP_GOODREADS, True)
    assert portable.write_bool_flag(path, portable.APP_AUDIBLE, False)
    _, result = scan(root)
    record = result["track_records"][str(path)]
    store = ListeningHistoryStore(tmp_path / "data", automatic_daily_backup=False)
    portable._invalidate_cache(path)  # First visit after restarting the app.
    reads = []
    load = portable._load_state_tags
    def record_read(p):
        reads.append(p)
        return load(p)
    monkeypatch.setattr(portable, "_load_state_tags", record_read)
    data = ui._load_book_data(path, record, store, lambda _: None)
    assert reads == [path] and data["track_id"] == record.track_id and not data["history_error"]
    assert not data["cover_image"].isNull() and data["embedded"] == image.read_bytes()
    assert data["record"].goodreads and not data["record"].audible


def test_replacement_during_open_does_not_register_the_old_title(library, tmp_path, monkeypatch):
    root, paths, _ = library
    _, scanned = scan(root)
    path, replacement = paths[:2]
    original, incoming = (scanned["track_records"][str(p)] for p in (path, replacement))
    store = ListeningHistoryStore(tmp_path / "state", automatic_daily_backup=False)
    read = ui._read_book_cover
    seen = []
    def swap_after_read(p, record):
        result = read(p, record)
        seen.append(record)
        if len(seen) == 1:
            replacement.replace(path)
        return result
    monkeypatch.setattr(ui, "_read_book_cover", swap_after_read)
    result = ui._load_book_data(path, original, store, lambda _: None)
    assert seen == [original, None] and not result["metadata_cached"]
    assert result["track_id"] == incoming.track_id and result["metadata"].title == incoming.metadata.title
    assert store.book(incoming.track_id).title == incoming.metadata.title and store.book(original.track_id) is None
