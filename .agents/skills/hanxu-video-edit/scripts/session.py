#!/usr/bin/env python3
"""Project-local editing intake, preference isolation and read-only observations."""
import argparse
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import shutil
import sys
import uuid

MODES = ('talking-head', 'talking-head-ppt', 'talking-head-story', 'ai-only',
         'interview', 'user-interview', 'livestream-clip', 'livestream-card')
ANIMATION_MODES = ('talking-head-ppt', 'talking-head-story', 'ai-only')
SCENE_METHODS = {'interview': 'references/访谈剪辑.md',
                 'user-interview': 'references/访谈剪辑.md',
                 'livestream-clip': 'references/直播切片剪辑.md',
                 'livestream-card': 'references/直播切片剪辑.md'}
SETTINGS = dict(display_name=None, title_lines=None, show_identity=None,
                visual_style='dark-orange', add_bgm=False, voice_source=None)
LOOK = json.loads((Path(__file__).resolve().parents[1] / 'assets/default-look.json').read_text())
SETTINGS.update(LOOK['defaults'])
TASK_KEYS = {'mode', 'source', 'script', 'brief', 'audio', 'script_approved',
             'animation_type', 'animation_scope', 'avatar_mode',
             'delivery_scope', 'stage', 'status', 'preflight', 'project',
             'revision', 'deliverables', 'checks', 'blocker', 'user_quote',
             'source_protection', 'visual_plan', 'scene_plan'}
STAGES = ('intake', 'content', 'editing', 'appearance', 'handoff', 'packaging', 'export', 'review', 'done')
STATUSES = ('intake', 'working', 'blocked', 'review', 'accepted', 'paused')


def now():
    return datetime.now(timezone.utc).isoformat()


def inside(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('State/output path escapes the selected workspace')
    return resolved


def settings_valid(data):
    if not isinstance(data, dict) or set(data) - set(SETTINGS):
        raise ValueError('Unknown preference keys')
    for key, value in data.items():
        if value is None:
            continue
        if key in ('show_identity', 'add_bgm'):
            if type(value) is not bool:
                raise ValueError(key + ' must be boolean or null')
        elif key == 'title_lines':
            if not isinstance(value, list) or len(value) > 2 or not all(isinstance(x, str) for x in value):
                raise ValueError('title_lines must contain zero to two strings')
        elif key in ('beauty_parameters', 'body_parameters', 'color_adjustments', 'framing'):
            if not isinstance(value, dict) or set(value) - set(SETTINGS[key]):
                raise ValueError(key + ' contains unknown controls')
            for control, number in value.items():
                if type(number) not in (int, float) or not math.isfinite(number):
                    raise ValueError(key + ' controls must be finite numbers')
                if key in ('beauty_parameters', 'body_parameters') and not 0 <= number <= 1:
                    raise ValueError(key + ' values must be between 0 and 1')
                if key == 'color_adjustments' and not -100 <= number <= 100:
                    raise ValueError('color controls must be between -100 and 100')
                if key == 'framing' and control == 'scale' and not 0.1 <= number <= 10:
                    raise ValueError('framing scale out of range')
        elif key in ('speed', 'audio_gain_db', 'caption_size', 'name_size', 'title_size', 'title_scale'):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(key + ' must be a finite number')
            if key == 'speed' and not 0.25 <= value <= 4:
                raise ValueError('speed out of supported configuration range')
            if key.endswith('_size') or key == 'title_scale':
                if value <= 0:
                    raise ValueError(key + ' must be positive')
        elif not isinstance(value, str):
            raise ValueError(key + ' must be text or null')


def merge_settings(target, patch):
    patch = deepcopy(patch)
    if isinstance(patch.get('title_lines'), str):
        patch['title_lines'] = patch['title_lines'].splitlines()
    settings_valid(patch)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key].update(value)
        else:
            target[key] = value


