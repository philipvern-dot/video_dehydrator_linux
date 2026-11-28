"""Engine checks. Run: python -m video_dehydrator.selftest"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

from .engine import (
    Probe, app_version, apply_encode_snapshot, batch_count_text, batch_wall_seconds, bloat_decision, budget_gib_per_hour, compare_versions,
    encode_row_snapshot,
    add_hold_busy_marker, clean_unfinished_encodes, delete_targets, encode_command, encode_output_paths,
    handbrake_arguments, hold_busy_path, is_leftover_encode_process, unfinished_encode_name,
    recovery_note,
    FIND_LIMITS, job_count, linux_release_is_update, linux_update_offer, output_path_missing, pass_two_did_not_start,
    CPU_TARGETS, EncodeAdmission, cpu_target_percent, dynamic_lane_count, encoder_threads,
    safe_core_budget, split_core_budget, system_reserve, target_core_budget,
    remove_empty_directory, remove_hold_busy_marker, scan_limit_reached,
    ac3_frame_size, ac3_short_tail, copied_ac3, estimated_savings_bytes,
    encode_rate, force_convertible, format_force_report, format_over_ratio, format_remaining_time,
    lane_bar_text, lane_pass_name,
    median_sample,
    FAULT_ISSUE_URL, fault_issue_request, format_fault_report, useful_handbrake_lines,
    harmless_decode_note, make_fault_record, merge_fault, merge_skip_row, over_band_id, remaining_estimate,
    audio_samples_per_packet, duration_basis, frame_seconds, job_view_height, packet_span_sec, presentation_count,
    space_saved_bytes, trailing_ac3_note, over_ratio, owned_plexignore_text,
    plexignore_lines, remembered_skip, set_budget_picks, size_skip_reason, target_kbps,
)
from .helptext import SECTIONS
from . import recycle as recycle_mod
from .recycle import (
    directory_is_empty, make_plan, purge_enabled, purge_file, purge_plexignore,
    remove_recycle_copy, remove_verified_empty, set_purge_enabled, stale_child,
)
from .store import clean_column_widths, flatten_rows


def check(condition, message):
    if not condition:
        raise SystemExit(message)


def fake_probe(**kwargs) -> Probe:
    values = dict(
        width=1920, height=1080, size_bytes=0, duration_sec=3600.0,
        video_bitrate=0, already_efficient=False, is_dolby_vision=False,
    )
    values.update(kwargs)
    return Probe(**values)


def test_rules():
    set_budget_picks('')
    check(target_kbps(1920, 1080) == 3000, '1080p target')
    check(target_kbps(3840, 2160) == 12000, '4K target')
    check(target_kbps(1280, 720) == 1333, '720p target')
    check(target_kbps(720, 480) == 500, '480p target')
    check(target_kbps(1920, 800) == 2222, 'scope target')
    check(target_kbps(4096, 2160) == 12800, 'DCI 4K target')
    check(target_kbps(160, 120) == 200, 'tiny picture floor')
    check(abs(budget_gib_per_hour(1920, 1080, False) - 2.0) < 0.0001, '1080p budget')
    check(abs(budget_gib_per_hour(1920, 1080, True) - 3.0) < 0.0001, '1080p HEVC line')
    check(abs(budget_gib_per_hour(3840, 2160, False) - 8.0) < 0.0001, '4K budget')
    check(over_ratio(None, 2) is None, 'missing rate has no overage')
    check(over_ratio(4, 0) is None, 'zero budget has no overage')
    check(abs(over_ratio(4, 2) - 2) < 0.0001, 'twice the budget')
    check(over_band_id(1.2) == 'slight', 'barely over is slight')
    check(over_band_id(1.49) == 'slight', 'under 1.5x is slight')
    check(over_band_id(1.5) == 'heavy', '1.5x is heavy')
    check(over_band_id(2.9) == 'heavy', 'under 3x is heavy')
    check(over_band_id(3) == 'extreme', '3x is extreme')
    check(over_band_id(0.8) == '', 'under budget is not a band')
    check(format_over_ratio(2) == '2.00x', 'over format')
    check(format_over_ratio(1.2) == '1.20x', 'ratio keeps two decimals')
    check(format_over_ratio(1.004) == '1.004x', 'a ratio just over 1.00 keeps another digit')
    check(format_over_ratio(None) == '', 'blank over format')
    shrink = bloat_decision(fake_probe(size_bytes=int(1.5 * (1024 ** 3)), video_bitrate=5_000_000))
    check(shrink['decision'] == 'shrink', 'a picture still above the target is can shrink')
    check(force_convertible(shrink), 'a can-shrink file can be forced')
    check(not force_convertible({'decision': 'dv', 'target_kbps': 3000}), 'Dolby Vision cannot be forced')
    check(estimated_savings_bytes(1000, 400, None) == 600, 'savings before convert')
    check(estimated_savings_bytes(1000, 400, 1000) is None, 'savings clear after convert')
    check(space_saved_bytes(400, 1000) == 600, 'space saved after convert')
    check(format_remaining_time(40) == 'about 40 sec', 'short remaining time')
    check(median_sample([10, 40, 12]) == 12, 'a spike does not move the median')
    check(abs(encode_rate(4, [2], 0.05) - 2.5) < 0.001, 'an early live rate is a quarter of the blend')
    check(abs(encode_rate(4, [2], 0.5) - 3) < 0.001, 'a later live rate shares the blend')
    check(encode_rate(0, [2, 4]) == 3, 'no live rate uses the finished average')
    check(encode_rate(4, []) == 4, 'no finished rate uses the live rate')
    first = remaining_estimate({'task': 1, 'tasks': 2, 'eta_seconds': 40, 'part': 10}, waiting=[])
    check(first['file_kind'] == 'pass' and 'first pass' in first['text'] and 'estimating' in first['text'], 'first pass has no batch estimate')
    second = remaining_estimate({'task': 2, 'tasks': 2, 'eta_seconds': 120, 'part': 50}, waiting=[])
    check(second['have_batch'] and second['file_seconds'] == 150, 'second pass adds the check cushion')
    report = format_force_report([
        {'name': 'a.mkv', 'can_convert': True, 'size': 1000, 'estimate': 400, 'estimate_rough': False, 'reason': ''},
        {'name': 'b.mkv', 'can_convert': False, 'reason': 'Dolby Vision is left alone.', 'size': 0, 'estimate': None},
    ])
    check('1 file can be converted.' in report and '1 file will be left alone.' in report, 'force report')
    set_purge_enabled(False)
    check(not purge_enabled(), 'purge stayed on')
    check(remove_recycle_copy('/tmp/library/.vd-originals/Episode.avi', 10) is None, 'a turned-off recycle purge still ran')
    set_purge_enabled(None)
    check(purge_enabled(), 'recycle purge was off with no choice stored')
    bloated = bloat_decision(fake_probe(size_bytes=3 * (1024 ** 3), video_bitrate=15000000))
    check(bloated['decision'] == 'bloated' and bloated['target_kbps'] == 3000, '1080p over budget is bloated')
    lean = bloat_decision(fake_probe(size_bytes=int(1.5 * (1024 ** 3)), video_bitrate=2500000))
    check(lean['decision'] == 'lean', '1080p under budget is lean')
    hevc = bloat_decision(fake_probe(size_bytes=int(2.5 * (1024 ** 3)), video_bitrate=2500000, already_efficient=True))
    check(hevc['decision'] == 'lean', 'HEVC under its line is lean')
    dv = bloat_decision(fake_probe(size_bytes=5 * (1024 ** 3), is_dolby_vision=True))
    check(dv['decision'] == 'dv', 'Dolby Vision is left alone')
    cartoon = bloat_decision(fake_probe(
        width=354, height=264, size_bytes=18029798, duration_sec=434.654,
        video_bitrate=0, already_efficient=True, audio_count=1,
    ))
    check(cartoon['decision'] == 'within' and cartoon['target_kbps'] == 200, 'an efficient cartoon with no picture rate stays off the list')
    old_cartoon = bloat_decision(fake_probe(
        width=354, height=264, size_bytes=18029798, duration_sec=434.654,
        video_bitrate=0, already_efficient=False, audio_count=1,
    ))
    check(old_cartoon['decision'] == 'bloated', 'the same cartoon in an older codec is still listed')
    fat_hevc = bloat_decision(fake_probe(
        width=354, height=264, size_bytes=80_000_000, duration_sec=434.654,
        video_bitrate=0, already_efficient=True, audio_count=1,
    ))
    check(fat_hevc['decision'] == 'bloated', 'a fat efficient file with no picture rate is still listed')
    check(harmless_decode_note('Application provided invalid, non monotonically increasing dts to muxer in stream 0: 10 >= 10'), 'equal dts is harmless')
    check(not harmless_decode_note('Application provided invalid, non monotonically increasing dts to muxer in stream 0: 11 >= 10'), 'a backwards timestamp fails')
    backstep = '[mp3float @ 0x25992d40] invalid new backstep -1'
    check(harmless_decode_note(backstep), 'a recovered MP3 backstep warning is harmless')
    check(harmless_decode_note(backstep + ' | Application provided invalid, non monotonically increasing dts to muxer in stream 0: 10 >= 10'), 'backstep plus an equal timestamp is harmless')
    check(not harmless_decode_note(backstep + ' | [mp3float @ 0x25992d40] Header missing'), 'a missing MP3 header stays a failure')
    check(not harmless_decode_note('[mp3float @ 0x25992d40] invalid old backstep -1'), 'an old backstep warning stays a failure')
    check(not harmless_decode_note('[mp3float @ 0x25992d40] invalid new backstep 4'), 'a positive backstep value stays a failure')
    check(not harmless_decode_note(backstep + ' | [mpeg4 @ 00000001] error while decoding'), 'a picture error stays a failure beside the MP3 note')
    aac_noise = '[aac @ 0xf8f3180] noise_facs_q 255 is invalid'
    aac_env = '[aac @ 0x46d12c00] env_facs_q 254 is invalid'
    check(harmless_decode_note(aac_noise), 'a recovered HE-AAC noise factor warning is harmless')
    check(harmless_decode_note(aac_env + ' | Last message repeated 3 times'), 'an HE-AAC envelope warning and its repeat are harmless')
    check(harmless_decode_note(aac_noise + ' | ' + aac_env + ' | Application provided invalid, non monotonically increasing dts to muxer in stream 0: 210 >= 210'), 'HE-AAC factor warnings plus an equal timestamp are harmless')
    check(not harmless_decode_note(aac_noise + ' | [mpeg4 @ 00000001] error while decoding'), 'a picture error stays a failure beside the HE-AAC note')
    check(not harmless_decode_note('[aac @ 0xf8f3180] Prediction is not allowed in AAC-LC'), 'another AAC error stays a failure')
    check(not harmless_decode_note('Last message repeated 3 times | [h264 @ 00000001] error while decoding'), 'a repeated line does not hide a picture error')
    notes = useful_handbrake_lines('Encoding: task 1 of 2, 10.00 %\n[error] Could not open the source\nMuxing: 50 %')
    check(notes == ['[error] Could not open the source'], 'a fault report keeps the HandBrake error and drops the percent lines')
    fault = {
        'when': '2026-11-11T16:42:27',
        'kind': 'check',
        'source': '/mnt/unified-media/Movies/The Stingiest Man in Town (1978).mp4',
        'name': 'The Stingiest Man in Town (1978).mp4',
        'reason': 'duration changed from 2993.8s to 2985.6s',
        'size_bytes': 427543074,
        'modified': '2026-11-01T12:00:00',
        'kbps': 500,
        'jobs': 4,
        'reencode_audio': False,
        'auto_delete': True,
        'handbrake_code': 0,
        'command': "HandBrakeCLI -i '/mnt/unified-media/Movies/The Stingiest Man in Town (1978).mp4'",
        'old_bytes': 427543074,
        'new_bytes': 236000000,
        'old_gibph': 0.479,
        'new_gibph': 0.265,
        'source_probe': {'video_codec': 'h264', 'width': 720, 'height': 480, 'pix_fmt': 'yuv420p', 'duration_sec': 2993.791, 'audio': ['aac'], 'subtitles': 0, 'video_bitrate': 1000000},
        'output_probe': {'video_codec': 'hevc', 'width': 720, 'height': 480, 'pix_fmt': 'yuv420p', 'duration_sec': 2985.6, 'audio': ['aac'], 'subtitles': 0, 'video_bitrate': 500000},
        'media': 'stream|codec_name=h264|nb_frames=89724|duration=2993.790800',
        'log': ['Encode done'],
    }
    report = format_fault_report([fault], '1.0.10', 'Ubuntu 26.04')
    check('App version: 1.0.10' in report, 'a fault report names the app version')
    check('The Stingiest Man in Town (1978).mp4' in report and 'duration changed from 2993.8s to 2985.6s' in report, 'a fault report names the file and the failure')
    check('nb_frames=89724' in report and 'HandBrakeCLI' in report and 'h264 720x480' in report, 'a fault report includes the probe and the command')
    check('Original file was left in place' in report or 'original file was left in place' in report, 'a fault report says the original stayed')
    issue = fault_issue_request([fault], '1.0.10', 'Ubuntu 26.04')
    check(issue['url'].startswith(FAULT_ISSUE_URL + '?title='), 'a fault report opens a Linux GitHub issue')
    check('body=' in issue['url'] and 'duration%20changed' in issue['url'], 'the issue link carries the failure')
    fat = dict(fault)
    fat['log'] = ['note ' + str(index) + (' x' * 80) for index in range(400)]
    short = fault_issue_request([fat], '1.0.10', 'Ubuntu 26.04', limit=2500)
    check(len(short['url']) <= 2500 and short['trimmed'], 'a long HandBrake log shrinks to fit the link')
    check('duration changed from 2993.8s to 2985.6s' in short['body'], 'a shortened report still has the failure')
    again = dict(fault)
    again['reason'] = 'HandBrake stopped (3).'
    merged = merge_fault([fault], again)
    check(len(merged) == 1 and merged[0]['reason'] == 'HandBrake stopped (3).', 'a later failure replaces the same file')
    made = make_fault_record('check', '/tmp/does-not-exist.mkv', 'duration changed from 10s to 3s', kbps=200, jobs=1)
    check(made['kind'] == 'check' and made['reason'].startswith('duration changed') and made['name'] == 'does-not-exist.mkv', 'a fault record keeps the kind and the file name')
    ac3_tail = 'decode Last exit=-1094995529 [ac3 @ 00000200d45dad40] incomplete frame | [aist#0:1/ac3 @ 00000200d2731900] [dec:ac3 @ 00000200d45b9b40] corrupt decoded frame | [aist#0:1/ac3 @ 00000200d2731900] [dec:ac3 @ 00000200d45b9b40] Error processing packet in decoder: Invalid data found when processing input | [aist#0:1/ac3 @ 00000200d2731900] [dec:ac3 @ 00000200d45b9b40] Task finished with error code: -1094995529 (Invalid data found when processing input) | [aist#0:1/ac3 @ 00000200d2731900] [dec:ac3 @ 00000200d45b9b40] Terminating thread with return code -1094995529 (Invalid data found when processing input)'
    check(trailing_ac3_note(ac3_tail), 'one trailing incomplete AC3 frame is the allowed note')
    same_stamp = '[null @ 0000028ef7cc3f80] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 10 >= 10 | [null @ 0000028ef7cc3f80] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 18 >= 18'
    ac3_prefix = 'decode Last exit=-1094995529 '
    mixed_tail = ac3_prefix + same_stamp + ' | ' + ac3_tail[len(ac3_prefix):]
    check(trailing_ac3_note(mixed_tail), 'equal timestamps plus one short AC3 frame are allowed')
    check(not harmless_decode_note(mixed_tail), 'the mixed note is not timestamps alone')
    check(not trailing_ac3_note(mixed_tail.replace('10 >= 10', '10 >= 4', 1)), 'a backwards timestamp stays a failure beside the AC3 tail')
    check(not trailing_ac3_note(ac3_tail + ' | [mpeg4 @ 00000001] error while decoding'), 'a picture error stays a failure')
    check(not trailing_ac3_note(ac3_tail.replace('decode Last', 'decode First')), 'the first window does not use the tail exception')
    frame = bytearray(768)
    frame[0] = 0x0B
    frame[1] = 0x77
    frame[4] = 20
    check(ac3_frame_size(bytes(frame), 0) == 768, '192 kbps 48 kHz AC3 frame is 768 bytes')
    whole = bytes(frame) + bytes(frame)
    check(not ac3_short_tail(whole), 'a complete AC3 tail is not short')
    short = bytearray(whole)
    short.extend(b'\x00' * 288)
    short[1536] = 0x0B
    short[1537] = 0x77
    short[1540] = 20
    check(ac3_short_tail(bytes(short)), 'a copied AC3 tail that ends mid-frame is short')
    check(copied_ac3(['ac3'], ['ac3']), 'copied AC3 stays AC3')
    check(not copied_ac3(['ac3'], ['aac']), 'AC3 re-encoded to AAC is not the tail exception')
    check(size_skip_reason('the new file is not smaller; the new file is not smaller per hour'), 'size skip')
    check(not size_skip_reason('the new file is not smaller; audio could not be copied'), 'mixed failure is not a size skip')
    check(compare_versions('1.0.5.1', 'v1.0.5.1') == 0, 'version prefix')
    check(compare_versions('1.0.5', '1.0.5.1') == -1, 'shorter version is older')
    check(not linux_release_is_update('v1.0.8', '1.0.7.4', None), 'a Windows-only release is not a Linux update')
    check(linux_release_is_update('v1.0.8', '1.0.7.4', {'name': 'video_dehydrator_linux.tar.gz', 'url': 'https://example.com/video_dehydrator_linux.tar.gz'}), 'a Linux archive is an update')
    check(compare_versions('v1.0.9.6', '1.0.9.5') == 1, '1.0.9.6 is newer than 1.0.9.5')
    check(compare_versions('1.0.10', '1.0.9.15') == 1, '1.0.10 is newer than 1.0.9.15')
    release = {
        'tag_name': 'v1.0.9.6',
        'assets': [{
            'name': 'video_dehydrator_linux.tar.gz',
            'size': 80000,
            'browser_download_url': 'https://github.com/philipvern-dot/video_dehydrator_linux/releases/download/v1.0.9.6/video_dehydrator_linux.tar.gz',
        }],
    }
    offer = linux_update_offer(release, '1.0.9.5')
    check(offer and offer['tag'] == '1.0.9.6', 'check for updates offers 1.0.9.6')
    check(offer['asset']['name'] == 'video_dehydrator_linux.tar.gz', 'the update file is the Linux archive')
    check(offer['asset']['url'].startswith('https://github.com/philipvern-dot/video_dehydrator_linux/releases/download/'), 'the update downloads from the Linux release')
    check(linux_update_offer(release, '1.0.9.6') is None, 'the same version is not an update')
    check(linux_update_offer(release, '1.0.9.7') is None, 'an older release is not an update')
    windows_only = {
        'tag_name': 'v1.0.9.6',
        'assets': [{
            'name': 'video_dehydrator.exe',
            'size': 10,
            'browser_download_url': 'https://github.com/philipvern-dot/video_dehydrator_linux/releases/download/v1.0.9.6/video_dehydrator.exe',
        }],
    }
    check(linux_update_offer(windows_only, '1.0.9.5') is None, 'a Windows installer is not a Linux update')
    elsewhere = {
        'tag_name': 'v1.0.9.6',
        'assets': [{
            'name': 'video_dehydrator_linux.tar.gz',
            'size': 10,
            'browser_download_url': 'https://example.com/video_dehydrator_linux.tar.gz',
        }],
    }
    check(linux_update_offer(elsewhere, '1.0.9.5') is None, 'an archive from another site is not installed')
    check(clean_column_widths({'File': 220, 'Size': 'wide', 'Tiny': 10}) == {'File': 220}, 'column widths keep real sizes')
    check(app_version() == '1.1.3.3', 'app version')
    window_row = {'source': '/shows/Episode.mkv', 'path': '/shows/Episode.mkv', 'status': 'Bloated', 'audio': ['aac']}
    worker_row = encode_row_snapshot(window_row)
    worker_row['status'] = 'Converting'
    worker_row['audio'].append('ac3')
    check(window_row['status'] == 'Bloated' and window_row['audio'] == ['aac'], 'an encode copy leaves the window row alone')
    posted = encode_row_snapshot(worker_row)
    worker_row['status'] = 'Original deleted'
    check(posted['status'] == 'Converting', 'a posted row stays at the moment it was posted')
    check(apply_encode_snapshot([window_row], posted), 'the window finds the posted file')
    check(window_row['status'] == 'Converting' and window_row['audio'] == ['aac', 'ac3'], 'the window applies the posted row')
    check(not apply_encode_snapshot([window_row], {'status': 'Failed'}), 'a posted row needs a source')
    check(batch_count_text(4, 100) == '4/100', 'batch count')
    check(batch_count_text(5, 100) == '5/100', 'batch count moves to the next file')
    check(batch_count_text(0, 100) == '', 'batch count waits until a file starts')
    check(batch_count_text(120, 100) == '100/100', 'batch count stays within the batch')
    guide = '\n'.join(body for _title, body in SECTIONS)
    check('4/100 means this batch has started file 4 of 100' in guide, 'guide batch count')
    check('Faulty source file stays off until a file fails' in guide, 'guide fault button')
    check('Drag a column edge to change its width' in guide, 'guide column width')
    check('each file has its own bar' in guide, 'guide per-file bars')
    check('already an efficient encode' in guide, 'guide leaves efficient files that will not shrink')
    check(lane_pass_name(1) == 'first pass', 'first pass name')
    check(lane_pass_name(2) == 'pass 2', 'second pass name')
    check(lane_pass_name(0, True) == 'checking', 'checking pass name')
    check(lane_bar_text('Episode.mkv', 42.4, 'first pass') == 'Episode.mkv   42%   first pass', 'lane bar text')
    check('opens a terminal, and runs its installer' in guide, 'guide update terminal')
    check(job_count('2') == 2 and job_count(1) == 1 and job_count('no') == 1, 'job count')
    check(job_count(6) == 6 and job_count(9) == 6 and job_count(0) == 1, 'job count stays between 1 and 6')
    check(CPU_TARGETS == (25, 50, 75, 95, 100), 'cpu use choices')
    check(cpu_target_percent('75') == 75 and cpu_target_percent('no') == 100 and cpu_target_percent(40) == 100, 'cpu use stays on a listed percent')
    check(encoder_threads(528, 400, 16) == 3 and encoder_threads(720, 480, 16) == 3, 'sd pictures use 3 threads')
    check(encoder_threads(1280, 720, 16) == 4, '720p uses 4 threads')
    check(encoder_threads(1920, 1080, 16) == 8 and encoder_threads(3840, 2160, 16) == 8, '1080p and 4k use 8 threads')
    check(encoder_threads(0, 0, 16) == 8 and encoder_threads(None, None, 4) == 4, 'a missing size stays within 8 and the node')
    check(encoder_threads(1920, 1080, 4) == 4 and encoder_threads(-1, 10, 16) == 8, 'thread count stays positive and inside the node')
    check(system_reserve(32) == 2 and system_reserve(4) == 1 and system_reserve(1) == 0, '100% leaves cores for the system')
    check(safe_core_budget(32) == 30 and target_core_budget(32, 100) == 30, '100% on 32 cores uses 30')
    check(target_core_budget(32, 95) == 28 and target_core_budget(32, 75) == 22, 'lower cpu use is a share of the safe budget')
    check(target_core_budget(32, 50) == 15 and target_core_budget(32, 25) == 7, 'half and quarter cpu use')
    check(split_core_budget([16, 16], 30) == [15, 15], 'a 30 core budget splits across two processors')
    check(dynamic_lane_count(100, 30) == 10 and dynamic_lane_count(1, 30) == 1 and dynamic_lane_count(4, 30) == 4, 'lane count follows the budget and the batch')
    admission = EncodeAdmission([32])
    for index in range(10):
        check(admission.try_admit(index, 3) is not None, 'ten small files fit in 32 cores')
    check(sum(admission.free) == 2, 'ten small files leave 2 cores')
    waiting = [('big', 8), ('later', 3)]
    check(admission.try_admit(waiting[0][0], waiting[0][1]) is None, 'a large file waits when 8 cores are not free')
    check('later' not in admission.running, 'a later small file does not pass a waiting large file')
    admission.release(0)
    admission.release(1)
    check(admission.try_admit('big', 8) is not None, 'the large file starts once 8 cores are free')
    check('later' not in admission.running, 'the file behind the large one is still waiting')
    check('Dynamic encode batch jobs' in guide and 'CPU use' in guide and 'leaves one or two cores free' in guide, 'guide explains dynamic batch and cpu use')
    check(scan_limit_reached(4, 5) is False and scan_limit_reached(5, 5) and scan_limit_reached(9, 0) is False, 'scan limit')
    check(FIND_LIMITS == (5, 10, 25, 50, 100, 500, 1000), 'scan limits include 500 and 1000')
    check(scan_limit_reached(500, 500) and not scan_limit_reached(499, 500), '500 scan limit')
    check(scan_limit_reached(1000, 1000) and not scan_limit_reached(999, 1000), '1000 scan limit')
    check('100, 500, or 1000' in guide, 'guide scan limits')
    check(output_path_missing('ERROR: avio_open2 failed, errno -2: Could not write'), 'a missing output folder was missed')
    check(not output_path_missing('encoded 0 frames\nEncoding: task 2 of 2, 0.00 %'), 'a failed second pass looked like a missing folder')
    check(pass_two_did_not_start('encoded 0 frames\nEncoding: task 2 of 2, 0.00 %'), 'a second pass that never started was missed')
    sample = handbrake_arguments('in.mkv', 'out.mkv', None, 3000, 'x265', False, 8, True)
    check('--no-multi-pass' in sample and '--multi-pass' not in sample, 'one pass dropped the second pass')
    ready = {'checked': False, 'status': 'Ready to delete', 'kept': '/tmp/old.mkv', 'name': 'old.mkv'}
    other = {'checked': True, 'status': 'Ready to delete', 'kept': '/tmp/b.mkv', 'name': 'b.mkv'}
    check(delete_targets([ready], ready) == [ready], 'a highlighted original can be deleted')
    check(delete_targets([ready, other], ready) == [other], 'checked originals come before the highlight')
    check(delete_targets([{'checked': True, 'status': 'Bloated', 'kept': ''}], None) == [], 'a bloated row is not deleted')
    check(batch_wall_seconds([100, 50], 30, 2) == 90, 'two jobs share the remaining time')
    check(encode_command('HandBrakeCLI', ['-i', 'a'], 1, 0, 2, True) == ['HandBrakeCLI', '-i', 'a'], 'one job is not pinned')
    check(encode_command('HandBrakeCLI', ['-i', 'a'], 2, 1, 2, True)[0] == 'numactl', 'two jobs can pin a processor')
    check('--cpunodebind=1' in encode_command('HandBrakeCLI', ['-i', 'a'], 6, 3, 2, True), 'a later job stays on a processor')
    busy_root = tempfile.mkdtemp(prefix='vd-busy-')
    try:
        show = os.path.join(busy_root, 'Season')
        os.makedirs(show)
        plan = {'source': os.path.join(show, 'Episode.mkv'), 'hold_dir': os.path.join(show, '.vd-originals'), 'temp': os.path.join(show, '.vd-originals', 'Episode.vd.tmp.mkv')}
        marker = add_hold_busy_marker(plan)
        check(os.path.basename(marker) == 'Episode.vd.busy', 'busy marker name')
        check(hold_busy_path(plan) == marker, 'busy marker path')
        other = os.path.join(plan['hold_dir'], 'Finished.mkv')
        with open(other, 'w', encoding='utf-8') as handle:
            handle.write('kept')
        os.remove(other)
        remove_empty_directory(plan['hold_dir'])
        check(os.path.isdir(plan['hold_dir']), 'a live encode marker let the holding folder be removed')
        with open(os.path.join(plan['hold_dir'], 'Old.vd.busy'), 'w', encoding='utf-8') as handle:
            handle.write('99999999\n')
        remove_empty_directory(plan['hold_dir'])
        check(not os.path.exists(os.path.join(plan['hold_dir'], 'Old.vd.busy')), 'a marker from a closed process stayed')
        check(os.path.isdir(plan['hold_dir']), 'a closed marker removed the folder while another encode is running')
        remove_hold_busy_marker(plan)
        remove_empty_directory(plan['hold_dir'])
        check(not os.path.isdir(plan['hold_dir']), 'the holding folder stayed after the last encode marker was removed')
    finally:
        shutil.rmtree(busy_root, ignore_errors=True)
    rows = merge_skip_row([], '/videos/a.mkv', 100, 3000)
    check(remembered_skip(rows, '/videos/a.mkv', 100, 3000), 'a skip was forgotten')
    check(not remembered_skip(rows, '/videos/a.mkv', 100, 2000), 'a lower budget still lists the file')
    check(owned_plexignore_text('\n'.join(plexignore_lines()) + '\n'), 'owned plexignore text was rejected')
    check(not owned_plexignore_text('\n'.join(plexignore_lines()) + '\nKeepMe\n'), 'a plexignore with another line was accepted')
    nested = flatten_rows([[{'id': 'a'}, {'id': 'b'}]])
    check(len(nested) == 2 and nested[1]['id'] == 'b', 'nested rows were not split')
    check(stale_child(['Movies'], 'Movies'), 'a lone removed folder was not stale')
    check(not stale_child(['Movies', 'other.txt'], 'Movies'), 'another name was treated as a stale listing')
    check(not stale_child(['keep.txt'], 'Movies'), 'a different file was treated as a stale listing')
    check(not stale_child([], 'Movies'), 'an empty folder was treated as a stale listing')
    local = make_plan('/tmp/library/.vd-originals/Episode.avi', allow_local=False)
    check(local is None, 'a local file produced a recycle plan')


def _plan(root, relative, name):
    recycle_root = os.path.join(root, '#recycle')
    return {
        'source': '/library/' + relative.replace('\\', '/'),
        'share_root': root,
        'relative': relative,
        'recycle_root': recycle_root,
        'recycle_file': os.path.join(recycle_root, relative),
        'name': name,
        'allow_local': True,
    }


def test_duration_basis():
    span = packet_span_sec('N/A,0.000000,0.041708\r\nN/A,2801.423625,0.041708')
    check(span is not None and abs(span - 2801.465333) < 0.001, 'packet span uses the last timestamp')
    from_pts = packet_span_sec('1.000000,N/A,0.040000\n2.000000,N/A,0.040000')
    check(from_pts is not None and abs(from_pts - 1.04) < 0.001, 'packet span can use a presentation time')
    check(packet_span_sec('') is None, 'an empty packet list has no span')
    capped = packet_span_sec('0,0,99999\n10,10,0.040000')
    check(capped is not None and abs(capped - 10.04) < 0.001, 'a bogus packet duration does not stretch the span')
    kept = duration_basis(3000.6, 2801.2, 2801.465333)
    check(not kept['changed'], 'a long header does not reject the real picture')
    check(abs(kept['basis'] - 2801.465333) < 0.001, 'the rate uses the real picture length')
    dropped = duration_basis(3000, 2801, 3000)
    check(dropped['changed'] and abs(dropped['basis'] - 3000) < 0.001, 'a short encode still fails when the picture is full length')
    normal = duration_basis(100, 100.4, 40)
    check(not normal['changed'] and abs(normal['basis'] - 100) < 0.001, 'a matching length keeps the header')
    shorter = duration_basis(3000, 2000, 2801)
    check(shorter['changed'], 'a file shorter than the picture still fails')
    unread = duration_basis(3000.6, 2801.2, 0)
    check(unread['changed'], 'a long header still fails when the picture length cannot be read')
    plow = duration_basis(3075.6, 3072.5, 3075.5, 3072.444, 3072.469)
    check(not plow['changed'] and abs(plow['basis'] - 3072.444) < 0.001, 'a timestamp hole keeps the pictures')
    station = duration_basis(3081.2, 3076.4, 3081.2, 3076.365, 3076.288)
    check(not station['changed'], 'Red River Station keeps the pictures')
    fever = duration_basis(2903.7, 2900.5, 2903.7, 2900.440, 2900.480)
    check(not fever['changed'], 'Gold Fever keeps the pictures')
    cut = duration_basis(2993.8, 2985.6, 2993.8, 2993.8, 2993.8)
    check(cut['changed'], 'a shorter picture still fails')
    audio_cut = duration_basis(3075.6, 3072.5, 3075.5, 3072.444, 3075.6)
    check(audio_cut['changed'], 'a timestamp hole still fails when the audio is longer')
    silent = duration_basis(3075.6, 3072.5, 3075.5, 3072.444, -1)
    check(not silent['changed'], 'a timestamp hole with no audio uses the picture clock')
    unknown = duration_basis(3075.6, 3072.5, 3075.5, 3072.444, 0)
    check(unknown['changed'], 'a timestamp hole still fails when the audio clock was not read')
    check(audio_samples_per_packet('aac', 'LC') == 1024, 'AAC-LC is 1024 samples')
    check(audio_samples_per_packet('aac', 'HE-AAC') == 2048, 'HE-AAC is 2048 samples')
    check(abs(frame_seconds('24000/1001') - (1001 / 24000.0)) < 0.0000001, '23.976 frame length')
    check(frame_seconds('0/0') == 0, 'a missing frame rate has no length')
    check(presentation_count('0.000,0.000,0.042\n0.042,0.042,0.042\nN/A,0.084,0.042') == 2, 'presentation count skips a missing time')
    ten = 8 + (10 * 36)
    check(job_view_height(ten, 0) == 8 + (4 * 36), 'four encode bars show before the list scrolls')
    check(job_view_height(8 + (2 * 36), 0) == 8 + (2 * 36), 'two encode bars do not leave empty space')
    check(job_view_height(ten, 80) == 8 + (4 * 36) + 80, 'dragging the encode bars keeps the extra pixels')
    check(job_view_height(ten, 9999) == ten, 'the encode bars do not grow past the files')
    check(job_view_height(ten, -9999) == 8 + 36, 'the encode bars keep one row')
    check(job_view_height(0, 40) == 0, 'hidden encode bars take no space')


def test_recycle():
    root = tempfile.mkdtemp(prefix='vdh-recycle-')
    try:
        relative = os.path.join('TV', 'Show', '.vd-originals', 'Episode.avi')
        recycle_root = os.path.join(root, '#recycle')
        hold = os.path.join(recycle_root, 'TV', 'Show', '.vd-originals')
        os.makedirs(hold)
        target = os.path.join(hold, 'Episode.avi')
        payload = b'original-bytes'
        with open(target, 'wb') as handle:
            handle.write(payload)
        decoy_dir = os.path.join(recycle_root, 'TV', 'Show')
        decoy = os.path.join(decoy_dir, 'Episode.avi')
        with open(decoy, 'wb') as handle:
            handle.write(payload)
        sibling = os.path.join(hold, 'Other.avi')
        with open(sibling, 'wb') as handle:
            handle.write(payload)
        hidden = os.path.join(hold, 'desktop.ini')
        with open(hidden, 'w', encoding='utf-8') as handle:
            handle.write('keep')
        keep = os.path.join(recycle_root, 'TV', 'keep.txt')
        with open(keep, 'w', encoding='utf-8') as handle:
            handle.write('keep')
        plan = _plan(root, relative, 'Episode.avi')
        blocked = dict(plan)
        blocked['allow_local'] = False
        refused = purge_file(blocked, len(payload))
        check(refused['status'] == 'skipped', 'a local root was accepted')
        check(os.path.isfile(target), 'a local root removed the file')
        wrong = purge_file(plan, len(payload) + 9)
        check(wrong['status'] == 'mismatch', 'a different size was deleted')
        check(os.path.getsize(target) == len(payload), 'a different size changed the file')
        library = os.path.join(root, 'library.txt')
        with open(library, 'w', encoding='utf-8') as handle:
            handle.write('library')
        bad = _plan(root, os.path.join('TV', 'Show', 'Episode.mkv'), 'Episode.mkv')
        bad['recycle_file'] = library
        bad['source'] = library
        refused_library = purge_file(bad, os.path.getsize(library))
        check(refused_library['status'] == 'skipped', 'a library path passed the recycle check')
        check(open(library, encoding='utf-8').read() == 'library', 'the library file was changed')
        purged = purge_file(plan, len(payload))
        check(purged['status'] == 'purged', 'the matching copy was not removed')
        check(not os.path.exists(target), 'the matching copy remained')
        check(os.path.isfile(decoy), 'a same-named file outside .vd-originals was deleted')
        check(os.path.isfile(sibling), 'a different held file was deleted')
        check(os.path.isfile(hidden), 'a hidden file was deleted')
        check(os.path.isfile(keep), 'keep.txt was deleted')
        check(os.path.isdir(hold), 'the holding folder was removed while files remained')
        check(os.path.isdir(recycle_root), 'the recycle root was removed')
        os.remove(sibling)
        os.remove(hidden)
        os.remove(decoy)
        with open(target, 'wb') as handle:
            handle.write(payload)
        collapsed = purge_file(plan, len(payload))
        check(collapsed['status'] == 'purged', 'the empty-folder purge failed')
        check(not os.path.isdir(hold), 'the empty holding folder remained')
        show = os.path.join(recycle_root, 'TV', 'Show')
        check(not os.path.isdir(show), 'the empty show folder remained')
        check(os.path.isdir(os.path.join(recycle_root, 'TV')), 'a folder that still had a file was removed')
        check(os.path.isfile(keep), 'keep.txt was removed with its folder')
        check(os.path.isdir(recycle_root), 'the recycle root was removed after cleanup')
        solo_hold = os.path.join(recycle_root, 'Solo', 'Nest', '.vd-originals')
        os.makedirs(solo_hold)
        clip = os.path.join(solo_hold, 'Clip.avi')
        with open(clip, 'wb') as handle:
            handle.write(payload)
        solo = purge_file(_plan(root, os.path.join('Solo', 'Nest', '.vd-originals', 'Clip.avi'), 'Clip.avi'), len(payload))
        check(solo['status'] == 'purged', 'an empty folder chain was not purged')
        check(not os.path.isdir(solo_hold), 'the empty nest holding folder remained')
        check(not os.path.isdir(os.path.join(recycle_root, 'Solo')), 'the empty solo folder remained')
        check(os.path.isdir(recycle_root), 'the recycle root was removed with an empty chain')
        check(os.path.isfile(keep), 'an empty chain removed keep.txt')
        recycle_mod.lookup_waits = (0.08, 0.08, 0.08, 0.08, 0.08)
        late_rel = os.path.join('Late', 'Show', '.vd-originals', 'Late.avi')
        late_hold = os.path.join(recycle_root, 'Late', 'Show', '.vd-originals')
        os.makedirs(late_hold)
        late_target = os.path.join(late_hold, 'Late.avi')
        late_payload = b'late-bytes'

        def write_late():
            time.sleep(0.12)
            with open(late_target, 'wb') as handle:
                handle.write(late_payload)

        thread = threading.Thread(target=write_late)
        thread.start()
        late = purge_file(_plan(root, late_rel, 'Late.avi'), len(late_payload))
        thread.join()
        check(late['status'] == 'purged', 'a recycle copy that arrived late was left behind')
        check(not os.path.exists(late_target), 'the late recycle copy remained')
        recycle_mod.lookup_waits = (0.001,)
        never_rel = os.path.join('Never', 'Show', '.vd-originals', 'Never.avi')
        os.makedirs(os.path.join(recycle_root, 'Never', 'Show', '.vd-originals'))
        never = purge_file(_plan(root, never_rel, 'Never.avi'), 4)
        check(never['status'] == 'missing', 'a copy that never arrived was removed')
        recycle_mod.lookup_waits = (1.0, 2.0, 4.0, 8.0)
        busy = os.path.join(recycle_root, 'Busy')
        os.makedirs(busy)
        busy_file = os.path.join(busy, 'other.txt')
        with open(busy_file, 'w', encoding='utf-8') as handle:
            handle.write('keep')
        started = time.perf_counter()
        busy_empty = directory_is_empty(busy, 'Gone')
        elapsed = time.perf_counter() - started
        check(busy_empty is False, 'a folder with another file was treated as empty')
        check(elapsed < 0.12, 'a folder with another file waited')
        check(not remove_verified_empty(busy, 'Gone'), 'a folder with a file was removed')
        check(open(busy_file, encoding='utf-8').read() == 'keep', 'a folder with a file lost that file')
        link_target = os.path.join(root, 'marker.txt')
        with open(link_target, 'w', encoding='utf-8') as handle:
            handle.write('marker')
        link_hold = os.path.join(recycle_root, 'TV', 'LinkShow', '.vd-originals')
        os.makedirs(link_hold)
        link = os.path.join(link_hold, 'Episode.avi')
        try:
            os.symlink(link_target, link)
        except OSError:
            link = ''
        if link:
            linked = purge_file(_plan(root, os.path.join('TV', 'LinkShow', '.vd-originals', 'Episode.avi'), 'Episode.avi'), 6)
            check(linked['status'] == 'mismatch', 'a symlink was deleted')
            check(open(link_target, encoding='utf-8').read() == 'marker', 'the symlink target was deleted')
            check(os.path.lexists(link), 'the symlink was removed')
        owned = '\n'.join(plexignore_lines()) + '\n'
        show_dir = os.path.join(recycle_root, 'Movies', 'Show')
        os.makedirs(show_dir)
        ignore = os.path.join(show_dir, '.plexignore')
        data = owned.encode('utf-8')
        with open(ignore, 'wb') as handle:
            handle.write(data)
        other = os.path.join(recycle_root, 'Movies', 'other.txt')
        with open(other, 'w', encoding='utf-8') as handle:
            handle.write('keep')
        ignore_plan = _plan(root, os.path.join('Movies', 'Show', '.plexignore'), '.plexignore')
        ignore_plan['source'] = '/library/Movies/Show/.plexignore'
        extra_dir = os.path.join(recycle_root, 'Movies', 'OtherShow')
        os.makedirs(extra_dir)
        extra = os.path.join(extra_dir, '.plexignore')
        extra_text = owned + 'KeepMe\n'
        with open(extra, 'w', encoding='utf-8', newline='') as handle:
            handle.write(extra_text)
        extra_plan = _plan(root, os.path.join('Movies', 'OtherShow', '.plexignore'), '.plexignore')
        extra_result = purge_plexignore(extra_plan, os.path.getsize(extra))
        check(extra_result['status'] == 'mismatch', 'a plexignore with other lines was deleted')
        check(os.path.isfile(extra), 'a plexignore with other lines was removed')
        ignore_result = purge_plexignore(ignore_plan, len(data))
        check(ignore_result['status'] == 'purged', 'the owned plexignore was not removed')
        check(not os.path.isfile(ignore), 'the owned plexignore remained')
        check(not os.path.isdir(show_dir), 'the empty show folder remained')
        check(os.path.isdir(os.path.join(recycle_root, 'Movies')), 'a folder that still had a file was removed')
        check(os.path.isfile(other), 'other.txt was removed')
        check(os.path.isfile(extra), 'the other plexignore was removed')
        check(os.path.isdir(recycle_root), 'the recycle root was removed')
    finally:
        recycle_mod.lookup_waits = (1.0, 2.0, 4.0, 8.0)
        shutil.rmtree(root, ignore_errors=True)


def _class_body(source, name):
    chunk = source.split(f'class {name}', 1)[1]
    # Stop at the next top-level class or function.
    lines = []
    for line in chunk.splitlines()[1:]:
        if line.startswith('class ') or line.startswith('def '):
            break
        lines.append(line)
    return '\n'.join(lines)


def test_scan_posts_events():
    source = open(os.path.join(os.path.dirname(__file__), 'ui.py'), encoding='utf-8').read()
    scan = _class_body(source, 'ScanWorker')
    manual = _class_body(source, 'ForceWorker')
    check("self._post('done'" in scan, 'scan posts a done event')
    check("self._post('hit'" in scan, 'scan posts each listed file')
    check('.emit(' not in scan, 'scan does not emit a Qt signal')
    check("self._post('done'" in manual, 'a manual walk posts a done event')
    check('.emit(' not in manual, 'a manual walk does not emit a Qt signal')
    try:
        import queue
        from PySide6.QtCore import QCoreApplication
        from .ui import ForceWorker, ScanWorker
    except ImportError:
        return
    if QCoreApplication.instance() is None:
        QCoreApplication([])
    root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(root, 'Season', '.vd-originals'))
        with open(os.path.join(root, 'Season', 'note.txt'), 'w', encoding='utf-8') as handle:
            handle.write('not a video')
        worker = ScanWorker(root, True, 'ffprobe', [], threading.Event(), 0)
        worker.run()
        events = []
        while True:
            try:
                events.append(worker.events.get_nowait())
            except queue.Empty:
                break
        check(events and events[-1][0] == 'done', 'scan posts a done event')
        check(events[-1][1][0]['seen'] == 0, 'a text file is not listed')
        check(all(kind != 'hit' for kind, _payload in events), 'a text file posts no hit')
        walked_worker = ForceWorker(root, True, False, 'ffprobe', threading.Event())
        walked_worker.run()
        walked = []
        while True:
            try:
                walked.append(walked_worker.events.get_nowait())
            except queue.Empty:
                break
        check(walked and walked[-1][0] == 'done', 'a manual walk posts a done event')
        check(walked[-1][1][0]['files'] == [], 'a manual walk skips a text file')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_encode_child():
    """One file runs outside the window, and a dead child does not encode in-process."""
    import sys
    from types import SimpleNamespace

    from .engine import Probe, app_dir, replace_plan
    loaded = {name for name in sys.modules if name.startswith('PySide') or name == 'video_dehydrator.ui'}
    from . import filejob
    from .filejob import (
        PipeHost, abandon_file, encode_file, merge_worker_row, run_isolated_file, wire_row,
    )
    from .recycle import RecycleBook, set_purge_enabled
    loaded_after = {name for name in sys.modules if name.startswith('PySide') or name == 'video_dehydrator.ui'}
    check(loaded == loaded_after, 'importing the encode child loaded the window')

    source = open(filejob.__file__, encoding='utf-8').read()
    check('PySide' not in source, 'the encode child imports PySide')
    check('video_dehydrator.ui' not in source and 'import ui' not in source, 'the encode child imports the window')
    window = open(os.path.join(os.path.dirname(__file__), 'ui.py'), encoding='utf-8').read()
    worker = _class_body(window, 'ConvertWorker')
    method = worker.split('def convert_row(', 1)[1]
    body = []
    for line in method.splitlines()[1:]:
        if line.startswith('    def ') or line.startswith('class '):
            break
        body.append(line)
    body_text = '\n'.join(body)
    check('run_isolated_file(self, row)' in body_text, 'convert_row does not use the encode child')
    check('probe_file' not in body_text and 'handbrake' not in body_text.lower(), 'convert_row still encodes in the window')
    gc_method = window.split('def _install_window_gc(', 1)[1]
    gc_body = []
    for line in gc_method.splitlines()[1:]:
        if line.startswith('    def ') or line.startswith('class '):
            break
        gc_body.append(line)
    gc_text = '\n'.join(gc_body)
    check('gc.disable()' in gc_text and 'gc.collect()' in gc_text, 'the window still collects automatically')
    check('_gc_timer' in gc_text and '5000' in gc_text, 'collection is not on the window timer')
    check('gc.freeze' not in window, 'the window freezes the collector')

    probe = Probe(width=320, height=240, path='movie.mkv')
    row = {'source': 'movie.mkv', 'path': 'movie.mkv', 'status': 'Bloated', 'probe': probe}
    posted = wire_row(row)
    check('probe' not in posted, 'a child row kept the probe object')
    merge_worker_row(row, {'status': 'Failed', 'probe': {'width': 1}})
    check(row['status'] == 'Failed', 'a child row did not update')
    check(row['probe'] is probe, 'a child row replaced the probe object')

    class CancelHost:
        def __init__(self):
            self.cancel = threading.Event()
            self.cancel.set()
            self.force = False
            self.slot = SimpleNamespace(name='')
            self.posted = []

            class Tools:
                def __getitem__(self, key):
                    raise SystemExit('a cancelled file used a tool')

            self.tools = Tools()

        def _slot(self):
            return self.slot

        def _reset_clock(self):
            pass

        def _set_progress(self, value, force=False):
            return True

        def _restore_status(self, item, previous):
            item['status'] = previous
            item['checked'] = True

        def _post_row(self, item):
            self.posted.append(dict(item))

    cancel_host = CancelHost()
    cancel_row = {'source': 'movie.mkv', 'path': 'movie.mkv', 'status': 'Can shrink', 'checked': True}
    check(encode_file(cancel_host, cancel_row) is None, 'a cancelled file returned a result')
    check(cancel_row['status'] == 'Can shrink', 'a cancelled file changed status')
    check(cancel_host.posted and cancel_host.posted[0]['status'] == 'Can shrink', 'a cancelled file was not posted')

    events = []
    pool_host = PipeHost({
        'dynamic': True,
        'locked_pool': False,
        'pool': 8,
        'node': 1,
        'node_cap': 16,
        'jobs': 2,
        'tools': {},
        'batch': 'batch',
    }, threading.Event(), emit=events.append)
    small = SimpleNamespace(width=640, height=480)
    check(pool_host._settle_pool({}, small) == 'requeue', 'a smaller picture kept the large thread pool')
    check(events and events[-1]['op'] == 'requeue' and events[-1]['need'] == 3, 'a pool change was not reported')
    check(pool_host._settle_pool({}, SimpleNamespace(width=1920, height=1080)) is None, 'a matching picture was sent back')
    pool_host.locked_pool = True
    check(pool_host._settle_pool({}, small) is None, 'a settled picture was sent back again')

    class Lane:
        def __init__(self, tools):
            self.events = []
            self.cancel = threading.Event()
            self.tools = tools
            self.reencode = False
            self.delete_original = False
            self.detailed = False
            self.force = False
            self.jobs = 1
            self.dynamic = False
            self.cpu_target = 100
            self.batch = 'batch'
            self.saved = 0
            self.book = RecycleBook()
            self.skips = []
            self._admission = None
            self._tls = SimpleNamespace(node=0, pool=0)
            self._state = threading.Lock()
            self.slot = SimpleNamespace(
                name='', percent=0.0, checking=False, ten_bit=False, pixels=0.0,
                paint_task=0, pass_started=0.0, pass1_sec=0.0, task=0, progress_info=None,
                remembered=False, live8=[], live10=[], file_samples=[], batch_samples=[],
                shown_at=None, shown_key='', file_seconds=0.0,
            )

        def _slot(self):
            return self.slot

        def _reset_clock(self):
            from .filejob import reset_clock
            reset_clock(self.slot)

        def _set_progress(self, value, force=False):
            self.slot.percent = float(value)
            return True

        def _note_pass(self, info):
            self.events.append(('pass', info))

        def _emit_eta(self):
            self.events.append(('eta',))

        def _remember_finished(self):
            self.events.append(('remember',))

        def _post(self, kind, *payload):
            self.events.append((kind, payload))

        def _post_row(self, item):
            self._post('file', dict(item))

        def _note_fault(self, kind, source, reason, probe=None, out_probe=None, old_bytes=0, new_bytes=0, old_gibph=None, new_gibph=None, kbps=0, command=None, handbrake_log='', handbrake_code=None):
            self.events.append(('fault-call', reason, handbrake_log))

        def note_skip(self, path, size_bytes, kbps):
            self.events.append(('skip-add', path))

        def note_unskip(self, path):
            self.events.append(('skip-remove', path))

        def _settle_pool(self, item, picture):
            return None

        def _pool_size(self):
            return 0

    root = tempfile.mkdtemp()
    previous_purge = None
    from . import recycle as recycle_mod
    previous_purge = recycle_mod._purge_choice
    set_purge_enabled(False)
    log_calls = []
    original_log = filejob.append_convert_log
    filejob.append_convert_log = lambda *args, **kwargs: log_calls.append(args)
    log_path = app_dir() / 'convert-log.tsv'
    log_before = log_path.read_bytes() if log_path.is_file() else None
    try:
        source_path = os.path.join(root, 'movie.mkv')
        with open(source_path, 'wb') as handle:
            handle.write(b'original-bytes')
        plan = replace_plan(source_path)
        os.makedirs(plan['hold_dir'], exist_ok=True)
        with open(plan['temp'], 'wb') as handle:
            handle.write(b'temporary-encode')
        add_hold_busy_marker(plan)
        busy = hold_busy_path(plan)
        check(os.path.isfile(plan['temp']), 'the temp encode was not created')
        check(os.path.isfile(busy), 'the busy marker was not created')
        lane = Lane({})
        item = {
            'source': source_path, 'path': source_path, 'status': 'Converting',
            'checked': True, 'width': 320, 'height': 240, 'target': 200, 'name': 'movie.mkv',
        }
        abandon_file(lane, item, 'traceback from the encode child')
        check(not os.path.isfile(plan['temp']), 'a stopped encode left the temp file')
        check(not os.path.isfile(busy), 'a stopped encode left the busy marker')
        check(open(source_path, 'rb').read() == b'original-bytes', 'a stopped encode changed the original')
        check(item['status'] == 'Failed' and item['reason'] == 'The encode stopped.', 'a stopped encode was not marked failed')
        check(any(
            event[0] == 'activity' and event[1] and event[1][0] == 'Failed movie.mkv. The encode stopped. The original stayed.'
            for event in lane.events
        ), 'a stopped encode did not say the original stayed')
        check(log_calls and log_calls[0][0] == 'FAIL' and log_calls[0][1] == 'The encode stopped.', 'a stopped encode was not logged')
        check(any(
            event[0] == 'fault-call' and 'traceback from the encode child' in str(event)
            for event in lane.events
        ), 'a stopped encode hid the child error')

        missing = os.path.join(root, 'missing-ffprobe')
        child_row = {
            'source': source_path, 'path': source_path, 'status': 'Bloated', 'checked': True,
            'width': 320, 'height': 240, 'name': 'movie.mkv',
        }
        child = Lane({
            'ffprobe': missing, 'ffmpeg': missing, 'handbrake': missing,
        })
        started = time.perf_counter()
        holder = {}

        def run_child():
            holder['outcome'] = run_isolated_file(child, child_row)

        thread = threading.Thread(target=run_child, daemon=True)
        thread.start()
        thread.join(15)
        elapsed = time.perf_counter() - started
        check(not thread.is_alive(), 'the encode child did not finish')
        check(holder.get('outcome') is None, 'an unread file was sent back to the queue')
        check(elapsed < 5, f'the encode child took {elapsed:.2f}s')
        activity = [event[1][0] for event in child.events if event[0] == 'activity' and len(event) > 1 and event[1]]
        check(any('Could not read movie.mkv' in text for text in activity), 'an unread file did not fail in the child: ' + repr(activity))
        check(child_row['status'] == 'Failed', 'an unread file was not marked failed')
        check(any(event[0] == 'fault' for event in child.events), 'an unread file did not report a fault')
        check(open(source_path, 'rb').read() == b'original-bytes', 'an unread file changed the original')
        log_after = log_path.read_bytes() if log_path.is_file() else None
        check(log_before == log_after, 'the encode child wrote the convert log')
    finally:
        filejob.append_convert_log = original_log
        recycle_mod._purge_choice = previous_purge
        shutil.rmtree(root, ignore_errors=True)


def test_startup_cleanup():
    """Starting the window stops a previous encode and drops its half-written files."""
    own = 10
    handbrake = ['HandBrakeCLI', '-i', '/show/Episode.mkv', '-o', '/show/.vd-originals/Episode.vd.tmp.mkv']
    check(is_leftover_encode_process(20, handbrake, own), 'a previous temp encode was left running')
    check(not is_leftover_encode_process(own, handbrake, own), 'the running window looked like a leftover encode')
    check(is_leftover_encode_process(21, ['python', '-u', '-m', 'video_dehydrator.filejob'], own), 'a previous file process was left running')
    check(not is_leftover_encode_process(22, ['HandBrakeCLI', '-i', '/home/movie.mkv', '-o', '/home/movie.mkv'], own), 'another program\'s HandBrake was treated as ours')
    check(encode_output_paths(handbrake) == ['/show/.vd-originals/Episode.vd.tmp.mkv'], 'the temp path was not read from the encode')
    check(unfinished_encode_name('Episode.vd.tmp.mkv.stats'), 'a pass file was kept')
    check(not unfinished_encode_name('Episode.mkv'), 'a held original looked unfinished')
    check(recovery_note(0, 0) == '', 'an idle start reported cleanup')
    check(recovery_note(2, 3) == 'Stopped 2 unfinished encodes and removed 3 temporary files.', 'cleanup was not reported')
    root = tempfile.mkdtemp(prefix='vd-cleanup-')
    try:
        show = os.path.join(root, 'Season')
        hold = os.path.join(show, '.vd-originals')
        os.makedirs(hold)
        kept = os.path.join(hold, 'Finished.mkv')
        original = os.path.join(show, 'Episode.mkv')
        temp = os.path.join(hold, 'Episode.vd.tmp.mkv')
        stats = temp + '.stats'
        busy = os.path.join(hold, 'Episode.vd.busy')
        outside = os.path.join(show, 'Episode.vd.tmp.mkv')
        for path in (kept, original, temp, stats, busy, outside):
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('x')
        guarded = {os.path.normcase(os.path.abspath(os.path.join(hold, 'Live.vd.tmp.mkv')))}
        live = os.path.join(hold, 'Live.vd.tmp.mkv')
        with open(live, 'w', encoding='utf-8') as handle:
            handle.write('still')
        removed = clean_unfinished_encodes(root, None, guarded)
        check(not os.path.exists(temp), 'a half-written encode stayed')
        check(not os.path.exists(stats), 'a pass file stayed')
        check(not os.path.exists(busy), 'a busy marker stayed')
        check(os.path.isfile(kept), 'a held original was removed')
        check(os.path.isfile(original), 'a show file was removed')
        check(os.path.isfile(outside), 'a file outside the holding folder was removed')
        check(os.path.isfile(live), 'an encode that was still running was removed')
        check(os.path.isdir(hold), 'the holding folder was removed while an original remained')
        check(all(path != live for path in removed), 'a live temp was counted as removed')
        empty_show = os.path.join(root, 'Empty')
        empty_hold = os.path.join(empty_show, '.vd-originals')
        os.makedirs(empty_hold)
        with open(os.path.join(empty_hold, 'Gone.vd.tmp.mkv'), 'w', encoding='utf-8') as handle:
            handle.write('x')
        clean_unfinished_encodes(root)
        check(not os.path.isdir(empty_hold), 'an empty holding folder stayed')
        check(os.path.isfile(kept), 'a later cleanup removed a held original')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    test_rules()
    test_duration_basis()
    test_recycle()
    test_scan_posts_events()
    test_encode_child()
    test_startup_cleanup()
    print('SELF TEST OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
