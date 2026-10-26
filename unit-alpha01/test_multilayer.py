from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ml_core import build,instant
from ml_contracts import validate_result,validate
from ml_outputs import outputs


def config():
    return {'schema_version':'unit.multilayer.event/1','record_id':'local:test','event_id':'047','shoot_id':None,
            'timezone':'Europe/Moscow','allow_naive':True,'event_start':None,'event_end':None,'master_mode':'all',
            'activity_scope':'master_photo_captures','interval_minutes':'AUTO',
            'devices':[{'device_id':'SONY','role':'MASTER_PHOTO','offset_seconds':0,'priority':0},
                       {'device_id':'POV','role':'POV','offset_seconds':7,'priority':1},
                       {'device_id':'FIXED','role':'FIXED','offset_seconds':-12,'priority':2}]}


def asset(id,time='2026-10-26T14:32:21+03:00',kind='photo',device='SONY',duration=None,ext='.jpg',stem=None):
    return {'asset_id':id,'name':(stem or id)+ext,'stem':stem or id,'extension':ext,'media_type':kind,
            'device_id':device,'raw_timestamp':time,'time_source':'synthetic:capture','warnings':[],
            'capture_duration_seconds':duration,'source_group':'card','camera':'SYNTHETIC','serial':'1',
            'is_raw':ext=='.arw','bytes':10,'source_ref':device+'/'+id+ext,'path':id+ext}


def review():return {'selection':{},'time_overrides':{},'manual_order':[],'markers':[]}


