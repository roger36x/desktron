"""Windows reading is lighter than the Codex reference without shrinking text.

Keep the Segoe UI/system CJK fallback, 14px content and 20px composer line height.
UI and reading weight 350 replaces 430; emphasis and explicit component weights
remain unchanged. Windows-only overrides preserve other platforms and terminals.
"""
from pathlib import Path
import html
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
STYLE = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
INDEX = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
TERM = (ROOT / 'frontend/term-embed.js').read_text(encoding='utf-8')
CORE = (ROOT / 'frontend/app-core.js').read_text(encoding='utf-8')
DETAIL = (ROOT / 'frontend/app-chat-detail.js').read_text(encoding='utf-8')
HAN_PATCH = CORE[CORE.index('function mdWithHanParagraphs('):CORE.index('\nconst md = window.markdownit')]
VENDOR = (ROOT / 'frontend/vendor/markdown-it.min.js').read_text(encoding='utf-8')
LITE = DETAIL[DETAIL.index('function hsLiteHtml('):DETAIL.index('\n}\n', DETAIL.index('function hsLiteHtml(')) + 2]


def _block(selector):
    start = STYLE.index(selector + ' {')
    return STYLE[start:STYLE.index('}', start)]


def test_windows_mark_is_set_by_operating_system_before_styles():
    mark = INDEX.index('classList.add("os-windows")')
    assert mark < INDEX.index('<link rel="stylesheet" href="/style.css"')
    script = INDEX[INDEX.rindex('<script>', 0, mark):mark]
    assert 'navigator.userAgentData' in script and 'navigator.userAgent' in script
    assert '/Windows/i.test(os)' in script
    # Browsers on Windows use the same system fonts, so the shell is not the criterion.
    assert 'messageHandlers' not in script[script.index('const os'):]


def test_macos_font_stack_is_unchanged_and_windows_stack_is_scoped():
    root = _block(':root')
    assert ('--font-family-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, '
            '"Segoe UI", sans-serif;') in root
    assert '--tracking-tight' not in root
    win = _block('html.os-windows')
    assert '--font-family-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;' in win
    assert 'Desk UI CJK' not in win and 'Segoe UI Variable Text' not in win
    assert 'font-synthesis-weight: none;' in win
    assert '--tracking-tight: 0;' in win


def test_windows_uses_system_cjk_fallback_without_weight_remapping():
    assert 'font-family: "Desk UI CJK"' not in STYLE
    assert 'font-synthesis-weight: none;' in _block('html.os-windows')
    assert '--windows-ui-weight: 350;' in _block('html.os-windows')
    # CJK monospace alignment is a separate contract and remains present.
    assert 'font-family: "Desk Mono CJK"' in STYLE


def test_negative_tracking_is_neutralised_only_on_windows():
    assert not re.search(r'letter-spacing:\s*-', STYLE)
    scaled = re.findall(r'letter-spacing: calc\((-[\d.]+(?:em|px)) \* var\(--tracking-tight, 1\)\)', STYLE)
    # Timeline 的详情标题已删除，其余负字距仍须由 Windows 开关控制。
    assert len(scaled) >= 19
    # Positive tracking (small caps labels) keeps its value on every platform.
    assert 'letter-spacing: .5px' in STYLE


def test_mono_cjk_alignment_falls_back_to_yahei_on_windows():
    order = ['local("PingFang SC")', 'local("Microsoft YaHei UI")']
    mono = STYLE[STYLE.index('font-family: "Desk Mono CJK"'):]
    mono = mono[:mono.index('}')]
    assert mono.index(order[0]) < mono.index(order[1]) and 'size-adjust: 120%;' in mono
    term = TERM[TERM.index('new FontFace("Desk Mono CJK"'):]
    term = term[:term.index('\n')]
    assert term.index(order[0]) < term.index(order[1])


