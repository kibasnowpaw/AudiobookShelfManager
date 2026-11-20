"""Shared decimal rating rules, independent of Qt and the database."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math


def normalize_rating(value):
    """Accept 0 (unrated) or 0.01-5.00; round a supplied value to hundredths."""
    try:
        number = Decimal(str(value).strip().replace(',', '.'))
        if not number.is_finite() or not 0 <= number <= 5:
            raise ValueError
        return float(number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Rating must be a number from 0 to 5. Use 0 for unrated.') from None


def rating_value(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 5 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def format_rating(value):
    number = Decimal(str(rating_value(value))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return format(number, '.2f').rstrip('0').rstrip('.')


def rating_text(value):
    return f'{format_rating(value)} out of 5' if rating_value(value) else 'Unrated'


def mean_rating(values):
    numbers = [Decimal(str(value)) for value in values if rating_value(value) > 0]
    return float(sum(numbers) / len(numbers)) if numbers else None


def rating_band(value):
    """Five non-overlapping bands: (0,1], (1,2], ... (4,5]; 0 is unrated."""
    return math.ceil(rating_value(value))


def band_label(band):
    return f'{band-1}.01\u2013{band}.00'
