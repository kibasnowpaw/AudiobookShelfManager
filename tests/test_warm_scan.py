"""Regressions for cached scans of a large or portal-backed library."""
from pathlib import Path
from shutil import copyfile
import os

import pytest

from booklibconnect import diagnostics, ui_mainwindow as ui
from booklibconnect.covers import CoverDirectory
from booklibconnect.metadata_io import write_metadata_to_file
from booklibconnect.scan_index_store import ScanIndexStore
from test_responsive_io import app, library, scan


def test_existing_index_remains_warm_and_file_replacements_still_refresh(library, monkeypatch):
    root, paths, image = library
    series = paths[0].parent
    folder = series / 'Folder.PNG'
    sidecar = paths[0].with_suffix('.PnG')
    copyfile(image, folder)
    copyfile(image, sidecar)
    index, cold = scan(root)
    # Reconstruct the v5 cover signature used by build diagnostics.1.
    for book in paths:
        sources = (book, series, sidecar if book == paths[0] else None, folder)
        signature = []
        for path in sources:
            if path is not None:
                st = path.stat()
                signature.append((str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns))
            else:
                signature.append(None)
        assert cold['track_records'][str(book)].cover_signature == repr((1, signature))
    prev = index.file_path().stat()
    with monkeypatch.context() as m:
        m.setattr(ui, 'get_embedded_cover_bytes', lambda *a: pytest.fail('Reopened unchanged audio'))
        _, warm = scan(root, ScanIndexStore())
        assert (warm['reused'], warm['refreshed'], warm['failed']) == (3, 0, 0)
    now = index.file_path().stat()
    assert (now.st_ino, now.st_mtime_ns) == (prev.st_ino, prev.st_mtime_ns)

    # Change one audio file outside the app. The now-cached original metadata
    # and cover result must not be applied to the replacement.
    from dataclasses import replace
    book = paths[-1]
    write_metadata_to_file(book, replace(warm['track_records'][str(book)].metadata, title='Changed externally'))
    _, changed = scan(root, ScanIndexStore())
    assert (changed['refreshed'], changed['reused']) == (1, 2)
    assert changed['track_records'][str(book)].metadata.title == 'Changed externally'


def test_stale_cover_name_and_in_place_edit_are_detected(tmp_path):
    series = tmp_path / 'series'
    series.mkdir()
    book = series / 'Book.mp3'
    book.touch()
    first = series / 'Cover.JpG'
    first.write_bytes(b'old')
    index = CoverDirectory(series)
    assert index.discover_tracks({'.mp3'}) == [book]
    original = ui._cover_source_signature(book, index)
    st = first.stat()
    first.write_bytes(b'updated cover')
    os.utime(first, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert ui._cover_source_signature(book, index) != original
    first.unlink()
    replacement = series / 'Front.PNG'
    replacement.write_bytes(b'new')
    # Simulate a filesystem with the same coarse directory timestamp after a rename.
    index._stamp = index._directory_stamp(series.stat())
    _, entries = index.signature_sources(book.stem)
    assert [path for path, _ in entries] == [None, replacement]


def test_changing_audio_during_cover_validation_does_not_seal_old_result(library, monkeypatch):
    root, paths, _ = library
    _, first = scan(root)
    book = paths[0]
    record = first['track_records'][str(book)]
    original = book.stat()
    def change_while_reading(*args):
        os.utime(book, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(ui, '_scan_cover_consistency_issue', change_while_reading)
    new = ui._validated_cover_record(record, book, track_stat=original)
    assert not new.cover_checked and not new.matches_file(book)


def test_kernel_runtime_and_portal_info_are_reported_without_private_strings(tmp_path):
    assert diagnostics._kernel_version('7.0.0-43-generic') == '7.0.0-43-generic'
    assert diagnostics._kernel_version('6.18.4-arch2-1') == '6.18.4-arch2-1'
    assert diagnostics._kernel_version('7.0.0-43-laptop-private') == '7.0.0-43'
    release = tmp_path / 'os-release'
    release.write_text('ID="org.kde.Platform"\nVERSION_ID=6.10\nPRETTY_NAME="private text"\n')
    assert diagnostics._release(release) == 'org.kde.Platform 6.10'
    for value in ('/run/flatpak/doc/PRIVATE/Books', '/run/user/1000/doc/PRIVATE/Books'):
        assert diagnostics.is_document_portal(value)
    assert not diagnostics.is_document_portal('/home/test/run/flatpak/doc/PRIVATE')
