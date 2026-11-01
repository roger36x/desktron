"""极简欢迎页：真实会话入口、可选上下文与响应式布局。"""
import os
import re
import unittest
import pytest
import _srcmap

from frontend_i18n_js import BROWSER_SCRIPT

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ASSETS = _srcmap.assets_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
SIDEBAR = open(os.path.join(ROOT, "frontend", "app-panel-sidebar.js"), encoding="utf-8").read()
INDEX = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
CHAT_PAGE = open(os.path.join(ROOT, "frontend", "app-chat-page.js"), encoding="utf-8").read()
CHAT_TOOLS = open(os.path.join(ROOT, "frontend", "app-chat-tools.js"), encoding="utf-8").read()
CLIP_ICON = re.search(r"^  clip: (.*),$", CHAT_TOOLS, re.M).group(1)


def _fn(name):
    match = re.search(rf"\n(?:async )?function {name}\(.*?\n}}\n", ASSETS, re.S)
    assert match, name
    return match.group(0)


class MinimalWelcomeStructure(unittest.TestCase):
    def test_empty_state_is_one_real_composer(self):
        html = _fn("_welcomeHTML")
        self.assertIn("今天想让 Desk 做什么？", html)
        self.assertIn('textarea class="frw-input"', html)
        self.assertIn('class="frw-send"', html)
        self.assertIn("添加文件夹", html)
        self.assertIn('select class="frw-domain"', html)
        self.assertIn('<option value="">聊天</option>', html)
        self.assertNotIn('class="frw-quick"', html)
        self.assertNotIn('class="frw-workflow"', html)
        self.assertIn("return _welcomeHTML();", _fn("vibeWorkingHTML"))

    def test_wizard_and_fake_preview_are_removed(self):
        self.assertNotIn("function _firstRunWelcomeHTML", ASSETS)
        self.assertNotIn("function _wireFirstRunWelcome", ASSETS)
        html = _fn("_welcomeHTML")
        for stale in ("data-step=", "frw-progress", "frw-choices", "frw-setup-grid",
                      "frw-ready", "进入工作台", "稍后设置"):
            self.assertNotIn(stale, html)

    def test_setup_remains_an_independent_surface(self):
        self.assertIn("if (isLocalView())", INIT)
        self.assertIn("openSetupGuide({ auto: true })", INIT)
        self.assertNotIn("firstRunWelcomeOpen", INIT)

    def test_engine_warning_only_renders_when_unavailable(self):
        html = _fn("_welcomeHTML")
        self.assertIn('available ? "" : `<div class="frw-notice">', html)
        wire = _fn("_wireWelcome")
        self.assertIn('root.querySelector(".frw-notice button")', wire)
        self.assertIn("settings.onclick = () => openSettings()", wire)


