"""Mobile sidebar/chat overlays ignore sticky hover, focus and keyboard layout changes."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'frontend/app-init.js').read_text(encoding='utf-8')
SCROLLBARS = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);', SOURCE, re.S).group()
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')


@pytest.mark.skipif(not shutil.which('node'), reason='需要 Node 执行事件回归')
@pytest.mark.parametrize('container', ['sidebar', 'chat'])
def test_mobile_scroll_only_events(container):
    harness = r'''
const assert=require('node:assert/strict');
class Element {
  constructor(id='', cls='') {
    this.id=id; this.className=cls; this.tagName='DIV'; this.children=[];
    this.handlers={}; this.style={}; this.offsetParent={}; this.isConnected=true;
    this.scrollHeight=1000; this.clientHeight=200; this.scrollTop=0;
    this.scrollWidth=200; this.clientWidth=200; this.scrollLeft=0;
    this.classList={contains:c=>this.className.split(/\s+/).includes(c),
      add:c=>{if(!this.classList.contains(c))this.className+=' '+c;},
      remove:c=>{this.className=this.className.split(/\s+/).filter(v=>v!==c).join(' ');}};
  }
  matches(s) {return s.split(',').some(v=>{v=v.trim();
    return v===':hover' ? !!this.hover : v[0]==='#' ? this.id===v.slice(1) : this.classList.contains(v.slice(1));});}
  closest(s) {return this.matches(s)?this:this.parentElement?.closest(s);}
  appendChild(el) {el.parentElement=this;this.children.push(el);}
  contains(el) {return this===el||this.children.some(c=>c.contains(el));}
  querySelectorAll() {return this.children.flatMap(c=>[c,...c.querySelectorAll()]);}
  setAttribute() {}
  addEventListener(n,fn) {(this.handlers[n]??=[]).push(fn);}
  dispatchEvent(e) {for(const fn of this.handlers[e.type]||[])fn(e);}
  getBoundingClientRect() {return {top:100,bottom:300,left:0,right:200,height:200,width:200};}
  remove() {this.isConnected=false;}
}
let mobile=true, next=0, mutation, now=1000;
Date.now=()=>now;
const timers=new Map(), intervals=[], handlers={};
global.setTimeout=(fn,delay)=>{timers.set(++next,{fn,delay});return next;};
global.clearTimeout=id=>timers.delete(id);
global.setInterval=fn=>intervals.push(fn);
global.requestAnimationFrame=fn=>setTimeout(fn,0);
global.CustomEvent=class {constructor(type){this.type=type;}};
global.MutationObserver=class {constructor(fn){mutation=fn;}observe(){}};
const windowHandlers={};
global.window={matchMedia:()=>({matches:mobile}),addEventListener:(n,fn)=>{windowHandlers[n]=fn;}};
global.getComputedStyle=el=>({overflowY:el.classList.contains('hs-toc')?'auto':'visible',overflowX:'visible'});
global.document={body:new Element(),createElement:()=>new Element(),
  querySelectorAll:()=>[],contains:el=>el.isConnected,
  addEventListener:(n,fn)=>{(handlers[n]??=[]).push(fn);},
  removeEventListener:(n,fn)=>{handlers[n]=(handlers[n]||[]).filter(v=>v!==fn);}};
const emit=(type,target)=>{for(const fn of handlers[type]||[])fn({target,deltaY:1});};
const tick=delay=>{for(const [id,t]of [...timers])if(t.delay===delay){timers.delete(id);t.fn();}};
const panel=new Element('panel-list');panel.hover=true;document.body.appendChild(panel);
const other=new Element('','rp-model-list');document.body.appendChild(other);
const toc=new Element('','hs-toc');document.body.appendChild(toc);
const input=new Element('','hs-inp');document.body.appendChild(input);
let jumps=0;toc.addEventListener('click',()=>jumps++);
'''
    if container == 'chat':
        harness = harness.replace("new Element('panel-list')", "new Element('','hs-body')")
    checks = r'''
const bar=document.body.children.find(t=>t._key===panel);
const visible=()=>bar.classList.contains('sb-show');
assert(bar&&!visible(),'initial registration must remain hidden under sticky hover');
for(const type of ['mouseover','focusin','input','wheel','scroll','desk-scrollbar-refresh']) emit(type,toc);
mutation([{target:toc,type:'attributes'}]);tick(16);
assert(!document.body.children.some(t=>t._key===toc),'TOC must never get an overlay');
toc.scrollTop=70;toc.dispatchEvent({type:'click'});assert.equal(jumps,1);assert.equal(toc.scrollTop,70);
if(panel.classList.contains('hs-body')) {
  emit('focusin',input);
  panel.clientHeight=140;windowHandlers.resize();tick(0);
  mutation([{target:panel,type:'attributes'}]);tick(16);
  panel.scrollTop=10;emit('scroll',panel);tick(0);
  assert(!visible(),'keyboard/focus-induced scroll without user intent stays hidden');
  panel.clientHeight=200;
}
for(const type of ['mouseover','focusin','input','wheel','desk-scrollbar-refresh']) {
  emit(type,panel);assert(!visible(),type+' must not reveal before actual scrolling');
}
panel.dispatchEvent({type:'mouseenter'});assert(!visible());
mutation([{target:panel,type:'attributes'}]);tick(16);assert(!visible(),'DOM updates stay hidden');
emit('touchmove',panel);assert(!visible(),'touchmove alone does not reveal before scroll');
panel.scrollTop=20;emit('scroll',panel);assert(visible(),'actual scroll reveals');
bar.dispatchEvent({type:'mouseenter'});
panel.dispatchEvent({type:'mouseenter'});
emit('mouseover',panel);emit('desk-scrollbar-refresh',panel);
mutation([{target:panel,type:'attributes'}]);tick(16);
tick(800);assert(!visible(),'sticky hover and mutation cannot keep the bar visible');
if(panel.classList.contains('hs-body')) {
  now+=1300;panel.scrollTop=30;emit('scroll',panel);tick(0);
  assert(!visible(),'automatic scroll after user intent expires stays hidden');
  emit('wheel',panel);assert(!visible(),'wheel waits for real scroll');
}
emit('scroll',panel);tick(0);now+=1000;emit('scroll',panel);
assert(visible());tick(800);assert(!visible(),'each scroll restarts idle timeout');
// Existing mouse dragging still keeps the thumb visible until release.
emit('scroll',panel);
const dragStart=panel.scrollTop;
bar.dispatchEvent({type:'mousedown',clientY:20,preventDefault(){},stopPropagation(){}});
tick(800);assert(visible());
for(const fn of handlers.mousemove||[])fn({clientY:60});
assert.equal(panel.scrollTop,dragStart+200);
for(const fn of handlers.mouseup||[])fn({});tick(800);assert(!visible());
// Short/collapsed and closed sidebar geometry cannot paint a stale bar.
panel.scrollHeight=200;emit('scroll',panel);assert(!visible());
panel.scrollHeight=1000;emit('scroll',panel);assert(visible());
panel.offsetParent=null;emit('desk-scrollbar-refresh',panel);assert(!visible());
panel.offsetParent={};emit('desk-scrollbar-refresh',panel);assert(!visible(),'reopen stays hidden');
// Other mobile menus and desktop chat/sidebar retain old behavior.
emit('mouseover',other);
const menuBar=document.body.children.find(t=>t._key===other);
assert(menuBar.classList.contains('sb-show'));tick(800);assert(menuBar.classList.contains('sb-show'));
mobile=false;emit('desk-scrollbar-refresh',panel);tick(800);assert(visible(),'desktop hover stays visible');
emit('desk-scrollbar-refresh',toc);assert(!document.body.children.some(t=>t._key===toc),'desktop TOC stays excluded too');
panel.hover=false;panel.dispatchEvent({type:'mouseleave'});
bar.dispatchEvent({type:'mouseleave'});tick(800);assert(!visible());
panel.remove();intervals.forEach(fn=>fn());assert(!bar.isConnected,'removed sidebar cleans up');
'''
    result = subprocess.run([shutil.which('node'), '-'], input=harness+SCROLLBARS+checks,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.desktop
@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('landscape', [False, True])
@pytest.mark.parametrize('container', ['sidebar', 'chat'])
def test_mobile_idle_and_inertia_in_webkit(theme, landscape, container):
    pw_module = pytest.importorskip('playwright.sync_api')
    with pw_module.sync_playwright() as pw:
        browser = pw.webkit.launch()
        context = browser.new_context(viewport={'width':900 if landscape else 390,
                                                'height':390 if landscape else 844},
                                      has_touch=True)
        page = context.new_page()
        page.set_content('<div id="panel-list" class="'+('hs-body' if container == 'chat' else '')+'" '
                         'style="position:fixed;left:0;top:50px;width:280px;'
                         'height:300px;display:block;overflow:auto"><div style="height:2000px">'
                         '合成滚动内容</div></div>'
                         '<div class="hs-toc" style="position:fixed;left:0;top:60px;width:22px;height:160px;overflow:auto">'
                         '<div style="height:1200px">合成刻度</div></div>'
                         '<div class="hs-inp" contenteditable="true" style="position:fixed;top:400px">合成草稿</div>')
        if container == 'chat':
            page.locator('#panel-list').evaluate('el=>el.id="chat-body"')
        selector = '#chat-body' if container == 'chat' else '#panel-list'
        page.add_style_tag(content=CSS)
        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
        page.add_script_tag(content=SCROLLBARS)
        panel = page.locator(selector)
        page.wait_for_function('(s)=>document.querySelector(s).hasAttribute("data-desk-scrollbar")', arg=selector)
        visible = '''()=>[...document.querySelectorAll('.sb-thumb.sb-show')]
            .some(t=>t._key===document.querySelector('SELECTOR'))'''.replace('SELECTOR', selector)
        assert not page.evaluate(visible)
        assert not page.locator('.hs-toc').evaluate('el=>el.hasAttribute("data-desk-scrollbar")')
        page.locator('.hs-inp').focus()
        panel.evaluate('el=>el.style.height="220px"')
        if container == 'chat':
            panel.evaluate('el=>el.scrollTop=20')
        page.wait_for_timeout(100)
        assert not page.evaluate(visible), 'focus and keyboard-sized layout must not reveal bars'
        panel.evaluate('el=>el.style.height="300px"')
        assert panel.evaluate('el=>getComputedStyle(el).scrollbarWidth') == 'none'
        assert panel.evaluate('el=>getComputedStyle(el,"::-webkit-scrollbar").display') == 'none'
        panel.hover()
        panel.evaluate('el=>el.dispatchEvent(new CustomEvent("desk-scrollbar-refresh",{bubbles:true}))')
        page.wait_for_timeout(80)
        assert not page.evaluate(visible), 'opening or simulated hover cannot reveal the sidebar bar'
        # Gesture plus browser-generated scroll events, including continuing inertia-style movement.
        if container == 'chat':
            panel.evaluate('el=>el.dispatchEvent(new Event("touchmove",{bubbles:true}))')
        for top in [40, 100, 160]:
            panel.evaluate('(el,top)=>el.scrollTop=top', top)
            page.wait_for_function(visible)
            page.wait_for_timeout(300)
        panel.evaluate('el=>{el.classList.add("changed");el.dispatchEvent(new CustomEvent("desk-scrollbar-refresh",{bubbles:true}));}')
        page.wait_for_function('()=>!('+visible+')()')
        assert panel.evaluate('el=>el.scrollTop') == 160
        context.close()
        browser.close()