class CoreTests(unittest.TestCase):
    def test_timezone_and_both_offset_signs_not_double_applied(self):
        raw=[asset('a'),asset('b','2026-10-26T11:32:14Z','video','POV',10,'.mp4'),
             asset('c','2026-10-26T14:32:33+03:00','video','FIXED',10,'.mp4')]
        a=build(raw,config(),review(),3);b=build(raw,config(),review(),3)
        self.assertEqual(a,b)
        self.assertEqual(len({x['corrected_start_utc'] for x in a['assets']}),1)
        self.assertEqual(raw[1]['raw_timestamp'],'2026-10-26T11:32:14Z')

    def test_ambiguous_and_nonexistent_local_time_require_offset(self):
        for text in ('2026-10-25T02:30:00','2026-03-29T02:30:00'):
            with self.assertRaises(ValueError):instant(text,'Europe/Berlin')
        self.assertEqual(instant('2026-10-25T02:30:00+02:00','Europe/Berlin')[0].hour,0)

    def test_raw_jpeg_pair_not_two_captures(self):
        raw=[asset('raw',ext='.arw',stem='ANT00001'),asset('jpeg',stem='ANT00001')]
        result=build(raw,config(),review(),2)
        self.assertEqual(result['stats']['capture_count'],1)
        self.assertEqual(result['stats']['raw_jpeg_pair_count'],1)
        self.assertEqual(result['stats']['source_file_count'],2)
        validate_result(result)
        raw[1]['serial']=''
        self.assertEqual(build(raw,config(),review(),2)['stats']['capture_count'],1)
        raw[1]['serial']='different-camera'
        self.assertEqual(build(raw,config(),review(),2)['stats']['capture_count'],2)

    def test_burst_and_same_name_on_other_cards_do_not_collapse(self):
        raw=[asset(str(i)) for i in range(3)]
        self.assertEqual(build(raw,config(),review(),3)['stats']['capture_count'],3)
        a,b=asset('raw',ext='.arw',stem='ANT00001'),asset('jpg',stem='ANT00001')
        b['source_group']='other-card'
        self.assertEqual(build([a,b],config(),review(),2)['stats']['capture_count'],2)

    def test_uncertain_pair_is_explicit(self):
        raw=[asset('raw1',ext='.arw',stem='ANT'),asset('raw2',ext='.arw',stem='ANT'),asset('jpg',stem='ANT')]
        result=build(raw,config(),review(),3)
        self.assertEqual(result['stats']['raw_jpeg_pair_count'],0)
        self.assertTrue(any('пара' in w['message'] for w in result['stats']['warnings']))

    def test_video_anchor_photo_first_ties_before_after(self):
        c=config();c['devices'][1]['offset_seconds']=0
        raw=[asset('p1'),asset('p2','2026-10-26T14:33:17+03:00'),
             asset('v','2026-10-26T14:32:24+03:00','video','POV',46,'.mp4')]
        result=build(raw,c,review(),3)
        self.assertEqual([r['asset_id'] for r in result['timeline']['items']],['p1','v','p2'])
        self.assertEqual(result['timeline']['items'][1]['working_filename'],'001-01.mp4')
        self.assertEqual(result['timeline']['items'][1]['covered_photo_ids'],[])
        raw[2]['raw_timestamp']=raw[0]['raw_timestamp']
        self.assertEqual([r['asset_id'] for r in build(raw,c,review(),3)['timeline']['items']],['p1','v','p2'])
        raw[2]['raw_timestamp']='2026-10-26T14:00:00+03:00'
        self.assertEqual(build(raw,c,review(),3)['timeline']['items'][0]['working_filename'],'000-01.mp4')

    def test_last_photo_of_equal_time_is_anchor(self):
        c=config();c['devices'][1]['offset_seconds']=0
        result=build([asset('p1'),asset('p2'),asset('v',kind='video',device='POV',duration=2,ext='.mp4')],c,review(),3)
        self.assertEqual(result['timeline']['items'][-1]['anchor_photo_id'],result['timeline']['items'][-2]['capture_id'])

    def test_overlapping_long_videos_duration_and_coverage_differ(self):
        c=config();c['devices'][1]['offset_seconds']=0;c['devices'][2]['offset_seconds']=0
        raw=[asset('pov','2026-10-26T14:00:00+03:00','video','POV',3600,'.mp4'),
             asset('fixed','2026-10-26T14:20:00+03:00','video','FIXED',3000,'.mp4')]
        result=build(raw,c,review(),2)
        self.assertEqual(len(result['timeline']['items']),2)
        self.assertEqual(result['stats']['video_duration_sum_seconds'],6600)
        self.assertEqual(result['stats']['video_coverage_seconds'],4200)
        self.assertTrue(result['timeline']['no_photo_master'])

    def test_unknown_timestamp_and_duration_not_zero(self):
        result=build([asset('u',None),asset('v',kind='video',device='POV',ext='.mp4')],config(),review(),2)
        self.assertEqual(result['timeline']['unplaced'],['u'])
        self.assertIsNone(result['assets'][1]['capture_duration_seconds'])
        self.assertEqual(result['stats']['quality']['unknown_duration_count'],1)
        self.assertFalse(result['stats']['quality']['analysis_complete'])

    def test_activity_reference_60_120_60_and_final_boundary(self):
        c=config();c.update(interval_minutes=30,event_start='2026-10-26T14:30:00+03:00',event_end='2026-10-26T16:00:00+03:00')
        raw=[]
        for bin_index,count in enumerate((60,120,60)):
            for index in range(count):
                dt=datetime(2026,10,26,14,30,tzinfo=timezone(timedelta(hours=3)))+timedelta(minutes=bin_index*30,seconds=index)
                raw.extend([asset(f'r{bin_index}-{index}',dt.isoformat(),ext='.arw',stem=f'ANT{bin_index}-{index}'),
                            asset(f'j{bin_index}-{index}',dt.isoformat(),stem=f'ANT{bin_index}-{index}')])
        for index in range(3):raw.append(asset('v'+str(index),kind='video',device='POV',duration=1,ext='.mp4'))
        result=build(raw,c,review(),483);s=result['stats']
        self.assertEqual(s['capture_count'],240)
        self.assertEqual([b['count'] for b in s['activity']['bins']],[60,120,60])
        self.assertAlmostEqual(s['photos_per_minute'],240/90)
        self.assertEqual(s['activity']['peak_count_bin_index'],1)
        raw.append(asset('end',c['event_end']))
        s=build(raw,c,review(),484)['stats']
        self.assertEqual([b['count'] for b in s['activity']['bins']],[60,120,61])

    def test_partial_bin_denominator_and_peak_ties(self):
        c=config();c.update(interval_minutes=10,event_start='2026-10-26T14:07:00+03:00',event_end='2026-10-26T14:23:00+03:00')
        r=build([asset('a','2026-10-26T14:07:00+03:00'),asset('b','2026-10-26T14:20:00+03:00')],c,review(),2)['stats']
        self.assertEqual([b['count'] for b in r['activity']['bins']],[1,0,1])
        self.assertEqual(r['activity']['bins'][0]['actual_duration_seconds'],180)
        self.assertAlmostEqual(r['activity']['bins'][0]['rate_per_minute'],1/3)
        self.assertEqual(r['activity']['peak_count_ties'],[0,2])
        self.assertEqual(r['activity']['peak_rate_bin_index'],1)  # only complete bin; partials aren't compared with full bins

    def test_empty_and_single_frame_have_no_infinity_or_fake_peak(self):
        for raw in ([],[asset('one')]):
            s=build(raw,config(),review(),len(raw))['stats']
            self.assertIsNone(s['photos_per_minute'])
            self.assertIsNone(s['activity']['peak_count_bin_index'])
            json.dumps(s,allow_nan=False)

    def test_auto_interval_90_240_and_midnight(self):
        for duration,expected in ((5400,5),(5401,10),(14400,10),(14401,30)):
            c=config();c.update(event_start='2026-10-26T23:30:00+03:00',event_end=(datetime(2026,10,26,23,30,tzinfo=timezone(timedelta(hours=3)))+timedelta(seconds=duration)).isoformat())
            self.assertEqual(build([asset('x')],c,review(),1)['stats']['activity']['interval_minutes'],expected)

    def test_manual_order_and_review_survive_offset_change(self):
        c=config();raw=[asset('p'),asset('v',kind='video',device='POV',duration=2,ext='.mp4')];r=review()
        first=build(raw,c,r,2);r['manual_order']=[x['item_id'] for x in reversed(first['timeline']['items'])];r['selection']={'v':'reject'}
        c['devices'][1]['offset_seconds']=-99
        second=build(raw,c,r,2)
        self.assertEqual([x['item_id'] for x in second['timeline']['items']],r['manual_order'])
        self.assertEqual(next(x for x in second['timeline']['items'] if x['asset_id']=='v')['selection'],'reject')

    def test_contract_rejects_wrong_version_and_nonfinite(self):
        c=config();validate('event',c)
        c['schema_version']='wrong'
        with self.assertRaises(ValueError):validate('event',c)
        c=config();c['devices'][0]['offset_seconds']=float('nan')
        with self.assertRaises(ValueError):validate('event',c)

    def test_outputs_are_same_revision_and_png_raster_exists(self):
        from PIL import Image
        r=build([asset('a'),asset('b','2026-10-26T14:40:00+03:00')],config(),review(),2)
        validate_result(r)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'DATA';outputs(folder,config(),r,7)
            for name in ('stats','assets','EVENT','markers'):
                self.assertEqual(json.loads((folder/(name+'.json')).read_text('utf-8'))['analysis_revision'],7)
            self.assertEqual(json.loads((folder/'timeline.json').read_text('utf-8'))['revision'],7)
            self.assertIn('R7',(folder/'activity.svg').read_text('utf-8'))
            with Image.open(folder/'activity.png') as image:self.assertEqual(image.size,(1200,300))
            self.assertFalse(list(Path(temp).rglob('*.xmp')))


if __name__=='__main__':unittest.main()
