import errno
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gui'))
import patch_engine as e

class PatcherTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.folder=Path(self.dir.name)
        self.before=b'0123456789'*1000
        self.after=self.before[:30]+b'HANGUL'+self.before[36:]
        self.source=self.folder/'원본 게임.iso';self.source.write_bytes(self.before)
        self.output=self.folder/'결과.iso'
        self.patch=self.folder/'patch.gz'
        self.data={'format':'ff2-ko-patch-v1','source_size':len(self.before),
                   'source_sha256':hashlib.sha256(self.before).hexdigest(),
                   'hunks':[{'iso_offset':30,'before':self.before[30:36].hex(),'after':b'HANGUL'.hex()}]}
        self.save_patch()
    def tearDown(self):self.dir.cleanup()
    def save_patch(self):
        with gzip.open(self.patch,'wt') as f:json.dump(self.data,f)
        self.options={'patch_digest':hashlib.sha256(self.patch.read_bytes()).hexdigest(),
                      'output_digest':hashlib.sha256(self.after).hexdigest()}
    def apply(self,**kwargs):return e.apply(self.source,self.patch,self.output,**self.options,**kwargs)
    def assert_clean(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.ff2-ko-*.building')),[])
        self.assertEqual(self.source.read_bytes(),self.before)
    def test_exact_patch_source_preserved_progress_complete(self):
        events=[];self.apply(progress=lambda stage,pct:events.append(pct))
        self.assertEqual(self.output.read_bytes(),self.after)
        self.assertEqual(self.source.read_bytes(),self.before)
        self.assertEqual(events[-1],100)
        self.assertEqual(events,sorted(events))
    def test_wrong_iso_rejected_before_output_creation(self):
        self.source.write_bytes(b'X'*len(self.before))
        with self.assertRaisesRegex(e.PatchError,'SHA-256'):self.apply()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.folder.glob('*.building')))
    def test_wrong_size(self):
        self.source.write_bytes(b'wrong')
        with self.assertRaisesRegex(e.PatchError,'크기'):self.apply()
        self.assertFalse(self.output.exists())
    def test_existing_output_preserved(self):
        self.output.write_bytes(b'my existing save')
        with self.assertRaises(e.PatchError):self.apply()
        self.assertEqual(self.output.read_bytes(),b'my existing save')
    def test_source_cannot_be_output(self):
        self.output=self.source
        with self.assertRaises(e.PatchError):self.apply()
        self.assertEqual(self.source.read_bytes(),self.before)
    def test_output_symlink_preserved(self):
        try:self.output.symlink_to(self.source)
        except OSError:self.skipTest('Symlinks unavailable')
        with self.assertRaises(e.PatchError):self.apply()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(self.source.read_bytes(),self.before)
    def test_patch_damage(self):
        self.patch.write_bytes(b'damaged')
        with self.assertRaisesRegex(e.PatchError,'손상'):self.apply()
        self.assert_clean()
    def test_invalid_negative_span(self):
        self.data['hunks'][0]['iso_offset']=-1;self.save_patch()
        with self.assertRaisesRegex(e.PatchError,'구조'):self.apply()
        self.assert_clean()
    def test_overlap_rejected(self):
        self.data['hunks'].append(dict(self.data['hunks'][0]));self.save_patch()
        with self.assertRaises(e.PatchError):self.apply()
        self.assert_clean()
    def test_span_beyond_end(self):
        self.data['hunks'][0]['iso_offset']=len(self.before)-1;self.save_patch()
        with self.assertRaises(e.PatchError):self.apply()
        self.assert_clean()
    def test_different_length_span(self):
        self.data['hunks'][0]['after']='ff';self.save_patch()
        with self.assertRaises(e.PatchError):self.apply()
        self.assert_clean()
    def test_before_bytes_mismatch_cleanup(self):
        self.data['hunks'][0]['before']=b'WRONG!'.hex();self.save_patch()
        with self.assertRaisesRegex(e.PatchError,'변경 구간'):self.apply()
        self.assert_clean()
    def test_output_hash_mismatch_cleanup(self):
        self.options['output_digest']='0'*64
        with self.assertRaisesRegex(e.PatchError,'결과'):self.apply()
        self.assert_clean()
    def test_cancel_during_write_cleans_temp(self):
        cancel=threading.Event()
        def progress(stage,pct):
            if pct>=30:cancel.set()
        with self.assertRaises(e.Cancelled):self.apply(cancel=cancel,progress=progress)
        self.assert_clean()
    def test_cancel_during_verification(self):
        cancel=threading.Event()
        def progress(stage,pct):
            if pct>=80:cancel.set()
        with self.assertRaises(e.Cancelled):self.apply(cancel=cancel,progress=progress)
        self.assert_clean()
    def test_source_changes_during_application(self):
        def progress(stage,pct):
            if pct==30:self.source.write_bytes(b'X'*len(self.before))
        with self.assertRaises(e.PatchError):self.apply(progress=progress)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.folder.glob('*.building')))
    def test_low_disk_rejected(self):
        with mock.patch.object(e.shutil,'disk_usage',return_value=type('Usage',(),{'free':1})()):
            with self.assertRaisesRegex(e.PatchError,'공간'):self.apply()
        self.assert_clean()
    def test_non_overwrite_collision_at_commit(self):
        def progress(stage,pct):
            if pct==97:self.output.write_bytes(b'another file')
        with self.assertRaises(e.PatchError):self.apply(progress=progress)
        self.assertEqual(self.output.read_bytes(),b'another file')
        self.assertFalse(list(self.folder.glob('*.building')))
    def test_exfat_fallback_copy(self):
        with mock.patch.object(e.os,'link',side_effect=OSError(errno.EOPNOTSUPP,'unsupported')):
            self.apply()
        self.assertEqual(self.output.read_bytes(),self.after)
    def test_fallback_cancel_removes_partial(self):
        temp=self.folder/'temporary';temp.write_bytes(b'T'*100)
        cancel=threading.Event()
        with mock.patch.object(e.os,'link',side_effect=OSError(errno.EOPNOTSUPP,'unsupported')):
            with self.assertRaises(e.Cancelled):
                e.publish_no_replace(temp,self.output,cancel,lambda f:cancel.set())
        self.assertFalse(self.output.exists())
    def test_next_output_avoids_existing_names(self):
        one=e.next_output(self.source);one.write_bytes(b'existing')
        two=e.next_output(self.source);two.write_bytes(b'existing')
        three=e.next_output(self.source)
        self.assertIn('(3)',three.name)
        self.assertNotEqual(one,two)
    def test_sparse_file_over_two_gib(self):
        # Exercise 64-bit offsets and bounded streaming with a sparse fixture.
        pos=(2**31)+11
        with self.source.open('wb') as f:f.seek(pos);f.write(b'AB')
        original=e.file_hash(self.source)
        self.data={'format':'ff2-ko-patch-v1','source_size':pos+2,'source_sha256':original,
                   'hunks':[{'iso_offset':pos,'before':b'AB'.hex(),'after':b'KO'.hex()}]}
        with self.source.open('r+b') as f:f.seek(pos);f.write(b'KO')
        expected=e.file_hash(self.source)
        with self.source.open('r+b') as f:f.seek(pos);f.write(b'AB')
        self.save_patch();self.options['output_digest']=expected
        self.apply()
        with self.output.open('rb') as f:f.seek(pos);self.assertEqual(f.read(),b'KO')
        self.assertEqual(e.file_hash(self.source),original)

if __name__=='__main__':unittest.main()
