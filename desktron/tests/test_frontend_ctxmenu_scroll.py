"""长模型列表使用生产菜单和 CSS，在隔离 WebKit 中验证滚动与视口边界。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "frontend/app-panel-terms.js").read_text(encoding='utf-8')
MENU = re.search(r"function openCtxMenu\(.*?\n}\n", SOURCE, re.S).group()
CSS = (ROOT / "frontend/style.css").read_text(encoding='utf-8')


def test_menu_has_scroll_container():
    rule = re.search(r"\.rp-ctx \{(.*?)\n}", CSS, re.S).group(1)
    for declaration in ("overflow-y: auto", "overscroll-behavior: contain",
                        "box-sizing: border-box", "max-height:", "max-width:"):
        assert declaration in rule


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_long_model_menu_webkit(tmp_path):
    script = r'''
const assert = (ok, label) => { if (!ok) throw new Error(label); };
const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
async function run() {
  const rows = [];
  for (const [width,height] of [[850,650],[390,600],[320,300]]) {
    const f = document.createElement('iframe');
    f.style.cssText = `border:0;width:${width}px;height:${height}px`;
    const loaded = new Promise(resolve => f.onload = resolve);
    f.srcdoc = '<html><style>' + CSS + '</style><body></body></html>';
    document.body.append(f); await loaded;
    const w = f.contentWindow, d = w.document;
    w.eval('const RP_CHECK="•", RP_CHEV="›"; function escapeHtml(s){return s;}' + MENU);
    const anchor = d.createElement('button');
    anchor.style.cssText = 'position:fixed;bottom:8px;right:8px';
    anchor.textContent = 'model'; d.body.append(anchor);
    let picked = -1;
    const items = Array.from({length:31}, (_,i) => ({
      label:'Model '+i+' · long model provider name', checked:i===2, fn:()=>picked=i
    }));
    items.push({label:'Effort',chev:true,fn:()=>w.openCtxMenu(20,20,[{label:'High',fn:()=>{}}],'rp-wide')});
    w.openCtxMenu._anchor = anchor;
    w.openCtxMenu(width-80, -900, items, 'rp-wide');
    const m = d.querySelector('.rp-ctx');
    function contained(label) {
      const r = m.getBoundingClientRect();
      assert(r.top>=7 && r.left>=7 && r.bottom<=w.innerHeight-7 && r.right<=w.innerWidth-7,label);
    }
    contained('viewport '+width);
    assert(m.scrollHeight>m.clientHeight,'scrollable '+width);
    assert(w.getComputedStyle(m).overflowY==='auto','overflow');
    m.scrollTop=m.scrollHeight;
    assert(m.scrollTop>0,'scroll moved');
    const last=m.lastElementChild.getBoundingClientRect();
    const rect=m.getBoundingClientRect();
    assert(last.bottom<=rect.bottom && last.top>=rect.top,'last action reachable');
    // 模拟窗口高度变化，菜单仍完整留在屏幕中。
    f.style.height = (height-70)+'px'; await frame(); await frame();
    w.dispatchEvent(new w.Event('resize')); contained('resize '+width);
    m.scrollTop=m.scrollHeight;
    m.lastElementChild.click();
    const sub=d.querySelector('.rp-ctx');
    assert(sub!==m && sub.textContent==='High','replacement submenu');
    assert(sub.offsetHeight<100,'short menu not stretched');
    w.openCtxMenu(8,8,items,'rp-wide');
    const again=d.querySelector('.rp-ctx'); again.scrollTop=again.scrollHeight;
    again.children[30].click();
    assert(picked===30 && !d.querySelector('.rp-ctx'),'select final model');
    // 可视视口缩小/偏移（手机软键盘）；事件应重新限高和定位。
    const vv=new w.EventTarget(); Object.assign(vv,{width:width-20,height:150,offsetTop:30,offsetLeft:10});
    Object.defineProperty(w,'visualViewport',{value:vv,configurable:true});
    w.openCtxMenu(0,0,items,'rp-wide');
    const keyboard=d.querySelector('.rp-ctx');
    vv.height=130; vv.dispatchEvent(new w.Event('resize'));
    const k=keyboard.getBoundingClientRect();
    assert(k.top>=38 && k.bottom<=152 && k.left>=18,'visual viewport');
    w.openCtxMenu.close();
    rows.push({width,height,scrollable:true}); f.remove();
  }
  window.webkit.messageHandlers.result.postMessage({rows});
}
run().catch(e=>window.webkit.messageHandlers.result.postMessage({error:String(e),stack:e.stack}));
'''
    html = ('<html><body><script>const CSS=' + json.dumps(CSS)
            + ';const MENU=' + json.dumps(MENU) + ';' + script + '</script></body></html>')
    page = tmp_path / 'menu.html'
    page.write_text(html, encoding='utf-8')
    binary = tmp_path / 'webkit'
    subprocess.run(['swiftc', str(Path(__file__).parent / 'fixtures/chat_latency_webkit.swift'),
                    '-o', str(binary)], check=True, capture_output=True, text=True, timeout=90, encoding='utf-8')
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert 'error' not in data, data
    assert len(data['rows']) == 3
