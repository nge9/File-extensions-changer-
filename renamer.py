



from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import uuid


class RenameError(Exception):
    pass


@dataclass(frozen=True)
class Rules:
    find: str = ''
    replace: str = ''
    prefix: str = ''
    suffix: str = ''
    case: str = 'keep'
    number: bool = False
    start: int = 1
    digits: int = 3
    number_position: str = 'end'
    separator: str = '_'
    modified_date: bool = False
    lowercase_extension: bool = False
    new_extension: str | None = None


@dataclass
class Entry:
    source: Path
    target: Path
    signature: tuple
    error: str = ''

    @property
    def changed(self):
        return os.fspath(self.source) != os.fspath(self.target)


def signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RenameError(f'Only regular files are supported: {path.name}')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def valid_name(name):
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{i}' for p in ('COM', 'LPT') for i in range(1, 10)}
    return bool(name and name not in ('.', '..') and not name.endswith((' ', '.'))
                and not any(c in name for c in '/\\:*?"<>|')
                and not any(ord(c) < 32 for c in name)
                and name.split('.')[0].upper() not in reserved)


def path_key(path):
                                                                                   
    return str(path).casefold()


def preview(paths, rules):
    if rules.case not in ('keep', 'lower', 'upper', 'title'):
        raise RenameError('Unknown case conversion.')
    if not 0 <= rules.start <= 999999999 or not 1 <= rules.digits <= 12:
        raise RenameError('Numbering start or padding is outside the supported range.')
    if rules.number_position not in ('start', 'end'):
        raise RenameError('Unknown numbering position.')
    replacement_extension = None
    if rules.new_extension is not None:
        if not isinstance(rules.new_extension, str):
            raise RenameError('The extension must be text.')
        raw = rules.new_extension.strip()
        normalized = raw.lstrip('.')
        if (raw and not normalized or normalized.endswith((' ', '.'))
                or any(c in normalized for c in '/\\:*?"<>|')
                or any(ord(c) < 32 for c in normalized)):
            raise RenameError('Use a valid extension such as jpg, .pdf, or tar.gz; leave it empty to remove the extension.')
        replacement_extension = '.' + normalized if normalized else ''
    entries, seen = [], set()
    for index, raw in enumerate(paths):
        raw = Path(raw).expanduser().absolute()
        if raw.is_symlink():
            raise RenameError(f'Symbolic links are not renamed: {raw.name}')
        source = raw.resolve(strict=True)
        if source in seen:
            continue
        seen.add(source)
        identity = signature(source)
        stem, extension = source.stem, source.suffix
        if rules.find:
            stem = stem.replace(rules.find, rules.replace)
        if rules.case != 'keep':
            stem = getattr(stem, rules.case)()
        stem = rules.prefix + stem + rules.suffix
        if rules.modified_date:
            stem = datetime.fromtimestamp(source.stat().st_mtime).strftime('%Y-%m-%d') + '_' + stem
        if rules.number:
            number = str(rules.start + len(entries)).zfill(rules.digits)
            stem = number + rules.separator + stem if rules.number_position == 'start' else stem + rules.separator + number
        if replacement_extension is not None:
            extension = replacement_extension
        if rules.lowercase_extension:
            extension = extension.lower()
        name = stem + extension
        target = source.parent / name
        entries.append(Entry(source, target, identity, '' if valid_name(name) else 'Invalid or reserved filename'))
    changed_sources = {e.source for e in entries if e.changed}
    destinations, identities = {}, {}
    for entry in entries:
        destinations.setdefault(path_key(entry.target), []).append(entry)
        identities.setdefault(entry.signature[:2], []).append(entry)
        if os.path.lexists(entry.target) and entry.target not in changed_sources:
            same_source = False
            try:
                same_source = path_key(entry.source) == path_key(entry.target) and os.path.samefile(entry.source, entry.target)
            except OSError:
                pass
            if not same_source:
                entry.error = 'Destination already exists'
    for group in destinations.values():
        if len(group) > 1:
            for entry in group:
                entry.error = 'Two files would have the same destination'
    for group in identities.values():
        if len(group) > 1:
            for entry in group:
                entry.error = 'The selection contains hard-linked copies of the same file'
    return entries


def move_no_replace(source, target):

    source, target = Path(source), Path(target)
    if os.name == 'nt':
        os.rename(source, target)                                            
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(os.fsencode(source), os.fsencode(target), 4)
    else:
        result = None
    if result == 0:
        return
    if result is not None:
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), str(target))
                                                           
    os.link(source, target, follow_symlinks=False)
    try:
        source.unlink()
    except BaseException:
        if os.path.samefile(source, target):
            target.unlink()
        raise