def test_windows_reading_parameters_are_scoped_to_chat_not_global_tokens():
    win = _block('html.os-windows')
    for token, value in {'size': '14px', 'weight': '350', 'tracking': '0',
                         'leading': '1.625', 'paragraph': '14px'}.items():
        assert f'--chat-read-{token}: {value};' in win
    assert '--font-size-base: 14px;' in _block(':root')
    assert '--font-weight-medium: 450;' in _block(':root')
    assert '--line-height-large: 150%;' in _block(':root')
    # Every use of a reading token must remain in an OS-scoped rule.
    for selectors, body in re.findall(r'([^{}]+)\{([^{}]+)\}', STYLE):
        if 'var(--chat-read-' in body:
            selectors = re.sub(r'/\*.*?\*/', '', selectors, flags=re.S).strip()
            assert selectors.startswith('html.os-windows .hs-page')


def test_han_paragraphs_use_rendered_text_and_preserve_renderer_chain():
    node = shutil.which('node')
    if not node:
        pytest.skip('需要 Node 验证真实 Markdown 渲染器')
    cases = ['中文 **强调**', 'English [link](https://example.test/中文)',
             'English ![中文](https://example.test/image)', 'かな 한글',
             '`中文`', '\U00020000', '[中文](https://example.test)', 'English &#20013;']
    script = VENDOR + '\n' + HAN_PATCH + '\n' + LITE + '\n' + '''
const renderer = mdWithHanParagraphs(module.exports({html:false}));
const cases = CASES;
const got = cases.map(text=>renderer.render(text).includes('data-markdown-han-text="true"'));
const custom = module.exports();
custom.renderer.rules.paragraph_open=(tokens,idx,opts,env,self)=>{
  tokens[idx].attrSet('class','kept');return self.renderToken(tokens,idx,opts);
};
const html=mdWithHanParagraphs(custom).render('中文');
const lite=hsLiteHtml('中文 `片段`\\n\\n第二段中文').match(/data-markdown-han-text="true"/g).length===2
  && !hsLiteHtml('English').includes('data-markdown-han-text');
console.log(JSON.stringify({got,lite,custom:html.includes('class="kept"')&&html.includes('data-markdown-han-text')}));
'''.replace('CASES', json.dumps(cases, ensure_ascii=False))
    result = subprocess.run([node, '-'], input=script, capture_output=True, text=True,
                            encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence == {'got': [True, False, False, False, True, True, True, True], 'custom': True, 'lite': True}
    assert 'mdWithHanParagraphs(md);' in CORE
    assert 'mdWithHanParagraphs(window.markdownit(' in DETAIL


def test_project_font_and_conditional_spacing_are_windows_scoped():
    assert 'font-size: 14px;' in _block('html.os-windows #panel :is(.proj-fold .pf-name, .subarea-fold .pf-name)')
    assert 'data-markdown-han-text' in STYLE
    selector = 'html.os-windows .hs-page :is(.hs-text:not(.hs-lead), .hs-u-body, .hs-execution-body).md > p[data-markdown-han-text]:has(+ p[data-markdown-han-text])'
    assert 'margin-bottom: 4px;' in _block(selector)


@pytest.mark.parametrize(('selector', 'weight'), [
    ('.rp-ctx .rp-ctx-head .rp-value', '400'),
    ('.side-brand .brand', '600'),
    ('#term-head', '600'),
    ('.sm-group-title', '700'),
    ('.settings-center-live .sm-section > .sm-title', '620'),
    ('.auth-tui-terminal .auth-tui-title strong', '560'),
    ('.hs-page .hs-current-task-title', '560'),
    ('.hs-page .hs-step-n', '600'),
    ('.hs-page .hs-peer-label', '450'),
    ('.resource-center .ap-create', '450'),
    ('.tm-window-title', '560'),
    ('.hs-sit-project-add', '400'),
    ('.fb-edit-ta', '400'),
])
def test_windows_shared_components_keep_baseline_weights(selector, weight):
    rules = re.findall(r'([^{}]+)\{([^{}]*)\}', re.sub(r'/\*.*?\*/', '', STYLE, flags=re.S))
    scoped = ':where(html.os-windows) ' + selector
    matching = [body for selectors, body in rules if scoped in [s.strip() for s in selectors.split(',')]]
    assert matching and f'font-weight: {weight};' in matching[-1]
    assert all('font-size:' not in body and 'line-height:' not in body for body in matching)


@pytest.mark.desktop
def test_windows_reading_in_edge_preserves_code_controls_and_other_platforms():
    edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if os.name != 'nt' or not edge.is_file():
        pytest.skip('需要 Windows Edge 隔离浏览器验收')
    fixture = '''<div class="hs-page"><div class="hs-stream">
<div id="answer" class="hs-text md"><h1>标题</h1><h2>标题</h2><h3>标题</h3><h4>标题</h4>
<h5>五级标题</h5><h6>六级标题</h6>
<p id="paragraph">中文与 Windows 混排 <strong id="bold">强调</strong> <b id="b">粗体</b> <code id="inline">x = 1</code></p>
<ul><li id="item"><p id="listP">列表段落</p></li></ul>
<pre id="code"><code>console.log("中文")</code></pre><p id="last">末段</p></div>
<div id="stream" class="hs-text">尚未落地的流式文本</div>
<div class="hs-bubble"><div id="user" class="hs-u-body md"><p id="userP" data-markdown-han-text="true">用户段落</p><p data-markdown-han-text="true">末段</p></div></div>
<div id="input" class="hs-inp" contenteditable="true">输入内容</div>
<div id="execution" class="hs-execution-body md"><p>执行结果</p><p>第二段</p></div>
<div id="lead" class="hs-text hs-lead">步骤摘要</div>
<div class="hs-proc"><div id="tool" class="hs-step-h">读取文件</div><pre id="output" class="hs-pre">工具输出</pre></div>
<div id="hanRoot" class="hs-text md"></div><div id="latinRoot" class="hs-text md"></div>
<div id="mixedRoot" class="hs-u-body md"></div><div id="execRoot" class="hs-execution-body md"></div>
<div id="listRoot" class="hs-text md"></div><div id="quoteRoot" class="hs-text md"></div>
<div id="headingRoot" class="hs-text md"></div><div id="liteRoot" class="hs-text md"></div>
</div></div><div id="panel"><div class="side-fixed"><button id="sidebar" class="side-fx">导航</button></div><div class="side-foot"><button id="footer" class="side-fx">底部</button></div><div id="panel-list"><div class="term-item"><span id="session" class="ti-label">会话</span></div></div><div class="proj-fold"><span id="project" class="pf-name">工作流</span></div><div class="subarea-fold"><span id="folder" class="pf-name">文件夹</span></div></div>
<div class="rp-ctx"><div class="rp-ctx-head"><span id="menuWeight" class="rp-value">菜单</span></div></div>
<div id="term-head">终端标题</div><div id="terminalWeight" class="tm-window-title">终端窗口</div>
<div id="settingsWeight" class="sm-group-title">设置</div>
<div class="settings-center-live"><section class="sm-section"><h2 id="settingsSectionWeight" class="sm-title">设置分组</h2></section></div>
<div class="resource-center"><button id="actionWeight" class="ap-create">新建</button></div>
<div class="auth-tui-terminal"><div class="auth-tui-title"><strong id="authWeight">登录</strong></div></div>
<div class="hs-page"><div id="taskWeight" class="hs-current-task-title">任务</div><div id="stepWeight" class="hs-step-n">步骤</div><span id="peerWeight" class="hs-peer-label">子代理</span></div>
<textarea id="editWeight" class="fb-edit-ta">编辑</textarea><button id="plainButtonWeight">普通按钮</button>
<div id="overlay-title">标题</div><p id="reference" style='font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;font-weight:430;line-height:1.625;letter-spacing:0;font-synthesis-weight:none'>参考</p><pre id="result">pending</pre>'''
    script = r'''
const $=id=>document.getElementById(id), style=id=>getComputedStyle($(id));
const assert=(ok,label)=>{if(!ok) throw new Error(label);};
const snapshot=id=>{const s=style(id);return [s.fontSize,s.fontWeight,s.lineHeight,s.marginTop,s.marginBottom];};
const renderer=mdWithHanParagraphs(window.markdownit({html:false,linkify:true}));
for(const [id,text] of Object.entries({hanRoot:'中文第一段\n\n第二段**中文**\n\n末段中文',latinRoot:'English first\n\nEnglish next\n\nEnglish last',mixedRoot:'中文 first\n\nEnglish next\n\n中文 last',execRoot:'代理报告中文\n\n第二段中文',listRoot:'- 第一段\n\n  列表第二段\n\n- 最后一项',quoteRoot:'> 引文中文\n>\n> 第二段',headingRoot:'# 中文标题\n\n标题后正文\n\n```text\ncode\n```\n\n代码后正文'})) $(id).innerHTML=renderer.render(text);
$('liteRoot').innerHTML=hsLiteHtml('正在输出中文\n\n第二段中文');
const sharedWeights={menuWeight:400,'term-head':600,terminalWeight:560,settingsWeight:700,settingsSectionWeight:620,actionWeight:450,authWeight:560,taskWeight:560,stepWeight:600,peerWeight:450,editWeight:400,plainButtonWeight:400};
const sharedBaseline=Object.fromEntries(Object.keys(sharedWeights).map(id=>[id,style(id).fontWeight]));
const rootSnapshot=()=>Object.fromEntries(['hanRoot','latinRoot','mixedRoot','execRoot','listRoot','quoteRoot','headingRoot','liteRoot'].map(id=>[id,[...$(id).querySelectorAll('p')].map(p=>{const s=getComputedStyle(p);return [s.marginTop,s.marginBottom,s.fontSize,s.fontWeight];})]));
try {
  const rootBaseline=rootSnapshot();
  const baseline=Object.fromEntries(['answer','stream','user','input','execution','lead','tool','output','sidebar','footer','session','project','folder','overlay-title','code','bold','b'].map(id=>[id,snapshot(id)]));
  assert([1200,760,390].includes(window.innerWidth),'actual iframe viewport '+window.innerWidth);
    for(const theme of ['', 'light']) {
      document.documentElement.dataset.theme=theme;
      document.documentElement.classList.add('os-windows');
      for(const id of ['answer','stream','user','execution']) {
        const s=style(id);
        assert(s.fontSize==='14px' && s.fontWeight==='350' && s.lineHeight==='22.75px' && s.letterSpacing==='normal',id+' reading parameters '+JSON.stringify([s.fontSize,s.fontWeight,s.lineHeight,s.letterSpacing]));
      }
      for(const [id,weight] of Object.entries(sharedWeights)) assert(style(id).fontWeight===String(weight),id+' shared Windows baseline '+style(id).fontWeight);
      assert(style('input').fontSize==='14px' && style('input').lineHeight==='20px' && style('input').fontWeight==='350','editor is not prose');
      assert(style('paragraph').marginTop==='7px' && style('paragraph').marginBottom==='3.5px','h5/h6 predecessor uses paragraph unit');
      assert(style('userP').marginBottom==='4px','adjacent Han paragraphs');
      const margins=id=>[...$(id).querySelectorAll(':scope > p')].map(p=>{const s=getComputedStyle(p);return [s.marginTop,s.marginBottom];});
      assert(JSON.stringify(margins('hanRoot'))===JSON.stringify([['0px','4px'],['14px','4px'],['14px','0px']]),'Han conditional margins');
      assert(JSON.stringify(margins('latinRoot'))===JSON.stringify([['0px','3.5px'],['14px','14px'],['14px','0px']]),'Latin paragraph combinations');
      assert(JSON.stringify(margins('mixedRoot'))===JSON.stringify([['0px','3.5px'],['14px','14px'],['14px','0px']]),'Han-Latin transition is not a Han pair');
      assert(JSON.stringify(margins('execRoot'))===JSON.stringify([['0px','4px'],['14px','0px']]),'agent return uses same renderer');
      assert(JSON.stringify(margins('liteRoot'))===JSON.stringify([['0px','4px'],['14px','0px']]),'stream tail uses same Han condition');
      const list=[...$('listRoot').querySelectorAll('li > p')].map(p=>{const s=getComputedStyle(p);return [s.marginTop,s.marginBottom];});
      assert(JSON.stringify(list)===JSON.stringify([['0px','0px'],['14px','0px'],['0px','0px']]),'loose list paragraph margins');
      assert([...$('quoteRoot').querySelectorAll('p')].every(p=>getComputedStyle(p).margin==='0px'),'quote margins');
      assert(JSON.stringify(margins('headingRoot'))===JSON.stringify([['0px','3.5px'],['7px','0px']]),'non-paragraph predecessor');
      const paragraphs=[...$('hanRoot').querySelectorAll(':scope > p')];
      assert(Math.abs(paragraphs[1].getBoundingClientRect().top-paragraphs[0].getBoundingClientRect().bottom-14)<0.1,'Han margin collapse geometry (not a claimed 4px gap)');
      assert(style('last').marginBottom==='0px' && style('listP').marginBottom==='0px','no trailing or nested paragraph gap');
      assert(style('paragraph').fontWeight==='350' && style('item').fontWeight==='350','Windows reading baseline');
      assert(style('bold').fontWeight==='600' && style('b').fontWeight==='600','Windows semibold baseline');
      for(const id of ['sidebar','footer','session']) assert(style(id).fontSize==='13px' && style(id).fontWeight==='350' && style(id).lineHeight==='21px',id+' desktop navigation');
      for(const id of ['project','folder']) assert(style(id).fontSize==='14px' && style(id).fontWeight==='350' && style(id).lineHeight==='21px',id+' project hierarchy');
      assert(style('sidebar').minHeight==='31px' && style('sidebar').paddingTop==='5px','navigation row density');
      assert(style('overlay-title').fontSize==='18px' && style('overlay-title').fontWeight==='600','desktop title');
      assert(style('answer').fontFamily===style('reference').fontFamily,'system fallback stack identical');
      assert(style('answer').fontSynthesisWeight==='none','no synthetic weight');
      const ctx=document.createElement('canvas').getContext('2d');
      for(const weight of ['350','600']) {
        ctx.font=weight+' 14px '+style('answer').fontFamily;const actual=ctx.measureText('Windows中文字体对照').width;
        ctx.font=weight+' 14px '+style('reference').fontFamily;assert(actual===ctx.measureText('Windows中文字体对照').width,'mixed glyph metrics match reference');
      }
      // Compare actual glyph coverage, not merely computed font-weight strings.
      ctx.canvas.width=800;ctx.canvas.height=60;
      const ink=(text,weight)=>{
        ctx.clearRect(0,0,800,60);ctx.font=weight+' 14px '+style('answer').fontFamily;
        ctx.fillStyle='#000';ctx.fillText(text,4,30);
        const data=ctx.getImageData(0,0,800,60).data;
        let coverage=0;for(let i=3;i<data.length;i+=4)coverage+=data[i];return coverage;
      };
      for(const text of ['Windows reading sample','中文字体笔画粗细对照']) {
        const lighter=ink(text,style('answer').fontWeight), reference=ink(text,style('reference').fontWeight);
        assert(lighter>0 && lighter<reference,'lighter Latin/CJK glyph coverage '+JSON.stringify({text,lighter,reference}));
      }
      const sizes=[...$('answer').querySelectorAll('h1,h2,h3,h4')].map(h=>getComputedStyle(h).fontSize);
      assert(JSON.stringify(sizes)===JSON.stringify(['21px','17.5px','15.75px','14px']),'heading scale');
      for(const h of $('answer').querySelectorAll('h1,h2,h3,h4,h5,h6')) {
        const s=getComputedStyle(h);assert(s.fontWeight==='600' && s.paddingBottom==='0px' && s.borderBottomWidth==='0px','heading decoration removed');
        if(['H5','H6'].includes(h.tagName)) assert(s.fontSize==='14px' && s.lineHeight==='22.75px','small headings inherit content scale');
      }
      for(const id of ['inline','code']) assert(style(id).letterSpacing==='normal','code retains monospace tracking');
      assert(style('code').fontSize===baseline.code[0] && style('code').lineHeight===baseline.code[2],'code size/line-height unchanged');
      for(const id of ['lead','tool','output']) {
        const now=snapshot(id), before=baseline[id];
        assert(now[0]===before[0] && now[2]===before[2] && now[3]===before[3] && now[4]===before[4],id+' keeps compact dimensions');
        assert(now[1]===({lead:'500',tool:'350',output:'350'})[id],id+' retains Windows compact weight '+now[1]);
      }
      document.documentElement.classList.remove('os-windows');
    }
  document.documentElement.dataset.theme='';
  for(const [id,s] of Object.entries(baseline)) assert(JSON.stringify(snapshot(id))===JSON.stringify(s),id+' non-Windows unchanged');
  assert(JSON.stringify(rootSnapshot())===JSON.stringify(rootBaseline),'non-Windows paragraph rules unchanged');
  for(const [id,weight] of Object.entries(sharedBaseline)) assert(style(id).fontWeight===weight,id+' non-Windows weight unchanged');
  parent.postMessage({ok:true,viewport:innerWidth,themes:2,nonWindows:true,code:true,controls:true},'*');
} catch(error) {parent.postMessage({error:String(error),viewport:innerWidth},'*');}
'''
    with tempfile.TemporaryDirectory(prefix='desk-type-') as directory:
        folder = Path(directory)
        page = folder / 'reading.html'
        child = ('<!doctype html><meta charset="utf-8"><style>' + STYLE + '</style><body>' + fixture
                 + '<script>' + VENDOR + '</script><script>' + HAN_PATCH + '\n' + LITE + '\n' + script + '</script>')
        escaped = html.escape(child, quote=True)
        parent = '''<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
        const results=[];addEventListener('message',event=>{
          if(![...document.querySelectorAll('iframe')].some(f=>f.contentWindow===event.source))return;
          results.push(event.data);if(results.length===3)document.getElementById('result').textContent=JSON.stringify(results);
        });</script>'''
        parent += ''.join(f'<iframe style="width:{width}px;height:900px;border:0" srcdoc="{escaped}"></iframe>' for width in [1200,760,390])
        page.write_text(parent, encoding='utf-8')
        # Desk 进程继承的 AppHealth shim 会使 Edge headless 返回0但不输出DOM。
        # 仅隔离本测试子进程；不改宿主/系统兼容设置，不在失败后静默换浏览器。
        browser_env = dict(os.environ)
        if browser_env.get('__COMPAT_LAYER') == 'DetectorsAppHealth':
            browser_env.pop('__COMPAT_LAYER')
        result = subprocess.run([str(edge), '--headless=new', '--disable-gpu', '--no-first-run',
                                 '--disable-background-networking', '--disable-extensions',
                                 '--user-data-dir=' + str(folder / 'profile'), '--window-size=1400,900',
                                 '--virtual-time-budget=1000', '--dump-dom', page.as_uri()],
                                env=browser_env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
        assert result.returncode == 0, result.stderr[-1000:]
        match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
        assert match, {'stdout': result.stdout[-1000:], 'stderr': result.stderr[-2000:]}
        evidence = json.loads(html.unescape(match.group(1)))
        assert sorted(item['viewport'] for item in evidence) == [390, 760, 1200], evidence
        assert all(item.get('ok') and item['themes'] == 2 for item in evidence), evidence
