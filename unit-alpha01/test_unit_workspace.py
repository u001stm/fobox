import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from engine import Engine
from unit_workspace import UnitWorkspace, cluster_faces


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'card';self.source.mkdir()
        self.e = Engine(self.root/'catalog'/'test.sqlite3')
        self.u = UnitWorkspace(self.e)
        self.sid = 'a'*12
        self.files = []
        with self.e.conn() as c:
            c.execute("INSERT INTO sessions(id,title,company,source,first_capture,last_capture) VALUES(?,?,?,?,?,?)",
                      (self.sid,'Unit test','unit',str(self.source),'2026-10-26 10:00:00','2026-10-26 10:10:00'))
            for i in range(2):
                p=self.source/f'ANT{i:05}.ARW';p.write_bytes(b'raw source '+bytes([i]));self.files.append(str(p))
                c.execute('INSERT INTO files(session_id,path,name,capture_at,kind,bytes,preview) VALUES(?,?,?,?,?,?,?)',
                          (self.sid,str(p),p.name,f'2026-10-26 10:{i:02}:00','raw',p.stat().st_size,None))

    def tearDown(self):
        self.tmp.cleanup()

    def selected(self):
        st,rev=self.u.state(self.sid);st['selected']=self.files[:]
        st['items']=[{'id':str(i),'kind':'photo','path':p} for i,p in enumerate(reversed(self.files))]
        self.u.save(self.sid,st,rev)

    def test_optimistic_save_and_reopen_preserve_order(self):
        self.selected()
        st,rev=self.u.state(self.sid)
        self.assertEqual(st['items'][0]['path'],self.files[1])
        with self.assertRaisesRegex(ValueError,'другой вкладке'):
            self.u.save(self.sid,st,0)
        reopened=UnitWorkspace(Engine(self.e.db_path))
        self.assertEqual(reopened.state(self.sid),(st,rev))

    def test_export_blocks_missing_jpeg_and_preserves_raw(self):
        self.selected();before=[Path(p).read_bytes() for p in self.files]
        with self.assertRaisesRegex(ValueError,'Нет обработанного JPEG'):
            self.u.export(self.sid,self.root/'delivery')
        self.assertEqual(before,[Path(p).read_bytes() for p in self.files])
        with self.assertRaisesRegex(ValueError,'отдельно'):
            self.u.export(self.sid,self.source/'output')

    def test_mixed_delivery_numbers_and_original_audio_bytes(self):
        self.selected();jpeg=self.root/'processed';jpeg.mkdir()
        for i,p in enumerate(self.files):
            Image.new('RGB',(120,180),(50+i*50,20,30)).save(jpeg/(Path(p).stem+'.jpg'))
        self.assertEqual(self.u.connect_jpeg(self.sid,jpeg)['linked'],2)
        wav=self.root/'media'/'clip.wav';wav.parent.mkdir();wav.write_bytes(b'RIFF unchanged audio')
        self.u.add_media(self.sid,wav)
        st,rev=self.u.state(self.sid)
        st['items'].insert(1,{'id':'time','kind':'time','source':self.files[1]})
        st['items'].insert(2,{'id':'txt','kind':'text','text':'Пауза'})
        self.u.save(self.sid,st,rev)
        result=self.u.export(self.sid,self.root/'delivery');out=Path(result['path'])
        self.assertTrue((out/'001.jpg').exists());self.assertTrue((out/'002.jpg').exists())
        self.assertEqual((out/'005.wav').read_bytes(),wav.read_bytes())
        self.assertEqual(len(json.loads((out/'MANIFEST.json').read_text('utf-8'))['items']),5)
        self.assertFalse(list(out.glob('*.xmp')))
        self.assertNotEqual(self.u.export(self.sid,self.root/'delivery')['path'],result['path'])

    def test_duplicate_jpeg_stems_are_not_guessed(self):
        jpeg=self.root/'processed';(jpeg/'a').mkdir(parents=True);(jpeg/'b').mkdir()
        for d in ('a','b'):Image.new('RGB',(20,30)).save(jpeg/d/'ANT00000.jpg')
        result=self.u.connect_jpeg(self.sid,jpeg)
        self.assertEqual(result['linked'],0);self.assertEqual(result['ambiguous'],['ANT00000.ARW'])

    def test_changed_media_and_unknown_path_rejected(self):
        wav=self.root/'media'/'x.wav';wav.parent.mkdir();wav.write_bytes(b'1');self.u.add_media(self.sid,wav)
        wav.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'изменился'):
            self.u.export(self.sid,self.root/'delivery')
        st,rev=self.u.state(self.sid);st['items']=[{'id':'bad','kind':'video','path':'C:/secret.mp4'}]
        with self.assertRaises(ValueError):self.u.save(self.sid,st,rev)

    def test_channel_summary_and_pending_bridge(self):
        st,rev=self.u.state(self.sid);st['channel']='model';self.u.save(self.sid,st,rev)
        self.assertEqual(len(self.u.summary('model')['sessions']),1)
        self.assertEqual(len(self.u.summary('startup')['sessions']),0)
        result=self.u.bridge();self.assertEqual(result['status'],'pending')
        self.assertTrue(Path(result['path']).is_file())

    def test_clusters_all_faces_and_never_merge_two_faces_in_same_photo(self):
        a=np.zeros(512,dtype='float32');a[0]=1
        b=np.zeros(512,dtype='float32');b[1]=1
        def f(e):return {'emb':e,'score':.99,'h':.2,'box':[0,0,10,10]}
        groups=cluster_faces({'a':[f(a),f(b)],'b':[f(a),f(b)],'c':[f(a),f(a)]})
        self.assertEqual(len(groups),3)
        self.assertEqual(sum(g['appearances'] for g in groups),6)
        self.assertTrue(all(len(g['paths'])==len(set(g['paths'])) for g in groups))

    def test_unit_no_face_is_not_a_defect(self):
        self.e._mark_noface(self.sid)
        with self.e.conn() as c:self.assertFalse(any(r[0] for r in c.execute('SELECT flags FROM files')))

    def test_tiny_and_low_confidence_faces_are_not_claimed_as_unique_people(self):
        a=np.zeros(512,dtype='float32');a[0]=1
        result=cluster_faces({'frame':[{'emb':a,'score':.99,'h':.03},{'emb':a,'score':.6,'h':.2}]})
        self.assertEqual(result,[])

    def test_import_namespace_cannot_overwrite_legacy_shoot(self):
        folder=self.root/'images';folder.mkdir();Image.new('RGB',(100,150),'white').save(folder/'IMG0001.jpg')
        legacy=self.e.import_folder(folder,{'company':'vypusk'})
        unit=self.e.import_folder(folder,{'company':'unit','workspace':'unit'})
        self.assertNotEqual(legacy,unit)
        with self.e.conn() as c:
            self.assertEqual(c.execute('SELECT company FROM sessions WHERE id=?',(legacy,)).fetchone()[0],'vypusk')
            self.assertEqual(c.execute('SELECT company FROM sessions WHERE id=?',(unit,)).fetchone()[0],'unit')


if __name__=='__main__':unittest.main()
