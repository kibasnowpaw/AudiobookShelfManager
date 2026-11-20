"""Library defaults and sparse series overrides; no file I/O during painting."""
from copy import deepcopy
from pathlib import Path

DEFAULT_OPTIONS = {
    'tag_rule': {'mode': 'exact', 'count': 10, 'maximum': 10},
    'show_rating': True,
    'rating_corner': 'top_left',
    'show_owned': True,
    'show_completed': True,
    'show_goodreads': True,
    'show_audible': False,
    'show_reviewed': False,
    'show_listening_dates': True,
}
MARKER_LABELS = {
    'show_owned': 'Owned', 'show_completed': 'Completed',
    'show_goodreads': 'Goodreads reviewed', 'show_audible': 'Audible reviewed',
    'show_reviewed': 'Reviewed (general)',
    'show_listening_dates': 'Last listened and days to finish',
}
CORNERS = {'top_left': 'Top left', 'top_right': 'Top right',
           'bottom_left': 'Bottom left', 'bottom_right': 'Bottom right'}
RULE_MODES = {'off': 'No count rule', 'exact': 'Exactly', 'minimum': 'At least',
              'maximum': 'At most', 'range': 'Between'}


def validate_rule(rule):
    if not isinstance(rule, dict):
        raise ValueError('Invalid tag rule.')
    result = {**DEFAULT_OPTIONS['tag_rule'], **rule}
    if result['mode'] not in RULE_MODES:
        raise ValueError('Choose a tag count rule.')
    for key in ('count', 'maximum'):
        if type(result[key]) is not int or not 0 <= result[key] <= 999:
            raise ValueError('Tag counts must be whole numbers between 0 and 999.')
    if result['mode'] == 'range' and result['maximum'] < result['count']:
        raise ValueError('Maximum tags must be at least the minimum.')
    return result


def validate_options(options, *, partial=False):
    if not isinstance(options, dict):
        raise ValueError('Invalid gallery options.')
    result = deepcopy(options if partial else {**DEFAULT_OPTIONS, **options})
    for key, value in result.items():
        if key not in DEFAULT_OPTIONS:
            raise ValueError('Unknown gallery option: ' + str(key))
        if key == 'tag_rule':
            result[key] = validate_rule(value)
        elif key == 'rating_corner':
            if value not in CORNERS:
                raise ValueError('Choose a rating corner.')
        elif not isinstance(value, bool):
            raise ValueError('Invalid display switch: ' + key)
    return result


def series_key(path):
    # Scan paths are already absolute. Do not resolve/stat files on the UI thread.
    return str(Path(path).expanduser())


def effective_options(profile=None, series=None):
    profile = profile or {}
    options = {**DEFAULT_OPTIONS, **profile.get('options', {})}
    if series is not None:
        options.update(profile.get('series_overrides', {}).get(series_key(series), {}))
    # All other public options are scalar; callers may edit their returned rule.
    options['tag_rule'] = dict(options['tag_rule'])
    return options
