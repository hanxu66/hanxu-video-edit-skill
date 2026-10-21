#!/usr/bin/env python3
"""Project-scoped preflight. Installation, connection, resources and QA are distinct."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
from platform_support import binary_candidates, font_candidates, host_info, observe_apps

GIB=1024**3
CAPABILITIES={
 'content':['chatcut_project_readable'],
 'readiness':['chatcut_project_readable','editable_handoff_available','jianying_control_available'],
 'preview':['chatcut_project_readable','editable_handoff_available','jianying_control_available','native_preview_reopened','native_preview_render_checked'],
 'look':['chatcut_project_readable','look_controls_available','font_render_verified','beauty_render_verified'],
 'delivery':['chatcut_project_readable','editable_handoff_available','jianying_control_available'],
 'review':[],
}

def local_path(raw,root):
    p=Path(raw).expanduser().resolve()
    if not p.is_relative_to(root):raise ValueError('Output/receipt path escapes workspace')
    return p

def run(argv):
    try:
        p=subprocess.run(argv,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=8,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1'))
        return p.returncode,(p.stdout if p.returncode==0 else p.stderr[:1800]).strip()
    except (OSError,subprocess.TimeoutExpired) as e:return -1,type(e).__name__

def executable(name,explicit,root):
    candidates=binary_candidates(name,root,explicit)
    fallback=None
    for p in candidates:
        if p.is_file() and os.access(p,os.X_OK):
            code,msg=run([str(p),'-version'])
            if code==0:
                item=dict(status='usable',path=str(p.resolve()),version_line=msg.splitlines()[0] if msg else 'version output empty')
                if name!='ffmpeg':return item
                filter_code,filters=run([str(p),'-hide_banner','-filters'])
                item['ass_filter']=any(len(row.split())>1 and row.split()[1]=='ass' for row in filters.splitlines()) if filter_code==0 else None
                if item['ass_filter']:return item
                if fallback is None:fallback=item
    if fallback:return fallback
    return dict(status='missing_or_unusable',path=None)

def assess(root,stage,source=None,ffmpeg=None,ffprobe=None,font=None,receipt=None,task_id=None,reserve_gb=0.5):
    root=Path(root).resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Choose a separate editing workspace, outside the installed skill')
    binaries={n:executable(n,x,root) for n,x in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]}
    checks={'python':{'status':'usable' if sys.version_info>=(3,9) else 'unsupported','path':sys.executable,'version':platform.python_version()},'pillow':{'status':'available' if importlib.util.find_spec('PIL') else 'missing','needed_for':'glyph width checks only; not intake or native editing'},'font':{'status':'not_checked','needed_for':'exact glyph rendering; no font files distributed'}}
    if font:
        p=Path(font).expanduser()
        checks['font']={'status':'file_readable' if p.is_file() and os.access(p,os.R_OK) else 'missing','path':str(p),'native_app_render':'not_checked'}
    elif platform.system() in ('Darwin','Windows'):
        fonts=font_candidates()
        checks['font']={'status':'file_found' if fonts else 'not_found_locally','paths':fonts,'native_app_render':'not_checked'}
    media={'status':'not_provided'};source_bytes=0;source_outside=False
    if source:
        p=Path(source).expanduser().resolve()
        source_outside=not p.is_relative_to(root)
        media={'status':'readable' if p.is_file() and os.access(p,os.R_OK) else 'missing','path':str(p),'protected_copy_required':source_outside}
        if p.is_file():source_bytes=p.stat().st_size;media['bytes']=source_bytes
        if media['status']=='readable' and binaries['ffprobe']['path']:
            code,msg=run([binaries['ffprobe']['path'],'-v','error','-show_entries','format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate','-of','json',str(p)])
            if code==0:
                try:media['metadata']=json.loads(msg)
                except ValueError:media['probe_status']='output_incomplete'
            else:media['probe_status']='failed'
    observations={}
    if receipt:
        data=json.loads(local_path(receipt,root).read_text(encoding='utf-8-sig'))
        if not task_id or data.get('task_id')!=task_id:raise ValueError('Capability receipt must match an explicit task id')
        for row in data.get('observations',[]):
            if row.get('status') not in ('passed','failed','not_checked'):raise ValueError('Invalid capability status')
            if row.get('status')=='passed' and not all(row.get(k) for k in ('observed_at','evidence')):raise ValueError('Passed capability lacks actual observation/evidence')
            observations[row['capability']]=row
    required=CAPABILITIES[stage]
    unproven=[k for k in required if observations.get(k,{}).get('status')!='passed']
    floor=5*GIB if stage=='delivery' else 0
    budget=int(reserve_gb*GIB)+(source_bytes if source_outside else 0)
    free=shutil.disk_usage(root).free
    blockers=[]
    if host_info()['wsl'] and stage in ('readiness','preview','look','delivery'):
        blockers.append('native_windows_runtime_required_for_windows_desktop')
    if checks['python']['status']!='usable':blockers.append('python_3_9_required')
    if media['status']=='missing':blockers.append('source_missing_keep_progress')
    if media.get('probe_status')=='failed':blockers.append('media_probe_failed')
    if free<floor+budget:blockers.append('insufficient_space')
    if stage=='review':
        blockers.extend(n+'_missing' for n in binaries if binaries[n]['status']!='usable')
    blockers.extend(unproven)
    actions=[]
    if unproven:actions.append({'owner':'agent','action':'Discover actual host tools and read the intended test project, then record evidence; app installation alone is not connection.'})
    if any(x['status']!='usable' for x in binaries.values()):actions.append({'owner':'agent','action':'Locate host-bundled runtime or accept an explicit binary path before asking the user to install anything; do not use a developer-only path.'})
    if checks['pillow']['status']=='missing':actions.append({'owner':'agent','action':'Only if glyph measurement is needed, prepare project-local dependencies; native editing can continue.'})
    return {'schema_version':1,'observed_at':datetime.now(timezone.utc).isoformat(),'workspace':str(root),'stage':stage,'platform':platform.system(),'host':host_info(),'python_checks':checks,'binaries':binaries,'applications':observe_apps(),'source':media,'capacity':{'free_bytes':free,'required_bytes':floor+budget,'sufficient':free>=floor+budget},'capabilities':observations,'blocking_checks':blockers,'stage_status':'ready_for_checked_stage' if not blockers else 'needs_checks_or_action','can_continue_intake':True,'next_actions':actions,'scope':'read-only observations, no install/login/global changes; no proof of media quality or fresh-user success'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,required=True);p.add_argument('--stage',choices=tuple(CAPABILITIES),default='content');p.add_argument('--source');p.add_argument('--ffmpeg');p.add_argument('--ffprobe');p.add_argument('--font');p.add_argument('--capabilities-file');p.add_argument('--task-id');p.add_argument('--reserve-gb',type=float,default=.5);p.add_argument('--report',type=Path)
    a=p.parse_args()
    if not 0<=a.reserve_gb<100000:raise ValueError('Invalid reserve')
    root=a.workspace.resolve(strict=True);report=assess(root,a.stage,a.source,a.ffmpeg,a.ffprobe,a.font,a.capabilities_file,a.task_id,a.reserve_gb)
    if a.report:
        out=local_path(a.report,root);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

# Keep Unicode paths/JSON readable in Windows terminals and redirected output.
for _stream in (sys.stdout, sys.stderr):
    if __name__ == "__main__" and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError) as e:print(json.dumps({'error':str(e)}),file=sys.stderr);raise SystemExit(2)
