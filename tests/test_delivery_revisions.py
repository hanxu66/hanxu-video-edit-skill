"""Synthetic file/receipt gates only; no native editor was opened by these tests."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import delivery
import session


class DeliveryRevisions(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('TEST_WORKSPACE', str(ROOT.parent / 'test-work')))
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='revision 中文 ', dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()

    def spec(self, revision='r2', bound=True):
        assets = []
        for role in ('video', 'cover', 'subtitles', 'caption_source', 'cover_source',
                     'render_master', 'source_map', 'native_project_archive', 'reopen_observation'):
            source = self.work / (role + '.txt')
            source.write_text('Synthetic ' + revision + ' ' + role + '; not a native editor result', encoding='utf-8')
            assets.append({'role': role, 'path': str(source), 'name': role + '.txt'})
        native = dict(revision=revision, same_revision=True, opened_in_editor=True,
                      linked_media_verified=True, render_checked=True)
        if bound:
            native.update(editor='Jianying', saved_project_id='synthetic-final-project',
                          reopened_project_id='synthetic-restored-project',
                          restore_scope='same_machine_relocated', observed_at='2026-10-30T20:30:00+08:00',
                          asset_sha256={a['role']: delivery.digest(Path(a['path'])) for a in assets},
                          evidence_roles=['reopen_observation'])
        return dict(revision=revision, assets=assets, render={}, native=native)

    def build(self, spec, name='bundle'):
        folder = self.work / name
        result = delivery.build(spec, self.work, folder)
        return folder, result

    def test_four_booleans_do_not_certify_a_native_reopen(self):
        _, result = self.build(self.spec(bound=False))
        self.assertEqual(result['delivery_level'], 'review_bundle_native_pending')
        self.assertFalse(result['native_final_verified'])

    def test_reopen_proof_must_bind_the_exact_archive_and_subtitles(self):
        for role in ('native_project_archive', 'subtitles'):
            with self.subTest(role=role):
                spec = self.spec()
                asset = next(a for a in spec['assets'] if a['role'] == role)
                Path(asset['path']).write_text('Different revision bytes', encoding='utf-8')
                _, result = self.build(spec, 'mismatch-' + role)
                self.assertFalse(result['native_final_verified'])

    def test_upstream_project_cannot_be_the_final_native_project(self):
        spec = self.spec(); spec['native']['editor'] = 'ChatCut'
        _, result = self.build(spec)
        self.assertFalse(result['native_final_verified'])

    def test_missing_or_stale_observation_never_reports_native_verified(self):
        folder, result = self.build(self.spec())
        self.assertTrue(result['native_final_verified'])
        (folder / 'reopen_observation.txt').write_text('changed observation', encoding='utf-8')
        result = delivery.verify(folder)
        self.assertFalse(result['native_final_verified'])
        self.assertEqual(result['status'], 'failed')

    def test_receipt_and_assets_survive_relocation_and_process_restart(self):
        folder, result = self.build(self.spec())
        self.assertTrue(result['native_final_verified'])
        moved = self.work / 'relocated'; shutil.copytree(folder, moved)
        import subprocess
        p = subprocess.run([sys.executable, '-B', str(moved / 'delivery.py'), 'verify', '--bundle', str(moved)],
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertTrue(report['native_final_verified'])
        self.assertIn('recorded', report['verification_scope'])

    def test_manifest_cannot_drop_subtitles_but_keep_native_complete(self):
        folder, _ = self.build(self.spec())
        path = folder / 'project.json'; data = json.loads(path.read_text(encoding='utf-8'))
        data['assets'] = [a for a in data['assets'] if a['role'] != 'subtitles']
        path.write_text(json.dumps(data), encoding='utf-8')
        report = delivery.verify(folder)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['native_final_verified'])

    def test_separately_delivered_subtitles_must_match_the_bundle(self):
        folder, _ = self.build(self.spec())
        task = session.fresh_task({})
        # Isolate the final bundle gate here; current media checks have separate
        # real FFmpeg fixtures in test_feedback_regressions.
        task.pop('qa_contract')
        task.update(status='review', delivery_scope='formal', revision='r2',
                    deliverables={'video':str(folder/'video.txt'), 'cover':str(folder/'cover.txt'),
                                  'subtitles':str(folder/'subtitles.txt'),
                                  'delivery_manifest':str(folder/'project.json')})
        state = {'profile':{}, 'task':task}; session.validate(state,self.work)
        stale = self.work/'old-subtitles.srt'; stale.write_text('Old revision captions',encoding='utf-8')
        task['deliverables']['subtitles'] = str(stale)
        with self.assertRaisesRegex(ValueError, 'subtitles.*differs'):
            session.validate(state,self.work)

    def test_old_receipt_can_enter_revision_without_bypassing_new_review_gate(self):
        folder, _ = self.build(self.spec(bound=False))
        manifest=folder/'project.json';data=json.loads(manifest.read_text(encoding='utf-8'))
        data['delivery_level']='native_complete'  # Model an old-format stored task.
        manifest.write_text(json.dumps(data),encoding='utf-8')
        task=session.fresh_task({});task.pop('qa_contract')
        task.update(status='review',stage='review',revision='r2',delivery_scope='formal',
                    deliverables={'video':str(folder/'video.txt'),'cover':str(folder/'cover.txt'),
                                  'delivery_manifest':str(manifest)})
        directory=self.work/'.video-agent';directory.mkdir()
        state_path=directory/'session.json';state_path.write_text(json.dumps({'profile':{},'task':task}),encoding='utf-8')
        patch=self.work/'patch.json'
        import subprocess
        def update(value,scope):
            patch.write_text(json.dumps(value),encoding='utf-8')
            return subprocess.run([sys.executable,'-B',str(ROOT/'scripts/session.py'),'--workspace',str(self.work),
                                   'update','--scope',scope,'--data-file',str(patch)],capture_output=True,text=True,encoding='utf-8')
        old=state_path.read_bytes()
        rejected=update({'status':'review'},'task')
        self.assertEqual(rejected.returncode,2);self.assertEqual(state_path.read_bytes(),old)
        revised=update({'name_size':20},'overrides')
        self.assertEqual(revised.returncode,0,revised.stderr)
        current=json.loads(state_path.read_text(encoding='utf-8'))
        self.assertEqual(current['task']['status'],'working')
        self.assertEqual(current['task']['deliverables'],{})
        self.assertEqual(current['revision_history'][-1]['task'],task)


if __name__ == '__main__':
    unittest.main()
