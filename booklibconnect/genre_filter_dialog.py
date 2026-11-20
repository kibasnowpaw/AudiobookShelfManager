"""Searchable multi-genre selection; Cancel leaves the current filter alone."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QDialogButtonBox, QPushButton, QLabel)
from .statistics_data import normalized


class GenreFilterDialog(QDialog):
    def __init__(self, entries, selected=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle('Choose genres')
        self.resize(430, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Choose one or more genres from this library.'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Find a genre...')
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.tags = QListWidget()
        chosen = {normalized(tag) for tag in selected}
        for name, count in entries:
            item = QListWidgetItem(f'{name or "No genre tags"} ({count})')
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if normalized(name) in chosen else Qt.Unchecked)
            self.tags.addItem(item)
        layout.addWidget(self.tags)
        self.search.textChanged.connect(self._filter)
        clear = QPushButton('Clear selection')
        clear.clicked.connect(self.clear_selection)
        layout.addWidget(clear)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter(self, text):
        query = normalized(text)
        for index in range(self.tags.count()):
            item = self.tags.item(index)
            item.setHidden(query not in normalized(item.text()))

    def clear_selection(self):
        for index in range(self.tags.count()):
            self.tags.item(index).setCheckState(Qt.Unchecked)

    def selected_genres(self):
        return tuple(self.tags.item(index).data(Qt.UserRole) for index in range(self.tags.count())
                     if self.tags.item(index).checkState() == Qt.Checked)
