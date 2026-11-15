
from datetime import datetime
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import renamer
from renamer import Entry, History, RenameError, Rules, move_no_replace, preview, signature


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.history=History(self.root/'history')

    def file(self,name,content=None):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content if content is not None else ('contents of '+name).encode())
        return path

    def test_default_preview_does_not_modify_files(self):
        path=self.file('Report.PDF');before=path.read_bytes()
        entries=preview([path],Rules())
        self.assertFalse(entries[0].changed)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.history.records(),[])

    def test_rules_preserve_extension_and_numbering(self):
        a=self.file('IMG one.JPG');b=self.file('IMG two.JPG')
        rules=Rules(find='IMG ',replace='',prefix='trip_',suffix='_edited',case='lower',number=True,start=5,digits=3,lowercase_extension=True)
        entries=preview([a,b],rules)
        self.assertEqual([e.target.name for e in entries],['trip_one_edited_005.jpg','trip_two_edited_006.jpg'])
        self.assertTrue(all(not e.error for e in entries))

    def test_literal_replace_and_hidden_or_extensionless_files(self):
        paths=[self.file('my.file.txt'),self.file('.env'),self.file('README')]
        entries=preview(paths,Rules(find='.',replace='-',prefix='x_'))
        self.assertEqual([e.target.name for e in entries],['x_my-file.txt','x_-env','x_README'])

    def test_date_prefix_uses_file_modified_time(self):
        path=self.file('photo.jpg');timestamp=datetime(2024,3,12,12).timestamp()
        os.utime(path,(timestamp,timestamp))
        self.assertEqual(preview([path],Rules(modified_date=True))[0].target.name,'2024-03-12_photo.jpg')

    def test_existing_destination_is_a_preview_error(self):
        source=self.file('one.txt');other=self.file('new_one.txt',b'keep')
        entry=preview([source],Rules(prefix='new_'))[0]
        self.assertIn('already exists',entry.error)
        with self.assertRaises(RenameError):self.history.apply([entry])
        self.assertEqual(other.read_bytes(),b'keep')

    def test_duplicate_destinations_flag_every_file(self):
        a=self.file('a.txt');b=self.file('b.txt')
        entries=preview([a,b],Rules(find='a',replace='b'))
        self.assertTrue(all(e.error for e in entries))
        with self.assertRaises(RenameError):self.history.apply(entries)

    def test_invalid_and_reserved_names(self):
        path=self.file('name.txt')
        for rules in [Rules(prefix='../'),Rules(prefix='bad:'),Rules(find='name',replace='CON')]:
            with self.subTest(rules=rules):self.assertTrue(preview([path],rules)[0].error)

    def test_change_custom_extension_with_or_without_dot(self):
        source=self.file('document.txt',b'original file contents')
        for value,expected in [('pdf','document.pdf'),('.custom123','document.custom123'),('tar.gz','document.tar.gz')]:
            with self.subTest(value=value):
                entry=preview([source],Rules(new_extension=value))[0]
                self.assertEqual(entry.target.name,expected)
                self.assertFalse(entry.error)
        self.assertTrue(source.exists())

    def test_extension_change_keeps_bytes_and_undo_restores_name(self):
        source=self.file('image.jpg',b'These bytes are not a PDF')
        self.history.apply(preview([source],Rules(new_extension='.pdf')))
        renamed=self.root/'image.pdf'
        self.assertEqual(renamed.read_bytes(),b'These bytes are not a PDF')
        self.assertFalse(source.exists())
        self.history.undo()
        self.assertEqual(source.read_bytes(),b'These bytes are not a PDF')

    def test_remove_extension_and_add_to_extensionless_file(self):
        source=self.file('notes.txt')
        self.assertEqual(preview([source],Rules(new_extension=''))[0].target.name,'notes')
        plain=self.file('README')
        self.assertEqual(preview([plain],Rules(new_extension='md'))[0].target.name,'README.md')

    def test_extension_change_conflict_blocks_both_files(self):
        a=self.file('report.txt');b=self.file('report.csv')
        entries=preview([a,b],Rules(new_extension='pdf'))
        self.assertTrue(all(e.error for e in entries))
        with self.assertRaises(RenameError):self.history.apply(entries)
        self.assertTrue(a.exists());self.assertTrue(b.exists())

    def test_invalid_extension_is_rejected_before_mutation(self):
        source=self.file('notes.txt')
        for value in ['../pdf','bad/name','bad\\name','.', '???', 'pdf.']:
            with self.subTest(value=value):
                with self.assertRaises(RenameError):preview([source],Rules(new_extension=value))
        self.assertTrue(source.exists())

    def test_windows_case_only_names_are_changes(self):
        entry=Entry(PureWindowsPath('C:/files/report.txt'),PureWindowsPath('C:/files/REPORT.txt'),())
        self.assertTrue(entry.changed)

    def test_case_only_rename_and_undo(self):
        path=self.file('report.TXT',b'case test')
        self.history.apply(preview([path],Rules(case='upper')))
        renamed=self.root/'REPORT.TXT'
        self.assertEqual(renamed.read_bytes(),b'case test')
        History(self.history.folder).undo()
        self.assertEqual(path.read_bytes(),b'case test')

    def test_cycles_and_persistent_undo(self):
        a=self.file('a.txt',b'A');b=self.file('b.txt',b'B');c=self.file('c.txt',b'C')
        entries=[Entry(a,b,signature(a)),Entry(b,c,signature(b)),Entry(c,a,signature(c))]
        self.history.apply(entries)
        self.assertEqual([p.read_bytes() for p in (a,b,c)],[b'C',b'A',b'B'])
        restarted=History(self.history.folder);restarted.undo()
        self.assertEqual([p.read_bytes() for p in (a,b,c)],[b'A',b'B',b'C'])
        self.assertEqual(restarted.pending(),[])

    def test_undo_multiple_batches_in_reverse_order(self):
        a=self.file('file.txt')
        self.history.apply(preview([a],Rules(prefix='one_')))
        b=self.root/'one_file.txt'
        self.history.apply(preview([b],Rules(prefix='two_')))
        self.history.undo();self.assertTrue(b.exists())
        self.history.undo();self.assertTrue(a.exists())

    def test_undo_rejects_edited_file(self):
        path=self.file('a.txt');self.history.apply(preview([path],Rules(prefix='x_')))
        (self.root/'x_a.txt').write_bytes(b'changed after rename')
        with self.assertRaises(RenameError):self.history.undo()
        self.assertEqual((self.root/'x_a.txt').read_bytes(),b'changed after rename')
        self.assertFalse(path.exists())

    def test_undo_never_overwrites_new_original_name(self):
        path=self.file('a.txt',b'old');self.history.apply(preview([path],Rules(prefix='x_')))
        path.write_bytes(b'new unrelated file')
        with self.assertRaises(RenameError):self.history.undo()
        self.assertEqual(path.read_bytes(),b'new unrelated file')
        self.assertEqual((self.root/'x_a.txt').read_bytes(),b'old')

    def test_stale_preview_rejected_before_rename(self):
        path=self.file('a.txt');entries=preview([path],Rules(prefix='x_'))
        path.write_bytes(b'edited after preview')
        with self.assertRaises(RenameError):self.history.apply(entries)
        self.assertTrue(path.exists());self.assertFalse((self.root/'x_a.txt').exists())

    def test_destination_race_rejected_after_preview(self):
        path=self.file('a.txt');entries=preview([path],Rules(prefix='x_'))
        conflict=self.file('x_a.txt',b'unrelated')
        with self.assertRaises(RenameError):self.history.apply(entries)
        self.assertTrue(path.exists());self.assertEqual(conflict.read_bytes(),b'unrelated')

    def test_move_primitive_refuses_existing_destination(self):
        a=self.file('a.txt',b'A');b=self.file('b.txt',b'B')
        with self.assertRaises(OSError):move_no_replace(a,b)
        self.assertEqual(a.read_bytes(),b'A');self.assertEqual(b.read_bytes(),b'B')

    def test_move_primitive_refuses_dangling_symlink(self):
        a=self.file('a.txt',b'A');link=self.root/'dangling'
        try:link.symlink_to(self.root/'missing')
        except OSError:self.skipTest('Symlinks unavailable')
        with self.assertRaises(OSError):move_no_replace(a,link)
        self.assertTrue(a.exists());self.assertTrue(link.is_symlink())

    def test_failed_commit_rolls_back_all_files(self):
        a=self.file('a.txt',b'A');b=self.file('b.txt',b'B')
        actual=move_no_replace;counter=[0]
        def fail_once(source,target):
            counter[0]+=1
            if counter[0]==4:raise PermissionError('Simulated destination failure')
            actual(source,target)
        with patch.object(renamer,'move_no_replace',side_effect=fail_once):
            with self.assertRaisesRegex(RenameError,'original names restored'):
                self.history.apply(preview([a,b],Rules(prefix='x_')))
        self.assertEqual(a.read_bytes(),b'A');self.assertEqual(b.read_bytes(),b'B')
        self.assertFalse((self.root/'x_a.txt').exists());self.assertEqual(self.history.pending(),[])

    def test_interrupted_operation_can_be_recovered_after_restart(self):
        a=self.file('a.txt',b'A');b=self.file('b.txt',b'B')
        actual=move_no_replace;counter=[0]
        def interrupt(source,target):
            counter[0]+=1
            if counter[0]==4:raise KeyboardInterrupt('Simulated process stop')
            actual(source,target)
        with patch.object(renamer,'move_no_replace',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):self.history.apply(preview([a,b],Rules(prefix='x_')))
        restarted=History(self.history.folder)
        self.assertEqual(len(restarted.pending()),1)
        self.assertEqual(restarted.recover(),1)
        self.assertEqual(a.read_bytes(),b'A');self.assertEqual(b.read_bytes(),b'B')
        self.assertEqual(restarted.pending(),[])

    def test_unrelated_file_blocks_recovery_without_overwrite(self):
        path=self.file('a.txt',b'A')
        actual=move_no_replace
        def fail(source,target):
            if target.name=='x_a.txt':
                path.write_bytes(b'new file at original path')
                raise PermissionError('Simulated concurrent file creation')
            actual(source,target)
        with patch.object(renamer,'move_no_replace',side_effect=fail):
            with self.assertRaisesRegex(RenameError,'Recovery is needed'):
                self.history.apply(preview([path],Rules(prefix='x_')))
        self.assertEqual(path.read_bytes(),b'new file at original path')
        pending=self.history.pending();self.assertEqual(len(pending),1)
        self.assertEqual(Path(pending[0]['entries'][0]['stage']).read_bytes(),b'A')
        path.unlink();self.history.recover();self.assertEqual(path.read_bytes(),b'A')

    def test_symlink_source_and_hardlink_selection_are_rejected(self):
        a=self.file('a.txt');link=self.root/'link.txt';hard=self.root/'hard.txt'
        try:link.symlink_to(a);os.link(a,hard)
        except OSError:self.skipTest('Links unavailable')
        with self.assertRaises(RenameError):preview([link],Rules(prefix='x_'))
        self.assertTrue(all(e.error for e in preview([a,hard],Rules(prefix='x_'))))

    def test_corrupt_history_is_reported(self):
        (self.history.folder/'bad.json').write_text('not json')
        with self.assertRaisesRegex(RenameError,'Cannot read history'):self.history.records()

    def test_failed_journal_write_keeps_sources(self):
        path=self.file('a.txt',b'A')
        with patch.object(self.history,'save',side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):self.history.apply(preview([path],Rules(prefix='x_')))
        self.assertEqual(path.read_bytes(),b'A');self.assertFalse((self.root/'x_a.txt').exists())


if __name__=='__main__':unittest.main()
