"""Nested custom scrollbar frontend regressions."""
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class FrontendScrollbarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8") as f:
            cls.source = f.read()

    def test_local_scrollbars_use_the_macos_desk_shared_style(self):
        with open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8") as f:
            css = f.read()
        self.assertIn('".rp-model-list",', self.source)
        for selector in ('.rp-model-list', '.hs-tt-o', '.hs-tt-c', '.hs-pre', '.hs-diff',
                         '.hs-current-tasks', '.hs-tasks-list'):
            self.assertIn('"' + selector + '"', self.source)
            self.assertRegex(css, re.escape(selector) + r'[^{}]*\{\s*scrollbar-width:\s*none;')
            self.assertRegex(css, re.escape(selector) + r'::-webkit-scrollbar[^{}]*\{\s*display:\s*none;')
        rule = re.search(r'\.sb-thumb\.sb-menu \{([^}]+)\}', css).group(1)
        self.assertIn('z-index: 641', rule)
        self.assertNotIn('background:', rule)
        self.assertNotIn('width:', rule)
        base = re.search(r'\.sb-thumb \{([^}]+)\}', css).group(1)
        self.assertIn('width: 8px', base)
        self.assertIn('border-radius: 4px', base)
        self.assertIn('background: var(--shade-40)', base)
        self.assertIn('.sb-thumb:hover, .sb-thumb.dragging { background: var(--shade-55); }', css)
        self.assertIn('if (menu) t.classList.add("sb-menu")', self.source)

    @unittest.skipUnless(shutil.which("node"), "需要 Node 执行滚动条事件回归")
    def test_model_scrollbar_events_and_menu_drag(self):
        source = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);',
                           self.source, re.S).group(0)
        with open(os.path.join(ROOT, "frontend", "app-panel-terms.js"), encoding="utf-8") as f:
            menu_source = f.read()
        close = re.search(r'  const close = \(e\) => \{.*?\n  \};', menu_source, re.S).group(0)
        harness = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(className='') {
    this.className=className; this.children=[]; this.handlers={}; this.style={};
    this.tagName='DIV'; this.offsetParent={}; this.isConnected=true;
    this.scrollHeight=1000; this.clientHeight=200; this.scrollTop=0;
    this.rect={top:100,right:300,height:200};
    this.classList={
      contains:c=>this.className.split(/\s+/).includes(c),
      add:c=>{if(!this.classList.contains(c)) this.className+=' '+c;},
      remove:c=>{this.className=this.className.split(/\s+/).filter(v=>v!==c).join(' ');}
    };
  }
  matches(s) {return s.split(',').some(v=>this.classList.contains(v.trim().slice(1)));}
  closest(s) {return this.matches(s)?this:this.parentElement?.closest(s);}
  appendChild(el) {el.parentElement=this; this.children.push(el);}
  contains(el) {return el===this || this.children.some(c=>c.contains(el));}
  addEventListener(n,fn) {(this.handlers[n]??=[]).push(fn);}
  dispatchEvent(e) {for(const fn of this.handlers[e.type]||[]) fn(e);}
  getBoundingClientRect() {return this.rect;}
  remove() {this.isConnected=false; if(this.parentElement)
    this.parentElement.children=this.parentElement.children.filter(c=>c!==this);}
}
const timers=new Map(); let timerId=0; const intervals=[];
global.setTimeout=(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;};
global.clearTimeout=id=>timers.delete(id);
global.setInterval=fn=>intervals.push(fn);
global.requestAnimationFrame=fn=>setTimeout(fn,0);
global.CustomEvent=class {constructor(type){this.type=type;}};
const handlers={};
global.getComputedStyle=el=>({overflowY:el.overflowY||'visible'});
global.document={body:new Element(),createElement:()=>new Element(),
  querySelectorAll:()=>[], contains:el=>el.isConnected,
  addEventListener:(n,fn)=>{(handlers[n]??=[]).push(fn);},
  removeEventListener:(n,fn)=>{handlers[n]=(handlers[n]||[]).filter(v=>v!==fn);}};
const emit=(type,e)=>{for(const fn of handlers[type]||[]) fn(e);};
const tick=delay=>{for(const [id,t] of [...timers]) if(t.delay===delay){timers.delete(id);t.fn();}};
const menu=new Element('rp-ctx rp-model'), list=new Element('rp-model-list');
document.body.appendChild(menu); menu.appendChild(list);
'''
        assertions = r'''
emit('desk-scrollbar-refresh',{target:list});
const thumb=document.body.children.find(el=>el.classList.contains('sb-thumb'));
assert(thumb, 'opening the model menu gets an overlay without scrolling');
assert(!thumb.classList.contains('sb-compact') && thumb.classList.contains('sb-menu'));
assert.equal(thumb._width,8);
assert.equal(thumb.style.left,'290px');
assert.equal(thumb.style.height,'40px');
assert(thumb.classList.contains('sb-show'));
tick(800); assert(!thumb.classList.contains('sb-show'), 'idle fades out');
list.dispatchEvent({type:'mouseenter'}); tick(800);
assert(thumb.classList.contains('sb-show'), 'entering the list reveals and keeps the thumb');
emit('scroll',{target:list}); tick(800);
assert(thumb.classList.contains('sb-show'), 'scroll while list hovered does not hide the thumb');
list.dispatchEvent({type:'mouseleave'}); tick(800);
assert(!thumb.classList.contains('sb-show'), 'leaving the list fades out');
emit('scroll',{target:list});
thumb.dispatchEvent({type:'mouseenter'}); tick(800);
assert(thumb.classList.contains('sb-show'), 'hover keeps thumb visible');
thumb.dispatchEvent({type:'mouseleave'}); tick(800);
assert(!thumb.classList.contains('sb-show'));
// 使用生产菜单的关闭判断；body 上的本菜单滑块不能触发关闭。
const m=menu; let drops=0; const drop=()=>drops++;
'''
        final = r'''
