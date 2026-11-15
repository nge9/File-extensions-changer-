import csv
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Session
from renamer import RenameError


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'files'
        self.folder.mkdir()
        self.session = Session(self.root / 'history')

    def file(self, name, content=b'unchanged contents'):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path.resolve()

    def test_folder_load_and_only_checked_file_changes(self):
        one = self.file('one.txt', b'one')
        two = self.file('two.txt', b'two')
        self.session.add([self.folder])
        self.assertEqual(len(self.session.snapshot()['files']), 2)
        plan = self.session.make_preview([str(one)], 'custom')
        result = self.session.apply(plan['preview_id'])
        self.assertEqual((self.folder / 'one.custom').read_bytes(), b'one')
        self.assertEqual(two.read_bytes(), b'two')
        self.assertEqual(result['state']['history_total'], 1)

    def test_per_file_history_persists_with_undo_status(self):
        paths = [self.file('a.txt'), self.file('b.txt')]
        self.session.add(paths)
        plan = self.session.make_preview(list(map(str, paths)), 'log')
        self.session.apply(plan['preview_id'])
        before = self.session.snapshot()
        self.assertEqual(before['history_total'], 2)
        self.assertTrue(all(r['state'] == 'complete' for r in before['history']))
        self.session.undo()
        restarted = Session(self.session.history.folder)
        rows = restarted.snapshot()['history']
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r['operation'] == 'Undo' for r in rows), 2)
        self.assertEqual(sum(r['state'] == 'undone' for r in rows), 2)
        self.assertTrue(all(p.exists() for p in paths))

    def test_no_extension_typed_does_not_remove_it(self):
        path = self.file('a.txt')
        self.session.add([path])
        plan = self.session.make_preview([str(path)], '')
        self.assertFalse(plan['can_apply'])
        self.assertTrue(plan['needs_extension'])
        self.assertFalse(plan['entries'][0]['changed'])
        with self.assertRaises(RenameError):
            self.session.apply(plan['preview_id'])
        plan = self.session.make_preview([str(path)], '', True)
        self.session.apply(plan['preview_id'])
        self.assertTrue((self.folder / 'a').is_file())

    def test_stale_preview_id_is_rejected(self):
        path = self.file('a.txt')
        self.session.add([path])
        first = self.session.make_preview([str(path)], 'log')
        self.session.make_preview([str(path)], 'md')
        with self.assertRaises(RenameError):
            self.session.apply(first['preview_id'])
        self.assertTrue(path.exists())

    def test_unloaded_paths_are_rejected(self):
        path = self.file('a.txt')
        with self.assertRaises(RenameError):
            self.session.make_preview([str(path)], 'log')
        with self.assertRaises(RenameError):
            self.session.zip_files([str(path)], self.root / 'output.zip')

    def test_recursive_folder_loading_and_list_removal(self):
        top = self.file('a.txt')
        nested = self.file('nested/b.txt')
        self.session.add([self.folder])
        self.assertEqual(self.session.files, [top])
        self.session.add([self.folder], recursive=True)
        self.assertEqual(set(self.session.files), {top, nested})
        self.session.remove([str(top)])
        self.assertTrue(top.exists())
        self.assertEqual(self.session.files, [nested])
        self.session.clear()
        self.assertTrue(nested.exists())

    def test_alias_input_paths_match_the_resolved_selection(self):
        raw = self.folder / 'raw-input.txt'
        raw.write_bytes(b'file at the OS temporary path')
        self.session.add([raw])
        plan = self.session.make_preview([str(raw)], 'custom')
        self.assertEqual(plan['entries'][0]['source'], str(raw.resolve()))
        self.session.apply(plan['preview_id'])
        self.assertTrue(raw.with_suffix('.custom').exists())

    def test_internal_history_files_are_not_loaded(self):
        path = self.file('a.txt')
        self.session.add([path])
        plan = self.session.make_preview([str(path)], 'custom')
        self.session.apply(plan['preview_id'])
        self.session.clear()
        result = self.session.add([self.session.history.folder], recursive=True)
        self.assertEqual(result['state']['files'], [])
        self.assertEqual(result['state']['history_total'], 1)
        self.assertIn('protected history', result['message'])

    def test_file_limit_does_not_modify_sources(self):
        paths = [self.file(f'{i}.txt') for i in range(5)]
        session = Session(self.root / 'limited-history', max_files=3)
        result = session.add([self.folder])
        self.assertEqual(len(session.files), 3)
        self.assertIn('limit', result['message'])
        self.assertTrue(all(p.exists() for p in paths))

    def test_export_history_and_no_overwrite(self):
        path = self.file('=formula.txt')
        self.session.add([path])
        plan = self.session.make_preview([str(path)], 'md')
        self.session.apply(plan['preview_id'])
        destination = self.root / 'history-export.csv'
        self.session.export_history(destination)
        with destination.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][3], "'=formula.txt")
        before = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            self.session.export_history(destination)
        self.assertEqual(destination.read_bytes(), before)

    def test_zip_and_unzip_history_has_a_record_per_file(self):
        paths = [self.file('a.txt', b'A' * 5000), self.file('b.py', b'print(1)\n')]
        self.session.add(paths)
        result = self.session.zip_files(list(map(str, paths)), self.root / 'selected.zip')
        archive = Path(result['path'])
        self.assertTrue(archive in self.session.files)
        self.assertEqual(result['state']['history_total'], 2)
        destination = self.root / 'output'
        destination.mkdir()
        result = self.session.unzip_files([str(archive)], destination)
        self.assertEqual(result['state']['history_total'], 4)
        self.assertEqual(len(result['results']), 1)
        output = Path(result['results'][0]['path'])
        self.assertEqual((output / 'a.txt').read_bytes(), b'A' * 5000)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertTrue(archive.exists())

    def test_unzip_rejects_renamed_non_zip_before_extracting(self):
        path = self.file('fake.zip', b'not a ZIP')
        self.session.add([path])
        destination = self.root / 'output'
        destination.mkdir()
        with self.assertRaisesRegex(RenameError, 'not a ZIP'):
            self.session.unzip_files([str(path)], destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(path.read_bytes(), b'not a ZIP')


if __name__ == '__main__':
    unittest.main()
