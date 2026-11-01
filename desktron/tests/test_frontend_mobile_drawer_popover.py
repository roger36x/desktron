"""手机抽屉里的锚定菜单必须完整盖在遮罩之上，并可在伸出抽屉处点击。

`.resume-pop` 挂在 body 下，默认 z-index 50；手机抽屉遮罩是 #app::after/z-index 55。
菜单在抽屉内部的部分看似正常，一旦伸到抽屉右侧就被遮暗，点击也先落到遮罩。
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
RAIL = (ROOT / "frontend/app-rail-topnav.js").read_text(encoding="utf-8")
TERM_UI = (ROOT / "frontend/app-assets-term-ui.js").read_text(encoding="utf-8")
DRAWER_LISTENER = RAIL[RAIL.index('  document.addEventListener("click", (ev) => {', RAIL.index("// 抽屉收起的两条路")):]
DRAWER_LISTENER = DRAWER_LISTENER[:DRAWER_LISTENER.index("}, true);") + len("}, true);")]
PICKER_START = TERM_UI.index("async function openResumePicker")
PICKER_CLOSE_START = TERM_UI.index("  const close = (e) => {", PICKER_START)
PICKER_CLOSE_END = TERM_UI.index("  }, 0);", PICKER_CLOSE_START) + len("  }, 0);")
PICKER_CLOSE = TERM_UI[PICKER_CLOSE_START:PICKER_CLOSE_END]


def mobile_blocks():
    marker = '@media (max-width: 820px), (max-height: 520px) and (pointer: coarse) {'
    start = 0
    while True:
        i = CSS.find(marker, start)
        if i < 0:
            return
        head = CSS.find("{", i)
        depth, j = 1, head + 1
        while depth and j < len(CSS):
            if CSS[j] == "{": depth += 1
            elif CSS[j] == "}": depth -= 1
            j += 1
        yield CSS[head + 1:j - 1]
        start = j


def test_mobile_drawer_popovers_are_above_the_drawer_backdrop():
    mobile = "\n".join(mobile_blocks())
    assert re.search(r"\.resume-pop\s*\{[^}]*z-index:\s*(?:6\d|[7-9]\d|\d{3,})", mobile), mobile


def test_desktop_popover_layer_is_unchanged():
    base = CSS[:CSS.index('@media (max-width: 820px), (max-height: 520px) and (pointer: coarse) {')]
    rule = re.search(r"\.resume-pop\s*\{([^}]*)\}", base)
    assert rule and re.search(r"z-index:\s*50(?:\D|$)", rule.group(1)), rule.group(1) if rule else base


def test_resume_picker_closes_on_touch_outside_and_drawer_close():
    picker = TERM_UI[PICKER_START:TERM_UI.index("// resume 列表行的右键菜单", PICKER_START)]
    assert 'document.addEventListener("pointerdown", close)' in picker
    assert 'window.addEventListener("cd-drawer-closed", close)' in picker
    assert 'window.removeEventListener("cd-drawer-closed", close)' in picker
    assert "target.nodeType" in picker
    assert "drawerClosed" in picker and "run !== _resumePickerRun" in picker
    # 请求期间用隐藏的 .resume-pop 占位；打开/关闭任何同族菜单都会移除它，迟到结果不得复活。
    assert 'pending.className = "resume-pop"' in picker
    assert "!pending.isConnected" in picker
    assert "pending.replaceWith(pop)" in picker


def test_top_nav_button_uses_the_same_drawer_close_broadcast():
    side_click = RAIL[RAIL.index("side.onclick = () => {"):RAIL.index("const on =", RAIL.index("side.onclick = () => {"))]
    assert "closeDrawer()" in side_click
    assert 'classList.toggle("mo-drawer")' not in side_click


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="需要 macOS WebKit")
@pytest.mark.desktop
def test_webkit_menu_extension_is_visible_and_hit_testable(tmp_path):
    html = f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
      html,body{{margin:0;width:100%;height:100%;}} {CSS}
      #app{{position:relative;width:100%;height:100%;}}
      #panel{{display:block;}}
      .resume-pop{{left:170px;top:300px;width:210px;height:180px;visibility:visible;}}
      .resume-pop .rp-row{{height:34px;}}
    </style>
    <body class="mo-drawer"><div id="app"><aside id="panel"></aside><main id="main"></main></div>
      <div class="resume-pop"><div class="rp-row"><span class="rp-title">历史会话</span></div></div>
      <script>
        let selected = 0;
        const row = document.querySelector('.rp-row');
        const pop = document.querySelector('.resume-pop');
        const anchor = document.getElementById('panel');
        {PICKER_CLOSE}
        row.onclick = () => {{ selected += 1; close(); }};
        const closeDrawer = () => {{
          if (!document.body.classList.contains('mo-drawer')) return;
          document.body.classList.remove('mo-drawer');
          window.dispatchEvent(new Event('cd-drawer-closed'));
        }};
        {DRAWER_LISTENER}
        window.collectDrawerPopoverLayout = () => {{
          const panel = document.getElementById('panel');
          const menu = pop;
          const mr = menu.getBoundingClientRect(), pr = panel.getBoundingClientRect();
          const x = Math.min(mr.right - 8, Math.max(pr.right + 20, mr.left + 20));
          const y = mr.top + 20;
          const hit = document.elementFromPoint(x, y);
          const hitMenu = !!hit?.closest('.resume-pop');
          const menuZ = getComputedStyle(menu).zIndex;
          if (hitMenu) hit.dispatchEvent(new MouseEvent('click', {{bubbles:true}}));
          window.webkit.messageHandlers.result.postMessage({{
            mobile: innerWidth <= 820,
            extendsPanel: mr.right > pr.right + 1,
            hitMenu,
            menuZ,
            panelZ: getComputedStyle(panel).zIndex,
            selected,
            drawerClosed: !document.body.classList.contains('mo-drawer'),
            menuClosed: !document.querySelector('.resume-pop')
          }});
        }};
      </script></body>'''
    page = tmp_path / "mobile-drawer-popover.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "mobile-drawer-popover"
    fixture = Path(__file__).parent / "fixtures/mobile_drawer_popover_webkit.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(binary)], capture_output=True, text=True,
                   check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["mobile"] and data["extendsPanel"], data
    assert data["hitMenu"], data
    assert int(data["menuZ"]) > 60, data
    assert data["selected"] == 1, data
    assert data["drawerClosed"] and data["menuClosed"], data
