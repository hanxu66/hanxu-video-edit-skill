#!/usr/bin/env python3
"""Collect, relocate and verify real delivery assets. Does not update native editors."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import platform
import re
import shutil
import subprocess
import sys

def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()

def within(p,root):
    p=Path(p).expanduser().resolve()
    if not p.is_relative_to(root):raise ValueError('Destination escapes selected workspace')
    return p

def relative(raw):
    p=PurePosixPath(raw)
    if p.is_absolute() or '..' in p.parts or not p.parts or not re.fullmatch(r'[A-Za-z0-9_./-]+',raw):raise ValueError('Use a safe relative ASCII bundle filename')
    if any(part.endswith('.') or part.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for part in p.parts):raise ValueError('Bundle filename is reserved on Windows')
    return p

def copy_file(source,target):
    source,target=Path(source),Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    if platform.system()=='Darwin':
        p=subprocess.run(['cp','-c',str(source),str(target)],capture_output=True)
        if p.returncode==0:return
    shutil.copy2(source,target)

def native_valid(native,revision,roles):
    return all(native.get(k) is True for k in ['same_revision','opened_in_editor','linked_media_verified','render_checked']) and native.get('revision')==revision and 'native_project_archive' in roles

def build(spec,root,out):
    root=root.resolve(strict=True);out=within(out,root)
    skill_root=Path(__file__).resolve().parents[1]
    if (skill_root/'SKILL.md').is_file() and root.is_relative_to(skill_root):raise ValueError('Choose a separate editing workspace, outside the installed skill')
    if out.exists():raise ValueError('Never overwrite an existing delivery; choose a new revision directory')
    names=set();roles=set();assets=[]
    for a in spec['assets']:
        name=str(relative(a['name']));source=Path(a['path']).expanduser()
        if source.is_symlink() or not source.is_file():raise ValueError('Asset must be a real file: '+a['role'])
        if name.casefold() in names or a['role'] in roles:raise ValueError('Duplicate filename or role (case-insensitive)')
        names.add(name.casefold());roles.add(a['role']);assets.append((a,source))
    if not {'video','cover','subtitles','caption_source','cover_source','render_master','source_map'}<=roles:raise ValueError('Missing a required current-revision deliverable or editable source')
    budget=sum(p.stat().st_size for _,p in assets)+1024**3
    if shutil.disk_usage(root).free<budget:raise ValueError('Insufficient space for self-contained delivery and reserve')
    out.mkdir(parents=True)
    entries=[]
    for a,source in assets:
        target=out/a['name'];copy_file(source,target)
        h=digest(target)
        if h!=digest(source):raise ValueError('Copied bytes differ: '+a['role'])
        entries.append({'role':a['role'],'path':a['name'],'bytes':target.stat().st_size,'sha256':h})
    native=spec.get('native',{})
    # A folder or an old draft is not evidence of a matching final native project.
    native_ok=native_valid(native,spec['revision'],roles)
    result={'schema_version':1,'revision':spec['revision'],'created_at':datetime.now(timezone.utc).isoformat(),'delivery_level':'native_complete' if native_ok else 'review_bundle_native_pending','native':native,'assets':entries,'render':spec['render'],'hearing':spec.get('hearing','not_verified'),'limitations':spec.get('limitations',[]),'source_policy':'All listed paths relative to this bundle; software and resource requirements documented separately. Native app archives may retain their own managed caches.'}
    (out/'project.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    copy_file(Path(__file__).resolve(),out/'delivery.py')
    return verify(out)

def verify(folder):
    folder=Path(folder).resolve(strict=True);p=json.loads((folder/'project.json').read_text(encoding='utf-8-sig'));errors=[]
    if p.get('delivery_level')=='native_complete' and not native_valid(p.get('native',{}),p['revision'],{a['role'] for a in p['assets']}):errors.append('native_final_evidence_missing_or_stale')
    for a in p['assets']:
        target=within(folder/str(relative(a['path'])),folder)
        if not target.is_file():errors.append(a['role']+':missing')
        elif target.stat().st_size!=a['bytes'] or digest(target)!=a['sha256']:errors.append(a['role']+':changed')
    return {'revision':p['revision'],'delivery_level':p['delivery_level'],'assets_verified':len(p['assets'])-len(errors),'errors':errors,'status':'passed' if not errors else 'failed','native_final_verified':p['delivery_level']=='native_complete'}

def validate_ass(path):
    path=Path(path)
    text=path.read_text(encoding='utf-8-sig')
    if not all(section in text for section in ('[Script Info]','[Events]')):
        raise ValueError('caption_source/--captions-ass must contain ASS [Script Info] and [Events]; subtitles is the SRT role, JSON card plans are not renderable ASS')
    return path


def rebuild(folder,ffmpeg,font_file,output,encoder,captions_ass=None):
    if encoder=='h264_videotoolbox' and platform.system()!='Darwin':raise ValueError('h264_videotoolbox is Mac-only; use libx264 on Windows')
    folder=Path(folder).resolve(strict=True);out=within(output,folder)
    if out.exists():raise ValueError('Do not overwrite delivered or rebuilt media')
    check=verify(folder)
    if check['errors']:raise ValueError('Restore or intentionally revise changed assets before rebuild')
    project=json.loads((folder/'project.json').read_text(encoding='utf-8-sig'));assets={a['role']:a['path'] for a in project['assets']};config=project['render'];font=Path(font_file).expanduser().resolve(strict=True)
    features=subprocess.run([str(Path(ffmpeg).resolve()),'-hide_banner','-filters'],capture_output=True,text=True,encoding='utf-8',errors='replace')
    if features.returncode or not any(len(line.split())>1 and line.split()[1]=='ass' for line in features.stdout.splitlines()):
        raise ValueError('Selected FFmpeg lacks the ass/libass filter; choose a verified subtitle-capable binary before rebuild. Existing files are preserved.')
    captions=str(relative(assets['caption_source']))
    validate_ass(folder/captions)
    if captions_ass is not None:
        edited=within(captions_ass,folder)
        if not edited.is_file():raise ValueError('Edited captions missing')
        validate_ass(edited)
        captions=str(relative(edited.relative_to(folder).as_posix()))
    # Stage only the caller-selected licensed font inside the bundle cache.
    # Relative ASCII filter paths avoid Windows drive-colon/backslash and
    # apostrophe escaping in libavfilter; subprocess arguments stay separate.
    fontdir=folder/'.runtime-cache/fonts'/digest(font)[:16];fontdir.mkdir(parents=True,exist_ok=True)
    staged=fontdir/('selected'+font.suffix.lower())
    if not staged.exists() or digest(staged)!=digest(font): copy_file(font,staged)
    x,y,w,h=config['caption_clear_region'];vf=f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill,ass={captions}:fontsdir={fontdir.relative_to(folder).as_posix()}"
    args=[str(Path(ffmpeg).resolve()),'-hide_banner','-i',assets['render_master'],'-vf',vf,'-map','0:v:0','-map','0:a:0','-c:v',encoder,'-b:v',str(config['bitrate']),'-pix_fmt','yuv420p','-r',str(config['fps']),'-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-c:a','copy','-movflags','+faststart','-f','mp4',str(out)]
    out.parent.mkdir(parents=True,exist_ok=True)
    cache=folder/'.runtime-cache';temp=cache/'tmp';temp.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,XDG_CACHE_HOME=str(cache),TMPDIR=str(temp),TMP=str(temp),TEMP=str(temp))
    log=out.with_suffix('.log')
    with log.open('w') as f:p=subprocess.run(args,cwd=folder,stdout=f,stderr=f,env=env)
    if p.returncode:raise ValueError('Rebuild failed; inspect local log, do not retry blindly')
    return {'output':str(out),'sha256':digest(out),'log':str(log),'native_draft_updated':False,'caption_source_used':captions,'note':'Picture re-encoded for burned captions; source audio copied. Verify actual file and playback separately.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build');b.add_argument('--workspace',type=Path,required=True);b.add_argument('--spec',type=Path,required=True);b.add_argument('--destination',type=Path,required=True)
    v=sub.add_parser('verify');v.add_argument('--bundle',type=Path,required=True)
    r=sub.add_parser('rebuild');r.add_argument('--bundle',type=Path,required=True);r.add_argument('--ffmpeg',required=True);r.add_argument('--font-file',required=True);r.add_argument('--captions-ass',type=Path);r.add_argument('--output',type=Path,required=True);r.add_argument('--encoder',choices=['libx264','h264_videotoolbox'],default='libx264')
    a=p.parse_args()
    if a.command=='build':result=build(json.loads(within(a.spec,a.workspace.resolve()).read_text(encoding='utf-8-sig')),a.workspace,a.destination)
    elif a.command=='verify':result=verify(a.bundle)
    else:result=rebuild(a.bundle,a.ffmpeg,a.font_file,a.output,a.encoder,a.captions_ass)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get('errors'):raise SystemExit(2)

# Keep Unicode paths/JSON readable in Windows terminals and redirected output.
for _stream in (sys.stdout, sys.stderr):
    if __name__ == "__main__" and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError) as e:print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr);raise SystemExit(2)
