"""Desktop window. The same jobs as the Windows program, on any Linux desktop."""

from __future__ import annotations

import html
import math
import os
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from PySide6.QtCore import QEvent, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from . import helptext
from .compare import CompareDialog
from .engine import (
    BUDGET_ANCHORS, BUDGET_FACTORS, anchor_budget, app_version, bloat_decision,
    budget_choice_label, budget_pick_pack, bundled_tools, compare_versions,
    linux_release_is_update,
    encoder_name, estimated_savings_bytes, force_convertible, force_video_paths,
    format_budget_gib, format_byte_size, format_clock, format_eta_text, format_force_report,
    format_over_ratio, format_rate, handbrake_arguments, handbrake_progress,
    is_encode_progress_line, is_video_file, larger_file_note,
    encode_rate, live_encode_rate, mean_sample, median_sample, merge_skip_row, missing_tools, nearest_budget_anchor,
    over_band_id, over_ratio, pixel_seconds, probe_file, remaining_estimate,
    remembered_skip, remove_kept_original, remove_skip_row, replace_plan,
    replace_with_encode, run_lines, set_budget_picks, size_skip_reason,
    space_saved_bytes, target_kbps, test_encoded_file, undo_replaced_file,
)
from .recycle import RecycleBook, set_purge_enabled
from .store import (
    append_convert_log, load_settings, read_pending, read_skips, save_settings,
    write_pending, write_skips,
)

COLUMNS = [
    'File', 'Dimensions', 'Length', 'Size', 'GiB/h', 'Budget', 'Ratio', 'Target',
    'New size', 'Status', 'Space Saved', '~Savings',
]
CONVERTIBLE = ('Bloated', 'Can shrink', 'Failed', 'Skipped', 'Forced')
FOLDER_STATUSES = ('Bloated', 'Can shrink', 'Failed')
SHOW_FILTERS = (
    ('shrink', 'Can shrink'),
    ('bloated', 'Bloated'),
    ('slight', 'Slight'),
    ('heavy', 'Heavy'),
    ('extreme', 'Extreme'),
    ('failed', 'Failed'),
    ('skipped', 'Skipped'),
    ('ready', 'Ready to delete'),
    ('finished', 'Finished'),
)
OVER_COLORS = {
    'slight': ('#dcfce7', '#166534'),
    'heavy': ('#fef9c3', '#854d0e'),
    'extreme': ('#fee2e2', '#991b1b'),
}
ISSUE_URL = 'https://github.com/philipvern-dot/video-dehydrator/issues/new?template=bug_report.yml'
KOFI = 'https://ko-fi.com/philipvern'
RELEASES = 'https://api.github.com/repos/philipvern-dot/video_dehydrator_linux/releases/latest'


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
                kind = decision['decision']
                if kind == 'bloated':
                    status = 'Bloated'
                elif kind == 'shrink':
                    status = 'Can shrink'
                else:
                    continue
                if remembered_skip(self.skips, entry.path, probe.size_bytes, decision['target_kbps'] or 0):
                    stats['ignored'] += 1
                    continue
                stats['hits'] += 1
                if status == 'Can shrink':
                    stats['shrink'] = stats.get('shrink', 0) + 1
                self.hit.emit(row_from_probe(entry.path, probe, decision, status))
        self.finished_scan.emit(stats)


def row_from_probe(path, probe, decision, status):
    anchor = nearest_budget_anchor(probe.width, probe.height)
    ratio = over_ratio(decision['gibph'], decision['budget_gibph'])
    return {
        'path': path,
        'name': os.path.basename(path),
        'checked': status in ('Bloated', 'Can shrink', 'Failed', 'Forced'),
        'status': status,
        'width': probe.width,
        'height': probe.height,
        'duration': probe.duration_sec,
        'size': probe.size_bytes,
        'old_bytes': None,
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
        'ten_bit': encoder_name(probe) == 'x265_10bit',
        'kept': '',
        'new': '',
        'source': path,
        'pending_id': '',
        'batch': '',
        'probe': probe,
    }


def show_criteria(row) -> list:
    found = []
    status = str(row.get('status') or '')
    band = ''
    if status in ('Bloated', 'Failed'):
        band = over_band_id(row.get('over'))
    if status == 'Can shrink':
        found.append('shrink')
    if status == 'Bloated':
        found.append('bloated')
    if band:
        found.append(band)
    if status == 'Failed':
        found.append('failed')
    if status == 'Skipped':
        found.append('skipped')
    if status == 'Ready to delete':
        found.append('ready')
    if not found:
        found.append('finished')
    return found