class MinimalWelcomeFlow(unittest.TestCase):
    def test_submit_uses_normal_html_session_prompt_path(self):
        wire = _fn("_wireWelcome")
        self.assertIn("state = { path:", wire)
        self.assertIn("engine: _welcomeEngine()", wire)
        self.assertIn("newHtmlSessionWithPrompt({ domain, engine: state.engine, shepherd_mode: state.shepherd }, text)", wire)
        self.assertIn('event.key === "Enter" && !event.shiftKey && !event.isComposing', wire)
        self.assertIn("event.preventDefault(); submit();", wire)

    def test_engine_picker_is_local_to_the_pending_session(self):
        html = _fn("_welcomeHTML")
        wire = _fn("_wireWelcome")
        self.assertIn('aria-haspopup="listbox"', html)
        self.assertIn('data-engine=', html)
        self.assertIn('role="option"', html)
        self.assertIn('state.engine = row.dataset.engine || state.engine || _welcomeEngine()', wire)
        self.assertNotIn('api("/workbench/engine"', wire)
        self.assertNotIn("location.reload", wire)

    def test_shepherd_is_a_per_session_choice(self):
        html = _fn("_welcomeHTML")
        wire = _fn("_wireWelcome")
        self.assertIn('data-shepherd="1"', html)
        self.assertIn("engineRows + shepherdRow", html)
        self.assertIn('state.shepherd = row.dataset.shepherd === "1"', wire)
        self.assertIn('row.dataset.engine || state.engine || _welcomeEngine()', wire)
        self.assertIn('typeof opts.shepherd_mode === "boolean"', CHAT_PAGE)

    def test_unavailable_current_engine_falls_back_and_blocks_unready_send(self):
        default = _fn("_welcomeEngine")
        wire = _fn("_wireWelcome")
        self.assertIn("find((id) => e.available[id])", default)
        self.assertIn("state.busy || !engineReady()", wire)
        self.assertIn('DeskI18n.t("welcome.needEngine")', wire)

    def test_first_prompt_failure_is_not_reported_as_session_success(self):
        send = re.search(r"async function newHtmlSessionWithPrompt\(.*?\n}\n", CHAT_PAGE, re.S).group(0)
        self.assertIn("if (!await hsDispatch", send)
        self.assertIn('throw new Error("首条消息未发送，请重试")', send)
        self.assertNotIn("catch {}", send)

    def test_empty_submit_stays_on_welcome_and_focuses_input(self):
        wire = _fn("_wireWelcome")
        submit = wire[wire.index("const submit") :]
        empty = submit[submit.index("if (!text)"):submit.index("if (state.busy)")]
        self.assertIn('DeskI18n.t("welcome.needText")', empty)
        self.assertIn("input.focus(); return", empty)
        self.assertNotIn("newHtmlSession", empty)

    def test_folder_is_deferred_until_first_send(self):
        wire = _fn("_wireWelcome")
        choose = wire[wire.index("const chooseFolder"):wire.index("const linkFolder")]
        self.assertIn("const path = await pickFolder()", choose)
        self.assertNotIn("createWorkspaceByName", choose)
        submit = wire[wire.index("const submit"):]
        self.assertIn("if (state.path && state.domain) await linkFolder(domain)", submit)
        self.assertIn('DeskI18n.t("welcome.needDomain")', choose)
        self.assertIn('JSON.stringify({ op: "add", path: state.path })', wire)

    def test_existing_workflow_is_used_without_creating_another(self):
        wire = _fn("_wireWelcome")
        self.assertIn("state.domain = destination.value", wire)
        self.assertIn("const domain = state.domain || DESKTOP", wire)
        self.assertNotIn("createWorkspaceByName", wire)
        self.assertIn('state.path = ""', wire)

    def test_workspace_creation_still_has_one_shared_implementation(self):
        create = _fn("createWorkspaceByName")
        popup = _fn("openNewWorkspace")
        self.assertIn('api("/workbench/domains"', create)
        self.assertIn("await createWorkspaceByName(name)", popup)


