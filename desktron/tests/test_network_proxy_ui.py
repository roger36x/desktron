"""Real settings DOM/CSS in an isolated Edge profile, with a synthetic local API only."""
import html
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def preview_page():
    css = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
    core = (ROOT / 'frontend/app-settings-core.js').read_text(encoding='utf-8')
    network = (ROOT / 'frontend/app-settings-network.js').read_text(encoding='utf-8')
    setup = r'''
document.documentElement.classList.add('desk-windows-shell');
document.documentElement.dataset.theme=new URLSearchParams(location.search).get('theme')==='light'?'light':'dark';
const $=s=>document.querySelector(s);
const escapeHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const DeskI18n={bind(){},t(key){return ({'settings.network':'网络与代理'})[key]||key;}};
let fixture={config:{mode:'manual',host:'127.0.0.1',port:10808,bypass:['localhost','127.0.0.1','::1'],revision:'legacy',configured:false},
 supported:true,browser_capable:true,pending:false};
const requests=[]; let failSave=false;
async function api(path,opts={}) {
 requests.push({path,method:opts.method||'GET',body:opts.body});
 if (path.endsWith('/test')) return {proxy_tcp:'passed',webpage:'not_run',model:'not_run',message:'代理 TCP 端口可连接；尚未验证 HTTP 转发、网页或模型。'};
 if(opts.method==='PUT') {
   if(failSave) throw Error('网络设置已更新，请重新读取后再保存。');
   const value=JSON.parse(opts.body); fixture={...fixture,config:{...value.config,bypass:value.config.bypass.split(','),revision:'saved',configured:true},pending:true};
 }
 return structuredClone(fixture);
}
const assert=(ok,label)=>{if(!ok)throw Error(label);};
async function mount() {
 _smActiveView='network';
 _renderSettingsNav(); _wireSettingsSearch(document.querySelector('#sm-body'));
 const host=$('#sm-host'), group=smGroup('工具与连接'); host.appendChild(group);
 const section=smSection('网络与代理','保存本机配置，不修改系统代理。');
 _routeSettingsSection(section,'network'); group.appendChild(section);
 _finishSettingsCenter(host,{}); _activateSettingsView('network');
 await renderNetworkSection(section);
 return section;
}
window.ready=mount();
'''
    markup = '''<div id="settings-modal"><div class="sm-backdrop"></div><div class="sm-panel" role="dialog" aria-modal="true" aria-label="设置">
<button id="sm-close" type="button" aria-label="关闭">×</button><div id="sm-body" class="settings-center-live">
<div class="sm-page"><nav id="sm-nav" class="sm-inner-nav" aria-label="设置目录"></nav><section class="sm-settings-content">
<div id="sm-settings-scroll"><div id="sm-host"></div></div></section></div></div></div></div>'''
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>网络与代理 · 设置页预览（模拟数据）</title><style>' + css + '</style>'
            '<body class="settings-dialog-open">' + markup + '<script>' + core + network + setup
            + '</script><pre id="result" hidden>pending</pre></body></html>')


