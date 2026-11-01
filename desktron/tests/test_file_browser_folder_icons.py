"""原创目录开合图标：状态切换、透明边界、深浅主题与窄屏。"""
from pathlib import Path
import hashlib
import json
import struct
from urllib.parse import urlparse, parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'
HTML = (FRONT / 'pages/file-browser.html').read_text(encoding='utf-8')
ICONS = {'file-browser-folder-closed-original-34d18b546b71.png': '34d18b546b71d7e2c08a543e7b4f63fe3aef79d1795959f86bf5963ff19dd2f0', 'file-browser-folder-open-original-615e0fce22fa.png': '615e0fce22fafccaecd1cea4bd6ed659e5366206e1456ffa7bf8034641abc290', 'file-browser-document-original-d8bd1e3d7169.png': 'd8bd1e3d716928fce1827bcab3f77fc10796df1efb4c26ef722bbe87f58dc2a9'}


def test_reviewed_original_assets_and_sources():
    license_text = (FRONT / "vendor/ICONS-LICENSE.txt").read_text(encoding="utf-8")
    for name, digest in ICONS.items():
        data = (FRONT / "icons" / name).read_bytes()
        assert struct.unpack(">II", data[16:24]) == (512, 512)
        assert hashlib.sha256(data).hexdigest() == digest
        assert (FRONT / "icons" / name.replace(".png", ".svg")).is_file()
        assert "/icons/" + name in HTML
        assert name in license_text
    assert "Desktron 原创文件图标" in license_text
    assert "8316630.png" not in HTML



@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        def route(req):
            url = urlparse(req.request.url)
            if url.path == '/file-browser':
                req.fulfill(content_type='text/html', body=HTML)
            elif url.path.startswith('/icons/'):
                req.fulfill(content_type='image/png', body=(FRONT / url.path.lstrip('/')).read_bytes())
            elif url.path == '/workbench/fs':
                path = parse_qs(url.query).get('path', [''])[0]
                items = ([{'name': 'Closed folder', 'path': 'closed', 'type': 'dir'},
                          {'name': 'Open folder', 'path': 'open', 'type': 'dir'},
                          {'name': 'Empty folder', 'path': 'empty', 'type': 'dir'},
                          {'name': 'notes.txt', 'type': 'file'}] if not path else
                         [{'name': 'Nested folder', 'path': path + '/nested', 'type': 'dir'}] if path in ('closed', 'open') else [])
                req.fulfill(content_type='application/json', body=json.dumps({'items': items}))
            else:
                req.fulfill(content_type='text/javascript', body='')
        page.route('http://folders.test/**', route)
        yield page
        browser.close()


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.desktop
def test_folder_state_and_normalized_ink_geometry(page, theme, width):
    page.set_viewport_size({'width': width, 'height': 800})
    page.add_init_script("if (!localStorage.getItem('cd_file_browser_expanded__desktop')) localStorage.setItem('cd_file_browser_expanded__desktop', '[\"open\"]')")
    # 独立加载内容页时用系统主题入口，不人为触发宿主主题观察器的自观察循环。
    page.emulate_media(color_scheme=theme)
    page.goto('http://folders.test/file-browser')
    assert page.evaluate('document.documentElement.dataset.theme') == theme
    opened = page.locator('.tree-row[data-path="open"]')
    closed = page.locator('.tree-row[data-path="closed"]')
    empty = page.locator('.tree-row[data-path="empty"]')
    opened.locator('.folder-open').wait_for(state='visible')
    assert closed.locator('.folder-closed').is_visible()
    assert not opened.locator('.folder-closed').is_visible()
    assert not closed.locator('.folder-open').is_visible()
    # 图像透明边界而非 img 方框：用真实浏览器解码像素，在等比缩放后的墨迹上比较。
    bounds = page.evaluate('''async () => {
      const result = [];
      for (const selector of ['.tree-row[data-path="closed"] .folder-closed', '.tree-row[data-path="open"] .folder-open']) {
        const img = document.querySelector(selector); await img.decode();
        const canvas = document.createElement('canvas'); canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
        const ctx = canvas.getContext('2d'); ctx.drawImage(img, 0, 0);
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        let x0=512, y0=512, x1=0, y1=0;
        for (let y=0; y<512; y++) for (let x=0; x<512; x++) if (data[(y*512+x)*4+3]>5) {
          x0=Math.min(x0,x); y0=Math.min(y0,y); x1=Math.max(x1,x); y1=Math.max(y1,y);
        }
        const r=img.getBoundingClientRect(), slot=img.closest('.ic').getBoundingClientRect();
        const style=getComputedStyle(img);
        result.push({w:(x1-x0+1)*r.width/512,h:(y1-y0+1)*r.height/512,
          slotW:slot.width,slotH:slot.height,filter:style.filter,fit:style.objectFit});
      } return result;
    }''')
    assert abs(bounds[0]['w'] - bounds[1]['w']) < 1.25, bounds
    # 独立开合素材高度一致，开合后名称与槽位保持稳定。
    assert abs(bounds[0]["h"] - bounds[1]["h"]) < .2, bounds
    assert .65 < bounds[0]["h"] / bounds[0]["w"] < .90, bounds
    assert opened.locator(".folder-open").get_attribute("src") != closed.locator(".folder-closed").get_attribute("src")
    file_icon = page.locator('.tree-row.file .document-icon')
    assert file_icon.count() == 1
    assert file_icon.evaluate('el => getComputedStyle(el).webkitMaskImage').endswith('file-browser-document-original-d8bd1e3d7169.png")')
    assert file_icon.evaluate('el => getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
    for b in bounds:
        assert b['slotW'] == b['slotH'] == 15
        assert b['filter'] == 'none' and b['fit'] == 'contain'
    before = closed.locator('.name').bounding_box()
    closed.click()
    closed.locator('.folder-open').wait_for(state='visible')
    assert before == closed.locator('.name').bounding_box()
    closed.click()
    closed.locator('.folder-closed').wait_for(state='visible')
    empty.click()
    empty.locator('.folder-open').wait_for(state='visible')
    empty.click()
    empty.locator('.folder-closed').wait_for(state='visible')
    opened.click()
    opened.locator('.folder-closed').wait_for(state='visible')
    # 持久展开与自动定位都走同一 open 状态类。
    page.reload()
    opened.locator('.folder-closed').wait_for(state='visible')
    page.goto('http://folders.test/file-browser?path=open/nested&dir=1')
    page.locator('.tree-row[data-path="open/nested"] .folder-open').wait_for(state='visible')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_preview_only_uses_local_assets():
    assert 'cdn-icons-png.flaticon.com' not in HTML
    assert 'dir: \'<svg' not in HTML
