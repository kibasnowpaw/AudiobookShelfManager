"""Local support journal. Only explicit fields are recorded; never ingest debug.log."""
from __future__ import annotations

import configparser
import os
import platform
import re
import shutil
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from . import __build__, __version__

MAX_RUNS, MAX_EVENTS = 3, 200
_OPERATIONS = frozenset((
    'app', 'startup', 'library.open', 'library.settings', 'library.backup', 'scan',
    'scan.discovery', 'scan.checks', 'scan.index', 'scan.history', 'scan.display', 'series.covers', 'book.open', 'book.render', 'book.save', 'book.cover',
    'book.review', 'book.remove.check', 'book.remove', 'statistics.open',
    'statistics.read', 'statistics.render', 'ui', 'python', 'python.thread',
))
_STATES = {'start', 'ok', 'error', 'warning', 'cancelled', 'retry', 'skipped', 'delay', 'exit'}
_COUNTS = frozenset((
    'ms', 'roots', 'configured', 'available', 'series', 'books', 'reused', 'refreshed',
    'removed', 'failed', 'album_warnings', 'genre_warnings', 'cover_warnings', 'id_warnings',
    'index_saved', 'index_written', 'paths_updated', 'cover_cached', 'cover_rechecked', 'cover_ms', 'portal_roots',
    'history_path_error', 'history_written', 'preview_cached', 'prepared', 'mp3', 'm4b', 'exit_code', 'cover_changed',
    'rename_error', 'refresh_error', 'rating_error', 'purchase_error', 'history_error',
    'listens', 'cached', 'attempt', 'width', 'height', 'errno', 'sqlite_code', 'metadata_ms', 'history_ms', 'artwork_ms',
))
_BACKENDS = {'wayland', 'wayland-egl', 'xcb', 'offscreen', 'minimal', 'windows', 'cocoa',
             'eglfs', 'vnc', 'linuxfb'}
_IDENTIFIER = re.compile(r'[A-Za-z_]\w{0,80}\Z', re.ASCII)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def elapsed_ms(start):
    return round((time.perf_counter() - start) * 1000)


def audio_kind(path):
    suffix = Path(path).suffix.lower()
    return suffix[1:] if suffix in ('.mp3', '.m4b') else 'unknown'


def is_document_portal(path):
    # Count portal-backed roots without keeping the path, document ID or UID.
    parts = Path(path).parts
    return (parts[:4] == ('/', 'run', 'flatpak', 'doc') or
            (len(parts) >= 5 and parts[:3] == ('/', 'run', 'user')
             and parts[3].isdigit() and parts[4] == 'doc'))


def _kernel_version(release):
    # Keep common packaging suffixes, but exclude arbitrary local build names.
    core = re.match(r'[0-9]+(?:\.[0-9]+)+', release)
    if not core:
        return 'unknown'
    version = core[0]
    for tail in re.finditer(r'[-.+][^-+.]*', release[len(version):]):
        token = tail[0][1:]
        if not re.fullmatch(r'[0-9]+|(?:arch|lts|zen|generic|amd64|common|azure|default|'
                            r'lowlatency|rt|rc|fc|el|deb|x86_64|aarch64)[0-9]*', token):
            break
        version += tail[0]
    return version


def _cache_file():
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'audiobookshelfmanager' / 'support' / 'recent.sqlite3'


def _error_fields(exc):
    typ = type(exc)
    domain = typ.__module__.split('.')[0]
    name = typ.__name__
    data = {'error': name if domain in ('builtins', 'sqlite3', 'mutagen', 'booklibconnect', 'shiboken6', 'PySide6')
            and _IDENTIFIER.fullmatch(name) else 'Exception'}
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        data['errno'] = exc.errno
    if isinstance(exc, sqlite3.Error) and isinstance(getattr(exc, 'sqlite_errorcode', None), int):
        data['sqlite_code'] = exc.sqlite_errorcode
    frames = deque(maxlen=5)
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        mod = frame.f_globals.get('__name__', '')
        # App module + source line only. Never format a traceback or inspect locals.
        if isinstance(mod, str) and mod.startswith('booklibconnect.'):
            leaf = mod.removeprefix('booklibconnect.')
            if _IDENTIFIER.fullmatch(leaf):
                frames.append(f'{leaf}:{tb.tb_lineno}')
        tb = tb.tb_next
    if frames:
        data['at'] = ' > '.join(frames)
    return data


