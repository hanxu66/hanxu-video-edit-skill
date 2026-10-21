#!/usr/bin/env python3
import sys
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
CACHE=Path(os.environ.get('TEST_WORKSPACE',str(ROOT.parent/'test-work')));CACHE.mkdir(parents=True,exist_ok=True)
if not os.environ.get('SUBTITLE_TEST_FONT'):
    fonts=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/arial.ttf',Path('/System/Library/Fonts/Supplemental/Arial.ttf')]
    os.environ['SUBTITLE_TEST_FONT']=str(next(p for p in fonts if p.is_file()))
import shutil
for key,name in [('FFMPEG_TEST_BIN','ffmpeg'),('FFPROBE_TEST_BIN','ffprobe')]:
    if key not in os.environ and shutil.which(name):os.environ[key]=shutil.which(name)

"""Exercise real font metrics and subtitle boundary failure cases."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from PIL import ImageFont
from check_subtitle_width import check


class SubtitleWidthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=CACHE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.font = Path(os.environ['SUBTITLE_TEST_FONT']).resolve()
        (self.root / 'calibration.json').write_text('{"fixture":true}', encoding="utf-8")
        self.profile = {'font_path': str(self.font), 'font_sha256': hashlib.sha256(self.font.read_bytes()).hexdigest(),
                        'font_size_px': 80, 'canvas_width_px': 1080,
                        'safe_left_px': 90, 'safe_right_px': 90,
                        'calibrated': True, 'calibration_ref': 'calibration.json'}

    def run_plan(self, *texts, **profile):
        return check([{'id': i + 1, 'text': t} for i, t in enumerate(texts)],
                     {**self.profile, **profile}, self.root)

    def test_exact_width_and_one_pixel_over(self):
        width = self.run_plan('WWWW')['cards'][0]['width_px']
        edge = (1080 - width) / 2
        self.assertEqual(self.run_plan('WWWW', safe_left_px=edge, safe_right_px=edge)['status'], 'width_preflight_pass')
        self.assertEqual(self.run_plan('WWWW', safe_left_px=edge + 0.5, safe_right_px=edge + 0.5)['failure_count'], 1)

    def test_equal_character_counts_have_different_widths(self):
        self.assertGreater(self.run_plan('WWWW')['cards'][0]['width_px'],
                           self.run_plan('iiii')['cards'][0]['width_px'])

    def test_early_semantic_split_and_protected_word(self):
        good = self.run_plan('\u6211\u4eec\u7684\u5546\u4e1a\u673a\u4f1a', '\u5728\u54ea\u91cc', protected_terms=['\u5546\u4e1a\u673a\u4f1a'])
        self.assertEqual(good['failure_count'], 0)
        bad = self.run_plan('\u6211\u4eec\u7684\u5546\u4e1a', '\u673a\u4f1a\u5728\u54ea\u91cc', protected_terms=['\u5546\u4e1a\u673a\u4f1a'])
        self.assertEqual(bad['failure_count'], 2)
        self.assertIn('split_protected_term:', bad['cards'][0]['reasons'][0])

    def test_font_and_calibration_must_match(self):
        self.assertEqual(self.run_plan('test', calibrated=False)['status'], 'needs_calibration')
        self.assertEqual(self.run_plan('test', calibration_ref='missing.json')['status'], 'needs_calibration')
        with self.assertRaises(ValueError):
            self.run_plan('test', font_sha256='changed')

    def test_multiline_and_replacement_characters_fail(self):
        result = self.run_plan('hello\nworld', 'bad\ufffd')
        self.assertEqual(result['failure_count'], 2)
        self.assertIn('multiline_text', result['cards'][0]['reasons'])

    def test_asymmetric_safe_area_uses_centered_budget(self):
        self.assertEqual(self.run_plan('test', safe_right_px=180)['budget_px'], 720)

    def test_negative_spacing_is_rejected_and_effects_consume_width(self):
        with self.assertRaises(ValueError):
            self.run_plan('test', letter_spacing_px=-1)
        plain = self.run_plan('test')['cards'][0]['width_px']
        padded = self.run_plan('test', padding_each_side_px=8)['cards'][0]['width_px']
        self.assertEqual(padded - plain, 16)


if __name__ == '__main__':
    unittest.main()