close({target:thumb}); assert.equal(drops,0);
const other=new Element('sb-thumb'); other._key=new Element('rp-model-list');
close({target:other}); assert.equal(drops,1, 'other menu thumb remains an outside click');
emit('wheel',{target:list,deltaY:1});
thumb.dispatchEvent({type:'mousedown',clientY:20,preventDefault:()=>{}});
emit('mousemove',{clientY:100}); emit('scroll',{target:list});
assert.equal(list.scrollTop,400, 'drag scrolls the model list');
assert.equal(thumb.style.top,'180px');
tick(800); assert(thumb.classList.contains('sb-show'), 'drag keeps thumb visible');
emit('mouseup',{}); tick(800); assert(!thumb.classList.contains('sb-show'));
list.rect={top:50,right:350,height:200}; emit('scroll',{target:list});
assert.equal(thumb.style.top,'130px'); assert.equal(thumb.style.left,'340px');
list.scrollHeight=list.clientHeight; list.scrollTop=0;
emit('wheel',{target:list,deltaY:1});
assert(!thumb.classList.contains('sb-show'), 'short or collapsed list has no scrollbar');
list.remove(); intervals.forEach(fn=>fn());
assert(!thumb.isConnected, 'closing menu reclaims the thumb');
const alreadyHovered=new Element('rp-model-list hover');
document.body.appendChild(alreadyHovered);
emit('desk-scrollbar-refresh',{target:alreadyHovered});
const newThumb=document.body.children.find(el=>el._key===alreadyHovered);
tick(800);
assert(newThumb.classList.contains('sb-show'), 'opening under the mouse keeps thumb visible');
alreadyHovered.dispatchEvent({type:'mouseleave'}); tick(800);
assert(!newThumb.classList.contains('sb-show'));
// 工具输出等局部容器在首次移入时绑定同款滑块，拖动、淡出及祖先裁剪均可用。
for (const cls of ['hs-tt-o','hs-tt-c','hs-pre','hs-diff','hs-current-tasks','hs-tasks-list']) {
  const outer=new Element('hs-body'), output=new Element(cls);
  outer.overflowY='auto'; outer.rect={top:80,bottom:260,right:400,height:180};
  document.body.appendChild(outer); outer.appendChild(output);
  emit('mouseover',{target:output,relatedTarget:null});
  const bar=document.body.children.find(el=>el._key===output);
  assert(bar && bar.classList.contains('sb-show'), 'first hover shows '+cls);
  assert.equal(bar._width,8); assert(!bar.classList.contains('sb-menu'));
  assert.equal(bar.style.clipPath,'inset(0px 0 0px 0)');
  tick(800); assert(bar.classList.contains('sb-show'));
  bar.dispatchEvent({type:'mousedown',clientY:20,preventDefault:()=>{}});
  emit('mousemove',{clientY:60}); emit('scroll',{target:output});
  assert.equal(output.scrollTop,200); assert.equal(bar.style.top,'140px');
  emit('mouseup',{}); output.dispatchEvent({type:'mouseleave'}); tick(800);
  assert(!bar.classList.contains('sb-show'));
  outer.rect.top=200; emit('scroll',{target:output});
  assert.equal(bar.style.clipPath,'inset(40px 0 0px 0)', 'no painting above clipped ancestor');
  output.offsetParent=null; emit('desk-scrollbar-refresh',{target:output});
  assert(!bar.classList.contains('sb-show'), 'close hides immediately without polling');
  output.offsetParent={}; outer.rect.top=80;
  emit('desk-scrollbar-refresh',{target:output});
  assert(bar.classList.contains('sb-show'), 'reopening shows existing thumb');
  output.scrollHeight=200; emit('scroll',{target:output});
  assert(!bar.classList.contains('sb-show'), 'short output has no thumb');
  output.remove(); intervals.forEach(fn=>fn()); assert(!bar.isConnected);
  outer.remove();
}
'''
        result = subprocess.run([shutil.which("node"), "-"],
                                input=harness + source + assertions + close + final,
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @pytest.mark.desktop
    def test_model_scrollbar_in_windows_chromium(self):
        edge = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
        if os.name != "nt" or not edge.is_file():
            self.skipTest("需要 Windows Edge 隔离浏览器验收")
        import tempfile
        with open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8") as f:
            css = f.read()
        with open(os.path.join(ROOT, "frontend", "app-panel-terms.js"), encoding="utf-8") as f:
            menu = re.search(r'function openCtxMenu\(.*?\n}\n', f.read(), re.S).group(0)
        scrollbars = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);',
                               self.source, re.S).group(0)
        script = r'''
