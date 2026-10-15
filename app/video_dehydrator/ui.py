"""Desktop window. The same jobs as the Windows program, on any Linux desktop."""

from __future__ import annotations

import html
import os
import subprocess
import tarfile
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import helptext
from .compare import CompareDialog
from .engine import (
    BUDGET_ANCHORS, BUDGET_FACTORS, app_version, bloat_decision, budget_choice_label,
    budget_pick_pack, bundled_tools, compare_versions, encoder_name, format_byte_size,
    format_clock, format_over_ratio, format_rate, handbrake_arguments, handbrake_fraction,
    is_encode_progress_line, is_video_file, larger_file_note, merge_skip_row,
    missing_tools, over_band_id, over_ratio, probe_file, remembered_skip,
    remove_kept_original, remove_skip_row, replace_plan, replace_with_encode,
    run_lines, set_budget_picks, size_skip_reason, target_kbps, test_encoded_file,
    undo_replaced_file,
)
from .recycle import RecycleBook
from .store import (
    append_convert_log, load_settings, read_pending, read_skips, save_settings,
    write_pending, write_skips,
)

COLUMNS = ['File', 'Dimensions', 'Length', 'Size', 'GiB/h', 'Budget', 'Over', 'Target', 'New size', 'Status']
OVER_COLORS = {
    'slight': ('#dcfce7', '#166534'),
    'heavy': ('#fef9c3', '#854d0e'),
    'extreme': ('#fee2e2', '#991b1b'),
}
ISSUE_URL = 'https://github.com/philipvern-dot/video-dehydrator/issues/new?template=bug_report.yml'
KOFI = 'https://ko-fi.com/philipvern'
RELEASES = 'https://api.github.com/repos/philipvern-dot/video-dehydrator/releases/latest'


def os_label() -> str:
    path = Path('/etc/os-release')
    name = 'Linux'
    if path.is_file():
        text = path.read_text(encoding='utf-8', errors='replace')
        for line in text.splitlines():
            if line.startswith('PRETTY_NAME='):
                name = line.split('=', 1)[1].strip().strip('"')
                break
    return name


