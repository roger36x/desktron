"""Windows 双顶栏的能力标记、坐标桥与资源契约；不启动真实窗口。"""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "desktop/windows/js/window-chrome.js"
CSS = ROOT / "frontend/window-chrome.css"
APP = ROOT / "desktop/windows/src/app.rs"
NATIVE = ROOT / "desktop/windows/src/chrome.rs"


class ChromeContract(unittest.TestCase):
    def test_css_is_gated_and_keeps_sidebar_full_height(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("html.desk-windows-chrome #main { padding-top: var(--desk-caption-height); }", css)
        self.assertIn("--desk-caption-height: 32px", css)
        self.assertIn("left: calc(var(--desk-main-left) + 10px)", css)
        self.assertIn("body.side-collapsed #top-nav", css)
        self.assertIn("body.term-fs #top-nav", css)
        self.assertIn("padding-left: 128px", css)
        self.assertNotIn("fullsize-shell", css)
        self.assertNotIn("#panel {", css)
        self.assertIn('href="/window-chrome.css"', (ROOT / "frontend/index.html").read_text(encoding="utf-8"))

    def test_native_caption_is_not_html_window_controls(self):
        native = NATIVE.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        self.assertIn("pub const HEIGHT: f64 = 32.0", native)
        self.assertIn("RGN_DIFF", native)
        self.assertIn("SetWindowRgn(self.web", native)
        self.assertIn("HTMAXBUTTON", native)
        self.assertIn("DefSubclassProc", native)
        self.assertIn("WM_SYSCOMMAND", native)
        self.assertIn("SC_CLOSE", native)
        self.assertIn("Chrome::new(&window, &web).expect", app)
        self.assertIn(".with_undecorated_shadow(true)", app)
        self.assertIn("self.chrome.layout()", app)
        self.assertIn("self.chrome.set_light(light)", app)
        self.assertIn("WindowEvent::CloseRequested, .. } => app.request_quit()", app)
        self.assertIn('include_str!("../js/window-chrome.js")', app)
        self.assertIn("'windowChrome'", (ROOT / "desktop/windows/js/main-bridge.js").read_text(encoding="utf-8"))

    def test_full_client_frame_has_a_separate_native_resize_input_layer(self):
        native = NATIVE.read_text(encoding="utf-8")
        self.assertIn("WM_NCCALCSIZE if lp.0 != 0 =>", native)
        self.assertIn("CreateWindowExW(WS_EX_LAYERED", native)
        self.assertIn("SetLayeredWindowAttributes(edge, COLORREF(0), 1, LWA_ALPHA)", native)
        self.assertIn("self.state.layout_resize()?", native)
        resize = native.split('unsafe extern "system" fn resize_callback(', 1)[1].split("fn resize_hit(", 1)[0]
        self.assertIn("state.resize_hit(p.x, p.y)", resize)
        self.assertIn("SendMessageW(state.hwnd, msg, Some(wp), Some(lp))", resize)
        self.assertIn("WM_NCLBUTTONDBLCLK", resize)
        self.assertIn("WM_SETCURSOR", resize)
        self.assertIn("self.chrome.raise_resize()", APP.read_text(encoding="utf-8"))
        self.assertIn("state.resize.set(HWND::default())", resize)
        self.assertIn("state.alive.set(false)", native)
        self.assertIn("RemoveWindowSubclass(hwnd, Some(resize_callback)", resize)

    def test_caption_has_no_visible_duplicate_brand_but_keeps_window_identity(self):
        native = NATIVE.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        self.assertNotIn("DrawTextW", native)
        self.assertNotIn('"Desktron".encode_utf16()', native)
        self.assertIn('.with_title("Desktron")', app)

    def test_paint_restores_persistent_dc_state_before_end_paint(self):
        native = NATIVE.read_text(encoding="utf-8")
        paint = native.split("unsafe fn paint(&self)", 1)[1].split("unsafe fn draw", 1)[0]
        self.assertLess(paint.index("with_saved_dc(dc, |dc| self.clear_backdrop(dc))"), paint.index("EndPaint("))
        caption = native.split("unsafe fn paint_caption", 1)[1].split("unsafe fn draw(&self", 1)[0]
        self.assertIn("paint_opaque(dc", caption)
        self.assertIn("SetViewportOrgEx(surface, -g.left, 0, None)", caption)
        scope = native.split("unsafe fn with_saved_dc", 1)[1].split("fn rgb", 1)[0]
        self.assertLess(scope.index("SaveDC(dc)"), scope.index("draw(dc)"))
        self.assertLess(scope.index("draw(dc)"), scope.index("RestoreDC(dc, saved)"))
        self.assertIn("window_chrome_dc_save_failed", scope)
        self.assertIn("window_chrome_dc_restore_failed", scope)


@unittest.skipUnless(shutil.which("node"), "Node required for coordinate bridge tests")
class CoordinateBridge(unittest.TestCase):
    def run_js(self, body, setup="", script=SCRIPT):
        harness = r"""
const messages=[], changes=[], errors=[], raf=[], observers=[], events={};
let left=248;
const main={getBoundingClientRect(){return {left};}};
const root={classList:{add(value){changes.push(['class',value]);}},
 style:{setProperty(name,value){changes.push([name,value]);}}};
const document={documentElement:root,body:{},readyState:'complete',
 getElementById(id){return id==='main'?main:null;},
 addEventListener(type,callback){events[type]=callback;}};
const window={location:{origin:'http://127.0.0.1:7765',pathname:'/'},
 webkit:{messageHandlers:{windowChrome:{postMessage(body){messages.push(body);}}}},
 addEventListener(type,callback){events[type]=callback;}};
window.top=window;
class ResizeObserver {constructor(callback){this.callback=callback;observers.push(this);} observe(target){this.target=target;}}
class MutationObserver extends ResizeObserver {}
function requestAnimationFrame(callback){raf.push(callback);}
function flush(){while(raf.length)raf.shift()();}
const originalConsole=console;
const testConsole={error(value){errors.push(value);}};
"""
        # 脚本运行在页面式作用域内，用实际源码测试而不复制逻辑。
        source = harness + setup + "\n(function(console){\n" + script.read_text(encoding="utf-8") + "\n})(testConsole);\n" + body
        result = subprocess.run(["node"], input=source, text=True, encoding="utf-8",
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_initial_layout_resize_collapse_and_no_duplicate_messages(self):
        out = self.run_js(r"""
observers[0].callback(); observers[1].callback(); flush();
left=320; observers[0].callback(); events.resize(); flush();
left=0; observers[1].callback(); flush();
left=248; observers[0].callback(); flush();
originalConsole.log(JSON.stringify({messages,changes,errors,observers:observers.length}));
""")
        self.assertEqual(out["messages"], [{"left": 248}, {"left": 320}, {"left": 0}, {"left": 248}])
        self.assertIn(["--desk-main-left", "0px"], out["changes"])
        self.assertEqual(out["observers"], 2)
        self.assertEqual(out["errors"], [])

    def test_iframe_does_not_gain_chrome_capability(self):
        out = self.run_js("originalConsole.log(JSON.stringify({messages,changes}));", "window.top={};")
        self.assertEqual(out, {"messages": [], "changes": []})

    def test_placeholder_and_other_pages_do_not_enable_layout(self):
        out = self.run_js("originalConsole.log(JSON.stringify({messages,changes,errors}));",
                          "window.location={origin:'null',pathname:'blank'};")
        self.assertEqual(out, {"messages": [], "changes": [], "errors": []})

    def test_missing_bridge_explicitly_reports_error_and_does_not_enable_layout(self):
        out = self.run_js("originalConsole.log(JSON.stringify({messages,changes,errors}));", "window.webkit={};")
        self.assertEqual(out["changes"], [])
        self.assertEqual(out["messages"], [])
        self.assertEqual(out["errors"], ["window_chrome_bridge_unavailable"])

    def test_waits_for_dom_before_reading_main(self):
        out = self.run_js("events.DOMContentLoaded(); originalConsole.log(JSON.stringify(messages));",
                          "document.readyState='loading';")
        self.assertEqual(out, [{"left": 248}])

    def test_document_creation_before_root_waits_for_parser(self):
        out = self.run_js(r"""
const before={messages:messages.length,changes:changes.length};
document.documentElement=root;
events.DOMContentLoaded();
originalConsole.log(JSON.stringify({before,messages,changes,errors}));
""", "document.readyState='loading'; document.documentElement=null;")
        self.assertEqual(out["before"], {"messages": 0, "changes": 0})
        self.assertEqual(out["messages"], [{"left": 248}])
        self.assertIn(["class", "desk-windows-chrome"], out["changes"])
        self.assertEqual(out["errors"], [])

    def test_main_bridge_before_root_keeps_input_and_layout_bridge(self):
        out = self.run_js(r"""
const handlers=Object.keys(window.webkit.messageHandlers);
const listeners=Object.keys(events);
document.documentElement=root;
events.DOMContentLoaded();
window.webkit.messageHandlers.windowChrome.postMessage({left:248});
originalConsole.log(JSON.stringify({handlers,listeners,changes,sent}));
""", r"""
delete window.webkit;
const sent=[];
window.ipc={postMessage(value){sent.push(JSON.parse(value));}};
document.readyState='loading'; document.documentElement=null;
""", ROOT / "desktop/windows/js/main-bridge.js")
        self.assertIn("windowChrome", out["handlers"])
        self.assertIn("keydown", out["listeners"])
        self.assertIn("pointerdown", out["listeners"])
        self.assertIn(["class", "desk-windows-shell"], out["changes"])
        self.assertEqual(out["sent"], [{"name": "windowChrome", "body": {"left": 248}}])

    def test_paint_precedes_default_validation(self):
        native = NATIVE.read_text(encoding="utf-8")
        parent = native.split('unsafe extern "system" fn callback(', 1)[1]
        handler = parent.split("WM_PAINT => {", 1)[1].split("WM_ERASEBKGND", 1)[0]
        self.assertLess(handler.index("state.paint()"), handler.index("DefSubclassProc("))

    def test_invalid_left_is_not_sent_and_negative_left_is_clamped(self):
        out = self.run_js(r"""
left=NaN; events.resize(); flush();
left=-5; events.resize(); flush();
originalConsole.log(JSON.stringify(messages));
""")
        self.assertEqual(out, [{"left": 248}, {"left": 0}])


if __name__ == "__main__":
    unittest.main()
