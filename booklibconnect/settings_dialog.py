from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QIODevice, QSaveFile
from PySide6.QtGui import QIcon, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication, QPlainTextEdit, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget, QSpinBox, QGridLayout,
)
from . import diagnostics
from .library_settings_store import LibrarySettingsStore, DEFAULT_PREFERENCES
from .gallery_options import MARKER_LABELS, CORNERS, RULE_MODES, effective_options, series_key
from .genre_tags import rule_description
from .gallery_cards import render_card, card_height, GREEN


class DiagnosticsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        intro = QLabel('Preview the system information and recent app activity you can share with a bug report. '
                       'Nothing is sent automatically.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.report_preview = QPlainTextEdit()
        self.report_preview.setReadOnly(True)
        self.report_preview.setAccessibleName('Diagnostics report preview')
        self.report_preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.report_preview, 1)
        note = QLabel('Includes up to three runs across libraries. Book titles, library names, '
                      'file paths and listening history are excluded.')
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        refresh = QPushButton('Refresh')
        refresh.clicked.connect(self.refresh)
        copy_button = QPushButton('Copy report')
        copy_button.clicked.connect(self._copy_diagnostics)
        self.save_report_button = QPushButton('Save report…')
        self.save_report_button.clicked.connect(self._save_diagnostics)
        for button in (refresh, copy_button, self.save_report_button):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.diag_status = QLabel()
        layout.addWidget(self.diag_status)

    def refresh(self):
        self.report_preview.setPlainText(diagnostics.report())
        self.diag_status.clear()

    def _copy_diagnostics(self):
        QApplication.clipboard().setText(self.report_preview.toPlainText())
        self.diag_status.setText('Report copied.')

    def _save_diagnostics(self):
        filename, _ = QFileDialog.getSaveFileName(self, 'Save diagnostics report',
            f'AudiobookShelfManager-diagnostics-{date.today().isoformat()}.txt', 'Text files (*.txt)')
        if not filename:
            return
        output = QSaveFile(filename)
        output.setDirectWriteFallback(True)  # A document portal may grant access only to the selected file.
        content = self.report_preview.toPlainText().encode('utf-8')
        if not output.open(QIODevice.WriteOnly) or output.write(content) != len(content) or not output.commit():
            self.diag_status.setText('Could not save the report: ' + output.errorString())
            return
        self.diag_status.setText('Report saved.')


