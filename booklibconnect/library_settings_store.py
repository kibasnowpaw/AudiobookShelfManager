"""Named libraries and preferences; the original collection remains Main."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
from .atomic_json import write_json_atomic as _write_json_atomic
import uuid
from pathlib import Path
from .gallery_options import validate_options, effective_options, series_key


DEFAULT_PREFERENCES = {
    'playback_speed': 2.0,
    'auto_rename': True,
    'rename_sidecar': True,
    'scan_warning_dialog': True,
    'remember_sort': True,
}


def normalized_root(value):
    text = str(value).strip()
    if not text:
        raise ValueError('Choose a library folder.')
    return Path(text).expanduser().resolve()


def roots_overlap(first, second):
    first, second = normalized_root(first), normalized_root(second)
    if first == second or first in second.parents or second in first.parents:
        return True
    try:
        return first.samefile(second)
    except OSError:
        return False


class LibrarySettingsStore:
    VERSION = 3

    def __init__(self, *, data=None, config_directory=None, catalog_path=None):
        self._config_directory = Path(config_directory) if config_directory is not None else (
            Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'booklibconnect')
        if catalog_path is not None:
            self._catalog_path = Path(catalog_path)
        elif config_directory is not None:
            self._catalog_path = self._config_directory / 'library_settings.json'
        else:
            from .listening_history_store import persistent_data_dir
            self._catalog_path = persistent_data_dir() / 'library_profiles.json'
        self.needs_default_root = False
        self.migrated_legacy = False
        if data is not None:
            self._data = self.validate(data)
            return
        path = self.file_path()
        if not path.exists():
            legacy = self.config_dir() / 'library_settings.json'
            if legacy.exists():
                path = legacy
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise ValueError(f'Could not read library settings. Your file has been kept: {path}\n{exc}') from exc
            if not isinstance(raw, dict):
                raise ValueError(f'Invalid library settings; the file has been kept: {path}')
        else:
            raw = {'library_roots': []}
        if 'version' not in raw:
            roots = raw.get('library_roots', [])
            if not isinstance(roots, list):
                raise ValueError('Invalid saved library folders; settings have not been changed.')
            self.needs_default_root = not roots
            self.migrated_legacy = True
            self._data = self.validate({
                'version': self.VERSION,
                'active_library': 'main',
                'startup_library': 'main',
                'libraries': [{'id': 'main', 'name': 'Main', 'roots': roots, 'sort': {}}],
                'preferences': {},
            })
            if path.exists():
                backup = path.with_name('library_settings.before-separate-libraries.json')
                if not backup.exists():
                    shutil.copy2(path, backup)
            self.save()
        else:
            self._data = self.validate(raw)
            if raw.get('version') == 2:
                backup = path.with_name(path.stem + '.before-gallery-rules.json')
                if not backup.exists():
                    shutil.copy2(path, backup)
            if path != self.file_path() or raw.get('version') != self.VERSION:
                self.save()

    @classmethod
    def validate(cls, data):
        data = copy.deepcopy(data)
        if data.get('version') == 2:
            data['version'] = cls.VERSION
        if data.get('version') != cls.VERSION:
            raise ValueError('These library settings require a different application version.')
        profiles = data.get('libraries')
        if not isinstance(profiles, list) or not profiles:
            raise ValueError('At least the Main library must be kept.')
        ids, names, assigned = set(), set(), []
        for profile in profiles:
            ident = str(profile.get('id', ''))
            if not re.fullmatch(r'main|[a-f0-9]{32}', ident) or ident in ids:
                raise ValueError('Invalid or duplicate library identity.')
            name = str(profile.get('name', '')).strip()
            if not name or len(name) > 60 or name.casefold() in names:
                raise ValueError('Give each library a different name (1-60 characters).')
            ids.add(ident)
            names.add(name.casefold())
            profile['name'] = name
            raw_roots = profile.get('roots', [])
            if not isinstance(raw_roots, list):
                raise ValueError('Library folders must be a list.')
            roots = []
            for raw in raw_roots:
                root = normalized_root(raw)
                if str(root) in roots:
                    continue
                for other, other_name in assigned:
                    if roots_overlap(root, other):
                        raise ValueError(f'"{name}" and "{other_name}" cannot share or contain each other\'s folders.\n{root}\n{other}')
                roots.append(str(root))
            profile['roots'] = roots
            profile['sort'] = dict(profile.get('sort') or {})
            profile['options'] = validate_options(profile.get('options', {}))
            overrides = profile.get('series_overrides', {})
            if not isinstance(overrides, dict):
                raise ValueError('Invalid series overrides.')
            profile['series_overrides'] = {
                series_key(path): validate_options(values, partial=True)
                for path, values in overrides.items() if values
            }
            assigned.extend((root, name) for root in roots)
        if 'main' not in ids:
            raise ValueError('Keep the original Main library so its saved data remains accessible.')
        if data.get('active_library') not in ids:
            data['active_library'] = 'main'
        if data.get('startup_library') not in ids | {'last'}:
            data['startup_library'] = 'main'
        preferences = dict(data.get('preferences') or {})
        if 'playback_speed' in preferences:
            speed = float(preferences['playback_speed'])
            if not math.isfinite(speed) or not .5 <= speed <= 4:
                raise ValueError('Listening speed must be between 0.50 and 4.00.')
            preferences['playback_speed'] = round(speed, 2)
        for key in DEFAULT_PREFERENCES.keys() - {'playback_speed'}:
            if key in preferences and not isinstance(preferences[key], bool):
                raise ValueError(f'Invalid setting: {key}')
        data['preferences'] = preferences
        data['library_roots'] = next(p['roots'][:] for p in profiles if p['id'] == 'main')
        return data

    def config_dir(self):
        self._config_directory.mkdir(parents=True, exist_ok=True)
        return self._config_directory

    def file_path(self):
        return self._catalog_path

    def snapshot(self):
        return copy.deepcopy(self._data)

    def clone(self, data=None):
        return type(self)(data=self.snapshot() if data is None else data,
                          config_directory=self._config_directory, catalog_path=self._catalog_path)

    def save(self):
        valid = self.validate(self._data)
        _write_json_atomic(self.file_path(), valid)
        self._data = valid

    def libraries(self):
        return copy.deepcopy(self._data['libraries'])

    def profile(self, ident=None):
        ident = ident or self.active_library_id()
        for profile in self._data['libraries']:
            if profile['id'] == ident:
                return copy.deepcopy(profile)
        raise ValueError('That library no longer exists.')

    def active_library_id(self):
        return self._data['active_library']

    def startup_library_id(self):
        value = self._data['startup_library']
        return self.active_library_id() if value == 'last' else value

    def set_active_library(self, ident, *, save=True):
        self.profile(ident)
        previous = self._data['active_library']
        self._data['active_library'] = ident
        try:
            if save:
                self.save()
        except Exception:
            self._data['active_library'] = previous
            raise

    def library_roots(self, ident=None):
        return [Path(value) for value in self.profile(ident)['roots']]

    def set_library_roots(self, roots, ident=None):
        ident = ident or self.active_library_id()
        candidate = self.clone()
        for profile in candidate._data['libraries']:
            if profile['id'] == ident:
                profile['roots'] = [str(root) for root in roots]
        candidate.save()
        self._data = candidate.snapshot()
        self.needs_default_root = False

    def add_library(self, name, roots=()):
        ident = uuid.uuid4().hex
        candidate = self.clone()
        candidate._data['libraries'].append({'id': ident, 'name': name,
                                             'roots': [str(root) for root in roots], 'sort': {}})
        candidate.save()
        self._data = candidate.snapshot()
        return ident

    def profile_config_dir(self, ident=None):
        ident = self.profile(ident)['id']
        return self.config_dir() if ident == 'main' else self.config_dir() / 'libraries' / ident

    def profile_data_dir(self, ident=None):
        from .listening_history_store import persistent_data_dir
        ident = self.profile(ident)['id']
        base = persistent_data_dir()
        return base if ident == 'main' else base / 'libraries' / ident

    def preferences(self):
        return {**DEFAULT_PREFERENCES, **self._data['preferences']}

    def set_preference(self, key, value):
        candidate = self.clone()
        candidate._data['preferences'][key] = value
        candidate.save()
        self._data = candidate.snapshot()

    def set_sort(self, category, value):
        candidate = self.clone()
        for profile in candidate._data['libraries']:
            if profile['id'] == self.active_library_id():
                profile['sort'][category] = value
        candidate.save()
        self._data = candidate.snapshot()

    def options(self, series=None, ident=None):
        # Painting one card must not copy every other series override.
        ident = ident or self.active_library_id()
        for profile in self._data['libraries']:
            if profile['id'] == ident:
                return effective_options(profile, series)
        raise ValueError('That library no longer exists.')
