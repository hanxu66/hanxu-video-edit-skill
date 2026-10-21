"""Run on actual Windows/macOS; fixtures are synthetic and never private media."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import doctor
import delivery
import platform_support as host
import session


class Compatibility(unittest.TestCase):
    def setUp(self):
        cache=Path(os.environ.get('TEST_WORKSPACE',str(ROOT.parent/'compatibility-work')))
        cache.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='中文 user space ',dir=cache)
        self.base=Path(self.temp.name)
        self.workspace=self.base/"剪辑 O'Brien";self.workspace.mkdir()

    def tearDown(self): self.temp.cleanup()

    def cli(self,script,*args):
        # Explicit UTF-8 files/console must work even under a legacy Windows
        # default encoding. This is also executed on Windows in Actions.
        env=dict(os.environ,PYTHONUTF8='0',PYTHONIOENCODING='cp1252',PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts'/script),*map(str,args)],capture_output=True,encoding='utf-8',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_unicode_intake_identity_and_protected_source(self):
        self.cli('session.py','--workspace',self.workspace,'init')
        raw=self.base/'中文原片.mp4';raw.write_bytes(b'synthetic intake fixture; not playable footage')
        task=self.workspace/'task.json';task.write_text(json.dumps({'source':str(raw),'mode':'livestream-clip'},ensure_ascii=False),encoding='utf-8-sig')
        self.cli('session.py','--workspace',self.workspace,'update','--scope','task','--data-file',task)
        identity=self.workspace/'identity.json';identity.write_text(json.dumps({'display_name':'演示用户','title_lines':['测试身份'],'show_identity':True},ensure_ascii=False),encoding='utf-8')
        self.cli('session.py','--workspace',self.workspace,'update','--scope','overrides','--data-file',identity)
        result=self.cli('session.py','--workspace',self.workspace,'protect-source','--reserve-gb','0')
        self.assertEqual(Path(result['working_path']).read_bytes(),raw.read_bytes())
        result=self.cli('session.py','--workspace',self.workspace,'plan')
        self.assertEqual(result['effective_settings']['display_name'],'演示用户')
        self.assertEqual(result['effective_settings']['speed'],1.2)
        other=self.base/'另一个目录';other.mkdir()
        self.assertEqual(self.cli('session.py','--workspace',other,'init')['profile'],{})
        self.assertTrue(raw.exists())

    def test_all_modes_keep_defaults_and_explicit_overrides(self):
        for mode in session.MODES:
            work=self.base/mode;work.mkdir();raw=work/'素材.mp4';raw.write_bytes(b'synthetic')
            data=work/'task.json';data.write_text(json.dumps({'mode':mode,'source':str(raw)}),encoding='utf-8')
            self.cli('session.py','--workspace',work,'update','--scope','task','--data-file',data)
            state=self.cli('session.py','--workspace',work,'status')
            expected=1.2 if mode in ('livestream-clip','livestream-card') else 1.0 if mode in ('interview','user-interview') else 1.1
            self.assertEqual(session.effective(state)['speed'],expected)
            state['task']['overrides']={'speed':1.3}
            self.assertEqual(session.effective(state)['speed'],1.3)
            self.assertEqual(session.effective(state)['caption_size'],session.LOOK['defaults']['caption_size'])

    def test_native_editor_not_assumed_from_installation(self):
        r=doctor.assess(self.workspace,'readiness',reserve_gb=0)
        self.assertIn('jianying_control_available',r['blocking_checks'])
        self.assertEqual(r['applications']['connection'],'not_checked')
        self.assertEqual(r['host']['system'],host.platform.system())

    def test_windows_discovery_paths_without_launching_apps(self):
        local=self.base/'LocalAppData';exe=local/'JianyingPro/Apps/9.0/JianyingPro.exe'
        exe.parent.mkdir(parents=True);exe.write_bytes(b'non-executable detection fixture')
        chat=local/'Programs/ChatCut/ChatCut.exe';chat.parent.mkdir(parents=True);chat.write_bytes(b'fixture')
        with patch.dict(os.environ,{'LOCALAPPDATA':str(local)},clear=False),patch.object(host,'windows_registry_apps',return_value=[]):
            found=host.application_candidates('Windows')
        self.assertIn(exe,found['Jianying']);self.assertIn(chat,found['ChatCut'])

    def test_runtime_plan_and_read_only_probe(self):
        before=set(self.workspace.rglob('*'))
        r=self.cli('setup_local_dependencies.py','--workspace',self.workspace)
        self.assertEqual(set(self.workspace.rglob('*')),before)
        self.assertEqual(Path(r['python']).parts[-2:],('Scripts','python.exe') if os.name=='nt' else ('bin','python'))
        r=self.cli('inspect_video_editing_environment.py','--path',self.workspace,'--min-free-gb','0')
        self.assertEqual(r['capacityStatus'],'sufficient')
        self.assertEqual(r['applicationHealth'],'not_tested')

    def test_caption_audit_unicode_report(self):
        cards=self.workspace/'字幕.json';cards.write_text(json.dumps([{'text':'中文测试','start_us':0,'end_us':1000000}],ensure_ascii=False),encoding='utf-8-sig')
        report=self.workspace/'检查.json'
        result=self.cli('review_media.py','--workspace',self.workspace,'--cards',cards,'--report',report)
        self.assertEqual(result['objective_status'],'passed')
        self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['cards'],1)

    def test_project_local_dependency_install(self):
        if not os.environ.get('REQUIRE_MEDIA_TEST'):self.skipTest('Optional network dependency install runs in CI')
        result=self.cli('setup_local_dependencies.py','--workspace',self.workspace,'--install')
        self.assertTrue(result['installed']);self.assertTrue(Path(result['python']).is_file())
        result=subprocess.run([result['python'],'-c','from PIL import ImageFont; print("ok")'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def make_bundle(self,master=None):
        assets=[]
        for role,ext in [('video','.mp4'),('cover','.png'),('subtitles','.srt'),('caption_source','.ass'),('cover_source','.svg'),('render_master','.mp4'),('source_map','.json')]:
            p=self.workspace/(role+ext);p.write_bytes(b'synthetic '+role.encode())
            if role=='render_master' and master:shutil.copy2(master,p)
            if role=='caption_source':p.write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 240\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,22,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.00,0:00:00.90,Default,,0,0,0,,Compatibility\n',encoding='utf-8')
            assets.append({'role':role,'path':str(p),'name':'sources/'+p.name})
        spec={'revision':'r-test','assets':assets,'render':{'caption_clear_region':[0,190,320,50],'fps':30,'bitrate':300000}}
        out=self.workspace/'delivery';delivery.build(spec,self.workspace,out)
        return out

    def test_delivery_relocation_and_path_rules(self):
        bundle=self.make_bundle();moved=self.base/'换目录交付';shutil.copytree(bundle,moved)
        self.assertEqual(delivery.verify(moved)['status'],'passed')
        self.assertFalse(delivery.verify(moved)['native_final_verified'])
        for name in ('C:/escape.mp4','../escape.mp4','CON.txt','aux.srt','source./v.mp4'):
            with self.assertRaises(ValueError):delivery.relative(name)
        manifest=json.loads((moved/'project.json').read_text(encoding='utf-8'))
        (moved/manifest['assets'][0]['path']).write_bytes(b'changed')
        self.assertEqual(delivery.verify(moved)['status'],'failed')

    def test_real_ffmpeg_rebuild_in_unicode_apostrophe_workspace(self):
        ffmpeg=os.environ.get('TEST_FFMPEG') or shutil.which('ffmpeg')
        ffprobe=os.environ.get('TEST_FFPROBE') or shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            if os.environ.get('REQUIRE_MEDIA_TEST'):self.fail('CI must provide actual FFmpeg and FFprobe')
            self.skipTest('Real FFmpeg/FFprobe unavailable in this local runtime')
        fonts=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/arial.ttf',Path('/System/Library/Fonts/Supplemental/Arial.ttf')]
        font=next((p for p in fonts if p.is_file()),None)
        if font is None:self.fail('Expected native test font missing; no production font assumed')
        master=self.base/'original.mp4'
        subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','color=c=blue:s=320x240:r=30','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-c:a','aac',str(master)],check=True)
        bundle=self.make_bundle(master)
        try:
            result=delivery.rebuild(bundle,ffmpeg,font,bundle/'rebuild.mp4','libx264')
        except ValueError:
            log=bundle/'rebuild.log'
            if log.is_file():print(log.read_text(encoding='utf-8',errors='replace')[-5000:],file=sys.stderr)
            raise
        subprocess.run([str(ffmpeg),'-v','error','-i',result['output'],'-f','null','-'],check=True)
        data=json.loads(subprocess.check_output([str(ffprobe),'-v','error','-show_format','-show_streams','-of','json',result['output']]))
        self.assertEqual(data['streams'][0]['codec_name'],'h264')
        self.assertEqual(data['streams'][0]['width'],320)
        pixels=subprocess.check_output([str(ffmpeg),'-v','error','-ss','0.5','-i',result['output'],'-vf','crop=320:50:0:190','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
        white=sum(1 for i in range(0,len(pixels),3) if min(pixels[i:i+3])>180)
        self.assertGreater(white,20,'ASS text was not actually rendered on the black caption region')
        def audiohash(p):return subprocess.check_output([str(ffmpeg),'-v','error','-i',str(p),'-map','0:a:0','-c','copy','-f','streamhash','-hash','sha256','-'])
        self.assertEqual(audiohash(master),audiohash(result['output']))


if __name__=='__main__':unittest.main(verbosity=2)
