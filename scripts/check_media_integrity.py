#!/usr/bin/env python3
"""Check decoded media and required audio; never edits or repairs the source.

Byte-copy identity, decodability and audible content are separate conclusions.
This detects all-zero decoded audio, not intelligibility or speech quality.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
from check_subtitle_render import binary, digest


def inspect_media(media, ffmpeg, ffprobe, audio_policy='required', timeout=1800):
    media=Path(media).resolve(strict=True)
    if audio_policy not in ('required','optional','silent-preview','intentional-silent'):
        raise ValueError('audio-policy allowed: required, optional, silent-preview, intentional-silent')
    result={'media':str(media),'media_sha256':digest(media),'audio_policy':audio_policy,
            'status':'failed','issues':[],'hearing':'unverified','semantic_review':'unverified',
            'scope':'Full decode, duration consistency and zero-signal audio only; no quality or human approval claim'}
    meta=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(media)],timeout=30))
    streams=meta.get('streams',[])
    if not any(s.get('codec_type')=='video' for s in streams):result['issues'].append('video_stream_missing')
    declared=float(meta.get('format',{}).get('duration',0))
    if not math.isfinite(declared) or declared<=0:raise ValueError('Missing finite positive declared duration')
    decoded=subprocess.run([ffmpeg,'-v','error','-xerror','-nostats','-progress','pipe:1','-i',str(media),'-f','null','-'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    values=[int(line.split('=',1)[1])/1e6 for line in decoded.stdout.splitlines() if line.startswith('out_time_us=') and line.split('=',1)[1].lstrip('-').isdigit()]
    actual=max(values,default=0)
    result['decode']={'returncode':decoded.returncode,'declared_seconds':declared,'decoded_seconds':actual,'error':decoded.stderr[-3000:]}
    if decoded.returncode or decoded.stderr.strip():result['issues'].append('decode_error')
    if actual<=0 or abs(actual-declared)>max(1.0,declared*.01):result['issues'].append('duration_inconsistent')
    audio=any(s.get('codec_type')=='audio' for s in streams)
    result['audio']={'stream_present':audio,'signal':'not_checked'}
    if audio:
        # astats is a streaming analysis: no raw audio or private recording is stored.
        measured=subprocess.run([ffmpeg,'-hide_banner','-nostats','-i',str(media),'-map','0:a:0','-vn','-af','astats=metadata=0:reset=0','-f','null','-'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
        peaks=[line.rsplit(':',1)[1].strip() for line in measured.stderr.splitlines() if 'Peak level dB:' in line]
        if measured.returncode or not peaks:result['issues'].append('audio_analysis_failed')
        else:
            silent=all(x.lower()=='-inf' for x in peaks)
            result['audio'].update(signal='all_zero' if silent else 'nonzero',peak_db=peaks[-1])
            if silent and audio_policy=='required':result['issues'].append('required_audio_is_silent')
    elif audio_policy=='required':result['issues'].append('required_audio_missing')
    if digest(media)!=result['media_sha256']:result['issues'].append('input_changed_during_check')
    if not result['issues']:result['status']='passed'
    result['delivery_label']='静音预览｜尚未配音，节奏未锁定' if audio_policy=='silent-preview' else '按本次约定审看；听辨与内容检查另列'
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--media',required=True);p.add_argument('--report',required=True)
    p.add_argument('--ffmpeg');p.add_argument('--ffprobe')
    p.add_argument('--audio-policy',choices=('required','optional','silent-preview','intentional-silent'),default='required')
    a=p.parse_args();out=Path(a.report).resolve();media=Path(a.media).resolve()
    if out==media:raise ValueError('Report cannot overwrite input media')
    result={'status':'failed','issues':['inspection_failed']}
    try:result=inspect_media(media,binary('ffmpeg',a.ffmpeg),binary('ffprobe',a.ffprobe),a.audio_policy)
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as error:result['error']=str(error)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='passed' else 2


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    raise SystemExit(main())
