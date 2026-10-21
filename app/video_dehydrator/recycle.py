"""Remove one matching #recycle copy. Never empty the bin."""

from __future__ import annotations

import os
import stat
import time

from .engine import owned_plexignore_text

RECYCLE_NAME = '#recycle'
REMOTE_TYPES = {
    'cifs', 'smb', 'smb3', 'nfs', 'nfs4', 'fuse.sshfs', 'fuse.rclone',
    'davfs', 'fuse.davfs', 'fuse.gvfsd-fuse',
}
PROBLEM_STATUSES = ('missing', 'denied', 'mismatch', 'failed', 'remained')


class RecycleBook:
    def __init__(self):
        self.rows = []

    def add(self, row):
        if row:
            self.rows.append(row)
        return row

    def clear(self):
        self.rows = []

    def problems(self):
        return [row for row in self.rows if row and not row.get('shown') and row.get('status') in PROBLEM_STATUSES]


# None means the engine is running on its own, where the purge stays available.
# The window sets False until Clear the drive recycle copy is checked.
_purge_choice = None


def set_purge_enabled(enabled):
    global _purge_choice
    _purge_choice = None if enabled is None else bool(enabled)


def purge_enabled() -> bool:
    return True if _purge_choice is None else bool(_purge_choice)


def result(status, source, recycle_file, message, name):
    return {
        'status': status,
        'source': source or '',
        'recycle_file': recycle_file or '',
        'message': message or '',
        'name': name or '',
        'shown': False,
        'reported': False,
    }


def refusal(plan, message=''):
    source = ''
    recycle_file = ''
    name = ''
    if plan:
        source = plan.get('source') or ''
        recycle_file = plan.get('recycle_file') or ''
        name = plan.get('name') or ''
    return result('skipped', source, recycle_file, message or 'This path is not a held original on a shared drive.', name)


def is_recycle_segment(part: str) -> bool:
    return part.casefold() in (RECYCLE_NAME.casefold(), '@recycle')


def plain_parts(path: str):
    text = os.fspath(path).replace('\\', '/')
    parts = [part for part in text.split('/') if part != '']
    if text.startswith('//') or text.startswith('\\\\'):
        parts = parts
    return parts


def bad_segment(part: str) -> bool:
    if part in ('.', '..'):
        return True
    if is_recycle_segment(part):
        return True
    if any(ch in part for ch in '/\\:*?"<>|'):
        return True
    return False


def same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def under_root(path: str, root: str) -> bool:
    path_full = os.path.normcase(os.path.normpath(path))
    root_full = os.path.normcase(os.path.normpath(root))
    if path_full == root_full:
        return False
    prefix = root_full.rstrip('\\/') + os.sep
    return path_full.startswith(os.path.normcase(prefix))


def _unescape_mount(text: str) -> str:
    return text.replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')


def read_mounts():
    path = '/proc/mounts'
    if not os.path.isfile(path):
        return []
    found = []
    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            bits = line.split()
            if len(bits) < 3:
                continue
            found.append((_unescape_mount(bits[1]), bits[2]))
    return found


def find_share(path: str, allow_local: bool = False):
    real = os.path.realpath(path)
    best = None
    for mount, fstype in read_mounts():
        mount_norm = os.path.normpath(mount)
        if real == mount_norm or under_root(real, mount_norm) or same_path(real, mount_norm):
            if best is None or len(mount_norm) > len(best[0]):
                best = (mount_norm, fstype)
    if best is None:
        return None
    if not allow_local and best[1] not in REMOTE_TYPES and not best[1].startswith('fuse.'):
        return None
    relative = os.path.relpath(real, best[0])
    if relative in ('.', os.curdir):
        parts = []
    else:
        parts = relative.split(os.sep)
    return {'share_root': best[0], 'parts': parts, 'relative': '' if not parts else os.path.join(*parts), 'fstype': best[1]}


