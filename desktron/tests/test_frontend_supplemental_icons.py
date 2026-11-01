"""Five approved supplementary icons retain V4 geometry and existing usage wiring."""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

FRONT = Path(__file__).resolve().parents[2] / 'frontend'
MANIFEST = json.loads((FRONT / 'icons/supplemental-original-manifest.json').read_text(encoding='utf-8'))
ICONS = {i['name']: i for i in MANIFEST['icons']}


def test_manifest_contains_exactly_the_five_approved_icons():
    assert MANIFEST['version'] == 'supplemental-original-v4'
    assert MANIFEST['license'] == 'Apache-2.0'
    assert set(ICONS) == {'browser', 'move', 'bell', 'shepherd', 'root-folder'}


@pytest.mark.parametrize('name', sorted(ICONS))
def test_svg_and_png_match_registered_hashes_and_have_safe_margins(name):
    item = ICONS[name]
    for ext in ('svg', 'png'):
        p = FRONT / item[ext]
        assert p.is_relative_to(FRONT)
        assert hashlib.sha256(p.read_bytes()).hexdigest() == item[ext + '_sha256']
    svg = ET.fromstring((FRONT / item['svg']).read_bytes())
    assert svg.attrib['width'] == svg.attrib['height'] == '512'
    with Image.open(FRONT / item['png']) as im:
        assert im.size == (512, 512)
        assert im.mode == 'RGBA'
        left, top, right, bottom = im.getchannel('A').getbbox()
        assert left > 0 and top > 0 and right < 512 and bottom < 512


def test_ide_root_reuses_blue_folder_and_browser_retains_orange():
    for ext in ('svg', 'png'):
        assert (FRONT / ICONS['root-folder'][ext]).read_bytes() == (FRONT / f'icons/ide-folder-closed-original-905d2778fcb2.{ext}').read_bytes()
    assert '/icon-folder.png' in (FRONT / 'app-files.js').read_text(encoding='utf-8')
    browser = (FRONT / 'pages/file-browser.html').read_text(encoding='utf-8')
    assert '/icons/file-browser-folder-closed-original-34d18b546b71.png' in browser
    assert '#FFB90B' in (FRONT / 'icons/file-browser-folder-closed-original-34d18b546b71.svg').read_text(encoding='utf-8')


def test_bell_body_is_solid_without_changing_its_outer_bounds():
    svg = ET.fromstring((FRONT / ICONS['bell']['svg']).read_bytes())
    body = svg.find('{http://www.w3.org/2000/svg}path')
    assert body.attrib['fill'] == '#45454b'
    with Image.open(FRONT / ICONS['bell']['png']) as im:
        assert im.getchannel('A').getbbox() == (73, 40, 439, 474)
        assert im.getpixel((256, 256))[3] == 255
        assert im.getpixel((256, 176))[3] == 255
        assert im.getpixel((0, 0))[3] == 0
    css = (FRONT / 'style.css').read_text(encoding='utf-8')
    rule = css.split('.term-item .ti-completed-unread {', 1)[1].split('}', 1)[0]
    assert 'width: 10px; height: 10px' in rule


def test_shepherd_ear_is_solid_and_eye_remains_transparent():
    with Image.open(FRONT / ICONS['shepherd']['png']) as im:
        assert im.getpixel((267, 163))[3] == 255
        assert im.getpixel((336, 250))[3] == 0


def test_existing_css_mask_consumers_and_welcome_are_preserved():
    css = (FRONT / 'style.css').read_text(encoding='utf-8')
    for name in ('move', 'bell', 'shepherd'):
        assert '/' + ICONS[name]['png'] in css
    assert 'BRAND_ICONS.shepherd' in (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    assert '/' + ICONS['browser']['png'] in (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
