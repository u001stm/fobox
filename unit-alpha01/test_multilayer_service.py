import json
from pathlib import Path
import shutil
import tempfile
import unittest
from PIL import Image
from engine import Engine
from lab_workspace import LabWorkspace,sha256
from ml_metadata import Cancelled
from unit_workspace import UnitWorkspace


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.engine=Engine(self.root/'catalog/lumio.sqlite3');self.unit=UnitWorkspace(self.engine)
        self.lab=LabWorkspace(self.engine,self.unit,autostart=False);self.ml=self.lab.multilayer
        self.sid=self.ml.create('Съёмка — тест')['session']
        self.source=self.root/'Карта с пробелами';self.source.mkdir()
        for i in range(2):
            exif=Image.Exif();exif[36867]=f'2026:10:26 14:{30+i*10:02d}:00';exif[271]='Test';exif[272]='Synthetic'
            Image.new('RGB',(40,60),(80+i*40,60,30)).save(self.source/f'ANT{i:05d}.jpg',exif=exif)
        state,revision=self.ml.state(self.sid)
        self.ml.save(self.sid,{'sources':[{'root':str(self.source),'device_id':'SONY-01'}]},revision,True)

    def tearDown(self):self.temp.cleanup()

    def task(self,operation,payload=None):
        out=self.ml.enqueue(self.sid,operation,payload or {});self.lab.run(out['job']);return self.lab.job(out['job'])

    def analyzed(self):
        j=self.task('analyze');self.assertEqual(j['status'],'complete',j);return self.ml.project(self.sid)

    def test_real_metadata_paths_and_repeated_analysis_preserve_review(self):
        before={p.name:sha256(p) for p in self.source.iterdir()}
        first=self.analyzed();assets=first['result']['assets'];self.assertEqual(len(assets),2)
        self.assertTrue(all(a['raw_timestamp'] for a in assets))
        self.ml.save(self.sid,{'selection':{assets[0]['asset_id']:'reject'},'markers':[{'id':'mark','asset_id':assets[0]['asset_id'],'text':'Момент'}]},first['revision'])
        again=self.analyzed()
        self.assertEqual(again['state']['review']['selection'][assets[0]['asset_id']],'reject')
        self.assertEqual(len(again['result']['markers']),1)
        self.assertEqual({p.name:sha256(p) for p in self.source.iterdir()},before)
        self.assertFalse(list(self.source.glob('*.xmp')))

    def test_working_copy_deletion_requires_explicit_reconcile(self):
        self.analyzed();j=self.task('working',{'naming':'anchors'});self.assertEqual(j['status'],'complete',j)
        folder=Path(j['result']['folder']);item=next((folder/'TIMELINE').glob('*.jpg'));item.unlink()
        missing=self.ml.reconcile(self.sid,folder)['missing'];self.assertEqual(len(missing),1)
        self.assertEqual(self.ml.state(self.sid)[0]['review']['selection'],{})
        self.assertEqual(len(list(self.source.glob('*.jpg'))),2)

    def test_prepared_photo_replaces_export_without_extra_capture(self):
        d=self.analyzed();asset=d['result']['assets'][0]
        prepared=self.root/'Обработанный.jpg';Image.new('RGB',(80,120),'white').save(prepared)
        self.ml.prepared_photo(self.sid,{'asset_id':asset['asset_id'],'path':str(prepared)},d['revision'])
        state,revision=self.ml.state(self.sid)
        self.ml.save(self.sid,{'selection':{asset['asset_id']:'keep'}},revision)
        job=self.task('final');self.assertEqual(job['status'],'complete',job)
        with Image.open(Path(job['result']['folder'])/'CLIENT/MEDIA/001.jpg') as image:self.assertEqual(image.size,(80,120))
        self.assertEqual(self.ml.latest(self.sid)['stats']['capture_count'],2)

    def test_external_trim_uses_parent_time_and_offset_once(self):
        from ml_metadata import tool,command
        from datetime import datetime
        exe=tool('ffmpeg',{})
        if not exe:self.skipTest('FFmpeg unavailable')
        video=self.source/'POV.mp4'
        command([exe,'-v','error','-y','-f','lavfi','-i','color=gray:s=64x96:r=10','-t','2','-c:v','libx264','-metadata','creation_time=2026-10-26T11:31:00Z',video])
        d=self.analyzed();parent=next(a for a in d['result']['assets'] if a['media_type']=='video')
        prepared=self.root/'trim.mp4'
        command([exe,'-v','error','-y','-i',video,'-ss','0.5','-t','1','-c:v','libx264',prepared])
        child=self.ml.derivative(self.sid,{'parent_asset_id':parent['asset_id'],'path':str(prepared),'trim_in_seconds':.5,'trim_out_seconds':1.5},d['revision'])
        state,revision=self.ml.state(self.sid);devices=state['config']['devices'];devices[0]['offset_seconds']=7
        self.ml.save(self.sid,{'devices':devices},revision,True)
        job=self.task('sync');self.assertEqual(job['status'],'complete',job)
        assets={a['asset_id']:a for a in self.ml.latest(self.sid)['assets']}
        delta=datetime.fromisoformat(assets[child['asset_id']]['corrected_start_utc'])-datetime.fromisoformat(assets[parent['asset_id']]['corrected_start_utc'])
        self.assertEqual(delta.total_seconds(),.5)

    def test_final_only_explicit_keep_flat_numbering_versions(self):
        d=self.analyzed();ids=[a['asset_id'] for a in d['result']['assets']]
        self.ml.save(self.sid,{'selection':{aid:'keep' for aid in ids}},d['revision'])
        first=self.task('final');self.assertEqual(first['status'],'complete',first)
        folder=Path(first['result']['folder']);self.assertEqual(sorted(p.name for p in (folder/'CLIENT/MEDIA').iterdir()),['001.jpg','002.jpg'])
        second=self.task('final');self.assertNotEqual(first['result']['folder'],second['result']['folder'])
        public=json.loads((folder/'CLIENT/event.public.json').read_text('utf-8'))
        self.assertNotIn(str(self.source),json.dumps(public))

    def test_offset_sync_uses_cache_and_keeps_asset_ids(self):
        d=self.analyzed();before={a['asset_id']:a['corrected_start_utc'] for a in d['result']['assets']}
        devices=d['state']['config']['devices'];devices[0]['offset_seconds']=7
        self.ml.save(self.sid,{'devices':devices},d['revision'],True)
        # A sync does not need the source directory to be readable.
        renamed=self.root/'hidden';self.source.rename(renamed)
        j=self.task('sync');self.assertEqual(j['status'],'complete',j)
        from datetime import datetime
        after=self.ml.latest(self.sid)
        for a in after['assets']:
            delta=datetime.fromisoformat(a['corrected_start_utc'])-datetime.fromisoformat(before[a['asset_id']])
            self.assertEqual(delta.total_seconds(),7)

    def test_dedup_and_no_recursive_generated_outputs(self):
        original=self.source/'ANT00000.jpg';shutil.copy2(original,self.source/'same-bytes.jpg')
        exports=self.source/'EXPORTS';exports.mkdir();shutil.copy2(original,exports/'not-input.jpg')
        d=self.analyzed()
        self.assertEqual(d['result']['stats']['capture_count'],2)
        self.assertEqual(d['result']['stats']['source_file_count'],3)
        self.assertEqual(d['result']['stats']['duplicate_count'],1)

    def test_cancel_leaves_no_complete_analysis(self):
        job=self.ml.enqueue(self.sid,'analyze',{});self.ml.cancel(job['job']);self.lab.run(job['job'])
        self.assertEqual(self.lab.job(job['job'])['status'],'cancelled')
        self.assertTrue(self.lab.job(job['job'])['finished'])
        self.assertIsNone(self.ml.latest(self.sid))

    def test_offline_assets_and_selection_survive_analyze(self):
        d=self.analyzed();aid=d['result']['assets'][0]['asset_id']
        self.ml.save(self.sid,{'selection':{aid:'keep'}},d['revision'])
        for p in self.source.iterdir():p.unlink()
        d=self.analyzed();self.assertEqual(len(d['result']['assets']),2)
        self.assertEqual(d['state']['review']['selection'][aid],'keep')
        self.assertTrue(all(not a['available'] for a in d['result']['assets']))

    def test_import_event_preserves_unknown_fields_and_external_file(self):
        path=self.root/'EVENT.json';path.write_text(json.dumps({'event_id':'047','custom':{'x':1},'record_id':'existing-record'}),'utf-8')
        before=sha256(path);state,revision=self.ml.state(self.sid)
        self.ml.import_event(self.sid,path,revision)
        state,_=self.ml.state(self.sid)
        self.assertEqual(state['config']['generator_event']['custom'],{'x':1})
        self.assertEqual(state['config']['record_id'],'existing-record')
        self.assertEqual(sha256(path),before)

    def test_stale_revision_and_cross_workspace_rejected(self):
        state,revision=self.ml.state(self.sid)
        self.ml.save(self.sid,{'interval_minutes':10},revision,True)
        with self.assertRaises(ValueError):self.ml.save(self.sid,{'interval_minutes':30},revision,True)
        with self.engine.conn() as c:c.execute("INSERT INTO sessions(id,title,company,source) VALUES('111111111111','School','vypusk',?)",(str(self.source),))
        with self.assertRaises(ValueError):self.ml.state('111111111111')


if __name__=='__main__':unittest.main()