def make_plan(path: str, allow_local: bool = False, share=None):
    loc = share if share is not None else find_share(path, allow_local=allow_local)
    if not loc:
        return None
    parts = list(loc['parts'])
    if len(parts) < 2:
        return None
    leaf = parts[-1]
    parent = parts[-2]
    if parent.casefold() != '.vd-originals':
        return None
    if leaf.casefold() == '.vd-originals':
        return None
    if any(bad_segment(part) for part in parts):
        return None
    root = os.path.join(loc['share_root'], RECYCLE_NAME)
    relative = os.path.join(*parts)
    return {
        'source': path,
        'share_root': loc['share_root'],
        'relative': relative,
        'recycle_root': root,
        'recycle_file': os.path.join(root, relative),
        'name': leaf,
        'allow_local': allow_local,
    }


def directory_plan(directory: str, allow_local: bool = False, share=None):
    loc = share if share is not None else find_share(directory, allow_local=allow_local)
    if not loc:
        return None
    parts = list(loc['parts'])
    if len(parts) < 1:
        return None
    if parts[-1].casefold() != '.vd-originals':
        return None
    if any(bad_segment(part) for part in parts):
        return None
    root = os.path.join(loc['share_root'], RECYCLE_NAME)
    relative = os.path.join(*parts)
    return {
        'source': directory,
        'share_root': loc['share_root'],
        'relative': relative,
        'recycle_root': root,
        'recycle_file': os.path.join(root, relative),
        'name': parts[-1],
        'allow_local': allow_local,
    }


def named_plan(path: str, leaf_name: str, allow_local: bool = False, share=None):
    if leaf_name != '.plexignore':
        return None
    loc = share if share is not None else find_share(path, allow_local=allow_local)
    if not loc:
        return None
    parts = list(loc['parts'])
    if not parts or parts[-1].casefold() != leaf_name.casefold():
        return None
    if any(bad_segment(part) for part in parts):
        return None
    root = os.path.join(loc['share_root'], RECYCLE_NAME)
    relative = os.path.join(*parts)
    return {
        'source': path,
        'share_root': loc['share_root'],
        'relative': relative,
        'recycle_root': root,
        'recycle_file': os.path.join(root, relative),
        'name': parts[-1],
        'allow_local': allow_local,
    }


def relative_under(plan, path: str):
    root = os.path.normpath(plan['recycle_root'])
    full = os.path.normpath(path)
    if same_path(full, root):
        return ''
    if not under_root(full, root):
        return None
    prefix = os.path.normcase(root.rstrip('\\/') + os.sep)
    text = os.path.normcase(full)
    if not text.startswith(prefix):
        return None
    return full[len(root.rstrip('\\/')) + 1:]


def is_symlink(path: str) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return stat.S_ISLNK(mode)


