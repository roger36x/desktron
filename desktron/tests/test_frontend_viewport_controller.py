"""运行真实滚动函数与可控帧队列，验证意图、布局事务和任务生命周期。"""
import json
import re
import subprocess
import unittest

import _srcmap
from frontend_i18n_js import NODE_PRELUDE

CHAT = _srcmap.chat_src()


def fn(name):
    match = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert match, name
    return match.group(0)


NAMES = ["hsViewport", "hsViewportCancel", "hsViewportWrite", "hsViewportSchedule",
         "hsViewportCommit", "hsViewportChange", "hsSetStick", "hsScrollCommit",
         "hsFollowCap", "hsFollowTarget", "hsRestoreViewportAnchor", "hsViewportApplyAnchor",
         "hsHoldCapture", "hsHoldAdjust", "hsScroll", "hsUnstick", "hsPin", "hsObserveLayout",
         "hsRestickAfterScroll"]

HARNESS = r"""
let now=1000, serial=0, frames=new Map(), timers=new Map(), observers=[];
const performance={now:()=>now}, HS_FOLLOW_K=.34, HS_FOLLOW_MIN=1.5;
function requestAnimationFrame(f){frames.set(++serial,f);return serial}
function cancelAnimationFrame(id){frames.delete(id)}
function setTimeout(f){timers.set(++serial,f);return serial}
function clearTimeout(id){timers.delete(id)}
function tick(){now+=16;const batch=[...frames.values()];frames.clear();batch.forEach(f=>f())}
function run(n=5){for(let i=0;i<n;i++)tick()}
function hsTocSchedule(){}
function hsRemount(){}
function hsMeasureBubbles(v){if(v.measure)v.measure()}
function hsViewportAnchor(v){return v.stick?null:{bodyTop:v.body.scrollTop,turn:{}}}
function hsTurnAtAnchor(v){return v.testTurn||null}
function hsBlockAt(){return null}
function addEventListener(){}
class ResizeObserver{constructor(cb){this.cb=cb;observers.push(this)} observe(){}}
function makeView(){
 const writes=[], events={};let top=1400;
 const classes={toggle(){},remove(){},add(){},contains(){return false}};
 const body={scrollHeight:2000,clientHeight:600,isConnected:true,classList:classes,
   get scrollTop(){return top},set scrollTop(t){top=Math.max(0,Math.min(this.scrollHeight-this.clientHeight,t));writes.push(top)},
   getBoundingClientRect:()=>({top:0}), addEventListener:(type,cb)=>events[type]=cb};
 return {body,el:{isConnected:true,querySelector:()=>null,classList:classes},stream:{},
   turns:[],stick:true,busy:false,lastTop:1400,writes,events};
}
const v=makeView();
"""


