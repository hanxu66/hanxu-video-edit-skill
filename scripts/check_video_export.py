#!/usr/bin/env python3
"""Inspect actual export, optionally repair only a MOV container, then fully decode.

Target parameters come from --spec, not app UI or a file extension. No re-encoding,
app control, source overwrite or deletion. FFmpeg/FFprobe must already be available.
"""
import argparse
import json
import re
import subprocess
import time
from fractions import Fraction
from pathlib import Path

from check_subtitle_render import binary, digest

MP4_BRANDS={'isom','iso2','iso3','iso4','iso5','iso6','mp41','mp42','avc1'}


def inspect(path,spec,ffprobe):
    d=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_format','-show_streams',
        '-show_packets','-show_entries','packet=stream_index,pts:stream:format','-of','json',str(path)],timeout=120))
    video=next(s for s in d['streams'] if s['codec_type']=='video')
    audio=next((s for s in d['streams'] if s['codec_type']=='audio'),None)
    errors=[]
    for field,target in [('width',spec['width']),('height',spec['height']),
                         ('codec_name',spec['video_codec']),('color_space',spec['color_space'])]:
        if video.get(field)!=target:errors.append('video_'+field)
    if spec['audio_codec'] is None:
        if audio is not None:errors.append('unexpected_audio_stream')
    elif audio is None or audio.get('codec_name')!=spec['audio_codec'] or int(audio.get('sample_rate',0))!=spec['sample_rate']:
        errors.append('audio_parameters')
    if Fraction(video['avg_frame_rate'])!=Fraction(str(spec['fps'])):errors.append('average_fps')
    pts=sorted(p['pts'] for p in d['packets'] if p['stream_index']==video['index'])
    step=1/Fraction(str(spec['fps']))/Fraction(video['time_base'])
    if not pts or pts[0]!=0 or any(abs((b-a)-step)>Fraction(1,100) for a,b in zip(pts,pts[1:])):
        errors.append('nonconstant_or_missing_frame_timestamps')
    if spec.get('expected_frames') is not None and len(pts)!=spec['expected_frames']:errors.append('frame_count')
    brand=d['format'].get('tags',{}).get('major_brand','').strip()
    if brand not in MP4_BRANDS:errors.append('container')
    return {'errors':errors,'containerBrand':brand,'video':video,'audio':audio,'format':d['format'],
            'packetCount':len(pts),'target':spec}


def stream_hash(path,ffmpeg):
    return subprocess.check_output([ffmpeg,'-v','error','-i',str(path),'-map','0:v:0','-map','0:a:0?',
        '-c','copy','-f','streamhash','-hash','sha256','-'],timeout=180).decode()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ['video','spec','output-dir']:p.add_argument('--'+arg,required=True)
    p.add_argument('--ffmpeg');p.add_argument('--ffprobe');p.add_argument('--remux-to')
    a=p.parse_args();out=Path(a.output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();report={'status':'check_failed','startedInput':str(Path(a.video).resolve())}
    try:
        ffmpeg,ffprobe=binary('ffmpeg',a.ffmpeg),binary('ffprobe',a.ffprobe)
        spec=json.loads(Path(a.spec).read_text(encoding="utf-8-sig"));src=Path(a.video).resolve();report['inputSha256']=digest(src)
        original=inspect(src,spec,ffprobe);report['originalInspection']=original;final=src
        if a.remux_to:
            if original['errors']!=['container'] or original['containerBrand']!='qt':
                raise ValueError('Only an otherwise-correct native MOV can be container-repaired')
            final=Path(a.remux_to).resolve()
            if final.exists() or final==src:raise ValueError('Repair output must be new; native original retained')
            subprocess.run([ffmpeg,'-v','error','-i',str(src),'-map','0:v:0','-map','0:a:0?',
                '-c','copy','-movflags','+faststart','-f','mp4',str(final)],check=True,timeout=300)
            before,after=stream_hash(src,ffmpeg),stream_hash(final,ffmpeg)
            report['streamHashes']={'native':before,'final':after,'identical':before==after}
            if before!=after:raise ValueError('Stream identity failed; preserve both files for review')
        result=inspect(final,spec,ffprobe);report['finalInspection']=result
        if result['errors']:raise ValueError('Export parameter/timestamp failure: '+','.join(result['errors']))
        log=out/'decode.log'
        with log.open('w') as stream:
            subprocess.run([ffmpeg,'-hide_banner','-nostats','-xerror','-v','info','-i',str(final),
                '-map','0:v:0','-map','0:a:0?','-vf','blackdetect=d=0.02:pix_th=0.02:pic_th=0.98',
                '-f','null','-'],stdout=stream,stderr=stream,check=True,timeout=1800)
        black=[{'start':float(s),'end':float(e),'duration':float(d)} for s,e,d in
               re.findall(r'black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)',log.read_text(encoding="utf-8-sig"))]
        report.update(status='black_candidates_need_review' if black else 'file_checks_complete',
                      finalFile=str(final),finalSha256=digest(final),fullDecode='exit0',blackCandidates=black)
        report['limits']='File checks only. Black candidates require visual judgment; full normal playback, hearing and content/packaging checks remain separate.'
    except (ValueError,OSError,KeyError,TypeError,StopIteration,subprocess.SubprocessError) as error:
        report['error']=str(error)
    report['seconds']=time.monotonic()-start
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
    print(json.dumps({k:report[k] for k in ['status','seconds','error','blackCandidates'] if k in report}))
    return 0 if report['status']=='file_checks_complete' else (1 if report['status']=='black_candidates_need_review' else 2)


import sys
if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8", errors="replace")

if __name__=='__main__':raise SystemExit(main())
