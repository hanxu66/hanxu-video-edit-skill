#!/usr/bin/env python3
"""Read-only caption/file checks. Reports risks; never edits source or guesses speech."""
import argparse
import sys
import json
from pathlib import Path
import re
import subprocess


def audit(cards, timing=None, measure=None, max_width=960):
    issues=[];previous_end=0
    for i,c in enumerate(cards):
        start,end=c['start_us'],c['end_us'];text=c['text']
        if end<=start:issues.append(dict(card=i+1,kind='invalid_interval'))
        if i and start<previous_end:issues.append(dict(card=i+1,kind='overlap'))
        if len(text)>=3 and end-start<300000:issues.append(dict(card=i+1,kind='flash',text=text,seconds=(end-start)/1e6))
        if '\n' in text:issues.append(dict(card=i+1,kind='multiline',text=text))
        if measure and measure(text)>max_width:issues.append(dict(card=i+1,kind='width_overflow',width=measure(text)))
        previous_end=end
    if timing:
        full=''.join(c['text'] for c in cards).lower();group=''
        for ch,point in list(zip(timing['text'],timing['map']))+[('',dict(source_start_us=0,source_end_us=10000))]:
            if point['source_end_us']-point['source_start_us']<=2000:group+=ch
            else:
                if len(group)>=3 and group.lower() in full:issues.append(dict(kind='collapsed_source_words',text=group,action='retranscribe_local_context_before_editing'))
                group=''
    return dict(cards=len(cards),issues=issues,objective_status='failed' if issues else 'passed',glyph_width_check='checked' if measure else 'unverified',semantic_review='unverified',hearing='unverified')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,required=True);p.add_argument('--cards',type=Path,required=True)
    p.add_argument('--word-timings',type=Path);p.add_argument('--font',type=Path);p.add_argument('--font-px',type=float,default=80);p.add_argument('--max-width',type=float,default=960)
    p.add_argument('--media',type=Path);p.add_argument('--ffprobe',type=Path);p.add_argument('--report',type=Path,required=True)
    a=p.parse_args();root=a.workspace.resolve(strict=True);out=a.report.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Choose a separate editing workspace, outside the installed skill')
    if not out.is_relative_to(root):raise ValueError('Report must stay inside the selected workspace')
    raw=json.loads(a.cards.read_text(encoding='utf-8-sig'));cards=raw if isinstance(raw,list) else raw['cards']
    timing=json.loads(a.word_timings.read_text(encoding='utf-8-sig')) if a.word_timings else None
    measure=None
    if a.font:
        from PIL import ImageFont
        font=ImageFont.truetype(str(a.font),round(a.font_px));measure=font.getlength
    result=audit(cards,timing,measure,a.max_width)
    if a.media:
        if not a.ffprobe:raise ValueError('Provide the actual ffprobe executable for file checks')
        data=json.loads(subprocess.check_output([str(a.ffprobe),'-v','error','-show_format','-show_streams','-of','json',str(a.media)]))
        brand=data.get('format',{}).get('tags',{}).get('major_brand','')
        result['file']={'major_brand':brand,'duration':data['format'].get('duration'),'streams':[{'type':s['codec_type'],'codec':s.get('codec_name'),'width':s.get('width'),'height':s.get('height'),'frame_rate':s.get('avg_frame_rate'),'sample_rate':s.get('sample_rate')} for s in data['streams']]}
        if brand.strip()=='qt':result['issues'].append(dict(kind='mov_container_instead_of_mp4',action='preserve_native_and_stream_copy_then_verify'))
    result['objective_status']='failed' if result['issues'] else 'passed'
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n', encoding='utf-8');print(json.dumps(result,ensure_ascii=False))


# Keep Unicode paths/JSON readable in Windows terminals and redirected output.
for _stream in (sys.stdout, sys.stderr):
    if __name__ == "__main__" and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

if __name__=='__main__':main()
