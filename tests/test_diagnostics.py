"""Reports must be useful after a failure and safe to give to someone else."""
import os
import subprocess
import sys

from PySide6.QtWidgets import QFileDialog
from booklibconnect import diagnostics as d
from booklibconnect.settings_dialog import SettingsDialog
from test_responsive_io import app, library, window, wait_until, open_book
from test_m4b_support import make_m4b


def test_cli_after_abrupt_exit_keeps_error_but_not_private_text(tmp_path):
    env = dict(os.environ, XDG_CACHE_HOME=str(tmp_path), XDG_CURRENT_DESKTOP='KDE:PRIVATE-DESKTOP',
               CANARY_SECRET='SECRET-TOKEN', DISPLAY='no GUI here')
    def run(*args):
        return subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, check=False)
    first = run('-m', 'booklibconnect', '--diagnostics')
    assert first.returncode == 0 and 'No recorded runs yet' in first.stdout
    assert not list(tmp_path.rglob('*.sqlite3'))
    raw = tmp_path / 'booklibconnect' / 'debug.log'
    raw.parent.mkdir()
    raw.write_text('RAW-PRIVATE-DEBUG Book Title /home/private-librarian')
    stopped = run('-c', """
import os
from booklibconnect import diagnostics as d
d.start()
d.record('book.open', 'start', format='m4b', path='/home/private-librarian/Book Title.m4b')
try:
    raise PermissionError(13, 'SECRET-TOKEN', '/home/private-librarian/Book Title.m4b')
except OSError as exc:
    d.record('book.open', 'error', exc=exc, format='m4b', ms=127)
os._exit(81)
""")
    assert stopped.returncode == 81
    out = run('-m', 'booklibconnect', '--diagnostics')
    assert out.returncode == 0 and not out.stderr
    assert 'no exit recorded' in out.stdout and 'PermissionError' in out.stdout and 'errno=13' in out.stdout
    assert 'ms=127' in out.stdout and 'format=m4b' in out.stdout and 'At launch:' in out.stdout
    assert 'Desktop: kde' in out.stdout and 'CPU architecture:' in out.stdout
    assert all(secret not in out.stdout for secret in
               ('SECRET-TOKEN', str(tmp_path), 'private-librarian', 'Book Title', 'RAW-PRIVATE-DEBUG', 'PRIVATE-DESKTOP'))
    assert out.stdout.count('| build ') == 1


def test_early_startup_exception_is_available_without_importing_qt(tmp_path):
    env = dict(os.environ, XDG_CACHE_HOME=str(tmp_path))
    child = subprocess.run([sys.executable, '-c', """
import sys
class NoQt:
    def find_spec(self, name, *args):
        if name.startswith('PySide6'):
            raise ModuleNotFoundError('Private install path /Secret Name here')
sys.meta_path.insert(0, NoQt())
from booklibconnect.app import main
main()
"""], env=env, capture_output=True, check=False)
    assert child.returncode != 0
    command = subprocess.run([sys.executable, '-S', '-m', 'booklibconnect', '--diagnostics'],
                             env=env, capture_output=True, text=True, check=True)
    assert 'ModuleNotFoundError' in command.stdout and 'app:' in command.stdout
    assert 'Secret Name' not in command.stdout
    assert 'no exit recorded' in command.stdout and 'currently running' not in command.stdout


def test_retention_and_concurrent_workers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    db = tmp_path / 'sessions.sqlite3'
    for n in range(d.MAX_RUNS + 1):
        log = d.Journal(db)
        log.record('scan', 'ok', refreshed=n)
        if n == d.MAX_RUNS:
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda i, journal=log: journal.record('ui', 'delay', ms=i), range(d.MAX_EVENTS + 9)))
        log.record('app', 'exit', exit_code=0)
        assert not log.degraded
        log.close()
    out = d.report(db)
    assert out.count('| normal exit') == d.MAX_RUNS
    assert 'refreshed=0' not in out and 'refreshed=1' in out
    assert out.count('  ui  delay') == d.MAX_EVENTS - 1


def test_memory_fallback_on_unwritable_store_and_corrupt_existing_log(tmp_path, monkeypatch):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('sentinel')
    log = d.Journal(blocked / 'journal.sqlite3')
    monkeypatch.setattr(d, '_active', log)
    log.record('library.backup', 'error', exc=OSError(28, 'private text'), ms=9)
    out = d.report()
    assert 'in-memory history' in out and 'errno=28' in out and 'private text' not in out
    assert blocked.read_text() == 'sentinel'
    assert 'could not be read' in d.report(blocked)


