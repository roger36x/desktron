"""会话里的 citation、URL、hash 等无空格长串不能冲出正文列。

用户气泡早已有桌面端兜底，主回复、思考、压缩摘要和子代理结果却只在窄屏时允许
任意断行。桌面端外层又是 overflow-x:hidden，于是长串不是横滚，而是越过 760px
内容列后被静默裁掉。这里同时锁定语义规则和真实 WebKit 几何。
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")


def rule(selector):
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, f"missing CSS rule: {selector}"
    return match.group(1)


def test_chat_markdown_narrative_has_desktop_long_token_fallback():
    narrative = rule(".hs-page .md")
    assert "overflow-wrap: anywhere" in narrative
    assert re.search(r"\n\.hs-page \.md\s*\{", CSS), "规则必须位于顶层，不能只在窄屏生效"


def test_single_line_and_path_exceptions_remain_intentional():
    step = rule(".hs-page .hs-step-a")
    for value in ("overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"):
        assert value in step
    assert "white-space: nowrap" in rule(".hs-page .hs-text.md code.hs-path")
    full = rule(".hs-page .hs-text.md code.hs-path.hs-path-full")
    assert "white-space: normal" in full
    assert "overflow-wrap: anywhere" in full
    assert "overflow-x: auto" in rule(".md pre.hljs")
    assert "overflow-x: auto" in rule(".md table")


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="需要 macOS WebKit")
@pytest.mark.desktop
def test_webkit_desktop_narrative_wraps_without_page_clipping(tmp_path):
    token = "shr1." + "Ab9_" * 500
    html = f'''<!doctype html><meta charset="utf-8"><style>
      html,body{{margin:0;width:100%;height:100%;}} {CSS}
      .fixture{{width:1200px;height:800px;}}
      .fixture .hs-body{{height:760px;}}
      .fixture .hs-stream{{box-sizing:border-box;}}
      .fixture .hs-proc-body{{display:block;}}
      .fixture .hs-think-body{{display:block;}}
      .fixture .hs-cmp-b{{display:block;}}
    </style>
    <div class="overlay-content hs-page fixture">
      <div class="hs-wrap"><main class="hs-body"><div class="hs-stream hs-col">
        <section class="hs-turn"><div class="hs-a">
          <div id="answer" class="hs-text md"><p>{token}</p></div>
          <div class="hs-proc open"><div class="hs-proc-body">
            <div class="hs-think"><div id="thinking" class="hs-think-body md"><p>{token}</p></div></div>
            <div id="compact" class="hs-cmp-b md"><p>{token}</p></div>
          </div></div>
        </div></section>
        <section class="hs-agv-turn"><div class="hs-agv-result"><div id="agent" class="hs-text md hs-agv-p"><p>{token}</p></div></div></section>
      </div></main></div>
    </div>
    <script>
      window.collectNarrativeLayout = () => {{
        const col = document.querySelector('.hs-stream');
        const body = document.querySelector('.hs-body');
        const lineHeight = parseFloat(getComputedStyle(document.querySelector('#answer')).lineHeight);
        const rows = ['answer','thinking','compact','agent'].map(id => {{
          const el = document.getElementById(id), p = el.querySelector('p');
          const er = el.getBoundingClientRect(), cr = col.getBoundingClientRect();
          return {{id, fits: el.scrollWidth <= el.clientWidth + 1,
            inside: er.right <= cr.right + 1, wraps: p.getBoundingClientRect().height > lineHeight * 2}};
        }});
        window.webkit.messageHandlers.result.postMessage({{
          desktop: innerWidth > 820,
          rows,
          pageFits: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
          bodyFits: body.scrollWidth <= body.clientWidth + 1
        }});
      }};
    </script>'''
    page = tmp_path / "chat-narrative-wrap.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "chat-narrative-wrap"
    fixture = Path(__file__).parent / "fixtures/chat_narrative_wrap_webkit.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(binary)], capture_output=True, text=True,
                   check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["desktop"], data
    assert data["pageFits"] and data["bodyFits"], data
    for row in data["rows"]:
        assert row["fits"] and row["inside"] and row["wraps"], row
