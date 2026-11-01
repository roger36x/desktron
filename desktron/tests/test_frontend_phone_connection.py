"""设置页手机连接：离线编码、实际权限状态、窄屏和复制失败。"""
from pathlib import Path
import json
import re
import subprocess

import pytest
from frontend_i18n_js import BROWSER_SCRIPT, NODE_PRELUDE

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = (ROOT / "frontend/app-settings-core.js").read_text(encoding='utf-8')
QR = (ROOT / "frontend/vendor/qrcode.js").read_text(encoding='utf-8')
CSS = (ROOT / "frontend/style.css").read_text(encoding='utf-8')


def test_phone_route_and_vendored_qr_are_delivered():
    model = re.search(r"const _SM_NAV_MODEL = \[[\s\S]+?\n\];", SETTINGS).group()
    assert model.index('id: "browser"') < model.index('id: "phone"') < model.index('id: "intranet"')
    assert '_routeSettingsSection(phone, "phone")' in SETTINGS
    assert 'gTools.appendChild(phone)' in SETTINGS
    assert '/vendor/qrcode.js' in SETTINGS
    assert 'phone.querySelector(".sm-card").className = "sm-phone-cards"' in SETTINGS
    assert "else phone.querySelector('.sm-phone-cards').replaceChildren()" in SETTINGS
    assert "document.querySelectorAll('.sm-phone-section .sm-phone-cards')" in SETTINGS
    assert '.sm-phone-cards { display: flex; flex-direction: column; gap: 18px;' in CSS
    assert "MIT" in (ROOT / "frontend/vendor/qrcode-LICENSE.txt").read_text(encoding='utf-8')
    assert "function" in QR


def _run_phone_node(state, assertions):
    source = SETTINGS[SETTINGS.index('let _phoneQrPromise ='):SETTINGS.index('function _paintSystemArrangementsSettings')]
    script = r'''
const assert=require('node:assert/strict');
const elements=new Map(), blocks=new Map();
function el(){return {textContent:'',innerHTML:'',setAttribute(){},focus(){},select(){},remove(){this.removed=true}}}
function block(){const nodes=new Map(); return {querySelector(k){return nodes.get(k)||null},nodes}}
const card=el();
Object.defineProperty(card,'innerHTML',{get(){return this.html||''},set(value){
  this.html=value;blocks.clear();elements.clear();
  for(const match of value.matchAll(/data-entry="([^"]+)"/g)){
    const b=block();blocks.set(match[1],b);
    for(const k of ['.sm-phone-qr','.sm-phone-address','.sm-phone-feedback','.sm-phone-copy-link']) b.nodes.set(k,el());
    b.nodes.set('.sm-phone-copy',{appendChild(input){b.nodes.set('.sm-phone-link',input)}});
  }
  for(const k of ['.sm-phone-ts-hint','.sm-phone-ts-command']) elements.set(k,el());
}});
card.querySelector=k=>k.startsWith('[data-entry=') ? blocks.get(k.match(/"([^"]+)"/)[1]) : elements.get(k);
const section={isConnected:true,hidden:false,querySelector:selector=>{
  assert.equal(selector,'.sm-phone-cards');return card;
}};
const scripts=[];let copied=[];let timers=[];
globalThis.window={};
globalThis.document={getElementById:()=>({hidden:false}),head:{appendChild:s=>scripts.push(s)},createElement:()=>el()};
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>copied.push(text)}}});
globalThis.location={protocol:'http:'};
globalThis.setTimeout=fn=>{timers.push(fn);return timers.length};
globalThis.clearTimeout=()=>{};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
'''
    script += '\nconst state=' + json.dumps(state) + ';\n'
    script += 'globalThis.fetch=async()=>({ok:true,json:async()=>state});\n'
    script += source + '\n(async()=>{' + assertions + '})().catch(e=>{console.error(e);process.exitCode=1});'
    subprocess.run(['node', '-e', NODE_PRELUDE + script], check=True)