class Awake:
    def __init__(self):
        self.proc = None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        try:
            self.proc = subprocess.Popen(
                ['systemd-inhibit', '--what=idle', '--who=Video Dehydrator',
                 '--why=Video Dehydrator is working', 'sleep', 'infinity'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.proc = None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


class ScanWorker(QThread):
    hit = Signal(object)
    progress = Signal(int, int, str)
    finished_scan = Signal(object)

    def __init__(self, root, recurse, ffprobe, skips, cancel):
        super().__init__()
        self.root = root
        self.recurse = recurse
        self.ffprobe = ffprobe
        self.skips = skips
        self.cancel = cancel

    def run(self):
        stats = {'seen': 0, 'hits': 0, 'errors': 0, 'ignored': 0, 'cancelled': False}
        stack = [self.root]
        while stack:
            if self.cancel.is_set():
                stats['cancelled'] = True
                break
            directory = stack.pop()
            try:
                with os.scandir(directory) as scan:
                    names = list(scan)
            except OSError:
                stats['errors'] += 1
                continue
            for entry in names:
                if self.cancel.is_set():
                    stats['cancelled'] = True
                    stack = []
                    break
                if entry.is_dir(follow_symlinks=False):
                    if entry.name == '.vd-originals':
                        continue
                    if self.recurse:
                        stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False) or not is_video_file(entry.path):
                    continue
                stats['seen'] += 1
                self.progress.emit(stats['seen'], stats['hits'], entry.name)
                try:
                    probe = probe_file(self.ffprobe, entry.path)
                    decision = bloat_decision(probe)
                except Exception:
                    stats['errors'] += 1
                    continue
                if decision['decision'] != 'bloated':
                    continue
                if remembered_skip(self.skips, entry.path, probe.size_bytes, decision['target_kbps'] or 0):
                    stats['ignored'] += 1
                    continue
                stats['hits'] += 1
                self.hit.emit(row_from_probe(entry.path, probe, decision, 'Bloated'))
        self.finished_scan.emit(stats)


def row_from_probe(path, probe, decision, status):
    anchor = None
    from .engine import nearest_budget_anchor
    anchor = nearest_budget_anchor(probe.width, probe.height)
    ratio = over_ratio(decision['gibph'], decision['budget_gibph'])
    return {
        'path': path,
        'name': os.path.basename(path),
        'checked': status in ('Bloated', 'Failed'),
        'status': status,
        'width': probe.width,
        'height': probe.height,
        'duration': probe.duration_sec,
        'size': probe.size_bytes,
        'old_bytes': probe.size_bytes,
        'gibph': decision['gibph'],
        'budget': decision['budget_gibph'],
        'over': ratio,
        'target': decision['target_kbps'],
        'estimate': decision['estimate_bytes'],
        'estimate_rough': decision['estimate_rough'],
        'video_bitrate': probe.video_bitrate,
        'codec': probe.video_codec,
        'audio': list(probe.audio_codecs),
        'subs': probe.sub_count,
        'reason': decision['reason'],
        'anchor': anchor['id'] if anchor else '',
        'kept': '',
        'new': '',
        'source': path,
        'pending_id': '',
        'batch': '',
        'probe': probe,
    }


class ConvertWorker(QThread):
    activity = Signal(str, str)
    progress = Signal(float)
    file_update = Signal(object)
    finished_job = Signal(object)

    def __init__(self, rows, tools, reencode, delete_original, detailed, skips, cancel):
        super().__init__()
        self.rows = rows
        self.tools = tools
        self.reencode = reencode
        self.delete_original = delete_original
        self.detailed = detailed
        self.skips = list(skips)
        self.cancel = cancel
        self.book = RecycleBook()
        self.batch = uuid.uuid4().hex
        self.saved = 0

    def run(self):
        for row in self.rows:
            if self.cancel.is_set():
                break
            self.convert_row(row)
        self.finished_job.emit({'book': self.book, 'skips': self.skips, 'saved': self.saved, 'cancelled': self.cancel.is_set(), 'batch': self.batch})

    def convert_row(self, row):
        source = row['source'] or row['path']
        previous = row['status']
        self.activity.emit(f"Converting {os.path.basename(source)}.", 'text')
        row['status'] = 'Converting'
        self.file_update.emit(row)
        self.progress.emit(0)
        if self.cancel.is_set():
            row['status'] = previous if previous in ('Failed', 'Skipped') else 'Bloated'
            row['checked'] = True
            self.file_update.emit(row)
            return
        try:
            probe = probe_file(str(self.tools['ffprobe']), source)
        except Exception as exc:
            row['status'] = 'Failed'
            row['reason'] = str(exc)
            row['checked'] = True
            self.activity.emit(f'Could not read {os.path.basename(source)}. {exc}', 'err')
            self.file_update.emit(row)
            return
        plan = replace_plan(source)
        if plan['blocked']:
            row['status'] = 'Failed'
            row['reason'] = 'A file already has the new name.'
            row['checked'] = True
            self.activity.emit(row['reason'] + ' ' + plan['final'], 'err')
            self.file_update.emit(row)
            return
        os.makedirs(plan['hold_dir'], exist_ok=True)
        encoder = encoder_name(probe)
        kbps = target_kbps(probe.width, probe.height)
        args = handbrake_arguments(source, plan['temp'], probe, kbps, encoder, self.reencode)
        log_lines = []

        def on_line(line):
            log_lines.append(line)
            fraction = handbrake_fraction(line)
            if fraction is not None:
                self.progress.emit(fraction)
            if self.detailed and line and not is_encode_progress_line(line):
                self.activity.emit(line, 'dim')

        code, _joined = run_lines([str(self.tools['handbrake'])] + args, on_line, self.cancel)
        handbrake_log = '\n'.join(log_lines)
        if self.cancel.is_set():
            if os.path.isfile(plan['temp']):
                os.remove(plan['temp'])
            row['status'] = previous if previous in ('Failed', 'Skipped') else 'Bloated'
            row['checked'] = True
            self.activity.emit(f"Stopped {os.path.basename(source)}. The original stayed.", 'text')
            self.file_update.emit(row)
            return
        checked = test_encoded_file(
            str(self.tools['ffmpeg']), str(self.tools['ffprobe']), plan['temp'], probe, handbrake_log, self.reencode,
        )
        if not checked['ok']:
            if os.path.isfile(plan['temp']):
                os.remove(plan['temp'])
            if size_skip_reason(checked['reason']):
                note = larger_file_note(
                    checked['reason'], probe.size_bytes, checked['new_bytes'], kbps,
                    probe.audio_codecs, (checked['out_probe'].audio_codecs if checked['out_probe'] else []),
                )
                row['status'] = 'Skipped'
                row['checked'] = False
                row['reason'] = note or checked['reason']
                self.skips = merge_skip_row(self.skips, source, probe.size_bytes, kbps or 0)
                append_convert_log('SKIP', row['reason'], source, '', '', probe.size_bytes, checked['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
                self.activity.emit('Skipped. ' + row['reason'], 'text')
            else:
                row['status'] = 'Failed'
                row['checked'] = True
                row['reason'] = checked['reason']
                append_convert_log('FAIL', checked['reason'], source, '', '', probe.size_bytes, checked['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
                self.activity.emit(f"Failed {os.path.basename(source)}. {checked['reason']}", 'err')
            self.file_update.emit(row)
            return
        try:
            swap = replace_with_encode(source, plan['temp'], self.delete_original, self.book)
        except Exception as exc:
            row['status'] = 'Failed'
            row['reason'] = str(exc)
            row['checked'] = True
            self.activity.emit(f'Could not replace {os.path.basename(source)}. {exc}', 'err')
            self.file_update.emit(row)
            return
        row['new'] = swap['new_path']
        row['path'] = swap['new_path']
        row['name'] = os.path.basename(swap['new_path'])
        row['kept'] = swap['kept_path'] or ''
        row['source'] = source
        row['old_bytes'] = swap['old_bytes']
        row['size'] = swap['new_bytes']
        row['estimate'] = None
        self.saved += max(0, swap['old_bytes'] - swap['new_bytes'])
        if swap['deleted']:
            row['status'] = 'Original deleted'
            row['pending_id'] = ''
            row['checked'] = False
            append_convert_log('DELETED', 'ok', source, swap['new_path'], '', swap['old_bytes'], swap['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
        else:
            row['status'] = 'Ready to delete'
            row['pending_id'] = uuid.uuid4().hex
            row['batch'] = self.batch
            row['checked'] = False
            append_convert_log('KEPT', 'ok', source, swap['new_path'], swap['kept_path'], swap['old_bytes'], swap['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
        self.skips = remove_skip_row(self.skips, source)
        self.activity.emit(f"Finished {row['name']}.", 'ok')
        self.progress.emit(100)
        self.file_update.emit(row)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Video Dehydrator')
        self.resize(1120, 900)
        icon = bundled_tools()['root'] / 'vd-icon.png'
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self.tools = bundled_tools()
        self.settings = load_settings()
        set_budget_picks(str(self.settings.get('budgets') or ''))
        self.rows = []
        self.pending = read_pending()
        self.skips = read_skips()
        self.work = 'idle'
        self.cancel = threading.Event()
        self.worker = None
        self.awake = Awake()
        self.loading = True
        self.saved_bytes = 0
        self.update_offer = None
        self._update_started = False
        self._build()
        self._load_pending_rows()
        self.loading = False
        self._refresh()
        missing = missing_tools()
        if missing:
            self.log('These tools need to be in the tools folder:\n' + '\n'.join(missing), 'err')
        else:
            self.log('Ready. Choose a folder, then click Scan.', 'text')
            self.log('Scan lists videos that are too big for their picture size. The Budgets row starts on Recommended.', 'text')
        self.status.setText('Ready')

    def _build(self):
        page = QWidget()
        page.setStyleSheet('background:#eef1f4; color:#111827;')
        self.setCentralWidget(page)
        root = QVBoxLayout(page)
        root.setContentsMargins(16, 12, 16, 12)
        header = QLabel('Video Dehydrator')
        header.setStyleSheet('font-size:22px; font-weight:700;')
        version = QLabel(app_version())
        version.setStyleSheet('color:#4b5563;')
        self.check_update = QPushButton('Check for updates')
        self.update_button = QPushButton('Update')
        self.update_button.setEnabled(False)
        self.donate = QPushButton('Donate')
        self.report = QPushButton('Report a bug')
        self.check_update.clicked.connect(self.check_for_updates)
        self.update_button.clicked.connect(self.install_update)
        self.donate.clicked.connect(lambda: self.open_url(KOFI))
        self.report.clicked.connect(self.report_bug)
        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch(1)
        top.addWidget(self.check_update)
        top.addWidget(self.update_button)
        top.addWidget(self.donate)
        top.addWidget(self.report)
        top.addWidget(version)
        root.addLayout(top)

        self.folder = QLineEdit(str(self.settings.get('folder') or ''))
        browse = QPushButton('Browse')
        browse.clicked.connect(self.browse)
        self.subfolders = QCheckBox('Include subfolders')
        self.subfolders.setChecked(bool(self.settings.get('includeSubfolders', True)))
        self.folder_pick = QComboBox()
        self.folder_pick.addItem('All folders')
        self.select_folder = QPushButton('Select folder')
        self.select_folder.clicked.connect(self.apply_folder)
        self.over_pick = QComboBox()
        self.over_pick.addItems(['All overages', 'Slight', 'Heavy', 'Extreme'])
        self.select_over = QPushButton('Select overage')
        self.select_over.clicked.connect(self.apply_over)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(self.subfolders)
        root.addLayout(folder_row)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.folder_pick, 1)
        pick_row.addWidget(self.select_folder)
        pick_row.addWidget(self.over_pick)
        pick_row.addWidget(self.select_over)
        select_all = QPushButton('Select all')
        select_none = QPushButton('Deselect all')
        clear = QPushButton('Clear')
        select_all.clicked.connect(lambda: self.set_checks(True))
        select_none.clicked.connect(lambda: self.set_checks(False))
        clear.clicked.connect(self.clear_rows)
        pick_row.addWidget(select_all)
        pick_row.addWidget(select_none)
        pick_row.addWidget(clear)
        root.addLayout(pick_row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_column)
        self._sort_over_desc = True
        root.addWidget(self.table, 1)

        options = QHBoxLayout()
        self.auto = QCheckBox('Delete originals automatically')
        self.auto.setChecked(bool(self.settings.get('autoDelete')))
        self.auto.setToolTip('After a new file checks out, delete the original. On a shared drive, the matching #recycle copy is removed when the path and the size match.')
        self.reencode = QCheckBox('Re-encode audio that cannot be copied')
        self.reencode.setChecked(bool(self.settings.get('reencodeAudio')))
        self.detailed = QCheckBox('Show detailed activity')
        self.detailed.setChecked(bool(self.settings.get('detailed')))
        self.shutdown = QCheckBox('Shutdown computer upon completion')
        self.shutdown.setChecked(bool(self.settings.get('shutdown')))
        self.auto.toggled.connect(self.on_auto)
        for box in (self.reencode, self.detailed, self.shutdown, self.subfolders):
            box.toggled.connect(self.persist)
        options.addWidget(self.auto)
        options.addWidget(self.reencode)
        options.addWidget(self.detailed)
        options.addWidget(self.shutdown)
        root.addLayout(options)
        budget_row = QHBoxLayout()
        self.budget_boxes = {}
        picks = {}
        for part in str(self.settings.get('budgets') or '').split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                picks[key] = value
        for anchor in BUDGET_ANCHORS:
            label = QLabel(anchor['name'])
            label.setStyleSheet(f"color:{anchor['color']}; font-weight:600;")
            box = QComboBox()
            for factor in BUDGET_FACTORS:
                box.addItem(budget_choice_label(anchor, factor['id']), factor['id'])
            current = picks.get(anchor['id'], 'recommended')
            index = box.findData(current)
            if index < 0:
                index = box.findData('recommended')
            self.budget_boxes[anchor['id']] = box
            box.blockSignals(True)
            if index >= 0:
                box.setCurrentIndex(index)
            box.blockSignals(False)
            box.currentIndexChanged.connect(self.on_budget)
            budget_row.addWidget(label)
            budget_row.addWidget(box)
        clear_skips = QPushButton('Clear skips')
        clear_skips.setToolTip('Forget files whose new copy was larger, so the same budget can be tried again.')
        clear_skips.clicked.connect(self.clear_skips)
        budget_row.addStretch(1)
        budget_row.addWidget(clear_skips)
        root.addLayout(budget_row)

        actions = QHBoxLayout()
        self.scan_button = QPushButton('Scan')
        self.convert_button = QPushButton('Convert')
        self.cancel_button = QPushButton('Cancel')
        self.compare_button = QPushButton('Compare')
        self.undo_button = QPushButton('Undo')
        self.delete_button = QPushButton('Delete originals')
        self.help_button = QPushButton('How this works')
        self.scan_button.clicked.connect(self.scan)
        self.convert_button.clicked.connect(self.convert)
        self.cancel_button.clicked.connect(self.request_cancel)
        self.compare_button.clicked.connect(self.compare)
        self.undo_button.clicked.connect(self.undo)
        self.delete_button.clicked.connect(self.delete_originals)
        self.help_button.clicked.connect(lambda: self.show_help(''))
        self.compare_button.setToolTip('Sound is the new file, and it will not stay in sync with the picture. Open the file on disk to confirm the audio.')
        self.convert_button.setToolTip('Shrink every checked video that is bloated, failed, or skipped earlier. The new file takes the original place.')
        self.delete_button.setToolTip('Delete the checked originals that are waiting in .vd-originals. The new files stay. Empty folders from the delete are removed when nothing else is in them.')
        for button in (self.scan_button, self.convert_button, self.cancel_button, self.compare_button, self.undo_button, self.delete_button, self.help_button):
            actions.addWidget(button)
        self.savings = QLabel('')
        actions.addStretch(1)
        actions.addWidget(self.savings)
        root.addLayout(actions)

        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumHeight(180)
        self.activity.setStyleSheet('background:#1c2430; color:#e5e7eb; font-family:monospace;')
        root.addWidget(self.activity)
        bottom = QHBoxLayout()
        self.status = QLabel('Ready')
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.progress, 1)
        root.addLayout(bottom)
        self.folder.textChanged.connect(self.persist)

    def log(self, text, kind='text'):
        self.activity.appendPlainText(str(text))
        plain = self.activity.toPlainText()
        if len(plain) > 180000:
            self.activity.setPlainText(plain[-120000:])
            bar = self.activity.verticalScrollBar()
            bar.setValue(bar.maximum())

    def persist(self):
        if self.loading:
            return
        self.settings.update({
            'folder': self.folder.text().strip(),
            'includeSubfolders': self.subfolders.isChecked(),
            'autoDelete': self.auto.isChecked(),
            'reencodeAudio': self.reencode.isChecked(),
            'detailed': self.detailed.isChecked(),
            'shutdown': self.shutdown.isChecked(),
            'budgets': budget_pick_pack(),
        })
        save_settings(self.settings)

    def on_budget(self):
        packed = []
        for anchor in BUDGET_ANCHORS:
            box = self.budget_boxes[anchor['id']]
            packed.append(anchor['id'] + '=' + str(box.currentData()))
        set_budget_picks(';'.join(packed))
        self.persist()
        self.reprice_rows()

    def on_auto(self, checked):
        if self.loading:
            return
        if checked and not self.settings.get('autoWarnAccepted'):
            if not self.auto_warn():
                self.auto.blockSignals(True)
                self.auto.setChecked(False)
                self.auto.blockSignals(False)
        self.persist()

    def auto_warn(self) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle('Video Dehydrator')
        layout = QVBoxLayout(dialog)
        label = QLabel(
            "Delete originals automatically removes each original as soon as its new file checks out.\n\n"
            "The check looks at size, length, picture size, audio, subtitles, and whether the start and end play. "
            "It does not judge how the picture looks.\n\nAfter the original is deleted, Compare and Undo cannot bring it back."
        )
        label.setWordWrap(True)
        hide = QCheckBox("Don't show this again")
        buttons = QHBoxLayout()
        on = QPushButton('Turn auto on')
        off = QPushButton('Leave it off')
        buttons.addStretch(1)
        buttons.addWidget(on)
        buttons.addWidget(off)
        layout.addWidget(label)
        layout.addWidget(hide)
        layout.addLayout(buttons)
        chosen = {'on': False}
        on.clicked.connect(lambda: (chosen.__setitem__('on', True), dialog.accept()))
        off.clicked.connect(dialog.reject)
        dialog.exec()
        if chosen['on'] and hide.isChecked():
            self.settings['autoWarnAccepted'] = True
        return chosen['on']

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Select folder', self.folder.text())
        if path:
            self.folder.setText(path)

    def set_checks(self, checked):
        for row in self.rows:
            row['checked'] = checked
        self.refill()

    def clear_rows(self):
        self.rows = [row for row in self.rows if row.get('pending_id')]
        self.refill()

    def scan(self):
        if self.work != 'idle':
            return
        root = self.folder.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, 'Video Dehydrator', 'Choose a folder that exists.')
            return
        if missing_tools():
            QMessageBox.warning(self, 'Video Dehydrator', 'FFmpeg and HandBrake need to be in the tools folder. Run the installer again if they are missing.')
            return
        self.persist()
        self.rows = [row for row in self.rows if row.get('pending_id')]
        self.refill()
        self.cancel.clear()
        self.work = 'scan'
        self.awake.start()
        self.status.setText('Listing videos...')
        self.worker = ScanWorker(root, self.subfolders.isChecked(), str(self.tools['ffprobe']), self.skips, self.cancel)
        self.worker.hit.connect(self.add_hit)
        self.worker.progress.connect(self.on_scan_progress)
        self.worker.finished_scan.connect(self.scan_finished)
        self.worker.start()
        self._refresh()

    def add_hit(self, row):
        self.rows.append(row)
        self.refill()

    def on_scan_progress(self, seen, hits, name):
        self.status.setText(f'Listing videos... {seen} seen, {hits} over budget. {name}')

    def scan_finished(self, stats):
        self.work = 'idle'
        self.awake.stop()
        if stats.get('cancelled'):
            self.status.setText('Scan stopped')
            self.log('Scan stopped.', 'text')
        else:
            self.status.setText(f"Scan finished. {stats['hits']} files are over budget.")
            self.log(f"Scan finished. {stats['seen']} videos seen, {stats['hits']} over budget.", 'ok')
            if stats.get('ignored'):
                self.log(f"{stats['ignored']} files stayed off the list because a smaller copy was not smaller last time.", 'text')
        self._refresh()

    def convert(self):
        if self.work != 'idle':
            return
        chosen = [row for row in self.rows if row['checked'] and row['status'] in ('Bloated', 'Failed', 'Skipped')]
        if not chosen:
            return
        if missing_tools():
            QMessageBox.warning(self, 'Video Dehydrator', 'FFmpeg and HandBrake need to be in the tools folder.')
            return
        if self.auto.isChecked():
            answer = QMessageBox.question(
                self, 'Video Dehydrator',
                "Auto is on.\n\nAfter each file checks out, its original is deleted. The new file stays in the original's place. "
                "There will be nothing left to compare. On a shared drive, the matching copy in #recycle is removed when the path and the size match. "
                "The .plexignore from that convert is removed when it only hides the holding folder.",
            )
            if answer != QMessageBox.Yes:
                return
        self.persist()
        self.cancel.clear()
        self.work = 'encode'
        self.awake.start()
        self.saved_bytes = 0
        if self.shutdown.isChecked():
            self.log('The computer will shut down when this convert finishes.', 'text')
        self.worker = ConvertWorker(chosen, self.tools, self.reencode.isChecked(), self.auto.isChecked(), self.detailed.isChecked(), self.skips, self.cancel)
        self.worker.activity.connect(self.log)
        self.worker.progress.connect(lambda value: self.progress.setValue(int(value)))
        self.worker.file_update.connect(self.on_file_update)
        self.worker.finished_job.connect(self.convert_finished)
        self.worker.start()
        self.status.setText('Converting...')
        self._refresh()

    def convert_finished(self, payload):
        self.work = 'idle'
        self.awake.stop()
        self.skips = payload['skips']
        write_skips(self.skips)
        self.saved_bytes = payload['saved']
        self._sync_pending()
        self.refill()
        opened_steps = self.show_recycle_problems(payload['book'])
        if payload['cancelled']:
            self.status.setText('Convert stopped')
            self.log('Convert stopped. The computer stays on.', 'text')
        else:
            self.status.setText(f"Finished. Saved about {format_byte_size(self.saved_bytes)}.")
            if self.shutdown.isChecked() and opened_steps:
                self.log('The manual steps are open, so the computer stays on.', 'text')
            elif self.shutdown.isChecked():
                self.offer_shutdown()
        self._refresh()

    def on_file_update(self, row):
        if row.get('status') == 'Ready to delete' and row.get('pending_id'):
            self._sync_pending()
        self.refill()

    def request_cancel(self):
        if self.work == 'idle':
            return
        self.cancel.set()
        self.status.setText('Stopping...')

    def selected_row(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        index = indexes[0].row()
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def compare(self):
        row = self.selected_row()
        if not row or row['status'] != 'Ready to delete' or not row.get('kept') or not row.get('new'):
            QMessageBox.information(self, 'Video Dehydrator', 'Select a file marked Ready to delete.')
            return
        dialog = CompareDialog(self, str(self.tools['ffmpeg']), str(self.tools['ffplay']), row['kept'], row['new'], row.get('duration') or 0)
        dialog.exec()

    def undo(self):
        ready = [row for row in self.pending if row.get('kept') and os.path.isfile(str(row.get('kept')))]
        if not ready:
            return
        batch = str(ready[-1].get('batch') or '')
        group = [row for row in ready if str(row.get('batch') or '') == batch] if batch else [ready[-1]]
        names = '\n'.join(os.path.basename(str(row.get('source') or row.get('new') or '')) for row in group[:8])
        answer = QMessageBox.question(self, 'Video Dehydrator', f"Undo the last conversion?\n\n{len(group)} file(s) will be put back, and the new files will be deleted.\n\n{names}")
        if answer != QMessageBox.Yes:
            return
        book = RecycleBook()
        for row in group:
            try:
                undo_replaced_file(str(row['source']), str(row['kept']), str(row['new']), book)
                self.pending = [item for item in self.pending if item.get('id') != row.get('id')]
                self.rows = [item for item in self.rows if item.get('pending_id') != row.get('id')]
                self.log(f"Restored {os.path.basename(str(row.get('source')))}.", 'ok')
            except Exception as exc:
                self.log(f"Could not undo {os.path.basename(str(row.get('source') or ''))}. {exc}", 'err')
        write_pending(self.pending)
        self.refill()
        self.show_recycle_problems(book)

    def delete_confirm_hidden(self) -> bool:
        saved = str(self.settings.get('deleteConfirmVersion') or '')
        if not saved.strip():
            return False
        return compare_versions(saved, app_version()) == 0

    def delete_originals(self):
        chosen = [row for row in self.rows if row['checked'] and row['status'] == 'Ready to delete' and row.get('kept')]
        if not chosen:
            return
        if not self.delete_confirm_hidden():
            dialog = QDialog(self)
            dialog.setWindowTitle('Video Dehydrator')
            layout = QVBoxLayout(dialog)
            word = 'original' if len(chosen) == 1 else 'originals'
            label = QLabel(
                f"Delete {len(chosen)} {word}?\n\nThe new smaller files stay where the originals were. "
                "On a shared drive, the matching copy in #recycle is removed when the path and the size match. "
                "The .plexignore from that convert is removed when it only hides the holding folder."
            )
            label.setWordWrap(True)
            hide = QCheckBox("Don't show this in the future")
            hint = QLabel('This question comes back when a new version of the program is installed.')
            buttons = QHBoxLayout()
            delete = QPushButton('Delete')
            cancel = QPushButton('Cancel')
            buttons.addStretch(1)
            buttons.addWidget(delete)
            buttons.addWidget(cancel)
            layout.addWidget(label)
            layout.addWidget(hide)
            layout.addWidget(hint)
            layout.addLayout(buttons)
            chosen_delete = {'ok': False, 'hide': False}
            def accept():
                chosen_delete['ok'] = True
                chosen_delete['hide'] = hide.isChecked()
                dialog.accept()
            delete.clicked.connect(accept)
            cancel.clicked.connect(dialog.reject)
            dialog.exec()
            if not chosen_delete['ok']:
                return
            if chosen_delete['hide']:
                self.settings['deleteConfirmVersion'] = app_version()
                self.persist()
        book = RecycleBook()
        removed = 0
        for row in chosen:
            try:
                remove_kept_original(row['kept'], row.get('new') or '', book)
                self.pending = [item for item in self.pending if item.get('id') != row.get('pending_id')]
                row['status'] = 'Original deleted'
                row['kept'] = ''
                row['pending_id'] = ''
                row['checked'] = False
                removed += 1
                self.log(f"Deleted original of {row['name']}.", 'ok')
            except Exception as exc:
                self.log(f"Could not delete {os.path.basename(row.get('kept') or '')}. {exc}", 'err')
        write_pending(self.pending)
        self.refill()
        self.status.setText(f'Deleted {removed} originals.')
        self.show_recycle_problems(book)

    def show_recycle_problems(self, book: RecycleBook) -> bool:
        problems = book.problems()
        if not problems:
            return False
        for row in problems:
            row['shown'] = True
            self.log(row.get('message') or '', 'err')
        dialog = QDialog(self)
        dialog.setWindowTitle('Video Dehydrator')
        dialog.resize(560, 420)
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "The original is already deleted from the library. The drive recycle bin may still have a copy, and the program could not remove it.\n\n"
            "The button below opens the manual steps. Those steps remove only .vd-originals folders inside #recycle."
        )
        intro.setWordWrap(True)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText('\n\n'.join((row.get('name') or '') + '\n' + (row.get('message') or '') + '\n' + (row.get('recycle_file') or '') for row in problems[:8]))
        steps = QPushButton('Show the manual deletion steps')
        close = QPushButton('Close')
        opened = {'steps': False}
        def open_steps():
            opened['steps'] = True
            dialog.accept()
            self.show_help('Drive recycle bin')
        steps.clicked.connect(open_steps)
        close.clicked.connect(dialog.reject)
        layout.addWidget(intro)
        layout.addWidget(detail, 1)
        layout.addWidget(steps)
        layout.addWidget(close)
        dialog.exec()
        return opened['steps']

    def show_help(self, section):
        dialog = QDialog(self)
        dialog.setWindowTitle('How this works')
        dialog.resize(760, 640)
        layout = QHBoxLayout(dialog)
        names = QComboBox()
        browser = QTextBrowser()
        blocks = []
        for title, body in helptext.SECTIONS:
            names.addItem(title)
            anchor = _help_anchor(title)
            text = html.escape(body).replace('\n', '<br>')
            blocks.append(f'<a name="{anchor}"></a><h2>{html.escape(title)}</h2><p>{text}</p>')
        browser.setHtml(''.join(blocks))
        def jump():
            browser.scrollToAnchor(_help_anchor(names.currentText()))
        names.currentIndexChanged.connect(lambda _index: jump())
        if section:
            index = names.findText(section)
            if index >= 0:
                names.setCurrentIndex(index)
        QTimer.singleShot(0, jump)
        side = QVBoxLayout()
        side.addWidget(names)
        side.addStretch(1)
        layout.addLayout(side)
        layout.addWidget(browser, 1)
        dialog.exec()

    def offer_shutdown(self):
        try:
            subprocess.run(['shutdown', '-h', '+1', 'Video Dehydrator finished converting.'], check=False)
        except OSError:
            self.log('The computer will stay on. Shutdown could not be scheduled.', 'err')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Video Dehydrator')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('The computer will shut down in about one minute.'))
        buttons = QHBoxLayout()
        cancel = QPushButton('Cancel')
        now = QPushButton('Shut down now')
        buttons.addWidget(cancel)
        buttons.addWidget(now)
        layout.addLayout(buttons)
        choice = {'now': False, 'cancel': False}
        def do_cancel():
            choice['cancel'] = True
            dialog.accept()
        def do_now():
            choice['now'] = True
            dialog.accept()
        cancel.clicked.connect(do_cancel)
        now.clicked.connect(do_now)
        dialog.exec()
        if choice['cancel'] or not choice['now']:
            subprocess.run(['shutdown', '-c'], check=False)
            self.log('Shutdown cancelled. The computer stays on.', 'text')
            return
        subprocess.run(['shutdown', '-h', '+0', 'Video Dehydrator finished converting.'], check=False)

    def clear_skips(self):
        count = len(self.skips)
        if count < 1:
            return
        answer = QMessageBox.question(self, 'Video Dehydrator', f'Forget {count} file(s) that were skipped because the new copy was larger?\n\nThe next scan can list them again.')
        if answer != QMessageBox.Yes:
            return
        self.skips = []
        write_skips(self.skips)
        self.log(f'Forgot {count} skipped files. Scan again to list them.', 'text')

    def report_bug(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Video Dehydrator')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Report a bug opens a GitHub page. This version and the system name are filled in. Nothing else is added unless you check the box.\n\nThis program does not send the report.'))
        include = QCheckBox('Include latest activity')
        include.setEnabled(bool(self.activity.toPlainText().strip()))
        buttons = QHBoxLayout()
        open_button = QPushButton('Open report')
        cancel = QPushButton('Cancel')
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(cancel)
        layout.addWidget(include)
        layout.addLayout(buttons)
        go = {'open': False}
        def accept():
            go['open'] = True
            dialog.accept()
        open_button.clicked.connect(accept)
        cancel.clicked.connect(dialog.reject)
        dialog.exec()
        if not go['open']:
            return
        activity = ''
        if include.isChecked():
            lines = [line for line in self.activity.toPlainText().splitlines() if line.strip()]
            activity = '\n'.join(lines[-40:])
            while activity and len(quote(activity)) > 4500:
                activity = '\n'.join(activity.splitlines()[1:])
        url = ISSUE_URL + '&version=' + quote(app_version()) + '&windows=' + quote(os_label())
        if activity:
            url += '&activity=' + quote(activity)
        self.open_url(url)

    def open_url(self, url):
        allowed = url == KOFI or url.startswith(ISSUE_URL)
        if not allowed:
            return
        webbrowser.open(url)

    def check_for_updates(self):
        worker = getattr(self, 'update_worker', None)
        if worker is not None and worker.isRunning():
            return
        self.status.setText('Checking for updates...')

        class UpdateWorker(QThread):
            found = Signal(object)
            def run(self_inner):
                try:
                    request = Request(RELEASES, headers={'User-Agent': 'VideoDehydrator', 'Accept': 'application/vnd.github+json'})
                    with urlopen(request, timeout=20) as response:
                        import json
                        payload = json.loads(response.read().decode('utf-8'))
                except Exception as exc:
                    self_inner.found.emit({'error': str(exc)})
                    return
                tag = str(payload.get('tag_name') or '')
                assets = payload.get('assets') or []
                linux_asset = None
                for asset in assets:
                    name = str(asset.get('name') or '')
                    lowered = name.lower()
                    if 'linux' in lowered and lowered.endswith(('.tar.gz', '.tar.xz', '.run')):
                        linux_asset = {
                            'name': name,
                            'size': int(asset.get('size') or 0),
                            'url': str(asset.get('browser_download_url') or ''),
                        }
                self_inner.found.emit({'tag': tag, 'asset': linux_asset})

        self.update_worker = UpdateWorker()
        self.update_worker.found.connect(self.update_found)
        self.update_worker.start()

    def update_found(self, payload):
        if payload.get('error'):
            self.status.setText('Could not check for updates.')
            self.log('Could not check for updates. ' + payload['error'], 'err')
            return
        tag = str(payload.get('tag') or '')
        newer = compare_versions(tag, app_version())
        if newer is None or newer <= 0:
            self.update_offer = None
            self.status.setText('This is the newest published version.')
            self.update_button.setEnabled(False)
            return
        remote = tag[1:] if tag[:1].lower() == 'v' else tag
        asset = payload.get('asset')
        self.update_offer = {'tag': remote, 'asset': asset}
        self.status.setText(f'Version {remote} is available.')
        self.update_button.setEnabled(bool(asset and asset.get('url')))
        if asset:
            self.update_button.setToolTip(f"Download {asset.get('name')} and run its installer.")
        else:
            self.update_button.setToolTip('The published file is the Windows installer. This Linux copy does not download it.')
            self.log(f'Version {remote} is published for Windows. This Linux copy does not download that installer.', 'text')
        saved = str(self.settings.get('updatePrompt') or '')
        if compare_versions(saved, remote) == 0:
            return
        self.show_update_ready(remote, bool(asset))

    def show_update_ready(self, remote, has_linux):
        text = f'Version {remote} is available.'
        if not has_linux:
            text += '\n\nThe published file is the Windows installer. This Linux copy does not download it.'
        QMessageBox.information(self, 'Video Dehydrator', text)
        self.settings['updatePrompt'] = remote
        self.persist()

    def install_update(self):
        offer = self.update_offer or {}
        asset = offer.get('asset') or {}
        name = str(asset.get('name') or '')
        url = str(asset.get('url') or '')
        size = int(asset.get('size') or 0)
        lowered = name.lower()
        if 'linux' not in lowered or not lowered.endswith(('.tar.gz', '.tar.xz', '.run')):
            return
        prefix = 'https://github.com/philipvern-dot/video-dehydrator/releases/download/'
        if not url.startswith(prefix):
            return
        if size > 500 * 1024 * 1024:
            QMessageBox.warning(self, 'Video Dehydrator', 'That published file is too large to install from here.')
            return
        answer = QMessageBox.question(
            self, 'Video Dehydrator',
            f'Download {name} and run its installer?\n\nThe program closes after the download checks out.',
        )
        if answer != QMessageBox.Yes:
            return
        self.update_button.setEnabled(False)
        self.status.setText('Downloading the update...')

        class DownloadWorker(QThread):
            done = Signal(object)
            def run(self_inner):
                try:
                    request = Request(url, headers={'User-Agent': 'VideoDehydrator', 'Accept': 'application/octet-stream'})
                    with urlopen(request, timeout=300) as response:
                        data = response.read(500 * 1024 * 1024 + 1)
                    if len(data) > 500 * 1024 * 1024:
                        self_inner.done.emit({'error': 'The download is larger than this installer accepts.'})
                        return
                    if size and len(data) != size:
                        self_inner.done.emit({'error': f'The download is {len(data)} bytes. The published file is {size} bytes.'})
                        return
                    folder = Path(tempfile.mkdtemp(prefix='video-dehydrator-'))
                    archive = folder / name
                    archive.write_bytes(data)
                    if lowered.endswith('.run'):
                        archive.chmod(0o755)
                        self_inner.done.emit({'script': str(archive)})
                        return
                    unpack = folder / 'unpack'
                    unpack.mkdir()
                    with tarfile.open(archive) as tar:
                        _safe_extract(tar, unpack)
                    scripts = [
                        path for path in unpack.rglob('install.sh')
                        if path.is_file() and '__MACOSX' not in path.parts
                    ]
                    if len(scripts) != 1:
                        self_inner.done.emit({'error': 'The archive does not contain one install.sh.', 'folder': str(unpack)})
                        return
                    scripts[0].chmod(0o755)
                    self_inner.done.emit({'script': str(scripts[0])})
                except Exception as exc:
                    self_inner.done.emit({'error': str(exc)})

        self.download_worker = DownloadWorker()
        self.download_worker.done.connect(self.update_downloaded)
        self.download_worker.start()

    def update_downloaded(self, payload):
        if payload.get('error'):
            self.update_button.setEnabled(True)
            self.status.setText('The update was not installed.')
            self.log('The update was not installed. ' + payload['error'], 'err')
            return
        script = payload.get('script')
        if not script:
            return
        subprocess.Popen(['bash', script], cwd=str(Path(script).parent), start_new_session=True)
        QApplication.quit()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._update_started:
            self._update_started = True
            QTimer.singleShot(600, self.check_for_updates)

    def apply_folder(self):
        name = self.folder_pick.currentText()
        if name == 'All folders':
            return
        for row in self.rows:
            parent = os.path.dirname(row.get('source') or row['path'])
            row['checked'] = parent == name and row['status'] in ('Bloated', 'Failed')
        self.refill()

    def apply_over(self):
        label = self.over_pick.currentText().lower()
        if label == 'all overages':
            return
        for row in self.rows:
            row['checked'] = over_band_id(row.get('over')) == label
        self.refill()

    def reprice_rows(self):
        kept = []
        for row in self.rows:
            if row['status'] not in ('Bloated', 'Failed', 'Skipped'):
                kept.append(row)
                continue
            probe = row.get('probe')
            if not probe:
                kept.append(row)
                continue
            decision = bloat_decision(probe)
            if decision['decision'] in ('lean', 'within'):
                continue
            if decision['decision'] != 'bloated':
                kept.append(row)
                continue
            row['budget'] = decision['budget_gibph']
            row['target'] = decision['target_kbps']
            row['gibph'] = decision['gibph']
            row['over'] = over_ratio(decision['gibph'], decision['budget_gibph'])
            row['estimate'] = decision['estimate_bytes']
            row['estimate_rough'] = decision['estimate_rough']
            if row['status'] == 'Bloated':
                row['reason'] = decision['reason']
            kept.append(row)
        self.rows = kept
        self.refill()

    def sort_column(self, index):
        def key(row):
            if index == 0:
                return row['name'].casefold()
            if index == 6:
                return row.get('over') or -1
            if index == 4:
                return row.get('gibph') or -1
            if index == 3:
                return row.get('size') or 0
            if index == 9:
                return row['status']
            return row['name'].casefold()
        descending = False
        if index == 6:
            descending = self._sort_over_desc
            self._sort_over_desc = not self._sort_over_desc
        self.rows.sort(key=key, reverse=descending)
        self.refill()

    def on_item_changed(self, item):
        if self.loading or item.column() != 0:
            return
        index = item.row()
        if 0 <= index < len(self.rows):
            self.rows[index]['checked'] = item.checkState() == Qt.Checked
            self.update_savings()

    def add_table_row(self, row):
        index = self.table.rowCount()
        self.table.insertRow(index)
        name = QTableWidgetItem(row['name'])
        name.setFlags(name.flags() | Qt.ItemIsUserCheckable)
        name.setCheckState(Qt.Checked if row['checked'] else Qt.Unchecked)
        values = [
            name,
            QTableWidgetItem(f"{row['width']}x{row['height']}" if row.get('width') else ''),
            QTableWidgetItem(format_clock(row.get('duration') or 0) if row.get('duration') else ''),
            QTableWidgetItem(self.size_text(row)),
            QTableWidgetItem(format_rate(row.get('gibph'))),
            QTableWidgetItem(format_rate(row.get('budget'))),
            QTableWidgetItem(format_over_ratio(row.get('over'))),
            QTableWidgetItem('' if not row.get('target') else str(row['target'])),
            QTableWidgetItem(self.new_size_text(row)),
            QTableWidgetItem(row['status']),
        ]
        for column, cell in enumerate(values):
            self.table.setItem(index, column, cell)
        anchor = next((item for item in BUDGET_ANCHORS if item['id'] == row.get('anchor')), None)
        if anchor:
            self.table.item(index, 1).setForeground(QColor(anchor['color']))
        band = over_band_id(row.get('over'))
        if band in OVER_COLORS and row['status'] in ('Bloated', 'Failed'):
            back, fore = OVER_COLORS[band]
            self.table.item(index, 6).setBackground(QColor(back))
            self.table.item(index, 6).setForeground(QColor(fore))

    def size_text(self, row) -> str:
        size = row.get('size') or 0
        old = row.get('old_bytes') or 0
        if old and size and old != size:
            return f"{format_byte_size(size)} from {format_byte_size(old)}"
        return format_byte_size(size) if size else ''

    def new_size_text(self, row) -> str:
        if row['status'] in ('Ready to delete', 'Original deleted'):
            return ''
        estimate = row.get('estimate')
        if not estimate:
            return ''
        text = format_byte_size(int(estimate))
        if row.get('estimate_rough'):
            return '~' + text
        return text

    def refill(self):
        self.loading = True
        self.table.setRowCount(0)
        folders = []
        for row in self.rows:
            self.add_table_row(row)
            parent = os.path.dirname(row.get('source') or row['path'])
            if row['status'] in ('Bloated', 'Failed') and parent not in folders:
                folders.append(parent)
        current = self.folder_pick.currentText()
        self.folder_pick.blockSignals(True)
        self.folder_pick.clear()
        self.folder_pick.addItem('All folders')
        for folder in folders:
            self.folder_pick.addItem(folder)
        index = self.folder_pick.findText(current)
        if index >= 0:
            self.folder_pick.setCurrentIndex(index)
        self.folder_pick.blockSignals(False)
        self.loading = False
        self.update_savings()
        self._refresh()

    def update_savings(self):
        total = 0
        any_row = False
        rough = False
        for row in self.rows:
            if row['checked'] and row['status'] in ('Bloated', 'Failed', 'Skipped') and row.get('estimate') and row.get('size'):
                total += max(0, int(row['size']) - int(row['estimate']))
                any_row = True
                if row.get('estimate_rough'):
                    rough = True
        mark = '~' if rough else ''
        self.savings.setText('' if not any_row else f'Estimated savings {mark}{format_byte_size(total)}')

    def _load_pending_rows(self):
        for item in self.pending:
            if not item.get('kept') or not os.path.isfile(str(item.get('kept'))):
                continue
            self.rows.append({
                'path': item.get('new') or '',
                'name': os.path.basename(str(item.get('new') or item.get('source') or '')),
                'checked': False,
                'status': 'Ready to delete',
                'width': item.get('width') or 0,
                'height': item.get('height') or 0,
                'duration': item.get('duration') or 0,
                'size': item.get('new_bytes') or 0,
                'old_bytes': item.get('old_bytes') or 0,
                'gibph': None,
                'budget': None,
                'over': None,
                'target': item.get('target') or '',
                'estimate': None,
                'estimate_rough': False,
                'video_bitrate': 0,
                'codec': '',
                'audio': [],
                'subs': 0,
                'reason': 'Held original',
                'anchor': '',
                'kept': item.get('kept') or '',
                'new': item.get('new') or '',
                'source': item.get('source') or '',
                'pending_id': item.get('id') or '',
                'batch': item.get('batch') or '',
                'probe': None,
            })
        self.refill()

    def _sync_pending(self):
        existing = {item.get('id'): item for item in self.pending if item.get('id')}
        for row in self.rows:
            if row['status'] == 'Ready to delete' and row.get('pending_id'):
                existing[row['pending_id']] = {
                    'id': row['pending_id'],
                    'source': row.get('source') or '',
                    'kept': row.get('kept') or '',
                    'new': row.get('new') or row.get('path') or '',
                    'batch': row.get('batch') or '',
                    'width': row.get('width') or 0,
                    'height': row.get('height') or 0,
                    'duration': row.get('duration') or 0,
                    'old_bytes': row.get('old_bytes') or 0,
                    'new_bytes': row.get('size') or 0,
                    'target': row.get('target') or 0,
                }
            elif row['status'] == 'Original deleted' and row.get('pending_id') in existing:
                existing.pop(row['pending_id'], None)
        self.pending = [item for item in existing.values() if item.get('kept') and os.path.isfile(str(item.get('kept')))]
        write_pending(self.pending)

    def _refresh(self):
        busy = self.work != 'idle'
        self.scan_button.setEnabled(not busy)
        self.convert_button.setEnabled((not busy) and any(row['checked'] and row['status'] in ('Bloated', 'Failed', 'Skipped') for row in self.rows))
        self.cancel_button.setEnabled(busy)
        self.delete_button.setEnabled((not busy) and any(row['checked'] and row['status'] == 'Ready to delete' for row in self.rows))
        self.compare_button.setEnabled(not busy)
        self.undo_button.setEnabled((not busy) and any(os.path.isfile(str(item.get('kept') or '')) for item in self.pending))
        self.shutdown.setEnabled(not busy)
        self.auto.setEnabled(not busy)

    def closeEvent(self, event):
        self.cancel.set()
        self.awake.stop()
        self.persist()
        super().closeEvent(event)


def _help_anchor(title: str) -> str:
    slug = ''.join(ch.lower() if ch.isalnum() else '-' for ch in title)
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug.strip('-')


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    root = dest.resolve()
    for member in tar.getmembers():
        name = member.name.replace('\\', '/')
        if name.startswith('/') or name.startswith('../') or '/../' in name or name == '..':
            raise RuntimeError('The archive has an unsafe path.')
        if member.issym() or member.islnk():
            raise RuntimeError('The archive contains a link. It was not unpacked.')
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError('The archive has an unsafe path.')
    tar.extractall(root)


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    QThread.currentThread()
    app.aboutToQuit.connect(window.awake.stop)
    return app.exec()
