"""Keep unused artwork out without removing active controls or shared SVG sources."""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"


def node(code):
    return json.loads(subprocess.check_output(["node", "-e", code], text=True, encoding="utf-8"))


def function(source, name):
    return re.search(r"\n(?:async )?function " + re.escape(name) + r"\(.*?\n}", source, re.S).group()


def test_application_matching_rules_have_no_shadowed_duplicates():
    source = (FRONT / "app-assets-apps.js").read_text(encoding="utf-8")
    snippet = source[source.index("const _APP_COLORS"):source.index("const _APP_RECENT_MAX")]
    rules = node(snippet + "\nconsole.log(JSON.stringify(_APP_GLYPHS.map(([re])=>String(re))));")
    assert len(rules) == len(set(rules))
    assert rules.count("/浏览器|browser/i") == 1


def test_approval_uses_the_shared_lock_after_its_bundle_definition():
    bundles = runpy.run_path(str(ROOT / "desktron/lib/frontend_bundle.py"))["ASSET_BUNDLES"]
    chat = bundles["/app-chat.js"]
    assert chat.index("app-chat-tools.js") < chat.index("app-approval.js")
    source = (FRONT / "app-approval.js").read_text(encoding="utf-8")
    assert "_APPR_GLYPH" not in source
    tools = (FRONT / "app-chat-tools.js").read_text(encoding="utf-8")
    icons = re.search(r"const HS_ICONS = \{.*?\n};", tools, re.S).group()
    result = node(icons + function(source, "_apprApprovalInner") +
                  "\nconsole.log(JSON.stringify({html:_apprApprovalInner(),lock:HS_ICONS.lock}));")
    assert result["lock"] and result["lock"] in result["html"]
    assert 'class="hs-acts"' in result["html"]


def test_missing_approval_icons_are_not_hidden_by_old_artwork():
    source = (FRONT / "app-approval.js").read_text(encoding="utf-8")
    code = function(source, "_apprApprovalInner") + """
let name = '';
try { _apprApprovalInner(); } catch (e) { name = e.name; }
console.log(JSON.stringify(name));
"""
    assert node(code) == "ReferenceError"


def test_system_cards_keep_real_icons_without_unused_image_overlay():
    source = (FRONT / "app-panel-arrangement-system.js").read_text(encoding="utf-8")
    assert "iconUrl" not in source
    code = "const escapeHtml=s=>String(s); const DeskI18n={tx:s=>s,txh:s=>s,tag:'zh-CN'};\n" + source + """
const cards = _arrSysCards({memory:{}, lark:{}});
console.log(JSON.stringify(cards.map(c=>({kind:c.kind,html:_arrSysCard(c)}))));
"""
    cards = node(code)
    assert {card["kind"] for card in cards} == {"memory"}
    for card in cards:
        assert 'class="arr-sys-icon"><svg' in card["html"]
        assert "<img" not in card["html"]
        assert 'class="arr-sys-chev"><svg' in card["html"]


class Buttons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.buttons.append(dict(attrs))


def test_notepad_removes_only_noop_toolbar_controls():
    source = (FRONT / "pages/notepad.html").read_text(encoding="utf-8")
    parsed = Buttons()
    parsed.feed(source.split("<script>")[0])
    toolbar = [b for b in parsed.buttons if "tb-btn" in b.get("class", "") or "tb-text-btn" in b.get("class", "")]
    assert {b.get("id") for b in toolbar} == {
        "mBackFolders", "btnSideExpand", "btnNew", "mBackList", "btnDelete", "btnEmptyTrash", "btnRestore",
    }
    assert "（占位）" not in source and "tb-aa" not in source
    assert 'id="searchInput"' in source
    assert 'id="editor" contenteditable="plaintext-only"' in source
    assert 'id="btnNewFolder"' in source


def test_current_memory_reader_is_retained_without_old_edit_artwork():
    source = (FRONT / "app-settings-memory.js").read_text(encoding="utf-8")
    assert "mem-edit-btn" not in source
    assert "const pencil" not in source
    view = function(source, "openWorkflowMemory")
    assert "projectMemoryReaderHtml(p, item, txt)" in view
    assert "/workbench/memory/edit" not in view
    assert "_loadContainerMemory" not in view
    assert "function _splitFm(t)" in source and "function _stripFm(t)" in source


def test_original_artwork_sources_and_compatibility_terminal_are_preserved():
    for name in ["app-file-browser-original-d8f988ab8b2b.svg", "app-terminal.png",
                 "app-browser-original-5d5d9b828321.svg", "app-monaco-ide-original-00700ed9f4c1.svg"]:
        assert (FRONT / "icons" / name).is_file()