def test_both_phone_links_work_while_qr_load_is_pending():
    _run_phone_node({'local': True, 'enabled': True, 'address': 'http://lan.example/',
        'url': 'http://lan.example/?token=phone-test-fixture', 'tailscale': {'enabled': True,
        'state': 'ready', 'address': 'https://mac.example.ts.net/',
        'url': 'https://mac.example.ts.net/?token=phone-test-fixture'}}, r'''
const pending=renderPhoneConnectionSettings(section); await tick();
assert.equal(scripts.length,1); assert.equal(section._phoneLoading,true);
assert.equal((card.innerHTML.match(/class="sm-card sm-phone-connection-card"/g)||[]).length,2);
assert.match(card.innerHTML,/class="sm-card sm-phone-connection-card" data-entry="lan"/);
assert.match(card.innerHTML,/class="sm-card sm-phone-connection-card" data-entry="tailscale"/);
assert.equal((card.innerHTML.match(/二维码和链接包含访问凭据，请勿公开。/g)||[]).length,2);
for(const [key,address,url] of [['lan',state.address,state.url],['tailscale',state.tailscale.address,state.tailscale.url]]){
  const b=blocks.get(key);
  assert.equal(b.querySelector('.sm-phone-address').textContent,address);
  await b.querySelector('.sm-phone-copy-link').onclick();
  assert.equal(copied.at(-1),url);
}
// Clipboard denial still exposes a manual-copy input before QR is available.
navigator.clipboard.writeText=async()=>{throw new Error('denied')};
await blocks.get('tailscale').querySelector('.sm-phone-copy-link').onclick();
assert.equal(blocks.get('tailscale').querySelector('.sm-phone-link').value,state.tailscale.url);
// A suspended script is bounded, leaves the links intact and can be retried.
timers[0](); await pending;
assert.equal(section._phoneLoading,false); assert.equal(scripts[0].removed,true);
assert.equal(blocks.get('lan').querySelector('.sm-phone-qr').textContent,'二维码不可用');
assert.match(blocks.get('lan').querySelector('.sm-phone-feedback').textContent,/加载超时/);
const again=renderPhoneConnectionSettings(section); await tick();assert.equal(scripts.length,2);
window.qrcode=()=>({addData(){},make(){},createSvgTag(){return '<svg></svg>'}});
scripts[1].onload(); await again;
assert.equal(blocks.get('tailscale').querySelector('.sm-phone-qr').innerHTML,'<svg></svg>');
''')


@pytest.mark.parametrize('kind', ['missing', 'offline', 'unconfigured', 'error'])
def test_phone_tailscale_status_hint_without_link(kind):
    _run_phone_node({'local': True, 'enabled': False, 'tailscale': {'enabled': True,
        'state': kind, 'command': 'tailscale serve --bg http://127.0.0.1:7766'}}, r'''
window.qrcode=()=>({}); await renderPhoneConnectionSettings(section);
assert.equal(blocks.size,0);assert.notEqual(elements.get('.sm-phone-ts-hint').textContent,'');
assert.equal(elements.get('.sm-phone-ts-command').textContent,state.tailscale.command);
assert.equal(scripts.length,0);
''')


def test_phone_tailscale_only_and_remote_privacy():
    _run_phone_node({'local': True, 'enabled': False, 'tailscale': {'enabled': True,
        'state': 'ready', 'address': 'https://mac.example.ts.net/',
        'url': 'https://mac.example.ts.net/?token=phone-test-fixture'}}, r'''
let encoded=[];window.qrcode=()=>({addData:v=>encoded.push(v),make(){},createSvgTag(){return '<svg></svg>'}});
await renderPhoneConnectionSettings(section);
assert.equal(blocks.size,1);assert(blocks.has('tailscale'));
assert.equal((card.innerHTML.match(/class="sm-card sm-phone-connection-card"/g)||[]).length,1);
assert.deepEqual(encoded,[state.tailscale.url]);
state.local=false;location.protocol='https:';await renderPhoneConnectionSettings(section);
assert.equal(blocks.size,0);assert.match(card.innerHTML,/已通过 Tailscale 连接/);
assert(!card.innerHTML.includes('phone-test-fixture'));
''')


