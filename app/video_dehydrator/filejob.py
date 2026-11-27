"""Encode one file outside the window process.

The window process loads the desktop toolkit. A native crash there used to
kill the batch. This module does not import the window. The lane thread starts
one process per file and reads a JSON object from each stdout line.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from types import SimpleNamespace

from .engine import (
    add_hold_busy_marker, bloat_decision, encode_command, encode_row_snapshot,
    encoder_name, encoder_threads, force_convertible, handbrake_arguments,
    handbrake_progress, is_encode_progress_line, larger_file_note, make_fault_record,
    numa_node_count, output_path_missing, pass_two_did_not_start, pixel_seconds,
    probe_file, remove_empty_directory, remove_hold_busy_marker, replace_plan,
    replace_with_encode, run_lines, size_skip_reason, target_kbps, test_encoded_file,
    budget_pick_pack, set_budget_picks,
)
from .recycle import RecycleBook, purge_enabled, set_purge_enabled
from .store import append_convert_log

# After a stop signal, wait long enough for HandBrake to exit and the file
# cleanup to finish, then kill the process group.
STOP_WAIT_SEC = 20
_STDOUT_DONE = object()


def reset_clock(slot) -> None:
    slot.pass_started = time.monotonic()
    slot.pass1_sec = 0.0
    slot.task = 0
    slot.progress_info = None
    slot.checking = False
    slot.remembered = False
    slot.ten_bit = False
    slot.pixels = 0.0
    slot.live8 = []
    slot.live10 = []
    slot.file_samples = []
    slot.batch_samples = []
    slot.shown_at = None
    slot.shown_key = ''
    slot.percent = 0.0
    slot.file_seconds = 0.0


def apply_pass(slot, info, now) -> None:
    task = int((info or {}).get('task') or 0)
    if slot.task >= 1 and task > slot.task and slot.task == 1 and task >= 2:
        slot.pass1_sec = max(0.0, now - slot.pass_started)
        slot.pass_started = now
    elif task > slot.task:
        slot.pass_started = now
    if task > slot.task:
        slot.live8 = []
        slot.live10 = []
        slot.task = task
    slot.progress_info = info


def wire_row(row) -> dict:
    """Plain row for the pipe. The probe object stays in the window process."""
    copy = encode_row_snapshot(row)
    copy.pop('probe', None)
    return copy


def merge_worker_row(row, snapshot) -> None:
    """Copy a child row onto the lane's row and keep the lane's probe object."""
    if not isinstance(row, dict) or not isinstance(snapshot, dict):
        return
    probe = row.get('probe')
    clean = dict(snapshot)
    clean.pop('probe', None)
    row.update(clean)
    if probe is not None:
        row['probe'] = probe


def _write_stdout(event) -> None:
    sys.stdout.write(json.dumps(event, default=str) + '\n')
    sys.stdout.flush()


class ReportingSlot:
    """Slot whose picture fields are reported to the parent as they change."""

    def __init__(self, emit):
        object.__setattr__(self, '_emit', emit)
        object.__setattr__(self, 'name', '')
        object.__setattr__(self, 'paint_task', 0)
        object.__setattr__(self, 'percent', 0.0)
        object.__setattr__(self, 'checking', False)
        object.__setattr__(self, 'ten_bit', False)
        object.__setattr__(self, 'pixels', 0.0)
        object.__setattr__(self, 'paint_at', None)

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)
        if key in ('ten_bit', 'pixels', 'checking'):
            self._emit({'op': 'slot', 'key': key, 'value': value})


class PipeHost:
    """Host for encode_file. Progress goes out as JSON. Lane bars stay in the parent."""

    def __init__(self, job, cancel, emit=None):
        self._emit = emit or _write_stdout
        self.cancel = cancel
        self.tools = dict(job.get('tools') or {})
        self.reencode = bool(job.get('reencode'))
        self.delete_original = bool(job.get('delete_original'))
        self.detailed = bool(job.get('detailed'))
        self.force = bool(job.get('force'))
        self.jobs = int(job.get('jobs') or 1)
        self.dynamic = bool(job.get('dynamic'))
        self.cpu_target = int(job.get('cpu_target') or 100)
        self.batch = job.get('batch') or ''
        self.assigned_pool = int(job.get('pool') or 0)
        self.node_cap = int(job.get('node_cap') or 0)
        self.locked_pool = bool(job.get('locked_pool'))
        self.node = int(job.get('node') or 0)
        self._tls = SimpleNamespace(node=self.node, pool=self.assigned_pool)
        self._slot_obj = ReportingSlot(self._emit)
        self.book = RecycleBook()
        self.saved = 0

    def _slot(self):
        return self._slot_obj

    def _reset_clock(self):
        reset_clock(self._slot_obj)

    def _pool_size(self) -> int:
        return int(self.assigned_pool)

    def _start_line(self, verb, name) -> str:
        if not self.dynamic:
            return f'{verb} {name}.'
        return f'{verb} {name}. {self._pool_size()} threads.'

    def _restore_status(self, row, previous):
        if previous in ('Failed', 'Skipped', 'Can shrink', 'Forced'):
            row['status'] = previous
        else:
            row['status'] = 'Bloated'
        row['checked'] = True

    def _settle_pool(self, row, probe):
        if not self.dynamic or self.locked_pool:
            return None
        need = encoder_threads(getattr(probe, 'width', 0), getattr(probe, 'height', 0), self.node_cap)
        if need == int(self.assigned_pool):
            return None
        self._emit({
            'op': 'requeue',
            'width': getattr(probe, 'width', 0),
            'height': getattr(probe, 'height', 0),
            'need': need,
        })
        return 'requeue'

    def _set_progress(self, value, force=False):
        slot = self._slot_obj
        slot.percent = float(value)
        now = time.monotonic()
        last = getattr(slot, 'paint_at', None)
        if not force and last is not None and (now - last) < 0.5:
            return False
        slot.paint_at = now
        self._emit({'op': 'progress', 'value': value, 'force': bool(force)})
        return True

    def _note_pass(self, info):
        apply_pass(self._slot_obj, info, time.monotonic())
        self._emit({'op': 'pass', 'info': info})

    def _emit_eta(self):
        self._emit({'op': 'eta'})

    def _remember_finished(self):
        slot = self._slot_obj
        if slot.remembered or slot.task < 2 or slot.pass1_sec < 1 or slot.pixels <= 0:
            return
        pass2 = time.monotonic() - slot.pass_started
        if pass2 < 1:
            return
        self._emit({
            'op': 'remember',
            'ten_bit': bool(slot.ten_bit),
            'pixels': slot.pixels,
        })
        slot.remembered = True

    def _post(self, kind, *payload):
        self._emit({'op': 'post', 'kind': kind, 'payload': list(payload)})

    def _post_row(self, row):
        self._post('file', wire_row(row))

    def _note_fault(self, kind, source, reason, probe=None, out_probe=None, old_bytes=0, new_bytes=0, old_gibph=None, new_gibph=None, kbps=0, command=None, handbrake_log='', handbrake_code=None):
        record = make_fault_record(
            kind, source, reason, probe=probe, out_probe=out_probe,
            old_bytes=old_bytes, new_bytes=new_bytes, old_gibph=old_gibph, new_gibph=new_gibph,
            kbps=kbps, command=command, handbrake_log=handbrake_log, handbrake_code=handbrake_code,
            jobs=self.jobs, reencode_audio=self.reencode, auto_delete=self.delete_original,
            ffprobe=str(self.tools.get('ffprobe') or ''),
        )
        self._emit({'op': 'fault', 'record': record})

    def note_skip(self, source, size_bytes, kbps):
        self._emit({
            'op': 'skip-add',
            'source': source,
            'size': size_bytes,
            'kbps': kbps or 0,
        })

    def note_unskip(self, source):
        self._emit({'op': 'skip-remove', 'source': source})

    def note_saved(self, amount):
        try:
            number = int(amount or 0)
        except (TypeError, ValueError):
            return
        if number > 0:
            self.saved += number


def encode_file(host, row):
    """Encode one file. Returns 'requeue' when the thread pool has to change."""
    source = row['source'] or row['path']
    previous = row['status']
    verb = 'Forcing' if host.force else 'Converting'
    slot = host._slot()
    slot.name = os.path.basename(source)
    host._reset_clock()
    slot.name = os.path.basename(source)
    host._set_progress(0)
    if host.cancel.is_set():
        host._restore_status(row, previous)
        host._post_row(row)
        return
    try:
        probe = probe_file(str(host.tools['ffprobe']), source)
    except Exception as exc:
        # A stop signal kills ffprobe with the process group. That is a stop,
        # not a file that could not be read.
        if host.cancel.is_set():
            host._restore_status(row, previous)
            host._post('activity', f"Stopped {os.path.basename(source)}. The original stayed.", 'text')
            host._post_row(row)
            return
        row['status'] = 'Failed'
        row['reason'] = str(exc)
        row['checked'] = True
        host._post('activity', f'Could not read {os.path.basename(source)}. {exc}', 'err')
        host._note_fault('unreadable', source, str(exc))
        host._post_row(row)
        return
    if host._settle_pool(row, probe) == 'requeue':
        return 'requeue'
    host._post('activity', host._start_line(verb, slot.name), 'text')
    row['status'] = 'Converting'
    host._post_row(row)
    host._emit_eta()
    decision = bloat_decision(probe)
    if host.force and not force_convertible(decision):
        row['status'] = 'Left alone'
        row['checked'] = False
        row['reason'] = decision.get('reason') or 'Left alone.'
        host._post('activity', f"Left {os.path.basename(source)} alone. {row['reason']}", 'text')
        host._post_row(row)
        return
    slot = host._slot()
    row['ten_bit'] = encoder_name(probe) == 'x265_10bit'
    row['width'] = probe.width
    row['height'] = probe.height
    row['duration'] = probe.duration_sec
    slot.ten_bit = bool(row['ten_bit'])
    slot.pixels = pixel_seconds(probe.width, probe.height, probe.duration_sec)
    plan = replace_plan(source)
    if plan['blocked']:
        row['status'] = 'Failed'
        row['reason'] = 'A file already has the new name.'
        row['checked'] = True
        host._post('activity', row['reason'] + ' ' + plan['final'], 'err')
        host._post_row(row)
        return
    os.makedirs(plan['hold_dir'], exist_ok=True)
    encoder = encoder_name(probe)
    kbps = target_kbps(probe.width, probe.height)
    pool = host._pool_size()
    output_retries = 0
    single_pass = False
    handbrake_log = ''
    while True:
        add_hold_busy_marker(plan)
        args = handbrake_arguments(source, plan['temp'], probe, kbps, encoder, host.reencode, pool, single_pass)
        command = encode_command(
            str(host.tools['handbrake']), args, host.jobs, int(getattr(host._tls, 'node', 0) or 0),
            numa_node_count(), shutil.which('numactl') is not None,
        )
        log_lines = []

        def on_line(line, _lines=log_lines):
            _lines.append(line)
            if len(_lines) > 500:
                del _lines[:-500]
            info = handbrake_progress(line)
            if info is not None:
                task = int(info.get('task') or 0)
                changed = task != int(getattr(host._slot(), 'paint_task', 0) or 0)
                host._slot().paint_task = task
                host._note_pass(info)
                if host._set_progress(info['overall'], force=changed):
                    host._emit_eta()
            elif host.detailed and line and not is_encode_progress_line(line):
                prefix = f'{os.path.basename(source)}  ' if host.jobs > 1 else ''
                host._post('activity', prefix + line, 'dim')

        code, _joined = run_lines(command, on_line, host.cancel)
        handbrake_log = '\n'.join(log_lines)
        if host.cancel.is_set():
            break
        missing_file = code != 0 or not os.path.isfile(plan['temp'])
        if missing_file and output_path_missing(handbrake_log) and output_retries < 2:
            output_retries += 1
            if os.path.isfile(plan['temp']):
                os.remove(plan['temp'])
            host._post('activity', f'The output folder was missing for {os.path.basename(source)}. Trying the two-pass encode again.', 'text')
            continue
        if missing_file and not single_pass and not output_path_missing(handbrake_log) and pass_two_did_not_start(handbrake_log):
            single_pass = True
            if os.path.isfile(plan['temp']):
                os.remove(plan['temp'])
            host._post('activity', f'The two-pass encode did not start for {os.path.basename(source)}. Trying one pass.', 'text')
            continue
        break
    if host.cancel.is_set():
        remove_hold_busy_marker(plan)
        if os.path.isfile(plan['temp']):
            os.remove(plan['temp'])
        remove_empty_directory(plan['hold_dir'], host.book)
        host._restore_status(row, previous)
        host._post('activity', f"Stopped {os.path.basename(source)}. The original stayed.", 'text')
        host._post_row(row)
        return
    if code != 0 or not os.path.isfile(plan['temp']):
        remove_hold_busy_marker(plan)
        if os.path.isfile(plan['temp']):
            os.remove(plan['temp'])
        remove_empty_directory(plan['hold_dir'], host.book)
        row['status'] = 'Failed'
        row['checked'] = True
        row['reason'] = f'HandBrake stopped ({code}).'
        append_convert_log('FAIL', row['reason'], source, '', '', probe.size_bytes, 0, 0, 0, probe.width, probe.height, kbps)
        host._post('activity', f"Failed {os.path.basename(source)}. {row['reason']}", 'err')
        host._note_fault(
            'encode', source, row['reason'], probe=probe, old_bytes=probe.size_bytes, kbps=kbps,
            command=command, handbrake_log=handbrake_log, handbrake_code=code,
        )
        host._post_row(row)
        return
    host._remember_finished()
    host._slot().checking = True
    host._set_progress(host._slot().percent, force=True)
    host._emit_eta()
    remove_hold_busy_marker(plan)
    checked = test_encoded_file(
        str(host.tools['ffmpeg']), str(host.tools['ffprobe']), plan['temp'], probe, handbrake_log, host.reencode,
    )
    if not checked['ok']:
        if os.path.isfile(plan['temp']):
            os.remove(plan['temp'])
        remove_empty_directory(plan['hold_dir'], host.book)
        if size_skip_reason(checked['reason']):
            note = larger_file_note(
                checked['reason'], probe.size_bytes, checked['new_bytes'], kbps,
                probe.audio_codecs, (checked['out_probe'].audio_codecs if checked['out_probe'] else []),
            )
            row['status'] = 'Skipped'
            row['checked'] = False
            row['reason'] = note or checked['reason']
            host.note_skip(source, probe.size_bytes, kbps or 0)
            append_convert_log('SKIP', row['reason'], source, '', '', probe.size_bytes, checked['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
            host._post('activity', 'Skipped. ' + row['reason'], 'text')
        else:
            row['status'] = 'Failed'
            row['checked'] = True
            row['reason'] = checked['reason']
            append_convert_log('FAIL', checked['reason'], source, '', '', probe.size_bytes, checked['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
            host._post('activity', f"Failed {os.path.basename(source)}. {checked['reason']}", 'err')
            host._note_fault(
                'check', source, checked['reason'], probe=probe, out_probe=checked.get('out_probe'),
                old_bytes=probe.size_bytes, new_bytes=checked.get('new_bytes') or 0,
                old_gibph=checked.get('old_gibph'), new_gibph=checked.get('new_gibph'), kbps=kbps,
                command=command, handbrake_log=handbrake_log, handbrake_code=code,
            )
        host._post_row(row)
        return
    try:
        swap = replace_with_encode(source, plan['temp'], host.delete_original, host.book)
    except Exception as exc:
        row['status'] = 'Failed'
        row['reason'] = str(exc)
        row['checked'] = True
        host._post('activity', f'Could not replace {os.path.basename(source)}. {exc}', 'err')
        host._post_row(row)
        return
    row['new'] = swap['new_path']
    row['path'] = swap['new_path']
    row['name'] = os.path.basename(swap['new_path'])
    row['kept'] = swap['kept_path'] or ''
    row['source'] = source
    row['old_bytes'] = swap['old_bytes']
    row['size'] = swap['new_bytes']
    row['estimate'] = None
    host.note_saved(max(0, swap['old_bytes'] - swap['new_bytes']))
    if swap['deleted']:
        row['status'] = 'Original deleted'
        row['pending_id'] = ''
        row['checked'] = False
        append_convert_log('DELETED', 'ok', source, swap['new_path'], '', swap['old_bytes'], swap['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
    else:
        row['status'] = 'Ready to delete'
        row['pending_id'] = uuid.uuid4().hex
        row['batch'] = host.batch
        row['checked'] = False
        append_convert_log('KEPT', 'ok', source, swap['new_path'], swap['kept_path'], swap['old_bytes'], swap['new_bytes'], checked['old_gibph'], checked['new_gibph'], probe.width, probe.height, kbps)
    host.note_unskip(source)
    host._post('activity', f"Finished {row['name']}.", 'ok')
    host._set_progress(100)
    host._post_row(row)


def _install_cancel(cancel) -> None:
    def handle(signum, frame):
        cancel.set()

    for name in ('SIGTERM', 'SIGINT'):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handle)
        except (OSError, ValueError):
            pass


def _child_main() -> int:
    line = sys.stdin.readline()
    if not line.strip():
        return 1
    job = json.loads(line)
    set_budget_picks(job.get('budgets') or '')
    set_purge_enabled(bool(job.get('purge')))
    cancel = threading.Event()
    _install_cancel(cancel)
    host = PipeHost(job, cancel)
    row = job.get('row') or {}
    outcome = encode_file(host, row)
    host._emit({
        'op': 'result',
        'outcome': outcome or '',
        'saved': host.saved,
        'book': list(host.book.rows),
        'row': wire_row(row),
    })
    return 0


def main(argv=None) -> int:
    try:
        return _child_main()
    except Exception:
        traceback.print_exc()
        return 1


def _job_for(worker, row, locked_pool: bool) -> dict:
    pool = int(worker._pool_size())
    node = int(getattr(worker._tls, 'node', 0) or 0)
    node_cap = 0
    admission = getattr(worker, '_admission', None)
    if getattr(worker, 'dynamic', False) and admission is not None:
        node_cap = max(admission.capacity)
    tools = {}
    for key, value in dict(getattr(worker, 'tools', {}) or {}).items():
        tools[str(key)] = str(value)
    return {
        'row': wire_row(row),
        'tools': tools,
        'reencode': bool(worker.reencode),
        'delete_original': bool(worker.delete_original),
        'detailed': bool(worker.detailed),
        'force': bool(worker.force),
        'jobs': int(worker.jobs),
        'dynamic': bool(worker.dynamic),
        'cpu_target': int(getattr(worker, 'cpu_target', 100) or 100),
        'pool': pool,
        'node': node,
        'node_cap': int(node_cap),
        'batch': worker.batch,
        'budgets': budget_pick_pack(),
        'purge': bool(purge_enabled()),
        'locked_pool': bool(locked_pool),
    }


def _show_file(worker, row) -> None:
    source = row.get('source') or row.get('path') or ''
    name = os.path.basename(source)
    slot = worker._slot()
    slot.name = name
    worker._reset_clock()
    slot.name = name
    worker._set_progress(0)


def _apply_child_event(worker, row, event, state) -> None:
    if not isinstance(event, dict):
        return
    op = event.get('op')
    if op == 'pass':
        worker._note_pass(event.get('info') or {})
    elif op == 'slot':
        key = event.get('key')
        if key in ('ten_bit', 'pixels', 'checking'):
            setattr(worker._slot(), key, event.get('value'))
    elif op == 'progress':
        worker._set_progress(event.get('value') or 0, force=bool(event.get('force')))
    elif op == 'eta':
        worker._emit_eta()
    elif op == 'remember':
        slot = worker._slot()
        slot.ten_bit = bool(event.get('ten_bit'))
        try:
            slot.pixels = float(event.get('pixels') or 0)
        except (TypeError, ValueError):
            slot.pixels = 0.0
        worker._remember_finished()
    elif op == 'post':
        payload = list(event.get('payload') or [])
        kind = event.get('kind')
        if kind == 'file' and payload and isinstance(payload[0], dict):
            merge_worker_row(row, payload[0])
            payload[0] = wire_row(payload[0])
        worker._post(kind, *payload)
    elif op == 'fault':
        worker._post('fault', event.get('record') or {})
    elif op == 'skip-add':
        worker.note_skip(event.get('source') or '', event.get('size') or 0, event.get('kbps') or 0)
    elif op == 'skip-remove':
        worker.note_unskip(event.get('source') or '')
    elif op == 'requeue':
        state['requeue'] = event
    elif op == 'result':
        state['result'] = event


def _stop_group(proc, force: bool) -> None:
    if proc.poll() is not None:
        return
    if os.name == 'posix':
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(proc.pid, sig)
            return
        except OSError:
            pass
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except OSError:
        pass


def _read_stdout(proc, pending) -> None:
    try:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                pending.put(json.loads(text))
            except json.JSONDecodeError:
                continue
    finally:
        pending.put(_STDOUT_DONE)


def _read_stderr(proc, bucket) -> None:
    try:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            with bucket['lock']:
                bucket['text'] = (bucket['text'] + line)[-16384:]
    except Exception:
        return


def _stderr_text(bucket) -> str:
    with bucket['lock']:
        return bucket['text']


def abandon_file(worker, row, log_text='') -> None:
    """Mark one file failed after its process died. The original stays."""
    source = ''
    if isinstance(row, dict):
        source = row.get('source') or row.get('path') or ''
    name = os.path.basename(source) or 'file'
    try:
        if source:
            plan = replace_plan(source)
            remove_hold_busy_marker(plan)
            temp = plan.get('temp') or ''
            if temp and os.path.isfile(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
            remove_empty_directory(plan.get('hold_dir') or '', getattr(worker, 'book', None))
    except Exception as exc:
        extra = str(exc)
        log_text = (log_text + '\n' + extra).strip() if log_text else extra
    if isinstance(row, dict):
        row['status'] = 'Failed'
        row['checked'] = True
        row['reason'] = 'The encode stopped.'
    if source:
        append_convert_log(
            'FAIL', 'The encode stopped.', source, '', '',
            0, 0, 0, 0, row.get('width') if isinstance(row, dict) else None,
            row.get('height') if isinstance(row, dict) else None,
            (row.get('target') if isinstance(row, dict) else 0) or 0,
        )
    reason = 'The encode stopped.'
    if hasattr(worker, '_note_fault'):
        worker._note_fault('encode', source, reason, handbrake_log=log_text or reason)
    worker._post('activity', f'Failed {name}. The encode stopped. The original stayed.', 'err')
    if isinstance(row, dict) and hasattr(worker, '_post_row'):
        worker._post_row(row)


def _accept_result(worker, row, result) -> str:
    snapshot = result.get('row') or {}
    merge_worker_row(row, snapshot)
    try:
        saved = int(result.get('saved') or 0)
    except (TypeError, ValueError):
        saved = 0
    book = result.get('book') or []
    if not isinstance(book, list):
        book = []
    with worker._state:
        if book:
            worker.book.rows.extend(item for item in book if item)
        if saved > 0:
            worker.saved += saved
    outcome = result.get('outcome') or ''
    return outcome or ''


def _run_child(worker, row, locked_pool: bool):
    """Spawn one child. Returns 'requeue', 'again', or None."""
    try:
        job = _job_for(worker, row, locked_pool)
        payload = json.dumps(job) + '\n'
    except (TypeError, ValueError) as exc:
        abandon_file(worker, row, log_text=str(exc))
        return None
    command = [sys.executable, '-u', '-m', 'video_dehydrator.filejob']
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    popen_kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
        env=env,
    )
    # The child leads a new session so a stop can signal HandBrake and the
    # flatpak wrapper together. The window process is not in that group.
    if os.name == 'posix':
        popen_kwargs['start_new_session'] = True
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        abandon_file(worker, row, log_text=str(exc))
        return None
    bucket = {'lock': threading.Lock(), 'text': ''}
    pending = queue.Queue()
    out_thread = threading.Thread(target=_read_stdout, args=(proc, pending), name='vd-file-out', daemon=True)
    err_thread = threading.Thread(target=_read_stderr, args=(proc, bucket), name='vd-file-err', daemon=True)
    out_thread.start()
    err_thread.start()
    try:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        state = {}
        cancel_sent = False
        killed = False
        deadline = None
        while True:
            if worker.cancel.is_set() and not cancel_sent:
                _stop_group(proc, False)
                cancel_sent = True
                deadline = time.monotonic() + STOP_WAIT_SEC
            try:
                event = pending.get(timeout=0.2)
            except queue.Empty:
                if cancel_sent and not killed and deadline is not None and time.monotonic() >= deadline:
                    _stop_group(proc, True)
                    killed = True
                continue
            if event is _STDOUT_DONE:
                break
            _apply_child_event(worker, row, event, state)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _stop_group(proc, True)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = proc.poll()
    finally:
        if proc.poll() is None:
            _stop_group(proc, True)
        err_thread.join(timeout=2)
        out_thread.join(timeout=2)
    result = state.get('result')
    requeue = state.get('requeue')
    wants_requeue = isinstance(requeue, dict) or (
        isinstance(result, dict) and (result.get('outcome') or '') == 'requeue'
    )
    if wants_requeue and code == 0:
        if locked_pool or not isinstance(requeue, dict):
            abandon_file(worker, row, log_text=_stderr_text(bucket))
            return None
        probe = SimpleNamespace(width=requeue.get('width'), height=requeue.get('height'))
        if worker._settle_pool(row, probe) == 'requeue':
            return 'requeue'
        return 'again'
    if isinstance(result, dict) and code == 0:
        outcome = _accept_result(worker, row, result)
        if outcome == 'requeue':
            return 'requeue'
        return None
    # No finished result. Leave the original, drop any partial recycle book,
    # and mark this file failed so the batch can continue.
    abandon_file(worker, row, log_text=_stderr_text(bucket))
    return None


def run_isolated_file(worker, row):
    """Run encode_file in a child process. Returns 'requeue' or None.

    A failed start marks the file failed. The file is not encoded in this process.
    """
    try:
        _show_file(worker, row)
        locked = False
        for _attempt in range(2):
            outcome = _run_child(worker, row, locked)
            if outcome == 'again':
                locked = True
                continue
            return outcome
        abandon_file(worker, row, log_text='The file asked to change thread count twice.')
        return None
    except Exception:
        abandon_file(worker, row, log_text=traceback.format_exc())
        return None


if __name__ == '__main__':
    raise SystemExit(main())