@unittest.skipUnless(sync_playwright, "需要 Playwright 和 WebKit")
@pytest.mark.desktop
class MinimalWelcomeEnginePickerWebKit(unittest.TestCase):
    def test_selected_engine_creates_first_session_without_global_switch(self):
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_content('<main id="host"></main>')
            page.add_style_tag(content=CSS)
            page.add_script_tag(content=r'''
const DESKTOP = "_desktop";
const ICONS = {target:"<svg></svg>",folder:"<svg></svg>",terminal:"<svg></svg>",code:"<svg></svg>",layers:"<svg></svg>"};
const BRAND_ICONS = {claude:"<svg data-brand='claude'></svg>",codex:"<svg data-brand='codex'></svg>",shepherd:"<span class='brand-shepherd img-ic'></span>"};
const ENGINE_ICON = {claude:"layers",codex:"code",pi:"terminal"};
const RP_CHECK = "✓";
let curEngine = "claude";
let engineInfo = {
  engines:{claude:{label:"Claude Code"},codex:{label:"Codex"},pi:{label:"Pi"}},
  available:{claude:true,codex:true,pi:false}
};
const domains = [{id:"ws-test",name:"示例工作流"},{id:"_desktop",name:"桌面"},{id:"special",name:"不应显示",kind:"system"}], nav = {type:"desktop",id:DESKTOP};
function escapeHtml(value) { const el=document.createElement("span"); el.textContent=String(value); return el.innerHTML; }
async function pickFolder(){ window.folderCalls=(window.folderCalls||0)+1; return window.folderPath || ""; }
async function api(path, opts){ window.apiCalls=(window.apiCalls||0)+1; window.lastApi={path,opts}; return {ok:true}; }
async function newHtmlSessionWithPrompt(opts,prompt){ if(window.failSend) throw Error("模拟发送失败"); window.created={opts,prompt}; return {id:"new"}; }
async function createWorkspaceByName(){ throw Error("not expected"); }
async function reloadDomains(){}
function openSettings(){}
''')
            page.add_script_tag(content="const HS_ICONS = {clip:" + CLIP_ICON + "};")
            page.add_script_tag(content=BROWSER_SCRIPT)
            page.add_script_tag(content=_fn("curDomainId") + _fn("_welcomeEngine") + _fn("_welcomeHTML")
                                + _fn("_welcomeI18n") + _fn("_wireWelcome"))
            page.evaluate('''() => {
  const host=document.getElementById("host");
  host.innerHTML=_welcomeHTML();
  _wireWelcome(host);
}''')
            self.assertEqual(page.locator(".frw-lead").count(), 0)
            self.assertEqual(page.locator(".frw-engine-chevron").count(), 0)
            self.assertEqual(page.locator(".frw-input").get_attribute("placeholder"), "写点什么...")
            self.assertEqual(page.locator(".frw-quick").count(), 0)
            self.assertEqual(page.locator(".frw-domain option").all_inner_texts(), ["聊天", "示例工作流"])
            self.assertEqual(page.locator(".frw-domain").input_value(), "")
            page.evaluate('''() => {
              const reference=document.createElement("div"); reference.className="hs-page";
              reference.id="clip-reference";
              reference.innerHTML='<button class="hs-clip">'+HS_ICONS.clip+'</button>';
              document.body.appendChild(reference);
            }''')
            for theme in ("dark", "light"):
                page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                for width in (1280, 390, 320):
                    page.set_viewport_size({"width": width, "height": 844})
                    self.assertEqual(page.locator(".frw-attach").inner_html(), page.locator("#clip-reference .hs-clip").inner_html())
                    for welcome, reference, properties in (
                        (".frw-attach", "#clip-reference .hs-clip", ["width", "height", "padding", "color", "borderRadius"]),
                        (".frw-attach svg", "#clip-reference svg", ["width", "height", "strokeWidth", "strokeLinecap", "strokeLinejoin"]),
                    ):
                        styles = page.evaluate('''({welcome,reference,properties}) => {
                          const a=getComputedStyle(document.querySelector(welcome));
                          const b=getComputedStyle(document.querySelector(reference));
                          return properties.map(k=>[k,a[k],b[k]]);
                        }''', {"welcome": welcome, "reference": reference, "properties": properties})
                        for name, a, b in styles:
                            self.assertEqual(a, b, (theme, width, name))
                    icon = page.locator(".frw-attach svg").bounding_box()
                    send = page.locator(".frw-send").bounding_box()
                    self.assertAlmostEqual(icon["y"]+icon["height"]/2, send["y"]+send["height"]/2, delta=.5)
                    for by_keyboard in (False, True):
                        page.locator(".frw-attach").focus()
                        if by_keyboard:
                            page.keyboard.press("Tab")
                        else:
                            page.locator(".frw-domain").focus()
                        self.assertTrue(page.locator(".frw-domain").evaluate("el => el === document.activeElement"))
                        state = page.locator(".frw-domain").evaluate('''el => {
                          const s=getComputedStyle(el);
                          return [s.borderWidth,s.outlineStyle,s.boxShadow,s.webkitAppearance];
                        }''')
                        self.assertEqual(state, ["0px", "none", "none", "none"])
            page.evaluate('document.getElementById("clip-reference").remove()')
            page.locator(".frw-input").focus()
            for width, height, input_height in ((1280, 800, 24), (390, 844, 24), (320, 640, 24)):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertAlmostEqual(page.locator(".frw-input").bounding_box()["height"], input_height, delta=1)
                    self.assertLessEqual(page.locator(".frw-composer").bounding_box()["height"], 100)
                    self.assertLessEqual(page.locator(".frw-composer").bounding_box()["width"], width)
                    page.locator(".frw-input").fill("多行输入\n" * 20)
                    self.assertAlmostEqual(page.locator(".frw-input").bounding_box()["height"], 200, delta=1)
                    page.locator(".frw-input").fill("")
            page.set_viewport_size({"width": 1280, "height": 800})
            self.assertEqual(page.locator(".frw-engine-label").inner_text(), "Claude Code")
            page.locator(".frw-engine").click()
            self.assertEqual(page.locator(".frw-engine-item").count(), 3)
            self.assertEqual(page.locator(".frw-engine-item > span:nth-child(2)").all_inner_texts(), ["Claude Code", "Codex", "Shepherd"])
            self.assertEqual(page.locator('.frw-engine-item[data-engine="claude"]').get_attribute("aria-selected"), "true")
            self.assertEqual(page.locator('.frw-engine-item[data-engine="claude"] .frw-engine-check').inner_text(), "✓")
            # 用工作流右键菜单的真实类名作参照，验证公共样式实际生效而不只检查源码值。
            page.evaluate('''() => {
              const reference=document.createElement("div"); reference.id="menu-reference";
              reference.className="rp-ctx"; reference.style.cssText="left:8px;top:8px";
              reference.innerHTML='<div class="rp-row rp-hasico"><span class="rp-ico"></span><span>示例工作流</span></div><div class="rp-row rp-sep">下一组</div>';
              document.body.appendChild(reference);
            }''')
            for theme in ("dark", "light"):
                page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                for width, height in ((1280, 800), (390, 844), (320, 640)):
                    with self.subTest(menu_theme=theme, menu_width=width):
                        page.set_viewport_size({"width": width, "height": height})
                        for welcome, reference, properties in (
                            (".frw-engine-menu", "#menu-reference", [
                                "backgroundColor", "borderWidth", "borderColor", "borderRadius",
                                "boxShadow", "padding", "webkitBackdropFilter", "boxSizing"]),
                            ('.frw-engine-item[data-engine="codex"]', "#menu-reference .rp-hasico", [
                                "fontFamily", "fontSize", "lineHeight", "color", "padding", "gap",
                                "height", "borderRadius", "marginRight"]),
                        ):
                            actual = page.evaluate('''({welcome, reference, properties}) => {
                              const a=getComputedStyle(document.querySelector(welcome));
                              const b=getComputedStyle(document.querySelector(reference));
                              return properties.map(k => [k,a[k],b[k]]);
                            }''', {"welcome": welcome, "reference": reference, "properties": properties})
                            for name, a, b in actual:
                                self.assertEqual(a, b, (theme, width, welcome, name))
                        separator = page.evaluate('''() => {
                          const a=getComputedStyle(document.querySelector(".frw-engine-sep"));
                          const b=getComputedStyle(document.querySelector("#menu-reference .rp-sep"),"::before");
                          return [a.height,b.height,a.backgroundColor,b.backgroundColor];
                        }''')
                        self.assertEqual(separator[0], separator[1])
                        self.assertEqual(separator[2], separator[3])
                        box = page.locator(".frw-engine-menu").bounding_box()
                        self.assertGreaterEqual(box["x"], 0)
                        self.assertGreaterEqual(box["y"], 0)
                        self.assertLessEqual(box["x"] + box["width"], width)
                        self.assertLessEqual(box["y"] + box["height"], height)
                page.set_viewport_size({"width": 1280, "height": 800})
                page.locator('.frw-engine-item[data-engine="codex"]').hover()
                hover_color = page.locator('.frw-engine-item[data-engine="codex"]').evaluate("el => getComputedStyle(el).backgroundColor")
                page.locator("#menu-reference .rp-hasico").hover()
                self.assertEqual(hover_color, page.locator("#menu-reference .rp-hasico").evaluate("el => getComputedStyle(el).backgroundColor"))
                selected_color = page.locator('.frw-engine-item[data-engine="claude"]').evaluate("el => getComputedStyle(el).backgroundColor")
                self.assertEqual(selected_color, hover_color)
            page.evaluate('document.getElementById("menu-reference").remove()')
            page.locator(".frw-input").focus()
            page.keyboard.press("Escape")
            self.assertTrue(page.locator(".frw-engine-menu").is_hidden())
            self.assertEqual(page.locator(".frw-engine").get_attribute("aria-expanded"), "false")
            page.locator(".frw-engine").click()
            page.locator('.frw-engine-item[data-shepherd="1"]').click()
            self.assertEqual(page.locator(".frw-engine-label").inner_text(), "Shepherd")
            self.assertEqual(page.locator(".frw-engine .brand-shepherd").count(), 1)
            page.locator(".frw-engine").click()
            page.locator('.frw-engine-item[data-engine="codex"]').click()
            self.assertEqual(page.locator(".frw-engine-label").inner_text(), "Codex")
            page.locator(".frw-input").fill("修复默认聊天页")
            page.locator(".frw-send").click()
            page.wait_for_function("window.created !== undefined")
            self.assertEqual(page.evaluate("window.created"), {
                "opts": {"domain": "_desktop", "engine": "codex", "shepherd_mode": False},
                "prompt": "修复默认聊天页",
            })
            self.assertEqual(page.evaluate("window.apiCalls || 0"), 0)
            # 空列表仍有聊天；文件夹关联不得改变聊天归属或在选择时提前写入。
            page.evaluate('''() => {
              const host=document.getElementById("host"); host.innerHTML=_welcomeHTML();
              _wireWelcome(host); window.created=undefined; window.folderPath="/tmp/example";
            }''')
            page.locator(".frw-attach").click()
            self.assertEqual(page.evaluate("window.folderCalls || 0"), 0)
            self.assertIn("请先选择工作流", page.locator(".frw-error").inner_text())
            page.locator(".frw-domain").select_option("ws-test")
            page.locator(".frw-attach").click()
            self.assertEqual(page.evaluate("window.apiCalls || 0"), 0)
            self.assertFalse(page.locator(".frw-context").is_hidden())
            page.locator(".frw-domain").select_option("")
            self.assertTrue(page.locator(".frw-context").is_hidden())
            page.locator(".frw-domain").select_option("ws-test")
            page.locator(".frw-attach").click()
            page.locator(".frw-input").fill("关联工作流文件夹")
            page.locator(".frw-send").click()
            page.wait_for_function("window.created !== undefined")
            self.assertEqual(page.evaluate("window.created.opts.domain"), "ws-test")
            self.assertEqual(page.evaluate("window.lastApi.path"), "/workbench/domains/ws-test/repos")
            self.assertEqual(page.evaluate("JSON.parse(window.lastApi.opts.body)"), {"op": "add", "path": "/tmp/example"})
            page.evaluate("window.apiCalls=0")
            # 失败保留文本与工作流选择，重试应发送到所选工作流，不新建工作流。
            page.evaluate('''() => {
              const host=document.getElementById("host"); host.innerHTML=_welcomeHTML();
              _wireWelcome(host); window.created=undefined; window.failSend=true;
            }''')
            page.locator(".frw-domain").select_option("ws-test")
            self.assertEqual(page.locator(".frw-domain").input_value(), "ws-test")
            page.locator(".frw-input").fill("工作流消息")
            page.locator(".frw-send").click()
            page.wait_for_function('document.querySelector(".frw-error").textContent.includes("模拟发送失败")')
            self.assertEqual(page.locator(".frw-input").input_value(), "工作流消息")
            self.assertEqual(page.locator(".frw-domain").input_value(), "ws-test")
            page.evaluate("window.failSend=false")
            page.locator(".frw-send").click()
            page.wait_for_function("window.created !== undefined")
            self.assertEqual(page.evaluate("window.created.opts.domain"), "ws-test")
            self.assertEqual(page.evaluate("window.apiCalls || 0"), 0)
            # 比较稳定态，不把按钮过渡动画的中间色误判成样式差异。
            page.add_style_tag(content=".frw-send, .hs-send { transition: none !important; }")
            # 浅/深色及聚焦态与会话框比较计算样式，防止仅数值相似却被覆盖。
            page.evaluate('''() => {
              const sample=document.createElement("div"); sample.className="hs-page";
              sample.innerHTML='<div class="hs-box"><div class="hs-inp">样例</div><div class="hs-row"><button class="hs-send"></button></div></div>';
              document.body.appendChild(sample);
            }''')
            for theme in ("dark", "light"):
                page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                for welcome, session, properties in (
                    (".frw-composer", ".hs-box", ["backgroundColor", "borderRadius", "padding", "boxShadow", "borderWidth"]),
                    (".frw-input", ".hs-inp", ["fontSize", "lineHeight", "minHeight", "maxHeight", "color"]),
                    (".frw-send", ".hs-send", ["width", "height", "borderRadius", "backgroundColor", "color"]),
                ):
                    # 当前 welcome 已发出，处于 busy，send 保持未激活的同一档。
                    actual = page.evaluate('''({welcome, session, properties}) => {
                      const a=getComputedStyle(document.querySelector(welcome));
                      const b=getComputedStyle(document.querySelector(session));
                      return properties.map(k => [k,a[k],b[k]]);
                    }''', {"welcome": welcome, "session": session, "properties": properties})
                    for name, a, b in actual:
                        self.assertEqual(a, b, (theme, welcome, name))
            page.evaluate('''() => {
              domains.length=0;
              const host=document.getElementById("host"); host.innerHTML=_welcomeHTML(); _wireWelcome(host);
            }''')
            self.assertEqual(page.locator(".frw-domain option").all_inner_texts(), ["聊天"])
            self.assertEqual(errors, [])
            browser.close()


