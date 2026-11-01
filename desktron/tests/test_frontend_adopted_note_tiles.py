"""Pin adopted TaskNote V3 and warm-white paper/warm-gray fold note V19."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'
EXPECTED = {
    'tasknote': ('01-tasknote-v3.svg', '581834a510cc69f4d6400500742f476dabf4af50d0887b9fc7d5b1328aad12e8'),
    '便笺': ('02-note-v19.svg', '9cad8c19662202ab78c45ede87445b25628b5c73768db205c8bbf73202723e95'),
}


def render(cases):
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    snippet = source[source.index('const _APP_COLORS'):source.index('const _APP_RECENT_MAX')]
    code = 'const escapeHtml=s=>String(s);\n' + snippet
    code += '\nconsole.log(JSON.stringify(' + json.dumps(cases, ensure_ascii=False)
    code += '.map(args=>_appIconHTML(...args))));'
    return json.loads(subprocess.check_output(['node', '-e', code], text=True, encoding='utf-8'))


def approved(name):
    file, digest = EXPECTED[name]
    data = (FRONT / 'icons/reviewed-scattered' / file).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    return data.decode('utf-8').strip()


def test_adopted_sources_and_manifest_are_pinned():
    manifest = json.loads((FRONT / 'vendor/scattered-inline-icon-manifest.json').read_text(encoding='utf-8'))
    assert manifest['paused_tiles'] == []
    assert len(manifest['adopted_tiles']) == 2
    for row in manifest['adopted_tiles']:
        file, digest = EXPECTED[row['name']]
        assert row['source_file'] == 'frontend/icons/reviewed-scattered/' + file
        assert row['source_sha256'] == digest
        approved(row['name'])


def test_alias_name_key_description_keep_exact_adopted_artwork():
    cases, expected = [], []
    for name, aliases in [('tasknote', ['TaskNote', 'notepaper', 'taskpaper', '任务纸', '待办清单']),
                          ('便笺', ['便笺', '笔记', '便签', '备忘录', 'note'])]:
        for alias in aliases:
            for args in [[alias, 'custom', '', '', ''], ['示例', alias, 'sm', '', ''],
                         ['示例', 'custom', '', alias, '']]:
                cases.append(args)
                extra = ' sm' if args[2] else ''
                expected.append('<div class="ap-ic tile' + extra + '">' + approved(name) + '</div>')
    assert render(cases) == expected


def test_note_has_no_lines_warm_white_lower_two_thirds_and_warm_gray_fold():
    svg = ET.fromstring(approved('便笺'))
    ns = {'s': 'http://www.w3.org/2000/svg'}
    assert svg.attrib['viewBox'] == '0 0 48 48'
    assert not svg.findall('.//s:g', ns)
    assert not svg.findall('.//s:line', ns)
    assert not svg.findall('.//s:circle', ns)
    paths = svg.findall('s:path', ns)
    assert len(paths) == 2
    assert paths[0].attrib['fill'] == 'url(#paper)'
    paper = svg.find("s:defs/s:linearGradient[@id='paper']", ns)
    assert {k: paper.attrib[k] for k in ('x1', 'y1', 'x2', 'y2')} == {'x1': '0', 'y1': '0', 'x2': '0', 'y2': '1'}
    assert [(n.attrib['offset'], n.attrib['stop-color']) for n in paper] == [
        ('0', '#ffd166'), ('.333333', '#ffd166'),
        ('.333333', '#f8f6f1'), ('1', '#e9e6de'),
    ]
    # Freeze the adopted geometry rather than requiring a retired artwork file.
    assert [p.attrib['d'] for p in paths] == [
        'M14 0H34C43.4 0 48 4.6 48 14V30Q48 32 46.6 33.4L33.4 46.6Q32 48 30 48H14C4.6 48 0 43.4 0 34V14C0 4.6 4.6 0 14 0Z',
        'M30 48Q32 48 32 46V38Q32 32 38 32H46Q48 32 48 30Q48 32 46.6 33.4L33.4 46.6Q32 48 30 48Z',
    ]
    fold = svg.find("s:defs/s:linearGradient[@id='fold']", ns)
    assert {k: fold.attrib[k] for k in ('x1', 'y1', 'x2', 'y2')} == {'x1': '0', 'y1': '0', 'x2': '1', 'y2': '1'}
    assert [(n.attrib['offset'], n.attrib['stop-color']) for n in fold] == [('0', '#dedad2'), ('1', '#cec8bd')]
    assert paths[1].attrib['fill'] == 'url(#fold)'


def test_tasknote_has_small_red_checks_and_warm_white_background():
    svg = ET.fromstring(approved('tasknote'))
    ns = {'s': 'http://www.w3.org/2000/svg'}
    assert [n.attrib['stop-color'] for n in svg.findall('.//s:stop', ns)] == ['#fbfaf8', '#f1efe9']
    checks = next(g for g in svg.findall('.//s:g', ns) if g.attrib.get('stroke') == '#ef5350')
    assert len(checks) == 3
    assert all('scale(.85)' in g.attrib['transform'] for g in checks)
    assert not svg.findall('.//s:circle', ns)


def test_explicit_image_and_tasknote_precedence_are_preserved():
    cases = [['TaskNote', 'custom', '', '', '/icons/custom.png'],
             ['TaskNote', 'notepaper', '', 'note code task', '']]
    values = render(cases)
    assert '<img src="/icons/custom.png"' in values[0]
    assert '<svg' not in values[0]
    assert values[1] == '<div class="ap-ic tile">' + approved('tasknote') + '</div>'
