"""Behavioral checks for calibrated caption QA, evidence gates and export repair."""
import sys
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
CACHE=Path(os.environ.get('TEST_WORKSPACE',str(ROOT.parent/'test-work')));CACHE.mkdir(parents=True,exist_ok=True)
if not os.environ.get('SUBTITLE_TEST_FONT'):
    fonts=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/arial.ttf',Path('/System/Library/Fonts/Supplemental/Arial.ttf')]
    os.environ['SUBTITLE_TEST_FONT']=str(next(p for p in fonts if p.is_file()))
import shutil
for key,name in [('FFMPEG_TEST_BIN','ffmpeg'),('FFPROBE_TEST_BIN','ffprobe')]:
    if key not in os.environ and shutil.which(name):os.environ[key]=shutil.which(name)

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from check_subtitle_render import Detector, parity, read_srt
from manage_video_editing_run import CHECKS, budget, checklist, init, summary, timer_start, timer_stop, verify
from check_video_export import inspect


class EditingQA(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=CACHE);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.font=Path(os.environ['SUBTITLE_TEST_FONT'])
        self.profile={'font_path':str(self.font),'font_sha256':hashlib.sha256(self.font.read_bytes()).hexdigest(),
                      'font_size_px':82,'canvas_width_px':1080,'safe_left_px':120,'safe_right_px':120,
                      'render':{'canvas_height_px':400,'crop':[0,0,1080,400],'scan_band_px':[90,310],
                                'expected_center_y_px':200,'center_tolerance_px':8,'white_threshold':220,
                                'max_channel_difference':30,'row_min_pixels':10,'row_join_gap':8,
                                'row_min_rows':6,'row_padding':3,'shape_f1_min':.98}}
        self.detector=Detector(self.profile,self.root)

    def frame(self,text,ys=(200,),dx=0):
        im=Image.new('RGB',(1080,400));draw=ImageDraw.Draw(im);font=self.detector.font
        box=font.getbbox(text)
        for y in ys:draw.text(((1080-(box[2]-box[0]))/2-box[0]+dx,y-(box[3]-box[1])/2-box[1]),text,font=font,fill='white')
        p=self.root/'frame.png';im.save(p);return self.detector.mask(p)

    def test_native_like_single_wrap_and_missing_are_distinct(self):
        self.assertEqual(self.detector.inspect(self.frame('Business'), 'Business')['flags'],[])
        self.assertIn('line_count_or_missing',self.detector.inspect(self.frame('Business',(150,250)), 'Business')['flags'])
        self.assertEqual(self.detector.inspect(self.frame(''), 'Business')['lines'],0)

    def test_wrong_word_position_and_edge_are_review_flags(self):
        self.assertIn('glyph_shape_difference',self.detector.inspect(self.frame('Business'),'Strategy')['flags'])
        self.assertIn('vertical_position',self.detector.inspect(self.frame('Business',(245,)),'Business')['flags'])
        self.assertIn('safe_width',self.detector.inspect(self.frame('Business opportunity',dx=110),'Business opportunity')['flags'])

    def test_unknown_font_and_glyph_do_not_silently_pass(self):
        with self.assertRaises(ValueError):Detector({**self.profile,'font_sha256':'changed'},self.root)
        with self.assertRaises(ValueError):Detector({**self.profile,'letter_spacing_px':-1},self.root)
        result=self.detector.inspect(self.frame('Biz\U0010ffff'),'Biz\U0010ffff')
        self.assertIn('unvalidated_glyph',result['flags'])

    def test_srt_uses_real_boundaries_and_rejects_overlap(self):
        p=self.root/'test.srt';p.write_text('1\n00:00:00,000 --> 00:00:00,733\nBusiness\n\n2\n00:00:00,733 --> 00:00:01,767\n在哪里\n', encoding="utf-8")
        a=read_srt(p);b=[dict(x) for x in a];b[0]['endMs']-=1
        self.assertTrue(parity(a,b)['pass']);b[0]['text']='wrong';self.assertFalse(parity(a,b)['pass'])
        p.write_text('1\n00:00:00,000 --> 00:00:01,000\na\n\n2\n00:00:00,900 --> 00:00:02,000\nb\n', encoding="utf-8")
        with self.assertRaises(ValueError):read_srt(p)

    def test_failed_scan_cannot_leave_an_old_green_receipt(self):
        out=self.root/'qc';out.mkdir();(out/'report.json').write_text('{"status":"render_checks_complete"}', encoding="utf-8")
        profile=self.root/'profile.json';profile.write_text(json.dumps({**self.profile,'font_sha256':'stale'}), encoding="utf-8")
        cmd=[sys.executable,str(ROOT/'scripts/check_subtitle_render.py'),'scan',
             '--profile',str(profile),'--output-dir',str(out)]
        for key in ['video','srt','native-srt','validation']:cmd.extend(['--'+key,str(self.root/'missing')])
        result=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        report=json.loads((out/'report.json').read_text( encoding="utf-8"))
        self.assertEqual(report['status'],'check_failed');self.assertFalse(report['coverageAccepted'])

    def test_checklist_requires_all_fields_current_evidence_and_matching_target(self):
        rec={'revision':'r1'};req={k:{'required':True,'target':1} for k in CHECKS};init(rec,'r1',req)
        self.assertEqual(set(checklist(rec)['pending']),CHECKS)
        evidence=self.root/'readback.txt';evidence.write_text('actual native readback', encoding="utf-8")
        for key in CHECKS:verify(rec,key,1,[evidence],'all affected timeline items, native readback')
        self.assertEqual(checklist(rec)['pending'],[])
        verify(rec,'voice',2,[evidence],'all affected');self.assertIn('voice',checklist(rec)['pending'])
        evidence.write_text('changed', encoding="utf-8");self.assertEqual(set(checklist(rec)['pending']),CHECKS)
        with self.assertRaises(ValueError):init(rec,'r2',req)

    def test_not_applicable_requires_reason(self):
        req={k:{'required':True,'target':1} for k in CHECKS};req['animation']={'required':False}
        with self.assertRaises(ValueError):init({},'r1',req)

    def test_timer_is_exclusive_and_does_not_backfill_unknown_history(self):
        r={};init(r,'r1',{k:{'required':True,'target':1} for k in CHECKS})
        self.assertIsNone(summary(r)['measuredSecondsByCategory']['active'])
        timer_start(r,'subtitle_prepare','active')
        with self.assertRaises(ValueError):timer_start(r,'export','wait')
        timer_stop(r);self.assertGreaterEqual(summary(r)['measuredSecondsByCategory']['active'],0)
        with self.assertRaises(ValueError):timer_stop(r)

    def test_budget_counts_native_remux_retry_and_temp_once_on_same_volume(self):
        kinds=['native_export','container_copy','retry_export','temporary']
        plan={'items':[{'kind':k,'label':k,'directory':str(self.root),'bytes':100} for k in kinds]}
        result=budget(plan);self.assertEqual(len(result['volumes']),1)
        self.assertEqual(result['volumes'][0]['estimatedAdditionalBytes'],400)
        self.assertEqual(result['volumes'][0]['requiredBytes'],5*1024**3+400)
        with self.assertRaises(ValueError):budget({'items':plan['items'][:-1]})
        with self.assertRaises(ValueError):budget({**plan,'excluded':{'native_export':'already done'}})

    def test_already_present_and_per_volume_budget(self):
        one=self.root/'one';two=self.root/'two';one.mkdir();two.mkdir();existing=one/'source.mp4';existing.write_bytes(b'input')
        plan={'items':[{'label':'native','kind':'native_export','directory':str(one),'bytes':100},
                       {'label':'temp','kind':'temporary','directory':str(two),'bytes':200},
                       {'label':'existing','kind':'source_copy','directory':str(one),'bytes':300,'already_present':True,'existing_path':str(existing)}],
              'excluded':{'container_copy':'already verified MP4','retry_export':'remaining stage is QA only'}}
        original=Path.stat
        def fake_stat(p,*a,**kw):
            s=original(p,*a,**kw)
            if p in [one,two]:
                values=list(s);values[2]=11 if p==one else 22;return os.stat_result(values)
            return s
        with patch.object(Path,'stat',fake_stat):result=budget(plan)
        self.assertEqual(sorted(g['estimatedAdditionalBytes'] for g in result['volumes']),[100,200])
        self.assertTrue(all(g['requiredBytes']>=5*1024**3 for g in result['volumes']))


