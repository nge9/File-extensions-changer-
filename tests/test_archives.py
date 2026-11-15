import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archives import create_zip, extract_zip, validate_zip
from renamer import History, RenameError


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.history = History(self.root / 'logs')
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.output.mkdir()

    def file(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def archive(self, name, members):
        path = self.root / name
        with zipfile.ZipFile(path, 'w') as archive:
            for entry, content in members:
                archive.writestr(entry, content)
        return path

    def test_compression_verification_and_relative_structure(self):
        a = self.file('a/readme.txt', b'A' * 10000)
        b = self.file('b/readme.txt', b'B' * 10000)
        result = create_zip([a, b], self.root / 'data.zip', self.history)
        self.assertLess(result['archive_bytes'], result['original_bytes'])
        with zipfile.ZipFile(result['path']) as archive:
            self.assertEqual(archive.read('a/readme.txt'), b'A' * 10000)
            self.assertEqual(archive.read('b/readme.txt'), b'B' * 10000)
        self.assertTrue(a.exists() and b.exists())
        record = self.history.records()[0]
        self.assertEqual(record['state'], 'complete')
        self.assertEqual(len(record['entries']), 2)

    def test_old_source_timestamps_are_supported(self):
        source = self.file('old.txt', b'old source bytes')
        os.utime(source, (0, 0))
        result = create_zip([source], self.root / 'old.zip', self.history)
        with zipfile.ZipFile(result['path']) as archive:
            self.assertEqual(archive.read('old.txt'), b'old source bytes')
            self.assertEqual(archive.getinfo('old.txt').date_time[0], 1980)

    def test_unsupported_compression_is_rejected_before_extracting(self):
        source = self.root / 'unusual.zip'
        with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_BZIP2) as archive:
            archive.writestr('a.txt', b'data')
        with self.assertRaisesRegex(RenameError, 'Stored and Deflate'):
            extract_zip(source, self.output, self.history)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_zip_destination_never_overwritten(self):
        source = self.file('a.txt', b'A')
        destination = self.root / 'keep.zip'
        destination.write_bytes(b'keep')
        with self.assertRaises(RenameError):
            create_zip([source], destination, self.history)
        self.assertEqual(destination.read_bytes(), b'keep')
        self.assertEqual(source.read_bytes(), b'A')

    def test_extract_creates_unique_folders_and_preserves_empty_directories(self):
        source = self.archive('data.zip', [('folder/', b''), ('folder/empty/', b''), ('folder/a.txt', b'A')])
        one = extract_zip(source, self.output, self.history)
        two = extract_zip(source, self.output, self.history)
        self.assertNotEqual(one['path'], two['path'])
        self.assertEqual((Path(one['path']) / 'folder/a.txt').read_bytes(), b'A')
        self.assertTrue((Path(one['path']) / 'folder/empty').is_dir())
        self.assertTrue(source.exists())

    def test_traversal_absolute_and_windows_paths_are_rejected(self):
        for index, name in enumerate(['../escape.txt', '/absolute.txt', 'C:/escape.txt', 'folder\\evil.txt']):
            source = self.archive(f'bad{index}.zip', [(name, b'bad')])
            with self.subTest(name=name):
                with self.assertRaises(RenameError):
                    extract_zip(source, self.output, self.history)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_symlink_archive_member_is_rejected(self):
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        source = self.archive('link.zip', [(info, b'../outside')])
        with self.assertRaises(RenameError):
            extract_zip(source, self.output, self.history)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_duplicate_case_and_parent_file_conflicts_are_rejected(self):
        examples = [[('A.txt', b'A'), ('a.txt', b'B')], [('folder', b'A'), ('folder/a.txt', b'B')]]
        for index, members in enumerate(examples):
            source = self.archive(f'conflict{index}.zip', members)
            with self.assertRaises(RenameError):
                extract_zip(source, self.output, self.history)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_extraction_limit_rejects_before_writing(self):
        source = self.archive('large.zip', [('a.txt', b'12345')])
        with patch('archives.MAX_EXTRACT_BYTES', 4):
            with self.assertRaisesRegex(RenameError, 'limit'):
                extract_zip(source, self.output, self.history)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_corrupt_archive_does_not_publish_partial_output(self):
        source = self.root / 'broken.zip'
        source.write_bytes(b'not a ZIP archive')
        with self.assertRaises(RenameError):
            extract_zip(source, self.output, self.history)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(source.exists())

    def test_no_output_published_when_history_write_fails(self):
        source = self.file('a.txt', b'A')
        destination = self.root / 'data.zip'
        with patch.object(self.history, 'save', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                create_zip([source], destination, self.history)
        self.assertFalse(destination.exists())
        self.assertTrue(source.exists())
        self.assertFalse(list(self.root.glob('.zip-*')))

    def test_linked_source_is_not_archived(self):
        source = self.file('a.txt', b'A')
        link = self.root / 'link.txt'
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest('Symbolic links unavailable')
        with self.assertRaises(RenameError):
            create_zip([link], self.root / 'data.zip', self.history)
        self.assertTrue(source.exists())


if __name__ == '__main__':
    unittest.main()