const RP_CHECK='•', RP_CHEV='›';
function escapeHtml(s){return String(s);}
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const assert=(ok,label)=>{if(!ok) throw new Error(label);};
async function run() {
  const items=Array.from({length:40},(_,i)=>({label:'Model '+i,modelRow:true,checked:i===0,fn:()=>{}}));
  items.push({label:'Effort',value:'High',fn:()=>{}});
  openCtxMenu(30,30,items,'rp-wide rp-model'); await pause(100);
  const m=document.querySelector('.rp-model'), list=m.querySelector('.rp-model-list');
  const thumb=document.querySelector('.sb-menu');
  assert(thumb && thumb.classList.contains('sb-show'),'opening shows overlay without wheel or scroll');
  assert(list.scrollTop===0,'initial model needs no programmatic scroll');
  const painted=()=>{
    const style=getComputedStyle(thumb), color=style.backgroundColor;
    assert(color!=='transparent' && color!=='rgba(0, 0, 0, 0)','thumb has a nontransparent computed background');
    assert(parseFloat(style.opacity)>0,'thumb is visibly painted: '+JSON.stringify({opacity:style.opacity,color,classes:thumb.className}));
    return color;
  };
  const darkColor=painted();
  document.documentElement.dataset.theme='light';
  const lightColor=painted();
  assert(darkColor!==lightColor,'thumb color adapts to light theme');
  document.documentElement.removeAttribute('data-theme');
  await pause(1000);
  assert(!thumb.classList.contains('sb-show'),'idle after opening fades out');
  list.dispatchEvent(new MouseEvent('mouseenter'));
  await pause(1000);
  assert(thumb.classList.contains('sb-show'),'list hover reveals and keeps overlay');
  painted();
  list.dispatchEvent(new MouseEvent('mouseleave'));
  await pause(1000);
  assert(!thumb.classList.contains('sb-show'),'leaving list fades out');
  list.dispatchEvent(new WheelEvent('wheel',{deltaY:10,bubbles:true}));
  assert(getComputedStyle(list).scrollbarWidth==='none','native scrollbar hidden');
  assert(list.clientWidth===list.offsetWidth,'no native track consumes content width');
  assert(getComputedStyle(thumb).width==='8px','shared macOS Desk width');
  assert(getComputedStyle(thumb).borderRadius==='4px','shared radius');
  assert(parseInt(getComputedStyle(thumb).zIndex)>parseInt(getComputedStyle(m).zIndex),'thumb above menu');
  const fixed=m.lastElementChild.getBoundingClientRect().top;
  const before=list.scrollTop, rect=thumb.getBoundingClientRect();
  thumb.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  thumb.dispatchEvent(new MouseEvent('mousedown',{clientY:rect.top+5,bubbles:true}));
  document.dispatchEvent(new MouseEvent('mousemove',{clientY:rect.top+55}));
  document.dispatchEvent(new MouseEvent('mouseup'));
  assert(m.isConnected,'drag does not close the menu');
  assert(list.scrollTop>before,'drag scrolls list');
  await pause(1000);
  assert(!thumb.classList.contains('sb-show'),'idle fades out');
  list.scrollTop=list.scrollHeight; await pause(100);
  assert(m.lastElementChild.getBoundingClientRect().top===fixed,'fixed parameters do not scroll');
  openCtxMenu.close(); await pause(450);
  assert(!thumb.isConnected,'close cleans up scrollbar');
  openCtxMenu(30,30,items.slice(0,2),'rp-wide rp-model');
  const short=document.querySelector('.rp-model-list');
  assert(!document.querySelector('.sb-menu.sb-show'),'opening short list has no thumb');
  short.dispatchEvent(new WheelEvent('wheel',{deltaY:10,bubbles:true}));
  assert(!document.querySelector('.sb-menu.sb-show'),'short list has no thumb');
  openCtxMenu.close();
  // 真实 CSS 盒模型：输出没有白色轨道、箭头和宽度占位，样式与共用基准完全一致。
  const host=document.createElement('div'); host.className='hs-page';
  host.style.cssText='position:fixed;left:30px;top:30px;width:360px;height:300px;overflow:auto';
  const box=document.createElement('div'); box.className='hs-term';
  const output=document.createElement('div'); output.className='hs-tt-o';
  output.textContent=Array.from({length:100},(_,i)=>'Output line '+i).join('\n');
  box.appendChild(output); host.appendChild(box); document.body.appendChild(host);
  output.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
  const bar=[...document.querySelectorAll('.sb-thumb')].find(el=>el._key===output);
  assert(bar && bar.classList.contains('sb-show'),'first hover creates output thumb');
  assert(getComputedStyle(output).scrollbarWidth==='none','output native scrollbar hidden');
  assert(output.clientWidth===output.offsetWidth,'output has no native gutter');
  const reference=document.createElement('div'); reference.className='sb-thumb sb-show';
  document.body.appendChild(reference);
  for (const theme of ['', 'light']) {
    document.documentElement.dataset.theme=theme;
    for (const field of ['width','borderRadius','backgroundColor'])
      assert(getComputedStyle(bar)[field]===getComputedStyle(reference)[field],'shared '+field+' '+theme);
    bar.classList.add('dragging'); reference.classList.add('dragging');
    assert(getComputedStyle(bar).backgroundColor===getComputedStyle(reference).backgroundColor,'shared drag color');
    bar.classList.remove('dragging'); reference.classList.remove('dragging');
  }
  const r=bar.getBoundingClientRect();
  bar.dispatchEvent(new MouseEvent('mousedown',{clientY:r.top+5,bubbles:true}));
  document.dispatchEvent(new MouseEvent('mousemove',{clientY:r.top+45}));
  document.dispatchEvent(new MouseEvent('mouseup'));
  assert(output.scrollTop>0,'output thumb drag works');
  output.dispatchEvent(new MouseEvent('mouseleave')); await pause(1000);
  assert(!bar.classList.contains('sb-show'),'output fades after leaving');
  host.remove(); reference.remove(); await pause(450);
  assert(!bar.isConnected,'removed output releases thumb');
  return {nativeHidden:true,width:8,radius:4,opening:true,listHover:true,darkColor,lightColor,
    drag:true,autoHide:true,fixedParameters:true,shortList:true,toolOutput:true};
}
run().then(result=>document.getElementById('result').textContent=JSON.stringify({ok:true,...result}))
  .catch(error=>document.getElementById('result').textContent=JSON.stringify({error:String(error)}));