@pytest.mark.desktop
@pytest.mark.parametrize('width', [1160, 700, 390])
def test_proxy_settings_style_and_draft_in_real_windows_edge(tmp_path, width):
    edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if os.name != 'nt' or not edge.exists():
        pytest.skip('Windows Edge required')
    script = r'''
async function verify() {
 const section=await ready;
 assert(document.querySelector('.sm-nav-row[data-settings-view="network"]').classList.contains('active'),'navigation active');
 assert(document.querySelector('.sm-view-head h1').textContent==='网络与代理','shared view title');
 const nav=$('#sm-settings-search'); nav.value='代理'; nav.dispatchEvent(new Event('input'));
 assert(document.querySelector('[data-settings-result="network"]'),'settings search includes proxy');
 nav.value=''; nav.dispatchEvent(new Event('input'));
 const mode=$('#sm-network-mode'), host=$('#sm-network-host'), port=$('#sm-network-port');
 assert(host.value==='127.0.0.1'&&port.value==='10808','no fixed port');
 const ref=document.createElement('div'); ref.className='sm-field';
 ref.innerHTML='<label class="sm-flabel">参考字段</label><input class="sm-input"><select class="sm-select"><option>参考选择框</option></select><button class="sm-save">参考按钮</button>';
 section.appendChild(ref);
 // Native shells have no no-vibrancy class; browser fallback must not be required for dark controls.
 for(const fallback of [false,true]) {
   document.documentElement.classList.toggle('no-vibrancy',fallback);
   for(const theme of [null,'cyan','light','cyan']) {
     if(theme===null) delete document.documentElement.dataset.theme;
     else document.documentElement.dataset.theme=theme;
     const expected=theme==='light'?'light':'dark';
     for(const el of [document.documentElement,mode,host,port,...mode.options])
       assert(getComputedStyle(el).colorScheme===expected,'native control theme '+JSON.stringify({fallback,theme,tag:el.tagName,actual:getComputedStyle(el).colorScheme}));
   }
 }
 document.documentElement.classList.remove('no-vibrancy');
 for(const theme of ['dark','light']) {
   document.documentElement.dataset.theme=theme;
   for(const [actual,reference] of [[host,ref.querySelector('input')],[mode,ref.querySelector('select')],[section.querySelector('.sm-save'),ref.querySelector('button')]])
     for(const key of ['backgroundColor','color','fontSize','fontFamily','borderRadius','borderTopColor','paddingTop','paddingLeft'])
       assert(getComputedStyle(actual)[key]===getComputedStyle(reference)[key],'shared '+theme+' '+key);
 }
 document.documentElement.dataset.theme='dark'; ref.remove();
 mode.value='system'; mode.dispatchEvent(new Event('change')); assert(host.closest('.sm-field').hidden,'manual fields hidden');
 mode.value='direct'; mode.dispatchEvent(new Event('change')); assert($('#sm-network-bypass').closest('.sm-field').hidden,'direct hides bypass');
 mode.value='manual'; mode.dispatchEvent(new Event('change'));
 const form=section.querySelector('form'); host.value='127.0.0.1'; port.value='7891';
 failSave=true; await form.onsubmit(new Event('submit',{cancelable:true}));
 assert(port.value==='7891'&&section.textContent.includes('草稿已保留'),'conflict preserves draft');
 failSave=false; await form.onsubmit(new Event('submit',{cancelable:true}));
 assert(section.textContent.includes('尚未完整生效'),'save not falsely live');
 const tests=[...section.querySelectorAll('button')].find(b=>b.textContent==='测试代理端口'); await tests.onclick();
 assert(section.textContent.includes('尚未验证 HTTP 转发、网页或模型'),'TCP does not prove model');
 assert(!requests.some(r=>r.path.includes('restart')),'no automatic restart');
 assert(document.querySelector('.sm-panel').scrollWidth<=document.querySelector('.sm-panel').clientWidth+2,'no panel horizontal overflow '+innerWidth);
 assert(form.scrollWidth<=form.clientWidth+2,'form not overflowing '+innerWidth);
 return {sharedStyle:true,lightDark:true,nativeControlTheme:true,search:true,draftPreserved:true,savePending:true,noAutoRestart:true,viewport:innerWidth};
}
verify().then(r=>$('#result').textContent=JSON.stringify({ok:true,...r}))
.catch(e=>$('#result').textContent=JSON.stringify({error:String(e)}));
'''
    page = tmp_path / 'proxy.html'
    page.write_text(preview_page().replace('</body>', '<script>' + script + '</script></body>'), encoding='utf-8')
    result = subprocess.run([str(edge), '--headless=new', '--disable-gpu', '--no-first-run',
                             '--disable-background-networking', '--disable-extensions',
                             '--user-data-dir=' + str(tmp_path / 'profile'), '--window-size=' + str(width) + ',860',
                             '--virtual-time-budget=4000', '--dump-dom', page.as_uri()],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
    assert result.returncode == 0, result.stderr[-1000:]
    match = re.search(r'<pre id="result" hidden="">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stdout[-1500:]
    evidence = json.loads(html.unescape(match.group(1)))
    assert evidence.get('ok'), evidence


@pytest.mark.desktop
def test_generated_browser_proxy_and_loopback_bypass(tmp_path, monkeypatch):
    import http.server
    import threading
    import network_proxy
    edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if os.name != 'nt' or not edge.exists():
        pytest.skip('Windows Edge required')
    counts = {'proxy': 0, 'origin': 0}
    class Fixture(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == 'http://proxy-fixture.invalid/fixture':
                counts['proxy'] += 1
                body = b'<html>PROXY_FIXTURE_OK</html>'
            elif self.path == '/local':
                counts['origin'] += 1
                body = b'<html>LOOPBACK_BYPASS_OK</html>'
            else:
                self.send_error(404); return
            self.send_response(200); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    for server in (proxy, origin):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(network_proxy, 'config_path', lambda: tmp_path / 'network.json')
    try:
        network_proxy.save({'mode': 'manual', 'host': '127.0.0.1', 'port': proxy.server_port}, 'legacy')
        args = network_proxy.browser_policy()['args']
        base = [str(edge), '--headless=new', '--disable-gpu', '--no-first-run',
                '--disable-background-networking', '--disable-extensions', '--dump-dom']
        for index, (url, text) in enumerate([
            ('http://proxy-fixture.invalid/fixture', 'PROXY_FIXTURE_OK'),
            ('http://127.0.0.1:' + str(origin.server_port) + '/local', 'LOOPBACK_BYPASS_OK')]):
            result = subprocess.run(base + args + ['--user-data-dir=' + str(tmp_path / ('profile' + str(index))), url],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
            assert result.returncode == 0 and text in result.stdout, result.stdout[-1000:]
        assert counts == {'proxy': 1, 'origin': 1}
    finally:
        for server in (proxy, origin):
            server.shutdown(); server.server_close()
