"""Approved 24 reuses and two new drawings; app tiles have separate approvals."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'frontend/vendor/scattered-inline-icon-manifest.json').read_text(encoding='utf-8'))
IDS = {9, 12, 14, 16, 17, 18, 19, 23, 24, 25, 26, 27, 28, 29, 30, 31, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45}
NEW_HASHES = {
    12: '9609f381f3db8f06e90b9929a2cc3f5fe88c49b7aff117fe7e36d6a14b465c17',
    42: '9194f3117569736e7e2947d94c9eff86a5826262edec081bdcd0adad8b5d72bf',
}


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def source_svg(row):
    text = (ROOT / row['source_file']).read_text(encoding='utf-8')
    if row['source_symbol']:
        key = row['source_symbol'].split('.')[1]
        return json.loads(re.search(r'^  ' + key + r': (".*"),$', text, re.M).group(1))
    return text


def shape(node):
    return (node.tag.split('}')[-1], sorted(node.attrib.items()), [shape(c) for c in node])


def occurrences(row):
    text = (ROOT / row['file']).read_text(encoding='utf-8')
    return re.findall(r'<svg\b[^>]*data-desk-scattered="' + f"{row['id']:02}" + r'"[^>]*>.*?</svg>', text, re.S)


def test_scope_exactly_24_reuses_and_two_approved_new_drawings():
    rows = MANIFEST['icons']
    assert {r['id'] for r in rows} == IDS
    assert len(rows) == MANIFEST['count'] == 26
    assert MANIFEST['reuse_count'] == 24
    assert MANIFEST['new_count'] == 2
    assert sum(r['occurrences'] for r in rows) == 29
    assert {r['id'] for r in rows if r['kind'].startswith('new-')} == {12, 42}
    for row in rows:
        assert sha(source_svg(row)) == row['source_sha256']
        if row['id'] in NEW_HASHES:
            assert row['source_sha256'] == NEW_HASHES[row['id']]
    all_markers = []
    for file in {r['file'] for r in rows}:
        all_markers.extend(int(x) for x in re.findall(r'data-desk-scattered="(\d+)"', (ROOT / file).read_text(encoding='utf-8')))
    assert set(all_markers) == IDS
    assert len(all_markers) == 29


def test_all_runtime_variants_preserve_approved_geometry_and_context_paint():
    for row in MANIFEST['icons']:
        approved = ET.fromstring(source_svg(row))
        expected = list(approved[0]) if row['source_symbol'] else list(approved)
        if row['id'] == 18:
            for node in expected:
                for child in node.iter():
                    for key, value in list(child.attrib.items()):
                        if value == '#45454b':
                            child.set(key, 'currentColor')
        values = occurrences(row)
        assert len(values) == row['occurrences'] == len(row['variants'])
        for svg, variant in zip(values, row['variants']):
            assert sha(svg) == variant['svg_sha256']
            actual = ET.fromstring(svg)
            assert actual.attrib['viewBox'] == '0 0 32 32'
            assert actual.attrib['aria-hidden'] == 'true'
            display = {k: v for k, v in actual.attrib.items() if k not in ('viewBox', 'aria-hidden', 'data-desk-scattered')}
            assert display == variant['display_attributes']
            assert len(actual) == 1 and actual[0].tag == 'g'
            paint = actual[0]
            assert paint.attrib['fill'] == 'none'
            assert paint.attrib['stroke-linecap'] == paint.attrib['stroke-linejoin'] == 'round'
            expected_paint = approved[0].attrib if row['source_symbol'] else approved.attrib
            assert paint.attrib['stroke-width'] == expected_paint['stroke-width']
            color = 'var(--ink-42)' if row['id'] == 23 else '#fff' if row['id'] == 45 else 'currentColor'
            assert paint.attrib['stroke'] == color
            assert [shape(c) for c in paint] == [shape(c) for c in expected]
            assert not any(n.tag in ('script', 'image', 'use') for n in actual.iter())


def test_runtime_js_constants_evaluate_and_keep_original_display_sizes():
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const root = process.argv[1];
for (const [file, symbol, id, size] of [
  ['app-core.js','CD_COPY_ICON','09','14'],
  ['app-settings-sections.js','EYE_SVG','12','15'],
  ['app-approval.js','_APPR_CHEV','23',null],
  ['app-panel-arrangement-system.js','ARR_SYS_BACK_ICON','16',null]
]) {
  const text = fs.readFileSync(root + '/frontend/' + file, 'utf8');
  const expr = text.match(new RegExp('const ' + symbol + ' = ([\\s\\S]*?);'))[1];
  const value = vm.runInNewContext('(' + expr + ')');
  assert.ok(value.includes('data-desk-scattered="' + id + '"'));
  if (size) assert.ok(value.includes('width="' + size + '" height="' + size + '"'));
}
'''
    subprocess.run(['node', '-e', script, str(ROOT)], check=True)


def test_tasknote_and_note_tiles_have_separate_adoption_records():
    text = (ROOT / 'frontend/app-assets-apps.js').read_text(encoding='utf-8')
    assert MANIFEST['paused_tiles'] == []
    assert {r['name'] for r in MANIFEST['adopted_tiles']} == {'tasknote', '便笺'}
    for row in MANIFEST['adopted_tiles']:
        assert sha((ROOT / row['source_file']).read_text(encoding='utf-8')) == row['source_sha256']
    assert 'data-desk-scattered="01"' not in text
    assert 'data-desk-scattered="02"' not in text


def test_review_sources_remain_bound_to_prior_manifests():
    prior = json.loads((ROOT / 'frontend/vendor/original-inline-icon-manifest.json').read_text(encoding='utf-8'))
    prior_by_symbol = {r['symbol']: r for r in prior['icons']}
    remaining = json.loads((ROOT / 'frontend/vendor/remaining-inline-icon-manifest.json').read_text(encoding='utf-8'))
    remaining_by_file = {r['approved_svg']: r for r in remaining['icons']}
    for row in MANIFEST['icons']:
        if row['source_symbol']:
            assert row['source_sha256'] == prior_by_symbol[row['source_symbol']]['svg_sha256']
        elif row['source_file'] in remaining_by_file:
            assert row['source_sha256'] == remaining_by_file[row['source_file']]['approved_draft_sha256']
    warning = ET.fromstring(occurrences(next(r for r in MANIFEST['icons'] if r['id'] == 18))[0])
    assert warning.find('.//circle').attrib['r'] == '13.7'
    assert not warning.findall('.//polygon')


def test_independent_pages_keep_control_ids_and_save_as_action():
    for name, ids in {
        'notepad': ['btnSideCollapse', 'btnSideExpand', 'btnNewFolder', 'mBackFolders', 'mBackList', 'btnNew', 'btnDelete', 'searchInput'],
        'file-browser': ['side-toggle', 'open-app-menu'],
        'files': ['search-icon', 'm-side', 'btn-save', 'btn-cancel'],
    }.items():
        text = (ROOT / f'frontend/pages/{name}.html').read_text(encoding='utf-8')
        for element_id in ids:
            assert len(re.findall(r'\bid="' + element_id + '"', text)) == 1
    browser = (ROOT / 'frontend/pages/file-browser.html').read_text(encoding='utf-8')
    assert 'data-action="save-as"' in browser
    assert 'data-desk-scattered="42"' in browser
