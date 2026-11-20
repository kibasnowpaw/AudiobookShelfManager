"""Measure warm rescans on disposable real MP3/M4B files; no user data is opened.

From a source checkout: PYTHONPATH=. python tools/bench_scan.py
Requires ffmpeg, PySide6 and Mutagen. Prints one JSON record.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--series', type=int, default=24)
    parser.add_argument('--books', type=int, default=24, help='Books per series')
    parser.add_argument('--art', choices=('none', 'mixed'), default='none',
                        help='Mix embedded, folder and per-book covers, or test without covers')
    parser.add_argument('--total-books', type=int, help='Spread this total across --series instead of using --books per series')
    parser.add_argument('--m4b-count', type=int, help='Use exactly this many M4Bs; the rest are MP3s')
    args = parser.parse_args()
    count = args.total_books if args.total_books is not None else args.series * args.books
    if count < args.series or (args.m4b_count is not None and not 0 <= args.m4b_count <= count):
        parser.error('Use at least one book per series and an M4B count between zero and the total.')
    if min(args.rounds, args.series, args.books) < 1:
        parser.error('Counts must be positive.')
    with tempfile.TemporaryDirectory(prefix='asm-bench-') as name:
        area = Path(name)
        media = area / 'media'
        os.environ.update(BOOKLIBCONNECT_DEBUG='0', QT_QPA_PLATFORM='offscreen',
                          XDG_CONFIG_HOME=str(area/'config'), XDG_CACHE_HOME=str(area/'cache'),
                          BOOKLIBCONNECT_DATA_DIR=str(area/'history'))
        from PySide6.QtCore import QCoreApplication
        from booklibconnect.ui_mainwindow import ScanWorker
        from booklibconnect.scan_index_store import ScanIndexStore
        from booklibconnect.album_expected_store import AlbumExpectedStore
        app = QCoreApplication.instance() or QCoreApplication([])
        if args.art == 'mixed':
            from PySide6.QtGui import QColor, QImage
            from booklibconnect.metadata_io import write_metadata_to_file
            from booklibconnect.models import TrackMeta
            image = QImage(200, 200, QImage.Format_RGB32)
            image.fill(QColor('#488c89'))
            art = area/'art.png'
            if not image.save(str(art)):
                raise RuntimeError('Cannot create cover fixture')
        for ext, codec in [('mp3', 'libmp3lame'), ('m4b', 'aac')]:
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi',
                            '-i','anullsrc=r=22050:cl=mono','-t','0.15','-c:a',codec,
                            '-metadata',f'track=1/{args.books}',str(area / ('source.'+ext))], check=True)
            if args.art == 'mixed':
                write_metadata_to_file(area / ('source.'+ext), TrackMeta(trackno=1, totaltracks=args.books), cover_path=art)
        made = 0
        for si in range(args.series):
            folder = media / f'Writer {si//2:03}' / f'Series {si:03}'
            folder.mkdir(parents=True)
            if args.art == 'mixed' and si % 3 == 1:
                shutil.copyfile(art, folder/'Folder.PNG')
            for n in range(count // args.series + int(si < count % args.series)):
                use_m4b = n % 2 if args.m4b_count is None else made < args.m4b_count
                ext = 'm4b' if use_m4b else 'mp3'
                made += 1
                shutil.copyfile(area / ('source.'+ext), folder / f'Book {n:03}.{ext}')
                if args.art == 'mixed' and si % 3 == 2:
                    shutil.copyfile(art, folder / f'Book {n:03}.png')
        assert made == count
        reads = writes = 0
        stat_calls = 0
        prefix = str(media) + os.sep
        # Count explicit Path.stat/is_file calls beneath the media root; this is
        # a proxy for repeated filesystem work, not a kernel syscall counter.
        original_stat = Path.stat
        def counted_stat(path, *values, **options):
            nonlocal stat_calls
            if str(path).startswith(prefix):
                stat_calls += 1
            return original_stat(path, *values, **options)
        def count_io(event, details):
            nonlocal reads, writes
            if event in ('os.listdir', 'os.scandir'):
                if details and str(details[0]).startswith(prefix):
                    reads += 1
            elif event == 'os.rename' and str(details[1]).endswith('/scan_index.json'):
                writes += 1
        sys.addaudithook(count_io)
        def scan():
            results = []
            worker = ScanWorker([media], AlbumExpectedStore(), ScanIndexStore(), {})
            worker.finished.connect(results.append)
            worker.run()
            if len(results) != 1 or results[0]['error'] or results[0]['cancelled'] or results[0]['failed']:
                raise RuntimeError('Synthetic scan did not finish successfully')
            return results[0]
        if scan()['refreshed'] != count:
            raise RuntimeError('Cold scan skipped books')
        from unittest.mock import patch
        times, listings, index_writes, file_checks = [], [], [], []
        for _ in range(args.rounds):
            reads = writes = stat_calls = 0
            start = time.perf_counter()
            with patch.object(Path, 'stat', counted_stat):
                result = scan()
            times.append(time.perf_counter() - start)
            if result['reused'] != count or result['refreshed'] != 0:
                raise RuntimeError('Warm scan did not reuse all books')
            listings.append(reads)
            index_writes.append(writes)
            file_checks.append(stat_calls)
        print(json.dumps(dict(books=count, series=args.series, rounds=args.rounds, artwork=args.art,
                              seconds=[round(t, 4) for t in times],
                              median_seconds=round(statistics.median(times), 4),
                              media_subdirectory_listings=listings, media_path_stats=file_checks, index_writes=index_writes)))
        del app


if __name__ == '__main__':
    main()
