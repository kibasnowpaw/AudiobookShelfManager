"""Optional personal purchase details and calendar-based ownership age."""
import calendar
import re
from datetime import date, datetime


def validate_purchase(purchased_on='', source='', *, today=None):
    text = str(purchased_on or '').strip()
    source = str(source or '').strip()
    if text:
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            raise ValueError('Enter a valid purchase date in YYYY-MM-DD format.') from None
        if text != parsed.isoformat():
            raise ValueError('Enter the purchase date as YYYY-MM-DD.')
        if parsed > (today or date.today()):
            raise ValueError('Purchase date cannot be in the future.')
    if len(source) > 200:
        raise ValueError('Bought from must be 200 characters or fewer.')
    return text, source


def owned_for(purchased_on, *, today=None):
    if not purchased_on:
        return 'Not recorded'
    try:
        purchased = date.fromisoformat(purchased_on)
    except ValueError:
        return 'Unknown'
    today = today or date.today()
    if purchased > today:
        return 'Purchase date is in the future'
    total_days = (today - purchased).days
    if not total_days:
        return '0 days (bought today)'
    months = (today.year - purchased.year) * 12 + today.month - purchased.month
    def shifted(value):
        year, month = divmod(purchased.year * 12 + purchased.month - 1 + value, 12)
        month += 1
        return date(year, month, min(purchased.day, calendar.monthrange(year, month)[1]))
    if shifted(months) > today:
        months -= 1
    days = (today - shifted(months)).days
    years, months_left = divmod(months, 12)
    parts = [f'{count} {unit}' + ('s' if count != 1 else '')
             for count, unit in ((years, 'year'), (months_left, 'month'), (days, 'day')) if count]
    text = ', '.join(parts)
    return text + (f' ({total_days:,} days)' if years or months_left else '')


def release_year(value):
    """Accept a Year tag, ISO date or timestamp; never guess an ambiguous year."""
    if type(value) is int:
        return value if 1 <= value <= 9999 else None
    text = str(value or '').strip()
    # M4B release dates commonly include a time and UTC offset.
    if re.match(r'^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}', text):
        try:
            return datetime.fromisoformat(text).year
        except ValueError:
            return None
    if not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', text):
        return None
    try:
        year = int(text[:4])
        date.fromisoformat(text + '-01-01' if len(text) == 4 else text + '-01' if len(text) == 7 else text)
        return year
    except ValueError:
        return None