def _fields(values, exc):
    clean = {k: int(v) for k, v in values.items()
             if k in _COUNTS and isinstance(v, (int, bool)) and abs(v) <= 10**12}
    if values.get('format') in ('mp3', 'm4b', 'unknown'):
        clean['format'] = values['format']
    if 'backend' in values:
        name = values['backend']
        clean['backend'] = name if isinstance(name, str) and name in _BACKENDS else 'other'
    qt = values.get('qt')
    if isinstance(qt, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', qt):
        clean['qt'] = qt
    if exc is not None:
        clean.update(_error_fields(exc))
    return clean


class Journal:
    """One separate bounded SQLite file; each event is committed at its operation boundary."""
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._recent = deque(maxlen=MAX_EVENTS)
        self._seen_errors = set()
        self.started = _now()
        info = dict(system_details())
        summary_fields = ('Install environment', 'OS', 'Kernel version', 'Flatpak runtime', 'Desktop',
                          'Display session', 'CPU architecture', 'Python', 'PySide6', 'Mutagen',
                          'SQLite', 'RAM available', 'Free on app data volume')
        self.environment = ' | '.join(f'{k}: {info[k]}' for k in summary_fields if k in info)
        self.conn = None
        self.session = None
        self.degraded = False
        conn = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=.05, check_same_thread=False)
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=NORMAL')
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, build TEXT, ended TEXT, environment TEXT)')
                conn.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run INTEGER REFERENCES runs(id) ON DELETE CASCADE, line TEXT)')
                conn.execute('CREATE INDEX IF NOT EXISTS by_run ON events(run, id)')
                self.session = conn.execute('INSERT INTO runs(started,build,environment) VALUES(?,?,?)',
                                            (self.started, f'{__version__} / {__build__}', self.environment)).lastrowid
                conn.execute('DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)', (MAX_RUNS,))
            self.conn = conn
        except (OSError, sqlite3.Error):
            if conn is not None:
                conn.close()
            self.degraded = True

    def record(self, op, state, *, exc=None, once=False, **fields):
        if op not in _OPERATIONS or state not in _STATES:
            return
        details = _fields(fields, exc)
        with self._lock:
            if once:
                key = (op, tuple(details.items()))
                if key in self._seen_errors or len(self._seen_errors) >= 32:
                    return
                self._seen_errors.add(key)
            line = '  '.join([_now(), op, state, *[f'{k}={v}' for k, v in details.items()]])
            self._recent.append(line)
            if self.conn is None:
                return
            try:
                with self.conn:
                    self.conn.execute('INSERT INTO events(run,line) VALUES(?,?)', (self.session, line))
                    self.conn.execute('DELETE FROM events WHERE run=? AND id <= '
                                      '(SELECT id FROM events WHERE run=? ORDER BY id DESC LIMIT 1 OFFSET ?)',
                                      (self.session, self.session, MAX_EVENTS))
                    if op == 'app' and state == 'exit':
                        self.conn.execute('UPDATE runs SET ended=? WHERE id=?', ('normal exit', self.session))
            except sqlite3.Error:
                # Keep a usable live report even on a read-only disk or lock.
                self.degraded = True

    def memory_snapshot(self):
        with self._lock:
            return list(self._recent)

    def close(self):
        with self._lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None


_active: Journal | None = None


def start():
    global _active
    _active = Journal(_cache_file())
    record('app', 'start')


def stop(code=0):
    global _active
    if _active is not None:
        record('app', 'exit', exit_code=code)
        _active.close()
        _active = None


def record(op, state, **fields):
    active = _active
    if active is not None:
        active.record(op, state, **fields)


def _release(path):
    # Only read distribution ID and version, not arbitrary OS branding fields.
    try:
        with open(path, encoding='utf-8') as f:
            values = dict(line.rstrip().split('=', 1) for line in f if '=' in line)
        ident = values.get('ID', '').strip('"\'')
        version = values.get('VERSION_ID', '').strip('"\'')
        if re.fullmatch(r'[A-Za-z0-9._-]{1,64}', ident):
            return ident + (' ' + version if re.fullmatch(r'[a-zA-Z0-9._-]{1,30}', version) else '')
    except (OSError, UnicodeError, ValueError):
        pass
    return 'unknown'


def _version(dist):
    try:
        version = metadata.version(dist)
        return version if re.fullmatch(r'[A-Za-z0-9.+_-]{1,64}', version or '') else 'unknown'
    except (ValueError, OSError, metadata.PackageNotFoundError):
        return 'unknown'


def system_details():
    # An unavailable platform probe must never keep the audiobook app from opening.
    try:
        return _system_details()
    except Exception:
        return [('App', f'Audiobook Shelf Manager {__version__}'), ('Build', __build__),
                ('System information', 'unavailable')]


