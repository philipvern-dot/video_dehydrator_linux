"""Size rules, probe, encode check, and file swap. Matches the Windows engine."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


REFERENCE_PIXELS = float(1920 * 1080)
REFERENCE_KBPS = 3000
REFERENCE_BUDGET_GIBPH = 2.0
REFERENCE_HEVC_GIBPH = 3.0
MINIMUM_KBPS = 200
AUDIO_COPY_MASK = 'aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,flac,opus'
COMPARE_HALF_WIDTH = 960
COMPARE_HALF_HEIGHT = 540
COMPARE_FPS = 12
GIB = 1024 ** 3
MIB = 1024 ** 2
KIB = 1024
MIN_KEEP_BYTES = 64 * 1024

VIDEO_EXTENSIONS = {
    '.avi', '.mpg', '.mpeg', '.mp4', '.mkv', '.m4v', '.wmv', '.mov',
    '.ts', '.vob', '.m2ts', '.webm', '.flv', '.divx',
}

BUDGET_ANCHORS = (
    {'id': '2160', 'name': '2160p', 'width': 3840, 'height': 2160, 'color': '#2563eb'},
    {'id': '1080', 'name': '1080p', 'width': 1920, 'height': 1080, 'color': '#0f766e'},
    {'id': '720', 'name': '720p', 'width': 1280, 'height': 720, 'color': '#7e22ce'},
    {'id': '480', 'name': '480p', 'width': 720, 'height': 480, 'color': '#c2410c'},
)

BUDGET_FACTORS = (
    {'id': 'half', 'label': 'Half', 'factor': 0.5},
    {'id': 'recommended', 'label': 'Recommended', 'factor': 1.0},
    {'id': 'plus', 'label': '1.5 times', 'factor': 1.5},
    {'id': 'double', 'label': 'Double', 'factor': 2.0},
)

_picks = {row['id']: 'recommended' for row in BUDGET_ANCHORS}


def project_root() -> Path:
    here = Path(__file__).resolve().parent
    parent = here.parent
    if parent.name == 'app':
        return parent.parent
    return parent


def app_dir() -> Path:
    return project_root() / 'app'


def tools_dir() -> Path:
    return project_root() / 'tools'


def tool_path(name: str) -> Path:
    direct = tools_dir() / name
    if direct.is_file():
        return direct
    exe = tools_dir() / (name + '.exe')
    if exe.is_file():
        return exe
    return direct


def bundled_tools() -> dict:
    return {
        'root': project_root(),
        'tools': tools_dir(),
        'ffmpeg': tool_path('ffmpeg'),
        'ffprobe': tool_path('ffprobe'),
        'ffplay': tool_path('ffplay'),
        'handbrake': tool_path('HandBrakeCLI'),
    }


def missing_tools() -> list:
    tools = bundled_tools()
    missing = []
    for key in ('handbrake', 'ffmpeg', 'ffprobe'):
        if not tools[key].is_file():
            missing.append(str(tools[key]))
    return missing


def picture_scale(width: int, height: int):
    if width <= 0 or height <= 0:
        return None
    return (width * float(height)) / REFERENCE_PIXELS


def nearest_budget_anchor(width: int, height: int):
    pixels = float(width) * float(height)
    best = None
    best_distance = None
    for anchor in BUDGET_ANCHORS:
        distance = abs(pixels - (anchor['width'] * anchor['height']))
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = anchor
    return best


def budget_pick_factor(width: int, height: int) -> float:
    anchor = nearest_budget_anchor(width, height)
    pick = 'recommended'
    if anchor and anchor['id'] in _picks:
        pick = _picks[anchor['id']]
    for row in BUDGET_FACTORS:
        if row['id'] == pick:
            return float(row['factor'])
    return 1.0


def set_budget_picks(packed: str) -> None:
    global _picks
    _picks = {row['id']: 'recommended' for row in BUDGET_ANCHORS}
    if not packed:
        return
    known = {row['id'] for row in BUDGET_FACTORS}
    for part in packed.split(';'):
        match = re.fullmatch(r'(\d+)=([A-Za-z0-9.]+)', part)
        if not match:
            continue
        anchor_id, choice = match.group(1), match.group(2)
        if anchor_id in _picks and choice in known:
            _picks[anchor_id] = choice


def budget_pick_pack() -> str:
    return ';'.join(row['id'] + '=' + _picks.get(row['id'], 'recommended') for row in BUDGET_ANCHORS)


def anchor_budget(anchor, pick_id: str, efficient: bool = False) -> dict:
    scale = picture_scale(anchor['width'], anchor['height'])
    factor = 1.0
    for row in BUDGET_FACTORS:
        if row['id'] == pick_id:
            factor = float(row['factor'])
    base = REFERENCE_HEVC_GIBPH if efficient else REFERENCE_BUDGET_GIBPH
    kbps = int(round(REFERENCE_KBPS * scale * factor))
    if kbps < MINIMUM_KBPS:
        kbps = MINIMUM_KBPS
    return {
        'gibph': base * scale * factor,
        'hevc_gibph': REFERENCE_HEVC_GIBPH * scale * factor,
        'kbps': kbps,
    }


def format_budget_gib(value: float) -> str:
    text = f'{value:.2f}'.rstrip('0').rstrip('.')
    if text == '':
        text = '0'
    return text + ' GiB/h'


def budget_choice_label(anchor, pick_id: str) -> str:
    label = pick_id
    for row in BUDGET_FACTORS:
        if row['id'] == pick_id:
            label = row['label']
    gib = anchor_budget(anchor, pick_id)['gibph']
    return label + ', ' + format_budget_gib(gib)


def target_kbps(width: int, height: int):
    scale = picture_scale(width, height)
    if scale is None:
        return None
    kbps = int(round(REFERENCE_KBPS * scale * budget_pick_factor(width, height)))
    if kbps < MINIMUM_KBPS:
        kbps = MINIMUM_KBPS
    return kbps


def budget_gib_per_hour(width: int, height: int, efficient: bool):
    scale = picture_scale(width, height)
    if scale is None:
        return None
    base = REFERENCE_HEVC_GIBPH if efficient else REFERENCE_BUDGET_GIBPH
    return base * scale * budget_pick_factor(width, height)


def gib_per_hour(size_bytes: int, seconds: float):
    if seconds is None or seconds <= 0:
        return None
    return (size_bytes / GIB) / (seconds / 3600.0)


def format_byte_size(size_bytes: int) -> str:
    if size_bytes >= GIB:
        return f'{size_bytes / GIB:.2f} GiB'
    if size_bytes >= MIB:
        return f'{size_bytes / MIB:.1f} MiB'
    if size_bytes >= KIB:
        return f'{size_bytes / KIB:.0f} KiB'
    return f'{size_bytes} B'


def format_clock(seconds: float) -> str:
    if seconds is None or math.isnan(seconds) or seconds < 0:
        seconds = 0
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 1:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def format_rate(value) -> str:
    if value is None:
        return ''
    return f'{float(value):.2f}'


def over_ratio(gibph, budget):
    if gibph is None or budget is None or gibph == '' or budget == '':
        return None
    budget = float(budget)
    if budget <= 0:
        return None
    return float(gibph) / budget


def over_band_id(ratio) -> str:
    if ratio is None or ratio == '':
        return ''
    value = float(ratio)
    if value < 1:
        return ''
    if value < 1.5:
        return 'slight'
    if value < 3:
        return 'heavy'
    return 'extreme'


def format_over_ratio(ratio) -> str:
    if ratio is None or ratio == '':
        return ''
    value = float(ratio)
    places = 2
    if value > 1:
        while places < 4 and round(value + 1e-12, places) <= 1:
            places += 1
    return f'{value:.{places}f}x'


def handbrake_progress(line: str):
    match = re.search(r'Encoding:\s*task\s+(\d+)\s+of\s+(\d+),\s*([\d.]+)\s*%', line or '')
    if not match:
        return None
    task = int(match.group(1))
    tasks = int(match.group(2))
    part = float(match.group(3))
    if tasks < 1:
        tasks = 1
    if task < 1:
        task = 1
    if task > tasks:
        task = tasks
    overall = ((task - 1) + (part / 100.0)) / tasks * 100.0
    overall = max(0.0, min(100.0, overall))
    eta = -1.0
    eta_match = re.search(r'ETA\s+(\d+)h(\d+)m(\d+)s', line or '')
    if eta_match:
        eta = int(eta_match.group(1)) * 3600 + int(eta_match.group(2)) * 60 + int(eta_match.group(3))
    return {
        'task': task,
        'tasks': tasks,
        'part': part,
        'overall': overall,
        'eta_seconds': eta,
    }


def handbrake_fraction(line: str):
    progress = handbrake_progress(line)
    if progress is None:
        return None
    return progress['overall']


def pixel_seconds(width, height, duration_sec) -> float:
    try:
        width = int(width)
        height = int(height)
        duration_sec = float(duration_sec)
    except (TypeError, ValueError):
        return 0.0
    if width <= 0 or height <= 0 or duration_sec <= 0:
        return 0.0
    if math.isnan(duration_sec) or math.isinf(duration_sec):
        return 0.0
    return float(width) * float(height) * duration_sec


def encode_cushion_seconds() -> int:
    return 30


def _round_away(value: float) -> int:
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def format_remaining_time(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ''
    if seconds < 0 or math.isnan(seconds) or math.isinf(seconds):
        return ''
    rounded = _round_away(seconds)
    if rounded < 60:
        if rounded < 1:
            rounded = 1
        return f'about {rounded} sec'
    minutes = _round_away(seconds / 60.0)
    if minutes < 90:
        if minutes < 1:
            minutes = 1
        return f'about {minutes} min'
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f'about {hours} hr'
    return f'about {hours} hr {mins} min'


def mean_sample(values) -> float:
    total = 0.0
    count = 0
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0 or math.isnan(number) or math.isinf(number):
            continue
        total += number
        count += 1
    if count == 0:
        return 0.0
    return total / count


def median_sample(values) -> float:
    kept = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0 or math.isnan(number) or math.isinf(number):
            continue
        kept.append(number)
    if not kept:
        return 0.0
    kept.sort()
    mid = len(kept) // 2
    if len(kept) % 2 == 1:
        return kept[mid]
    return (kept[mid - 1] + kept[mid]) / 2.0


def encode_rate(live_rate, finished_rates, pass_fraction=1.0) -> float:
    finished = mean_sample(finished_rates)
    try:
        live = float(live_rate)
    except (TypeError, ValueError):
        live = 0.0
    if math.isnan(live) or math.isinf(live) or live <= 0:
        live = 0.0
    if live <= 0:
        return finished
    if finished <= 0:
        return live
    try:
        fraction = float(pass_fraction)
    except (TypeError, ValueError):
        fraction = 1.0
    if math.isnan(fraction) or fraction < 0:
        fraction = 0.0
    if fraction > 1:
        fraction = 1.0
    live_weight = 0.25 if fraction < 0.15 else 0.5
    return (live * live_weight) + (finished * (1.0 - live_weight))


def live_encode_rate(pass1_sec, pass_elapsed_sec, pass_fraction, pixels) -> float:
    try:
        pass1_sec = float(pass1_sec)
        pass_elapsed_sec = float(pass_elapsed_sec)
        pass_fraction = float(pass_fraction)
        pixels = float(pixels)
    except (TypeError, ValueError):
        return 0.0
    if pass1_sec <= 0 or pixels <= 0:
        return 0.0
    if pass_elapsed_sec < 8 or pass_fraction < 0.05:
        return 0.0
    if pass_fraction > 1:
        pass_fraction = 1
    pass2 = pass_elapsed_sec / pass_fraction
    encode = pass1_sec + pass2
    if encode <= 0 or math.isnan(encode) or math.isinf(encode):
        return 0.0
    return encode / pixels


def remaining_estimate(progress, pass_elapsed_sec=0, pass1_sec=0, pass_ratio=0, rate8=0, rate10=0, waiting=None, phase='encode') -> dict:
    cushion = float(encode_cushion_seconds())
    file_kind = 'none'
    file_seconds = 0.0
    if phase == 'check':
        file_kind = 'check'
        file_seconds = cushion
    elif progress is not None and float(progress.get('eta_seconds', -1)) >= 0:
        last_pass = int(progress.get('task') or 0) >= int(progress.get('tasks') or 0)
        if last_pass:
            file_kind = 'full'
            file_seconds = float(progress['eta_seconds']) + cushion
        elif pass_ratio and float(pass_ratio) > 0:
            so_far = float(pass_elapsed_sec)
            if so_far < 0:
                so_far = 0
            total_pass1 = so_far + float(progress['eta_seconds'])
            file_kind = 'full'
            file_seconds = float(progress['eta_seconds']) + (total_pass1 * float(pass_ratio)) + cushion
        else:
            file_kind = 'pass'
            file_seconds = float(progress['eta_seconds'])
    have_batch = False
    batch_seconds = 0.0
    if file_kind in ('full', 'check'):
        have_batch = True
        batch_seconds = file_seconds
        for item in waiting or []:
            if not item:
                continue
            known = bool(item.get('known', True))
            pixels = float(item.get('pixel_seconds') or 0)
            if not known or pixels <= 0:
                have_batch = False
                break
            rate = float(rate10 if item.get('ten_bit') else rate8)
            if rate <= 0 or math.isnan(rate) or math.isinf(rate):
                have_batch = False
                break
            batch_seconds += (pixels * rate) + cushion
    return {
        'file_kind': file_kind,
        'file_seconds': file_seconds,
        'batch_seconds': batch_seconds if have_batch else 0,
        'have_batch': have_batch,
        'text': format_eta_text(progress, file_kind, file_seconds, batch_seconds, have_batch),
    }


def format_eta_text(progress, file_kind, file_seconds, batch_seconds, have_batch) -> str:
    if file_kind == 'pass':
        pass_no = int(progress.get('task') or 1) if progress else 1
        pass_name = 'first pass' if pass_no < 2 else f'pass {pass_no}'
        file_text = f'{pass_name}, {format_remaining_time(file_seconds)} left'
    elif file_kind in ('full', 'check'):
        file_text = f'{format_remaining_time(file_seconds)} left'
    else:
        file_text = 'estimating'
    batch_text = format_remaining_time(batch_seconds) if have_batch else 'estimating'
    return f'This file: {file_text}. Estimated time remaining: {batch_text}'


def estimated_savings_bytes(size_bytes, estimate_bytes, old_bytes) -> int | None:
    if old_bytes:
        return None
    try:
        size = int(size_bytes or 0)
        estimate = int(estimate_bytes or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or estimate <= 0 or estimate >= size:
        return None
    return size - estimate


def space_saved_bytes(size_bytes, old_bytes) -> int | None:
    try:
        old = int(old_bytes or 0)
        new = int(size_bytes or 0)
    except (TypeError, ValueError):
        return None
    if old <= 0 or new <= 0 or old <= new:
        return None
    return old - new


def is_encode_progress_line(line: str) -> bool:
    text = line or ''
    if re.search(r'Encoding:\s*task\s+\d+\s+of\s+\d+.*%', text):
        return True
    if re.search(r'Muxing:\s*\d+', text):
        return True
    return False


def _positive_int(text) -> int:
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _tagged_text(tags, name: str) -> str:
    if not isinstance(tags, dict) or not name:
        return ''
    direct = tags.get(name)
    if direct not in (None, ''):
        return str(direct)
    prefix = name.lower() + '-'
    for key, value in tags.items():
        if str(key).lower().startswith(prefix) and value not in (None, ''):
            return str(value)
    return ''


def stream_bitrate(stream: dict, duration_sec: float) -> int:
    if not stream:
        return 0
    bits = _positive_int(stream.get('bit_rate'))
    if bits > 0:
        return bits
    tags = stream.get('tags') if isinstance(stream.get('tags'), dict) else {}
    tagged = _positive_int(_tagged_text(tags, 'BPS'))
    if tagged > 0:
        return tagged
    if duration_sec and duration_sec > 0:
        nbytes = _positive_int(_tagged_text(tags, 'NUMBER_OF_BYTES'))
        if nbytes > 0:
            return int(round((nbytes * 8.0) / duration_sec))
    return 0


# AC-3 frame size in 16-bit words. Row is frmsizecod 0..37.
# Column is 48 kHz, 44.1 kHz, 32 kHz.
AC3_FRAME_WORDS = (
    (64, 69, 96), (64, 70, 96), (80, 87, 120), (80, 88, 120),
    (96, 104, 144), (96, 105, 144), (112, 121, 168), (112, 122, 168),
    (128, 139, 192), (128, 140, 192), (160, 174, 240), (160, 175, 240),
    (192, 208, 288), (192, 209, 288), (224, 243, 336), (224, 244, 336),
    (256, 278, 384), (256, 279, 384), (320, 348, 480), (320, 349, 480),
    (384, 417, 576), (384, 418, 576), (448, 487, 672), (448, 488, 672),
    (512, 557, 768), (512, 558, 768), (640, 696, 960), (640, 697, 960),
    (768, 835, 1152), (768, 836, 1152), (896, 975, 1344), (896, 976, 1344),
    (1024, 1114, 1536), (1024, 1115, 1536), (1152, 1253, 1728), (1152, 1254, 1728),
    (1280, 1393, 1920), (1280, 1394, 1920),
)


@dataclass
class Probe:
    path: str = ''
    duration_sec: float = 0.0
    size_bytes: int = 0
    video_codec: str = ''
    width: int = 0
    height: int = 0
    pix_fmt: str = ''
    is_10bit: bool = False
    is_interlaced: bool = False
    is_dolby_vision: bool = False
    is_hdr10_plus: bool = False
    is_hdr10: bool = False
    is_hlg: bool = False
    audio_codecs: list = field(default_factory=list)
    audio_count: int = 0
    sub_count: int = 0
    already_efficient: bool = False
    video_bitrate: int = 0


def probe_from_json(payload: dict, file_path: str = '') -> Probe:
    streams = payload.get('streams') or []
    video = None
    audio = []
    subs = []
    for stream in streams:
        kind = stream.get('codec_type')
        if kind == 'audio':
            audio.append(stream)
            continue
        if kind == 'subtitle':
            subs.append(stream)
            continue
        if kind != 'video' or video is not None:
            continue
        disposition = stream.get('disposition') or {}
        if str(disposition.get('attached_pic')) == '1':
            continue
        if _positive_int(stream.get('width')) <= 0:
            continue
        video = stream
    if video is None:
        raise RuntimeError(f'No video picture in {file_path}')
    fmt = payload.get('format') or {}
    try:
        duration = float(fmt.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    size = _positive_int(fmt.get('size'))
    if size <= 0 and file_path and os.path.isfile(file_path):
        size = os.path.getsize(file_path)
    side_types = []
    for side in video.get('side_data_list') or []:
        side_type = side.get('side_data_type')
        if side_type:
            side_types.append(str(side_type))
    side_blob = ' | '.join(side_types)
    tag = str(video.get('codec_tag_string') or '')
    is_dovi = bool(re.match(r'^(dvhe|dvh1|dva1|dvav)$', tag)) or bool(re.search(r'DOVI|Dolby Vision|dovi', side_blob)) or ('dovi_configuration' in video)
    transfer = str(video.get('color_transfer') or '')
    pix = str(video.get('pix_fmt') or '')
    field = str(video.get('field_order') or '')
    codec = str(video.get('codec_name') or '')
    return Probe(
        path=file_path or '',
        duration_sec=duration,
        size_bytes=size,
        video_codec=codec,
        width=_positive_int(video.get('width')),
        height=_positive_int(video.get('height')),
        pix_fmt=pix,
        is_10bit=bool(re.search(r'p10|10le|10be', pix)),
        is_interlaced=bool(field) and field not in ('progressive', 'unknown'),
        is_dolby_vision=is_dovi,
        is_hdr10_plus=bool(re.search(r'HDR10\+|SMPTE2094-40|SMPTE 2094', side_blob)),
        is_hdr10=(transfer == 'smpte2084') or bool(re.search(r'Mastering display|Content light level', side_blob)),
        is_hlg=(transfer == 'arib-std-b67'),
        audio_codecs=[str(item.get('codec_name') or '') for item in audio],
        audio_count=len(audio),
        sub_count=len(subs),
        already_efficient=codec in ('hevc', 'h265', 'av1'),
        video_bitrate=stream_bitrate(video, duration),
    )


def run_captured(command, cancel=None) -> tuple:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    out, err = proc.communicate()
    if cancel is not None and cancel.is_set():
        return proc.returncode, '', 'cancelled'
    return proc.returncode, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


def probe_file(ffprobe: str, file_path: str) -> Probe:
    code, out, err = run_captured([
        ffprobe, '-v', 'error', '-show_format', '-show_streams', '-print_format', 'json', '--', file_path,
    ])
    if not out.strip():
        raise RuntimeError(f'ffprobe returned nothing for {file_path}' + (f': {err.strip()}' if err.strip() else ''))
    if code not in (0, None) and not out.strip():
        raise RuntimeError(err.strip() or f'ffprobe failed for {file_path}')
    return probe_from_json(json.loads(out), file_path)


def estimated_bytes(probe: Probe, target: int):
    if probe is None or probe.duration_sec <= 0 or target is None or target <= 0:
        return None
    other = 0.0
    if probe.video_bitrate > 0 and probe.size_bytes > 0:
        file_bps = (probe.size_bytes * 8.0) / probe.duration_sec
        other = file_bps - probe.video_bitrate
        if other < 0:
            other = 0
    estimate = ((target * 1000.0) + other) * probe.duration_sec / 8.0
    if estimate < 0:
        return None
    return int(round(estimate))


def shrinkable_picture(probe: Probe, target, estimate) -> bool:
    if probe is None or not target or int(target) <= 0:
        return False
    if probe.video_bitrate <= 0 or probe.size_bytes <= 0 or probe.duration_sec <= 0:
        return False
    if estimate is None or int(estimate) <= 0 or int(estimate) >= int(probe.size_bytes):
        return False
    file_bps = (probe.size_bytes * 8.0) / probe.duration_sec
    if file_bps <= 0 or probe.video_bitrate < (file_bps * 0.4):
        return False
    return probe.video_bitrate > (int(target) * 1000.0 * 1.05)


def bloat_decision(probe: Probe) -> dict:
    scale = picture_scale(probe.width, probe.height)
    target = target_kbps(probe.width, probe.height)
    budget = None
    if scale is not None:
        budget = budget_gib_per_hour(probe.width, probe.height, probe.already_efficient)
    rate = gib_per_hour(probe.size_bytes, probe.duration_sec)
    estimate = estimated_bytes(probe, target) if target is not None else None
    decision = 'lean'
    reason = 'Under the budget for this picture size.'
    if probe.is_dolby_vision:
        decision = 'dv'
        reason = 'Dolby Vision is left alone.'
    elif probe.duration_sec <= 0:
        decision = 'nodur'
        reason = 'The duration could not be read.'
    elif probe.width <= 0 or probe.height <= 0:
        decision = 'nopicture'
        reason = 'The picture size could not be read.'
    elif rate is None or budget is None:
        decision = 'nodur'
        reason = 'The size per hour could not be read.'
    elif rate < budget:
        if shrinkable_picture(probe, target, estimate):
            decision = 'shrink'
            reason = 'The file is under the budget line. The picture is still above the target rate, so a new file should be smaller.'
        else:
            decision = 'lean'
            reason = 'Under the budget for this picture size.'
    else:
        file_bps = (probe.size_bytes * 8.0) / probe.duration_sec
        target_bps = (target or 0) * 1000.0
        video_known = probe.video_bitrate > 0 and file_bps > 0 and probe.video_bitrate >= (file_bps * 0.4)
        if video_known and probe.video_bitrate <= (target_bps * 1.05):
            decision = 'within'
            reason = 'The picture is already within the target bits per second. Extra size is audio or other tracks, which stay as they are.'
        else:
            decision = 'bloated'
            reason = 'Over the budget for this picture size.'
    return {
        'decision': decision,
        'reason': reason,
        'scale': scale,
        'budget_gibph': budget,
        'target_kbps': target,
        'gibph': rate,
        'estimate_bytes': estimate,
        'estimate_rough': bool(estimate is not None and estimate > 0 and probe.video_bitrate <= 0),
    }


def force_convertible(decision) -> bool:
    if not decision:
        return False
    kind = str(decision.get('decision') or '')
    if kind in ('dv', 'nodur', 'nopicture'):
        return False
    try:
        return int(decision.get('target_kbps') or 0) > 0
    except (TypeError, ValueError):
        return False


def force_video_paths(path: str, recurse: bool) -> list:
    if not path or not str(path).strip():
        return []
    if os.path.isfile(path):
        return [os.path.abspath(path)] if is_video_file(path) else []
    if not os.path.isdir(path):
        return []
    found = []

    def walk(directory, nested):
        leaf = os.path.basename(directory.rstrip('\\/'))
        if leaf == '.vd-originals':
            return
        try:
            with os.scandir(directory) as scan:
                names = list(scan)
        except OSError:
            return
        for entry in names:
            if entry.is_file(follow_symlinks=False) and is_video_file(entry.path):
                found.append(entry.path)
            elif nested and entry.is_dir(follow_symlinks=False):
                walk(entry.path, True)

    walk(path, recurse)
    return found


def format_force_report(files) -> str:
    ready = 0
    blocked = 0
    known_now = 0
    known_next = 0
    rough = False
    unknown = 0
    grows = 0
    lines = []
    shown = 0
    for file in files or []:
        if not file:
            continue
        name = file.get('name') or os.path.basename(str(file.get('path') or ''))
        if not file.get('can_convert'):
            blocked += 1
            if shown < 8:
                lines.append(f"{name} is left alone. {file.get('reason') or ''}".rstrip())
                shown += 1
            continue
        ready += 1
        try:
            size = int(file.get('size') or 0)
        except (TypeError, ValueError):
            size = 0
        estimate = file.get('estimate')
        has_estimate = False
        try:
            estimate = int(estimate) if estimate not in (None, '') else 0
        except (TypeError, ValueError):
            estimate = 0
        if estimate > 0:
            has_estimate = True
        if file.get('estimate_rough'):
            rough = True
        note = name
        if not has_estimate or size <= 0:
            unknown += 1
            note += ' has no size estimate.'
        else:
            known_now += size
            known_next += estimate
            if estimate >= size:
                grows += 1
                note += ' may not get smaller.'
            else:
                note += ' could save about ' + format_byte_size(size - estimate) + '.'
                if file.get('estimate_rough'):
                    note += ' That guess can run high.'
        if shown < 8:
            lines.append(note)
            shown += 1
    total = ready + blocked
    if total == 0:
        return 'No video files were found.'
    word = 'file' if ready == 1 else 'files'
    text = f'{ready} {word} can be converted.'
    if known_now > 0 and known_next > 0:
        text += ' They are ' + format_byte_size(known_now) + ' now. The estimate is ' + format_byte_size(known_next) + '.'
        if known_next < known_now:
            text += ' About ' + format_byte_size(known_now - known_next) + ' smaller.'
        else:
            text += ' The estimate is not smaller.'
    if unknown == 1:
        text += ' 1 file has no size estimate.'
    elif unknown > 1:
        text += f' {unknown} files have no size estimate.'
    if grows == 1:
        text += ' 1 file may not get smaller. A larger result is thrown away.'
    elif grows > 1:
        text += f' {grows} files may not get smaller. A larger result is thrown away.'
    if rough:
        text += ' A guess with no picture rate can run high.'
    if blocked == 1:
        text += ' 1 file will be left alone.'
    elif blocked > 1:
        text += f' {blocked} files will be left alone.'
    text += ' This test reads the files. It does not encode.'
    hidden = total - shown
    if lines:
        text += '\n\n' + '\n'.join(lines)
    if hidden > 0:
        text += '\n' + f'and {hidden} more.'
    return text


def encoder_name(probe: Probe) -> str:
    if probe.is_10bit or probe.is_hdr10 or probe.is_hdr10_plus or probe.is_hlg:
        return 'x265_10bit'
    return 'x265'


def handbrake_arguments(source: str, temp: str, probe: Probe, video_kbps: int, encoder: str, reencode_audio: bool, thread_pool: int = 0, single_pass: bool = False) -> list:
    fallback = 'av_aac' if reencode_audio else 'none'
    passes = ['--no-multi-pass', '--no-turbo'] if single_pass else ['--multi-pass', '--turbo']
    arguments = [
        '-i', source, '-o', temp, '--format', 'av_mkv', '--encoder', encoder,
        '--encoder-preset', 'medium', '--vb', str(video_kbps), *passes,
        '--vfr', '--crop-mode', 'none', '--crop', '0:0:0:0', '--markers',
        '--all-audio', '--aencoder', 'copy', '--audio-copy-mask', AUDIO_COPY_MASK,
        '--audio-fallback', fallback,
    ]
    sub_count = probe.sub_count if probe else 0
    if sub_count > 0:
        arguments += ['--all-subtitles', '--subtitle-burned=none', '--subtitle-default=none', '--keep-subname']
    else:
        arguments += ['--subtitle', 'none']
    if probe and probe.is_hdr10_plus:
        arguments += ['--hdr-dynamic-metadata', 'hdr10plus']
    if probe and probe.is_interlaced:
        arguments += ['--comb-detect', 'default', '--decomb']
    else:
        arguments += ['--no-comb-detect', '--no-deinterlace', '--no-decomb', '--no-bwdif']
    if thread_pool and int(thread_pool) > 0:
        arguments += ['--encopts', f'pools={int(thread_pool)}']
    return arguments


def job_count(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    if number < 1:
        return 1
    if number > 6:
        return 6
    return number


def scan_limit_reached(found, limit) -> bool:
    try:
        cap = int(limit)
        count = int(found)
    except (TypeError, ValueError):
        return False
    if cap <= 0:
        return False
    return count >= cap


def pass_two_did_not_start(text: str) -> bool:
    body = text or ''
    if 'encoded 0 frames' in body and 'task 2 of 2' in body:
        return True
    for needle in ('x265_encoder_open failed', 'Failure to initialise thread', "can't open stats file"):
        if needle in body:
            return True
    return False


def output_path_missing(text: str) -> bool:
    body = text or ''
    return 'avio_open2 failed' in body and 'errno -2' in body


def hold_busy_path(plan) -> str:
    if not plan or not plan.get('hold_dir'):
        return ''
    leaf = os.path.basename(plan.get('temp') or plan.get('source') or '')
    if leaf.endswith('.vd.tmp.mkv'):
        leaf = leaf[:-len('.vd.tmp.mkv')]
    else:
        leaf = os.path.splitext(leaf)[0]
    if not leaf:
        return ''
    return os.path.join(plan['hold_dir'], leaf + '.vd.busy')


def add_hold_busy_marker(plan) -> str:
    path = hold_busy_path(plan)
    if not path:
        return ''
    os.makedirs(plan['hold_dir'], exist_ok=True)
    if plan.get('source'):
        add_plexignore(os.path.dirname(plan['source']))
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(str(os.getpid()) + '\n')
    return path


def remove_hold_busy_marker(plan) -> None:
    path = hold_busy_path(plan)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def remove_stale_busy_markers(directory: str) -> None:
    if not directory or not os.path.isdir(directory):
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith('.vd.busy'):
            continue
        path = os.path.join(directory, name)
        pid = 0
        try:
            text = open(path, encoding='utf-8').read().strip()
            if text.isdigit():
                pid = int(text)
        except OSError:
            pid = 0
        alive = False
        if pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            try:
                os.remove(path)
            except OSError:
                pass


def delete_targets(rows, selected=None) -> list:
    chosen = [
        row for row in (rows or [])
        if row and row.get('checked') and row.get('status') == 'Ready to delete' and row.get('kept')
    ]
    if chosen:
        return chosen
    if selected and selected.get('status') == 'Ready to delete' and selected.get('kept'):
        return [selected]
    return []


def batch_wall_seconds(running_seconds, queued_seconds, jobs) -> float:
    lanes = job_count(jobs)
    running = []
    for item in running_seconds or []:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and not math.isnan(number) and not math.isinf(number):
            running.append(number)
    try:
        queued = float(queued_seconds or 0)
    except (TypeError, ValueError):
        queued = 0
    if queued < 0 or math.isnan(queued) or math.isinf(queued):
        queued = 0
    return (sum(running) + queued) / lanes


def numa_node_count() -> int:
    root = Path('/sys/devices/system/node')
    if not root.is_dir():
        return 1
    count = 0
    for child in root.iterdir():
        if child.name.startswith('node') and child.name[4:].isdigit():
            count += 1
    return max(1, count)


def encode_command(handbrake: str, arguments, jobs: int, node: int, numa_nodes: int, have_numactl: bool) -> list:
    command = [str(handbrake)] + list(arguments or [])
    if job_count(jobs) < 2 or not have_numactl or int(numa_nodes) < 2:
        return command
    bound = int(node) % int(numa_nodes)
    return ['numactl', f'--cpunodebind={bound}', f'--membind={bound}'] + command


def ac3_frame_size(data: bytes, offset: int) -> int:
    if data is None or offset < 0 or offset + 5 > len(data):
        return 0
    if data[offset] != 0x0B or data[offset + 1] != 0x77:
        return 0
    fscod = data[offset + 4] >> 6
    code = data[offset + 4] & 0x3F
    if fscod > 2 or code > 37:
        return 0
    words = AC3_FRAME_WORDS[code][fscod]
    if words <= 0:
        return 0
    return words * 2


def ac3_short_tail(data: bytes) -> bool:
    if not data or len(data) < 16:
        return False
    start = -1
    for index in range(0, len(data) - 5):
        size = ac3_frame_size(data, index)
        if size < 64:
            continue
        nxt = index + size
        if nxt >= len(data):
            continue
        if ac3_frame_size(data, nxt) > 0:
            start = index
            break
    if start < 0:
        return False
    pos = start
    frames = 0
    while pos < len(data):
        size = ac3_frame_size(data, pos)
        if size < 64:
            return False
        if pos + size > len(data):
            return frames >= 2
        if pos + size == len(data):
            return False
        frames += 1
        pos += size
    return False


def trailing_ac3_note(text: str) -> bool:
    if text is None or not str(text).strip():
        return False
    lines = [part.strip() for part in re.split(r'\s+\|\s+|\r?\n', text) if part and part.strip()]
    if not lines:
        return False
    incomplete = 0
    patterns = (
        re.compile(r'^\[ac3 @ [0-9A-Fa-fx]+\] incomplete frame$'),
        re.compile(r'^\[aist#\d+:\d+/ac3 @ [^\]]+\] \[dec:ac3 @ [^\]]+\] corrupt decoded frame$'),
        re.compile(r'^\[aist#\d+:\d+/ac3 @ [^\]]+\] \[dec:ac3 @ [^\]]+\] Error processing packet in decoder: Invalid data found when processing input$'),
        re.compile(r'^\[aist#\d+:\d+/ac3 @ [^\]]+\] \[dec:ac3 @ [^\]]+\] Error submitting packet to decoder: Invalid data found when processing input$'),
        re.compile(r'^\[aist#\d+:\d+/ac3 @ [^\]]+\] \[dec:ac3 @ [^\]]+\] Task finished with error code: -1094995529 \(Invalid data found when processing input\)$'),
        re.compile(r'^\[aist#\d+:\d+/ac3 @ [^\]]+\] \[dec:ac3 @ [^\]]+\] Terminating thread with return code -1094995529 \(Invalid data found when processing input\)$'),
    )
    prefix = re.compile(r'^decode Last exit=(-?\d+)\s+(.*)$')
    for line in lines:
        body = line
        match = prefix.match(body)
        if match:
            if int(match.group(1)) == 0:
                return False
            body = match.group(2)
        if equal_timestamp_line(body):
            continue
        if patterns[0].match(body):
            incomplete += 1
            continue
        if any(pattern.match(body) for pattern in patterns[1:]):
            continue
        return False
    return incomplete == 1


def copied_ac3(source_codecs, output_codecs) -> bool:
    source = list(source_codecs or [])
    output = list(output_codecs or [])
    found = False
    for index, left in enumerate(source):
        if str(left).strip().lower() != 'ac3':
            continue
        right = output[index] if index < len(output) else ''
        if str(right).strip().lower() != 'ac3':
            return False
        found = True
    return found


def source_ac3_short_tail(ffmpeg: str, file_path: str, codecs) -> bool:
    if not ffmpeg or not file_path or not os.path.isfile(file_path):
        return False
    indexes = [index for index, name in enumerate(list(codecs or [])) if str(name).strip().lower() == 'ac3']
    if not indexes:
        return False
    for index in indexes:
        temp_path = ''
        try:
            handle = tempfile.NamedTemporaryFile(prefix='vd-ac3-', suffix='.ac3', delete=False)
            temp_path = handle.name
            handle.close()
            code, _out, _err = run_captured([
                ffmpeg, '-y', '-hide_banner', '-nostdin', '-v', 'error',
                '-sseof', '-3', '-i', file_path,
                '-map', f'0:a:{index}',
                '-c', 'copy', '-f', 'ac3', temp_path,
            ])
            if code != 0 or not os.path.isfile(temp_path):
                return False
            with open(temp_path, 'rb') as raw:
                data = raw.read()
            if not ac3_short_tail(data):
                return False
        except Exception:
            return False
        finally:
            if temp_path and os.path.isfile(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    return True


def equal_timestamp_line(text: str) -> bool:
    if text is None or not str(text).strip():
        return False
    match = re.match(
        r'^(?:\[[^\]]+\]\s*)?Application provided invalid, non monotonically increasing dts to muxer in stream \d+:\s*(\d+)\s*>=\s*(\d+)\s*$',
        str(text).strip(),
    )
    return bool(match and match.group(1) == match.group(2))


def harmless_decode_note(text: str) -> bool:
    if text is None or not str(text).strip():
        return True
    lines = [part.strip() for part in re.split(r'\s+\|\s+|\r?\n', text) if part and part.strip()]
    if not lines:
        return True
    for line in lines:
        if not equal_timestamp_line(line):
            return False
    return True


def decode_window(ffmpeg: str, file_path: str, duration_sec: float, window: str) -> dict:
    seconds = 15
    if duration_sec and 0 < duration_sec < seconds:
        seconds = max(1, int(math.floor(duration_sec)))
    args = [ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-xerror']
    if window == 'Last' and duration_sec > seconds:
        args += ['-sseof', f'-{seconds}']
    elif window == 'First':
        args += ['-ss', '0']
    args += ['-t', str(seconds), '-i', file_path, '-f', 'null', '-']
    code, out, err = run_captured(args)
    detail = ' | '.join(part for part in re.split(r'\r?\n', (out + '\n' + err)) if part and part.strip())
    # -xerror turns a repeated equal timestamp into a non-zero exit. Those
    # notes are harmless on their own. A blank failure is still a failure.
    if harmless_decode_note(detail) and str(detail).strip():
        return {'ok': True, 'detail': ''}
    if code != 0 or not harmless_decode_note(detail):
        return {'ok': False, 'detail': f'decode {window} exit={code} {detail}'.strip()}
    return {'ok': True, 'detail': ''}


def copyable_audio_codec(codec: str) -> bool:
    if not codec or not str(codec).strip():
        return False
    name = str(codec).strip().lower()
    return name in {part.strip().lower() for part in AUDIO_COPY_MASK.split(',')}


def audio_fallback_reason(log: str, allow_reencode: bool) -> str:
    if allow_reencode or not log:
        return ''
    if re.search(r'passthru not possible|Auto Passthru: no codecs allowed|using fallback encoder', log):
        return 'audio could not be copied'
    return ''


def audio_change_reasons(source_codecs, output_codecs, allow_reencode: bool) -> list:
    source = list(source_codecs or [])
    output = list(output_codecs or [])
    reasons = []
    if len(source) != len(output):
        reasons.append(f'audio tracks changed from {len(source)} to {len(output)}')
    slots = max(len(source), len(output))
    for index in range(slots):
        left = source[index] if index < len(source) else 'missing'
        right = output[index] if index < len(output) else 'missing'
        if left == right:
            continue
        reencoded = allow_reencode and not copyable_audio_codec(left) and str(right).lower() == 'aac'
        if reencoded:
            continue
        reasons.append(f'audio {index} changed from {left} to {right}')
    return reasons


def size_skip_reason(reason: str) -> bool:
    parts = [part.strip() for part in str(reason or '').split(';') if part.strip()]
    if not parts:
        return False
    allowed = {'the new file is not smaller', 'the new file is not smaller per hour'}
    return all(part in allowed for part in parts)


def larger_file_note(reason, source_bytes=0, new_bytes=0, target=0, source_codecs=None, output_codecs=None) -> str:
    if 'the new file is not smaller' not in str(reason or ''):
        return ''
    source = list(source_codecs or [])
    output = list(output_codecs or [])
    reencoded = False
    copied = True
    slots = min(len(source), len(output))
    for index in range(slots):
        left, right = str(source[index]), str(output[index])
        if left == right:
            continue
        copied = False
        if right.lower() == 'aac' and not copyable_audio_codec(left):
            reencoded = True
    if len(output) < len(source):
        copied = False
    rate = f'{target} kbps' if target and target > 0 else 'the target rate'
    sentences = ['The original was left in place.']
    if reencoded:
        sentences.append(f'The picture was encoded at {rate}. Audio that could not be copied was re-encoded to AAC, and the rest was copied.')
    elif copied:
        sentences.append(f'The picture was encoded at {rate}, and the audio was copied as it was.')
    else:
        sentences.append(f'The picture was encoded at {rate}.')
    if source_bytes > 0 and new_bytes >= source_bytes:
        sentences.append(f'Together that made {format_byte_size(new_bytes)}, which is not smaller than the original {format_byte_size(source_bytes)}.')
    elif source_bytes > 0 and new_bytes > 0:
        sentences.append(f'The new file is {format_byte_size(new_bytes)} and the original is {format_byte_size(source_bytes)}. It is not smaller for the length of the video.')
    else:
        sentences.append('Together that is not smaller than the original.')
    sentences.append('That happens when the target rate plus the audio is more than this file already used.')
    return ' '.join(sentences)


def test_encoded_file(ffmpeg, ffprobe, temp, source_probe: Probe, handbrake_log: str, allow_audio_reencode: bool) -> dict:
    reasons = []
    if not os.path.isfile(temp):
        return {'ok': False, 'reason': 'The new file is missing.', 'out_probe': None, 'new_gibph': None, 'old_gibph': None, 'new_bytes': 0}
    new_bytes = os.path.getsize(temp)
    if new_bytes < MIN_KEEP_BYTES:
        reasons.append(f'new file is too small ({new_bytes} bytes)')
    fallback = audio_fallback_reason(handbrake_log, allow_audio_reencode)
    if fallback:
        reasons.append(fallback)
    if handbrake_log and re.search(r'Burned-in|Render/Burn-in', handbrake_log):
        reasons.append('a subtitle was burned into the picture')
    try:
        out = probe_file(ffprobe, temp)
    except Exception as exc:
        reasons.append(f'could not read the new file: {exc}')
        return {'ok': False, 'reason': '; '.join(reasons), 'out_probe': None, 'new_gibph': None, 'old_gibph': None, 'new_bytes': new_bytes}
    if abs(out.duration_sec - source_probe.duration_sec) > 2:
        reasons.append(f'duration changed from {round(source_probe.duration_sec, 1)}s to {round(out.duration_sec, 1)}s')
    if out.width != source_probe.width or out.height != source_probe.height:
        reasons.append(f'picture size changed from {source_probe.width}x{source_probe.height} to {out.width}x{out.height}')
    first = decode_window(ffmpeg, temp, out.duration_sec, 'First')
    if not first['ok']:
        reasons.append(first['detail'])
    last = decode_window(ffmpeg, temp, out.duration_sec, 'Last')
    if not last['ok']:
        # A VirtualDub-style AVI can end in the middle of an AC3 frame.
        # Copying that track keeps the short tail. Accept one incomplete
        # frame at the end when every copied AC3 track already ended that way.
        source_path = getattr(source_probe, 'path', '') or ''
        trailing = (
            trailing_ac3_note(last['detail'])
            and copied_ac3(source_probe.audio_codecs, out.audio_codecs)
            and source_ac3_short_tail(ffmpeg, source_path, source_probe.audio_codecs)
        )
        if not trailing:
            reasons.append(last['detail'])
    reasons.extend(audio_change_reasons(source_probe.audio_codecs, out.audio_codecs, allow_audio_reencode))
    if out.sub_count < source_probe.sub_count:
        reasons.append(f'subtitles dropped from {source_probe.sub_count} to {out.sub_count}')
    if new_bytes >= source_probe.size_bytes:
        reasons.append('the new file is not smaller')
    old_rate = gib_per_hour(source_probe.size_bytes, source_probe.duration_sec)
    new_rate = gib_per_hour(new_bytes, out.duration_sec)
    if new_rate is None or old_rate is None or new_rate >= old_rate:
        reasons.append('the new file is not smaller per hour')
    ok = len(reasons) == 0
    return {
        'ok': ok,
        'reason': 'ok' if ok else '; '.join(reasons),
        'out_probe': out,
        'new_gibph': new_rate,
        'old_gibph': old_rate,
        'new_bytes': new_bytes,
    }


def is_video_file(path: str) -> bool:
    extension = os.path.splitext(path)[1].lower()
    if extension not in VIDEO_EXTENSIONS:
        return False
    leaf = os.path.basename(path)
    lowered = leaf.lower()
    if lowered.endswith('.vd.tmp.mkv') or lowered.endswith('.hb.tmp.mkv') or '.hb.src.bak' in lowered:
        return False
    return True


def plexignore_lines():
    return ['.vd-originals', '.vd-originals/*']


def add_plexignore(directory: str) -> None:
    path = os.path.join(directory, '.plexignore')
    lines = plexignore_lines()
    if os.path.isfile(path):
        existing = Path(path).read_text(encoding='utf-8', errors='replace')
        missing = []
        for line in lines:
            if not re.search(r'(?m)^\s*' + re.escape(line) + r'\s*$', existing):
                missing.append(line)
        if not missing:
            return
        with open(path, 'a', encoding='utf-8', newline='') as handle:
            handle.write('\n' + '\n'.join(missing) + '\n')
        return
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def owned_plexignore_text(text: str) -> bool:
    if text is None:
        return False
    clean = text.lstrip('\ufeff')
    kept = [line.strip() for line in re.split(r'\r\n|\n|\r', clean) if line.strip()]
    owned = plexignore_lines()
    return kept == owned


def remove_plexignore(directory: str, book=None) -> None:
    hold = os.path.join(directory, '.vd-originals')
    if os.path.isdir(hold):
        return
    path = os.path.join(directory, '.plexignore')
    if not os.path.isfile(path):
        return
    owned = plexignore_lines()
    raw_lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    kept = []
    for line in raw_lines:
        drop = any(re.match(r'^\s*' + re.escape(name) + r'\s*$', line) for name in owned)
        if not drop:
            kept.append(line)
    meaningful = [line for line in kept if line.strip()]
    if meaningful:
        Path(path).write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
        return
    length = os.path.getsize(path)
    os.remove(path)
    if not os.path.exists(path):
        from .recycle import remove_recycled_plexignore
        remove_recycled_plexignore(path, length, book)


def replace_plan(source: str) -> dict:
    directory = os.path.dirname(source)
    base, ext = os.path.splitext(os.path.basename(source))
    hold_dir = os.path.join(directory, '.vd-originals')
    final = os.path.join(directory, base + '.mkv')
    temp = os.path.join(hold_dir, base + '.vd.tmp.mkv')
    hold = os.path.join(hold_dir, os.path.basename(source))
    if os.path.exists(hold):
        stamp = __import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')
        hold = os.path.join(hold_dir, base + '.' + stamp + ext)
    same_final = os.path.normcase(os.path.abspath(final)) == os.path.normcase(os.path.abspath(source))
    return {
        'source': os.path.abspath(source),
        'hold_dir': hold_dir,
        'temp': temp,
        'final': final,
        'hold': hold,
        'same_final': same_final,
        'blocked': (not same_final) and os.path.exists(final),
    }


def _move(src: str, dst: str) -> None:
    os.replace(src, dst)


def replace_with_encode(source: str, temp_file: str, delete_original: bool, book=None) -> dict:
    if not os.path.isfile(source):
        raise RuntimeError(f'The original is missing: {source}')
    if not os.path.isfile(temp_file):
        raise RuntimeError(f'The new file is missing: {temp_file}')
    source_size = os.path.getsize(source)
    temp_size = os.path.getsize(temp_file)
    if temp_size >= source_size:
        raise RuntimeError('The new file is not smaller than the original.')
    if temp_size < MIN_KEEP_BYTES:
        raise RuntimeError('The new file is too small to keep.')
    plan = replace_plan(source)
    if plan['blocked']:
        raise RuntimeError(f"A file already has the new name: {plan['final']}")
    os.makedirs(plan['hold_dir'], exist_ok=True)
    add_plexignore(os.path.dirname(source))
    if os.path.exists(plan['hold']):
        raise RuntimeError(f"A held original already exists: {plan['hold']}")
    _move(source, plan['hold'])
    try:
        _move(temp_file, plan['final'])
    except Exception:
        if os.path.exists(plan['hold']) and not os.path.exists(source):
            _move(plan['hold'], source)
        raise
    final_size = os.path.getsize(plan['final'])
    if final_size < MIN_KEEP_BYTES:
        if os.path.exists(plan['final']):
            os.remove(plan['final'])
        if not os.path.exists(source) and os.path.exists(plan['hold']):
            _move(plan['hold'], source)
        raise RuntimeError('The new file was too small after it was moved into place.')
    kept = plan['hold']
    deleted = False
    if delete_original:
        hold_length = os.path.getsize(plan['hold'])
        os.remove(plan['hold'])
        kept = None
        deleted = True
        if not os.path.exists(plan['hold']):
            from .recycle import remove_recycle_copy
            remove_recycle_copy(plan['hold'], hold_length, book)
        remove_empty_directory(plan['hold_dir'], book)
    return {
        'new_path': plan['final'],
        'kept_path': kept,
        'deleted': deleted,
        'new_bytes': final_size,
        'old_bytes': source_size,
    }


def _holding_path(path: str) -> bool:
    parts = re.split(r'[\\/]', os.path.abspath(path))
    return any(part.casefold() == '.vd-originals' for part in parts[:-1])


def undo_replaced_file(source: str, kept_path: str, new_path: str, book=None) -> bool:
    kept = os.path.abspath(kept_path)
    new = os.path.abspath(new_path)
    source = os.path.abspath(source)
    if not _holding_path(kept):
        raise RuntimeError(f'Refusing to restore a file outside .vd-originals: {kept}')
    if not os.path.isfile(kept):
        raise RuntimeError(f'The held original is missing: {kept}')
    source_dir = os.path.dirname(source)
    os.makedirs(source_dir, exist_ok=True)
    new_exists = os.path.exists(new)
    if os.path.exists(source) and os.path.normcase(source) != os.path.normcase(new):
        raise RuntimeError(f'Something else is already at the original place: {source}')
    if not new_exists:
        if os.path.exists(source):
            raise RuntimeError(f'The new file is missing and the original place is occupied: {source}')
        _move(kept, source)
        remove_empty_directory(os.path.dirname(kept), book)
        return True
    hold_dir = os.path.dirname(kept)
    aside = os.path.join(hold_dir, '.vd-undo-' + os.urandom(8).hex() + '.mkv')
    _move(new, aside)
    try:
        if os.path.exists(source):
            raise RuntimeError(f'The original place is still occupied: {source}')
        _move(kept, source)
    except Exception:
        if os.path.exists(aside) and not os.path.exists(new):
            _move(aside, new)
        raise
    aside_length = os.path.getsize(aside)
    os.remove(aside)
    if not os.path.exists(aside):
        from .recycle import remove_recycle_copy
        remove_recycle_copy(aside, aside_length, book)
    remove_empty_directory(hold_dir, book)
    return True


def remove_kept_original(kept_path: str, new_path: str = '', book=None) -> bool:
    full = os.path.abspath(kept_path)
    if not _holding_path(full):
        raise RuntimeError(f'Refusing to delete a file outside .vd-originals: {full}')
    if new_path and not os.path.exists(new_path):
        raise RuntimeError(f'The new file is missing, so the original stays: {new_path}')
    if not os.path.isfile(full):
        return False
    length = os.path.getsize(full)
    os.remove(full)
    if not os.path.exists(full):
        from .recycle import remove_recycle_copy
        remove_recycle_copy(full, length, book)
    remove_empty_directory(os.path.dirname(full), book)
    return True


def remove_empty_directory(directory: str, book=None) -> None:
    if not directory or not os.path.isdir(directory):
        return
    remove_stale_busy_markers(directory)
    try:
        if os.listdir(directory):
            return
        os.rmdir(directory)
    except OSError:
        return
    if os.path.exists(directory):
        return
    leaf = os.path.basename(directory.rstrip('\\/'))
    if leaf != '.vd-originals':
        return
    parent = os.path.dirname(directory)
    if parent:
        remove_plexignore(parent, book)
    from .recycle import remove_recycled_holding_folder
    remove_recycled_holding_folder(directory, book)


def compare_arguments(left: str, right: str, start_sec: float = 0, frame_count: int = 0, output: str = 'pipe:1') -> list:
    width = COMPARE_HALF_WIDTH
    height = COMPARE_HALF_HEIGHT
    filt = (
        f'[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,'
        f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[a];'
        f'[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,'
        f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[b];'
        f'[a][b]hstack=inputs=2[v]'
    )
    start = '0' if start_sec <= 0 else f'{start_sec:.3f}'.rstrip('0').rstrip('.')
    arguments = [
        '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-ss', start, '-i', left, '-ss', start, '-i', right,
        '-filter_complex', filt, '-map', '[v]', '-an', '-r', str(COMPARE_FPS),
    ]
    if frame_count > 0:
        arguments += ['-frames:v', str(frame_count)]
    arguments += ['-f', 'rawvideo', '-pix_fmt', 'bgr24', output]
    return arguments


def compare_frame_info() -> dict:
    width = COMPARE_HALF_WIDTH * 2
    height = COMPARE_HALF_HEIGHT
    return {'width': width, 'height': height, 'fps': COMPARE_FPS, 'bytes': width * height * 3}


def remembered_skip(rows, path: str, size_bytes: int, target: int) -> bool:
    if not path or size_bytes <= 0 or target <= 0 or not rows:
        return False
    for row in rows:
        if not row or not row.get('path'):
            continue
        if os.path.normcase(str(row['path'])) != os.path.normcase(path):
            continue
        try:
            saved_size = int(row.get('size') or 0)
            saved_target = int(row.get('targetKbps') or 0)
        except (TypeError, ValueError):
            continue
        if saved_size == size_bytes and saved_target > 0 and target >= saved_target:
            return True
    return False


def merge_skip_row(rows, path: str, size_bytes: int, target: int) -> list:
    kept = []
    found = False
    for row in rows or []:
        if not row or not row.get('path'):
            continue
        if os.path.normcase(str(row['path'])) != os.path.normcase(path):
            kept.append(row)
            continue
        found = True
        try:
            saved_size = int(row.get('size') or 0)
            saved_target = int(row.get('targetKbps') or 0)
        except (TypeError, ValueError):
            saved_size = 0
            saved_target = 0
        use_target = target
        if saved_size == size_bytes and 0 < saved_target < use_target:
            use_target = saved_target
        kept.append({'path': path, 'size': size_bytes, 'targetKbps': use_target})
    if not found and size_bytes > 0 and target > 0:
        kept.append({'path': path, 'size': size_bytes, 'targetKbps': target})
    return kept


def remove_skip_row(rows, path: str) -> list:
    if not path:
        return [row for row in (rows or []) if row and row.get('path')]
    wanted = os.path.normcase(path)
    return [row for row in (rows or []) if row and row.get('path') and os.path.normcase(str(row['path'])) != wanted]


def version_numbers(text: str):
    clean = (text or '').strip()
    if clean[:1].lower() == 'v':
        clean = clean[1:]
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?', clean)
    if not match:
        return None
    numbers = [int(match.group(1)), int(match.group(2)), int(match.group(3))]
    if match.group(4) is not None:
        numbers.append(int(match.group(4)))
    return numbers


def linux_release_is_update(tag: str, current: str, asset) -> bool:
    newer = compare_versions(tag, current)
    if newer is None or newer <= 0:
        return False
    if not isinstance(asset, dict):
        return False
    name = str(asset.get('name') or '').lower()
    url = str(asset.get('url') or '')
    return 'linux' in name and name.endswith(('.tar.gz', '.tar.xz', '.run')) and bool(url)


def compare_versions(left: str, right: str):
    a = version_numbers(left)
    b = version_numbers(right)
    if a is None or b is None:
        return None
    count = max(len(a), len(b))
    for index in range(count):
        left_number = a[index] if index < len(a) else 0
        right_number = b[index] if index < len(b) else 0
        if left_number > right_number:
            return 1
        if left_number < right_number:
            return -1
    return 0


def app_version() -> str:
    path = project_root() / 'version.txt'
    if path.is_file():
        text = path.read_text(encoding='utf-8').strip()
        if re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', text):
            return text
    from . import __version__
    return __version__


def run_lines(command, on_line=None, cancel=None) -> tuple:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
    )
    lines = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip('\r\n')
            lines.append(text)
            if on_line:
                on_line(text)
            if cancel is not None and cancel.is_set():
                proc.terminate()
                break
        code = proc.wait(timeout=30)
    except Exception:
        proc.kill()
        code = proc.wait(timeout=10)
    return code, '\n'.join(lines)
