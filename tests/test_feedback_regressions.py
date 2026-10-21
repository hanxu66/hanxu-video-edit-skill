"""Synthetic Codex regressions; no private footage, fonts or account calls."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import session
import doctor
import delivery
import review_media
from check_media_integrity import inspect_media
from check_subtitle_render import validate, run as scan, digest


class Feedback(unittest.TestCase):
    def setUp(self):
        cache=Path(os.environ.get('TEST_WORKSPACE',str(ROOT.parent/'test-work')));cache.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='新用户 中文 ',dir=cache);self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name).resolve()
        self.ff=os.environ.get('TEST_FFMPEG') or shutil.which('ffmpeg')
        self.fp=os.environ.get('TEST_FFPROBE') or shutil.which('ffprobe')

    def cli(self,name,*args):
        return subprocess.run([sys.executable,'-B',str(ROOT/'scripts'/name),*map(str,args)],
            capture_output=True,text=True,encoding='utf-8',
            env=dict(os.environ,PYTHONUTF8='0',PYTHONIOENCODING='cp1252',PYTHONDONTWRITEBYTECODE='1'))

    def write(self,name,value):
        p=self.work/name;p.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8-sig');return p

    def state(self):
        task=session.fresh_task({});script=self.work/'稿件.txt';script.write_text('确认稿',encoding='utf-8')
        task.update(mode='ai-only',script=str(script),script_approved=True,animation_type='PPT型',delivery_scope='review')
        return {'profile':{},'task':task}

    def test_cancelled_voice_is_not_retried_and_silent_preview_is_labelled(self):
        state=self.state();state['task']['voice_plan']={'status':'declined'}
        self.assertEqual(session.plan(state,self.work)['action'],'voice_paused')
        state['task']['voice_plan']={'status':'silent_preview'}
        result=session.plan(state,self.work)
        self.assertFalse(result['duration_locked']);self.assertIn('静音预览',result['delivery_label'])
        self.assertNotIn('voice_generation_available',result.get('missing_capabilities',[]))
        state['task']['status']='accepted'
        with self.assertRaisesRegex(ValueError,'Silent preview'):session.validate(state,self.work)

    def test_explicit_silent_work_is_distinct_from_unfinished_voice(self):
        state=self.state();state['task']['voice_plan']={'status':'intentional_silent','user_quote':'我只需要无声背景动画'}
        session.validate(state,self.work)
        self.assertNotEqual(session.plan(state,self.work)['action'],'ask_voice')

    def test_bad_enum_returns_allowed_and_nonzero_without_changing_state(self):
        self.assertEqual(self.cli('session.py','--workspace',self.work,'init').returncode,0)
        state=self.work/'.video-agent/session.json';before=state.read_bytes()
        patch=self.write('错误.json',{'animation_type':'ppt'})
        result=self.cli('session.py','--workspace',self.work,'update','--scope','task','--data-file',patch)
        self.assertEqual(result.returncode,2);error=json.loads(result.stderr)
        self.assertEqual(error['received'],'ppt');self.assertIn('PPT型',error['allowed'])
        self.assertEqual(before,state.read_bytes())

    def test_overrides_scope_and_task_error_hint(self):
        patch=self.write('参数.json',{'speed':1.3})
        r=self.cli('session.py','--workspace',self.work,'update','--scope','overrides','--data-file',patch)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['task']['overrides']['speed'],1.3)
        r=self.cli('session.py','--workspace',self.work,'update','--scope','task','--data-file',patch)
        self.assertEqual(r.returncode,2);self.assertIn('--scope overrides',json.loads(r.stderr)['error'])

    def test_caption_schema_and_ass_roles_fail_with_actionable_errors(self):
        with self.assertRaisesRegex(ValueError,'start_us'):review_media.audit([{'text':'test','start':0,'end':1}])
        bad=self.write('caption_source.json',{'cards':[]})
        with self.assertRaisesRegex(ValueError,'ASS'):delivery.validate_ass(bad)
        result=self.cli('review_media.py','--workspace',self.work,'--cards',bad,'--report',self.work/'report.json')
        self.assertEqual(result.returncode,2)

    def test_checklist_updates_existing_task_without_destroying_preferences(self):
        rec=self.write('session.json',{'profile':{'display_name':'测试者'},'task':{'id':'one','revision':'r1'}})
        req=self.write('requirements.json',{k:{'required':False,'reason':'Synthetic unit fixture'} for k in ('speed','voice','beauty','body','grade','subtitles','identity','animation','cover')})
        r=self.cli('manage_video_editing_run.py','init','--record',rec,'--revision','r1','--requirements',req)
        self.assertEqual(r.returncode,0,r.stderr+r.stdout)
        data=json.loads(rec.read_text(encoding='utf-8-sig'))
        self.assertEqual(data['profile']['display_name'],'测试者');self.assertIn('editingExecution',data['task'])
        self.assertNotIn('editingExecution',data)

    def media(self,name,audio='sine=frequency=440:sample_rate=48000'):
        if not self.ff or not self.fp:
            if os.environ.get('REQUIRE_MEDIA_TEST'):self.fail('CI must run media tests')
            self.skipTest('FFmpeg unavailable')
        p=self.work/name
        subprocess.run([self.ff,'-v','error','-f','lavfi','-i','color=c=blue:s=96x96:r=30:d=1','-f','lavfi','-i',audio,
                        '-shortest','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(p)],check=True)
        return p

    def test_silent_aac_is_rejected_but_explicit_preview_is_allowed(self):
        p=self.media('无声.mp4','anullsrc=r=48000:cl=stereo')
        result=inspect_media(p,self.ff,self.fp)
        self.assertIn('required_audio_is_silent',result['issues']);self.assertEqual(result['audio']['signal'],'all_zero')
        result=inspect_media(p,self.ff,self.fp,'silent-preview')
        self.assertEqual(result['status'],'passed');self.assertIn('静音预览',result['delivery_label'])
        self.assertEqual(result['hearing'],'unverified')

    def test_decodable_signal_and_stale_final_report(self):
        p=self.media('有声.mp4');result=inspect_media(p,self.ff,self.fp)
        self.assertEqual(result['status'],'passed',result)
        report=self.write('媒体检查.json',result)
        before=doctor.assess(self.work,'review',source=str(p),ffmpeg=self.ff,ffprobe=self.fp,reserve_gb=0)
        self.assertIn('final_media_integrity_report_required',before['blocking_checks'])
        valid=doctor.assess(self.work,'review',source=str(p),ffmpeg=self.ff,ffprobe=self.fp,reserve_gb=0,media_check_report=report)
        self.assertEqual(valid['blocking_checks'],[])
        p.write_bytes(p.read_bytes()+b'changed')
        stale=doctor.assess(self.work,'review',source=str(p),ffmpeg=self.ff,ffprobe=self.fp,reserve_gb=0,media_check_report=report)
        self.assertIn('final_media_integrity_report_stale',stale['blocking_checks'])

    def test_final_task_requires_exact_media_report_and_correct_voice_policy(self):
        p=self.media('成片.mp4');cover=self.work/'cover.png';cover.write_bytes(b'synthetic cover placeholder')
        state=self.state();task=state['task'];task.update(status='review',deliverables={'video':str(p),'cover':str(cover)})
        with self.assertRaisesRegex(ValueError,'media_integrity'):session.validate(state,self.work)
        report=self.write('final-integrity.json',inspect_media(p,self.ff,self.fp))
        task['deliverables']['media_integrity']=str(report)
        session.validate(state,self.work)
        task['voice_plan']={'status':'silent_preview'}
        with self.assertRaisesRegex(ValueError,'audio policy'):session.validate(state,self.work)

    def test_review_doctor_cli_is_not_green_without_final_media_evidence(self):
        r=self.cli('doctor.py','--workspace',self.work,'--stage','review','--reserve-gb','0')
        self.assertEqual(r.returncode,2)
        self.assertIn('final_media_required',json.loads(r.stdout)['blocking_checks'])

    def test_truncated_media_cannot_pass_as_copy_verified(self):
        p=self.media('坏片.mp4');p.write_bytes(p.read_bytes()[:200])
        report=self.work/'坏片检查.json'
        r=self.cli('check_media_integrity.py','--media',p,'--report',report,'--ffmpeg',self.ff,'--ffprobe',self.fp)
        self.assertEqual(r.returncode,2);self.assertEqual(json.loads(report.read_text())['status'],'failed')

    def test_content_plan_stops_at_missing_failed_or_stale_source_report(self):
        state=self.state();source=self.work/'source.mp4';source.write_bytes(b'synthetic corrupt input')
        state['task'].update(mode='talking-head',source=str(source))
        self.assertEqual(session.plan(state,self.work)['action'],'check_source_integrity')
        receipt=self.write('source-integrity.json',{'media_sha256':digest(source),'status':'failed','issues':['decode_error']})
        state['task']['source_integrity']=str(receipt)
        self.assertEqual(session.plan(state,self.work)['action'],'review_source_issue')
        source.write_bytes(b'changed')
        self.assertEqual(session.plan(state,self.work)['action'],'check_source_integrity')

    def test_explicit_video_without_audio_can_be_file_checked(self):
        from check_video_export import inspect
        if not self.ff or not self.fp:self.skipTest('FFmpeg unavailable')
        p=self.work/'intentional-silent.mp4'
        subprocess.run([self.ff,'-v','error','-f','lavfi','-i','color=c=blue:s=96x96:r=30:d=1',
                        '-c:v','libx264','-pix_fmt','yuv420p','-colorspace','bt709',str(p)],check=True)
        spec={'width':96,'height':96,'fps':'30/1','video_codec':'h264','audio_codec':None,'color_space':'bt709','expected_frames':30}
        self.assertEqual(inspect(p,spec,self.fp)['errors'],[])
        self.assertEqual(inspect_media(p,self.ff,self.fp,'intentional-silent')['status'],'passed')

    def test_formal_bundle_cannot_pair_an_old_video_with_a_new_delivered_file(self):
        from manage_video_editing_run import CHECKS, init
        original=self.media('bundle-video.mp4');current=self.work/'new-video.mp4'
        current.write_bytes(original.read_bytes()+b'different delivered revision')
        cover=self.work/'cover.png';cover.write_bytes(b'synthetic cover fixture')
        assets=[]
        for role in ('video','cover','subtitles','caption_source','cover_source','render_master','source_map','native_project_archive'):
            source=original if role in ('video','render_master') else cover if role=='cover' else self.work/(role+'.txt')
            if not source.exists():source.write_text('synthetic fixture',encoding='utf-8')
            assets.append({'role':role,'path':str(source),'name':role+source.suffix})
        spec={'revision':'r1','assets':assets,'render':{},'native':{'revision':'r1',
              'same_revision':True,'opened_in_editor':True,'linked_media_verified':True,'render_checked':True}}
        bundle=self.work/'bundle';delivery.build(spec,self.work,bundle)
        state=self.state();task=state['task'];task.update(status='review',delivery_scope='formal',
            deliverables={'video':str(current),'cover':str(cover),'delivery_manifest':str(bundle/'project.json')})
        task['deliverables']['media_integrity']=str(self.write('integrity.json',inspect_media(current,self.ff,self.fp)))
        init(task,'r1',{key:{'required':False,'reason':'Synthetic gate unit fixture only'} for key in CHECKS})
        with self.assertRaisesRegex(ValueError,'video differs'):session.validate(state,self.work)


if __name__=='__main__':unittest.main()