def fresh_task(profile):
    snapshot = deepcopy(SETTINGS)
    merge_settings(snapshot, profile)
    return dict(id='video-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6],
                mode=None, source=None, script=None, brief=None, audio=None,
                script_approved=False, animation_type=None,
                animation_scope='local', avatar_mode='none', delivery_scope='formal',
                stage='intake', status='intake', overrides={}, preflight={},
                profile_snapshot=snapshot,
                project=None, revision='r1', deliverables={}, checks={}, blocker=None,
                source_protection={}, visual_plan={}, scene_plan={})


def validate(state, root):
    settings_valid(state['profile'])
    task = state.get('task')
    if task is None:
        return
    settings_valid(task['overrides'])
    settings_valid(task.get('profile_snapshot', {}))
    if task['mode'] is not None and task['mode'] not in MODES:
        raise ValueError('Unknown editing mode')
    if task['stage'] not in STAGES or task['status'] not in STATUSES:
        raise ValueError('Unknown stage/status')
    if type(task['script_approved']) is not bool:
        raise ValueError('script_approved must be boolean')
    if task['animation_type'] not in (None, 'PPT型', '故事型'):
        raise ValueError('Unknown animation type')
    if task['avatar_mode'] not in ('none', 'circle') or task['animation_scope'] not in ('local', 'main'):
        raise ValueError('Unknown avatar/animation scope')
    if task['delivery_scope'] not in ('formal', 'review'):
        raise ValueError('Unknown delivery scope')
    for key in ('source', 'script', 'audio'):
        path = task.get(key)
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ValueError(key + ' must be a file path or null')
    for key in ('preflight', 'checks', 'deliverables', 'scene_plan'):
        if key == 'scene_plan' and key not in task:  # Older paused tasks stay unchanged.
            continue
        if not isinstance(task[key], dict):
            raise ValueError(key + ' must be an object')
    for label, raw in task['deliverables'].items():
        if not isinstance(raw, str) or not inside(Path(raw), root).is_file():
            raise ValueError('Deliverable does not exist inside workspace: ' + label)
    if task['status'] in ('review', 'accepted') and not {'video', 'cover'} <= set(task['deliverables']):
        raise ValueError('Review/acceptance requires actual video and cover files')
    if task['delivery_scope'] == 'formal' and task['status'] in ('review', 'accepted'):
        raw = task['deliverables'].get('delivery_manifest')
        if not raw:
            raise ValueError('Formal delivery requires a matching, verified native-project delivery manifest; use review scope for a preview')
        manifest = json.loads(inside(Path(raw), root).read_text())
        if manifest.get('revision') != task['revision'] or manifest.get('delivery_level') != 'native_complete':
            raise ValueError('Native final project is missing, stale or not verified')
        from delivery import verify
        if verify(Path(raw).parent)['errors']:
            raise ValueError('Native delivery assets have changed or are missing')


def effective(state):
    result = deepcopy(SETTINGS)
    task = state.get('task')
    layers = [task.get('profile_snapshot', state['profile']) if task else state['profile']]
    if task:
        layers.append(task['overrides'])
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key].update(value)
            else:
                result[key] = value
    return result