class ConvertWorker(QThread):
    activity = Signal(str, str)
    progress = Signal(float)
    file_update = Signal(object)
    finished_job = Signal(object)
    eta = Signal(str)

    def __init__(self, rows, tools, reencode, delete_original, detailed, skips, cancel, force=False):
        super().__init__()
        self.rows = rows
        self.tools = tools
        self.reencode = reencode
        self.delete_original = delete_original
        self.detailed = detailed
        self.skips = list(skips)
        self.cancel = cancel
        self.force = force
        self.stop_after = threading.Event()
        self.book = RecycleBook()
        self.batch = uuid.uuid4().hex
        self.saved = 0
        self.file_index = 0
        self.rates8 = []
        self.rates10 = []
        self.live8 = []
        self.live10 = []
        self.ratios = []
        self.file_samples = []
        self.batch_samples = []
        self.shown_at = None
        self.shown_key = ''
        self._reset_clock()

    def _reset_clock(self):
        self.pass_started = time.monotonic()
        self.pass1_sec = 0.0
        self.task = 0
        self.progress_info = None
        self.checking = False
        self.remembered = False
        self.ten_bit = False
        self.pixels = 0.0
        self.live8 = []
        self.live10 = []
        self.file_samples = []
        self.batch_samples = []
        self.shown_at = None
        self.shown_key = ''

    def _add_sample(self, bucket, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if number <= 0:
            return
        bucket.append(number)
        del bucket[:-8]

    def _remember_finished(self):
        if self.remembered or self.task < 2 or self.pass1_sec < 1 or self.pixels <= 0:
            return
        pass2 = time.monotonic() - self.pass_started
        if pass2 < 1:
            return
        rate = (self.pass1_sec + pass2) / self.pixels
        ratio = pass2 / self.pass1_sec
        if self.ten_bit:
            self._add_sample(self.rates10, rate)
        else:
            self._add_sample(self.rates8, rate)
        self._add_sample(self.ratios, ratio)
        self.remembered = True

    def _note_pass(self, info):
        task = int(info.get('task') or 0)
        if self.task >= 1 and task > self.task and self.task == 1 and task >= 2:
            self.pass1_sec = max(0.0, time.monotonic() - self.pass_started)
            self.pass_started = time.monotonic()
        elif task > self.task:
            self.pass_started = time.monotonic()
        if task > self.task:
            self.live8 = []
            self.live10 = []
            self.task = task
        self.progress_info = info

    def _chosen_rate(self, ten):
        finished = self.rates10 if ten else self.rates8
        live = 0.0
        part = 0.0
        info = self.progress_info
        on_last = info and int(info.get('task') or 0) >= int(info.get('tasks') or 0) and self.task >= 2
        if on_last and ten == self.ten_bit and not self.checking:
            part = float(info.get('part') or 0) / 100.0
            live = live_encode_rate(self.pass1_sec, time.monotonic() - self.pass_started, part, self.pixels)
            if live > 0:
                bucket = self.live10 if ten else self.live8
                self._add_sample(bucket, live)
                live = median_sample(bucket)
        return encode_rate(live, finished, part)

    def _waiting(self):
        if self.stop_after.is_set():
            return []
        waiting = []
        for row in self.rows[self.file_index + 1:]:
            pixels = pixel_seconds(row.get('width'), row.get('height'), row.get('duration'))
            waiting.append({
                'pixel_seconds': pixels,
                'ten_bit': bool(row.get('ten_bit')),
                'known': pixels > 0,
            })
        return waiting

    def _publish_key(self, phase, estimate):
        task = int((self.progress_info or {}).get('task') or 0)
        stop = 'stop' if self.stop_after.is_set() else 'run'
        batch = 'ready' if estimate.get('have_batch') else 'wait'
        return f"{phase}|{estimate.get('file_kind')}|{batch}|{stop}|{task}"

    def _window_add(self, bucket, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if number <= 0 or math.isnan(number) or math.isinf(number):
            return
        bucket.append(number)
        del bucket[:-160]

    def _emit_eta(self):
        phase = 'check' if self.checking else 'encode'
        estimate = remaining_estimate(
            self.progress_info,
            pass_elapsed_sec=time.monotonic() - self.pass_started,
            pass1_sec=self.pass1_sec,
            pass_ratio=mean_sample(self.ratios),
            rate8=self._chosen_rate(False),
            rate10=self._chosen_rate(True),
            waiting=self._waiting(),
            phase=phase,
        )
        key = self._publish_key(phase, estimate)
        key_changed = key != self.shown_key
        if key_changed:
            self.file_samples = []
            self.batch_samples = []
        if float(estimate.get('file_seconds') or 0) > 0:
            self._window_add(self.file_samples, estimate['file_seconds'])
        if estimate.get('have_batch') and float(estimate.get('batch_seconds') or 0) > 0:
            self._window_add(self.batch_samples, estimate['batch_seconds'])
        file_seconds = median_sample(self.file_samples) or float(estimate.get('file_seconds') or 0)
        batch_seconds = float(estimate.get('batch_seconds') or 0)
        if estimate.get('have_batch'):
            median_batch = median_sample(self.batch_samples)
            if median_batch > 0:
                batch_seconds = median_batch
        text = format_eta_text(
            self.progress_info, estimate.get('file_kind'), file_seconds, batch_seconds, bool(estimate.get('have_batch')),
        )
        since = 999.0 if self.shown_at is None else time.monotonic() - self.shown_at
        if key_changed or since >= 30:
            self.eta.emit(text)
            self.shown_at = time.monotonic()
            self.shown_key = key
            if not key_changed:
                self.file_samples = []
                self.batch_samples = []
                if file_seconds > 0:
                    self._window_add(self.file_samples, file_seconds)
                if estimate.get('have_batch') and batch_seconds > 0:
                    self._window_add(self.batch_samples, batch_seconds)

    def run(self):
        stopped = False
        for index, row in enumerate(self.rows):
            if self.cancel.is_set():
                break
            self.file_index = index
            self.convert_row(row)
            if self.cancel.is_set():
                break
            if self.stop_after.is_set():
                stopped = True
                break
        self.eta.emit('')
        self.finished_job.emit({
            'book': self.book,
            'skips': self.skips,
            'saved': self.saved,
            'cancelled': self.cancel.is_set(),
            'stopped_after': stopped,
            'batch': self.batch,
        })

    def _restore_status(self, row, previous):
        if previous in ('Failed', 'Skipped', 'Can shrink', 'Forced'):
            row['status'] = previous
        else:
            row['status'] = 'Bloated'
        row['checked'] = True

    def convert_row(self, row):
        source = row['source'] or row['path']
        previous = row['status']
        verb = 'Forcing' if self.force else 'Converting'
        self.activity.emit(f"{verb} {os.path.basename(source)}.", 'text')
        row['status'] = 'Converting'
        self.file_update.emit(row)
        self.progress.emit(0)
        self._reset_clock()
        self._emit_eta()
        if self.cancel.is_set():
            self._restore_status(row, previous)
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
        decision = bloat_decision(probe)
        if self.force and not force_convertible(decision):
            row['status'] = 'Left alone'
            row['checked'] = False
            row['reason'] = decision.get('reason') or 'Left alone.'
            self.activity.emit(f"Left {os.path.basename(source)} alone. {row['reason']}", 'text')
            self.file_update.emit(row)
            return
        row['ten_bit'] = encoder_name(probe) == 'x265_10bit'
        row['width'] = probe.width
        row['height'] = probe.height
        row['duration'] = probe.duration_sec
        self.ten_bit = bool(row['ten_bit'])
        self.pixels = pixel_seconds(probe.width, probe.height, probe.duration_sec)
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
            info = handbrake_progress(line)
            if info is not None:
                self._note_pass(info)
                self.progress.emit(info['overall'])
                self._emit_eta()
            elif self.detailed and line and not is_encode_progress_line(line):
                self.activity.emit(line, 'dim')

        code, _joined = run_lines([str(self.tools['handbrake'])] + args, on_line, self.cancel)
        handbrake_log = '\n'.join(log_lines)
        if self.cancel.is_set():
            if os.path.isfile(plan['temp']):
                os.remove(plan['temp'])
            self._restore_status(row, previous)
            self.activity.emit(f"Stopped {os.path.basename(source)}. The original stayed.", 'text')
            self.file_update.emit(row)
            return
        self._remember_finished()
        self.checking = True
        self._emit_eta()
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


class ForceWorker(QThread):
    progress = Signal(int, str)
    finished_walk = Signal(object)

    def __init__(self, path, recurse, probe_files, ffprobe, cancel):
        super().__init__()
        self.path = path
        self.recurse = recurse
        self.probe_files = probe_files
        self.ffprobe = ffprobe
        self.cancel = cancel

    def run(self):
        files = []
        try:
            paths = force_video_paths(self.path, self.recurse)
        except Exception as exc:
            self.finished_walk.emit({'cancelled': False, 'files': [], 'error': str(exc)})
            return
        for index, file_path in enumerate(paths, 1):
            if self.cancel.is_set():
                self.finished_walk.emit({'cancelled': True, 'files': files})
                return
            name = os.path.basename(file_path)
            self.progress.emit(index, name)
            if not self.probe_files:
                try:
                    size = os.path.getsize(file_path)
                except OSError:
                    size = 0
                files.append({
                    'path': file_path, 'name': name, 'can_convert': True, 'reason': '',
                    'width': 0, 'height': 0, 'duration': 0, 'size': size, 'estimate': None,
                    'estimate_rough': False, 'ten_bit': False, 'target': 0, 'gibph': None,
                    'budget': None, 'over': None, 'video_bitrate': 0, 'codec': '', 'anchor': '',
                    'probe': None, 'decision': '',
                })
                continue
            try:
                probe = probe_file(self.ffprobe, file_path)
                decision = bloat_decision(probe)
                anchor = nearest_budget_anchor(probe.width, probe.height)
                files.append({
                    'path': file_path,
                    'name': name,
                    'can_convert': force_convertible(decision),
                    'reason': decision.get('reason') or '',
                    'width': probe.width,
                    'height': probe.height,
                    'duration': probe.duration_sec,
                    'size': probe.size_bytes,
                    'estimate': decision.get('estimate_bytes'),
                    'estimate_rough': decision.get('estimate_rough'),
                    'ten_bit': encoder_name(probe) == 'x265_10bit',
                    'target': decision.get('target_kbps') or 0,
                    'gibph': decision.get('gibph'),
                    'budget': decision.get('budget_gibph'),
                    'over': over_ratio(decision.get('gibph'), decision.get('budget_gibph')),
                    'video_bitrate': probe.video_bitrate,
                    'codec': probe.video_codec,
                    'anchor': anchor['id'] if anchor else '',
                    'probe': probe,
                    'decision': decision.get('decision') or '',
                })
            except Exception as exc:
                files.append({
                    'path': file_path, 'name': name, 'can_convert': False, 'reason': str(exc),
                    'width': 0, 'height': 0, 'duration': 0, 'size': 0, 'estimate': None,
                    'estimate_rough': False, 'ten_bit': False, 'target': 0, 'gibph': None,
                    'budget': None, 'over': None, 'video_bitrate': 0, 'codec': '', 'anchor': '',
                    'probe': None, 'decision': 'error',
                })
        self.finished_walk.emit({'cancelled': False, 'files': files})


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
        self.show_filters = {key: False for key, _label in SHOW_FILTERS}
        self._sort_desc = {6: True, 10: True, 11: True}
        self._split_ready = False
        self._default_table = 0
        set_purge_enabled(bool(self.settings.get('purgeRecycle')))
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
        self.select_all = QPushButton('Select all')
        self.select_none = QPushButton('Deselect all')
        self.clear_button = QPushButton('Clear')
        self.show_button = QPushButton('Showing all')
        self.select_shown = QPushButton('Select shown')
        self.select_all.clicked.connect(lambda: self.set_checks(True))
        self.select_none.clicked.connect(lambda: self.set_checks(False))
        self.clear_button.clicked.connect(self.clear_rows)
        self.show_button.clicked.connect(self.open_show_filter)
        self.select_shown.clicked.connect(self.select_shown_files)
        pick_row.addWidget(self.select_all)
        pick_row.addWidget(self.select_none)
        pick_row.addWidget(self.clear_button)
        root.addLayout(pick_row)
        show_row = QHBoxLayout()
        show_row.addWidget(self.show_button)
        show_row.addWidget(self.select_shown)
        show_row.addStretch(1)
        root.addLayout(show_row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemChanged.connect(self.on_item_changed)
        header.sectionClicked.connect(self.sort_column)
        header.sectionMoved.connect(self._column_moved)
        self._restore_columns()

        options_page = QWidget()
        options_layout = QVBoxLayout(options_page)
        options_layout.setContentsMargins(0, 0, 0, 0)
        self.auto = QCheckBox('Delete originals automatically')
        self.auto.setChecked(bool(self.settings.get('autoDelete')))
        self.recycle = QCheckBox('Clear the drive recycle copy')
        self.recycle.setChecked(bool(self.settings.get('purgeRecycle')))
        self.reencode = QCheckBox('Re-encode audio that cannot be copied')
        self.reencode.setChecked(bool(self.settings.get('reencodeAudio')))
        self.detailed = QCheckBox('Show detailed activity')
        self.detailed.setChecked(bool(self.settings.get('detailed')))
        self.shutdown = QCheckBox('Shutdown computer upon completion')
        self.shutdown.setChecked(bool(self.settings.get('shutdown')))
        self.auto.toggled.connect(self.on_auto)
        self.recycle.toggled.connect(self.on_recycle)
        for box in (self.reencode, self.detailed, self.shutdown, self.subfolders):
            box.toggled.connect(self.persist)
        for box in (self.auto, self.recycle, self.reencode, self.detailed, self.shutdown):
            options_layout.addWidget(box)
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
        self.clear_skips_button = QPushButton('Clear skips')
        self.clear_skips_button.clicked.connect(self.clear_skips)
        budget_row.addStretch(1)
        budget_row.addWidget(self.clear_skips_button)
        options_layout.addLayout(budget_row)
        self.options_scroll = QScrollArea()
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setFrameShape(QFrame.NoFrame)
        self.options_scroll.setWidget(options_page)
        self.options_scroll.setMinimumHeight(96)
        self.options_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        actions = QHBoxLayout()
        self.scan_button = QPushButton('Scan')
        self.convert_button = QPushButton('Optimize')
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
        for button in (self.scan_button, self.convert_button, self.cancel_button, self.compare_button, self.undo_button, self.delete_button, self.help_button):
            actions.addWidget(button)
        self.savings = QLabel('')
        actions.addStretch(1)
        actions.addWidget(self.savings)
        actions_wrap = QWidget()
        actions_wrap.setLayout(actions)
        batch = QHBoxLayout()
        self.stop_after_button = QPushButton('Stop after current file')
        self.continue_button = QPushButton('Continue')
        self.force_button = QPushButton('Manual')
        self.stop_after_button.clicked.connect(self.request_stop_after)
        self.continue_button.clicked.connect(self.request_continue)
        self.force_button.clicked.connect(self.start_force)
        batch.addWidget(self.stop_after_button)
        batch.addWidget(self.continue_button)
        batch.addWidget(self.force_button)
        batch.addStretch(1)
        batch_wrap = QWidget()
        batch_wrap.setLayout(batch)

        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMinimumHeight(48)
        self.activity.setStyleSheet('background:#1c2430; color:#e5e7eb; font-family:monospace;')
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.options_scroll)
        lower_layout.addWidget(actions_wrap)
        lower_layout.addWidget(batch_wrap)
        lower_layout.addWidget(self.activity, 1)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(lower)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.splitterMoved.connect(self._split_moved)
        self.split_handle = self.splitter.handle(1)
        self.split_handle.installEventFilter(self)
        root.addWidget(self.splitter, 1)

        self.eta = QLabel('')
        self.eta.setVisible(False)
        self.eta.setWordWrap(True)
        root.addWidget(self.eta)
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
        self.browse_button = browse
        self._install_tips()
        self._update_budget_tips()

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
            'purgeRecycle': self.recycle.isChecked(),
            'listExtra': int(self.settings.get('listExtra') or 0),
            'columnOrder': self._column_names(),
        })
        save_settings(self.settings)

    def _install_tips(self):
        tips = {
            self.folder: 'Folder to scan. Click Browse to choose one.',
            self.savings: 'Current size minus New size, for the checked files. A ~ means the picture rate was missing.',
            self.browse_button: 'Choose the folder to scan.',
            self.subfolders: 'Also scan the folders inside the chosen folder.',
            self.auto: 'Delete each original as soon as the new file checks out. The #recycle copy is removed only when Clear the drive recycle copy is on.',
            self.recycle: 'Remove the matching #recycle copy after a delete. Mute hides the failure window after one time.',
            self.reencode: 'Turn audio that cannot be copied, such as Windows Media Audio, into AAC.',
            self.detailed: 'List each encode step. The percent stays on the bar.',
            self.shutdown: 'Turn the computer off after Optimize finishes. You get one minute to cancel.',
            self.clear_skips_button: 'Forget files skipped because the new copy was larger. Scan again to list them.',
            self.scan_button: 'List videos that are too big for their picture size.',
            self.convert_button: 'Shrink the checked bloated, can-shrink, failed, and skipped files.',
            self.force_button: 'Convert a file or folder even when it is under the budget.',
            self.cancel_button: 'Stop the scan or the file that is converting.',
            self.stop_after_button: 'Finish this file, then stop. The rest stay checked.',
            self.continue_button: 'Keep converting the rest of the batch.',
            self.compare_button: 'Play both files side by side. The sound will not stay in sync.',
            self.undo_button: 'Put the last conversion back. The program asks first.',
            self.delete_button: 'Delete the checked originals in .vd-originals. The #recycle copy is removed only when that box is on.',
            self.help_button: 'Open the guide, including how to clear a drive recycle bin by hand.',
            self.check_update: 'Look on GitHub for a newer version.',
            self.donate: 'Free forever. Tips of $5 or more can include a feature suggestion.',
            self.report: 'Open a GitHub bug report with this version filled in.',
            self.update_button: 'Download the newer Linux archive and install it. This window closes while that runs.',
            self.folder_pick: 'Folders with files to shrink. Pick one for a smaller job.',
            self.select_folder: 'Check the files in this folder and clear the other checks.',
            self.over_pick: 'Slight is under 1.5x, Heavy is under 3x, and Extreme is 3x or more.',
            self.select_over: 'Check this ratio group and clear the other checks.',
            self.select_all: 'Check every shown row.',
            self.select_none: 'Clear every check.',
            self.clear_button: 'Remove scanned files from the list. Files in .vd-originals stay.',
            self.show_button: 'Check a group to show only those rows. Hidden rows stay unchecked.',
            self.select_shown: 'Check the visible rows Optimize can take.',
            self.table: 'Checked rows are ready to shrink. Click a heading to sort, or drag it to move that column.',
            self.split_handle: 'Drag to make the video list taller or shorter. Double-click to put it back.',
            self.activity: 'Saved space, skipped videos, and problems are listed here.',
            self.progress: 'How far the current file has been encoded. The two passes share the bar evenly.',
            self.eta: 'This file is the encode in progress. Estimated time remaining is the rest of the batch.',
            self.status: 'What the program is doing right now.',
        }
        for widget, text in tips.items():
            widget.setToolTip(text)

    def _update_budget_tips(self):
        for anchor in BUDGET_ANCHORS:
            box = self.budget_boxes[anchor['id']]
            pick = str(box.currentData() or 'recommended')
            numbers = anchor_budget(anchor, pick)
            hevc = format_budget_gib(numbers['hevc_gibph'])
            box.setToolTip(f"{anchor['name']}: {box.currentText()}. Encode at {numbers['kbps']} kbps. HEVC and AV1 use {hevc}.")

    def _column_names(self) -> list:
        header = self.table.horizontalHeader()
        names = []
        for visual in range(header.count()):
            logical = header.logicalIndex(visual)
            if 0 <= logical < len(COLUMNS):
                names.append(COLUMNS[logical])
        return names

    def _restore_columns(self):
        saved = self.settings.get('columnOrder') or []
        if sorted(saved) != sorted(COLUMNS):
            return
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        for visual, name in enumerate(saved):
            logical = COLUMNS.index(name)
            current = header.visualIndex(logical)
            if current != visual:
                header.moveSection(current, visual)
        header.blockSignals(False)

    def _column_moved(self, _logical, _old, _new):
        self._skip_sort = True
        if self.loading:
            return
        self.settings['columnOrder'] = self._column_names()
        self.persist()

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'split_handle', None) and event.type() == QEvent.MouseButtonDblClick:
            self._reset_split()
            return True
        return super().eventFilter(watched, event)

    def _apply_split(self):
        if not self.splitter.height():
            return
        total = self.splitter.height()
        default_table = max(180, int(total * 0.55))
        self._default_table = default_table
        extra = int(self.settings.get('listExtra') or 0)
        table_h = max(120, min(total - 140, default_table + extra))
        self.splitter.setSizes([table_h, max(140, total - table_h)])
        self._split_ready = True

    def _split_moved(self, _pos, _index):
        if self.loading or not self._split_ready or not self._default_table:
            return
        extra = self.splitter.sizes()[0] - self._default_table
        self.settings['listExtra'] = int(extra)
        self.persist()

    def _reset_split(self):
        self.settings['listExtra'] = 0
        self._split_ready = False
        self._apply_split()
        self.persist()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._split_ready:
            QTimer.singleShot(0, self._apply_split)
        if not self._update_started:
            self._update_started = True
            QTimer.singleShot(600, self.check_for_updates)

    def on_recycle(self, checked):
        if self.loading:
            return
        set_purge_enabled(bool(checked))
        self.persist()

    def _budget_pack(self) -> str:
        packed = []
        for anchor in BUDGET_ANCHORS:
            box = self.budget_boxes[anchor['id']]
            packed.append(anchor['id'] + '=' + str(box.currentData()))
        return ';'.join(packed)

    def _factor(self, pick) -> float:
        for row in BUDGET_FACTORS:
            if row['id'] == pick:
                return float(row['factor'])
        return 1.0

    def on_budget(self):
        if self.loading:
            return
        previous = budget_pick_pack()
        pack = self._budget_pack()
        if pack == previous:
            return
        old = {}
        new = {}
        for part in previous.split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                old[key] = value
        for part in pack.split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                new[key] = value
        tighter = any(self._factor(new.get(key)) < self._factor(old.get(key, 'recommended')) for key in new)
        set_budget_picks(pack)
        self.persist()
        removed = self.reprice_rows()
        self._update_budget_tips()
        notes = []
        for anchor in BUDGET_ANCHORS:
            box = self.budget_boxes[anchor['id']]
            pick = str(box.currentData() or 'recommended')
            was = old.get(anchor['id'], 'recommended')
            if was == pick:
                continue
            numbers = anchor_budget(anchor, pick)
            notes.append(f"{anchor['name']} is {box.currentText()}, encoding at {numbers['kbps']} kbps")
        if removed == 1:
            notes.append('1 file fell under the new budget and left the list')
        elif removed > 1:
            notes.append(f'{removed} files fell under the new budget and left the list')
        if tighter:
            notes.append('Scan again to include files that were under the old budget')
        if notes:
            self.log('. '.join(notes) + '.', 'text')

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

    def row_shown(self, row) -> bool:
        if row.get('status') == 'Forced':
            return True
        if not any(self.show_filters.values()):
            return True
        return any(self.show_filters.get(key) for key in show_criteria(row))

    def shown_rows(self):
        return [row for row in self.rows if self.row_shown(row)]

    def set_checks(self, checked):
        if checked:
            for row in self.shown_rows():
                row['checked'] = True
        else:
            for row in self.rows:
                row['checked'] = False
        self.refill()

    def open_show_filter(self):
        popup = QFrame(self, Qt.Popup)
        layout = QVBoxLayout(popup)
        for key, label in SHOW_FILTERS:
            box = QCheckBox(label)
            box.setChecked(bool(self.show_filters.get(key)))
            box.toggled.connect(lambda checked, key=key: self._toggle_show(key, checked))
            layout.addWidget(box)
        popup.adjustSize()
        popup.move(self.show_button.mapToGlobal(self.show_button.rect().bottomLeft()))
        popup.show()

    def _toggle_show(self, key, checked):
        self.show_filters[key] = bool(checked)
        self.apply_show_filter()

    def apply_show_filter(self):
        for row in self.rows:
            if not self.row_shown(row):
                row['checked'] = False
        names = [label for key, label in SHOW_FILTERS if self.show_filters.get(key)]
        self.show_button.setText('Showing all' if not names else 'Showing ' + ', '.join(names))
        self.refill()

    def select_shown_files(self):
        if self.work != 'idle':
            return
        checked = 0
        for row in self.rows:
            take = self.row_shown(row) and row.get('status') in ('Bloated', 'Can shrink', 'Failed', 'Skipped')
            row['checked'] = bool(take)
            if take:
                checked += 1
        word = 'file' if checked == 1 else 'files'
        self.log(f'Checked {checked} shown {word}. Hidden files stay unchecked.', 'text')
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
        self.status.setText(f'Listing videos... {seen} seen, {hits} listed. {name}')

    def scan_finished(self, stats):
        self.work = 'idle'
        self.awake.stop()
        if stats.get('cancelled'):
            self.status.setText('Scan stopped')
            self.log('Scan stopped.', 'text')
        else:
            shrink = int(stats.get('shrink') or 0)
            self.status.setText(f"Scan finished. {stats['hits']} files can be optimized.")
            self.log(f"Scan finished. {stats['seen']} videos seen, {stats['hits']} listed.", 'ok')
            if shrink:
                self.log(f'{shrink} of those are under the line, and the picture can still shrink.', 'text')
            if stats.get('ignored'):
                self.log(f"{stats['ignored']} files stayed off the list because a smaller copy was not smaller last time.", 'text')
        self._refresh()

    def auto_ask(self) -> bool:
        if not self.auto.isChecked() or self.settings.get('autoAsk') is False:
            return True
        dialog = QDialog(self)
        dialog.setWindowTitle('Video Dehydrator')
        layout = QVBoxLayout(dialog)
        label = QLabel(
            "Auto is on.\n\nAfter each file checks out, its original is deleted. The new file stays in the original's place. "
            "There will be nothing left to compare. The #recycle copy is removed only when Clear the drive recycle copy is on. "
            "The .plexignore from that convert is removed when it only hides the holding folder."
        )
        label.setWordWrap(True)
        hide = QCheckBox('Stop asking')
        hide.setToolTip('Stop opening this warning. Cancel leaves it on.')
        buttons = QHBoxLayout()
        convert = QPushButton('Convert')
        cancel = QPushButton('Cancel')
        convert.setToolTip('Convert, and delete each original after it checks out.')
        cancel.setToolTip('Leave the files alone.')
        buttons.addStretch(1)
        buttons.addWidget(convert)
        buttons.addWidget(cancel)
        layout.addWidget(label)
        layout.addWidget(hide)
        layout.addLayout(buttons)
        chosen = {'ok': False}
        def accept():
            chosen['ok'] = True
            dialog.accept()
        convert.clicked.connect(accept)
        cancel.clicked.connect(dialog.reject)
        dialog.exec()
        if chosen['ok'] and hide.isChecked():
            self.settings['autoAsk'] = False
            self.persist()
        return chosen['ok']

    def convert(self):
        if self.work != 'idle':
            return
        chosen = [row for row in self.rows if row['checked'] and row['status'] in CONVERTIBLE]
        if not chosen:
            return
        if missing_tools():
            QMessageBox.warning(self, 'Video Dehydrator', 'FFmpeg and HandBrake need to be in the tools folder.')
            return
        if not self.auto_ask():
            return
        self._start_convert(chosen, force=False)

    def _start_convert(self, chosen, force):
        self.persist()
        self.cancel.clear()
        self.work = 'encode'
        self.awake.start()
        self.saved_bytes = 0
        self.set_eta('')
        if self.shutdown.isChecked():
            self.log('The computer will shut down when this convert finishes.', 'text')
        self.worker = ConvertWorker(
            chosen, self.tools, self.reencode.isChecked(), self.auto.isChecked(),
            self.detailed.isChecked(), self.skips, self.cancel, force=force,
        )
        self.worker.activity.connect(self.log)
        self.worker.progress.connect(lambda value: self.progress.setValue(int(value)))
        self.worker.file_update.connect(self.on_file_update)
        self.worker.eta.connect(self.set_eta)
        self.worker.finished_job.connect(self.convert_finished)
        self.worker.start()
        self.status.setText('Converting...')
        self._refresh()

    def set_eta(self, text):
        text = str(text or '')
        show = bool(text.strip())
        self.eta.setText(text)
        self.eta.setVisible(show)
        if show:
            self.eta.setToolTip(text)

    def convert_finished(self, payload):
        self.work = 'idle'
        self.awake.stop()
        self.set_eta('')
        self.skips = payload['skips']
        write_skips(self.skips)
        self.saved_bytes = payload['saved']
        self._sync_pending()
        self.refill()
        opened_steps = self.show_recycle_problems(payload['book'])
        if payload.get('stopped_after') and not payload.get('cancelled'):
            self.status.setText('Stopped after the current file')
            self.log('Stopped after the current file. The computer stays on.', 'text')
        elif payload['cancelled']:
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

    def request_stop_after(self):
        worker = self.worker
        if self.work != 'encode' or worker is None:
            return
        worker.stop_after.set()
        self.status.setText('Stopping after the current file')
        self.log('Stopping after the current file.', 'text')

    def request_continue(self):
        worker = self.worker
        if self.work != 'encode' or worker is None:
            return
        worker.stop_after.clear()
        self.status.setText('Converting...')
        self.log('Continuing the rest of this batch.', 'text')

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.request_cancel()
            return
        super().keyPressEvent(event)

    def selected_row(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        shown = self.shown_rows()
        index = indexes[0].row()
        if 0 <= index < len(shown):
            return shown[index]
        return None

    def start_force(self):
        if self.work != 'idle':
            return
        if missing_tools():
            QMessageBox.warning(self, 'Video Dehydrator', 'FFmpeg and HandBrake need to be in the tools folder.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Manual')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Convert a file or folder even when it is under the budget.'))
        path = QLineEdit()
        selected = self.selected_row()
        if selected:
            path.setText(selected.get('source') or selected.get('path') or '')
        else:
            path.setText(self.folder.text().strip())
        file_button = QPushButton('Browse file')
        folder_button = QPushButton('Browse folder')
        subs = QCheckBox('Include subfolders')
        subs.setChecked(self.subfolders.isChecked())
        subs.setToolTip('Also convert videos in folders inside this folder.')
        test = QPushButton('Test savings')
        now = QPushButton('Convert now')
        cancel = QPushButton('Cancel')
        test.setToolTip('Read the files and show the estimated savings. Nothing is encoded yet.')
        now.setToolTip('Convert without the savings estimate.')
        cancel.setToolTip('Leave the files alone.')
        choice = {'mode': ''}
        def pick_file():
            name, _filter = QFileDialog.getOpenFileName(dialog, 'Select a video', path.text())
            if name:
                path.setText(name)
        def pick_folder():
            name = QFileDialog.getExistingDirectory(dialog, 'Select folder', path.text())
            if name:
                path.setText(name)
        def go(mode):
            if not path.text().strip() or not os.path.exists(path.text().strip()):
                return
            choice['mode'] = mode
            choice['path'] = path.text().strip()
            choice['recurse'] = subs.isChecked()
            dialog.accept()
        file_button.clicked.connect(pick_file)
        folder_button.clicked.connect(pick_folder)
        test.clicked.connect(lambda: go('test'))
        now.clicked.connect(lambda: go('now'))
        cancel.clicked.connect(dialog.reject)
        row = QHBoxLayout()
        row.addWidget(path, 1)
        row.addWidget(file_button)
        layout.addLayout(row)
        layout.addWidget(folder_button)
        layout.addWidget(subs)
        buttons = QHBoxLayout()
        buttons.addWidget(test)
        buttons.addWidget(now)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        dialog.exec()
        if not choice.get('mode'):
            return
        self._force_choice = choice
        self.cancel.clear()
        self.work = 'scan'
        self.awake.start()
        self.status.setText('Reading files...')
        self.worker = ForceWorker(
            choice['path'], choice['recurse'] and os.path.isdir(choice['path']),
            choice['mode'] == 'test', str(self.tools['ffprobe']), self.cancel,
        )
        self.worker.progress.connect(lambda seen, name: self.status.setText(f'Reading files... {seen}. {name}'))
        self.worker.finished_walk.connect(self.force_finished)
        self.worker.start()
        self._refresh()

    def force_finished(self, payload):
        self.work = 'idle'
        self.awake.stop()
        self._refresh()
        if payload.get('cancelled'):
            self.status.setText('Scan stopped')
            return
        files = payload.get('files') or []
        if self._force_choice.get('mode') == 'test':
            report = format_force_report(files)
            ready = [item for item in files if item.get('can_convert')]
            dialog = QDialog(self)
            dialog.setWindowTitle('Test savings')
            layout = QVBoxLayout(dialog)
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(report)
            proceed = QPushButton('Proceed')
            cancel = QPushButton('Cancel')
            proceed.setToolTip('Convert these files.')
            cancel.setToolTip('Do not convert.')
            proceed.setEnabled(bool(ready))
            go = {'ok': False}
            def accept():
                go['ok'] = True
                dialog.accept()
            proceed.clicked.connect(accept)
            cancel.clicked.connect(dialog.reject)
            layout.addWidget(text)
            buttons = QHBoxLayout()
            buttons.addStretch(1)
            buttons.addWidget(proceed)
            buttons.addWidget(cancel)
            layout.addLayout(buttons)
            dialog.resize(640, 420)
            dialog.exec()
            if not go['ok']:
                return
            files = ready
        self._begin_force(files)

    def _begin_force(self, files):
        rows = []
        for item in files:
            if not item.get('can_convert'):
                continue
            rows.append({
                'path': item['path'],
                'name': item.get('name') or os.path.basename(item['path']),
                'checked': True,
                'status': 'Forced',
                'width': item.get('width') or 0,
                'height': item.get('height') or 0,
                'duration': item.get('duration') or 0,
                'size': item.get('size') or 0,
                'old_bytes': None,
                'gibph': item.get('gibph'),
                'budget': item.get('budget'),
                'over': item.get('over'),
                'target': item.get('target') or 0,
                'estimate': item.get('estimate'),
                'estimate_rough': bool(item.get('estimate_rough')),
                'video_bitrate': item.get('video_bitrate') or 0,
                'codec': item.get('codec') or '',
                'audio': [],
                'subs': 0,
                'reason': item.get('reason') or '',
                'anchor': item.get('anchor') or '',
                'ten_bit': bool(item.get('ten_bit')),
                'kept': '',
                'new': '',
                'source': item['path'],
                'pending_id': '',
                'batch': '',
                'probe': item.get('probe'),
            })
        if not rows:
            QMessageBox.information(self, 'Video Dehydrator', 'No video can be converted.')
            return
        if not self.auto_ask():
            return
        keep = [row for row in self.rows if row.get('pending_id')]
        self.rows = keep + rows
        self.log('The scanned list was replaced. Files still waiting in .vd-originals stay.', 'text')
        self.refill()
        self._start_convert(rows, force=True)

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
                "The #recycle copy is removed only when Clear the drive recycle copy is on. "
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
        if self.settings.get('recycleAlertMuted'):
            return False
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
        mute = QPushButton('Mute')
        close = QPushButton('Close')
        mute.setToolTip('Hide this window after this one time.')
        close.setToolTip('Close this list. It opens again next time.')
        opened = {'steps': False}
        def open_steps():
            opened['steps'] = True
            dialog.accept()
            self.show_help('Drive recycle bin')
        def do_mute():
            self.settings['recycleAlertMuted'] = True
            self.persist()
            dialog.reject()
        steps.clicked.connect(open_steps)
        mute.clicked.connect(do_mute)
        close.clicked.connect(dialog.reject)
        layout.addWidget(intro)
        layout.addWidget(detail, 1)
        layout.addWidget(steps)
        layout.addWidget(mute)
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
        asset = payload.get('asset')
        if not linux_release_is_update(tag, app_version(), asset):
            self.update_offer = None
            self.status.setText('This is the newest published Linux version.')
            self.update_button.setEnabled(False)
            return
        remote = tag[1:] if tag[:1].lower() == 'v' else tag
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
        prefix = 'https://github.com/philipvern-dot/video_dehydrator_linux/releases/download/'
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

    def apply_folder(self):
        name = self.folder_pick.currentText()
        if name == 'All folders':
            return
        for row in self.rows:
            parent = os.path.dirname(row.get('source') or row['path'])
            row['checked'] = parent == name and row['status'] in FOLDER_STATUSES
        self.refill()

    def apply_over(self):
        label = self.over_pick.currentText().lower()
        if label == 'all overages':
            return
        for row in self.rows:
            band = over_band_id(row.get('over')) if row.get('status') in ('Bloated', 'Failed') else ''
            row['checked'] = band == label
        self.refill()

    def reprice_rows(self):
        from .engine import Probe
        kept = []
        removed = 0
        for row in self.rows:
            if row['status'] not in ('Bloated', 'Can shrink', 'Failed', 'Skipped'):
                kept.append(row)
                continue
            probe = row.get('probe')
            if probe is None and row.get('width') and row.get('height'):
                codec = str(row.get('codec') or '')
                probe = Probe(
                    width=int(row['width']),
                    height=int(row['height']),
                    size_bytes=int(row.get('size') or 0),
                    duration_sec=float(row.get('duration') or 0),
                    video_bitrate=int(row.get('video_bitrate') or 0),
                    video_codec=codec,
                    already_efficient=codec.lower() in ('hevc', 'h265', 'av1'),
                )
            if probe is None:
                kept.append(row)
                continue
            decision = bloat_decision(probe)
            if decision['decision'] in ('lean', 'within', 'dv'):
                removed += 1
                continue
            if decision['decision'] not in ('bloated', 'shrink'):
                kept.append(row)
                continue
            row['budget'] = decision['budget_gibph']
            row['target'] = decision['target_kbps']
            row['gibph'] = decision['gibph']
            row['over'] = over_ratio(decision['gibph'], decision['budget_gibph'])
            row['estimate'] = decision['estimate_bytes']
            row['estimate_rough'] = decision['estimate_rough']
            if row['status'] in ('Bloated', 'Can shrink'):
                row['status'] = 'Can shrink' if decision['decision'] == 'shrink' else 'Bloated'
                row['reason'] = decision['reason']
                row['checked'] = True
            kept.append(row)
        self.rows = kept
        self.apply_show_filter()
        return removed

    def sort_column(self, index):
        if getattr(self, '_skip_sort', False):
            self._skip_sort = False
            return
        def key(row):
            if index == 0:
                return (row.get('name') or '').casefold()
            if index == 6:
                return row.get('over') if row.get('over') is not None else -1
            if index == 10:
                saved = space_saved_bytes(row.get('size'), row.get('old_bytes'))
                return saved if saved is not None else -1
            if index == 11:
                guess = estimated_savings_bytes(row.get('size'), row.get('estimate'), row.get('old_bytes'))
                return guess if guess is not None else -1
            if index == 4:
                return row.get('gibph') if row.get('gibph') is not None else -1
            if index == 3:
                return row.get('size') or 0
            if index == 8:
                return row.get('estimate') or -1
            if index == 9:
                return row.get('status') or ''
            return (row.get('name') or '').casefold()
        descending = False
        if index in self._sort_desc:
            descending = self._sort_desc[index]
            self._sort_desc[index] = not descending
        self.rows.sort(key=key, reverse=descending)
        self.refill()

    def on_item_changed(self, item):
        if self.loading or item.column() != 0:
            return
        shown = self.shown_rows()
        index = item.row()
        if 0 <= index < len(shown):
            shown[index]['checked'] = item.checkState() == Qt.Checked
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
            QTableWidgetItem('' if not row.get('target') else f"{row['target']} kbps"),
            QTableWidgetItem(self.new_size_text(row)),
            QTableWidgetItem(row['status']),
            QTableWidgetItem(self.saved_text(row)),
            QTableWidgetItem(self.guess_text(row)),
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

    def saved_text(self, row) -> str:
        saved = space_saved_bytes(row.get('size'), row.get('old_bytes'))
        if saved is None:
            return ''
        return format_byte_size(saved)

    def guess_text(self, row) -> str:
        guess = estimated_savings_bytes(row.get('size'), row.get('estimate'), row.get('old_bytes'))
        if guess is None:
            return ''
        text = format_byte_size(guess)
        if row.get('estimate_rough'):
            return '~' + text
        return text

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
        for row in self.shown_rows():
            self.add_table_row(row)
            parent = os.path.dirname(row.get('source') or row['path'])
            if row['status'] in FOLDER_STATUSES and parent not in folders:
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
            if row['checked'] and row['status'] in CONVERTIBLE and row.get('estimate') and row.get('size'):
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
                'ten_bit': False,
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
        encoding = self.work == 'encode'
        self.convert_button.setEnabled((not busy) and any(row['checked'] and row['status'] in CONVERTIBLE for row in self.rows))
        self.cancel_button.setEnabled(busy)
        self.stop_after_button.setEnabled(encoding)
        self.continue_button.setEnabled(encoding)
        self.force_button.setEnabled((not busy) and not missing_tools())
        self.delete_button.setEnabled((not busy) and any(row['checked'] and row['status'] == 'Ready to delete' for row in self.rows))
        self.compare_button.setEnabled(not busy)
        self.undo_button.setEnabled((not busy) and any(os.path.isfile(str(item.get('kept') or '')) for item in self.pending))
        self.shutdown.setEnabled(not busy)
        self.auto.setEnabled(not busy)
        self.recycle.setEnabled(not busy)

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