class MinimalWelcomePresentation(unittest.TestCase):
    def test_welcome_uses_full_surface_without_terminal_gutters(self):
        self.assertIn("#terminal.welcome-on #term-host { max-width: none; padding: 0; }", CSS)
        self.assertIn('classList.add("welcome-on")', _fn("_showWatermark"))
        self.assertIn('classList.remove("welcome-on")', _fn("attachTerminal"))

    def test_welcome_cannot_be_minimized(self):
        clear = re.search(r"function clearTermHead\(\).*?\n}", SIDEBAR, re.S).group(0)
        restore = re.search(r"function setTermHead\(.*?\n}", SIDEBAR, re.S).group(0)
        self.assertIn('$("#term-min")', clear)
        self.assertIn("minimize.hidden = true", clear)
        self.assertIn("minimize.hidden = false", restore)
        self.assertIn("#term-min[hidden]", CSS)
        self.assertRegex(INDEX, r'id="term-min"[^>]* hidden')

    def test_composer_matches_confirmed_compact_direction(self):
        self.assertIn('.frw-composer,\n.hs-page .hs-box { --hs-box-bg: var(--ink-16);', CSS)
        self.assertIn('.frw-input,\n.hs-page .hs-inp { min-height: 24px;', CSS)
        self.assertIn('.frw-send,\n.hs-page .hs-send { width: 28px;', CSS)
        self.assertIn('placeholder="写点什么..."', _fn("_welcomeHTML"))
        self.assertIn(".frw-send.ready", CSS)
        self.assertNotIn(".frw-quick", CSS)

    def test_heading_is_text_only_without_separate_brand_row(self):
        html = _fn("_welcomeHTML")
        self.assertIn('<h1 class="frw-title"><span>今天想让 Desk 做什么？</span></h1>', html)
        self.assertNotIn('class="frw-title-logo"', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('class="frw-brand"', html)
        self.assertNotIn("描述目标；文件和工作流需要时再添加。", html)
        self.assertNotIn("frw-lead", html)
        self.assertNotIn(".frw-lead", CSS)
        self.assertNotIn("直接描述目标", html)
        title = re.search(r"\.frw-welcome \.frw-title \{([^}]*)\}", CSS).group(1)
        self.assertIn("align-items: center", title)
        self.assertIn("justify-content: center", title)
        self.assertNotIn('/welcome-logo.png', html)

    def test_attachment_reuses_session_icon_without_geometry_overrides(self):
        self.assertIn('const attachIcon = HS_ICONS.clip;', _fn('_welcomeHTML'))
        self.assertNotIn('.frw-attach svg', CSS)

    def test_destination_has_no_native_or_highlight_border(self):
        domain = re.search(r'\.frw-domain \{([^}]*)\}', CSS).group(1)
        for rule in ('appearance: none', 'outline: none', 'box-shadow: none', 'border: 0', 'line-height: inherit'):
            self.assertIn(rule, domain)
        self.assertIn('.frw-destination::after', CSS)
        focus = re.search(r'\.frw-domain:focus-visible \{([^}]*)\}', CSS).group(1)
        self.assertIn('background:', focus)
        self.assertNotIn('outline:', focus)

    def test_engine_picker_keeps_centerline_without_chevron(self):
        icon = re.search(r"\.frw-engine-icon \{([^}]*)\}", CSS).group(1)
        label = re.search(r"\.frw-engine-label \{([^}]*)\}", CSS).group(1)
        self.assertIn("height: 18px", icon)
        self.assertIn("line-height: 18px", label)
        self.assertIn("place-items: center", icon)
        self.assertNotIn("frw-engine-chevron", _fn("_welcomeHTML"))
        self.assertNotIn(".frw-engine-chevron", CSS)
        self.assertIn('aria-haspopup="listbox"', _fn("_welcomeHTML"))

    def test_engine_menu_reuses_workflow_context_menu_presentation(self):
        self.assertIn('.frw-engine-menu,\n.rp-ctx {', CSS)
        self.assertIn('.frw-engine-item,\n.rp-ctx .rp-row {', CSS)
        self.assertIn('.frw-engine-menu, .rp-ctx, .resume-pop, #engine-menu {', CSS)
        self.assertIn('.frw-engine-sep, .rp-ctx .rp-row.rp-sep::before {', CSS)
        menu = re.search(r'\.frw-engine-menu \{([^}]*)\}', CSS).group(1)
        for override in ('background:', 'box-shadow:', 'border-radius:', 'padding:'):
            self.assertNotIn(override, menu)

    def test_shepherd_icon_has_a_complete_mask_outside_engine_menu(self):
        html = _fn("_welcomeHTML")
        shepherd = re.search(r"\.brand-shepherd\.img-ic \{([^}]*)\}", CSS).group(1)
        self.assertIn("BRAND_ICONS.shepherd", html)
        self.assertIn('-webkit-mask: url("/icons/shepherd.png") center / contain no-repeat', shepherd)
        self.assertIn('mask: url("/icons/shepherd.png") center / contain no-repeat', shepherd)

    def test_mobile_keeps_composer_inside_viewport(self):
        welcome = CSS[CSS.index("/* 极简欢迎页："):]
        mobile = welcome[welcome.index("@media (max-width: 820px)"):welcome.index("@media (prefers-reduced-motion")]
        self.assertIn("#terminal.welcome-on .vibe-empty.frw-welcome", mobile)
        self.assertIn("padding: calc(62px + env(safe-area-inset-top)) 18px 42px", mobile)
        self.assertIn(".frw-welcome .frw-frame { width: 100%; max-width: none;", mobile)
        self.assertIn(".frw-destination { max-width: 120px; }", mobile)
        self.assertIn(".frw-engine-menu { right: 12px;", mobile)


if __name__ == "__main__":
    unittest.main()
