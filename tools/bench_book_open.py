"""Compare book-opening workers on the same four disposable MP3/M4B files.

Run: PYTHONPATH=. python tools/bench_book_open.py [--compare /path/to/previous/source]
Requires ffmpeg, Mutagen and PySide6. No user database or media directory is opened.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def prepare(area):
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
    from booklibconnect import ui_mainwindow as ui
    from booklibconnect.album_expected_store import AlbumExpectedStore
    from booklibconnect.listening_history_store import ListeningHistoryStore
    from booklibconnect.models import TrackMeta
    from booklibconnect.scan_index_store import ScanIndexStore

    art = area / 'cover.jpg'
    image = QImage(1400, 1400, QImage.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 1200, 1400)
    gradient.setColorAt(0, QColor('#dee5d8'))
    gradient.setColorAt(1, QColor('#28546d'))
    painter.fillRect(image.rect(), gradient)
    painter.end()
    assert image.save(str(art), 'JPEG', 91)
    root = area / 'media'
    series = root / 'Author' / 'Series'
    series.mkdir(parents=True)
    for ext, codec, fmt in [('mp3', 'libmp3lame', 'mp3'), ('m4b', 'aac', 'mp4')]:
        seed = area / ('sample.' + ext)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'anullsrc=r=44100:cl=mono', '-t', '0.6', '-c:a', codec,
                        '-f', fmt, str(seed)], check=True)
        for kind in ('embedded', 'sidecar'):
            path = series / (ext + '-' + kind + '.' + ext)
            shutil.copyfile(seed, path)
            ui.write_metadata_to_file(path, TrackMeta(title=path.stem, artist='Author', album='Series'),
                                      cover_path=art)
            if kind == 'sidecar':
                shutil.copyfile(art, path.with_suffix('.jpg'))
    results = []
    worker = ui.ScanWorker([root], AlbumExpectedStore(), ScanIndexStore(), {})
    worker.finished.connect(results.append)
    worker.run()
    assert len(results) == 1 and not results[0]['error'] and results[0]['refreshed'] == 4
    store = ListeningHistoryStore(area / 'state', automatic_daily_backup=False)
    for path, cached in results[0]['track_records'].items():
        ident = store.register_track(Path(path), cached.metadata, cached.length_seconds)
        store.set_rating(ident, 3.61)
        store.add_completed_listen(ident, started_on='2026-01-01', finished_on='2026-01-02')


def worker(area, rounds):
    from booklibconnect import __build__, portable_track_state as portable, ui_mainwindow as ui
    from booklibconnect.listening_history_store import ListeningHistoryStore
    from booklibconnect.scan_index_store import ScanIndexStore
    records = ScanIndexStore().entries()
    assert len(records) == 4
    store = ListeningHistoryStore(area / 'state', automatic_daily_backup=False)
    db = str(store.database_path())
    counts = dict(db_connections=0, audio_opens=0, fingerprints=0)
    enabled = False
    def audit(event, args):
        if enabled and event == 'open' and args[0] in records:
            counts['audio_opens'] += 1
        elif enabled and event == 'sqlite3.connect' and str(args[0]) == db:
            counts['db_connections'] += 1
    sys.addaudithook(audit)
    fingerprint = ui._image_visual_fingerprint
    def counted(data):
        counts['fingerprints'] += 1
        return fingerprint(data)
    ui._image_visual_fingerprint = counted
    times = []
    for _ in range(rounds):
        for filename, record in sorted(records.items()):
            path = Path(filename)
            portable._invalidate_cache(path)  # First tag lookup after a restart.
            enabled = True
            started = time.perf_counter_ns()
            try:
                result = ui._load_book_data(path, record, store, lambda _: None)
            finally:
                times.append((time.perf_counter_ns() - started) / 1e6)
                enabled = False
            assert result['metadata_cached'] and not result['history_error']
            assert result['listen_count'] == 1 and result['preferences'][0] == 3.61
            assert not result['cover_image'].isNull() and result['record'].cover_issue is None
    return dict(build=__build__, opens=len(times), median_worker_ms=round(statistics.median(times), 2),
                per_open={key: value / len(times) for key, value in counts.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=7)
    parser.add_argument('--compare', type=Path)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('Rounds must be positive.')
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication([])
    if args.worker is not None:
        print(json.dumps(worker(args.worker, args.rounds)))
        return
    with tempfile.TemporaryDirectory(prefix='asm-open-') as tmp:
        area = Path(tmp)
        os.environ.update(BOOKLIBCONNECT_DEBUG='0', QT_QPA_PLATFORM='offscreen',
                          XDG_CONFIG_HOME=str(area / 'config'), XDG_CACHE_HOME=str(area / 'cache'),
                          BOOKLIBCONNECT_DATA_DIR=str(area / 'state'))
        prepare(area)
        roots = ([args.compare] if args.compare else []) + [Path(__file__).resolve().parents[1]]
        for root in roots:
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(area),
                       '--rounds', str(args.rounds)]
            subprocess.run(command, env=dict(os.environ, PYTHONPATH=str(root.resolve())),
                           cwd=area, check=True)
    del app


if __name__ == '__main__':
    main()
