#!/usr/bin/env python3
"""Calibrated static-caption batch QA. Pillow required; FFmpeg/FFprobe supplied locally.

No OCR service, app control, video edits, speech recognition or font redistribution.
Exit 0: supplied checks complete; 1: visual review required; 2: invalid/failed coverage.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

from check_subtitle_width import check as width_check


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def read_srt(path):
    cards = []
    def timestamp(s):
        if not re.fullmatch(r'\d{2,}:\d{2}:\d{2}[,.]\d{3}', s):
            raise ValueError('Invalid SRT timestamp')
        h, m, sec, ms = map(int, re.split(r'[:,.]', s))
        if m >= 60 or sec >= 60:
            raise ValueError('Invalid SRT clock')
        return ((h * 60 + m) * 60 + sec) * 1000 + ms
    for block in re.split(r'\n\s*\n', Path(path).read_text(encoding='utf-8-sig').strip()):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit():
            raise ValueError('Malformed or empty SRT card')
        ends = lines[1].split(' --> ')
        if len(ends) != 2:
            raise ValueError('Malformed SRT range')
        start, end = map(timestamp, ends)
        if end <= start or start < (cards[-1]['endMs'] if cards else 0):
            raise ValueError('Overlapping, reversed or unordered SRT cards')
        cards.append({'id': int(lines[0]), 'text': '\n'.join(lines[2:]),
                      'startMs': start, 'endMs': end})
    if not cards or len({c['id'] for c in cards}) != len(cards):
        raise ValueError('Empty plan or duplicate subtitle IDs')
    return cards


def parity(planned, native, tolerance_ms=2):
    differences = []
    if len(planned) != len(native):
        differences.append({'reason': 'card_count', 'planned': len(planned), 'native': len(native)})
    for a, b in zip(planned, native):
        if a['text'] != b['text'] or any(abs(a[k] - b[k]) > tolerance_ms for k in ('startMs', 'endMs')):
            differences.append({'id': a['id'], 'planned': a, 'native': b})
    return {'count': len(native), 'differences': differences, 'pass': not differences}


class Detector:
    def __init__(self, profile, base):
        from PIL import ImageFont
        self.profile = profile
        self.cfg = profile['render']
        font = Path(profile['font_path']).expanduser()
        self.font_path = font if font.is_absolute() else base / font
        if digest(self.font_path) != profile['font_sha256']:
            raise ValueError('Font changed; recalibrate')
        if profile.get('letter_spacing_px', 0) != 0 or profile.get('scale_x', 1) != 1:
            raise ValueError('Glyph comparison for nonzero spacing/scaling is not validated')
        self.font = ImageFont.truetype(str(self.font_path), float(profile['font_size_px']),
                                      index=int(profile.get('font_index', 0)))
        missing = self.font.getmask('\U0010ffff')
        self.missing_glyph = (missing.size, bytes(missing))
        c = self.cfg
        x, y, w, h = c['crop']
        if (not all(isinstance(v, int) for v in (x, y, w, h)) or min(x, y) < 0 or min(w, h) <= 0
                or x + w > profile['canvas_width_px'] or y + h > c['canvas_height_px']):
            raise ValueError('Invalid crop')
        lo, hi = c['scan_band_px']
        if not (0 <= lo < c['expected_center_y_px'] < hi <= h
                and 0 < c['shape_f1_min'] <= 1 and 0 < c['white_threshold'] < 255
                and 0 < c['max_channel_difference'] <= 255 and c['center_tolerance_px'] >= 0
                and c['row_min_pixels'] > 0 and c['row_join_gap'] >= 0
                and c['row_min_rows'] >= 1 and c['row_padding'] >= 0):
            raise ValueError('Invalid detector thresholds')

    def mask(self, path):
        from PIL import Image, ImageChops
        image = Image.open(path).convert('RGB')
        if image.size != tuple(self.cfg['crop'][2:]):
            raise ValueError('Frame crop dimensions do not match calibrated profile')
        red, green, blue = image.split()
        low = ImageChops.darker(ImageChops.darker(red, green), blue)
        high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        white = low.point(lambda v: 255 if v > self.cfg['white_threshold'] else 0)
        neutral = ImageChops.subtract(high, low).point(
            lambda v: 255 if v < self.cfg['max_channel_difference'] else 0)
        mask = ImageChops.multiply(white, neutral)
        band = Image.new('L', image.size)
        lo, hi = self.cfg['scan_band_px']
        band.paste(mask.crop((0, lo, image.width, hi)), (0, lo))
        return band

    def inspect(self, mask, text):
        from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat
        c = self.cfg
        def mass(im):
            return ImageStat.Stat(im).sum[0] / 255
        rows = [y for y in range(*c['scan_band_px'])
                if mass(mask.crop((0, y, mask.width, y + 1))) > c['row_min_pixels']]
        groups = []
        for y in rows:
            if not groups or y - groups[-1][-1] > c['row_join_gap']:
                groups.append([])
            groups[-1].append(y)
        groups = [g for g in groups if len(g) >= c['row_min_rows']]
        flags = []
        if len(groups) != 1:
            flags.append('line_count_or_missing')
        selected = Image.new('L', mask.size)
        for g in groups:
            lo = max(0, g[0] - c['row_padding'])
            hi = min(mask.height, g[-1] + c['row_padding'] + 1)
            selected.paste(mask.crop((0, lo, mask.width, hi)), (0, lo))
        bounds = selected.getbbox()
        if not bounds:
            return {'lines': len(groups), 'flags': flags, 'shapeF1': 0}
        x, y, _, _ = c['crop']
        if (bounds[0] + x < self.profile['safe_left_px'] or bounds[2] + x >
                self.profile['canvas_width_px'] - self.profile['safe_right_px']):
            flags.append('safe_width')
        if abs((bounds[1] + bounds[3]) / 2 - c['expected_center_y_px']) > c['center_tolerance_px']:
            flags.append('vertical_position')
        actual = selected.crop(bounds)
        box = self.font.getbbox(text)
        expected = Image.new('L', (box[2] - box[0], box[3] - box[1]))
        ImageDraw.Draw(expected).text((-box[0], -box[1]), text, font=self.font, fill=255)
        ink = expected.getbbox()
        if not ink:
            raise ValueError('No expected glyph ink')
        expected = expected.crop(ink).resize(actual.size, Image.Resampling.LANCZOS).point(
            lambda v: 255 if v > c['white_threshold'] else 0)
        recall = mass(ImageChops.multiply(actual, expected.filter(ImageFilter.MaxFilter(3)))) / max(1, mass(actual))
        precision = mass(ImageChops.multiply(expected, actual.filter(ImageFilter.MaxFilter(3)))) / max(1, mass(expected))
        score = 2 * recall * precision / max(1e-9, recall + precision)
        if score < c['shape_f1_min']:
            flags.append('glyph_shape_difference')
        for char in set(text) - set(' \t\r\n'):
            glyph = self.font.getmask(char)
            if (glyph.size, bytes(glyph)) == self.missing_glyph:
                flags.append('unvalidated_glyph')
                break
        return {'lines': len(groups), 'boundsCrop': bounds, 'boundsCanvas':
                [bounds[0]+x, bounds[1]+y, bounds[2]+x, bounds[3]+y],
                'shapeF1': score, 'flags': flags}


def validate(profile, base, samples):
    from PIL import Image, ImageChops
    detector = Detector(profile, base)
    results = []
    seen = set()
    for row in samples:
        p = Path(row['path'])
        if not p.is_absolute():
            p = base / p
        result = detector.inspect(detector.mask(p), row['text'])
        passed = (result['lines'] == row['expectedLines'] and
                  (not result['flags'] if row['expectedLines'] == 1 else
                   'line_count_or_missing' in result['flags']))
        results.append({'kind': 'native', 'path': str(p), 'sha256': digest(p),
                        'expectedLines': row['expectedLines'], 'result': result, 'pass': passed})
        seen.add(row['expectedLines'])
    if not {1, 2}.issubset(seen):
        raise ValueError('Supply actual native single-line and wrapped samples')
    seed = next(s for s in samples if s['expectedLines'] == 1 and s.get('wrongText'))
    seed_path = Path(seed['path'])
    mask = detector.mask(seed_path if seed_path.is_absolute() else base / seed_path)
    step_x = int(profile['canvas_width_px'] * .1)
    step_y = int(profile['render']['crop'][3] * .12)
    cases = [('missing', Image.new('L', mask.size), seed['text'], 'line_count_or_missing'),
             ('shift_right', ImageChops.offset(mask, step_x, 0), seed['text'], 'safe_width'),
             ('shift_vertical', ImageChops.offset(mask, 0, step_y), seed['text'], 'vertical_position'),
             ('wrong_word', mask, seed['wrongText'], 'glyph_shape_difference')]
    for name, image, text, flag in cases:
        result = detector.inspect(image, text)
        results.append({'kind': 'synthetic', 'case': name, 'expectedFlag': flag,
                        'result': result, 'pass': flag in result['flags']})
    calibration = Path(profile['calibration_ref'])
    calibration = calibration if calibration.is_absolute() else base / calibration
    return {'status': 'validated' if all(r['pass'] for r in results) else 'validation_failed',
            'profile_sha256': fingerprint(profile), 'font_sha256': digest(detector.font_path),
            'tool_sha256': digest(__file__),
            'calibration_sha256': digest(calibration),
            'results': results, 'scope': 'Native line-count examples plus labelled synthetic controls; not general OCR/ASR validation'}


def binary(name, supplied):
    value = supplied or shutil.which(name)
    if not value:
        raise ValueError(f'{name} required; supply executable path')
    return str(value)


def extract(video, rows, detector, ffmpeg, destination):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Extraction artifacts already exist; preserve them and use a new output directory')
    destination.mkdir(parents=True, exist_ok=True)
    frames = sorted({r['frame'] for r in rows})
    crop = ':'.join(map(str, [*detector.cfg['crop'][2:], *detector.cfg['crop'][:2]]))
    # CFR verified below; n is the actual decoded frame index, not an approximate seek.
    expression = '+'.join(f'eq(n,{f})' for f in frames)
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(video), '-vf',
                    f"select='{expression}',crop={crop}", '-fps_mode', 'passthrough', '-q:v', '2',
                    str(destination / 'frame-%06d.jpg')], check=True, timeout=1800)
    files = list(sorted(destination.glob('frame-*.jpg')))
    if len(files) != len(frames):
        raise ValueError('Incomplete frame extraction; coverage cannot pass')
    mapping = dict(zip(frames, files))
    return [{**r, 'path': str(mapping[r['frame']]), 'frame_sha256': digest(mapping[r['frame']])} for r in rows]


def run(args, profile, base):
    from PIL import Image, ImageDraw
    begin = time.monotonic()
    detector = Detector(profile, base)
    validation = json.loads(Path(args.validation).read_text(encoding="utf-8-sig"))
    calibration = Path(profile['calibration_ref'])
    calibration = calibration if calibration.is_absolute() else base / calibration
    if (validation['status'] != 'validated' or validation['profile_sha256'] != fingerprint(profile)
            or validation['tool_sha256'] != digest(__file__)
            or not validation['results'] or not all(r['pass'] for r in validation['results'])
            or validation['calibration_sha256'] != digest(calibration)):
        raise ValueError('Detector validation missing or stale for this exact profile')
    for row in validation['results']:
        if row['kind'] == 'native' and digest(row['path']) != row['sha256']:
            raise ValueError('Native calibration evidence changed')
    cards = read_srt(args.srt)
    text_check = parity(cards, read_srt(args.native_srt))
    preflight = width_check(cards, profile, base)
    if not text_check['pass'] or preflight['status'] != 'width_preflight_pass':
        raise ValueError('Native SRT parity or calibrated width preflight failed')
    ffmpeg, ffprobe = binary('ffmpeg', args.ffmpeg), binary('ffprobe', args.ffprobe)
    video = Path(args.video).resolve()
    metadata = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-select_streams', 'v:0',
        '-show_streams', '-show_packets', '-show_entries',
        'stream=width,height,avg_frame_rate,time_base,duration,nb_frames:packet=pts', '-of', 'json', str(video)], timeout=120))
    stream = metadata['streams'][0]
    from fractions import Fraction
    fps = Fraction(stream['avg_frame_rate'])
    pts = sorted(p['pts'] for p in metadata['packets'])
    step = Fraction(1, 1) / fps / Fraction(stream['time_base'])
    if (not pts or pts[0] != 0 or any(abs((b-a)-step) > Fraction(1,100) for a,b in zip(pts,pts[1:]))
            or stream['width'] != profile['canvas_width_px'] or stream['height'] != detector.cfg['canvas_height_px']):
        raise ValueError('Video is not matching zero-origin CFR canvas; inspect export first')
    for row in cards:
        start, end = round(row['startMs'] * fps / 1000), round(row['endMs'] * fps / 1000)
        if end <= start or end > len(pts):
            raise ValueError('Subtitle range outside actual final frame count')
        row['frame'] = (start + end - 1) // 2
    signature = {'video_sha256': digest(video), 'srt_sha256': digest(args.srt),
                 'native_srt_sha256': digest(args.native_srt), 'profile_sha256': fingerprint(profile),
                 'validation_sha256': digest(args.validation), 'tool_sha256': digest(__file__)}
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = out / 'frames.json'
    if args.reuse_frames and cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8-sig"))
        if saved['signature'] != signature:
            raise ValueError('Cached frames belong to different inputs/tool; use a new output directory')
        rows = saved['rows']
        if len(rows) != len(cards) or any(digest(r['path']) != r['frame_sha256'] for r in rows):
            raise ValueError('Cached frame coverage changed')
        if [{k:r[k] for k in ('id','text','startMs','endMs','frame')} for r in rows] != cards:
            raise ValueError('Cached frame descriptors differ from authoritative SRT')
    else:
        if (out / 'frames').exists():
            raise ValueError('Existing frame directory; use --reuse-frames or a new directory')
        # Probe representative short/long/mixed/first/last cards before all-card classification.
        indices = sorted({0,len(cards)-1,max(range(len(cards)),key=lambda i:len(cards[i]['text'])),
                          min(range(len(cards)),key=lambda i:len(cards[i]['text'])),
                          next((i for i,c in enumerate(cards) if re.search('[A-Za-z]',c['text'])),0)})
        probes = extract(video, [cards[i] for i in indices], detector, ffmpeg, out/'probe')
        probe_results = [detector.inspect(detector.mask(r['path']),r['text']) for r in probes]
        geometry_failures = sum(any(f != 'glyph_shape_difference' for f in r['flags']) for r in probe_results)
        if geometry_failures > len(probes) * .4:
            raise ValueError('Detector probe geometry failed; do not turn this into per-card manual work')
        rows = extract(video, cards, detector, ffmpeg, out/'frames')
        cache.write_text(json.dumps({'signature': signature, 'rows': rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    results = [{**r, **detector.inspect(detector.mask(r['path']), r['text'])} for r in rows]
    suspects = [r for r in results if r['flags']]
    reviews = {}
    if args.review:
        review = json.loads(Path(args.review).read_text(encoding="utf-8-sig"))
        if review['signature'] != signature:
            raise ValueError('Review not bound to this scan')
        for row in review['reviews']:
            if row['id'] in reviews:
                raise ValueError('Duplicate review ID')
            reviews[row['id']] = row
    unresolved = []
    for row in suspects:
        review = reviews.get(row['id'], {})
        if not (review.get('disposition') == 'false_positive' and review.get('note')
                and review.get('frame_sha256') == row['frame_sha256']
                and Path(review.get('evidence', '')).is_file()):
            unresolved.append(row['id'])
    for page in range(math.ceil(len(suspects)/12)):
        batch = suspects[page*12:page*12+12]
        cell_w = min(540, detector.cfg['crop'][2])
        cell_h = round(detector.cfg['crop'][3]*cell_w/detector.cfg['crop'][2])
        sheet = Image.new('RGB', (cell_w*3, (cell_h+28)*4), '#eeeeee')
        draw = ImageDraw.Draw(sheet)
        for i,row in enumerate(batch):
            x,y=(i%3)*cell_w,(i//3)*(cell_h+28)
            sheet.paste(Image.open(row['path']).resize((cell_w,cell_h)),(x,y+28))
            draw.text((x+4,y+4),f"ID {row['id']} score {row['shapeF1']:.4f}",fill='black')
        sheet.save(out/f'suspects-{page+1:02d}.jpg',quality=93)
    report = {'status': 'needs_review' if unresolved else 'render_checks_complete',
              'signature': signature, 'count': len(cards), 'coverage': len(results),
              'suspectCount': len(suspects), 'unresolvedIds': unresolved, 'srtParity': text_check,
              'seconds': time.monotonic()-begin, 'results': results,
              'limits': 'Static stable frames and supplied text only; semantic judgment, speech synchronization/hearing, identity and animation composites and full playback remain separate'}
    write_report(out, report)
    print(json.dumps({k:report[k] for k in ('status','count','coverage','suspectCount','unresolvedIds','seconds')}))
    return 1 if unresolved else 0


def write_report(out, report):
    out.mkdir(parents=True, exist_ok=True)
    data=json.dumps(report,ensure_ascii=False,indent=2)
    (out/f'report-{time.time_ns()}.json').write_text(data, encoding="utf-8")
    (out/'report.json').write_text(data, encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    v=sub.add_parser('validate');v.add_argument('--profile',required=True);v.add_argument('--samples',required=True);v.add_argument('--output',required=True)
    scan=sub.add_parser('scan')
    for key in ('video','srt','native-srt','profile','validation','output-dir'):
        scan.add_argument('--'+key,required=True)
    scan.add_argument('--ffmpeg');scan.add_argument('--ffprobe');scan.add_argument('--reuse-frames',action='store_true');scan.add_argument('--review')
    args=parser.parse_args()
    try:
        path=Path(args.profile).resolve();profile=json.loads(path.read_text(encoding="utf-8-sig"))
        if args.command=='validate':
            result=validate(profile,path.parent,json.loads(Path(args.samples).read_text(encoding="utf-8-sig")))
            Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2), encoding="utf-8")
            print(json.dumps({'status':result['status'],'cases':len(result['results'])}))
            return 0 if result['status']=='validated' else 2
        return run(args,profile,path.parent)
    except (ValueError,KeyError,TypeError,OSError,ImportError,StopIteration,subprocess.SubprocessError) as error:
        failure={'status':'check_failed','coverageAccepted':False,'error':str(error)}
        if args.command=='scan':write_report(Path(args.output_dir),failure)
        print(json.dumps(failure))
        return 2


import sys
if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8", errors="replace")

if __name__=='__main__':
    raise SystemExit(main())
