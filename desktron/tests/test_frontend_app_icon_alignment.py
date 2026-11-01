"""Application artwork uses equal visible extents and no added shadow."""
from pathlib import Path
import json
import re
import subprocess
from PIL import Image

FRONT = Path(__file__).resolve().parents[2] / 'frontend'


def test_browser_scale_compensates_only_transparent_padding():
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    snippet = source[source.index('const _APP_COLORS'):source.index('const _APP_RECENT_MAX')]
    code = 'const escapeHtml=s=>String(s);\n' + snippet + '\nconsole.log(_appIconHTML("浏览器","custom","sm","",""));'
    html = subprocess.check_output(['node', '-e', code], text=True, encoding='utf-8')
    scale = float(re.search(r'scale\(([0-9.]+)\)', html).group(1))
    with Image.open(FRONT / 'icons/app-browser-original-5d5d9b828321.png') as im:
        left, top, right, bottom = im.getchannel('A').getbbox()
        assert abs((right - left) * scale - im.width) < 1e-8
        assert abs((bottom - top) * scale - im.height) < 1e-8
        assert left + right == im.width and top + bottom == im.height


def test_all_official_app_icon_wrappers_have_no_added_shadow():
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    snippet = source[source.index('const _APP_COLORS'):source.index('const _APP_RECENT_MAX')]
    apps = json.loads((FRONT / 'workbench.json').read_text(encoding='utf-8'))['desktopApps']
    code = 'const escapeHtml=s=>String(s);\n' + snippet + '\nconsole.log(JSON.stringify(' + json.dumps(apps, ensure_ascii=False) + '.map(a=>_appIconHTML(a.label,a.uid,"sm",a.desc,a.icon))));'
    htmls = json.loads(subprocess.check_output(['node', '-e', code], text=True, encoding='utf-8'))
    assert len(htmls) == 7
    for html in htmls:
        assert 'ap-ic tile sm' in html or 'ap-ic img sm' in html
    css = (FRONT / 'style.css').read_text(encoding='utf-8')
    for selector in ['.ap-ic.tile', '.ap-ic.img']:
        rule = re.search(re.escape(selector) + r'\s*\{([^}]+)\}', css).group(1)
        assert 'box-shadow: none' in rule
    for selector in ['.ap-ic.tile svg', '.ap-ic.img img']:
        rules = re.findall(re.escape(selector) + r'\s*\{([^}]+)\}', css)
        assert rules and all('filter: none' in rule for rule in rules)