def plan(state, root):
    task = state.get('task')
    result = {'effective_settings': effective(state), 'task_id': task['id'] if task else None}
    def answer(action, message, **extras):
        return dict(result, action=action, message=message, **extras)
    if task and task['status'] == 'paused':
        return answer('paused', '本条已按使用者要求暂停，保留素材引用、工程和小样；明确要求继续时才恢复。', deliverables=task['deliverables'])
    if not task or not any(task.get(k) for k in ('source', 'script', 'brief')):
        return answer('ask_material', '你这次有拍好的视频，还是想从文字做成视频？有视频可以直接发来，或者告诉我文件位置。')
    missing_inputs = [key for key in ('source', 'script', 'audio') if task.get(key) and not Path(task[key]).expanduser().is_file()]
    if missing_inputs:
        return answer('recover_material', '输入文件已不在原位置。保留剪辑和已有小样，只补文件位置；确认无法补回时由使用者决定暂停或看注明来源的静态排版。', missing_inputs=missing_inputs, deliverables=task['deliverables'])
    if task['status'] == 'blocked':
        return answer('resume_checkpoint', '重新核对当前受阻步骤的条件；不重复索取已给信息，不重做无关步骤。', blocker=task['blocker'])
    if task['status'] in ('review', 'accepted'):
        return answer('review' if task['status'] == 'review' else 'accepted', '查看当前修订或接收局部返修。', deliverables=task['deliverables'])
    mode = task['mode']
    if mode is None:
        return answer('ask_form', '这条想保留真人口播，还是加入讲解动画？不确定的话，我可以根据素材推荐。')
    result['method'] = SCENE_METHODS.get(mode, 'references/动画视频制作.md' if mode == 'ai-only' else 'references/口播剪辑.md')
    if mode in ('interview', 'user-interview'):
        result['scene_review'] = '按说话人和问答关系处理；用户反馈访谈使用结构卡与原话真实性检查。'
    if mode in ('livestream-clip', 'livestream-card'):
        result['presentation'] = 'card' if mode == 'livestream-card' else 'video'
        result['source_index_required'] = True
    result['animation_type'] = {'talking-head-ppt': 'PPT型', 'talking-head-story': '故事型'}.get(mode, task['animation_type'])
    if mode != 'ai-only' and not task['source']:
        return answer('ask_video', '请提供这条的原视频或它在电脑上的文件位置。')
    if task.get('source') and not Path(task['source']).expanduser().resolve().is_relative_to(root):
        return answer('protect_material', '先把项目外原片保存为本条可校验的工作副本，再导入剪辑；不要求使用者手动复制。')
    if mode == 'ai-only':
        if not task['script']:
            return answer('draft_script', '根据已有主题和真实材料准备确认稿，不编造个人事实。')
        if not task['script_approved']:
            return answer('confirm_script', '把本条完整稿件交使用者确认后再生成配音。')
        if task['animation_type'] is None:
            return answer('ask_animation_type', '画面想用观点图解（PPT型），还是用连续动作讲故事？')
        if not task['audio'] and not result['effective_settings']['voice_source']:
            return answer('ask_voice', '声音用你已有的录音，还是选择一个可用的AI配音音色？')
    if mode == 'user-interview' and task['stage'] in ('editing', 'appearance', 'handoff', 'packaging', 'export'):
        if task.get('scene_plan', {}).get('purpose') != 'research' and not task['checks'].get('interview_structure_approved'):
            return answer('review_interview_structure', '先展示原话、唯一主转变、拟保留内容与调序理由；用户确认结构卡后再正式剪辑。')
    if mode == 'livestream-card' and task['stage'] in ('appearance', 'packaging', 'export'):
        if not task['checks'].get('card_style_approved'):
            return answer('calibrate_card_style', '本条是回放剪成卡片式短视频。沿已确认卡片参考制作实际版式小样；现有参考未找到时只补该项，不当作直播间贴片，不把新设计冒称原版。')
    if task['stage'] in ('appearance', 'packaging', 'export'):
        config = result['effective_settings']
        if config['show_identity'] is None:
            result['identity_pending'] = True
            result['identity_hint'] = '先交实际效果小样；使用者要加身份时再收两行文字，不代填身份、不写成长期不显示。'
        if config['show_identity'] and not (config['display_name'] or config['title_lines']):
            return answer('ask_identity_text', '请告诉我画面上显示的姓名或身份介绍文字。')
        if not task['checks'].get('visual_preview_ready'):
            preview_caps = ['chatcut_project_readable']
            if mode != 'ai-only':
                preview_caps.append('look_controls_available')
            absent = [key for key in preview_caps if task['preflight'].get(key) is not True]
            if absent:
                return answer('check_tools', '只核对当前完整效果小样需要的实际能力，缺项不假称已应用。', missing_capabilities=absent)
            return answer('make_visual_preview', '按完整默认或本条调整制作代表片段：美颜美体、画面/调色、音量、字幕与身份样式逐项落实，直接交实际小样。', look_id=LOOK['id'])
        if not task['checks'].get('visual_preview_approved'):
            return answer('review_visual_preview', '直接展示实际小样，使用者可按默认继续，或用自然语言修改某一项。', deliverables=task['deliverables'])
    required = ['chatcut_project_readable']
    if task['delivery_scope'] == 'formal' and task['stage'] in ('handoff', 'packaging', 'export', 'review', 'done'):
        required += ['editable_handoff_available', 'jianying_control_available']
    if mode in ANIMATION_MODES and task['stage'] not in ('intake', 'content', 'editing'):
        required += ['motion_graphics_available']
    if mode == 'ai-only' and not task['audio']:
        required += ['voice_generation_available']
    missing = [name for name in required if task['preflight'].get(name) is not True]
    if missing:
        return answer('check_tools', '先实际核对本条所需工具，只有需本人操作时再提示。', missing_capabilities=missing)
    return answer('execute', '按当前方法、用户配置和真实检查点继续制作。')