@unittest.skipUnless(os.environ.get('FFMPEG_TEST_BIN') and os.environ.get('FFPROBE_TEST_BIN'),'Local FFmpeg required')
class ExportRepair(unittest.TestCase):
    def test_actual_black_tail_is_flagged_for_review(self):
        with tempfile.TemporaryDirectory(dir=CACHE) as t:
            r=Path(t);ff=os.environ['FFMPEG_TEST_BIN'];fp=os.environ['FFPROBE_TEST_BIN'];video=r/'black-tail.mp4'
            subprocess.run([ff,'-v','error','-f','lavfi','-i','color=c=blue:s=96x96:r=30:d=1',
                '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-shortest','-vf',
                "drawbox=color=black:t=fill:enable='gte(t,0.6)'",'-c:v','libx264','-pix_fmt','yuv420p',
                '-colorspace','bt709','-c:a','aac',str(video)],check=True)
            spec=r/'spec.json';spec.write_text(json.dumps({'width':96,'height':96,'fps':'30/1',
                'video_codec':'h264','audio_codec':'aac','sample_rate':48000,'color_space':'bt709','expected_frames':30}), encoding="utf-8")
            out=r/'checks'
            result=subprocess.run([sys.executable,str(ROOT/'scripts/check_video_export.py'),
                '--video',str(video),'--spec',str(spec),'--output-dir',str(out),'--ffmpeg',ff,'--ffprobe',fp],capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stdout+result.stderr)
            report=json.loads((out/'report.json').read_text( encoding="utf-8"));self.assertTrue(report['blackCandidates'])
            self.assertAlmostEqual(report['blackCandidates'][0]['start'],.6,places=2)

    def test_real_mov_repair_preserves_streams_and_missing_frame_is_detected(self):
        with tempfile.TemporaryDirectory(dir=CACHE) as t:
            r=Path(t);ff=os.environ['FFMPEG_TEST_BIN'];fp=os.environ['FFPROBE_TEST_BIN']
            native=r/'native.mov'
            subprocess.run([ff,'-v','error','-f','lavfi','-i','color=c=blue:s=96x96:r=30:d=1','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-shortest','-c:v','libx264','-pix_fmt','yuv420p','-colorspace','bt709','-c:a','aac','-f','mov',str(native)],check=True)
            spec={'width':96,'height':96,'fps':'30/1','video_codec':'h264','audio_codec':'aac','sample_rate':48000,'color_space':'bt709','expected_frames':30}
            self.assertEqual(inspect(native,spec,fp)['errors'],['container'])
            s=r/'spec.json';s.write_text(json.dumps(spec), encoding="utf-8");out=r/'checks';final=r/'final.mp4'
            cmd=[sys.executable,str(ROOT/'scripts/check_video_export.py'),'--video',str(native),'--spec',str(s),'--output-dir',str(out),'--remux-to',str(final),'--ffmpeg',ff,'--ffprobe',fp]
            result=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            report=json.loads((out/'report.json').read_text( encoding="utf-8"));self.assertTrue(report['streamHashes']['identical']);self.assertTrue(native.is_file());self.assertEqual(report['fullDecode'],'exit0')
            bad=r/'missing.mov'
            subprocess.run([ff,'-v','error','-i',str(native),'-vf',"select='not(eq(n,10))'",'-fps_mode','vfr','-c:v','libx264','-colorspace','bt709','-c:a','copy',str(bad)],check=True)
            self.assertIn('nonconstant_or_missing_frame_timestamps',inspect(bad,spec,fp)['errors'])
            # Existing final must never be overwritten by a repeated repair command.
            before=hashlib.sha256(final.read_bytes()).hexdigest();self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,2)
            self.assertEqual(hashlib.sha256(final.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
