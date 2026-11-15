from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid
import zipfile

from renamer import RenameError, signature, valid_name, move_no_replace

CHUNK = 1024 * 1024
MAX_EXTRACT_BYTES = 10 * 1024 * 1024 * 1024
MAX_ENTRIES = 10000


def member_parts(name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise RenameError('The archive contains an unsafe path.')
    parts = name.rstrip('/').split('/')
    if any(not valid_name(part) for part in parts):
        raise RenameError(f'The archive contains an unsafe or unsupported path: {name}')
    return parts


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            result.update(block)
    return result.hexdigest()


def archive_record(kind, entries):
    return {'version': 1, 'id': uuid.uuid4().hex, 'kind': kind, 'undo_of': None,
            'created_at': datetime.now(timezone.utc).isoformat(), 'state': 'prepared', 'entries': entries}


def create_zip(paths, destination, history):
    sources = list(dict.fromkeys(Path(p).resolve(strict=True) for p in paths))
    if not sources:
        raise RenameError('Check at least one file to create a ZIP.')
    for raw in paths:
        if Path(raw).is_symlink():
            raise RenameError('ZIP creation does not follow symbolic links.')
    identities = {path: signature(path) for path in sources}
    raw_target = Path(destination).expanduser().absolute()
    target = raw_target.parent.resolve(strict=True) / raw_target.name
    if target.suffix.lower() != '.zip':
        target = target.with_name(target.name + '.zip')
    if os.path.lexists(target):
        raise RenameError('The ZIP destination already exists. Choose a new filename.')
    try:
        common = Path(os.path.commonpath([str(path.parent) for path in sources]))
    except ValueError as exc:
        raise RenameError('Select files from the same filesystem volume for one ZIP.') from exc
    names = {path: path.relative_to(common).as_posix() for path in sources}
    if len({name.casefold() for name in names.values()}) != len(names):
        raise RenameError('Selected files have conflicting archive paths ignoring case.')
    for name in names.values():
        member_parts(name)
    fd, staged_name = tempfile.mkstemp(dir=target.parent, prefix='.zip-', suffix='.partial')
    os.close(fd)
    staged = Path(staged_name)
    entries = []
    published = False
    try:
        hashes = {path: digest(path) for path in sources}
        with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True, strict_timestamps=False) as archive:
            for path in sources:
                archive.write(path, names[path])
        with zipfile.ZipFile(staged) as archive:
            for path in sources:
                result = hashlib.sha256()
                with archive.open(names[path]) as stream:
                    for block in iter(lambda: stream.read(CHUNK), b''):
                        result.update(block)
                if result.hexdigest() != hashes[path] or signature(path) != identities[path]:
                    raise RenameError(f'A source changed during compression: {path.name}. Originals were kept.')
                entries.append({'source': str(path), 'target': str(target), 'member': names[path],
                                'signature': list(identities[path]), 'sha256': hashes[path], 'size': identities[path][2]})
        with staged.open('rb+') as stream:
            os.fsync(stream.fileno())
        record = archive_record('zip', entries)
        history.save(record)
        move_no_replace(staged, target)
        published = True
        record['state'] = 'complete'
        history.save(record)
        return {'path': str(target), 'files': len(entries), 'original_bytes': sum(e['size'] for e in entries),
                'archive_bytes': target.stat().st_size,
                'message': f'Created verified ZIP with {len(entries)} files: {target}. Original files were kept.'}
    except Exception as exc:
        if published:
            raise RenameError(f'The ZIP was created at {target}, but recording completion failed: {exc}') from exc
        raise
    finally:
        staged.unlink(missing_ok=True)


def validate_zip(archive):
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise RenameError(f'A ZIP can contain at most {MAX_ENTRIES:,} entries in this tool.')
    total = sum(info.file_size for info in entries)
    if total > MAX_EXTRACT_BYTES:
        raise RenameError('This ZIP exceeds the 10 GiB extraction limit.')
    seen, prefixes, files = set(), {}, set()
    for info in entries:
        parts = member_parts(info.orig_filename)
        if info.orig_filename != info.filename:
            raise RenameError('The ZIP contains a normalized or truncated unsafe path.')
        normalized = '/'.join(parts).casefold()
        if normalized in seen:
            raise RenameError('ZIP contains duplicate or case-conflicting paths.')
        seen.add(normalized)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or mode == stat.S_IFDIR and not info.is_dir():
            raise RenameError('ZIP links and special filesystem entries are not supported.')
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise RenameError('Only Stored and Deflate ZIP compression methods are supported.')
        if info.flag_bits & 1:
            raise RenameError('Password-protected ZIP files are not supported.')
        for length in range(1, len(parts) + 1):
            text = '/'.join(parts[:length])
            key = text.casefold()
            if key in prefixes and prefixes[key] != text:
                raise RenameError('ZIP paths differ only by letter case.')
            prefixes[key] = text
        if not info.is_dir():
            files.add(normalized)
    for info in entries:
        parts = member_parts(info.filename)
        if any('/'.join(parts[:i]).casefold() in files for i in range(1, len(parts))):
            raise RenameError('A ZIP file path is also used as a parent folder.')
    return entries, total


def extract_zip(source, destination, history):
    source = Path(source).expanduser().absolute()
    identity = signature(source)
    parent = Path(destination).expanduser().resolve(strict=True)
    if not parent.is_dir():
        raise RenameError('Choose an existing extraction folder.')
    base = source.stem if valid_name(source.stem) else 'archive'
    target = parent / (base + '-extracted-' + uuid.uuid4().hex[:8])
    staged = None
    published = False
    try:
        with zipfile.ZipFile(source) as archive:
            members, total = validate_zip(archive)
            if shutil.disk_usage(parent).free < total + CHUNK:
                raise RenameError('Not enough free space to extract this ZIP.')
            staged = Path(tempfile.mkdtemp(dir=parent, prefix='.unzip-'))
            entries = []
            for info in members:
                parts = member_parts(info.filename)
                output = staged.joinpath(*parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                result = hashlib.sha256()
                written = 0
                with archive.open(info) as src, output.open('xb') as dst:
                    for block in iter(lambda: src.read(CHUNK), b''):
                        written += len(block)
                        if written > info.file_size:
                            raise RenameError('The ZIP expanded beyond its declared file size.')
                        result.update(block)
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                if written != info.file_size or digest(output) != result.hexdigest():
                    raise RenameError('Extracted-file verification failed.')
                entries.append({'source': str(source), 'target': str(target.joinpath(*parts)), 'member': info.filename,
                                'signature': list(identity), 'sha256': result.hexdigest(), 'size': written})
        if signature(source) != identity:
            raise RenameError('The ZIP changed during extraction. The temporary output was discarded.')
        record = archive_record('unzip', entries)
        record['output_folder'] = str(target)
        history.save(record)
        move_no_replace(staged, target)
        published = True
        record['state'] = 'complete'
        history.save(record)
        return {'path': str(target), 'files': len(entries),
                'message': f'Extracted and verified {len(entries)} files into {target}. The original ZIP was kept.'}
    except zipfile.BadZipFile as exc:
        raise RenameError(f'{source.name} is not a valid ZIP archive. Renaming an extension does not create a ZIP.') from exc
    except Exception as exc:
        if published:
            raise RenameError(f'Files were extracted to {target}, but recording completion failed: {exc}') from exc
        raise
    finally:
        if staged is not None and staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
