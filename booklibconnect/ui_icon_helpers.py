from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QIcon, QPainter, QColor, QFont, QPixmap, QLinearGradient, QFontMetrics
from PySide6.QtWidgets import QStyle

from .debug_tools import dbg


def theme_icon_or_fallback(style: QStyle, theme_name: str, fallback_sp: QStyle.StandardPixmap) -> QIcon:
    ico = QIcon.fromTheme(theme_name)
    if not ico.isNull():
        return ico

    try:
        ico = style.standardIcon(fallback_sp)
        if not ico.isNull():
            return ico
    except Exception:
        pass

    pm = QPixmap(128, 128)
    pm.fill(QColor(60, 66, 75))
    return QIcon(pm)


def safe_icon_pixmap(icon: QIcon, w: int, h: int) -> QPixmap:
    w = max(1, int(w))
    h = max(1, int(h))

    pm = icon.pixmap(w, h)
    if not pm.isNull():
        return pm

    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    return pm


def qicon_from_bytes(data: bytes, size: QSize) -> QIcon | None:
    pm = QPixmap()
    if not pm.loadFromData(data) or pm.isNull():
        return None
    pm = pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QIcon(pm)


def _elide_text_for_rect(text: str, font: QFont, rect: QRect) -> str:
    text = (text or "").strip()
    if not text or rect.width() <= 4:
        return text

    fm = QFontMetrics(font)
    return fm.elidedText(text, Qt.ElideRight, max(1, rect.width() - 2))


def draw_tracker_text(
    painter: QPainter,
    text: str,
    color: QColor,
    rect: QRect,
    *,
    pt: int = 10,
) -> None:
    text = (text or "").strip()
    if not text:
        return

    painter.save()
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    font = QFont()
    font.setBold(True)
    font.setPointSize(max(8, int(pt)))
    painter.setFont(font)

    text = _elide_text_for_rect(text, font, rect)

    painter.setPen(QColor(0, 0, 0, 170))
    painter.drawText(rect.adjusted(0, 1, 0, 1), Qt.AlignVCenter | Qt.AlignLeft, text)

    painter.setPen(color)
    painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)

    painter.restore()


