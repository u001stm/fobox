import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from engine import Engine


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.e=Engine(Path(self.tmp.name)/'test.sqlite3');self.sid='b'*12
        start=datetime(2026,10,22,9,3,39)
        self.frames=[{'path':str(Path(self.tmp.name)/f'ANT{i:05}.ARW'),'name':f'ANT{i:05}.ARW',
                      'capture_at':(start+timedelta(seconds=t)).strftime('%Y-%m-%d %H:%M:%S'),'dhash':'0000000000000000'}
                     for i,t in enumerate((0,4,90,260,265,390,395))]
        with self.e.conn() as c:
            c.execute('INSERT INTO sessions(id,title,source) VALUES(?,?,?)',(self.sid,'Scene test',self.tmp.name))
            c.execute('INSERT INTO selections VALUES(?,?,?)',(self.sid,'report',self.frames[0]['path']))
            c.execute('INSERT INTO approvals VALUES(?,?,?,CURRENT_TIMESTAMP)',(self.sid,'report',1))
            c.execute("INSERT INTO marks VALUES(?,?,?,'dup')",(self.sid,'report',self.frames[1]['path']))

    def tearDown(self):self.e.offline.close();self.tmp.cleanup()

    def test_two_minute_pause_separates_groups_on_identical_background(self):
        self.assertEqual(len(self.e._scenes(self.frames)),1)
        groups=self.e._group_scenes(self.sid,self.frames)
        self.assertEqual([len(g) for g in groups],[3,2,2])
        self.assertEqual(sum(len(g) for g in groups),len(self.frames))

    def test_split_join_reset_persist_without_changing_selection(self):
        paths=[f['path'] for f in self.frames]
        def protected():
            with self.e.conn() as c:return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ('selections','marks','approvals','students','anchor_overrides')}
        before=protected()
        with patch.object(self.e,'sections',return_value=([{'kind':'reportage','files':paths}],[])):
            self.e.set_scene_boundary(self.sid,paths[3],False)
            self.assertEqual(len(self.e._group_scenes(self.sid,self.frames)),2)
            self.e.set_scene_boundary(self.sid,paths[1],True)
            self.assertEqual(len(self.e._group_scenes(self.sid,self.frames)),3)
            self.assertEqual(protected(),before)
            snapshot=self.e.offline.snapshot(self.sid)
            self.assertEqual(len(json.loads(Path(snapshot).read_text('utf-8'))['tables']['scene_boundaries']),2)
            reopened=Engine(self.e.db_path)
            self.assertEqual(len(reopened._group_scenes(self.sid,self.frames)),3)
            self.e.set_scene_boundary(self.sid,paths[1],None)
            self.assertEqual(len(self.e._group_scenes(self.sid,self.frames)),2)
            self.e.set_scene_boundary(self.sid,paths[3],None)
            self.assertEqual([len(g) for g in self.e._group_scenes(self.sid,self.frames)],[3,2,2])

    def test_personal_portrait_cannot_be_used_as_group_boundary(self):
        with patch.object(self.e,'sections',return_value=([{'kind':'portrait','files':[self.frames[0]['path']]}],[])):
            with self.assertRaises(ValueError):self.e.set_scene_boundary(self.sid,self.frames[0]['path'],True)

    def test_additional_frame_cannot_attach_to_another_scene(self):
        paths=[f['path'] for f in self.frames]
        section={'scenes':[paths[:3],paths[3:]]}
        info={f['path']:f for f in self.frames}
        self.assertIsNone(self.e.duplicate_parent(section,paths[3],[paths[0]],info))
        self.assertEqual(self.e.duplicate_parent(section,paths[1],[paths[0]],info),paths[0])


if __name__=='__main__':unittest.main()
