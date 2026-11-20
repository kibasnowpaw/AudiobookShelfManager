"""Native Qt statistics view. Widgets and table models stay on the GUI thread."""
from __future__ import annotations

import math
from datetime import date
from collections import Counter

from PySide6.QtCore import Qt, Signal, QAbstractTableModel, QModelIndex, QRectF, QSortFilterProxyModel, QSize, QSettings, QRect, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QComboBox, QPushButton, QProgressBar, QScrollArea, QTabWidget,
    QTableView, QHeaderView, QAbstractItemView, QToolTip, QSizePolicy,
    QDoubleSpinBox, QListView, QLineEdit, QDialog,
)

from .statistics_data import StatisticsResult, filter_listens, unique_listens, filter_collection, normalized
from .genre_filter_dialog import GenreFilterDialog
from .statistics_artwork import StatisticsArtwork
from .statistics_widgets import CategoryChart, CollectionBookModel, SeriesModel, RecapModel
from .rating_values import rating_text, format_rating, band_label


def _label(text='', *, size=None, bold=False, wrap=False):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(wrap)
    if size or bold:
        font = label.font()
        if size:
            font.setPointSize(size)
        font.setBold(bold)
        label.setFont(font)
    return label


def _hours(seconds):
    return f'{seconds / 3600:,.1f} h'


def _duration(seconds):
    if seconds is None:
        return 'Unknown'
    minutes = int(round(seconds / 60))
    return f'{minutes // 60:,} h {minutes % 60:02} m'


def _panel(title):
    panel = QFrame()
    panel.setObjectName('statisticsPanel')
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(18, 15, 18, 15)
    layout.setSpacing(10)
    layout.addWidget(_label(title, size=12, bold=True))
    return panel, layout


