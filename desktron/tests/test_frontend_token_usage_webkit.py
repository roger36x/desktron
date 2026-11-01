"""合成用量数据的原生 WebKit 验收；不读取真实账本或调用模型。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
SOURCE = (ROOT / 'frontend/app-settings-usage.js').read_text(encoding='utf-8')
pytestmark = [pytest.mark.desktop, pytest.mark.skipif(
    sys.platform != 'darwin' or not shutil.which('swiftc') or os.environ.get('DESK_WEBKIT_TESTS') != '1',
    reason='需显式启用原生 WebKit 验收')]


@pytest.fixture(scope='module')
def webkit(tmp_path_factory):
    folder = tmp_path_factory.mktemp('usage-webkit')
    binary = folder / 'webkit'
    subprocess.run(['swiftc', str(ROOT / 'desktron/tests/fixtures/terminal_chrome_webkit.swift'),
                    '-o', str(binary)], capture_output=True, text=True, check=True, timeout=120)

    def run(script, width, theme):
        page = folder / 'fixture.html'
        page.write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        + '<style>' + CSS + 'body{margin:0}#host{width:min(560px,100%);margin:auto}</style>'
                        + '<main id="host"></main><script>document.documentElement.dataset.theme='
                        + json.dumps(theme) + ';' + SOURCE + script + '''
window.collectTerminalChrome=async()=>{
 try{await run();window.webkit.messageHandlers.result.postMessage({ok:true});}
 catch(e){window.webkit.messageHandlers.result.postMessage({error:String(e),stack:e.stack});}
};</script>''')
        result = subprocess.run([str(binary), str(page), str(width), '900'],
                                capture_output=True, text=True, timeout=50)
        assert result.returncode == 0, result.stderr
        evidence = json.loads(result.stdout)
        assert evidence.get('ok'), evidence
    return run


@pytest.mark.parametrize('width', [320, 1280])
@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('days', [30, 90, 365])
def test_heatmap_palette_geometry_ranges_and_drilldown(webkit, width, theme, days):
    script = r'''
const assert=(ok,msg)=>{if(!ok)throw Error(msg);};
const tick=()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
const daily={};for(let i=0;i<8;i++)daily['2026-10-'+(23+i)]={recorded_total:(i+1)*100,total:(i+1)*100,turns:1,recorded_turns:1};
const data={range:{start:'2025-10-31',end:'2026-10-30'},coverage:{start:'2025-10-31'},
 sessions:[{id:'synthetic',domain:'fixture',workflow:'合成工作流',title:'合成会话',engine:'pi',last_at:1,daily}],
 workflows:[{id:'fixture',name:'合成工作流'}]};
const calls=[];async function api(path){calls.push(path);return data;}
async function run(){
 const host=document.getElementById('host');renderTokenUsageSettings(host);await tick();
 host.querySelector('[data-days="DAYS"]').click();await tick();
 const grid=host.querySelector('.tu-heat-grid'),scroll=host.querySelector('.tu-heat-scroll');
 assert(grid.dataset.range==='DAYS','selected range');
 const levels=Array.from(host.querySelectorAll('button.tu-day')).filter(x=>daily[x.dataset.date]).map(x=>+x.dataset.level);
 assert(JSON.stringify(levels)==='[1,1,1,2,2,3,3,4]','four nonzero quantile levels');
 assert(host.querySelectorAll('.tu-legend i').length===5,'five legend cells');
 const cell=host.querySelector('button.tu-day[data-level="1"]'),s=getComputedStyle(cell),r=cell.getBoundingClientRect();
 assert(r.width===10&&r.height===10,'10px square cells');
 assert(s.boxShadow.includes('inset'),'subtle inset stroke');
 const light=document.documentElement.dataset.theme==='light';
 assert(s.backgroundColor===(light?'rgb(172, 238, 187)':'rgb(3, 58, 22)'),'theme palette');
 const empty=host.querySelector('button.tu-day[data-level="0"]');
 assert(getComputedStyle(empty).backgroundColor===(light?'rgb(239, 242, 245)':'rgb(21, 27, 35)'),'empty palette');
 const weekdays=host.querySelector('.tu-weekdays');
 assert(getComputedStyle(weekdays).fontSize==='12px','readable weekday labels');
 assert(weekdays.textContent==='周一周三周五','complete weekday names');
 assert(getComputedStyle(weekdays).position==='sticky','weekday column pinned');
 assert(host.querySelector('.tu-months').firstElementChild.textContent.includes('11'),'short initial month replaced by full month');
 assert(scroll.scrollWidth>scroll.clientWidth,'annual chart remains horizontally scrollable');
 assert(scroll.scrollLeft>0,'recent days initially visible');
 assert(document.documentElement.scrollWidth<=innerWidth,'no page-level horizontal overflow');
 scroll.scrollLeft=0;cell.click();await tick();
 assert(scroll.scrollLeft===0,'same-range date filter keeps manual scroll');
 assert(!host.querySelector('.tu-date-chip').hidden,'date filter visible');
 host.querySelector('.tu-date-chip button').click();await tick();
 assert(host.querySelector('.tu-date-chip').hidden,'filter cleared');
 host.querySelector('.tu-row').click();assert(host.querySelector('.tu-columns span').textContent==='会话','workflow drilldown');
 host.querySelector('.tu-row').click();assert(!host.querySelector('.tu-drawer').hidden,'session details');
 host.querySelector('.tu-drawer-close').click();assert(host.querySelector('.tu-drawer').hidden,'detail closes');
 assert(calls.length===1&&calls[0]==='/workbench/usage?days=365','filters do not reread ledger');
}
'''.replace('DAYS', str(days))
    webkit(script, width, theme)
