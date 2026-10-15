"""Engine checks. Run: python -m video_dehydrator.selftest"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

from .engine import (
    Probe, app_version, bloat_decision, budget_gib_per_hour, compare_versions,
    format_over_ratio, harmless_decode_note, merge_skip_row, over_band_id,
    over_ratio, owned_plexignore_text, plexignore_lines, remembered_skip,
    set_budget_picks, size_skip_reason, target_kbps,
)
from .recycle import (
    directory_is_empty, make_plan, purge_file, purge_plexignore, remove_verified_empty,
    stale_child,
)
from .store import flatten_rows


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
    check(format_over_ratio(2) == '2.0x', 'over format')
    check(format_over_ratio(None) == '', 'blank over format')
    bloated = bloat_decision(fake_probe(size_bytes=3 * (1024 ** 3), video_bitrate=15000000))
    check(bloated['decision'] == 'bloated' and bloated['target_kbps'] == 3000, '1080p over budget is bloated')
    lean = bloat_decision(fake_probe(size_bytes=int(1.5 * (1024 ** 3)), video_bitrate=2500000))
    check(lean['decision'] == 'lean', '1080p under budget is lean')
    hevc = bloat_decision(fake_probe(size_bytes=int(2.5 * (1024 ** 3)), video_bitrate=5000000, already_efficient=True))
    check(hevc['decision'] == 'lean', 'HEVC under its line is lean')
    dv = bloat_decision(fake_probe(size_bytes=5 * (1024 ** 3), is_dolby_vision=True))
    check(dv['decision'] == 'dv', 'Dolby Vision is left alone')
    check(harmless_decode_note('Application provided invalid, non monotonically increasing dts to muxer in stream 0: 10 >= 10'), 'equal dts is harmless')
    check(not harmless_decode_note('Application provided invalid, non monotonically increasing dts to muxer in stream 0: 11 >= 10'), 'a backwards timestamp fails')
    check(size_skip_reason('the new file is not smaller; the new file is not smaller per hour'), 'size skip')
    check(not size_skip_reason('the new file is not smaller; audio could not be copied'), 'mixed failure is not a size skip')
    check(compare_versions('1.0.5.1', 'v1.0.5.1') == 0, 'version prefix')
    check(compare_versions('1.0.5', '1.0.5.1') == -1, 'shorter version is older')
    check(app_version() == '1.0.5.1', 'app version')
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
        shutil.rmtree(root, ignore_errors=True)


def main():
    test_rules()
    test_recycle()
    print('SELF TEST OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
