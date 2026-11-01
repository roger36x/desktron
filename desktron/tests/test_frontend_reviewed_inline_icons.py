"""Approved inline V2 and A/C iris demo: exact assets, not old-path snapshots."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'
NS = {'s': 'http://www.w3.org/2000/svg'}
MANIFEST = json.loads((FRONT / 'vendor/original-inline-icon-manifest.json').read_text(encoding='utf-8'))


def icon(symbol):
    row = next(x for x in MANIFEST['icons'] if x['symbol'] == symbol)
    text = (ROOT / row['file']).read_text(encoding='utf-8')
    obj, key = symbol.split('.')
    block = text.split('const ' + obj + ' = {', 1)[1].split('\n};', 1)[0]
    match = re.search(r'^  ' + re.escape(key) + r': (".*"),$', block, re.M)
    assert match, symbol
    return json.loads(match.group(1))


def test_all_approved_symbols_are_digest_bound_and_parseable():
    rows = MANIFEST['icons']
    assert len(rows) == 41
    assert len({x['symbol'] for x in rows}) == 41
    assert not any(x['symbol'].startswith('LKE_ICONS.') for x in rows)
    for row in rows:
        svg = icon(row['symbol'])
        assert hashlib.sha256(svg.encode('utf-8')).hexdigest() == row['svg_sha256']
        root = ET.fromstring(svg)
        assert root.attrib['viewBox'] == '0 0 32 32'
        assert root.attrib['data-desk-icon'] == row['symbol']
        assert len(root.findall('s:g', NS)) == 1
        assert not root.findall('.//s:script', NS)
        assert not root.findall('.//s:image', NS)


def test_license_counts_match_active_manifest_after_timeline_retirement():
    ordinary = [row for row in MANIFEST['icons'] if row['symbol'].startswith('ICONS.')]
    engines = [row for row in MANIFEST['icons'] if row['symbol'].startswith('BRAND_ICONS.')]
    assert len(ordinary) == 39 and len(engines) == 2
    notice = (FRONT / 'vendor/ICONS-LICENSE.txt').read_text(encoding='utf-8')
    assert f'{len(ordinary)}枚普通符号及{len(engines)}枚引擎识别符已接入' in notice
    assert '此前46项清单保持不变' not in notice
    assert '44枚普通符号' not in notice


def test_laptop_bottom_line_has_same_visible_width_as_screen():
    root = ET.fromstring(icon('ICONS.laptop'))
    rect = root.find('.//s:rect', NS)
    path = root.find('.//s:path', NS)
    assert (rect.attrib['x'], rect.attrib['width']) == ('5.5', '21')
    assert path.attrib['d'] == 'M5.5 26.5h21'
    paint = root.find('s:g', NS)
    assert paint.attrib['stroke-width'] == '2.1'
    assert paint.attrib['stroke-linecap'] == 'round'


def test_iris_is_24_short_same_direction_slanted_segments():
    for engine in ('claude', 'codex'):
        root = ET.fromstring(icon('BRAND_ICONS.' + engine))
        paint = root.find('s:g', NS)
        ring, letter = list(paint)
        assert ring.attrib['stroke-width'] == '0.7'
        assert ring.attrib['stroke-linecap'] == 'round'
        assert len(ring) == 24
        assert letter.attrib['stroke-width'] == '2.8'
        for segment in ring:
            nums = [float(v) for v in re.findall(r'-?\d+(?:\.\d+)?', segment.attrib['d'])]
            x, y, dx, dy = nums
            cx, cy = x + dx / 2 - 16, y + dy / 2 - 16
            assert math.isclose(math.hypot(dx, dy), 1.8, abs_tol=0.0002)
            assert math.isclose(math.hypot(cx, cy), 13.35, abs_tol=0.0002)
            assert cx * dy - cy * dx > 0
        assert 'brand-' + engine in root.attrib['class'].split()
        assert root.attrib['role'] == 'img'


def test_paint_group_prevents_legacy_root_stroke_overrides():
    """Context CSS may set svg stroke-width; reviewed child paint stays explicit."""
    for row in MANIFEST['icons']:
        root = ET.fromstring(icon(row['symbol']))
        group = root.find('s:g', NS)
        if row['symbol'].startswith('BRAND_ICONS.'):
            assert list(group)[0].attrib['stroke-width'] == '0.7'
        else:
            if row['symbol'] == 'ICONS.pencil':
                assert group.attrib['stroke-width'] == '2.2'
                assert '12.7-14.5' in icon(row['symbol'])
            else:
                assert group.attrib['stroke-width'] in ('2.1', '2.25')
            assert group.attrib['stroke-linecap'] == 'round'


def test_existing_pi_shepherd_and_legacy_zap_remain_compatible():
    events = (FRONT / 'app-events.js').read_text(encoding='utf-8')
    events = events.split('const BRAND_ICONS = {', 1)[1].split('\n};', 1)[0]
    for symbol, expected in MANIFEST['preserved_brand_expression_sha256'].items():
        key = symbol.split('.')[1]
        expression = re.search(r'^  ' + key + r': (.*),$', events, re.M).group(1)
        assert hashlib.sha256(expression.encode('utf-8')).hexdigest() == expected
    core = (FRONT / 'app-core.js').read_text(encoding='utf-8')
    assert 'zap: _svg(' in core
    assert '13 2 3 14 12 14 11 22 21 10 12 10 13 2' in core
    assert 'ICONS[d.icon]' in (FRONT / 'app-panel-sidebar.js').read_text(encoding='utf-8')
    sources = json.loads((FRONT / 'vendor/inline-icon-sources.json').read_text(encoding='utf-8'))
    assert [x['symbol'] for x in sources['matched']] == ['ICONS.zap']
    brands = json.loads((FRONT / 'vendor/brand-icon-sources.json').read_text(encoding='utf-8'))
    assert brands['matched'] == []


def test_engine_colors_follow_approved_demo_in_both_themes():
    css = (FRONT / 'style.css').read_text(encoding='utf-8')
    for rule in ('.brand-claude { color: #db9b7e; }', '.brand-codex { color: #70c2a0; }',
                 'html[data-theme="light"] .brand-claude { color: #b9765c; }',
                 'html[data-theme="light"] .brand-codex { color: #349c78; }'):
        assert rule in css


def test_confirmed_unused_definitions_stay_removed():
    tools = (FRONT / 'app-chat-tools.js').read_text(encoding='utf-8').split('const HS_ICONS = {', 1)[1].split('\n};', 1)[0]
    for key in ('again', 'bolt', 'edit'):
        assert not re.search(r'^  ' + key + r':', tools, re.M)
    arr = (FRONT / 'app-panel-arrangements.js').read_text(encoding='utf-8').split('const _ARR_ICONS = {', 1)[1].split('\n};', 1)[0]
    assert not re.search(r'^  plus:', arr, re.M)


def test_core_icons_evaluate_without_changing_dynamic_key_count():
    script = r'''
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('const _svg =');
const end = source.indexOf('\n};', source.indexOf('const ICONS = {')) + 3;
const icons = new Function(source.slice(start, end) + '\nreturn ICONS;')();
assert.equal(Object.keys(icons).length, 40);
for (const key of ['server', 'laptop', 'bell', 'chart', 'zap']) assert.equal(typeof icons[key], 'string');
'''
    subprocess.run(['node', '-e', script, str(FRONT / 'app-core.js')], check=True)