def protect_source(state, root, reserve_gb):
    task = state.get('task')
    if not task or not task.get('source'):
        raise ValueError('No source video has been received')
    source = Path(task['source']).expanduser().resolve(strict=True)
    if source.is_relative_to(root):
        return {'action': 'already_local', 'path': str(source)}
    if not math.isfinite(reserve_gb) or reserve_gb < 0:
        raise ValueError('reserve-gb must be finite and non-negative')
    size = source.stat().st_size
    required = size + int(reserve_gb * 1024 ** 3)
    if shutil.disk_usage(root).free < required:
        raise ValueError('Insufficient space for a protected source copy; preserve the original file and only defer copying/import')
    def digest(path):
        h = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    folder = inside(root / '.video-agent' / 'media' / task['id'], root)
    folder.mkdir(parents=True, exist_ok=True)
    dest = inside(folder / source.name, root)
    source_hash = digest(source)
    if dest.exists() and digest(dest) != source_hash:
        raise ValueError('Protected source name already exists with different bytes')
    if not dest.exists():
        temp = inside(folder / (source.name + '.copying'), root)
        shutil.copy2(source, temp)
        if digest(temp) != source_hash or digest(source) != source_hash:
            raise ValueError('Source changed or copy verification failed; retained the copy checkpoint')
        os.replace(temp, dest)
    task['source_protection'] = dict(original_path=str(source), working_path=str(dest), sha256=source_hash, bytes=size, verified_at=now())
    task['source'] = str(dest)
    return {'action': 'source_protected', **task['source_protection']}