def test_real_mixed_scan_book_save_statistics_and_settings_export(
        window, library, app, make_m4b, tmp_path, monkeypatch):
    w, messages = window
    root, paths, _ = library
    m4b = make_m4b(paths[0].parent / 'Never Share This Title.m4b')
    log = d.Journal(tmp_path / 'support' / 'sessions.sqlite3')
    monkeypatch.setattr(d, '_active', log)
    w.start_scan()
    wait_until(app, lambda: w._scan_thread is None)
    open_book(w, m4b, app, edit=True)
    w.chk_autorename.setChecked(False)
    w.ed_rating.setValue(3.61)
    w.save_metadata_for_selected()
    wait_until(app, lambda: w._file_job is None)
    assert w.listening_history_store.book(w._book_track_id).rating == 3.61
    w.show_statistics()
    wait_until(app, lambda: w._statistics_job is None and w.statistics_page.result is not None)
    dialog = SettingsDialog(w.library_settings_store, w)
    dialog.show()
    dialog.nav.setCurrentRow(dialog.nav.count() - 1)
    text = dialog.diagnostics_page.report_preview.toPlainText()
    assert dialog.diagnostics_page.report_preview.isVisible() and not dialog.library_choice.isVisible()
    assert 'scan.discovery  ok' in text and 'mp3=3' in text and 'm4b=1' in text
    assert 'scan.checks  ok' in text and 'cover_cached=' in text and 'cover_rechecked=' in text
    assert 'scan.index  ok' in text and 'index_written=' in text and 'scan.history  ok' in text
    open_line = next(line for line in text.splitlines() if 'book.open  ok' in line)
    assert all(key in open_line for key in ('metadata_ms=', 'history_ms=', 'artwork_ms=', 'history_written=', 'cover_cached='))
    assert 'book.save  ok' in text and 'format=m4b' in text and 'book.render  ok' in text
    assert 'statistics.read  ok' in text and 'statistics.render  ok' in text
    assert not any(v in text for v in ('Never Share This Title', 'Example Author', str(root), 'rating=3.61'))
    dst = tmp_path / 'my-report.txt'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: ('', ''))
    dialog.diagnostics_page.save_report_button.click()
    assert not dst.exists() and dialog.isVisible()
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (str(dst), ''))
    dialog.diagnostics_page.save_report_button.click()
    assert dst.read_text() == text and dialog.diagnostics_page.diag_status.text() == 'Report saved.'
    assert not messages
    dialog.close()
    log.close()


def test_diagnostics_menu_preserves_unsaved_edit(window, library, app):
    from PySide6.QtCore import QTimer
    from booklibconnect.settings_dialog import DiagnosticsPanel
    w, _ = window
    _, paths, _ = library
    open_book(w, paths[0], app, edit=True)
    w.ed_title.setText('Private unsaved draft')
    seen = []
    def inspect():
        dialog = app.activeModalWidget()
        if dialog is None:
            return
        try:
            panel = dialog.findChild(DiagnosticsPanel)
            if panel is not None:
                seen.append(panel.report_preview.toPlainText())
        finally:
            dialog.reject()
    QTimer.singleShot(0, inspect)
    w.act_diagnostics.trigger()
    assert seen and 'System at export' in seen[0] and 'Private unsaved draft' not in seen[0]
    assert w._book_editing and w.ed_title.text() == 'Private unsaved draft'


def test_flatpak_system_snapshot_distinguishes_runtime_from_host(monkeypatch):
    from pathlib import Path
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda path: str(path) == '/.flatpak-info' or original(path))
    monkeypatch.setattr(d, '_release', lambda path: {'/etc/os-release': 'freedesktop 25.08',
        '/run/host/os-release': 'unknown', '/run/host/etc/os-release': 'arch'}[path])
    def fake_ini(parser, *args, **kwargs):
        parser.read_string('[Application]\nruntime=runtime/org.kde.Platform/x86_64/6.10\n'
                           '[Instance]\nflatpak-version=1.16.2\ninstance-id=IDENTIFIER-MUST-NOT-LEAK\n')
    monkeypatch.setattr(d.configparser.ConfigParser, 'read', fake_ini)
    report = dict(d.system_details())
    assert report['Install environment'] == 'Flatpak' and report['OS'].endswith(' / arch')
    assert report['Sandbox OS'] == 'freedesktop 25.08'
    assert report['Flatpak runtime'] == 'org.kde.Platform/x86_64/6.10'
    assert report['Flatpak version'] == '1.16.2'
    assert 'IDENTIFIER-MUST-NOT-LEAK' not in str(report)
