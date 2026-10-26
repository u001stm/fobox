import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from engine import Engine


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.source=self.root/'card';self.source.mkdir()
        for i in range(3):Image.new('RGB',(240,360),(80+i*30,20,50)).save(self.source/f'ANT{i:05}.jpg',quality=90)
        self.e=Engine(self.root/'catalog'/'test.sqlite3')
        self.sid=self.e.import_folder(self.source,{'title':'Offline test'})
        self.paths=[str(p) for p in sorted(self.source.glob('*.jpg'))]
        for p in self.paths:self.assertTrue(self.e.preview_bytes(self.sid,p,1280))

    def tearDown(self):
        self.e.offline.close();self.tmp.cleanup()

    def disconnect(self):
        destination=self.root/'detached'
        assert self.source.resolve().is_relative_to(self.root) and destination.resolve().is_relative_to(self.root)
        self.source.rename(destination)
        self.e.offline._cache.clear()
        return destination

    def test_offline_previews_and_selection_survive_restart(self):
        section=self.e.session(self.sid)['sections'][0]
        self.e.review(self.sid,{'section':section['id'],'selected':[self.paths[1]],'approved':True})
        before=self.e.preview_bytes(self.sid,self.paths[1],480)
        snapshot=self.e.offline.snapshot(self.sid)
        self.disconnect()
        reopened=Engine(self.e.db_path)
        self.assertEqual(reopened.preview_bytes(self.sid,self.paths[1],480),before)
        restored=reopened.session(self.sid)
        self.assertIn(self.paths[1],restored['selected'][section['id']])
        self.assertTrue(restored['approved'][section['id']])
        self.assertTrue(all(f['has_preview'] and f['gone'] for f in restored['files']))
        self.assertTrue(reopened.offline.state(self.sid)['ready'])
        state=json.loads(Path(snapshot).read_text('utf-8'))
        self.assertIn('selections',state['tables']);self.assertNotIn('settings',state['tables'])

    def test_wrong_card_at_same_path_never_replaces_cached_photo(self):
        before=self.e.preview_bytes(self.sid,self.paths[0],480)
        self.disconnect();self.source.mkdir()
        for i in range(3):Image.new('RGB',(240,360),(10,160+i*20,30)).save(self.source/f'ANT{i:05}.jpg',quality=90)
        self.assertFalse(self.e.offline.state(self.sid)['online'])
        self.assertEqual(self.e.preview_bytes(self.sid,self.paths[0],480),before)
        self.assertFalse(self.e.offline.available(self.sid,self.paths[0],fresh=True))
        with self.assertRaisesRegex(ValueError,'другая'):
            self.e.offline.relink(self.sid,self.source)

    def test_relink_another_drive_and_export_real_original(self):
        section=self.e.session(self.sid)['sections'][0]
        self.e.review(self.sid,{'section':section['id'],'selected':[self.paths[0]],'approved':True})
        moved=self.disconnect()
        self.e.offline.relink(self.sid,moved)
        self.assertTrue(self.e.offline.available(self.sid,self.paths[0],fresh=True))
        self.assertEqual(Path(self.e.offline.resolve(self.sid,self.paths[0])),moved/'ANT00000.jpg')
        result=self.e.export(self.sid,self.root/'delivery')
        delivered=list(Path(result['root']).rglob('*.jpg'))
        self.assertTrue(delivered)
        self.assertEqual(delivered[0].read_bytes(),(moved/'ANT00000.jpg').read_bytes())

    def test_partial_card_cannot_be_linked(self):
        partial=self.root/'partial';partial.mkdir();shutil.copy2(self.paths[0],partial/'ANT00000.jpg')
        with self.assertRaises(ValueError):self.e.offline.relink(self.sid,partial)

    def test_removal_and_reinsert_generate_one_arrival_each(self):
        with patch.object(self.e.offline,'candidates',side_effect=lambda:[self.source] if self.source.exists() else []),patch.object(self.e.offline,'warm'):
            self.e.offline.scan();self.assertEqual(len(self.e.offline.events),1)
            self.assertEqual(self.e.offline.events[-1]['sessions'][0]['id'],self.sid)
            self.e.offline.scan();self.assertEqual(len(self.e.offline.events),1)
            moved=self.disconnect();self.e.offline.scan()
            assert moved.resolve().is_relative_to(self.root) and self.source.resolve().is_relative_to(self.root)
            moved.rename(self.source)
            self.e.offline.scan();self.assertEqual(len(self.e.offline.events),2)

    def test_passport_is_optional_and_does_not_replace_another_shoot(self):
        self.assertFalse((self.source/'.LUMIO.json').exists())
        result=self.e.offline.passport(self.sid)
        marker=Path(result['path']);self.assertEqual(json.loads(marker.read_text('utf-8'))['session_id'],self.sid)
        marker.write_text(json.dumps({'session_id':'other'}),'utf-8')
        with self.assertRaises(ValueError):self.e.offline.passport(self.sid)
        self.assertEqual(json.loads(marker.read_text())['session_id'],'other')


if __name__=='__main__':unittest.main()
