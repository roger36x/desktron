"""项目记忆阅读面：使用生产浮层、Markdown 与 CSS，不连接真实记忆或模型。"""
import os
from pathlib import Path
import re

import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]


def function(filename, name):
    source = (ROOT / "frontend" / filename).read_text()
    return re.search(r"(?:async )?function " + re.escape(name) + r"\(.*?\n}\n", source, re.S).group()


@pytest.fixture
def page():
    if sync_playwright is None:
        pytest.skip("需要 Playwright WebKit")
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.set_content('<main id="main" style="height:100dvh;width:100%"></main>')
        page.add_style_tag(content=(ROOT / "frontend/style.css").read_text())
        page.add_script_tag(content=(ROOT / "frontend/vendor/markdown-it.min.js").read_text())
        source = (ROOT / "frontend/app-settings-memory.js").read_text()
        script = r'''
const md = window.markdownit({html:false});
let _wfSheet = null, _wfImgSel = null;
const _WF_XMARK = '×';
const memoryText = '## Project context\n\n' +
  '这是用于排版验证的示例项目。读者打开项目记忆，是为了理解背景、既定选择和仍然有效的边界。\n\n' +
  '正文使用完整段落，不让辅助说明打断阅读。桌面限制行宽，窄屏保留必要留白。\n\n' +
  '## Decisions & rejections\n\n- 直接展示完整项目正文，不保留独立摘要。\n- 不改写存储的项目记忆。\n\n' +
  '## Constraints & principles\n\n只读展示保留来源与更新时间，不把阅读窗口变成编辑表单。\n\n' +
  '已被排除的做法仍应说明原因，避免后续会话反复讨论同一个问题。\n\n' +
  '事实、判断和待确认事项分别描述，让读者知道哪些结论已有依据。\n\n' +
  '```text\n' + 'very-long-code-'.repeat(100) + '\n```\n\n' +
  '| 名称 | 说明 |\n| --- | --- |\n| 测试 | ' + 'unbroken'.repeat(60) + ' |\n\n' +
  '## Open questions\n\n继续核对真实阅读效果。\n\n' + ('长文滚动测试段落。\n\n'.repeat(20));
async function api(path) {
  if (path.startsWith('/workbench/project-memory?')) return {items:[{
    id:'project-memory-'+'long-id-'.repeat(30), path:'demo.md', updated:'2026-10-04',
    summary:'示例项目摘要。'.repeat(35)}]};
  return memoryText;
}
'''
        script += function("app-core.js", "escapeHtml")
        script += re.search(r"function _stripFm\(t\) \{[^\n]+", source).group()
        for name in ("wfCloseTaskSheet", "_wfSheetFit", "openFloatSheet"):
            script += function("app-panel-task-memo.js", name)
        for name in ("projectMemoryBodyHtml", "projectMemoryReaderHtml", "openWorkflowMemory"):
            script += function("app-settings-memory.js", name)
        page.add_script_tag(content=script)
        yield page
        browser.close()


@pytest.mark.desktop
@pytest.mark.parametrize("theme,width,height", [("dark", 1200, 900), ("light", 1200, 900),
                                                ("dark", 390, 844), ("light", 320, 640)])
def test_reader_layout_with_full_body_and_no_summary(page, theme, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
    page.evaluate("openWorkflowMemory({id:'demo',name:'示例项目'})")
    state = page.evaluate('''() => {
      const sheet=document.querySelector('.wf-memory-sheet'), body=sheet.querySelector('.wf-sheet-body');
      const reader=sheet.querySelector('article'), prose=reader.querySelector('.wf-memory-body');
      const style=getComputedStyle(prose), r=sheet.getBoundingClientRect(), p=prose.getBoundingClientRect();
      return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:p.width,top:p.top,
        font:parseFloat(style.fontSize),line:parseFloat(style.lineHeight),
        scroll:body.scrollHeight>body.clientHeight,overflow:body.scrollWidth>body.clientWidth+1,
        noSummary:!reader.querySelector('details, .wf-memory-capsule'),
        article:reader.tagName,title:reader.querySelector('h1').textContent};
    }''')
    assert state["x"] >= 0 and state["y"] >= 0
    assert state["right"] <= width + 1 and state["bottom"] <= height + 1
    assert state["width"] <= 576 + 1
    assert state["width"] >= (500 if width > 600 else width - 68)
    assert state["font"] >= 15 and state["line"] >= state["font"] * 1.8
    assert state["top"] < height / 2
    assert state["scroll"] and not state["overflow"]
    assert state["noSummary"] and state["article"] == "ARTICLE" and state["title"] == "示例项目"
    assert page.locator('.wf-memory-body h2').first.inner_text() == "项目概况"
    # 长编号、代码块和表格仍不能制造整页横向滚动。
    assert page.locator('.wf-memory-source code').inner_text().startswith('project-memory-')
    assert page.locator('.wf-memory-capsule, .wf-memory-extras').count() == 0
    assert page.evaluate("document.querySelector('.wf-sheet-body').scrollWidth <= document.querySelector('.wf-sheet-body').clientWidth+1")
    page.evaluate("document.querySelector('.wf-sheet-body').scrollTop=0")
    artifact_dir = os.environ.get("DESK_MEMORY_READER_ARTIFACT_DIR")
    if artifact_dir:
        path = Path(artifact_dir)
        path.mkdir(parents=True, exist_ok=True)
        page.locator('.wf-memory-sheet').screenshot(path=str(path / f"reader-{theme}-{width}.png"))


@pytest.mark.desktop
def test_reader_shell_keyboard_close(page):
    page.evaluate("openWorkflowMemory({id:'demo',name:'示例项目'})")
    assert page.locator('.wf-memory-extras').count() == 0
    page.keyboard.press("Escape")
    assert page.locator('.wf-sheet').count() == 0


@pytest.mark.desktop
def test_reader_style_does_not_change_regular_task_sheet(page):
    page.evaluate("openFloatSheet({title:'普通任务'})")
    regular = page.locator('.wf-sheet').bounding_box()
    assert regular["width"] == 720
    assert page.locator('.wf-memory-sheet').count() == 0
    assert page.locator('.wf-sheet-body').evaluate("node => getComputedStyle(node).paddingLeft") == "16px"
    page.locator('.wf-sheet-x').click()
    assert page.locator('.wf-sheet').count() == 0
