"""Small native widgets and virtual models for the Statistics page."""
from __future__ import annotations

from collections import defaultdict
from PySide6.QtCore import Qt, Signal, QSize, QModelIndex, QAbstractTableModel, QAbstractListModel
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar
from .rating_values import rating_text
from .purchase_info import owned_for


def plain(text):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    return label


class CategoryChart(QWidget):
    activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.buttons = []
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(3)

    def set_entries(self, entries, unit='books', limit=12):
        self.entries = list(entries)
        while self.layout.count():
            widget = self.layout.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.buttons = []
        if not self.entries:
            self.layout.addWidget(plain('No matching data'))
            return
        shown = self.entries[:limit]
        unknown = next((entry for entry in self.entries if entry[0] == ''), None)
        if unknown and unknown not in shown:
            shown = shown[:-1] + [unknown]
        maximum = max(1, max(count for key, label, count in self.entries))
        for key, title, count in shown:
            button = QPushButton()
            button.setAccessibleName(f'{title}: {count:,} {unit}. Show matching books.')
            button.setToolTip(f'{title}: {count:,} {unit}. Click to see the books.')
            row = QHBoxLayout(button)
            row.setContentsMargins(7, 5, 7, 5)
            label = plain(title)
            label.setMinimumWidth(115)
            label.setMaximumWidth(220)
            row.addWidget(label, 2)
            progress = QProgressBar()
            progress.setRange(0, maximum)
            progress.setValue(count)
            progress.setTextVisible(False)
            progress.setFixedHeight(10)
            progress.setStyleSheet('QProgressBar {border: 0; background: palette(alternate-base);} QProgressBar::chunk {background: palette(highlight);}')
            row.addWidget(progress, 3)
            number = plain(f'{count:,}')
            number.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            number.setMinimumWidth(40)
            row.addWidget(number)
            for child in (label, progress, number):
                child.setAttribute(Qt.WA_TransparentForMouseEvents)
            button.clicked.connect(lambda checked=False, value=key: self.activated.emit(value))
            self.layout.addWidget(button)
            self.buttons.append(button)
        if len(self.entries) > limit:
            self.layout.addWidget(plain(f'Showing {limit} of {len(self.entries):,} categories. All categories are available in the book filters.'))


class CoverTableModel(QAbstractTableModel):
    def __init__(self, artwork, parent=None):
        super().__init__(parent)
        self.rows = ()
        self.artwork = artwork
        self._art_rows = defaultdict(list)
        self.artwork.changed.connect(self._cover_changed)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = tuple(rows)
        self._art_rows.clear()
        for index, row in enumerate(self.rows):
            self._art_rows[(row.path, row.artwork_key)].append(index)
        self.endResetModel()

    def _cover_changed(self, key):
        for row in self._art_rows.get(key, ()):
            self.dataChanged.emit(self.index(row, 0), self.index(row, 0), [Qt.DecorationRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None


class CollectionBookModel(CoverTableModel):
    HEADERS = ('Audiobook', 'Author', 'Series', 'Finished', 'My rating', 'Genres', 'Metadata attention', 'Release year', 'Purchased on', 'Bought from')

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = index.column()
        if role == Qt.DecorationRole and column == 0:
            return self.artwork.icon(row.path, row.artwork_key)
        if role == Qt.DisplayRole:
            return (row.title, row.author, row.series, 'Yes' if row.completed else 'No recorded finish',
                    rating_text(row.rating),
                    '; '.join(row.genres) or 'Unknown', '; '.join(row.issues) or 'None recorded',
                    str(row.release_year) if row.release_year else 'Unknown',
                    row.purchased_on or 'Not recorded', row.purchase_source or 'Not recorded')[column]
        if role == Qt.UserRole:
            return (row.title.casefold(), row.author.casefold(), row.series.casefold(),
                    int(row.completed), row.rating, '; '.join(row.genres).casefold(), len(row.issues),
                    row.release_year or 0, row.purchased_on or '9999-12-31', row.purchase_source.casefold())[column]
        if role == Qt.ToolTipRole:
            return '\n'.join((row.title, row.author, row.path, 'Owned for: ' + owned_for(row.purchased_on),
                              'Bought from: ' + (row.purchase_source or 'Not recorded'), '; '.join(row.issues)))
        return None


class SeriesModel(QAbstractTableModel):
    HEADERS = ('Series', 'Owned / expected', 'Finished / owned', 'Missing books', 'My average', 'Status')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = tuple(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        status = ('Owned and finished' if row.listening_complete else 'All owned; listening incomplete' if row.ownership_complete else row.note)
        if row.missing is None:
            missing = 'Unknown'
        elif row.missing == 0:
            missing = '0'
        elif row.missing_numbers is not None:
            positions = ', '.join(map(str, row.missing_numbers[:12]))
            if len(row.missing_numbers) > 12:
                positions += ', ...'
            missing = f'{row.missing:,} (#{positions})'
        else:
            missing = f'{row.missing:,} (numbers unknown)'
        if role == Qt.DisplayRole:
            return (row.name, f'{row.owned:,} / {row.expected if row.expected is not None else "Unknown"}',
                    f'{row.completed:,} / {row.owned:,}', missing,
                    f'{rating_text(row.average_rating)} ({row.rated} rated)' if row.average_rating is not None else 'Unrated', status)[col]
        if role == Qt.UserRole:
            return (row.name.casefold(), row.owned, row.completed,
                    row.missing if row.missing is not None else -1,
                    row.average_rating if row.average_rating is not None else -1, status.casefold())[col]
        if role == Qt.ToolTipRole:
            return f'{row.name}\n{row.author}\n{row.note}\n{row.path}\nDouble-click to see owned books.'
        return None


class RecapModel(QAbstractListModel):
    def __init__(self, artwork, parent=None):
        super().__init__(parent)
        self.artwork = artwork
        self.rows = ()
        self.badges = ()
        self.tile_size = QSize(224, 292)
        self._art_rows = defaultdict(list)
        self.artwork.changed.connect(self._cover_changed)

    def set_rows(self, rows, badges=None):
        self.beginResetModel()
        self.rows = tuple(rows)
        self.badges = tuple(badges or [''] * len(self.rows))
        self._art_rows.clear()
        for index, row in enumerate(self.rows):
            self._art_rows[(row.path, row.artwork_key)].append(index)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.DisplayRole:
            badge = self.badges[index.row()]
            rating = rating_text(row.rating)
            if not row.can_open:
                badge = '\n'.join(part for part in (row.file_status, badge) if part)
            return '\n'.join(part for part in (badge, row.title, row.author, rating) if part)
        if role == Qt.DecorationRole:
            return self.artwork.icon(row.path, row.artwork_key) if row.can_open else self.artwork.placeholder
        if role == Qt.SizeHintRole:
            return self.tile_size
        if role == Qt.ToolTipRole:
            return '\n'.join((row.title, row.author, row.series, row.file_status, row.path))
        if role == Qt.UserRole:
            return row.path
        return None

    def _cover_changed(self, key):
        for row in self._art_rows.get(key, ()):
            self.dataChanged.emit(self.index(row), self.index(row), [Qt.DecorationRole])