class History:
    def __init__(self, folder):
        self.folder = Path(folder).expanduser().resolve()
        self.folder.mkdir(parents=True, exist_ok=True)

    def save(self, record):
        identifier = record['id']
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise RenameError('Invalid transaction identifier.')
        fd, temporary = tempfile.mkstemp(dir=self.folder, prefix='.history-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(record, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.folder / (identifier + '.json'))
            if os.name == 'posix':
                directory = os.open(self.folder, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def records(self):
        records = []
        for path in self.folder.glob('*.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record.get('version') != 1 or not isinstance(record.get('entries'), list):
                    raise ValueError('Unsupported history format')
                records.append(record)
            except (ValueError, OSError) as exc:
                raise RenameError(f'Cannot read history {path.name}: {exc}') from exc
                                                                                     
        undone = {r.get('undo_of') for r in records if r['kind'] == 'undo' and r['state'] == 'complete'}
        for record in records:
            if record['id'] in undone and record['state'] == 'complete':
                record['state'] = 'undone'
                self.save(record)
        return sorted(records, key=lambda r: r['created_at'])

    def pending(self):
        return [r for r in self.records() if r['state'] in ('pending', 'recovery_required')]

    @staticmethod
    def locate(entry):
        matches = []
        for candidate in dict.fromkeys([entry['source'], entry['stage'], entry['target']]):
            path = Path(candidate)
            try:
                if signature(path) == tuple(entry['signature']):
                    if not any(os.path.samefile(path, other) for other in matches):
                        matches.append(path)
            except (OSError, RenameError):
                pass
        if len(matches) != 1:
            raise RenameError(f'Cannot identify the unchanged file for {entry["source"]}. Keep all files and inspect the history log.')
        return matches[0]

    def rollback(self, record):
        record['state'] = 'recovery_required'
        self.save(record)
        locations = [(entry, self.locate(entry)) for entry in record['entries']]
                                                                     
        for entry, location in locations:
            stage = Path(entry['stage'])
            if location != stage:
                move_no_replace(location, stage)
        for entry in record['entries']:
            move_no_replace(Path(entry['stage']), Path(entry['source']))
        record['state'] = 'recovered'
        self.save(record)

    def apply(self, entries, kind='rename', undo_of=None):
        if self.pending():
            raise RenameError('Recover the interrupted operation before renaming more files.')
        if not entries or any(e.error for e in entries):
            raise RenameError('Fix every preview error before applying.')
        changed = [e for e in entries if e.changed]
        if not changed:
            raise RenameError('There are no filename changes to apply.')
        for entry in changed:
            if signature(entry.source) != tuple(entry.signature):
                raise RenameError('A source file changed. Refresh the preview before applying.')
                                                                                
        occupied = {e.source for e in changed}
        if len({path_key(e.target) for e in changed}) != len(changed):
            raise RenameError('Duplicate destinations.')
        for entry in changed:
            if not valid_name(entry.target.name) or entry.target.parent != entry.source.parent:
                raise RenameError('Renames must stay inside their original folders.')
            if os.path.lexists(entry.target) and entry.target not in occupied:
                if path_key(entry.source) != path_key(entry.target) or not os.path.samefile(entry.source, entry.target):
                    raise RenameError(f'Destination now exists: {entry.target.name}')
        identifier = uuid.uuid4().hex
        record = {'version': 1, 'id': identifier, 'kind': kind, 'undo_of': undo_of,
                  'created_at': datetime.now(timezone.utc).isoformat(), 'state': 'pending',
                  'entries': [{'source': str(e.source), 'target': str(e.target),
                               'stage': str(e.source.with_name(f'.bulk-rename-{identifier}-{i}.tmp')),
                               'signature': list(e.signature)} for i, e in enumerate(changed)]}
        self.save(record)                                                        
        try:
            for entry in record['entries']:
                if signature(Path(entry['source'])) != tuple(entry['signature']):
                    raise RenameError('A file changed during the rename.')
                move_no_replace(Path(entry['source']), Path(entry['stage']))
            for entry in record['entries']:
                move_no_replace(Path(entry['stage']), Path(entry['target']))
            record['state'] = 'complete'
            self.save(record)
            return record
        except Exception as exc:
            try:
                self.rollback(record)
            except Exception as recovery:
                raise RenameError(f'Rename interrupted. Recovery is needed: {recovery}\nHistory: {self.folder}') from exc
            raise RenameError(f'Rename cancelled and original names restored: {exc}') from exc

    def undo(self):
        records = [r for r in self.records() if r['kind'] == 'rename' and r['state'] == 'complete']
        if not records:
            raise RenameError('There is no completed batch to undo.')
        original = records[-1]
        entries = []
        for item in original['entries']:
            source = Path(item['target'])
            if signature(source) != tuple(item['signature']):
                raise RenameError(f'File changed since renaming: {source.name}. Undo was not applied.')
            entries.append(Entry(source, Path(item['source']), tuple(item['signature'])))
        result = self.apply(entries, kind='undo', undo_of=original['id'])
        original['state'] = 'undone'
        self.save(original)
        return result

    def recover(self):
        records = self.pending()
        for record in reversed(records):
            self.rollback(record)
        return len(records)
