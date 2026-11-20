"""One tag-count rule for scanning, editing, cards and Statistics."""
import re
import unicodedata
from .gallery_options import validate_rule

REQUIRED_GENRE_TAGS = 10  # Backward-compatible default for existing callers.


def split_genre_tags(value):
    tags = {}
    for part in re.split(r'[,;|]', str(value or '')):
        part = part.strip()
        if part:
            tags.setdefault(unicodedata.normalize('NFKC', part).casefold(), part)
    return list(tags.values())


def rule_description(rule=None):
    rule = validate_rule(rule or {})
    mode, count = rule['mode'], rule['count']
    return {'off': 'no count rule', 'exact': f'exactly {count}',
            'minimum': f'at least {count}', 'maximum': f'at most {count}',
            'range': f"{count}-{rule['maximum']}"}[mode]


def genre_tag_issue(value, rule=None):
    rule = validate_rule(rule or {})
    mode, target = rule['mode'], rule['count']
    if mode == 'off':
        return None
    text = str(value or '').strip()
    count = len(split_genre_tags(text))
    lower = target if mode in ('exact', 'minimum', 'range') else 0
    upper = target if mode in ('exact', 'maximum') else rule['maximum'] if mode == 'range' else None
    if count >= lower and (upper is None or count <= upper):
        return None
    kind = 'Missing genre' if count == 0 else 'Too few genre tags' if count < lower else 'Too many genre tags'
    action = f'Add {lower-count} more.' if count < lower else f'Remove {count-upper}.'
    amount = f'{count}/{target}' if mode == 'exact' else str(count)
    reason = f'Genre has {amount} unique tags; {rule_description(rule)} required. {action}'
    if not count:
        reason = f'Genre is missing. Add {rule_description(rule)} unique tags.'
    return {'genre': text, 'count': str(count), 'reason': reason, 'kind': kind}