def environment(root):
    apps = {}
    candidates = {'ChatCut': ('ChatCut.app',),
                  '剪映': ('剪映专业版.app', 'JianyingPro.app', 'VideoFusion-macOS.app'),
                  'CapCut': ('CapCut.app',)}
    for name, dirs in candidates.items():
        found = []
        for base in (Path('/Applications'), Path.home() / 'Applications'):
            for leaf in dirs:
                app = base / leaf
                if app.exists():
                    item = {'path': str(app), 'version': None}
                    try:
                        with open(app / 'Contents/Info.plist', 'rb') as handle:
                            item['version'] = plistlib.load(handle).get('CFBundleShortVersionString')
                    except (OSError, plistlib.InvalidFileException):
                        pass
                    found.append(item)
        apps[name] = found
    return {'observed_at': now(), 'platform': platform.system(), 'applications_found': apps,
            'workspace_free_bytes': shutil.disk_usage(root).free,
            'control_and_connection': 'not_tested', 'subscriptions': 'unknown',
            'scope': 'read-only; app existence does not prove tool/project access'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('command', choices=('init', 'status', 'plan', 'new', 'update', 'environment', 'protect-source'))
    parser.add_argument('--reserve-gb', type=float, default=0.5)
    parser.add_argument('--scope', choices=('profile', 'task', 'overrides'))
    parser.add_argument('--data-file', type=Path)
    args = parser.parse_args()
    root = args.workspace.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('workspace must be a directory')
    directory = inside(root / '.video-agent', root)
    path = inside(directory / 'session.json', root)
    if args.command == 'environment':
        from doctor import assess
        result = environment(root)
        result['dependency_report'] = assess(root, 'content')
        print(json.dumps(result, ensure_ascii=False, indent=2)); return
    state = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'profile': {}, 'task': None}
    validate(state, root)
    mutation = args.command in ('init', 'new', 'update', 'protect-source')
    protection_result = protect_source(state, root, args.reserve_gb) if args.command == 'protect-source' else None
    if args.command == 'new':
        if state['task'] and state['task']['status'] not in ('accepted', 'paused'):
            raise ValueError('Active task exists; update/finish it instead of overwriting')
        if state['task']:
            archive = inside(directory / 'history' / (state['task']['id'] + '.json'), root)
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(json.dumps(state['task'], ensure_ascii=False, indent=2))
        state['task'] = fresh_task(state['profile'])
    if args.command == 'update':
        if not args.scope or not args.data_file:
            raise ValueError('update requires scope and project-local JSON patch')
        patch = json.loads(inside(args.data_file, root).read_text())
        if not isinstance(patch, dict):
            raise ValueError('patch must be an object')
        if args.scope == 'profile':
            merge_settings(state['profile'], patch)
        else:
            if state['task'] is None:
                state['task'] = fresh_task(state['profile'])
            if args.scope == 'overrides':
                merge_settings(state['task']['overrides'], patch)
            else:
                if set(patch) - TASK_KEYS:
                    raise ValueError('Unknown task keys')
                for key in ('source', 'script', 'audio'):
                    if key in patch and patch[key] is not None and patch[key] != state['task'].get(key):
                        if not isinstance(patch[key], str) or not Path(patch[key]).expanduser().is_file():
                            raise ValueError(key + ' must reference a real readable new input file')
                changed_source = any(key in patch and patch[key] != state['task'].get(key)
                                     for key in ('source', 'script', 'audio', 'mode', 'animation_type'))
                if changed_source:
                    state['task'].update(checks={}, deliverables={}, stage='intake', status='intake')
                if 'mode' in patch and patch['mode'] != state['task'].get('mode'):
                    is_ai = patch['mode'] == 'ai-only'
                    state['task']['delivery_scope'] = 'review' if is_ai else 'formal'
                    state['task']['animation_scope'] = 'main' if is_ai else 'local'
                    # Freeze a scene-specific baseline only on explicit mode change.
                    # Never rewrite a paused legacy task during status/plan reads.
                    baseline = deepcopy(SETTINGS)
                    if patch['mode'] in ('livestream-clip', 'livestream-card'):
                        baseline.update(speed=1.2, title_size=8)
                    elif patch['mode'] in ('interview', 'user-interview'):
                        baseline.update(speed=1.0, audio_gain_db=None,
                                        beauty_parameters={k: 0 for k in SETTINGS['beauty_parameters']},
                                        body_parameters={k: 0 for k in SETTINGS['body_parameters']})
                    merge_settings(baseline, state['profile'])
                    state['task']['profile_snapshot'] = baseline
                    state['task']['scene_plan'] = {}
                for key, value in patch.items():
                    if key in ('preflight', 'checks', 'deliverables') and isinstance(value, dict):
                        state['task'][key].update(value)
                    else:
                        state['task'][key] = value
    validate(state, root)
    if mutation:
        directory.mkdir(exist_ok=True)
        state['updated_at'] = now()
        temp = inside(directory / 'session.tmp', root)
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        os.replace(temp, path)
    output = plan(state, root) if args.command == 'plan' else protection_result if protection_result else state
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