class ViewportBehavior(unittest.TestCase):
    def check_js(self, body):
        code = "\n".join(fn(n) for n in NAMES) + HARNESS + "\n" + body
        result = subprocess.run(["node", "-e", NODE_PRELUDE + code], text=True, capture_output=True, timeout=15, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_新贴底意图撤销旧锚点包括迟到回调(self):
        got = self.check_js(r"""
hsSetStick(v,false);v.body.scrollTop=1000;
hsRestoreViewportAnchor(v,{bodyTop:1000});const stale=[...frames.values()];
hsSetStick(v,true);stale.forEach(f=>f());run();
console.log(JSON.stringify({top:v.body.scrollTop,restoring:v.restoringScroll,queued:frames.size}));
""")
        self.assertEqual(got, dict(top=1400, restoring=False, queued=0))

    def test_新阅读手势撤销旧跟随与旧恢复(self):
        got = self.check_js(r"""
v.busy=true;hsScroll(v);const follow=[...frames.values()];
hsUnstick(v);v.body.scrollTop=900;follow.forEach(f=>f());run();
const reading=v.body.scrollTop;
hsRestoreViewportAnchor(v,{bodyTop:900});const restore=[...frames.values()];
hsUnstick(v);v.body.scrollTop=700;restore.forEach(f=>f());run();
console.log(JSON.stringify({reading,top:v.body.scrollTop,stick:v.stick,queued:frames.size}));
""")
        self.assertEqual(got, dict(reading=900, top=700, stick=False, queued=0))

    def test_尺寸事件与消息事件只共用一个缓动帧(self):
        got = self.check_js(r"""
hsObserveLayout(v);v.busy=true;v.body.scrollHeight+=26;
for(let i=0;i<30;i++){hsScroll(v);hsScrollCommit(v);observers[0].cb([])}
const pending=frames.size,before=v.writes.length;tick();
const after=v.writes.length,top=v.body.scrollTop;v.busy=false;run();
console.log(JSON.stringify({pending,before,after,top,final:v.body.scrollTop,remaining:frames.size}));
""")
        self.assertEqual((got["pending"], got["before"], got["after"]), (1, 0, 1))
        self.assertGreater(got["top"], 1400)
        self.assertLess(got["top"], 1426)
        self.assertEqual((got["final"], got["remaining"]), (1426, 0))

    def test_布局事务在折叠与附属投影完成后一次提交(self):
        got = self.check_js(r"""
v.busy=true;v.measure=()=>{v.body.scrollHeight-=1800};
hsViewportChange(v,()=>{
 v.body.scrollHeight+=2000;hsScrollCommit(v,true);
 hsViewportChange(v,()=>{v.body.scrollHeight+=100});
});
console.log(JSON.stringify({writes:v.writes,top:v.body.scrollTop,depth:hsViewport(v).depth}));
""")
        self.assertEqual(got, dict(writes=[1700], top=1700, depth=0))

    def test_布局事务不能覆盖其中出现的新意图(self):
        got = self.check_js(r"""
hsViewportChange(v,()=>{hsUnstick(v);v.body.scrollTop=800});run();
console.log(JSON.stringify({top:v.body.scrollTop,stick:v.stick,depth:hsViewport(v).depth}));
""")
        self.assertEqual(got, dict(top=800, stick=False, depth=0))

    def test_补历史按原阅读元素恢复且不抢后续手势(self):
        got = self.check_js(r"""
hsSetStick(v,false);v.body.scrollTop=900;let y=900;
const el={isConnected:true,children:[],getBoundingClientRect:()=>({top:y-v.body.scrollTop,bottom:y-v.body.scrollTop+900,height:900})};
v.testTurn={el};v.turns=[v.testTurn];
hsViewportAnchor=()=>({turn:{},bodyTop:900,bodyOffset:0});
hsViewportChange(v,()=>{y+=400;v.body.scrollHeight+=400});
const restored=v.body.scrollTop,pending=frames.size;
v.body.scrollTop-=30;run();
console.log(JSON.stringify({restored,pending,top:v.body.scrollTop,restoring:v.restoringScroll}));
""")
        self.assertEqual(got, dict(restored=1300, pending=0, top=1270, restoring=False))

    def test_异步过程全文回填不依赖尺寸观察器也保持贴底(self):
        got = self.check_js(fn("hsThinkFull") + r"""
let reply;function api(){return new Promise(resolve=>reply=resolve)}
function hsResetNarrativeCommit(){}
function hsPaint(view,part){view.body.scrollHeight+=1200}
const more={hidden:false,disabled:false,textContent:""};
const part={id:"r1",type:"reasoning",text:"预览",textMore:5000,buf:"",
  el:{querySelector:()=>more}};v.sid="session";
hsThinkFull(v,part);reply({text:"完整过程"});
setImmediate(()=>console.log(JSON.stringify({top:v.body.scrollTop,
  bottom:v.body.scrollHeight-v.body.clientHeight,gap:v.body.scrollHeight-v.body.clientHeight-v.body.scrollTop,
  text:part.text,more:part.textMore,hidden:more.hidden})));
""")
        self.assertEqual(got, dict(top=2600, bottom=2600, gap=0,
                                   text="完整过程", hidden=True))
        self.assertNotIn("more", got)

    def test_异常退出释放布局事务(self):
        got = self.check_js(r"""
try{hsViewportChange(v,()=>{throw Error('render')})}catch(e){}
v.body.scrollHeight+=100;hsScrollCommit(v,true);
console.log(JSON.stringify({depth:hsViewport(v).depth,top:v.body.scrollTop}));
""")
        self.assertEqual(got, dict(depth=0, top=1500))

    def test_隐藏和销毁不继续读写视口(self):
        got = self.check_js(r"""
v.busy=true;hsScroll(v);v.body.clientHeight=0;run();
const hidden=frames.size,writes=v.writes.length;
v.body.clientHeight=600;v.body.scrollHeight+=200;hsScrollCommit(v);tick();
const resumed=v.body.scrollTop>1400;
v.body.isConnected=false;run();
console.log(JSON.stringify({hidden,writes,resumed,left:frames.size}));
""")
        self.assertEqual(got, dict(hidden=0, writes=0, resumed=True, left=0))

    def test_过期回贴计时器不覆盖新阅读意图(self):
        got = self.check_js(r"""
hsUnstick(v);hsRestickAfterScroll(v,true);const stale=[...timers.values()];
hsUnstick(v);v.body.scrollTop=1399;stale.forEach(f=>f());
console.log(JSON.stringify({stick:v.stick,top:v.body.scrollTop}));
""")
        self.assertEqual(got, dict(stick=False, top=1399))

    def test_隐藏期间暂停的阅读校正显示后能结束(self):
        got = self.check_js(r"""
hsObserveLayout(v);hsSetStick(v,false);hsRestoreViewportAnchor(v,{bodyTop:900});
v.body.clientHeight=0;run();const paused=frames.size;
v.body.clientHeight=600;observers[0].cb([]);run();
console.log(JSON.stringify({paused,restoring:v.restoringScroll,top:v.body.scrollTop,queued:frames.size}));
""")
        self.assertEqual(got, dict(paused=0, restoring=False, top=900, queued=0))

    def test_旧端短时贴底复用队列且可由阅读取消(self):
        got = self.check_js(r"""
for(let i=0;i<30;i++)hsPin(v);
const pending=frames.size;hsUnstick(v);v.body.scrollTop=600;run();
console.log(JSON.stringify({pending,queued:frames.size,top:v.body.scrollTop}));
""")
        self.assertEqual(got, dict(pending=1, queued=0, top=600))

    def test_只有主写入器能补偿正文位置(self):
        for name in ("hsScrollCommit", "hsPin", "hsRestoreViewportAnchor", "hsViewportApplyAnchor",
                     "hsHoldAdjust", "hsRebuild", "hsPrepend", "hsRemount", "hsTrimDom"):
            with self.subTest(name=name):
                self.assertNotRegex(fn(name), r"v\.body\.scrollTop\s*(?:[+-]=|=(?!=))")
        for name in ("hsPin", "hsRestoreViewportAnchor", "hsScroll"):
            self.assertNotIn("requestAnimationFrame", fn(name))
        for name in ("hsRebuild", "hsPrepend", "hsRemount", "hsTrimDom"):
            self.assertIn("hsViewportChange(v,", fn(name))

    def test_空闲位置漂移复位但按住鼠标不等于向上拖动(self):
        build = fn("hsBuild")
        start = build.index('v.body.addEventListener("scroll"')
        end = build.index('  // 滚轮向上即脱钩', start)
        got = self.check_js('const window={addEventListener(){}};function pageBack(){};\n'
                            + build[start:end] + r"""
v.events.pointerdown({clientY:100});v.body.scrollTop=900;v.events.scroll();
const idle={top:v.body.scrollTop,stick:v.stick};
v.events.pointermove({clientY:120,buttons:1});v.body.scrollTop=700;v.events.scroll();run();
console.log(JSON.stringify({idle,read:{top:v.body.scrollTop,stick:v.stick}}));
""")
        self.assertEqual(got, dict(idle=dict(top=1400, stick=True), read=dict(top=700, stick=False)))


if __name__ == "__main__":
    unittest.main()