def not_found(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    return getattr(exc, 'errno', None) in (2, 3)


def access_denied(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, 'errno', None) in (1, 13)


def stale_child(children, removed_name: str) -> bool:
    if not removed_name or not str(removed_name).strip():
        return False
    names = list(children or [])
    if len(names) != 1:
        return False
    only = str(names[0] or '').strip()
    if not only:
        return False
    return only.casefold() == removed_name.casefold()


def directory_is_empty(directory: str, removed_child: str):
    try:
        children = os.listdir(directory)
    except OSError:
        return None
    if len(children) == 0:
        return True
    if not stale_child(children, removed_child):
        return False
    stale = os.path.join(directory, removed_child)
    for _attempt in range(3):
        time.sleep(0.15)
        try:
            children = os.listdir(directory)
        except OSError:
            return None
        if len(children) == 0:
            return True
        if not stale_child(children, removed_child):
            return False
    if os.path.lexists(stale):
        return False
    return True


def remove_verified_empty(path: str, removed_child: str) -> bool:
    for attempt in range(4):
        empty = directory_is_empty(path, removed_child)
        if empty is None or not empty:
            return False
        try:
            os.rmdir(path)
        except OSError:
            pass
        if not os.path.exists(path):
            return True
        if attempt >= 3:
            return False
        time.sleep(0.15)
    return False


def _walk_empty(plan, start, expected, require_holding: bool) -> None:
    if not plan or not start or not expected:
        return
    expected = expected.strip('\\/')
    if require_holding:
        if expected.casefold() != '.vd-originals' and not expected.casefold().endswith(os.sep + '.vd-originals') and not expected.casefold().endswith('/.vd-originals'):
            if not expected.replace('\\', '/').casefold().endswith('/.vd-originals') and expected.casefold() != '.vd-originals':
                return
    actual = relative_under(plan, start)
    if actual is None or actual.replace('\\', '/').casefold() != expected.replace('\\', '/').casefold():
        return
    directory = start
    removed_child = ''
    first = True
    while directory:
        relative = relative_under(plan, directory)
        if not relative:
            return
        parts = relative.replace('\\', '/').split('/')
        if any(is_recycle_segment(part) or part in ('.', '..') for part in parts):
            return
        leaf = parts[-1]
        if first and require_holding and leaf.casefold() != '.vd-originals':
            return
        first = False
        if is_symlink(directory) or not os.path.isdir(directory):
            return
        seen = relative_under(plan, os.path.realpath(directory) if not is_symlink(directory) else directory)
        if seen is None or seen.replace('\\', '/').casefold() != relative.replace('\\', '/').casefold():
            # realpath can jump off the share. Compare the listed path.
            seen = relative_under(plan, directory)
            if seen is None or seen.replace('\\', '/').casefold() != relative.replace('\\', '/').casefold():
                return
        leaf_name = os.path.basename(directory.rstrip('\\/'))
        if not remove_verified_empty(directory, removed_child):
            return
        removed_child = leaf_name
        directory = os.path.dirname(directory.rstrip('\\/'))


def remove_empty_holding_chain(plan, start, expected) -> None:
    _walk_empty(plan, start, expected, True)


def remove_empty_ancestors(plan, start, expected) -> None:
    if not expected or not str(expected).strip('\\/'):
        return
    _walk_empty(plan, start, expected, False)


def _file_matches(plan, length: int):
    recycle_file = plan['recycle_file']
    if is_symlink(recycle_file):
        return result('mismatch', plan['source'], recycle_file, f'A link is at {recycle_file}. It was left in place.', plan['name'])
    if os.path.isdir(recycle_file):
        return result('mismatch', plan['source'], recycle_file, f'A folder is at {recycle_file}. It was left in place.', plan['name'])
    if not os.path.isfile(recycle_file):
        return result('mismatch', plan['source'], recycle_file, f'The file at {recycle_file} is not a normal file. It was left in place.', plan['name'])
    name = os.path.basename(recycle_file)
    if name.casefold() != str(plan['name']).casefold():
        return result('mismatch', plan['source'], recycle_file, f'The file at {recycle_file} is named {name}. It was left in place.', plan['name'])
    parent = os.path.basename(os.path.dirname(recycle_file))
    if parent.casefold() != '.vd-originals':
        return result('mismatch', plan['source'], recycle_file, f'The file at {recycle_file} is outside .vd-originals. It was left in place.', plan['name'])
    relative = relative_under(plan, recycle_file)
    if relative is None or relative.replace('\\', '/').casefold() != plan['relative'].replace('\\', '/').casefold():
        return result('mismatch', plan['source'], recycle_file, f'The file at {recycle_file} resolved to a different path. It was left in place.', plan['name'])
    size = os.path.getsize(recycle_file)
    if size != int(length):
        return result('mismatch', plan['source'], recycle_file, f"A file at {recycle_file} does not match the size of {plan['name']}. It was left in place.", plan['name'])
    return None


def share_is_remote(share: str) -> bool:
    if not share:
        return False
    text = os.fspath(share)
    if text.startswith('\\\\') or text.startswith('//'):
        return True
    real = os.path.normpath(share)
    for mount, fstype in read_mounts():
        mount_norm = os.path.normpath(mount)
        if not (same_path(real, mount_norm) or under_root(real, mount_norm)):
            continue
        if fstype in REMOTE_TYPES or fstype.startswith('fuse.'):
            return True
    return False


def validate_file_plan(plan) -> dict:
    if not plan:
        return refusal(plan)
    relative = str(plan.get('relative') or '')
    parts = [part for part in relative.replace('\\', '/').split('/') if part]
    if len(parts) < 2:
        return refusal(plan)
    if parts[-2].casefold() != '.vd-originals' or parts[-1].casefold() != str(plan.get('name') or '').casefold():
        return refusal(plan)
    if any(bad_segment(part) for part in parts):
        return refusal(plan)
    share = os.path.normpath(plan['share_root'])
    if not plan.get('allow_local') and not share_is_remote(share):
        return refusal(plan)
    root_expected = os.path.join(share, RECYCLE_NAME)
    file_expected = os.path.join(root_expected, *parts)
    if not same_path(plan['recycle_root'], root_expected) or not same_path(plan['recycle_file'], file_expected):
        return refusal(plan)
    if not under_root(file_expected, root_expected):
        return refusal(plan)
    return None


def purge_file(plan, length: int):
    refused = validate_file_plan(plan)
    if refused:
        return refused
    source = plan['source']
    name = plan['name']
    recycle_file = plan['recycle_file']
    if length <= 0:
        return result('skipped', source, recycle_file, 'The file length was not known.', name)
    root = plan['recycle_root']
    if not os.path.exists(root):
        return result('absent', source, recycle_file, f"No #recycle folder on {plan['share_root']}. Deleted files were not left in a drive recycle bin.", name)
    if is_symlink(root) or not os.path.isdir(root):
        return result('failed', source, recycle_file, 'The #recycle path is not the recycle folder. Nothing was deleted.', name)
    if relative_under(plan, root) not in ('', None) and relative_under(plan, root) != '':
        return result('failed', source, recycle_file, 'The #recycle folder resolved to a different path. Nothing was deleted.', name)
    if not os.path.lexists(recycle_file):
        return result('missing', source, recycle_file, f'The drive recycle bin has no copy of {name} at {recycle_file}.', name)
    mismatch = _file_matches(plan, length)
    if mismatch:
        return mismatch
    try:
        os.remove(recycle_file)
    except PermissionError:
        return result('denied', source, recycle_file, f'The drive recycle bin could not be changed for {name}. {recycle_file} may still hold a copy.', name)
    except OSError as exc:
        return result('failed', source, recycle_file, f'Could not remove the recycle copy of {name}. {exc}', name)
    if os.path.lexists(recycle_file):
        return result('remained', source, recycle_file, f'The recycle copy of {name} is still at {recycle_file}.', name)
    nested = os.path.join(root, RECYCLE_NAME, plan['relative'])
    if os.path.lexists(nested):
        return result('remained', source, nested, f'The drive put another copy of {name} inside the recycle bin at {nested}. It was left in place.', name)
    parent_relative = os.path.dirname(plan['relative'])
    if parent_relative:
        remove_empty_holding_chain(plan, os.path.dirname(recycle_file), parent_relative)
    return result('purged', source, recycle_file, f'Removed the drive recycle copy of {name}.', name)


def purge_plexignore(plan, length: int):
    if not plan:
        return refusal(plan)
    source = plan['source']
    name = '.plexignore'
    recycle_file = plan['recycle_file']
    relative = str(plan.get('relative') or '')
    parts = [part for part in relative.replace('\\', '/').split('/') if part]
    if not parts or parts[-1].casefold() != '.plexignore' or str(plan.get('name') or '').casefold() != '.plexignore':
        return refusal(plan)
    if any(bad_segment(part) for part in parts):
        return refusal(plan)
    if not plan.get('allow_local') and not share_is_remote(plan['share_root']):
        return refusal(plan)
    root_expected = os.path.join(os.path.normpath(plan['share_root']), RECYCLE_NAME)
    file_expected = os.path.join(root_expected, *parts)
    if not same_path(plan['recycle_root'], root_expected) or not same_path(recycle_file, file_expected):
        return refusal(plan)
    if length <= 0 or length > 4096:
        return result('mismatch', source, recycle_file, f'A .plexignore at {recycle_file} is not the holding-folder file. It was left in place.', name)
    if not os.path.exists(plan['recycle_root']):
        return result('absent', source, recycle_file, 'No #recycle folder was found.', name)
    if is_symlink(plan['recycle_root']) or not os.path.isdir(plan['recycle_root']):
        return result('failed', source, recycle_file, 'The #recycle path is not the recycle folder. Nothing was deleted.', name)
    if not os.path.lexists(recycle_file):
        return result('missing', source, recycle_file, 'The recycled .plexignore was not found.', name)
    if is_symlink(recycle_file) or os.path.isdir(recycle_file) or os.path.basename(recycle_file).casefold() != '.plexignore':
        return result('mismatch', source, recycle_file, f'The item at {recycle_file} is not the holding-folder .plexignore. It was left in place.', name)
    seen = relative_under(plan, recycle_file)
    if seen is None or seen.replace('\\', '/').casefold() != relative.replace('\\', '/').casefold():
        return result('mismatch', source, recycle_file, f'The .plexignore at {recycle_file} resolved to a different path. It was left in place.', name)
    if os.path.getsize(recycle_file) != int(length):
        return result('mismatch', source, recycle_file, f'A .plexignore at {recycle_file} does not match the size of the file that was deleted. It was left in place.', name)
    try:
        with open(recycle_file, 'r', encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except OSError as exc:
        return result('failed', source, recycle_file, f'Could not read the recycled .plexignore. {exc}', name)
    if not owned_plexignore_text(text):
        return result('mismatch', source, recycle_file, f'A .plexignore at {recycle_file} lists more than the holding folder. It was left in place.', name)
    try:
        os.remove(recycle_file)
    except PermissionError:
        return result('denied', source, recycle_file, f'The drive recycle bin could not be changed for .plexignore. {recycle_file} may still hold a copy.', name)
    except OSError as exc:
        return result('failed', source, recycle_file, f'Could not remove the recycled .plexignore. {exc}', name)
    if os.path.lexists(recycle_file):
        return result('remained', source, recycle_file, f'The recycled .plexignore is still at {recycle_file}.', name)
    parent_relative = os.path.dirname(relative)
    if parent_relative:
        remove_empty_ancestors(plan, os.path.dirname(recycle_file), parent_relative)
    return result('purged', source, recycle_file, 'Removed the drive recycle copy of .plexignore.', name)


def remove_recycle_copy(path: str, length: int, book: RecycleBook = None, allow_local: bool = False, share=None):
    if not purge_enabled():
        return None
    name = os.path.basename(path)
    if length <= 0:
        row = result('skipped', path, '', 'The file length was not known.', name)
        return book.add(row) if book else row
    plan = make_plan(path, allow_local=allow_local, share=share)
    if not plan:
        row = result('skipped', path, '', 'This path is not a held original on a shared drive.', name)
        return book.add(row) if book else row
    try:
        row = purge_file(plan, length)
    except Exception as exc:
        row = result('failed', path, plan.get('recycle_file') or '', f'Could not remove the recycle copy of {name}. {exc}', name)
    return book.add(row) if book else row


def remove_recycled_plexignore(path: str, length: int, book: RecycleBook = None, allow_local: bool = False, share=None):
    if not purge_enabled():
        return None
    if length <= 0 or length > 4096:
        return None
    plan = named_plan(path, '.plexignore', allow_local=allow_local, share=share)
    if not plan:
        return None
    try:
        row = purge_plexignore(plan, length)
    except Exception as exc:
        row = result('failed', path, plan.get('recycle_file') or '', f'Could not remove the recycled .plexignore. {exc}', '.plexignore')
    if not row or row['status'] in ('missing', 'absent', 'skipped'):
        return None
    return book.add(row) if book else row


def remove_recycled_holding_folder(directory: str, book: RecycleBook = None, allow_local: bool = False, share=None):
    if not purge_enabled():
        return None
    plan = directory_plan(directory, allow_local=allow_local, share=share)
    if not plan:
        return
    relative = plan['relative']
    parts = [part for part in relative.replace('\\', '/').split('/') if part]
    if any(bad_segment(part) for part in parts):
        return
    if not plan.get('allow_local') and not share_is_remote(plan['share_root']):
        return
    expected_root = os.path.join(os.path.normpath(plan['share_root']), RECYCLE_NAME)
    expected_dir = os.path.join(expected_root, *parts)
    if not same_path(plan['recycle_root'], expected_root) or not same_path(plan['recycle_file'], expected_dir):
        return
    remove_empty_holding_chain(plan, expected_dir, os.path.join(*parts))
