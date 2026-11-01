"""Contract for the separately approved 25-symbol redraw, not the older 46."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'
MANIFEST = json.loads((FRONT / 'vendor/remaining-inline-icon-manifest.json').read_text(encoding='utf-8'))
NS = {'s': 'http://www.w3.org/2000/svg'}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def runtime_icons():
    script = r'''
const fs = require('fs'), path = require('path');
const front = process.argv[1], result = {};
for (const [file, obj] of [['app-chat-tools.js', 'HS_ICONS'], ['app-panel-arrangements.js', '_ARR_ICONS']]) {
  const text = fs.readFileSync(path.join(front, file), 'utf8');
  const start = text.indexOf('const ' + obj + ' = {');
  const end = text.indexOf('\n};', start) + 3;
  const icons = new Function(text.slice(start, end) + '\nreturn ' + obj + ';')();
  for (const [key, value] of Object.entries(icons)) result[obj + '.' + key] = value;
}
const browser = fs.readFileSync(path.join(front, 'app-browser.js'), 'utf8');
for (const symbol of ['_BR_ICON_BACK','_BR_ICON_FWD','_BR_ICON_RELOAD','_BR_ICON_PLUS','_BR_ICON_GLOBE']) {
  const expr = browser.match(new RegExp('const ' + symbol + ' = ([\\s\\S]*?);'))[1];
  result[symbol] = new Function('return (' + expr + ');')();
}
console.log(JSON.stringify(result));
'''
    return json.loads(subprocess.check_output(['node', '-e', script, str(FRONT)], text=True, encoding='utf-8'))


ICONS = runtime_icons()


def shape(node):
    return (node.tag, sorted(node.attrib.items()), [shape(c) for c in node])


def test_all_25_evaluated_icons_match_approved_source_and_paint():
    rows = MANIFEST['icons']
    assert len(rows) == MANIFEST['count'] == 25
    assert len({row['symbol'] for row in rows}) == 25
    for row in rows:
        approved_bytes = (ROOT / row['approved_svg']).read_bytes()
        assert sha(approved_bytes) == row['approved_draft_sha256']
        approved = ET.fromstring(approved_bytes)
        value = ICONS[row['symbol']]
        assert sha(value.encode('utf-8')) == row['svg_sha256']
        actual = ET.fromstring(value)
        assert actual.attrib['viewBox'] == '0 0 32 32'
        assert actual.attrib['data-desk-icon'] == row['symbol']
        assert actual.attrib['aria-hidden'] == 'true'
        assert {k: actual.attrib[k] for k in ('width', 'height') if k in actual.attrib} == row['display_attributes']
        assert len(actual) == 1
        paint = actual.find('s:g', NS)
        assert paint is not None
        for key in ('fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin'):
            assert paint.attrib[key] == approved.attrib[key]
        assert [shape(c) for c in paint] == [shape(c) for c in approved]
        assert not actual.findall('.//s:script', NS)
        assert not actual.findall('.//s:image', NS)
    assert len({row['approved_draft_sha256'] for row in rows}) == 21


def test_symbol_counts_and_original_fork_preserved_without_removed_keys():
    assert len([key for key in ICONS if key.startswith('HS_ICONS.')]) == 15
    assert len([key for key in ICONS if key.startswith('_ARR_ICONS.')]) == 6
    assert len([key for key in ICONS if key.startswith('_BR_ICON_')]) == 5
    assert ICONS['HS_ICONS.fork'] == '<span class="hs-fork-icon" aria-hidden="true"></span>'
    tools = (FRONT / 'app-chat-tools.js').read_text(encoding='utf-8')
    fork = re.search(r'^  fork: (.*),$', tools, re.M).group(1)
    assert sha(fork.encode('utf-8')) == MANIFEST['preserved_fork_expression_sha256']
    for key in ('HS_ICONS.again', 'HS_ICONS.bolt', 'HS_ICONS.edit', '_ARR_ICONS.plus'):
        assert key not in ICONS


def test_final_straight_latitudes_and_distinct_navigation_and_lock_states():
    globe = ET.fromstring(ICONS['_BR_ICON_GLOBE'])
    assert globe.find('.//s:path', NS).attrib['d'] == 'M5.5 12.1h21M5.5 19.9h21'
    assert globe.find('.//s:circle', NS).attrib == {'cx': '16', 'cy': '16', 'r': '11.2'}
    assert globe.attrib['width'] == globe.attrib['height'] == '38'
    assert 'M26.2 16H5.8' in ICONS['_BR_ICON_BACK']
    assert 'M5.8 16h20.4' in ICONS['_BR_ICON_FWD']
    assert ICONS['HS_ICONS.lock'] != ICONS['HS_ICONS.unlock']
    for a, b in [('HS_ICONS.chev', '_ARR_ICONS.chev'), ('HS_ICONS.chev2', '_ARR_ICONS.down'), ('HS_ICONS.qclose', '_ARR_ICONS.close')]:
        assert shape(ET.fromstring(ICONS[a]).find('s:g', NS)) == shape(ET.fromstring(ICONS[b]).find('s:g', NS))


def test_prior_active_contract_and_legacy_feather_inventory_are_separate():
    prior = json.loads((FRONT / 'vendor/original-inline-icon-manifest.json').read_text(encoding='utf-8'))
    assert len(prior['icons']) == 41
    assert not {row['symbol'] for row in prior['icons']} & {row['symbol'] for row in MANIFEST['icons']}
    sources = json.loads((FRONT / 'vendor/inline-icon-sources.json').read_text(encoding='utf-8'))
    assert [row['symbol'] for row in sources['matched']] == ['ICONS.zap']
