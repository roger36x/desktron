"""工作流详情页退役，项目管理迁入标题右键菜单。"""
from pathlib import Path
import json
import shutil
import subprocess

import pytest
import _srcmap
from frontend_i18n_js import NODE_PRELUDE


ROOT = Path(__file__).resolve().parents[2]
PANEL = _srcmap.panel_src()
RAIL = _srcmap.rail_src()
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
SIDEBAR = (ROOT / "frontend/app-panel-sidebar.js").read_text(encoding="utf-8")


def test_workflow_detail_module_is_not_shipped():
    assert not (ROOT / "frontend/app-panel-workflow.js").exists()
    assert "app-panel-workflow.js" not in (ROOT / "desktron/lib/frontend_bundle.py").read_text(encoding='utf-8')
    for symbol in ("_openWorkflowPage", "openProjectHome", "openWorkspaceOverview",
                   "_wfPagePaintTasks", "_wfPagePaintArtifacts", "wf-home"):
        assert symbol not in PANEL + RAIL


def test_old_workflow_home_history_falls_back_to_welcome():
    assert 'if (e.kind === "home") return _restoreEngineWelcome()' in RAIL
    assert 'navPush({ kind: "home"' not in RAIL


def test_project_management_lives_in_workflow_context_menu():
    for text in ('"wf.projects"', '"wf.addProject"', '"wf.openProjectFiles"', '"wf.linkSessions"',
                 '"wf.removeProject"', '"wf.openFiles"', '"wf.delete"'):
        assert text in SIDEBAR
    assert "_workflowReposForMenu" in SIDEBAR
    assert "_openWorkflowProjectMenu" in SIDEBAR
    assert "只移除引用，不删除磁盘目录" in PANEL
    assert "工作流详情" not in PANEL


def test_retired_page_css_is_gone():
    for selector in (".wf-repos", ".wf-repo ", ".wf-artifact", ".wf-foot",
                     ".wf-archive", ".wf-sess", ".wf-panels", ".workflow-detail-icon"):
        assert selector not in CSS


def test_workflow_menu_uses_original_icons_and_project_folder():
    for mapping in ("conversation: ICONS.plus", "addProject: ICONS.plus",
                    "importProject: ICONS.plus", "removeProject: ICONS.close"):
        assert mapping in SIDEBAR
    assert 'rename: \'<span class="menu-icon-mask is-rename"' in SIDEBAR
    assert 'remove: \'<span class="menu-icon-mask is-delete"' in SIDEBAR
    assert 'project: \'<span class="ico-folder-open" aria-hidden="true"></span>\'' in SIDEBAR
    assert "wf-menu-icon" not in SIDEBAR + CSS
    assert '.rp-ctx .rp-ico .ico-folder-open' in CSS