def _system_details():
    flatpak = Path('/.flatpak-info').is_file()
    local_os = _release('/etc/os-release')
    if local_os == 'unknown':
        local_os = _release('/usr/lib/os-release')
    host_os = _release('/run/host/os-release') if flatpak else local_os
    if flatpak and host_os == 'unknown':
        host_os = _release('/run/host/etc/os-release')
    if flatpak and host_os == 'unknown':
        host_os = _release('/run/host/usr/lib/os-release')
    system = platform.system()
    rows = [('App', f'Audiobook Shelf Manager {__version__}'), ('Build', __build__),
            ('Install environment', 'Flatpak' if flatpak else 'outside Flatpak'),
            ('OS', f'{system} / {host_os}'), ('Kernel version', _kernel_version(platform.release())),
            ('CPU architecture', platform.machine()), ('Logical CPUs', str(os.cpu_count() or 'unknown'))]
    if hasattr(os, 'sched_getaffinity'):
        try:
            rows.append(('CPUs available to app', str(len(os.sched_getaffinity(0)))))
        except OSError:
            pass
    if flatpak:
        rows.append(('Sandbox OS', local_os))
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read('/.flatpak-info', encoding='utf-8')
            runtime = cfg.get('Application', 'runtime', fallback='').strip().removeprefix('runtime/')
            if re.fullmatch(r'(?:org\.(?:kde|gnome)\.Platform|org\.freedesktop\.Platform)/[\w.-]+/[\w.-]+', runtime):
                rows.append(('Flatpak runtime', runtime))
            version = cfg.get('Instance', 'flatpak-version', fallback='')
            if re.fullmatch(r'[\d.]{1,30}', version):
                rows.append(('Flatpak version', version))
        except (OSError, ValueError, UnicodeError, configparser.Error):
            pass
    desktop = set(re.split(r'[:;]', os.environ.get('XDG_CURRENT_DESKTOP', '').lower()))
    known = ['kde', 'gnome', 'xfce', 'cinnamon', 'mate', 'lxqt', 'sway', 'hyprland', 'cosmic', 'unity']
    qt_core = sys.modules.get('PySide6.QtCore')
    qt_binding = sys.modules.get('PySide6')
    qt_version = qt_core.qVersion() if qt_core is not None and hasattr(qt_core, 'qVersion') else 'not loaded'
    binding_version = getattr(qt_binding, '__version__', None) or _version('PySide6-Essentials')
    if binding_version == 'unknown':
        binding_version = _version('PySide6')
    session = os.environ.get('XDG_SESSION_TYPE', '').lower()
    rows.append(('Display session', session if session in ('wayland', 'x11', 'tty') else 'unknown'))
    rows.extend([('Desktop', ', '.join(d for d in known if d in desktop) or 'unknown'),
                 ('Python', platform.python_version()), ('PySide6', binding_version),
                 ('Qt runtime', qt_version), ('Mutagen', _version('mutagen')), ('SQLite', sqlite3.sqlite_version)])
    # PCI vendor and model IDs identify a GPU model, not an individual device.
    gpus = []
    for card in sorted(Path('/sys/class/drm').glob('card[0-9]*'))[:32]:
        if not re.fullmatch(r'card[0-9]+', card.name):
            continue
        try:
            pair = [(card / 'device' / key).read_text(encoding='ascii').strip() for key in ('vendor', 'device')]
            if all(re.fullmatch(r'0x[0-9a-fA-F]{4}', value) for value in pair):
                gpus.append(':'.join(value[2:] for value in pair))
        except (OSError, UnicodeError):
            pass
    if gpus:
        rows.append(('Graphics PCI vendor:model', ', '.join(gpus)))
    try:
        data = Path('/proc/meminfo').read_text(encoding='ascii')
        for key in ('MemTotal', 'MemAvailable'):
            match = re.search(r'^' + key + r':\s+(\d+) kB$', data, re.MULTILINE)
            if match:
                rows.append(('RAM ' + ('total' if key == 'MemTotal' else 'available'), f'{int(match[1])/1048576:.1f} GiB'))
    except (OSError, UnicodeError):
        pass
    try:
        path = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
        while not path.exists() and path != path.parent:
            path = path.parent
        rows.append(('Free on app data volume', f'{shutil.disk_usage(path).free/(2**30):.1f} GiB'))
    except OSError:
        pass
    return rows


def _read_runs(path):
    # Read-only: even the command-line reporter never starts a run or creates a database.
    if not path.is_file():
        return []
    uri = path.resolve().as_uri() + '?mode=ro'
    rows = []
    with closing(sqlite3.connect(uri, uri=True, timeout=.1)) as conn:
        conn.execute('BEGIN')
        for ident, started, build, ended, environment in conn.execute(
                'SELECT id,started,build,ended,environment FROM runs ORDER BY id DESC LIMIT ?', (MAX_RUNS,)):
            events = [r[0] for r in conn.execute('SELECT line FROM events WHERE run=? ORDER BY id', (ident,))]
            rows.append((ident, started, build, ended, environment, events))
    return rows


def report(path=None):
    active = _active
    path = Path(path) if path is not None else (active.path if active is not None else _cache_file())
    out = ['Audiobook Shelf Manager | Diagnostics', f'Generated (UTC): {_now()}', '',
           'System at export', *[f'{k}: {v}' for k, v in system_details()], '',
           'Recent runs (newest first; times are UTC; ms = milliseconds)']
    try:
        rows = _read_runs(path)
    except (OSError, sqlite3.Error):
        out.append('Saved events could not be read.')
        rows = []
    if active is not None and active.path == path:
        if active.degraded:
            out.append('Some events could not be saved to disk. This run includes its in-memory history.')
        rows = [r for r in rows if r[0] != active.session]
        rows.insert(0, (active.session, active.started, f'{__version__} / {__build__}', 'currently running',
                        active.environment, active.memory_snapshot()))
    if not rows:
        out.append('No recorded runs yet.')
    for _, started, build, ended, environment, events in rows[:MAX_RUNS]:
        out.extend(['', f'{started} | build {build} | {ended or "no exit recorded (may still be running)"}', f'At launch: {environment}', *events])
    return '\n'.join(out) + '\n'
