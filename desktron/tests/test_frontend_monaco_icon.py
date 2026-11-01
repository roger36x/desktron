"""Monaco IDE uses the approved code-only square icon."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'


def test_monaco_asset_hashes_and_minimal_geometry():
    manifest = json.loads((FRONT / 'icons/monaco-ide-original-manifest.json').read_text(encoding='utf-8'))
    assert manifest['license'] == 'Apache-2.0'
    assert manifest['size'] == [512, 512]
    for rel, digest in manifest['files'].items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest
    svg = ET.parse(FRONT / 'icons/app-monaco-ide-original-00700ed9f4c1.svg').getroot()
    assert svg.find('{http://www.w3.org/2000/svg}g').attrib['stroke-width'] == '5.4'
    ns = {'s': 'http://www.w3.org/2000/svg'}
    background = svg.find('s:path', ns)
    assert background.attrib['fill'] == '#FFFFFF'
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    assert f'const _AP_SQUIRCLE = "{background.attrib["d"]}";' in source
    paths = svg.findall('s:g/s:path', ns)
    assert len(paths) == 2
    assert paths[0].attrib['stroke'] == '#808080'
    assert paths[1].attrib['stroke'] == '#F26A3D'
    assert paths[1].attrib['d'] == 'M78 58L70 82'
    assert not svg.findall('.//s:filter', ns)
    assert not svg.findall('.//s:text', ns)
    assert not svg.findall('.//s:circle', ns)


def test_monaco_uses_plain_image_without_old_file_label():
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    snippet = source[source.index('const _APP_COLORS'):source.index('const _APP_RECENT_MAX')]
    code = 'const escapeHtml=s=>String(s);\n' + snippet + '''
console.log(JSON.stringify(['Monaco IDE','代码编辑器','IDE'].map(name =>
 _appIconHTML(name,'desktron.files.0001','','',''))));
'''
    output = subprocess.check_output(['node', '-e', code], text=True, encoding='utf-8')
    for html in json.loads(output):
        assert '/icons/app-monaco-ide-original-00700ed9f4c1.png' in html
        assert 'class="app-monaco-ide-icon"' in html
        assert 'apg-ide' not in html and 'clipPath' not in html
        assert '<text' not in html
    assert 'letter-spacing=".2">IDE</text>' not in source


def test_monaco_shadow_override_is_scoped():
    css = (FRONT / 'style.css').read_text(encoding='utf-8')
    assert '.ap-ic.img img[src^="/icons/app-monaco-ide-"], .ap-ic.tile svg.app-monaco-ide-icon { filter: none; }' in css
    assert '.ap-ic.tile svg { width: 100%; height: 100%; display: block; filter: none; }' in css


def test_other_application_icon_matches_remain_distinct():
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    snippet = source[source.index('const _APP_COLORS'):source.index('const _APP_RECENT_MAX')]
    code = 'const escapeHtml=s=>String(s);\n' + snippet + '''
console.log(JSON.stringify(['浏览器','终端','TaskNote'].map(name =>
 _appIconHTML(name,'custom','','',''))));
'''
    browser, terminal, task = json.loads(subprocess.check_output(['node', '-e', code], text=True, encoding='utf-8'))
    assert 'app-browser-original-5d5d9b828321.png' in browser
    assert 'app-terminal-3b65e9edf830.png' in terminal
    assert 'apg-notepaper' in task
    assert all('app-monaco-ide' not in html for html in (browser, terminal, task))