@pytest.mark.desktop
def test_phone_connection_rendering_and_permissions_in_webkit():
    from playwright.sync_api import sync_playwright

    source = SETTINGS[SETTINGS.index('let _phoneQrPromise ='):SETTINGS.index('function _paintSystemArrangementsSettings')]
    # Synthetic RFC1918 endpoint; the page's fetch is stubbed, no LAN requests.
    link = 'http://192.168.0.10:7765/?token=phone-test-fixture'
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            for width in (320, 390, 1280):
                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                page.set_content('<div id="settings-modal"><div class="settings-center-live" style="padding:12px">'
                                 '<section class="sm-phone-section"><div class="sm-phone-cards"></div></section></div></div>')
                page.add_style_tag(content=CSS)
                page.add_script_tag(content=QR)
                page.add_script_tag(content=BROWSER_SCRIPT)
                page.add_script_tag(content=source + '''
                  window.section = document.querySelector('.sm-phone-section');
                  window.state = {local:true, enabled:true, address:'http://192.168.0.10:7765/', url:''};
                  window.fetch = async () => ({ok:true, json:async () => window.state});
                ''')
                page.evaluate('(url) => window.state.url = url', link)
                for theme in ("dark", "light"):
                    page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
                    page.evaluate('renderPhoneConnectionSettings(section)')
                    assert page.locator('.sm-phone-qr svg').count() == 1
                    assert 'phone-test-fixture' not in page.locator('.sm-card').inner_text()
                    assert page.locator('.sm-phone-qr').get_attribute('aria-label') == '手机连接二维码'
                    bounds = page.locator('.sm-phone-qr').bounding_box()
                    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert page.locator('.sm-phone-qr').evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(255, 255, 255)'
                # Two endpoints have sibling cards, not a shared card, at every width and theme.
                page.evaluate('''state.tailscale={enabled:true,state:'ready',address:'https://mac.example.ts.net/',
                    url:'https://mac.example.ts.net/?token=phone-test-fixture'}''')
                for theme in ('dark', 'light'):
                    page.evaluate('theme => document.documentElement.dataset.theme=theme', theme)
                    page.evaluate('renderPhoneConnectionSettings(section)')
                    assert page.locator('.sm-phone-cards > .sm-card').count() == 2
                    assert page.locator('.sm-card .sm-card').count() == 0
                    for key in ('lan', 'tailscale'):
                        connection = page.locator('[data-entry="'+key+'"]')
                        assert connection.locator('.sm-phone-qr svg').count() == 1
                        assert connection.locator('.sm-phone-copy-link').count() == 1
                        assert '请勿公开' in connection.inner_text()
                    first = page.locator('[data-entry="lan"]').bounding_box()
                    second = page.locator('[data-entry="tailscale"]').bounding_box()
                    assert second['y'] >= first['y']+first['height']+17
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.evaluate('state.tailscale=null; renderPhoneConnectionSettings(section)')
                assert page.locator('.sm-phone-cards > .sm-card').count() == 1
                # Both copy success and clipboard denial must remain usable.
                page.evaluate('''Object.defineProperty(navigator, 'clipboard', {configurable:true,
                    value:{writeText:async text => { window.copied = text; }}})''')
                page.locator('.sm-phone-copy-link').click()
                assert page.evaluate('window.copied') == link
                page.evaluate('''Object.defineProperty(navigator, 'clipboard', {configurable:true,
                    value:{writeText:async () => { throw new Error('denied'); }}})''')
                page.locator('.sm-phone-copy-link').click()
                assert page.locator('.sm-phone-link').input_value() == link
                assert page.locator('.sm-phone-link').evaluate('el => el.selectionEnd - el.selectionStart') == len(link)
                # Remote browsers receive no QR, even if an accidental URL field exists.
                page.evaluate('state.local = false; renderPhoneConnectionSettings(section)')
                assert page.locator('.sm-phone-qr').count() == 0
                assert '已通过局域网连接' in page.locator('.sm-card').inner_text()
                assert 'phone-test-fixture' not in page.locator('.sm-card').inner_text()
                page.evaluate('state.local = true; state.enabled = false; renderPhoneConnectionSettings(section)')
                assert '未开放' in page.locator('.sm-card').inner_text()
                assert page.locator('.sm-phone-copy-link').count() == 0
                # A network error is visible and can be retried.
                page.evaluate('window.fetch = async () => {throw new Error("offline")}; renderPhoneConnectionSettings(section)')
                assert 'offline' in page.locator('.sm-card').inner_text()
                assert page.locator('.sm-card button').inner_text() == '重试'
                context.close()
        finally:
            browser.close()
