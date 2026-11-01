"""Recent UI backport: copy-only user footer, fork routing and welcome safety."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

FRONT = Path(__file__).resolve().parents[2] / 'frontend'


def source(name):
    return (FRONT / name).read_text(encoding='utf-8')


def function(name, symbol):
    return re.search(r'\n(?:async )?function ' + symbol + r'\(.*?\n}\n', source(name), re.S).group()


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required')
def test_user_footer_has_copy_and_time_but_never_edits_the_composer():
    script = r'''
const assert = require('node:assert/strict');
const time = {textContent:'',remove(){this.removed=true;}};
const button = {};
const element = {querySelector(s){
  if(s==='.hs-time')return time;
  if(s==='[data-a="ucopy"]')return button;
  throw Error('unexpected footer action '+s);
}};
const document={createElement(){return element;}};
const HS_ICONS={copy:'<svg></svg>'};
const hsWhen=()=> '12:30', hsMsgMs=()=>0;
let copied=''; const hsCopy=text=>copied=text;
const v={inp:{textContent:'existing draft'}};
const t={user:'literal user text',uid:'u',uat:1};
''' + function('app-chat-refs.js', 'hsUFoot') + r'''
hsUFoot(v,t,{appendChild(el){assert.equal(el,element);}});
assert.equal(time.textContent,'12:30');
assert.ok(element.innerHTML.includes('data-a="ucopy"'));
assert.ok(!element.innerHTML.includes('data-a="edit"'));
button.onclick();
assert.equal(copied,'literal user text');
assert.equal(v.inp.textContent,'existing draft');
assert.equal(t.uFootEl,element);
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required')
def test_fork_uses_reply_anchor_and_opens_the_returned_session():
    script = r'''
const assert=require('node:assert/strict');
let called,opened;
async function api(url,opts){called={url,opts};return {id:'child',title:'branch'};}
function openHtmlSession(id,title){opened={id,title};}
function alert(error){throw Error(error);}
''' + function('app-chat-refs.js', 'hsFork') + r'''
(async()=>{
await hsFork({sid:'source'},{msgId:'reply'});
assert.equal(called.url,'/session/source/fork');
assert.deepEqual(JSON.parse(called.opts.body),{msg_id:'reply'});
assert.equal(called.opts.timeout,90000);
assert.deepEqual(opened,{id:'child',title:'branch'});
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_welcome_keeps_text_only_heading_and_safe_engine_selection():
    html = function('app-assets-apps.js', '_welcomeHTML')
    title = re.search(r'<h1 class="frw-title">(.*?)</h1>', html).group(1)
    assert '<img' not in title and '<svg' not in title
    wire = function('app-assets-apps.js', '_wireWelcome')
    assert 'engineButton.disabled = state.busy' in wire
    assert 'if (state.busy) return;' in wire
    assert 'event.keyCode !== 229' in wire
    assert 'if (state.shepherd) state.engine = _welcomeEngine(true)' in wire
    assert 'e.available[e.shepherd_engine]' in function('app-assets-apps.js', '_welcomeEngine')


def test_browser_new_page_uses_centred_globe_and_more_visible_ink():
    browser = source('app-browser.js')
    expression = re.search(r'const _BR_ICON_GLOBE = (".*");', browser).group(1)
    globe = ET.fromstring(json.loads(expression))
    assert globe.attrib['width'] == globe.attrib['height'] == '38'
    assert globe.find('.//{http://www.w3.org/2000/svg}ellipse').attrib == {
        'cx': '16', 'cy': '16', 'rx': '5.3', 'ry': '11.2'}
    assert globe.find('.//{http://www.w3.org/2000/svg}path').attrib['d'] == 'M5.5 12.1h21M5.5 19.9h21'
    assert '.br-empty-mark { display: flex; color: var(--ink-48);' in source('style.css')
    manifest = json.loads(source('manifest.json'))
    for icon in manifest['icons']:
        assert (FRONT / icon['src'].lstrip('/')).is_file()
