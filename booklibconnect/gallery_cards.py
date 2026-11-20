"""Card layout shared by the library and the live Settings preview."""
from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen
from .rating_widgets import draw_stars
from .rating_values import rating_text

GREEN = QColor('#2ecc71')
MUTED = QColor('#aab6c2')
AMBER = QColor('#f3b34c')


def status_color(count, total):
    return GREEN if total > 0 and count >= total else AMBER if count else MUTED


def card_height(rows):
    return 216 + (12 + 22 * rows if rows else 0)


def cover_rect(size):
    side = min(200, size.width() - 16, size.height() - 16)
    return QRect((size.width() - side) // 2, 8, side, side)


def rating_rect(size, corner):
    cover = cover_rect(size)
    w, h, margin = 112, 44, 3
    return QRect(cover.right() - w - margin + 1 if corner.endswith('right') else cover.left() + margin,
                 cover.bottom() - h - margin + 1 if corner.startswith('bottom') else cover.top() + margin,
                 w, h)


def add_rating(icon, size, rating, corner='top_left'):
    if rating <= 0:
        return icon
    out = QPixmap(size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.drawPixmap(0, 0, icon.pixmap(size))
    badge = rating_rect(size, corner)
    painter.setPen(QPen(QColor('#f5c451'), 1))
    painter.setBrush(QColor(34, 29, 15, 232))
    painter.drawRoundedRect(badge, 8, 8)
    draw_stars(painter, QRectF(badge.x()+10, badge.y()+4, 92, 17), rating)
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(12)
    painter.setFont(font)
    painter.setPen(QColor('#ffd870'))
    painter.drawText(badge.adjusted(0, 23, 0, 0), Qt.AlignCenter, rating_text(rating))
    painter.end()
    return QIcon(out)


def add_warning(icon, size, rating_corner='top_left'):
    out = QPixmap(size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.drawPixmap(0, 0, icon.pixmap(size))
    cover = cover_rect(size)
    badge = QRect(cover.left()+3 if rating_corner == 'top_right' else cover.right()-35,
                  cover.top()+3, 32, 32)
    painter.setPen(QPen(QColor('#503c00'), 2))
    painter.setBrush(QColor('#facc15'))
    painter.drawEllipse(badge)
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(23)
    painter.setFont(font)
    painter.setPen(QColor('#2d2300'))
    painter.drawText(badge, Qt.AlignCenter, '!')
    painter.end()
    return QIcon(out)


def render_card(cover, size, lines, *, rating=0, options=None, warning=False):
    options = options or {}
    out = QPixmap(size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.setPen(QColor('#414652'))
    painter.setBrush(QColor('#14161a'))
    painter.drawRoundedRect(QRect(1, 1, size.width()-2, size.height()-2), 10, 10)
    rect = cover_rect(size)
    image = cover.pixmap(rect.size())
    painter.drawPixmap(rect.x()+(rect.width()-image.width())//2,
                       rect.y()+(rect.height()-image.height())//2, image)
    font = painter.font()
    font.setPixelSize(12)
    font.setBold(True)
    painter.setFont(font)
    for index, (text, color) in enumerate(lines):
        painter.setPen(color)
        label = painter.fontMetrics().elidedText(text, Qt.ElideRight, size.width()-24)
        painter.drawText(QRect(12, 220 + index*22, size.width()-24, 22), Qt.AlignVCenter, label)
    painter.end()
    result = QIcon(out)
    corner = options.get('rating_corner', 'top_left')
    if options.get('show_rating', True):
        result = add_rating(result, size, rating, corner)
    if warning:
        result = add_warning(result, size, corner if options.get('show_rating', True) and rating else '')
    return result