'''
        self._run_edge_scrollbar_page(css, menu + scrollbars + script)

    def test_dynamic_registry_and_horizontal_style_contract(self):
        css = (Path(ROOT) / 'frontend/style.css').read_text(encoding='utf-8')
        for token in ('function scrollable(key)', 'css.overflowY', 'css.overflowX',
                      'new MutationObserver', 'record.addedNodes', 'layoutHorizontal',
                      'data-desk-scrollbar', 'function clip(', 'function layer('):
            self.assertIn(token, self.source)
        self.assertIn('[data-desk-scrollbar] { scrollbar-width: none !important;', css)
        self.assertIn('[data-desk-scrollbar]::-webkit-scrollbar { display: none !important; }', css)
        self.assertIn('.sb-thumb.sb-horizontal { height: 8px; width: auto; }', css)
        self.assertNotIn('sb-compact', self.source)
        memo = (Path(ROOT) / 'frontend/app-panel-task-memo.js').read_text(encoding='utf-8')
        self.assertIn('sheet.contains(thumb._key)', memo)

    @pytest.mark.desktop
    def test_global_scrollbar_registry_in_windows_chromium(self):
        edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
        if os.name != 'nt' or not edge.is_file():
            self.skipTest('需要 Windows Edge 隔离浏览器验收')
        css = (Path(ROOT) / 'frontend/style.css').read_text(encoding='utf-8')
        tasks = (Path(ROOT) / 'frontend/app-chat-tasksbar.js').read_text(encoding='utf-8')
        outside = re.search(r'function hsTasksOutside\(.*?\n}', tasks, re.S).group(0)
        scrollbars = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);', self.source, re.S).group(0)
        script = r'''
const _hsTasksAway=new WeakMap(); let closes=0;
function hsContextClose(v) {closes++; v.pop.hidden=true;}
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const assert=(ok,label)=>{if(!ok) throw new Error(label);};
const barOf=(key,axis='y')=>[...document.querySelectorAll('.sb-thumb')]
  .find(t=>t._key===key && t.classList.contains('sb-horizontal')===(axis==='x'));
