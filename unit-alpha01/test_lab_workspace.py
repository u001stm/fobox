import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from engine import Engine
from lab_workspace import LabWorkspace, recipe, sha256
from unit_workspace import UnitWorkspace


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.e=Engine(self.root/'catalog'/'test.sqlite3')
        self.u=UnitWorkspace(self.e)
        self.lab=LabWorkspace(self.e,self.u,autostart=False)
        self.card=self.root/'card';self.card.mkdir()
        Image.new('RGB',(80,120),'#998877').save(self.card/'ANT00001.jpg')
        self.warm=patch.object(self.e.offline,'warm');self.warm.start()

    def tearDown(self):
        self.warm.stop();self.tmp.cleanup()

    def imported(self):
        out=self.lab.start_import({'source':str(self.card),'title':'Test'})
        self.lab.run(out['job'])
        self.assertEqual(self.lab.job(out['job'])['status'],'complete',self.lab.job(out['job']))
        return out['session']

    def test_cards_with_same_filename_do_not_replace_and_restart_deduplicates(self):
        sid=self.imported()
        other=self.root/'card2';other.mkdir()
        Image.new('RGB',(80,120),'#112233').save(other/'ANT00001.jpg')
        b=self.lab.start_import({'source':str(other),'session':sid})
        self.lab.run(b['job'])
        self.assertEqual(len(self.u.rows(sid)[1]),2)
        r=self.lab.start_import({'source':str(other),'session':sid});self.lab.run(r['job'])
        self.assertEqual(self.lab.job(r['job'])['result']['duplicates'],1)
        self.assertEqual(len(self.u.rows(sid)[1]),2)

    def test_other_workspace_is_rejected(self):
        with self.e.conn() as c:
            c.execute("INSERT INTO sessions(id,title,company,source) VALUES('111111111111','School','vypusk',?)",(str(self.card),))
        with self.assertRaises(ValueError):self.lab.start_import({'source':str(self.card),'session':'111111111111'})
        with self.assertRaises(ValueError):self.lab.start_render({'session':'111111111111','paths':[]})

    def test_nan_and_unknown_recipe_and_unselected_paths_rejected(self):
        for value in ({'exposure':float('nan')},{'exposure':4},{'wb':'bad'},{'unknown':1}):
            with self.assertRaises(ValueError):recipe(value)
        sid=self.imported()
        with self.assertRaises(ValueError):self.lab.start_render({'session':sid,'paths':[self.u.rows(sid)[1][0]['path']]})

    def test_completed_copies_survive_source_removal(self):
        sid=self.imported()
        file=self.u.rows(sid)[1][0]
        self.assertTrue(self.e.preview_bytes(sid,file['path'],1280))
        original=sha256(self.card/'ANT00001.jpg')
        (self.card/'ANT00001.jpg').unlink()
        self.assertEqual(sha256(file['path']),original)
        reopened=LabWorkspace(Engine(self.e.db_path),UnitWorkspace(Engine(self.e.db_path)),autostart=False)
        self.assertEqual(reopened.project(sid)['sources_verified'],1)

    def test_pending_queue_recovers_without_touching_selection(self):
        sid=self.imported()
        st,rev=self.u.state(sid);p=self.u.rows(sid)[1][0]['path']
        st['selected']=[p];st['items']=[{'id':'one','kind':'photo','path':p}];self.u.save(sid,st,rev)
        out=self.lab.enqueue(sid,'render',{'paths':[p],'recipe':recipe({})})
        self.lab.update(out['job'],status='running')
        restarted=LabWorkspace(self.e,self.u,autostart=False)
        self.assertEqual(restarted.job(out['job'])['status'],'queued')
        self.assertEqual(restarted.pending.get_nowait(),out['job'])
        self.assertEqual(self.u.state(sid)[0]['selected'],[p])


if __name__=='__main__':unittest.main()
