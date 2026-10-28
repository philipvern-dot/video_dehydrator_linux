"""Settings, held originals, size skips, and the convert log."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .engine import GIB, app_dir, app_version

DEFAULTS = {
    'folder': '',
    'includeSubfolders': True,
    'autoDelete': False,
    'autoWarnAccepted': False,
    'reencodeAudio': False,
    'detailed': False,
    'budgets': '',
    'shutdown': False,
    'updatePrompt': '',
    'deleteConfirmVersion': '',
    'autoAsk': True,
    'purgeRecycle': False,
    'recycleAlertMuted': False,
    'listExtra': 0,
    'columnOrder': [],
    'columnWidths': {},
}


def _path(name: str) -> Path:
    return app_dir() / name


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    path = _path('settings.json')
    if not path.is_file():
        return settings
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError):
        return settings
    for key in settings:
        if key in payload and payload[key] is not None:
            settings[key] = payload[key]
    try:
        settings['listExtra'] = int(settings.get('listExtra') or 0)
    except (TypeError, ValueError):
        settings['listExtra'] = 0
    order = settings.get('columnOrder')
    settings['columnOrder'] = [str(name) for name in order] if isinstance(order, list) else []
    settings['columnWidths'] = clean_column_widths(settings.get('columnWidths'))
    settings['autoAsk'] = bool(settings.get('autoAsk', True))
    settings['purgeRecycle'] = bool(settings.get('purgeRecycle'))
    settings['recycleAlertMuted'] = bool(settings.get('recycleAlertMuted'))
    return settings


def save_settings(settings: dict) -> None:
    path = _path('settings.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: settings.get(key, DEFAULTS[key]) for key in DEFAULTS}
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')


def clean_column_widths(value) -> dict:
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, raw in value.items():
        try:
            width = int(raw)
        except (TypeError, ValueError):
            continue
        if 48 <= width <= 4000:
            clean[str(key)] = width
    return clean


def flatten_rows(value):
    rows = []
    stack = list(value or [])
    stack.reverse()
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            for inner in reversed(item):
                if inner is not None:
                    stack.append(inner)
            continue
        if item is not None:
            rows.append(item)
    return rows


def read_pending() -> list:
    path = _path('pending.json')
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in flatten_rows(payload) if isinstance(row, dict)]


def write_pending(rows) -> None:
    path = _path('pending.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = [row for row in rows or [] if isinstance(row, dict)]
    path.write_text(json.dumps(clean, indent=2) + '\n', encoding='utf-8')


def read_skips() -> list:
    path = _path('skipped.json')
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in flatten_rows(payload) if isinstance(row, dict) and row.get('path')]


def write_skips(rows) -> None:
    path = _path('skipped.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = []
    for row in rows or []:
        if not row or not row.get('path'):
            continue
        try:
            size = int(row.get('size') or 0)
            target = int(row.get('targetKbps') or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0 or target <= 0:
            continue
        clean.append({'path': str(row['path']), 'size': size, 'targetKbps': target})
    path.write_text(json.dumps(clean, indent=2) + '\n', encoding='utf-8')


def append_convert_log(status, reason, source, new_path, kept, old_bytes, new_bytes, old_rate, new_rate, width, height, kbps) -> None:
    path = _path('convert-log.tsv')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        header = 'Timestamp\tStatus\tReason\tSource\tNew\tKept\tOldGiB\tNewGiB\tOldGiBph\tNewGiBph\tWidth\tHeight\tKbps\n'
        path.write_text(header, encoding='utf-8')
    def gib(value):
        if not value:
            return ''
        return f'{int(value) / GIB:.3f}'
    def rate(value):
        if value is None or value == '':
            return ''
        return f'{float(value):.3f}'
    reason_text = ' '.join(str(reason or '').split())
    row = [
        datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        status, reason_text, source or '', new_path or '', kept or '',
        gib(old_bytes), gib(new_bytes), rate(old_rate), rate(new_rate),
        '' if width is None else str(width),
        '' if height is None else str(height),
        '' if kbps is None else str(kbps),
    ]
    with path.open('a', encoding='utf-8', newline='') as handle:
        handle.write('\t'.join(row) + '\n')


def current_version() -> str:
    return app_version()
