"""Engine checks. Run: python -m video_dehydrator.selftest"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

from .engine import (
    Probe, app_version, batch_count_text, batch_wall_seconds, bloat_decision, budget_gib_per_hour, compare_versions,
    add_hold_busy_marker, delete_targets, encode_command, handbrake_arguments, hold_busy_path,
    job_count, linux_release_is_update, linux_update_offer, output_path_missing, pass_two_did_not_start,
    remove_empty_directory, remove_hold_busy_marker, scan_limit_reached,
    ac3_frame_size, ac3_short_tail, copied_ac3, estimated_savings_bytes,
    encode_rate, force_convertible, format_force_report, format_over_ratio, format_remaining_time,
    lane_bar_text, lane_pass_name,
    median_sample,
    harmless_decode_note, merge_skip_row, over_band_id, remaining_estimate,
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
    check(harmless_decode_note('Application provided invalid, non monotonically increasing dts to muxer in stream 0: 10 >= 10'), 'equal dts is harmless')
    check(not harmless_decode_note('Application provided invalid, non monotonically increasing dts to muxer in stream 0: 11 >= 10'), 'a backwards timestamp fails')
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
    check(app_version() == '1.0.9.9', 'app version')
    check(batch_count_text(4, 100) == '4/100', 'batch count')
    check(batch_count_text(5, 100) == '5/100', 'batch count moves to the next file')
    check(batch_count_text(0, 100) == '', 'batch count waits until a file starts')
    check(batch_count_text(120, 100) == '100/100', 'batch count stays within the batch')
    guide = '\n'.join(body for _title, body in SECTIONS)
    check('4/100 means this batch has started file 4 of 100' in guide, 'guide batch count')
    check('Drag a column edge to change its width' in guide, 'guide column width')
    check('each file has its own bar' in guide, 'guide per-file bars')
    check(lane_pass_name(1) == 'first pass', 'first pass name')
    check(lane_pass_name(2) == 'pass 2', 'second pass name')
    check(lane_pass_name(0, True) == 'checking', 'checking pass name')
    check(lane_bar_text('Episode.mkv', 42.4, 'first pass') == 'Episode.mkv   42%   first pass', 'lane bar text')
    check('opens a terminal, and runs its installer' in guide, 'guide update terminal')
    check(job_count('2') == 2 and job_count(1) == 1 and job_count('no') == 1, 'job count')
    check(job_count(6) == 6 and job_count(9) == 6 and job_count(0) == 1, 'job count stays between 1 and 6')
    check(scan_limit_reached(4, 5) is False and scan_limit_reached(5, 5) and scan_limit_reached(9, 0) is False, 'scan limit')
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


def main():
    test_rules()
    test_recycle()
    print('SELF TEST OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