def album_tile_icon_with_strip(
    style: QStyle,
    cover_icon: QIcon,
    size: QSize,
    *,
    read_line: str,
    read_color: QColor,
    own_line: str,
    own_color: QColor,
    gr_line: str,
    gr_color: QColor,
    strip_h: int = 102,
    pad: int = 10,
    gap: int = 8,
    text_pt: int = 10,
) -> QIcon:
    w = max(1, size.width())
    h = max(1, size.height())

    strip_h = max(40, int(strip_h))
    pad = max(4, int(pad))
    gap = max(2, int(gap))
    text_pt = max(8, int(text_pt))

    strip_rect = QRect(pad, h - pad - strip_h, w - pad * 2, strip_h)

    cover_max_h = strip_rect.top() - pad - gap
    cover_side = max(10, min(w - pad * 2, cover_max_h))
    cover_rect = QRect(pad, pad, cover_side, cover_side)

    card_bg = QColor(20, 22, 26, 255)
    card_border = QColor(65, 70, 82, 160)

    cover_pm = safe_icon_pixmap(cover_icon, cover_side, cover_side)
    if cover_pm.isNull():
        fallback = theme_icon_or_fallback(style, "media-optical", QStyle.SP_DirClosedIcon)
        cover_pm = safe_icon_pixmap(fallback, cover_side, cover_side)

    out = QPixmap(w, h)
    out.fill(Qt.transparent)

    painter = QPainter()
    if not painter.begin(out):
        dbg("QPainter.begin failed in album_tile_icon_with_strip size=%sx%s", w, h)
        return QIcon(out)

    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    painter.setPen(Qt.NoPen)
    painter.setBrush(card_bg)
    painter.drawRoundedRect(QRect(0, 0, w, h), 14, 14)

    painter.setBrush(Qt.NoBrush)
    painter.setPen(card_border)
    painter.drawRoundedRect(QRect(1, 1, w - 2, h - 2), 14, 14)

    if cover_pm.width() > 0 and cover_pm.height() > 0:
        scaled = cover_pm.scaled(cover_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        cx = cover_rect.x() + (cover_rect.width() - scaled.width()) // 2
        cy = cover_rect.y() + (cover_rect.height() - scaled.height()) // 2
        painter.drawPixmap(int(cx), int(cy), scaled)

    painter.setPen(QColor(255, 255, 255, 35))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(cover_rect, 10, 10)

    grad = QLinearGradient(strip_rect.topLeft(), strip_rect.bottomLeft())
    grad.setColorAt(0.0, QColor(0, 0, 0, 0))
    grad.setColorAt(0.35, QColor(0, 0, 0, 70))
    grad.setColorAt(1.0, QColor(0, 0, 0, 150))
    painter.setPen(Qt.NoPen)
    painter.setBrush(grad)
    painter.drawRoundedRect(strip_rect, 12, 12)

    inner = QRect(
        strip_rect.x() + 10,
        strip_rect.y() + 8,
        strip_rect.width() - 20,
        strip_rect.height() - 16,
    )

    lines = [
        (read_line, read_color),
        (own_line, own_color),
        (gr_line, gr_color),
    ]
    visible = [(text, color) for text, color in lines if (text or "").strip()]

    if not visible:
        painter.end()
        return QIcon(out)

    row_h = max(1, inner.height() // len(visible))
    y = inner.y()

    for idx, (text, color) in enumerate(visible):
        if idx == len(visible) - 1:
            rect = QRect(inner.x(), y, inner.width(), inner.bottom() - y + 1)
        else:
            rect = QRect(inner.x(), y, inner.width(), row_h)
        draw_tracker_text(painter, text, color, rect, pt=text_pt)
        y += row_h

    painter.end()
    return QIcon(out)


def icon_with_badge_text_at(
    style: QStyle,
    base_icon: QIcon,
    text: str,
    size: QSize,
    badge_color: QColor,
    side: str = "right",
) -> QIcon:
    text = (text or "").strip()
    if not text:
        return base_icon

    canvas_w = max(1, size.width())
    canvas_h = max(1, size.height())

    out = QPixmap(canvas_w, canvas_h)
    out.fill(Qt.transparent)

    # Small inset fixes GNOME/Wayland clipping without making KDE look worse.
    inset = 6
    margin = 6

    draw_w = max(1, canvas_w - inset * 2)
    draw_h = max(1, canvas_h - inset * 2)

    src = safe_icon_pixmap(base_icon, draw_w, draw_h)
    if src.isNull():
        src = safe_icon_pixmap(
            theme_icon_or_fallback(style, "user-identity", QStyle.SP_DirIcon),
            draw_w,
            draw_h,
        )

    painter = QPainter()
    if not painter.begin(out):
        dbg("QPainter.begin failed in icon_with_badge_text_at text=%s size=%sx%s", text, canvas_w, canvas_h)
        return QIcon(out)

    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    x_img = (canvas_w - src.width()) // 2
    y_img = (canvas_h - src.height()) // 2
    painter.drawPixmap(int(x_img), int(y_img), src)

    r = max(12, int(min(canvas_w, canvas_h) * 0.10))
    d = r * 2

    font = QFont()
    font.setBold(True)
    painter.setFont(font)

    fm = painter.fontMetrics()
    pad = max(8, int(r * 0.55))
    badge_w = max(d, fm.horizontalAdvance(text) + pad * 2)
    badge_h = d

    x = margin if side == "left" else canvas_w - badge_w - margin
    y = margin

    painter.setPen(Qt.NoPen)
    painter.setBrush(badge_color)
    painter.drawRoundedRect(int(x), int(y), int(badge_w), int(badge_h), r, r)

    painter.setPen(QColor(255, 255, 255))
    painter.drawText(int(x), int(y), int(badge_w), int(badge_h), Qt.AlignCenter, text)

    painter.end()
    return QIcon(out)
