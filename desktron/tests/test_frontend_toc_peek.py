"""刻度预览：触摸松手后的定时收起不能被兼容鼠标事件取消。"""
import subprocess
import unittest

from test_frontend_recall import fn


class TocPeek(unittest.TestCase):
    def test_touch_cleanup_and_mouse_hover(self):
        source = "\n".join(fn("app-chat-overlays.js", name) for name in
                           ("hsTocBind", "hsPeek", "hsPeekLater", "hsPeekHide"))
        script = r'''
const assert = require('node:assert/strict');
let seq=0, timers=new Map(), jumps=0;
function setTimeout(cb,ms){const id=++seq;timers.set(id,{cb,ms});return id}
function clearTimeout(id){timers.delete(id)}
class El {
  constructor(){this.events={};this.nodes={};this.style={};this.offsetHeight=40;
    const classes=new Set();this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)};}
  addEventListener(name,cb){this.events[name]=cb}
  fire(name,event={}){this.events[name]?.(event);this['on'+name]?.(event)}
  contains(el){return el===this}
  appendChild(){}
  querySelector(s){return this.nodes[s] ||= new El()}
  getBoundingClientRect(){return {top:20,height:400}}
}
const document={createElement:()=>new El()};
function hsTocSync(){} function hsUnstick(){}
function hsViewportReveal(){jumps++}
const turn={user:'问题',parts:new Map(),el:new El()};
const tick=new El();tick._turn=turn;
function hsTocHit(v){v.tkOn=tick;hsPeek(v,tick,turn)}
const v={toc:new El(),el:new El()};hsTocBind(v);
function touch(){v.toc.fire('touchstart',{touches:[{clientY:50}]})}
function expire(){assert.equal(timers.size,1);const entry=[...timers.values()][0];assert.equal(entry.ms,1600);entry.cb();assert.equal(timers.size,0);assert.equal(v.peekT,null);assert.equal(v.peekEl.classList.contains('on'),false)}
touch();assert.equal(v.peekEl.classList.contains('on'),true);
v.toc.fire('touchend');
// iOS 触摸后的兼容 mouseenter/mousemove 不应重新打开卡片或取消计时器。
v.toc.fire('mouseenter',{clientY:50});v.toc.fire('mousemove',{clientY:50});
v.peekEl.fire('mouseenter');v.peekEl.fire('pointerenter',{pointerType:'touch'});
v.toc.fire('pointermove',{pointerType:'touch',clientY:50});expire();
v.toc.fire('mousemove',{clientY:50});assert.equal(v.peekEl.classList.contains('on'),false);
touch();v.toc.fire('touchcancel');expire();
touch();v.toc.fire('touchend');v.toc.fire('touchend');assert.equal(timers.size,1);
v.el.fire('touchstart',{target:new El()});assert.equal(timers.size,0);assert.equal(v.peekEl.classList.contains('on'),false);
// 桌面鼠标仍能从刻度移入卡片停留，移出后收起。
v.toc.fire('pointerenter',{pointerType:'mouse',clientY:50});
v.toc.fire('pointerleave',{pointerType:'mouse'});assert.equal(timers.size,1);
v.peekEl.fire('pointerenter',{pointerType:'mouse'});assert.equal(timers.size,0);
assert.equal(v.peekEl.classList.contains('on'),true);
v.peekEl.fire('pointerleave',{pointerType:'mouse'});
assert.equal([...timers.values()][0].ms,140);
v.peekEl.fire('click');assert.equal(jumps,1);assert.equal(timers.size,0);
assert.equal(v.peekEl.classList.contains('on'),false);
'''
        result = subprocess.run(["node", "-e", source + script],
                                capture_output=True, text=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
