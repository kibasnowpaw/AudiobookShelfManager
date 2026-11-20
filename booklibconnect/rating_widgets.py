"""Native partially filled stars shared by book controls and cover badges."""
import math

from PySide6.QtCore import Qt, QRectF, QLocale
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget, QPushButton, QDoubleSpinBox

from .rating_values import rating_value, rating_text


def draw_stars(painter, rect, value, count=5):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    size = min(rect.height(), rect.width() / count)
    gap = (rect.width() - size * count) / max(1, count-1)
    for index in range(count):
        x = rect.left() + index * (size + gap)
        y = rect.top() + (rect.height() - size) / 2
        path = QPainterPath()
        for point in range(10):
            angle = -math.pi/2 + point * math.pi/5
            radius = size * (0.46 if point % 2 == 0 else 0.21)
            px, py = x + size/2 + math.cos(angle)*radius, y + size/2 + math.sin(angle)*radius
            path.moveTo(px, py) if point == 0 else path.lineTo(px, py)
        path.closeSubpath()
        painter.fillPath(path, QColor('#66717a'))
        fraction = max(0.0, min(1.0, value-index))
        bounds = path.boundingRect()
        if fraction:
            painter.save()
            painter.setClipRect(QRectF(bounds.left(), bounds.top(), bounds.width()*fraction, bounds.height()), Qt.IntersectClip)
            painter.fillPath(path, QColor('#f5c451'))
            painter.restore()
        painter.setPen(QPen(QColor('#c9a145'), 0.7))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
    painter.restore()


class RatingStars(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0.0
        self.setFixedSize(150, 30)

    def setValue(self, value):
        self.value = rating_value(value)
        self.setAccessibleName(rating_text(self.value))
        self.setToolTip(rating_text(self.value))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        draw_stars(painter, QRectF(self.rect()), self.value)
        painter.end()


class RatingStarButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fraction = 0.0
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)

    def setFraction(self, fraction):
        self.fraction = max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        draw_stars(painter, QRectF(self.rect()).adjusted(3, 3, -3, -3), self.fraction, count=1)
        if self.hasFocus() or self.underMouse():
            painter.setPen(QPen(QColor('#f5c451'), 1, Qt.DotLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 4, 4)
        painter.end()


class RatingSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLocale(QLocale.c())
        self.setRange(0, 5)
        self.setDecimals(2)
        self.setSingleStep(0.01)
        self.setSpecialValueText('Unrated')
        self.setKeyboardTracking(True)
        self.setMinimumWidth(100)
        self.setAccessibleName('Personal rating out of five')
        self.setToolTip('0.01 to 5.00. Use 0 for unrated. Saved with Save Metadata.')

    def validate(self, text, pos):
        state, _text, _pos = super().validate(text.replace(',', '.'), pos)
        return state, text, pos

    def valueFromText(self, text):
        return super().valueFromText(text.replace(',', '.'))
