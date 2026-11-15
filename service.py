import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import uuid
import zipfile

from archives import create_zip, extract_zip

from renamer import Rules, History, RenameError, preview, path_key


class Session:
    def __init__(self, history_folder, max_files=10000):
        self.history = History(history_folder)
        self.archive_history = History(self.history.folder / "archive-operations")
        self.files = []
        self.max_files = max_files
        self.plan_id = None
        self.plan = []
        self.can_apply = False

    def invalidate(self):
        self.plan_id = None
        self.plan = []
        self.can_apply = False

    def add(self, paths, recursive=False):
        existing = set(self.files)
        added, skipped, protected, warnings = 0, 0, 0, []
        limit_reached = False
        for raw in paths:
            root = Path(raw).expanduser().absolute()
            if root.is_symlink():
                skipped += 1
                continue
            try:
                candidates = root.rglob('*') if root.is_dir() and recursive else root.iterdir() if root.is_dir() else [root]
                for candidate in candidates:
                    if candidate.is_symlink():
                        skipped += 1
                        continue
                    if not candidate.is_file():
                        continue
                    path = candidate.resolve(strict=True)
                    if path.is_relative_to(self.history.folder):
                        protected += 1
                        continue
                    if path in existing:
                        continue
                    if len(existing) >= self.max_files:
                        limit_reached = True
                        break
                    existing.add(path)
                    self.files.append(path)
                    added += 1
            except OSError as exc:
                warnings.append(f'{root.name}: {exc}')
            if limit_reached:
                break
        self.invalidate()
        message = f'Added {added} files. Nothing has been renamed. Select the files you want to change.'
        if skipped:
            message += f' Skipped {skipped} symbolic links.'
        if protected:
            message += f' Skipped {protected} protected history files.'
        if limit_reached:
            message += f' The {self.max_files:,}-file selection limit was reached.'
        if warnings:
            message += '\n' + '\n'.join(warnings)
        return {'message': message, 'state': self.snapshot()}

    def remove(self, paths):
        selected = {Path(p).expanduser().resolve() for p in paths}
        self.files = [p for p in self.files if p not in selected]
        self.invalidate()
        return {'message': 'Removed items from the list. No files were deleted.', 'state': self.snapshot()}

    def clear(self):
        self.files = []
        self.invalidate()
        return {'message': 'List cleared. Your files and change history were kept.', 'state': self.snapshot()}

    def make_preview(self, paths, extension='', remove=False):
        if not isinstance(extension, str) or not isinstance(remove, bool):
            raise RenameError('Choose a valid extension or the remove-extension option.')
        selected = self.selected_paths(paths)
        rule = Rules(new_extension='' if remove else extension.strip() if extension.strip() else None)
        self.invalidate()
        self.plan = preview(selected, rule)
        self.plan_id = uuid.uuid4().hex
        changed = sum(entry.changed for entry in self.plan)
        conflicts = sum(bool(entry.error) for entry in self.plan)
        recovery = bool(self.history.pending())
        self.can_apply = bool(changed) and not conflicts and not recovery
        return {'preview_id': self.plan_id,
                'entries': [{'source': str(e.source), 'target': str(e.target), 'new_name': e.target.name,
                             'changed': e.changed, 'error': e.error} for e in self.plan],
                'changes': changed, 'conflicts': conflicts, 'can_apply': self.can_apply,
                'needs_extension': not remove and not extension.strip(), 'recovery_needed': recovery}

    def checked_plan(self, identifier):
        if not identifier or identifier != self.plan_id or not self.can_apply:
            raise RenameError('The preview is out of date or contains no valid changes. Refresh it first.')
        return list(self.plan)

    def apply(self, identifier):
        entries = self.checked_plan(identifier)
        self.invalidate()
        record = self.history.apply(entries)
        return self.changed(record, f'Changed {len(record["entries"])} file extensions. Contents were not converted. Undo is available.')

    def changed(self, record, message):
        mapping = {entry['source']: entry['target'] for entry in record['entries']}
        self.files = [Path(mapping.get(str(p), str(p))) for p in self.files]
        self.invalidate()
        return {'message': message, 'mapping': mapping, 'state': self.snapshot()}

    def undo(self):
        self.invalidate()
        record = self.history.undo()
        return self.changed(record, f'Restored the original names of {len(record["entries"])} files. History was kept.')

    def recover(self):
        self.invalidate()
        count = self.history.recover()
        self.files = [p for p in self.files if p.exists()]
        return {'message': f'Recovered {count} interrupted operations. Add restored files again if needed.', 'state': self.snapshot()}

    def history_rows(self, records=None):
        records = self.all_records() if records is None else records
        rows = []
        for record in reversed(records):
            for index, entry in enumerate(record['entries']):
                rows.append({'id': record['id'] + ':' + str(index), 'transaction': record['id'],
                             'time': record['created_at'], 'operation': {'undo': 'Undo', 'rename': 'Extension change', 'zip': 'Create ZIP', 'unzip': 'Unzip'}[record['kind']],
                             'state': record['state'], 'before': Path(entry['source']).name,
                             'after': (Path(entry['target']).name + ' :: ' + entry.get('member', '')) if record['kind'] == 'zip' else entry.get('member', Path(entry['target']).name) if record['kind'] == 'unzip' else Path(entry['target']).name, 'folder': str(Path(entry['source']).parent),
                             'source': entry['source'], 'target': entry['target']})
        return rows

    def snapshot(self):
        files = []
        for path in self.files:
            problem, size, modified = '', 0, None
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    problem = 'Not a regular file'
                size, modified = info.st_size, info.st_mtime
            except OSError:
                problem = 'Missing or unavailable'
            files.append({'path': str(path), 'name': path.name, 'folder': str(path.parent),
                          'extension': path.suffix.lower(), 'size': size, 'modified': modified, 'problem': problem})
        records = self.all_records()
        rows = self.history_rows(records)
        pending = sum(r['state'] in ('pending', 'recovery_required') for r in records)
        return {'files': files, 'history': rows[:1000], 'history_total': len(rows),
                'history_folder': str(self.history.folder), 'recovery_count': pending,
                'can_undo': not pending and any(r['kind'] == 'rename' and r['state'] == 'complete' for r in records)}

    def all_records(self):
        return sorted(self.history.records() + self.archive_history.records(), key=lambda record: record['created_at'])

    def selected_paths(self, paths):
        selected = []
        for raw in paths:
            path = Path(raw).expanduser().absolute()
            if path.is_symlink():
                raise RenameError('Symbolic links cannot be selected.')
            try:
                path = path.resolve(strict=True)
            except OSError as exc:
                raise RenameError(f'The selected file is missing or unavailable: {path.name}') from exc
            if path not in self.files:
                raise RenameError('Only files loaded into this window can be changed.')
            if path not in selected:
                selected.append(path)
        return selected

    def checked_files(self, paths):
        selected = self.selected_paths(paths)
        if not selected:
            raise RenameError('Check files that are loaded into this window first.')
        return selected

    def zip_files(self, paths, destination):
        selected = self.checked_files(paths)
        if Path(destination).expanduser().resolve().is_relative_to(self.history.folder):
            raise RenameError('Choose a ZIP destination outside the internal history folder.')
        result = create_zip(selected, destination, self.archive_history)
        created = Path(result['path']).resolve()
        if created not in self.files and len(self.files) < self.max_files:
            self.files.append(created)
        self.invalidate()
        result['state'] = self.snapshot()
        return result

    def unzip_files(self, paths, destination):
        selected = self.checked_files(paths)
        parent = Path(destination).expanduser().resolve(strict=True)
        if parent.is_relative_to(self.history.folder):
            raise RenameError('Choose an extraction folder outside the internal history folder.')
        for source in selected:
            if not zipfile.is_zipfile(source):
                raise RenameError(f'{source.name} is not a ZIP archive. Changing an extension does not convert its contents.')
        results, errors = [], []
        for source in selected:
            try:
                results.append(extract_zip(source, parent, self.archive_history))
            except Exception as exc:
                errors.append(f'{source.name}: {exc}')
        self.invalidate()
        return {'message': '\n\n'.join([r['message'] for r in results] + errors), 'results': results,
                'errors': errors, 'output_folder': str(parent), 'state': self.snapshot()}

    def export_history(self, destination):
        target = Path(destination).expanduser().absolute()
        if target.suffix.lower() != '.csv':
            target = target.with_name(target.name + '.csv')
        if target.resolve().is_relative_to(self.history.folder):
            raise RenameError('Choose a location outside the internal history folder.')
        protected = {path_key(p) for p in self.files} | {path_key(e.target) for e in self.plan}
        if path_key(target.resolve()) in protected:
            raise RenameError('The export cannot use a loaded file or a planned destination name.')
        rows = self.history_rows()
        with target.open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['time_utc', 'operation', 'state', 'old_name', 'new_name', 'folder', 'source', 'destination', 'transaction'])
            for row in rows:
                values = [row[k] for k in ('time', 'operation', 'state', 'before', 'after', 'folder', 'source', 'target', 'transaction')]
                writer.writerow(["'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in values])
        return {'message': f'Exported {len(rows)} history records to {target}.', 'path': str(target)}
