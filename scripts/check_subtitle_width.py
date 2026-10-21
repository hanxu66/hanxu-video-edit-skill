#!/usr/bin/env python3
"""Preflight semantic subtitle cards against calibrated final-render font metrics.

This reads a JSON card plan and writes a geometry report. It does not edit video,
split language mechanically, or certify semantic quality or final rendered pixels.
Requires Pillow; font files and calibration evidence are supplied by the user.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def check(plan: list[dict], profile: dict, profile_dir: Path) -> dict:
    from PIL import ImageFont
    if not isinstance(plan,list) or not plan:raise ValueError('Plan must contain at least one subtitle card')

    def resolve(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else profile_dir / p

    font_path = resolve(profile['font_path'])
    font_hash = hashlib.sha256(font_path.read_bytes()).hexdigest()
    if profile.get('font_sha256') and profile['font_sha256'] != font_hash:
        raise ValueError('Font changed; recalibrate the final-render profile')
    size = float(profile['font_size_px'])
    scale = float(profile.get('scale_x', 1))
    spacing = float(profile.get('letter_spacing_px', 0))
    width = float(profile['canvas_width_px'])
    left = float(profile['safe_left_px'])
    right = width - float(profile['safe_right_px'])
    anchor = float(profile.get('anchor_x_px', width / 2))
    padding = float(profile.get('padding_each_side_px', 0))
    if not (all(math.isfinite(x) for x in (size, scale, spacing, width, left, right, anchor, padding))
            and size > 0 and scale > 0 and spacing >= 0 and padding >= 0
            and 0 <= left < anchor < right <= width):
        raise ValueError('Invalid dimensions or negative spacing')
    budget = 2 * min(anchor - left, right - anchor)
    font = ImageFont.truetype(str(font_path), size, index=int(profile.get('font_index', 0)))

    def measure(text: str) -> float:
        if not text:
            return 0
        box = font.getbbox(text)
        # Preserve side bearings/overhangs and reserve effects and calibration error.
        base = max(float(font.getlength(text)), box[2]) - min(0, box[0])
        return base * scale + max(0, len(text) - 1) * spacing + 2 * padding

    evidence = profile.get('calibration_ref')
    calibrated = bool(profile.get('calibrated') and evidence
                      and resolve(evidence).is_file() and profile.get('font_sha256'))
    rows = []
    cursor = 0
    boundaries = []
    for i, card in enumerate(plan):
        text = card['text']
        if not isinstance(text, str):
            raise ValueError('Every card text must be a string')
        reasons = []
        if not text.strip():
            reasons.append('empty_text')
        if '\n' in text or '\r' in text:
            reasons.append('multiline_text')
        if '\ufffd' in text or any(ord(c) < 32 for c in text):
            reasons.append('invalid_or_control_character')
        pixels = measure(text.replace('\n', '').replace('\r', ''))
        if pixels > budget + 1e-6:
            reasons.append('over_width')
        if i:
            boundaries.append((cursor, i - 1, i))
        cursor += len(text)
        rows.append({'id': card.get('id', i + 1), 'text': text,
                     'width_px': round(pixels, 3), 'budget_px': budget,
                     'reasons': reasons})

    joined = ''.join(c['text'] for c in plan)
    for term in profile.get('protected_terms', []):
        if not isinstance(term, str) or not term:
            raise ValueError('Protected terms must be nonempty strings')
        start = joined.find(term)
        while start >= 0:
            for boundary, before, after in boundaries:
                if start < boundary < start + len(term):
                    for index in (before, after):
                        reason = 'split_protected_term:' + term
                        if reason not in rows[index]['reasons']:
                            rows[index]['reasons'].append(reason)
            start = joined.find(term, start + 1)

    cjk_step = max(float(font.getlength(c)) for c in '\u6c49\u56fd\u4f60\u7684\u65f6\u95f4') * scale + spacing
    cjk_reference_limit = max(0, math.floor((budget - 2 * padding + spacing) / cjk_step))
    failures = [r for r in rows if r['reasons']]
    profile_hash = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    calibration_hash = hashlib.sha256(resolve(evidence).read_bytes()).hexdigest() if evidence and resolve(evidence).is_file() else None
    return {'status': 'needs_calibration' if not calibrated else ('revise' if failures else 'width_preflight_pass'),
            'profile_sha256': profile_hash, 'calibration_sha256': calibration_hash,
            'calibrated': calibrated, 'font_sha256': font_hash,
            'font_family': list(font.getname()), 'canvas_width_px': width,
            'budget_px': budget, 'reference_cjk_limit': cjk_reference_limit,
            'count': len(rows), 'failure_count': len(failures), 'cards': rows,
            'scope': 'font-width and supplied protected-term boundary preflight only',
            'semantic_judgment': 'not_assessed', 'glyph_coverage': 'not_assessed',
            'final_native_render': 'not_assessed'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, help='JSON array of {id,text,...} cards')
    parser.add_argument('--profile', required=True, help='Final-render font and safe-area JSON')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        profile_path = Path(args.profile).resolve()
        report = check(json.loads(Path(args.plan).read_text(encoding='utf-8-sig')),
                       json.loads(profile_path.read_text(encoding='utf-8-sig')), profile_path.parent)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
        parser.exit(2, f'Subtitle width preflight failed: {error}\n')
    print(json.dumps({k: report[k] for k in ('status', 'count', 'failure_count', 'budget_px', 'reference_cjk_limit')}))
    return 2 if not report['calibrated'] else int(report['failure_count'] > 0)


import sys
if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8", errors="replace")

if __name__ == '__main__':
    raise SystemExit(main())