class MetricCard(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName('statisticsPanel')
        self.setMinimumWidth(170)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        layout.addWidget(_label(title, bold=True))
        self.value = _label('\u2014', size=25, bold=True)
        layout.addWidget(self.value)
        self.detail = _label('', wrap=True)
        self.detail.hide()
        layout.addWidget(self.detail)

    def set_value(self, value, detail):
        self.value.setText(value)
        self.detail.setText(detail)
        self.setToolTip(detail)


class Bars(QWidget):
    """Zero-based chart; pointer and keyboard activation open matching books."""
    activated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.unit = 'listens'
        self._regions = []
        self.setMinimumHeight(210)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._focus_index = 0
        self.empty_text = 'No completed listens in this period'
        self.unavailable_indices = set()
        self.setAccessibleName('Statistics chart. Use arrow keys and Enter to show matching books.')

    def set_entries(self, entries, unit):
        self.entries = list(entries)
        self.unit = unit
        self._focus_index = min(self._focus_index, max(0, len(self.entries)-1))
        label_width = max((self.fontMetrics().horizontalAdvance(str(label)) + 12
                           for label, _value, _tip in self.entries), default=48)
        self.setMinimumWidth(max(250, max(48, label_width) * len(self.entries) + 65))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.palette().color(QPalette.Text)
        self._regions = []
        if not self.entries:
            painter.setPen(color)
            painter.drawText(self.rect(), Qt.AlignCenter, self.empty_text)
            return
        plot = QRectF(54, 26, max(1, self.width()-68), max(1, self.height()-62))
        raw_max = max(value for label, value, tip in self.entries)
        maximum = max(1.0, raw_max)
        power = 10 ** math.floor(math.log10(maximum))
        maximum = math.ceil(maximum / power) * power
        for tick in range(5):
            value = maximum * tick / 4
            y = plot.bottom() - plot.height() * tick / 4
            faint = QColor(color)
            faint.setAlpha(35)
            painter.setPen(QPen(faint, 1))
            painter.drawLine(plot.left(), y, plot.right(), y)
            if self.unit != 'hours' and value != int(value):
                continue
            painter.setPen(color)
            text = f'{value:,.1f}' if self.unit == 'hours' and maximum < 10 else f'{value:,.0f}'
            painter.drawText(QRectF(0, y-10, 46, 20), Qt.AlignRight | Qt.AlignVCenter, text)
        width = plot.width() / len(self.entries)
        for index, (label, value, tip) in enumerate(self.entries):
            height = plot.height() * value / maximum
            bar_width = min(64, width * .6)
            bar = QRectF(plot.left() + index*width + (width-bar_width)/2, plot.bottom()-height, bar_width, height)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#559be7'))
            if height:
                painter.drawRoundedRect(bar, 4, 4)
            painter.setPen(color)
            left = plot.left() + index*width
            painter.drawText(QRectF(left, plot.bottom()+8, width, 22), Qt.AlignCenter, label)
            value_text = f'{value:,.1f}' if self.unit == 'hours' else f'{value:,}'
            if index in self.unavailable_indices:
                value_text = '\u2014'
            painter.drawText(QRectF(left, bar.top()-22, width, 20), Qt.AlignCenter, value_text)
            if self.hasFocus() and index == self._focus_index:
                painter.setPen(QPen(self.palette().color(QPalette.Highlight), 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(left+2, plot.bottom()+6, max(1, width-4), 25))
            self._regions.append((QRectF(plot.left()+index*width, 0, width, self.height()), tip))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            for index, (region, tip) in enumerate(self._regions):
                if region.contains(event.position()):
                    self._focus_index = index
                    self.activated.emit(index)
                    self.update()
                    break
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if self.entries and event.key() in (Qt.Key_Left, Qt.Key_Right):
            self._focus_index = (self._focus_index + (1 if event.key() == Qt.Key_Right else -1)) % len(self.entries)
            self.update()
            event.accept()
        elif self.entries and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.activated.emit(self._focus_index)
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        for region, tip in self._regions:
            if region.contains(event.position()):
                QToolTip.showText(event.globalPosition().toPoint(), tip, self)
                return
        QToolTip.hideText()

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class HistoryModel(QAbstractTableModel):
    HEADERS = ('Finished', 'Started', 'Audiobook', 'Author', 'Narrator', 'Audio length', 'Days', 'Listen', 'Rating', 'File')

    def __init__(self, parent=None, artwork=None):
        super().__init__(parent)
        self.rows = ()
        self.artwork = artwork
        self._cover_rows = {}
        if artwork is not None:
            artwork.changed.connect(self._cover_changed)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self._cover_rows = {}
        for index, row in enumerate(rows):
            self._cover_rows.setdefault((row.path, row.artwork_key), []).append(index)
        self.endResetModel()

    def _cover_changed(self, key):
        for row in self._cover_rows.get(key, ()):
            self.dataChanged.emit(self.index(row, 2), self.index(row, 2), [Qt.DecorationRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = index.column()
        if role == Qt.DecorationRole and column == 2 and self.artwork is not None:
            return self.artwork.icon(row.path, row.artwork_key) if row.can_open else self.artwork.placeholder
        if role == Qt.DisplayRole:
            title = row.title if row.can_open else f'[{row.file_status}] {row.title}'
            return (row.finished_on, row.started_on, title, row.author, row.narrator,
                    _duration(row.seconds), str(row.days), 'Relisten' if row.relisten else 'First listen',
                    rating_text(row.rating), row.file_status)[column]
        if role == Qt.UserRole:
            return (row.finished_on, row.started_on, row.title.casefold(), row.author.casefold(),
                    row.narrator.casefold(), row.seconds if row.seconds is not None else -1,
                    row.days, int(row.relisten), row.rating, row.file_status)[column]
        if role == Qt.ToolTipRole:
            return '\n'.join(part for part in (row.title, row.author, row.note, row.file_status, row.path) if part)
        if role == Qt.TextAlignmentRole and column in (5, 6, 8):
            return Qt.AlignRight | Qt.AlignVCenter
        return None


class StatisticsPage(QWidget):
    refresh_requested = Signal()
    open_book_requested = Signal(str)
    remove_book_requested = Signal(object)
    artwork_idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result: StatisticsResult | None = None
        self._columns = 0
        self._close_pending = False
        self._history_updating = False
        self._collection_updating = False
        self._collection_issue = None
        self.collection_genres = ()
        self._browse_focus_pending = False
        self._split_layouts = []
        self.artwork = StatisticsArtwork(self)
        self.artwork.idle.connect(self._on_artwork_idle)
        self.setObjectName('statisticsPage')
        self.ensurePolished()
        dark = self.palette().color(QPalette.Window).lightness() < 128
        surface, border = ('#29313d', '#3c4756') if dark else ('#ffffff', '#dce3ed')
        self.setStyleSheet(
            f'QFrame#statisticsPanel {{background: {surface}; border: 1px solid {border}; border-radius: 10px;}}'
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        self.header = QWidget()
        heading = QHBoxLayout(self.header)
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(_label('Statistics', size=23, bold=True))
        heading.addStretch()
        heading.addWidget(_label('Period'))
        self.period = QComboBox()
        self.period.setMinimumWidth(165)
        self.period.addItem(f'This year ({date.today().year})', date.today().year)
        self.period.addItem('All time', None)
        heading.addWidget(self.period)
        self.refresh_button = QPushButton('Refresh')
        heading.addWidget(self.refresh_button)
        root.addWidget(self.header)
        self.description = _label('Listening totals use finish dates and saved audio length at 1\u00d7. Collection shows the current scan and lifetime completion.', wrap=True)
        root.addWidget(self.description)
        self.description.hide()
        self.notice = _label('', wrap=True)
        root.addWidget(self.notice)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet('QTabWidget::pane {border: 0;}')
        root.addWidget(self.tabs, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        overview = QWidget()
        content = QVBoxLayout(overview)
        content.setContentsMargins(0, 4, 6, 4)
        content.setSpacing(14)
        self.cards_layout = QGridLayout()
        self.cards_layout.setSpacing(12)
        self.cards = [MetricCard(title) for title in (
            'Completed listens', 'Different books', 'Relistens', 'Audio hours', 'Average finish time', 'Average rating')]
        content.addLayout(self.cards_layout)
        self._arrange_cards()

        speed_row = QHBoxLayout()
        speed_row.addWidget(_label('Estimated listening time'))
        self.estimated_time = _label('', bold=True)
        speed_row.addWidget(self.estimated_time)
        speed_row.addWidget(_label('at'))
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.5, 4)
        self.speed.setSingleStep(.25)
        self.speed.setDecimals(2)
        self.speed.setSuffix('\u00d7')
        self.speed.setToolTip('Estimate only: known audio length divided by this speed. Playback is not measured.')
        self._settings = QSettings('KibaSnowpaw', 'AudiobookShelfManager')
        try:
            speed = float(self._settings.value('statistics/playback_speed', 2.0))
            self.speed.setValue(speed if math.isfinite(speed) else 2.0)
        except (TypeError, ValueError):
            self.speed.setValue(2.0)
        speed_row.addWidget(self.speed)
        speed_row.addStretch()
        content.addLayout(speed_row)

        panel, layout = _panel('Listening over time')
        row = QHBoxLayout()
        self.chart_title = _label('Books finished each month', bold=True)
        row.addWidget(self.chart_title)
        row.addStretch()
        self.metric_selector = QComboBox()
        self.metric_selector.addItem('Completed listens', 'listens')
        self.metric_selector.addItem('Audio hours', 'hours')
        row.addWidget(self.metric_selector)
        layout.addLayout(row)
        self.timeline = Bars()
        chart_scroll = QScrollArea()
        chart_scroll.setWidgetResizable(True)
        chart_scroll.setFrameShape(QFrame.NoFrame)
        chart_scroll.setMinimumHeight(238)
        chart_scroll.setWidget(self.timeline)
        layout.addWidget(chart_scroll)
        self.timeline.setToolTip('Grouped by finish date. Zero means no completions; a dash means a future period or unknown length.')
        content.addWidget(panel)

        collection_panel, collection_layout = _panel('Current collection')
        self.collection_text = _label('', wrap=True)
        collection_layout.addWidget(self.collection_text)
        content.addWidget(collection_panel)

        middle = QGridLayout()
        ratings_panel, ratings_layout = _panel('Your ratings')
        self.rating_chart = Bars()
        ratings_layout.addWidget(self.rating_chart)
        self.rating_note = _label('', wrap=True)
        ratings_layout.addWidget(self.rating_note)
        middle.addWidget(ratings_panel, 0, 0)
        highlights, highlight_layout = _panel('Longest and shortest audiobooks')
        self.longest = _label('', wrap=True)
        self.shortest = _label('', wrap=True)
        highlight_layout.addWidget(self.longest)
        highlight_layout.addWidget(self.shortest)
        highlight_layout.addStretch()
        highlights.setToolTip('Saved audio length at 1x; unknown lengths are excluded.')
        middle.addWidget(highlights, 0, 1)
        self._split_layouts.append((middle, ratings_panel, highlights))
        content.addLayout(middle)
        profile_panel, profile_layout = _panel('Explore your listening')
        profile_row = QHBoxLayout()
        profile_row.addWidget(_label('Group by'))
        self.profile_selector = QComboBox()
        for title, key in (('Genre tags', 'genre'), ('Authors', 'author'), ('Narrators', 'narrator')):
            self.profile_selector.addItem(title, key)
        profile_row.addWidget(self.profile_selector)
        self.profile_count = QComboBox()
        self.profile_count.addItem('Different books', 'books')
        self.profile_count.addItem('Completed listens', 'listens')
        self.profile_count.setEnabled(False)
        profile_row.addWidget(self.profile_count)
        profile_row.addStretch()
        profile_layout.addLayout(profile_row)
        self.profile_chart = CategoryChart()
        profile_layout.addWidget(self.profile_chart)
        self.profile_note = _label('', wrap=True)
        profile_layout.addWidget(self.profile_note)
        content.addWidget(profile_panel)
        profile_panel.setToolTip('Click a chart to see matching books.')
        content.addStretch()
        scroll.setWidget(overview)
        self.tabs.addTab(scroll, 'Listening')

        history = QWidget()
        history_layout = QVBoxLayout(history)
        self._build_history_filters(history_layout)
        self.history_note = _label('', wrap=True)
        history_layout.addWidget(self.history_note)
        self.history_model = HistoryModel(self, artwork=self.artwork)
        self.history_sort = QSortFilterProxyModel(self)
        self.history_sort.setSourceModel(self.history_model)
        self.history_sort.setSortRole(Qt.UserRole)
        self.history_table = QTableView()
        self.history_table.setModel(self.history_sort)
        self.history_table.setSortingEnabled(True)
        self.history_table.sortByColumn(0, Qt.DescendingOrder)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.verticalHeader().hide()
        self.history_table.setWordWrap(False)
        self.history_table.setIconSize(QSize(44, 44))
        self.history_table.verticalHeader().setDefaultSectionSize(52)
        self.history_table.activated.connect(self._open_history_book)
        self.history_table.selectionModel().currentChanged.connect(self._update_history_open)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        for column, width in enumerate((105, 105, 240, 150, 150, 115, 60, 105, 135, 135)):
            self.history_table.setColumnWidth(column, width)
        history_layout.addWidget(self.history_table, 1)
        self.history_filters['availability'].setToolTip('Available files are shown by default. Saved history includes missing files. Use Remove book to erase saved data.')
        self.tabs.addTab(history, 'Listening history')
        self._build_collection()
        self._build_recap()

        self.period.currentIndexChanged.connect(lambda: self.refresh_requested.emit())
        self.refresh_button.clicked.connect(lambda: self.refresh_requested.emit())
        self.metric_selector.currentIndexChanged.connect(self._update_chart)
        self.timeline.activated.connect(self._choose_timeline)
        self.rating_chart.activated.connect(self._choose_rating)
        self.profile_selector.currentIndexChanged.connect(self._update_profile)
        self.profile_count.currentIndexChanged.connect(self._update_profile)
        self.profile_chart.activated.connect(self._choose_profile)
        self.speed.valueChanged.connect(self._update_estimate)
        self.tabs.currentChanged.connect(self._tab_changed)

    def selected_year(self):
        return self.period.currentData()

    def use_external_navigation(self):
        self.header.hide()
        self.tabs.tabBar().hide()

    def _arrange_cards(self):
        columns = 6 if self.width() >= 1380 else 3 if self.width() >= 820 else 2
        if columns != self._columns:
            self._columns = columns
            for grid, cards in [(self.cards_layout, self.cards)] + ([(self.collection_cards_layout, self.collection_cards)] if hasattr(self, 'collection_cards') else []):
                for card in cards:
                    grid.removeWidget(card)
                for column in range(6):
                    grid.setColumnStretch(column, 1 if column < columns else 0)
                for index, card in enumerate(cards):
                    grid.addWidget(card, index // columns, index % columns)
        for grid, first, second in self._split_layouts:
            grid.removeWidget(first)
            grid.removeWidget(second)
            grid.addWidget(first, 0, 0)
            grid.addWidget(second, 0 if self.width() >= 1120 else 1, 1 if self.width() >= 1120 else 0)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1 if self.width() >= 1120 else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_cards()
        self._resize_history_title()
        if hasattr(self, 'recap_gallery'):
            QTimer.singleShot(0, self._fit_recap_tiles)

    def _resize_history_title(self):
        if hasattr(self, 'history_table'):
            header = self.history_table.horizontalHeader()
            other = sum(header.sectionSize(column) for column in range(header.count()) if column != 2)
            # Keep the title and missing-file label readable on small windows;
            # additional columns can be reached with the horizontal scrollbar.
            header.resizeSection(2, max(320, self.history_table.viewport().width() - other))

    def set_loading(self):
        self.notice.setText('Loading statistics, please wait...')
        self.notice.show()
        self.progress.show()
        self.tabs.setEnabled(False)
        self.refresh_button.setEnabled(False)

    def show_error(self, message):
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.tabs.setEnabled(False)
        self.notice.setText(f'Could not load statistics: {message}\nClick Refresh to try again.')
        self.notice.show()

    def show_result(self, result):
        self.result = result
        self.artwork.refresh()
        selected = result.year
        self.period.blockSignals(True)
        self.period.clear()
        years = sorted(set(result.years) | {date.today().year} | ({selected} if selected is not None else set()), reverse=True)
        for year in years:
            self.period.addItem(f'This year ({year})' if year == date.today().year else str(year), year)
        self.period.addItem('All time', None)
        self.period.setCurrentIndex(self.period.findData(selected))
        self.period.blockSignals(False)
        self.progress.hide()
        self.tabs.setEnabled(True)
        self.refresh_button.setEnabled(True)
        count = len(result.listens)
        notice = '' if count else 'No completed listens in this period.'
        if result.excluded_entries:
            notice += f' {result.excluded_entries:,} invalid entries excluded.'
        future_count = sum(row.finished_on > date.today().isoformat() for row in result.listens)
        if future_count:
            notice += f' {future_count:,} future-dated finishes.'
        unavailable = sum(not row.can_open for row in unique_listens(result.listens))
        if unavailable:
            noun = 'file' if unavailable == 1 else 'files'
            notice += f' {unavailable:,} unavailable {noun} | history kept.'
        self.notice.setToolTip('Missing files remain in saved totals. The Files filter shows them; Remove book erases their saved data. Rescan after moving files.')
        self.notice.setText(notice.strip())
        self.notice.setVisible(bool(notice.strip()) and self.tabs.currentIndex() != 2)
        self.cards[0].set_value(f'{count:,}', 'Every completed listen counts')
        self.cards[1].set_value(f'{result.unique_books:,}', 'Each audiobook counted once')
        self.cards[2].set_value(f'{result.relistens:,}', 'Includes books heard in earlier years')
        unknown = count-result.known_durations
        hours = _hours(result.audio_seconds) if result.known_durations or not count else '\u2014'
        self.cards[3].set_value(hours, f'{unknown:,} listens have unknown length' if unknown else 'Saved audio length at 1\u00d7 speed')
        days = f'{result.average_days:,.1f} days' if result.average_days is not None else '\u2014'
        self.cards[4].set_value(days, 'Calendar days per completed listen')
        rated = sum(result.rating_counts)
        rating = f'\u2605 {format_rating(result.average_rating)}/5' if result.average_rating is not None else '\u2014'
        self.cards[5].set_value(rating, f'{rated:,} rated books finished in this period')
        self.rating_chart.set_entries([(band_label(star), count, f'{band_label(star)} out of 5: {count:,} different books')
                                       for star, count in enumerate(result.rating_counts, 1)] + [('Unrated', result.unique_books-rated, f'{result.unique_books-rated:,} unrated books')], 'books')
        self.rating_note.setText(f'{rated:,} rated | {result.unique_books-rated:,} unrated | {result.favorite_books:,} favorites')
        for label, name, book in ((self.longest, 'Longest', result.longest), (self.shortest, 'Shortest', result.shortest)):
            label.setText(f'{name}\n{book.title}\n{book.author}  \u00b7  {_duration(book.seconds)}' if book else f'{name}\nNo known audio lengths')
        current = result.collection
        self.collection_text.setText(
            f'{current.books:,} audiobooks  \u00b7  {current.series:,} series  \u00b7  {current.authors:,} authors\n'
            f'{current.completed:,} finished at least once  \u00b7  {current.books-current.completed:,} with no recorded finish  \u00b7  {current.rated:,} rated  \u00b7  {current.favorites:,} favorites\n'
            'Current collection; lifetime completion.'
            if current.available else 'Collection totals will appear after a library scan. Saved listening history is available independently.'
        )
        self.history_note.setText(f'{count:,} completed listens \u00b7 {result.unique_books:,} different books. Click a column heading to sort.')
        self.tabs.setTabText(1, f'Listening history ({count:,})')
        self._populate_history_filters()
        self._apply_history_filters()
        self._update_collection()
        self._update_recap()
        self._update_estimate()
        self._update_profile()
        self._update_chart()

    def _update_chart(self):
        result = self.result
        if result is None:
            return
        metric = self.metric_selector.currentData()
        unit = 'month' if result.year is not None else 'year'
        self.chart_title.setText(f'Audio hours by finish {unit}' if metric == 'hours' else f'Books finished each {unit}')
        self.timeline.unavailable_indices = {
            index for index, period in enumerate(result.periods)
            if (result.year == date.today().year and index >= date.today().month and not period.listens)
            or (metric == 'hours' and period.listens and not period.known_durations)
        }
        self.timeline.set_entries([
            (period.label, period.seconds / 3600 if metric == 'hours' else period.listens,
             f'{period.label}: {period.listens:,} listens, {period.relistens:,} relistens\n'
             f'{_hours(period.seconds)} known audio length; {period.listens-period.known_durations:,} unknown lengths')
            for period in result.periods], metric)

    def _build_history_filters(self, layout):
        grid = QGridLayout()
        self.history_filters = {}
        for index, (key, title) in enumerate((('genre', 'Genre'), ('author', 'Author'), ('narrator', 'Narrator'), ('rating', 'My rating'), ('availability', 'Files'))):
            combo = QComboBox()
            combo.setMinimumContentsLength(14)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            if key == 'availability':
                combo.addItem('Available files', 'available')
                combo.addItem('All saved history', None)
                combo.addItem('Unavailable files', 'unavailable')
            else:
                combo.addItem('All', None)
            self.history_filters[key] = combo
            grid.addWidget(_label(title), index // 2, (index % 2) * 2)
            grid.addWidget(combo, index // 2, (index % 2) * 2 + 1)
            combo.currentIndexChanged.connect(self._apply_history_filters)
        layout.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(_label('Finished in'))
        self.history_period = QComboBox()
        self.history_period.addItem('All', None)
        self.history_period.currentIndexChanged.connect(self._apply_history_filters)
        row.addWidget(self.history_period)
        self.history_mode = QComboBox()
        self.history_mode.addItem('Completed listens', False)
        self.history_mode.addItem('Different books', True)
        self.history_mode.currentIndexChanged.connect(self._apply_history_filters)
        row.addWidget(self.history_mode)
        self.history_reset = QPushButton('Clear filters')
        self.history_reset.clicked.connect(self._clear_history_filters)
        row.addWidget(self.history_reset)
        self.history_open = QPushButton('Open book')
        self.history_open.setEnabled(False)
        self.history_open.clicked.connect(lambda: self._open_history_book(self.history_table.currentIndex()))
        row.addWidget(self.history_open)
        self.history_remove = QPushButton('Remove book...')
        self.history_remove.setEnabled(False)
        self.history_remove.clicked.connect(self._remove_history_book)
        row.addWidget(self.history_remove)
        row.addStretch()
        layout.addLayout(row)

    def _fill_combo(self, combo, choices, all_text='All'):
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_text, None)
        for text, key in choices:
            combo.addItem(text, key)
        found = combo.findData(previous)
        combo.setCurrentIndex(found if found >= 0 else 0)
        combo.blockSignals(False)

    def _populate_history_filters(self):
        result = self.result
        self._history_updating = True
        try:
            self._fill_combo(self.history_filters['genre'], [(entry.name or 'Unknown', entry.name) for entry in sorted(result.genres, key=lambda item: item.name.casefold())])
            self._fill_combo(self.history_filters['author'], [(entry.name, '' if entry.name == 'Unknown author' else entry.name) for entry in sorted(result.authors, key=lambda item: item.name.casefold())])
            self._fill_combo(self.history_filters['narrator'], [(entry.name, '' if entry.name == 'Unknown narrator' else entry.name) for entry in sorted(result.narrators, key=lambda item: item.name.casefold())])
            bands = [(f'{band_label(star)} out of 5', f'band:{star}') for star in range(1, 6)]
            exact = [(f'Exactly {rating_text(value)}', value) for value in sorted({row.rating for row in result.listens if row.rating})]
            self._fill_combo(self.history_filters['rating'], bands + [('Unrated', 0)] + exact)
            self._fill_combo(self.history_period, [(entry.label, index+1 if result.year is not None else int(entry.label)) for index, entry in enumerate(result.periods)], 'All months' if result.year is not None else 'All years')
        finally:
            self._history_updating = False

    def _clear_history_filters(self, _checked=False, *, all_files=False):
        self._history_updating = True
        for combo in self.history_filters.values():
            combo.setCurrentIndex(0)
        if all_files:
            combo = self.history_filters['availability']
            combo.setCurrentIndex(combo.findData(None))
        self.history_period.setCurrentIndex(0)
        self.history_mode.setCurrentIndex(0)
        self._history_updating = False
        self._apply_history_filters()

    def _apply_history_filters(self, *_args):
        if self.result is None or self._history_updating or not hasattr(self, 'history_model'):
            return
        values = {key: combo.currentData() for key, combo in self.history_filters.items()}
        distinct = bool(self.history_mode.currentData())
        rows = filter_listens(self.result, **values, period=self.history_period.currentData(), distinct=distinct)
        self.history_model.set_rows(rows)
        self._update_history_open()
        active = [f'{"Files" if key == "availability" else key}: {combo.currentText()}'
                  for key, combo in self.history_filters.items() if combo.currentData() is not None]
        if self.history_period.currentData() is not None:
            active.append(self.history_period.currentText())
        scope = str(self.result.year) if self.result.year is not None else 'All time'
        mode = ('different book' if len(rows) == 1 else 'different books') if distinct else ('completed listen' if len(rows) == 1 else 'completed listens')
        note = f'{scope}: {len(rows):,} {mode}'
        if active:
            note += ' | ' + '; '.join(active)
        self.history_note.setText(note)
        self.tabs.setTabText(1, f'Listening history ({len(rows):,})')

    def _update_history_open(self, *_args):
        source = self.history_sort.mapToSource(self.history_table.currentIndex())
        row = self.history_model.rows[source.row()] if source.isValid() else None
        self.history_open.setEnabled(row is not None and row.can_open)
        self.history_remove.setEnabled(row is not None)
        self.history_open.setToolTip('Open Book Details' if row is not None and row.can_open else
                                    'Select a book with an available file. Saved records for unavailable files cannot open Book Details.')

    def _remove_history_book(self):
        source = self.history_sort.mapToSource(self.history_table.currentIndex())
        if source.isValid():
            self.remove_book_requested.emit(self.history_model.rows[source.row()])

    def _open_history_book(self, index):
        if not index.isValid():
            return
        source = self.history_sort.mapToSource(index)
        row = self.history_model.rows[source.row()]
        if row.can_open:
            self.open_book_requested.emit(row.path)

    def _choose_timeline(self, index):
        if self.result is None or not 0 <= index < len(self.result.periods):
            return
        self._clear_history_filters(all_files=True)
        key = index + 1 if self.result.year is not None else int(self.result.periods[index].label)
        self.history_period.setCurrentIndex(self.history_period.findData(key))
        self.tabs.setCurrentIndex(1)

    def _choose_rating(self, index):
        if self.result is None:
            return
        self._clear_history_filters(all_files=True)
        star = f'band:{index+1}' if index < 5 else 0
        self.history_filters['rating'].setCurrentIndex(self.history_filters['rating'].findData(star))
        self.history_mode.setCurrentIndex(1)
        self.tabs.setCurrentIndex(1)

    def _choose_profile(self, key):
        self._clear_history_filters(all_files=True)
        field = self.profile_selector.currentData()
        self.history_filters[field].setCurrentIndex(self.history_filters[field].findData(key))
        self.history_mode.setCurrentIndex(0 if self.profile_count.currentData() == 'listens' and field != 'genre' else 1)
        self.tabs.setCurrentIndex(1)

    def _update_profile(self):
        if self.result is None:
            return
        kind = self.profile_selector.currentData()
        self.profile_count.setEnabled(kind != 'genre')
        if kind == 'genre':
            self.profile_count.blockSignals(True)
            self.profile_count.setCurrentIndex(0)
            self.profile_count.blockSignals(False)
            entries = [(entry.name, entry.name or 'Unknown', entry.books) for entry in self.result.genres]
            self.profile_note.setText('Different books; multiple tags can apply.')
        else:
            people = self.result.authors if kind == 'author' else self.result.narrators
            unknown = 'Unknown author' if kind == 'author' else 'Unknown narrator'
            metric = self.profile_count.currentData()
            entries = [('' if person.name == unknown else person.name, person.name, getattr(person, metric)) for person in sorted(people, key=lambda person: (-getattr(person, metric), person.name.casefold()))]
            self.profile_note.setText('Includes shared credits; click a bar to see books.')
        self.profile_chart.set_entries(entries, unit=self.profile_count.currentData())

    def _update_estimate(self, *_args):
        if self.result is None:
            return
        self._settings.setValue('statistics/playback_speed', self.speed.value())
        if self.result.listens and not self.result.known_durations:
            text = 'Unknown'
        else:
            text = _duration(self.result.audio_seconds / self.speed.value())
        unknown = len(self.result.listens) - self.result.known_durations
        self.estimated_time.setText(text + (f' ({unknown:,} unknown lengths excluded)' if unknown else ''))
        self.estimated_time.setToolTip('Estimated from known audio lengths only. Actual playback time and changes of speed are not recorded.')

    def _new_table(self, model, widths, stretch=0):
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.UserRole)
        table = QTableView()
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.Stretch)
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)
        return table, proxy

    def _build_collection(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        self.collection_notice = _label('', wrap=True)
        layout.addWidget(self.collection_notice)
        self.collection_cards_layout = QGridLayout()
        self.collection_cards = [MetricCard(title) for title in ('Owned books', 'Finished at least once', 'No recorded finish', 'Known missing books', 'All books owned', 'All books finished')]
        layout.addLayout(self.collection_cards_layout)
        series_panel, series_layout = _panel('Series ownership and listening progress')
        self.series_filter = QComboBox()
        for label, key in (('All series', 'all'), ('Missing books', 'missing'), ('Unknown or conflicting totals', 'unknown'), ('All books owned', 'owned'), ('All books finished', 'finished')):
            self.series_filter.addItem(label, key)
        self.series_filter.currentIndexChanged.connect(self._filter_series)
        series_layout.addWidget(self.series_filter)
        self.series_model = SeriesModel(self)
        self.series_table, self.series_sort = self._new_table(self.series_model, (230, 135, 140, 205, 150, 250))
        self.series_table.setMinimumHeight(210)
        self.series_table.activated.connect(self._choose_series)
        series_layout.addWidget(self.series_table)
        self.series_table.setToolTip('Double-click to see books. Unknown totals or conflicting numbering are not treated as complete.')
        layout.addWidget(series_panel)
        issues_panel, issues_layout = _panel('Metadata needing attention')
        self.issue_chart = CategoryChart()
        self.issue_chart.activated.connect(self._choose_issue)
        issues_layout.addWidget(self.issue_chart)
        self.issue_chart.setToolTip('Click to see affected books. Each book may have several issues.')
        layout.addWidget(issues_panel)
        books_panel, books_layout = _panel('Books in your collection')
        filters = QGridLayout()
        self.collection_search = QLineEdit()
        self.collection_search.setPlaceholderText('Search title, author, series or narrator...')
        self.collection_search.setClearButtonEnabled(True)
        self.collection_search.textChanged.connect(self._filter_collection)
        self.collection_search.setAccessibleName('Find an audiobook in this library')
        filters.addWidget(self.collection_search, 0, 0, 1, 2)
        self.collection_genre_button = QPushButton('Genres: All')
        self.collection_genre_button.clicked.connect(self._pick_collection_genres)
        filters.addWidget(self.collection_genre_button, 0, 2)
        self.collection_genre_match = QComboBox()
        self.collection_genre_match.addItem('Match all genres', 'all')
        self.collection_genre_match.addItem('Match any genre', 'any')
        self.collection_genre_match.currentIndexChanged.connect(self._filter_collection)
        self.collection_genre_match.setToolTip('All requires every selected genre; Any accepts at least one.')
        filters.addWidget(self.collection_genre_match, 0, 3)
        self.collection_status = QComboBox()
        for label, key in (('All books', 'all'), ('No recorded finish', 'unfinished'), ('Finished at least once', 'finished'), ('Unrated', 'unrated'), ('Favorites', 'favorites'), ('Metadata attention', 'issues'), ('Missing purchase date', 'missing_purchase_date'), ('Missing purchase source', 'missing_purchase_source')):
            self.collection_status.addItem(label, key)
        self.collection_status.currentIndexChanged.connect(self._filter_collection)
        filters.addWidget(self.collection_status, 1, 0)
        self.collection_series = QComboBox()
        self.collection_series.addItem('All series', None)
        self.collection_year = QComboBox()
        self.collection_year.addItem('All release years', None)
        self.collection_source = QComboBox()
        self.collection_source.addItem('All purchase sources', None)
        for column, combo in enumerate((self.collection_series, self.collection_year, self.collection_source), 1):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(14)
            combo.currentIndexChanged.connect(self._filter_collection)
            filters.addWidget(combo, 1, column)
        books_layout.addLayout(filters)
        actions = QHBoxLayout()
        self.collection_reset = QPushButton('Clear filters')
        self.collection_reset.clicked.connect(self._clear_collection)
        actions.addWidget(self.collection_reset)
        self.collection_oldest = QPushButton('Oldest purchases first')
        self.collection_oldest.clicked.connect(self._sort_oldest_purchases)
        actions.addWidget(self.collection_oldest)
        actions.addStretch()
        collection_open = QPushButton('Open book')
        collection_open.clicked.connect(lambda: self._open_collection_book(self.collection_table.currentIndex()))
        actions.addWidget(collection_open)
        self.collection_remove = QPushButton('Remove book...')
        self.collection_remove.setEnabled(False)
        self.collection_remove.clicked.connect(self._remove_collection_book)
        actions.addWidget(self.collection_remove)
        books_layout.addLayout(actions)
        self.collection_book_note = _label('', wrap=True)
        books_layout.addWidget(self.collection_book_note)
        self.collection_model = CollectionBookModel(self.artwork, self)
        self.collection_table, self.collection_sort = self._new_table(self.collection_model, (300, 170, 170, 145, 110, 200, 240, 100, 115, 150))
        self.collection_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.collection_table.setColumnWidth(0, 300)
        self.collection_table.setIconSize(QSize(44, 44))
        self.collection_table.verticalHeader().setDefaultSectionSize(52)
        self.collection_table.setMinimumHeight(310)
        self.collection_table.activated.connect(self._open_collection_book)
        self.collection_table.selectionModel().currentChanged.connect(self._update_collection_remove)
        books_layout.addWidget(self.collection_table)
        layout.insertWidget(2, books_panel)
        years_panel, years_layout = _panel('Owned books by release year')
        self.release_year_chart = Bars()
        self.release_year_chart.empty_text = 'No owned books in this library'
        self.release_year_chart.activated.connect(self._choose_release_year)
        year_scroll = QScrollArea()
        year_scroll.setWidgetResizable(True)
        year_scroll.setFrameShape(QFrame.NoFrame)
        year_scroll.setMinimumHeight(238)
        year_scroll.setWidget(self.release_year_chart)
        years_layout.addWidget(year_scroll)
        self.release_year_note = _label('', wrap=True)
        years_layout.addWidget(self.release_year_note)
        layout.insertWidget(3, years_panel)
        scroll.setWidget(container)
        self.tabs.addTab(scroll, 'Collection')
        self._columns = 0
        self._arrange_cards()

    def _update_collection(self):
        result = self.result
        current = result.collection
        self.collection_notice.setText('Current collection | lifetime completion' if current.available else 'Run a library scan to populate collection statistics. Saved listening history remains available.')
        values = (
            (current.books, 'Available audiobooks from the latest scan'),
            (current.completed, 'Unique books across all listening history'),
            (current.books-current.completed, 'May include books currently in progress'),
            (current.missing, f'{current.uncertain_gaps:,} series with uncertain gaps'),
            (current.ownership_complete, 'Series with complete recorded ownership'),
            (current.listening_complete, 'Series owned and finished in full'),
        )
        for card, (value, note) in zip(self.collection_cards, values):
            card.set_value(f'{value:,}' if current.available else '\u2014', note)
        self._collection_updating = True
        self._fill_combo(self.collection_series, [(row.name + ' - ' + row.author, row.path) for row in result.series], 'All series')
        years = [(entry.name, 0 if entry.name == 'Unknown' else int(entry.name)) for entry in result.release_years]
        self._fill_combo(self.collection_year, years, 'All release years')
        sources = {}
        for book in result.collection_books:
            sources.setdefault(normalized(book.purchase_source), book.purchase_source)
        self._fill_combo(self.collection_source, [(label or 'Not recorded', label)
                         for key, label in sorted(sources.items())], 'All purchase sources')
        self._collection_updating = False
        self.release_year_chart.empty_text = ('No owned books in this library' if current.available
                                              else 'Scan this library to count release years')
        self.release_year_chart.set_entries([(entry.name, entry.books, f'{entry.name}: {entry.books} owned books')
                                             for entry in result.release_years], 'books')
        self.release_year_note.setText(
            f'{current.books:,} owned books. Uses the saved Year tag; missing or invalid years are Unknown. Click a bar to find its books.'
            if current.available else 'Release-year totals will appear after a library scan.')
        self.issue_chart.set_entries([(issue.name, issue.name, issue.books) for issue in result.issues])
        if not result.issues and current.available:
            self.issue_chart.layout.itemAt(0).widget().setText('No metadata issues recorded in the scan.')
        self._filter_series()
        self._filter_collection()
        if self._browse_focus_pending:
            self._browse_focus_pending = False
            QTimer.singleShot(0, self._focus_collection_browser)

    def _filter_series(self):
        if self.result is None:
            return
        selected = self.series_filter.currentData()
        rows = [row for row in self.result.series if selected == 'all'
                or selected == 'missing' and row.missing is not None and row.missing > 0
                or selected == 'unknown' and row.missing is None
                or selected == 'owned' and row.ownership_complete
                or selected == 'finished' and row.listening_complete]
        self.series_model.set_rows(rows)

    def _filter_collection(self):
        if self.result is None or self._collection_updating:
            return
        rows = filter_collection(self.result, mode=self.collection_status.currentData(),
            series=self.collection_series.currentData(), issue=self._collection_issue,
            query=self.collection_search.text(), genres=self.collection_genres,
            genre_match=self.collection_genre_match.currentData(), year=self.collection_year.currentData(),
            source=self.collection_source.currentData())
        self.collection_model.set_rows(rows)
        count = len(self.collection_genres)
        self.collection_genre_button.setText(f'Genres: {count} selected' if count else 'Genres: All')
        self.collection_genre_button.setToolTip(', '.join(tag or 'No genre tags' for tag in self.collection_genres) or 'Choose one or more genres')
        self.collection_genre_match.setEnabled(count > 1)
        details = []
        if self._collection_issue:
            details.append(self._collection_issue)
        if count:
            details.append(('All: ' if self.collection_genre_match.currentData() == 'all' else 'Any: ')
                           + ', '.join(tag or 'No genre tags' for tag in self.collection_genres))
        self.collection_book_note.setText(f'{len(rows):,} of {len(self.result.collection_books):,} books'
                                          + (' | ' + ' | '.join(details) if details else ''))

    def _pick_collection_genres(self):
        if self.result is None:
            return
        counts, names = Counter(), {}
        for book in self.result.collection_books:
            for tag in book.genres or ('',):
                key = normalized(tag)
                names.setdefault(key, tag)
                counts[key] += 1
        dialog = GenreFilterDialog([(names[key], count) for key, count in sorted(counts.items())],
                                   self.collection_genres, self)
        if dialog.exec() == QDialog.Accepted:
            self.collection_genres = dialog.selected_genres()
            self._filter_collection()
        dialog.deleteLater()

    def _sort_oldest_purchases(self):
        self.collection_table.sortByColumn(8, Qt.AscendingOrder)
        self.collection_table.scrollToTop()
        self.collection_table.setFocus()

    def _choose_release_year(self, index):
        if self.result is None or not 0 <= index < len(self.result.release_years):
            return
        label = self.result.release_years[index].name
        self._clear_collection()
        self.collection_year.setCurrentIndex(self.collection_year.findData(0 if label == 'Unknown' else int(label)))
        self._focus_collection_browser()

    def show_collection_browser(self):
        self.tabs.setCurrentIndex(2)
        self._browse_focus_pending = True
        QTimer.singleShot(0, self._focus_collection_browser)

    def _focus_collection_browser(self):
        self.tabs.widget(2).ensureWidgetVisible(self.collection_search)
        self.collection_search.setFocus()

    def _clear_collection(self):
        self._collection_updating = True
        self._collection_issue = None
        self.collection_status.setCurrentIndex(0)
        self.collection_series.setCurrentIndex(0)
        self.collection_year.setCurrentIndex(0)
        self.collection_source.setCurrentIndex(0)
        self.collection_search.clear()
        self.collection_genres = ()
        self.collection_genre_match.setCurrentIndex(0)
        self._collection_updating = False
        self._filter_collection()

    def _choose_issue(self, issue):
        self._clear_collection()
        self._collection_issue = issue
        self._filter_collection()
        self.collection_table.setFocus()
        self.tabs.widget(2).ensureWidgetVisible(self.collection_book_note)

    def _choose_series(self, index):
        if not index.isValid():
            return
        source = self.series_sort.mapToSource(index)
        path = self.series_model.rows[source.row()].path
        self._clear_collection()
        self.collection_series.setCurrentIndex(self.collection_series.findData(path))
        self.collection_table.setFocus()
        self.tabs.widget(2).ensureWidgetVisible(self.collection_book_note)

    def _open_collection_book(self, index):
        if not index.isValid():
            return
        source = self.collection_sort.mapToSource(index)
        self.open_book_requested.emit(self.collection_model.rows[source.row()].path)

    def _update_collection_remove(self, *_args):
        self.collection_remove.setEnabled(self.collection_table.currentIndex().isValid())

    def _remove_collection_book(self):
        source = self.collection_sort.mapToSource(self.collection_table.currentIndex())
        if source.isValid():
            self.remove_book_requested.emit(self.collection_model.rows[source.row()])

    def _cover_view(self, model, height):
        view = QListView()
        view.setModel(model)
        view.setViewMode(QListView.IconMode)
        view.setResizeMode(QListView.Adjust)
        view.setMovement(QListView.Static)
        view.setUniformItemSizes(True)
        view.setIconSize(QSize(200, 200))
        view.setGridSize(QSize(224, 292))
        view.setSpacing(6)
        view.setWordWrap(True)
        view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        view.setMinimumHeight(height)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.activated.connect(lambda index: self._open_recap_book(model, index))
        return view

    def _open_recap_book(self, model, index):
        if index.isValid() and model.rows[index.row()].can_open:
            self.open_book_requested.emit(model.rows[index.row()].path)

    def _update_recap_open(self, *_args):
        index = self.recap_gallery.currentIndex()
        self.recap_open.setEnabled(index.isValid() and self.recap_model.rows[index.row()].can_open)

    def _build_recap(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        self.recap_title = _label('', size=22, bold=True, wrap=True)
        layout.addWidget(self.recap_title)
        self.recap_summary = _label('', size=12, wrap=True)
        layout.addWidget(self.recap_summary)
        self.recap_scope = _label('', wrap=True)
        layout.addWidget(self.recap_scope)
        panel, panel_layout = _panel('Longest and shortest finished audiobooks')
        self.recap_highlight_model = RecapModel(self.artwork, self)
        self.recap_highlights = self._cover_view(self.recap_highlight_model, 316)
        panel_layout.addWidget(self.recap_highlights)
        layout.addWidget(panel)
        gallery_panel, gallery_layout = _panel('Your audiobook covers')
        controls = QHBoxLayout()
        self.recap_selector = QComboBox()
        for text, key in (('My five-star books', 'five'), ('My favorites', 'favorites'), ('All books finished in this period', 'all')):
            self.recap_selector.addItem(text, key)
        self.recap_selector.currentIndexChanged.connect(self._update_recap_gallery)
        controls.addWidget(self.recap_selector)
        self.recap_count = _label('')
        controls.addWidget(self.recap_count)
        controls.addStretch()
        self.recap_open = QPushButton('Open book')
        self.recap_open.setEnabled(False)
        self.recap_open.clicked.connect(lambda: self._open_recap_book(self.recap_model, self.recap_gallery.currentIndex()))
        controls.addWidget(self.recap_open)
        gallery_layout.addLayout(controls)
        self.recap_model = RecapModel(self.artwork, self)
        self.recap_gallery = self._cover_view(self.recap_model, 350)
        self.recap_gallery.selectionModel().currentChanged.connect(self._update_recap_open)
        gallery_layout.addWidget(self.recap_gallery, 1)
        self.recap_empty = _label('', wrap=True)
        gallery_layout.addWidget(self.recap_empty)
        layout.addWidget(gallery_panel, 1)
        self.recap_gallery.setToolTip('Double-click a cover or press Enter to open the book.')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(container)
        self.tabs.addTab(scroll, 'Year recap')

    def _update_recap(self):
        result = self.result
        year = result.year
        self.recap_title.setText(f'Your {year} in audiobooks' if year is not None else 'Your listening highlights - all time')
        rating = rating_text(result.average_rating)
        length = _hours(result.audio_seconds) if result.known_durations or not result.listens else 'Unknown audio length'
        self.recap_summary.setText(
            f'{len(result.listens):,} completed listens | {result.unique_books:,} different books | {result.relistens:,} relistens\n'
            f'{length} at 1\u00d7 | My average: {rating} | {result.favorite_books:,} favorites')
        scope = 'Year in progress.' if year == date.today().year else ''
        if result.authors:
            scope += f'\nTop writer: {result.authors[0].name} ({result.authors[0].listens:,} listens).'
        if result.narrators:
            scope += f' Top narrator: {result.narrators[0].name} ({result.narrators[0].listens:,} listens).'
        self.recap_scope.setText(scope)
        rows, badges = [], []
        for label, book in (('Longest', result.longest), ('Shortest', result.shortest)):
            if book is not None:
                rows.append(book)
                badges.append(f'{label} | {_duration(book.seconds)}')
        self.recap_highlight_model.set_rows(rows, badges)
        self._update_recap_gallery()

    def _update_recap_gallery(self):
        if self.result is None:
            return
        mode = self.recap_selector.currentData()
        matching = [row for row in unique_listens(self.result.listens)
                if mode == 'all' or mode == 'five' and row.rating == 5 or mode == 'favorites' and row.favorite]
        rows = [row for row in matching if row.can_open]
        self.recap_model.set_rows(rows)
        self._update_recap_open()
        missing = len(matching) - len(rows)
        self.recap_count.setText(f'{len(rows):,} different books' +
                                (f' | {missing:,} unavailable files hidden; history is kept' if missing else ''))
        self.recap_empty.setText(('No files are available for this shelf. Saved history and totals are kept.' if matching else
                                 'No books match this shelf in the selected period. Choose another shelf or year.') if not rows else '')
        self.recap_empty.setVisible(not rows)
        QTimer.singleShot(0, self._fit_recap_tiles)

    def _fit_recap_tiles(self):
        for view, model in ((self.recap_highlights, self.recap_highlight_model),
                            (self.recap_gallery, self.recap_model)):
            metrics = QFontMetrics(view.font())
            text_height = max((metrics.boundingRect(QRect(0, 0, 208, 10000),
                               Qt.TextWordWrap, model.index(index).data()).height()
                               for index in range(model.rowCount())), default=60)
            size = QSize(224, 200 + text_height + 30)
            if model.tile_size != size:
                model.tile_size = size
                view.setGridSize(size)
                view.doItemsLayout()
            if view is self.recap_highlights:
                columns = max(1, view.viewport().width() // 224)
                rows = max(1, math.ceil(model.rowCount() / columns))
                view.setFixedHeight(rows * size.height() + 20)
            else:
                view.setMinimumHeight(max(350, size.height() + 20))

    def _tab_changed(self, index):
        # Collection is a current snapshot; disabling the year picker avoids
        # implying that an old year reconstructs historical ownership.
        self.period.setEnabled(index != 2)
        if self.tabs.isEnabled():
            self.notice.setVisible(index != 2 and bool(self.notice.text()))
        self.artwork.refresh()
        if self.isVisible():
            self.artwork.resume()
        if index == 1:
            self._resize_history_title()
            self.history_table.viewport().update()
        elif index == 2:
            self.collection_table.viewport().update()
        elif index == 3:
            QTimer.singleShot(0, self._fit_recap_tiles)
            self.recap_highlights.viewport().update()
            self.recap_gallery.viewport().update()

    def showEvent(self, event):
        super().showEvent(event)
        self.artwork.resume()
        self._arrange_cards()
        if hasattr(self, 'history_table'):
            self._tab_changed(self.tabs.currentIndex())

    def hideEvent(self, event):
        self.artwork.pause()
        super().hideEvent(event)

    def shutdown_artwork(self):
        return self.artwork.pause()

    def _on_artwork_idle(self):
        self.artwork_idle.emit()
        if self._close_pending:
            self.close()

    def closeEvent(self, event):
        if not self.shutdown_artwork():
            self._close_pending = True
            event.ignore()
            return
        self._close_pending = False
        super().closeEvent(event)