def test_workflow_menu_opens_read_only_memory_view():
    memory = (ROOT / "frontend/app-settings-memory.js").read_text(encoding="utf-8")
    license_text = (ROOT / "frontend/vendor/ICONS-LICENSE.txt").read_text(encoding="utf-8")
    assert 'memory: \'<span class="menu-icon-mask is-memory"' in SIDEBAR
    assert 'label: DeskI18n.t("wf.memory")' in SIDEBAR and "openWorkflowMemory(p)" in SIDEBAR
    assert SIDEBAR.index('label: DeskI18n.t("wf.memory")') < SIDEBAR.index('label: DeskI18n.t("wf.rename")')
    assert 'url("/icons/menu-memory.png")' in CSS
    png = (ROOT / "frontend/icons/menu-memory.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "https://www.flaticon.com/free-icon/writing_1001371" in license_text
    view = memory[memory.index("async function openWorkflowMemory"):]
    view = view[:view.index("\n}\n")]
    assert "/workbench/project-memory?domain=" in view and "/workbench/memory?name=" in view
    assert 'sheet.classList.add("wf-memory-sheet")' in view
    assert 'document.createElement("article")' in view
    assert "projectMemoryReaderHtml(p, item, txt)" in view
    reader = memory[memory.index("function projectMemoryReaderHtml"):memory.index("async function openWorkflowMemory")]
    assert "项目详情" in reader and "检索编号" in reader
    assert "projectMemoryBodyHtml(text)" in reader
    assert reader.index("wf-memory-body") < reader.index("wf-memory-source")
    assert "会话摘要" not in reader
    assert "projectMemoryCapsuleHtml" not in memory
    assert "wf-memory-extras" not in reader
    assert "/workbench/memory/edit" not in view and "mem-edit-btn" not in view


@pytest.mark.skipif(not shutil.which("node"), reason="需要 Node 验证记忆渲染函数")
def test_memory_rendering_preserves_body_and_escapes_metadata_without_summary():
    script = r'''
const fs = require("fs"), vm = require("vm"), assert = require("node:assert/strict");
const source = fs.readFileSync(ROOT + "/frontend/app-settings-memory.js", "utf8");
const core = fs.readFileSync(ROOT + "/frontend/app-core.js", "utf8");
const sandbox = { window: {}, module: { exports: {} } };
sandbox.self = sandbox.window; vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(ROOT + "/frontend/vendor/markdown-it.min.js", "utf8"), sandbox);
const md = (sandbox.window.markdownit || sandbox.markdownit)({html: false});
// 只提供两个函数实际使用的 DOM 接口；Markdown 使用产品内置的真实渲染器。
const document = { createElement(tag) {
  assert.equal(tag, "div");
  let html = "";
  return {
    get innerHTML() { return html; }, set innerHTML(value) { html = value; },
    querySelectorAll(selector) {
      assert.equal(selector, "h2");
      return Array.from(html.matchAll(/<h2>(.*?)<\/h2>/g), match => ({
        get textContent() { return match[1].replace(/<[^>]+>/g, "").replace(/&amp;/g, "&"); },
        set textContent(value) { html = html.replace(match[0], "<h2>" + value + "</h2>"); }
      }));
    }
  };
}};
const escapeHtml = eval("(" + core.match(/function escapeHtml\(s\) \{[\s\S]*?\n\}/)[0] + ")");
const _stripFm = eval("(" + source.match(/function _stripFm\(t\) \{[^\n]+/)[0] + ")");
eval(source.slice(source.indexOf("function projectMemoryBodyHtml"), source.indexOf("async function openWorkflowMemory")));
const text = "已废弃的独立摘要 <img src=x onerror=alert(1)> & 内容";
const input = "---\nid: demo\n---\n## Project context\n\n正文 Project context。\n\n" +
  "## Decisions & rejections\n\n选择 A。\n\n## Constraints & principles\n\n边界。\n\n## Open questions\n\n待定。\n\n" +
  "```md\n## Project context\n```\n\n### Open questions\n\n## constructor\n\n## 项目概况\n";
const rendered = projectMemoryBodyHtml(input);
for (const heading of ["项目概况", "已定方案", "长期约束", "待确认问题"]) assert(rendered.includes("<h2>" + heading + "</h2>"));
assert(rendered.includes("正文 Project context。"));
assert(rendered.includes("<code class=\"language-md\">## Project context"));
assert(rendered.includes("<h3>Open questions</h3>"));
assert(rendered.includes("<h2>constructor</h2>"));
assert(!rendered.includes("id: demo"));
assert(input.includes("## Project context"));
const reader = projectMemoryReaderHtml({name: '<标题>'}, {updated: '<日期>', id: '<编号>', summary: text}, input);
assert(reader.includes('<h1>&lt;标题&gt;</h1>'));
assert(reader.includes('更新于 &lt;日期&gt;'));
assert(reader.includes('检索编号 <code>&lt;编号&gt;</code>'));
assert(reader.indexOf('wf-memory-body') < reader.indexOf('wf-memory-source'));
assert(!reader.includes('wf-memory-extras'));
assert(!reader.includes('会话摘要'));
assert(!reader.includes('已废弃的独立摘要'));
assert(!reader.includes('onerror='));
'''.replace("ROOT", json.dumps(str(ROOT)))
    result = subprocess.run([shutil.which("node"), "-e", NODE_PRELUDE + script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