def show_diagnostics_dialog(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle('Diagnostics')
    dialog.resize(850, 520)
    layout = QVBoxLayout(dialog)
    panel = DiagnosticsPanel(dialog)
    layout.addWidget(panel, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    panel.refresh()
    dialog.exec()
    dialog.deleteLater()


class SettingsDialog(QDialog):
    """Work on a draft: Cancel never saves library or preference changes."""
    def __init__(self, settings: LibrarySettingsStore, parent=None, *, series_paths=(), selected_series=None):
        super().__init__(parent)
        self.settings = settings
        self._draft = settings.snapshot()
        self._editing_id = settings.active_library_id()
        self.requested_action = None
        self._series_paths = [Path(path) for path in series_paths]
        self._scope = None
        self._loading_options = False
        self._controls = {}
        self._overrides = {}
        self.setWindowTitle('Settings')
        self.resize(920, 600)
        root = QVBoxLayout(self)
        library_row = QHBoxLayout()
        self.library_label = QLabel('Library')
        library_row.addWidget(self.library_label)
        self.library_choice = QComboBox()
        self.library_choice.currentIndexChanged.connect(self._profile_changed)
        library_row.addWidget(self.library_choice, 1)
        root.addLayout(library_row)
        self.scope_widget = QWidget()
        scope_row = QHBoxLayout(self.scope_widget)
        scope_row.setContentsMargins(0, 0, 0, 0)
        scope_row.addWidget(QLabel('Apply to'))
        self.scope_choice = QComboBox()
        self.scope_choice.currentIndexChanged.connect(self._scope_changed)
        scope_row.addWidget(self.scope_choice, 1)
        choose_series = QPushButton('Choose series folder...')
        choose_series.clicked.connect(self._choose_series)
        scope_row.addWidget(choose_series)
        self.reset_series = QPushButton('Use library defaults')
        self.reset_series.clicked.connect(self._reset_series)
        scope_row.addWidget(self.reset_series)
        root.addWidget(self.scope_widget)
        content = QHBoxLayout()
        root.addLayout(content, 1)
        self.nav = QListWidget()
        self.nav.setFixedWidth(165)
        self.nav.addItems(['Libraries', 'General', 'Backups', 'Tag rules', 'Card display', 'Diagnostics'])
        self.nav.setStyleSheet('QListWidget::item {padding: 12px 8px;}')
        content.addWidget(self.nav)
        self.pages = QStackedWidget()
        content.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_libraries())
        self.pages.addWidget(self._build_general())
        self.pages.addWidget(self._build_backups())
        self.pages.addWidget(self._build_tag_rules())
        self.pages.addWidget(self._build_card_display())
        self.diagnostics_page = DiagnosticsPanel()
        self.pages.addWidget(self.diagnostics_page)
        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self._refresh_library_choices()
        self._show_profile(self._editing_id)
        if selected_series is not None:
            self._series_paths.append(Path(selected_series))
            self._refresh_scopes()
            self.scope_choice.setCurrentIndex(self.scope_choice.findData(series_key(selected_series)))
            self.nav.setCurrentRow(3)

    def _page(self, title, description):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel(title)
        heading.setStyleSheet('font-size: 20px; font-weight: 600;')
        layout.addWidget(heading)
        text = QLabel(description)
        text.setWordWrap(True)
        layout.addWidget(text)
        return page, layout

    def _build_libraries(self):
        page, layout = self._page('Your libraries',
            'Keep Main, Adult or other collections separate. Each library has its own history, ratings and statistics.')
        row = QHBoxLayout()
        self.new_library = QPushButton('New library...')
        self.new_library.clicked.connect(self._add_library)
        row.addWidget(self.new_library)
        rename = QPushButton('Rename...')
        rename.clicked.connect(self._rename_library)
        row.addWidget(rename)
        layout.addLayout(row)
        layout.addWidget(QLabel('Folders in this library'))
        self.lst_roots = QListWidget()
        self.lst_roots.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lst_roots.itemSelectionChanged.connect(self._refresh_buttons)
        layout.addWidget(self.lst_roots, 1)
        row = QHBoxLayout()
        self.btn_add = QPushButton('Add folder...')
        self.btn_add.setToolTip('Choose the library root containing author folders, with series folders underneath.')
        self.btn_remove = QPushButton('Remove folder')
        self.btn_up = QPushButton('Move up')
        self.btn_down = QPushButton('Move down')
        for button, action in ((self.btn_add, self._add_folder), (self.btn_remove, self._remove_folder),
                               (self.btn_up, lambda: self._move(-1)), (self.btn_down, lambda: self._move(1))):
            button.clicked.connect(action)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        text = QLabel('Use different folders for different libraries. Removing a folder does not delete its files or saved history.')
        text.setWordWrap(True)
        layout.addWidget(text)
        self.open_selected = QCheckBox('Open this library after saving')
        self.open_selected.setChecked(True)
        layout.addWidget(self.open_selected)
        return page

    def _build_general(self):
        page, layout = self._page('General', 'These preferences apply to all libraries.')
        form = QFormLayout()
        self.startup = QComboBox()
        form.addRow('Open at startup', self.startup)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.5, 4)
        self.speed.setDecimals(2)
        self.speed.setSingleStep(.25)
        self.speed.setSuffix('x')
        preferences = {**DEFAULT_PREFERENCES, **self._draft['preferences']}
        self.speed.setValue(preferences['playback_speed'])
        self.speed.setToolTip('Used for estimated listening time in Statistics. It does not change playback.')
        form.addRow('Listening speed estimate', self.speed)
        layout.addLayout(form)
        self.remember_sort = QCheckBox('Remember writer and series sorting for each library')
        self.auto_rename = QCheckBox('Rename audiobook files after saving metadata')
        self.rename_sidecar = QCheckBox('Also rename the matching sidecar cover')
        self.scan_warning_dialog = QCheckBox('Show the metadata warning report after scans')
        for name in ('remember_sort', 'auto_rename', 'rename_sidecar', 'scan_warning_dialog'):
            widget = getattr(self, name)
            widget.setChecked(preferences[name])
            layout.addWidget(widget)
        self.rename_sidecar.setEnabled(self.auto_rename.isChecked())
        self.auto_rename.toggled.connect(self.rename_sidecar.setEnabled)
        note = QLabel('Yellow warning markers remain visible when the report is turned off. File deletion always starts unchecked.')
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _build_backups(self):
        page, layout = self._page('Library backups',
            'Listening history, precise ratings and favorites are backed up automatically each day a library is opened.')
        self.backup_scope = QLabel()
        layout.addWidget(self.backup_scope)
        self.backup_now = QPushButton('Save settings and back up now')
        self.backup_now.clicked.connect(lambda: self._request_action('backup'))
        layout.addWidget(self.backup_now)
        self.open_backups = QPushButton('Save settings and open backup folder')
        self.open_backups.clicked.connect(lambda: self._request_action('open_backups'))
        layout.addWidget(self.open_backups)
        note = QLabel('Backups contain saved library data. Keep a separate copy of your audiobook files.')
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _page_changed(self, index):
        self.pages.setCurrentIndex(index)
        self.scope_widget.setVisible(index in (3, 4))
        is_diagnostics = self.pages.currentWidget() is self.diagnostics_page
        self.library_choice.setVisible(not is_diagnostics)
        self.library_label.setVisible(not is_diagnostics)
        if is_diagnostics:
            self.diagnostics_page.refresh()

    def _option_row(self, layout, row, key, label, widget):
        caption = QLabel(label)
        layout.addWidget(caption, row, 0)
        layout.addWidget(widget, row, 1)
        override = QCheckBox('Override')
        override.setToolTip('Use a different value for this series and its books.')
        override.toggled.connect(self._option_changed)
        layout.addWidget(override, row, 2)
        self._controls[key] = widget
        self._overrides[key] = override
        if isinstance(widget, QCheckBox):
            widget.toggled.connect(self._option_changed)
        elif isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(self._option_changed)

    def _build_tag_rules(self):
        page, layout = self._page('Tag rules',
            'Count unique genre tags. Duplicates do not count; separate tags with commas, semicolons or |.')
        grid = QGridLayout()
        self.rule_mode = QComboBox()
        for key, label in RULE_MODES.items():
            self.rule_mode.addItem(label, key)
        self._option_row(grid, 0, 'tag_rule', 'Required tags', self.rule_mode)
        self.rule_count = QSpinBox()
        self.rule_maximum = QSpinBox()
        for spin in (self.rule_count, self.rule_maximum):
            spin.setRange(0, 999)
            spin.valueChanged.connect(self._option_changed)
        self.rule_count_label = QLabel('Number')
        self.rule_maximum_label = QLabel('Maximum')
        grid.addWidget(self.rule_count_label, 1, 0)
        grid.addWidget(self.rule_count, 1, 1)
        grid.addWidget(self.rule_maximum_label, 2, 0)
        grid.addWidget(self.rule_maximum, 2, 1)
        layout.addLayout(grid)
        self.rule_summary = QLabel()
        self.rule_summary.setWordWrap(True)
        layout.addWidget(self.rule_summary)
        note = QLabel('Warnings update from cached tags as soon as you save. The rule never adds or removes tags from your files.')
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _build_card_display(self):
        page, layout = self._page('Card display',
            'Choose what appears on cards. Series overrides also apply to the books inside that series.')
        row = QHBoxLayout()
        grid = QGridLayout()
        self.show_rating = QCheckBox('Show')
        self._option_row(grid, 0, 'show_rating', 'Rating', self.show_rating)
        self.rating_corner = QComboBox()
        for key, label in CORNERS.items():
            self.rating_corner.addItem(label, key)
        self._option_row(grid, 1, 'rating_corner', 'Rating corner', self.rating_corner)
        for index, (key, label) in enumerate(MARKER_LABELS.items(), 2):
            self._option_row(grid, index, key, label, QCheckBox('Show'))
        row.addLayout(grid, 1)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.preview.setFixedWidth(232)
        row.addWidget(self.preview)
        layout.addLayout(row)
        self.display_note = QLabel('Preview: example series. Hiding a marker keeps its saved data. Review switches also hide unused review fields in Book Details and Edit.')
        self.display_note.setWordWrap(True)
        layout.addWidget(self.display_note)
        layout.addStretch()
        return page

    def _refresh_scopes(self):
        self.scope_choice.blockSignals(True)
        self.scope_choice.clear()
        self.scope_choice.addItem('Library defaults', None)
        profile = self._profile()
        paths = set(profile.get('series_overrides', {}))
        for path in self._series_paths:
            if any(str(path) == root or Path(root) in path.parents for root in profile['roots']):
                paths.add(series_key(path))
        for path in sorted(paths, key=str.casefold):
            p = Path(path)
            self.scope_choice.addItem(f'{p.parent.name} / {p.name}', path)
            self.scope_choice.setItemData(self.scope_choice.count()-1, path, Qt.ToolTipRole)
        self.scope_choice.setCurrentIndex(max(0, self.scope_choice.findData(self._scope)))
        self.scope_choice.blockSignals(False)
        self._load_options()

    def _scope_changed(self, index):
        if index < 0:
            return
        self._stash_options()
        self._scope = self.scope_choice.itemData(index)
        self._load_options()

    def _choose_series(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose a series folder')
        if not folder:
            return
        path = Path(folder).resolve()
        if not any(Path(root) in path.parents for root in self._profile()['roots']):
            QMessageBox.warning(self, 'Series folder', 'Choose a series inside this library.')
            return
        self._stash_options()
        self._series_paths.append(path)
        self._refresh_scopes()
        self.scope_choice.setCurrentIndex(self.scope_choice.findData(series_key(path)))

    def _reset_series(self):
        if self._scope is not None:
            self._profile().setdefault('series_overrides', {}).pop(self._scope, None)
            self._load_options()

    def _load_options(self):
        self._loading_options = True
        options = effective_options(self._profile(), self._scope)
        overrides = self._profile().get('series_overrides', {}).get(self._scope, {})
        for key, widget in self._controls.items():
            toggle = self._overrides[key]
            toggle.setVisible(self._scope is not None)
            toggle.setChecked(key in overrides)
            if key == 'tag_rule':
                rule = options[key]
                widget.setCurrentIndex(widget.findData(rule['mode']))
                self.rule_count.setValue(rule['count'])
                self.rule_maximum.setValue(rule['maximum'])
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(widget.findData(options[key]))
            else:
                widget.setChecked(options[key])
        self.reset_series.setEnabled(self._scope is not None)
        self._loading_options = False
        self._update_option_preview()

    def _option_values(self):
        result = {}
        for key, widget in self._controls.items():
            if key == 'tag_rule':
                result[key] = {'mode': self.rule_mode.currentData(), 'count': self.rule_count.value(),
                               'maximum': self.rule_maximum.value()}
            else:
                result[key] = widget.currentData() if isinstance(widget, QComboBox) else widget.isChecked()
        return result

    def _stash_options(self):
        if self._loading_options or not self._controls:
            return
        values = self._option_values()
        if self._scope is None:
            self._profile()['options'] = values
        else:
            values = {key: value for key, value in values.items() if self._overrides[key].isChecked()}
            overrides = self._profile().setdefault('series_overrides', {})
            if values:
                overrides[self._scope] = values
            else:
                overrides.pop(self._scope, None)

    def _option_changed(self, *_):
        if self._loading_options:
            return
        self._stash_options()
        # Only re-load controls when inheritance changes; leave typing intact.
        if self.sender() in self._overrides.values():
            self._load_options()
        else:
            self._update_option_preview()

    def _update_option_preview(self):
        options = self._option_values()
        for key, widget in self._controls.items():
            widget.setEnabled(self._scope is None or self._overrides[key].isChecked())
        tag_enabled = self.rule_mode.isEnabled() and options['tag_rule']['mode'] != 'off'
        self.rule_count.setEnabled(tag_enabled)
        is_range = options['tag_rule']['mode'] == 'range'
        self.rule_count_label.setText('Minimum' if is_range else 'Number')
        self.rule_maximum.setVisible(is_range)
        self.rule_maximum_label.setVisible(is_range)
        self.rule_maximum.setEnabled(tag_enabled)
        try:
            self.rule_summary.setText('Genre tags: ' + rule_description(options['tag_rule']) + '.')
        except ValueError as exc:
            self.rule_summary.setText(str(exc))
        self.rating_corner.setEnabled(self.rating_corner.isEnabled() and options['show_rating'])
        self._controls['show_listening_dates'].setEnabled(self._controls['show_listening_dates'].isEnabled() and options['show_completed'])
        examples = [('show_owned', 'Owned 4/5'), ('show_completed', 'Completed 3/5'),
                    ('show_goodreads', 'Goodreads 2/5'), ('show_audible', 'Audible 1/5'),
                    ('show_reviewed', 'Reviewed 3/5')]
        lines = [(text, GREEN) for key, text in examples if options[key]]
        cover = QPixmap(200, 200)
        cover.fill(QColor('#386577'))
        size = QSize(216, card_height(len(lines)))
        icon = render_card(QIcon(cover), size, lines, rating=3.61, options=options, warning=True)
        self.preview.setPixmap(icon.pixmap(size))

    def _profile(self):
        return next(profile for profile in self._draft['libraries'] if profile['id'] == self._editing_id)

    def _stash_roots(self):
        self._profile()['roots'] = [str(self.lst_roots.item(i).data(Qt.UserRole)) for i in range(self.lst_roots.count())]

    def _refresh_library_choices(self):
        previous = self.startup.currentData() or self._draft['startup_library']
        self.library_choice.blockSignals(True)
        self.library_choice.clear()
        self.startup.clear()
        for profile in self._draft['libraries']:
            self.library_choice.addItem(profile['name'], profile['id'])
            self.startup.addItem(profile['name'], profile['id'])
        self.startup.addItem('Last used library', 'last')
        self.startup.setCurrentIndex(max(0, self.startup.findData(previous)))
        self.library_choice.setCurrentIndex(self.library_choice.findData(self._editing_id))
        self.library_choice.blockSignals(False)

    def _show_profile(self, ident):
        self._editing_id = ident
        self._scope = None
        self._refresh_scopes()
        self.lst_roots.clear()
        for value in self._profile()['roots']:
            self._add_root_item(Path(value))
        self.backup_scope.setText('Selected library: ' + self._profile()['name'])
        self._refresh_buttons()

    def _profile_changed(self, index):
        if index < 0:
            return
        self._stash_roots()
        self._stash_options()
        self._show_profile(self.library_choice.itemData(index))

    def _add_library(self):
        name, accepted = QInputDialog.getText(self, 'New library', 'Library name (for example Adult):')
        if not accepted:
            return
        self._stash_roots()
        self._stash_options()
        draft = self.settings.clone(self._draft).snapshot()
        ident = uuid.uuid4().hex
        draft['libraries'].append({'id': ident, 'name': name.strip(), 'roots': [], 'sort': {}})
        try:
            self._draft = LibrarySettingsStore.validate(draft)
        except ValueError as exc:
            QMessageBox.warning(self, 'Library name', str(exc))
            return
        self._editing_id = ident
        self._refresh_library_choices()
        self._show_profile(ident)

    def _rename_library(self):
        name, accepted = QInputDialog.getText(self, 'Rename library', 'Library name:', text=self._profile()['name'])
        if not accepted:
            return
        previous = self._profile()['name']
        self._profile()['name'] = name.strip()
        try:
            LibrarySettingsStore.validate(self._draft)
        except ValueError as exc:
            self._profile()['name'] = previous
            QMessageBox.warning(self, 'Library name', str(exc))
            return
        self._refresh_library_choices()
        self.backup_scope.setText('Selected library: ' + self._profile()['name'])

    def _add_root_item(self, path):
        suffix = '' if path.is_dir() else ' [unavailable]'
        item = QListWidgetItem(str(path) + suffix)
        item.setData(Qt.UserRole, str(path))
        item.setToolTip(str(path))
        self.lst_roots.addItem(item)

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Add folder to ' + self._profile()['name'])
        if not folder:
            return
        self._stash_roots()
        self._stash_options()
        previous = list(self._profile()['roots'])
        self._profile()['roots'].append(str(Path(folder).resolve()))
        try:
            self._draft = LibrarySettingsStore.validate(self._draft)
        except ValueError as exc:
            self._profile()['roots'] = previous
            QMessageBox.warning(self, 'Folder already belongs to another library', str(exc))
            return
        self._show_profile(self._editing_id)

    def _remove_folder(self):
        row = self.lst_roots.currentRow()
        if row >= 0:
            self.lst_roots.takeItem(row)
        self._stash_roots()
        self._refresh_buttons()

    def _move(self, step):
        row = self.lst_roots.currentRow()
        if 0 <= row + step < self.lst_roots.count() and row >= 0:
            item = self.lst_roots.takeItem(row)
            self.lst_roots.insertItem(row + step, item)
            self.lst_roots.setCurrentRow(row + step)

    def _refresh_buttons(self):
        if not hasattr(self, 'btn_remove'):
            return
        row = self.lst_roots.currentRow()
        self.btn_remove.setEnabled(row >= 0)
        self.btn_up.setEnabled(row > 0)
        self.btn_down.setEnabled(0 <= row < self.lst_roots.count() - 1)

    def values(self):
        self._stash_roots()
        self._stash_options()
        self._draft['startup_library'] = self.startup.currentData()
        if self.open_selected.isChecked() or self.requested_action:
            self._draft['active_library'] = self._editing_id
        self._draft['preferences'].update({
            key: getattr(self, key).isChecked() for key in
            ('remember_sort', 'auto_rename', 'rename_sidecar', 'scan_warning_dialog')})
        self._draft['preferences']['playback_speed'] = self.speed.value()
        return LibrarySettingsStore.validate(self._draft)

    def _request_action(self, action):
        self.requested_action = action
        self.accept()

    def accept(self):
        try:
            self.values()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Could not save settings', str(exc))
            return
        super().accept()