async function run() {
  // 截图中的状态错误详情：高层级浮层、原生白轨道、关闭与重新打开。
  const page=document.createElement('div'); page.className='hs-page';
  page.style.cssText='position:fixed;inset:0;display:block';
  page.innerHTML='<div class="hs-context" style="position:absolute;left:40px;top:40px;width:340px;z-index:2500">'
    +'<div class="hs-context-popover" style="display:block;position:relative;bottom:auto">'
    +'<div class="hs-context-status-detail"></div></div></div>';
  document.body.appendChild(page);
  const detail=page.querySelector('.hs-context-status-detail'), pop=detail.parentElement;
  detail.textContent=Array.from({length:80},(_,i)=>'Error detail '+i).join('\n');
  const v={el:page,pop}; hsTasksOutside(v,true);
  await pause(100);
  const bar=barOf(detail);
  assert(bar?.classList.contains('sb-show'),'status detail registered without scrolling '+JSON.stringify({bar:bar?.className,marker:detail.hasAttribute('data-desk-scrollbar'),height:detail.clientHeight,scroll:detail.scrollHeight,offset:!!detail.offsetParent,all:[...document.querySelectorAll('.sb-thumb')].map(t=>t._key?.className)}));
  assert(getComputedStyle(detail).scrollbarWidth==='none','status native track disabled');
  assert(getComputedStyle(detail,'::-webkit-scrollbar').display==='none','status arrows/track disabled');
  const br=bar.getBoundingClientRect();
  assert(document.elementFromPoint(br.left+4,br.top+5)===bar,'status bar paints above its z=2500 card');
  bar.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  bar.dispatchEvent(new MouseEvent('mousedown',{clientY:br.top+5,bubbles:true}));
  document.dispatchEvent(new MouseEvent('mousemove',{clientY:br.top+35}));
  document.dispatchEvent(new MouseEvent('mouseup'));
  assert(closes===0 && !pop.hidden && detail.scrollTop>0,'drag keeps status card open');
  pop.hidden=true; pop.style.display='none'; await pause(100);
  assert(!bar.classList.contains('sb-show'),'hidden status card has no orphan thumb');
  pop.hidden=false; pop.style.display='block'; await pause(100);
  assert(barOf(detail)===bar && bar.classList.contains('sb-show'),'reopening reuses status thumb');
  hsTasksOutside(v,false); page.remove(); await pause(450);
  assert(!bar.isConnected,'status thumb reclaimed');
  // 各业务入口，以及不在选择器名单中的新容器和 textarea，均由 overflow 自动注册。
  const names=['resource-scroll','sm-inner-nav','sm-engine-dialog-body','setup-workspace',
    'fb-tree-body','fb-results','hs-rv-files','hs-rv-body','hs-lr-body','wf-sheet-body',
    'ri-list','arr-sys-logs','arr-result-full',
    'appr-notice-body','hs-execution-body','audit-new-inline-container'];
  const host=document.createElement('div');
  host.style.cssText='position:fixed;left:40px;top:40px;width:300px;height:180px;overflow:hidden';
  document.body.appendChild(host);
  for (const name of names) {
    const box=document.createElement('div'); box.className=name;
    box.style.cssText='display:block;position:relative;width:260px;height:120px;min-height:0;max-height:120px;overflow:auto;margin:0;padding:0';
    box.innerHTML='<div style="height:1200px">Synthetic long content</div>'; host.appendChild(box);
    await pause(50);
    const thumb=barOf(box);
    assert(thumb?.classList.contains('sb-show'),'automatic registration: '+name);
    assert(getComputedStyle(box).scrollbarWidth==='none','native hidden: '+name);
    assert(thumb.style.height && parseFloat(thumb.style.height)<=box.clientHeight,'thumb does not exceed track');
    for (const theme of ['', 'light']) {
      document.documentElement.dataset.theme=theme;
      assert(getComputedStyle(thumb).width==='8px' && getComputedStyle(thumb).borderRadius==='4px','shared geometry '+name);
      const ref=document.createElement('div'); ref.className='sb-thumb'; document.body.appendChild(ref);
      assert(getComputedStyle(thumb).backgroundColor===getComputedStyle(ref).backgroundColor,'shared theme '+name);
      thumb.classList.add('dragging'); ref.classList.add('dragging');
      assert(getComputedStyle(thumb).backgroundColor===getComputedStyle(ref).backgroundColor,'shared drag '+name);
      thumb.classList.remove('dragging'); ref.remove();
    }
    box.remove();
  }
  document.documentElement.removeAttribute('data-theme');
  const input=document.createElement('textarea');
  input.style.cssText='width:260px;height:120px;overflow:auto';
  input.value=Array.from({length:60},()=> 'Long input').join('\n'); host.appendChild(input); await pause(100);
  assert(barOf(input)?.classList.contains('sb-show') && getComputedStyle(input).scrollbarWidth==='none','textarea auto registered');
  input.remove();
  // 横向代码、表格等同款 8px 覆盖条；拖动、短内容、祖先裁剪和鼠标离开均有效。
  const horizontal=document.createElement('div'); horizontal.className='audit-horizontal';
  horizontal.style.cssText='width:260px;height:100px;overflow:auto;white-space:nowrap';
  horizontal.innerHTML='<div style="width:1400px;height:40px">A very wide line</div>';
  host.appendChild(horizontal); await pause(100);
  const x=barOf(horizontal,'x');
  assert(x?.classList.contains('sb-show') && !barOf(horizontal)?.classList.contains('sb-show'),'horizontal only');
  assert(getComputedStyle(x).height==='8px' && getComputedStyle(x).borderRadius==='4px','horizontal shared style');
  const xr=x.getBoundingClientRect();
  x.dispatchEvent(new MouseEvent('mousedown',{clientX:xr.left+5,bubbles:true}));
  document.dispatchEvent(new MouseEvent('mousemove',{clientX:xr.left+55}));
  document.dispatchEvent(new MouseEvent('mouseup')); await pause(50);
  assert(horizontal.scrollLeft>0,'horizontal drag scrolls content');
  horizontal.dispatchEvent(new MouseEvent('mouseenter')); await pause(1000);
  assert(x.classList.contains('sb-show'),'horizontal stays while hovered');
  horizontal.dispatchEvent(new MouseEvent('mouseleave')); await pause(1000);
  assert(!x.classList.contains('sb-show'),'horizontal idle fades');
  horizontal.innerHTML='<div style="height:40px">Short line</div>'; await pause(100);
  assert(!x.classList.contains('sb-show'),'short horizontal content hides thumb');
  horizontal.innerHTML='<div style="width:1400px;height:40px">Wide again</div>'; await pause(100);
  assert(x.classList.contains('sb-show'),'dynamic horizontal growth reveals');
  host.style.height='50px'; horizontal.dispatchEvent(new Event('scroll')); await pause(50);
  assert(parseFloat(x.style.clipPath.match(/inset\([^ ]+ [^ ]+ ([0-9.]+)/)[1])>0,'horizontal clipped by ancestor');
  host.remove(); await pause(450);
  assert(!x.isConnected,'horizontal reclaimed');
  return {status:true,autoContainers:names.length,textarea:true,horizontal:true,theme:true,layer:true,cleanup:true};
}
run().then(result=>document.getElementById('result').textContent=JSON.stringify({ok:true,...result}))
  .catch(error=>document.getElementById('result').textContent=JSON.stringify({error:String(error)}));
'''
        self._run_edge_scrollbar_page(css, outside + scrollbars + script)

    def test_composer_send_tooltip_stays_inside_scroll_row(self):
        css = (Path(ROOT) / 'frontend/style.css').read_text(encoding='utf-8')
        rule = re.search(r'\.hs-page \.hs-row \.hs-send::after \{([^}]+)\}', css)
        self.assertIsNotNone(rule, 'send tooltip must not extend the scrollable controls row')
        for declaration in ('left: auto', 'right: 0', 'transform: none'):
            self.assertIn(declaration, rule.group(1))

    @pytest.mark.desktop
    def test_composer_hover_has_no_phantom_horizontal_scrollbar(self):
        edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
        if os.name != 'nt' or not edge.is_file():
            self.skipTest('需要 Windows Edge 隔离浏览器验收')
        css = (Path(ROOT) / 'frontend/style.css').read_text(encoding='utf-8')
        scrollbars = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);', self.source, re.S).group(0)
        script = r'''
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const assert=(ok,label)=>{if(!ok) throw new Error(label);};
const barOf=key=>[...document.querySelectorAll('.sb-horizontal')].find(t=>t._key===key);
async function run() {
  const page=document.createElement('div'); page.className='hs-page';
  page.style.cssText='position:fixed;inset:0;display:block';
  // Keep production data-tip attributes: even an invisible pseudo-element contributes to scrollWidth.
  page.innerHTML='<div class="hs-box hs-col" style="margin:30px">'
    +'<div class="hs-inp" contenteditable="true" data-ph="写点什么…"></div><div class="hs-row">'
    +'<button class="hs-clip" data-tip="附件"><svg width="16" height="16"></svg></button>'
    +'<input class="hs-file" type="file" hidden>'
    +'<span class="hs-mode"><span class="hs-composer-icon is-lock"></span><span>bypass permissions mode</span></span>'
    +'<span class="hs-sandbox" hidden>Full access</span><span class="hs-agent" hidden></span>'
    +'<span class="hs-bash" hidden></span><span class="hs-sp"></span>'
    +'<span class="hs-model"><span class="hs-m1">Pi Coding · GPT-6.1 Synthetic</span><span class="hs-m2">High</span></span>'
    +'<button class="hs-send" data-tip="发送"></button></div></div>';
  document.body.appendChild(page);
  const box=page.querySelector('.hs-box'), row=page.querySelector('.hs-row'), send=page.querySelector('.hs-send');
  const hover=()=>row.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
  const refresh=()=>row.dispatchEvent(new CustomEvent('desk-scrollbar-refresh',{bubbles:true}));
  for (const theme of ['', 'light']) {
    document.documentElement.dataset.theme=theme;
    for (const width of [760, 759.5, 600]) {
      box.style.width=width+'px'; row.scrollLeft=0; await pause(60); hover();
      assert(row.scrollWidth<=row.clientWidth+1,'wide composer must fit, not count hidden send tooltip: '+JSON.stringify({theme,width,client:row.clientWidth,scroll:row.scrollWidth}));
      assert(!barOf(row)?.classList.contains('sb-show'),'hover must not show phantom horizontal thumb');
      const tip=getComputedStyle(send,'::after');
      assert(tip.right==='0px' && tip.transform==='none','tooltip aligned inward');
    }
    // 底栏不再显示覆盖条，但保留真实横向滚动，不裁掉宽标签。
    box.style.width='300px'; await pause(60); refresh(); hover();
    assert(row.scrollWidth>row.clientWidth+1 && !barOf(row),'narrow row scrolls without a thumb');
    row.scrollLeft=80; row.dispatchEvent(new Event('scroll')); await pause(30);
    assert(row.scrollLeft>0 && !barOf(row),'scrolling must not recreate the controls thumb');
    const sr=send.getBoundingClientRect(), rr=row.getBoundingClientRect();
    assert(sr.left>=rr.left-1 && sr.right<=rr.right+1,'send remains visible while controls scroll');
    box.style.width='760px'; await pause(60); refresh(); hover();
    assert(!barOf(row),'widening keeps the controls scrollbar hidden');
  }
  page.remove(); await pause(450);
  assert(!barOf(row),'removed composer reclaims its thumb');
  return {wideFits:true,hover:true,narrowScroll:true,noControlsThumb:true,sendVisible:true,resize:true,themes:true,cleanup:true};
}
run().then(result=>document.getElementById('result').textContent=JSON.stringify({ok:true,...result}))
  .catch(error=>document.getElementById('result').textContent=JSON.stringify({error:String(error)}));
'''
        self._run_edge_scrollbar_page(css, scrollbars + script)

    @pytest.mark.desktop
    def test_same_origin_frames_navigation_and_short_tracks(self):
        edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
        if os.name != 'nt' or not edge.is_file():
            self.skipTest('需要 Windows Edge 隔离浏览器验收')
        css = (Path(ROOT) / 'frontend/style.css').read_text(encoding='utf-8')
        scrollbars = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);', self.source, re.S).group(0)
        script = r'''
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const assert=(ok,label)=>{if(!ok) throw new Error(label);};
const barOf=(key,axis='y')=>[...document.querySelectorAll('.sb-thumb')]
  .find(t=>t._key===key && t.classList.contains('sb-horizontal')===(axis==='x'));
async function run() {
  const tiny=document.createElement('div');
  tiny.style.cssText='position:fixed;left:30px;top:30px;width:100px;height:12px;overflow:auto';
  tiny.innerHTML='<div style="height:300px">Tiny viewport</div>'; document.body.appendChild(tiny);
  await pause(100);
  assert(parseFloat(barOf(tiny).style.height)<=12,'minimum thumb fits short viewport'); tiny.remove();
  const frame=document.createElement('iframe');
  frame.style.cssText='position:fixed;left:60px;top:60px;width:320px;height:240px;border:0';
  const content=id=>'<style>body{margin:0}#'+id+'{width:200px;height:80px;overflow:auto}</style>'
    +'<div id="'+id+'"><div style="height:600px;width:700px">Synthetic frame content</div></div>'
    +'<div style="height:500px">Root scroll content</div>';
  frame.srcdoc=content('first'); document.body.appendChild(frame); await pause(650);
  const first=frame.contentDocument.getElementById('first'), root=barOf(frame);
  assert(root?.classList.contains('sb-show'),'frame document root registered');
  const bar=barOf(first), x=barOf(first,'x');
  assert(bar?.classList.contains('sb-show') && x?.classList.contains('sb-show'),'both frame element axes registered');
  assert(frame.contentWindow.getComputedStyle(first).scrollbarWidth==='none','iframe native scrollbar hidden');
  const br=bar.getBoundingClientRect(), fr=frame.getBoundingClientRect();
  bar.dispatchEvent(new MouseEvent('mousedown',{clientY:br.top+5,bubbles:true}));
  frame.contentDocument.dispatchEvent(new MouseEvent('mousemove',{clientY:br.top+35-fr.top}));
  frame.contentDocument.dispatchEvent(new MouseEvent('mouseup'));
  assert(first.scrollTop>0 && !bar.classList.contains('dragging'),'drag enters iframe and releases correctly');
  first.scrollTop=120; first.dispatchEvent(new Event('scroll')); await pause(50);
  assert(parseFloat(bar.style.top)>60,'frame offset applied to thumb');
  frame.srcdoc=content('second'); await pause(650);
  const second=frame.contentDocument.getElementById('second');
  assert(second && barOf(second)?.classList.contains('sb-show'),'navigation binds new Document');
  assert(!bar.isConnected && !x.isConnected,'navigation reclaims old element bars');
  second.scrollTop=140; second.dispatchEvent(new Event('scroll'));
  assert(barOf(second).classList.contains('sb-show'),'new Document receives scroll events');
  frame.remove(); await pause(450);
  assert(!root.isConnected && !barOf(second),'frame removal reclaims every axis');
  const opaque=document.createElement('iframe');
  opaque.src='data:text/html,<div style="height:900px">Opaque origin</div>';
  opaque.style.cssText='width:200px;height:80px'; document.body.appendChild(opaque); await pause(650);
  assert(opaque.contentDocument===null && !barOf(opaque),'opaque origin not injected or claimed');
  opaque.remove();
  return {tiny:true,frameRoot:true,frameAxes:true,navigation:true,cleanup:true,opaqueUntouched:true};
}
run().then(result=>document.getElementById('result').textContent=JSON.stringify({ok:true,...result}))
  .catch(error=>document.getElementById('result').textContent=JSON.stringify({error:String(error)}));
'''
        self._run_edge_scrollbar_page(css, scrollbars + script)

    def _run_edge_scrollbar_page(self, css, script):
        import tempfile
        edge = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
        with tempfile.TemporaryDirectory(prefix="desk-scrollbar-") as directory:
            folder = Path(directory)
            page = folder / "menu.html"
            # Chromium 的虚拟时钟不保证合成动画推进；只禁用过渡，仍检查真实显隐与计算色。
            page.write_text('<!doctype html><meta charset="utf-8"><style>' + css
                            + '.sb-thumb{transition:none}</style><body><pre id="result">pending</pre><script>'
                            + script + '</script></body>', encoding="utf-8")
            result = subprocess.run([str(edge), "--headless=new", "--disable-gpu",
                                     "--no-first-run", "--disable-background-networking", "--window-size=1000,900",
                                     "--disable-extensions", "--user-data-dir=" + str(folder / "profile"),
                                     "--virtual-time-budget=10000", "--dump-dom", page.as_uri()],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
            self.assertIsNotNone(match, result.stdout[-1000:])
            evidence = json.loads(html.unescape(match.group(1)))
            self.assertTrue(evidence.get("ok"), evidence)

    @pytest.mark.desktop
    def test_tasks_popover_scrollbars_in_windows_chromium(self):
        edge = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
        if os.name != "nt" or not edge.is_file():
            self.skipTest("需要 Windows Edge 隔离浏览器验收")
        css = (Path(ROOT) / 'frontend/style.css').read_text(encoding='utf-8')
        tasks = (Path(ROOT) / 'frontend/app-chat-tasksbar.js').read_text(encoding='utf-8')
        production = '\n'.join(re.search(r'function ' + name + r'\(.*?\n}', tasks, re.S).group(0)
                               for name in ('hsTasksOutside', 'hsTasksPaint'))
        scrollbars = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);',
                               self.source, re.S).group(0)
        script = r'''
const _hsTasksAway=new WeakMap();
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const assert=(ok,label)=>{if(!ok) throw new Error(label);};
let outsideCloses=0;
function hsContextClose(v) {outsideCloses++; hsTasksPaint(v,false);}
async function run() {
  const el=document.createElement('div'); el.className='hs-page';
  el.style.cssText='position:fixed;inset:0;display:block';
  el.innerHTML='<div class="hs-context" style="position:absolute;left:40px;bottom:40px;width:330px">'
    +'<div class="hs-tasks"><button class="hs-tasks-bar">Tasks</button>'
    +'<div class="hs-context-popover hs-tasks-popover" hidden>'
    +'<div class="hs-current-task-section"><div class="hs-current-tasks-title">Current tasks</div>'
    +'<div class="hs-current-tasks"></div></div><div class="hs-tasks-sum">Backlog</div>'
    +'<div class="hs-tasks-list"></div><input class="hs-tasks-add" placeholder="New task">'
    +'</div></div></div>';
  document.body.appendChild(el);
  const v={el}, pop=el.querySelector('.hs-tasks-popover');
  const lists=[...el.querySelectorAll('.hs-current-tasks,.hs-tasks-list')];
  for (const [i,list] of lists.entries()) {
    list.innerHTML=Array.from({length:20},(_,j)=>i
      ?'<div class="hs-task"><span class="hs-task-id">T'+j+'</span><span class="hs-task-t">Task '+j+'</span></div>'
      :'<button class="hs-current-task"><span class="hs-current-task-title">Task '+j+'</span>'
        +'<span class="hs-current-task-meta">T'+j+' · Doing</span></button>').join('');
  }
  const barOf=list=>[...document.querySelectorAll('.sb-thumb')].find(t=>t._key===list);
  hsTasksPaint(v,true); hsTasksOutside(v,true);
  for (const list of lists) {
    const bar=barOf(list);
    assert(bar && bar.classList.contains('sb-show'),'opening reveals both task list thumbs');
    assert(getComputedStyle(list).scrollbarWidth==='none','no native task scrollbar');
    assert(getComputedStyle(list,'::-webkit-scrollbar').display==='none','no native track or arrow');
    assert(list.offsetWidth===list.clientWidth,'no native gutter');
    const br=bar.getBoundingClientRect(), lr=list.getBoundingClientRect();
    assert(Math.abs(br.right-(lr.right-2))<0.1,'thumb occupies reserved right rail');
    const hit=document.elementFromPoint(br.left+4,br.top+5);
    assert(hit===bar,'thumb is above card and receives pointer: '+JSON.stringify({rect:br.toJSON(),hit:hit?.className}));
  }
  const reference=document.createElement('div'); reference.className='sb-thumb';
  document.body.appendChild(reference);
  for (const theme of ['', 'light']) {
    document.documentElement.dataset.theme=theme;
    for (const list of lists) for (const field of ['width','borderRadius','backgroundColor'])
      assert(getComputedStyle(barOf(list))[field]===getComputedStyle(reference)[field],'shared '+field+' '+theme);
  }
  document.documentElement.removeAttribute('data-theme'); reference.remove();
  // 拖动两个独立列表，不触发生产代码的卡片外点击关闭，不滚动另一段或底部输入框。
  const inputTop=pop.querySelector('input').getBoundingClientRect().top;
  for (const [i,list] of lists.entries()) {
    const bar=barOf(list), br=bar.getBoundingClientRect(), other=lists[1-i], before=other.scrollTop;
    bar.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
    bar.dispatchEvent(new MouseEvent('mousedown',{clientY:br.top+5,bubbles:true}));
    document.dispatchEvent(new MouseEvent('mousemove',{clientY:br.top+50}));
    document.dispatchEvent(new MouseEvent('mouseup'));
    assert(!pop.hidden && outsideCloses===0,'drag does not close task popover');
    assert(list.scrollTop>0 && other.scrollTop===before,'task lists scroll independently');
    assert(pop.querySelector('input').getBoundingClientRect().top===inputTop,'input stays fixed');
  }
  // 卡片外仍可关闭；关闭立即隐藏，不等400ms轮询。
  document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  assert(pop.hidden && outsideCloses===1,'outside pointer closes normally');
  assert(lists.every(l=>!barOf(l).classList.contains('sb-show')),'close immediately hides both thumbs');
  hsTasksPaint(v,true); hsTasksOutside(v,true);
  assert(lists.every(l=>barOf(l).classList.contains('sb-show')),'reopening reveals reused thumbs');
  lists[0].dispatchEvent(new MouseEvent('mouseenter')); await pause(1000);
  assert(barOf(lists[0]).classList.contains('sb-show'),'list hover keeps thumb visible');
  lists[0].dispatchEvent(new MouseEvent('mouseleave')); await pause(1000);
  assert(!barOf(lists[0]).classList.contains('sb-show'),'leaving fades out');
  lists[0].innerHTML='<button class="hs-current-task">Only one task</button>';
  hsTasksPaint(v,true);
  assert(!barOf(lists[0]).classList.contains('sb-show'),'shrinking to short list hides thumb');
  // 从短内容增长，无需wheel或scroll即可重新显示。
  lists[0].innerHTML=Array.from({length:20},()=>'<button class="hs-current-task">Task</button>').join('');
  hsTasksPaint(v,true);
  assert(barOf(lists[0]).classList.contains('sb-show'),'data refresh reveals grown list');
  hsTasksPaint(v,false); hsTasksOutside(v,false); el.remove(); await pause(450);
  assert(lists.every(l=>!barOf(l)),'removed card releases thumbs');
  return {tasks:true,backlog:true,nativeHidden:true,independentDrag:true,close:true,
    reopen:true,hover:true,shortList:true,dynamicGrowth:true,sharedTheme:true};
}
run().then(result=>document.getElementById('result').textContent=JSON.stringify({ok:true,...result}))
  .catch(error=>document.getElementById('result').textContent=JSON.stringify({error:String(error)}));
'''
        self._run_edge_scrollbar_page(css, production + scrollbars + script)

    def test_outer_scroll_reflows_visible_fixed_thumbs(self):
        scroll = re.search(
            r'document\.addEventListener\("scroll".*?\}, true\);', self.source, re.S
        ).group(0)

        self.assertIn("reflowShownThumbs()", scroll)
        self.assertIn('t.classList.contains("sb-show")', self.source)

    def test_wheel_chain_hides_inner_thumb_at_boundary(self):
        wheel = re.search(
            r'document\.addEventListener\("wheel".*?passive: true \}\);', self.source, re.S
        ).group(0)

        self.assertIn("wheelTarget(inner, e.deltaY)", wheel)
        self.assertIn("target !== inner", wheel)
        self.assertIn('stale.classList.remove("sb-show")', wheel)


if __name__ == "__main__":
    unittest.main()
