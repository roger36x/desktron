"""子代理详情的阅读排版必须与主会话正文一致。

子代理最终回复虽然使用同一个 `.hs-text` 类，但它不在 `.hs-stream` 内，而且为了把过程与
结论放在同一条时间线中，DOM 还嵌在 `.hs-proc` 下。前者会让字号和颜色退回 body，后者会
把行高压成过程标题的 1.3。静态类名相同不能证明实际排版一致，因此用原生 WebKit 比较
两处计算样式。
"""
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")


def test_agent_view_declares_the_main_reading_scale():
    assert ".hs-page .hs-agv-b { flex: 1; overflow-y: auto;" in CSS
    block = CSS.split(".hs-page .hs-agv-b {", 1)[1].split("}", 1)[0]
    for value in ("font-size: 14.5px", "line-height: 1.6", "color: var(--ink-100)"):
        assert value in block
    result = CSS.split(".hs-page .hs-agv-result .hs-text {", 1)[1].split("}", 1)[0]
    assert "color: var(--ink-100)" in result
    markdown = CSS.split(".hs-page .hs-agv-result .hs-text.md {", 1)[1].split("}", 1)[0]
    assert "line-height: 1.6" in markdown


@pytest.mark.desktop
@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                    reason="需要 macOS WebKit")
def test_webkit_agent_answer_matches_main_typography(tmp_path):
    html = f'''<!doctype html><meta charset="utf-8"><style>
      html,body{{margin:0;width:100%;height:100%;}} {CSS}
      .fixture{{width:1200px;height:800px;}}
      .fixture .hs-agview{{position:relative;height:400px;}}
      .fixture .hs-agv-result{{display:block;}}
    </style>
    <div class="overlay-content hs-page fixture">
      <div class="hs-wrap"><main class="hs-body"><div class="hs-stream hs-col">
        <section class="hs-turn">
          <div class="hs-u"><div class="hs-bubble"><div id="main-prompt" class="hs-u-body md"><p>任务内容</p></div></div></div>
          <div class="hs-a">
            <div class="hs-proc open">
              <div id="main-status" class="hs-proc-bar"><span class="hs-status st-done"><i class="hs-dot"></i><span class="hs-st-t">已处理 2 步</span></span></div>
              <div class="hs-proc-body">
                <div class="hs-think"><div id="main-process" class="hs-think-body md"><p>处理过程</p></div></div>
                <div class="hs-step"><div id="main-tool" class="hs-step-h"><span class="hs-step-n">读取</span><span class="hs-step-a txt">文件内容</span></div></div>
              </div>
            </div>
            <div id="main-answer" class="hs-text md"><p>正文段落</p><p>第二段</p></div>
          </div>
        </section>
      </div></main></div>
      <div class="hs-agview"><div class="hs-agv-b"><section class="hs-agv-turn">
        <div class="hs-u hs-u-peer hs-agv-prompt"><div class="hs-bubble"><div id="agent-prompt" class="hs-u-body md"><p>任务内容</p></div></div></div>
        <div class="hs-a"><div class="hs-proc hs-agv-proc open">
          <div id="agent-status" class="hs-proc-bar"><span class="hs-status st-done"><i class="hs-dot"></i><span class="hs-st-t">已处理 2 步</span></span></div>
          <div class="hs-proc-body">
            <div class="hs-think hs-agv-r"><div id="agent-process" class="hs-think-body md"><p>处理过程</p></div></div>
            <div class="hs-step hs-agv-s"><div id="agent-tool" class="hs-step-h"><span class="hs-step-n">读取</span><span class="hs-step-a txt">文件内容</span></div></div>
          </div>
          <div class="hs-agv-result"><div id="agent-answer" class="hs-text md hs-agv-p"><p>正文段落</p><p>第二段</p></div></div>
        </div></div>
      </section></div></div>
    </div>
    <script>
      setTimeout(() => {{
        const metric = id => {{
          const el = document.getElementById(id);
          const style = getComputedStyle(el);
          const paragraphNode = el.querySelector('p');
          const paragraph = paragraphNode ? getComputedStyle(paragraphNode) : null;
          return {{fontFamily: style.fontFamily, fontSize: style.fontSize,
            fontWeight: style.fontWeight, lineHeight: style.lineHeight,
            letterSpacing: style.letterSpacing, color: style.color,
            margin: style.margin, paragraphMargin: paragraph ? paragraph.margin : null}};
        }};
        window.webkit.messageHandlers.result.postMessage({{
          main: metric('main-answer'), agent: metric('agent-answer'),
          mainPrompt: metric('main-prompt'), agentPrompt: metric('agent-prompt'),
          mainStatus: metric('main-status'), agentStatus: metric('agent-status'),
          mainProcess: metric('main-process'), agentProcess: metric('agent-process'),
          mainTool: metric('main-tool'), agentTool: metric('agent-tool')
        }});
      }}, 100);
    </script>'''
    page = tmp_path / "agent-typography.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "agent-typography"
    fixture = ROOT / "desktron/tests/fixtures/chat_latency_webkit.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(binary)], capture_output=True,
                   text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["agent"] == data["main"], data
    typography = ("fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
                  "margin", "paragraphMargin")
    for left, right in (("agentPrompt", "mainPrompt"),
                        ("agentStatus", "mainStatus"),
                        ("agentProcess", "mainProcess"),
                        ("agentTool", "mainTool")):
        assert {key: data[left][key] for key in typography} == {
            key: data[right][key] for key in typography
        }, data
    assert data["main"]["fontSize"] == "14.5px"
    assert data["main"]["fontWeight"] == "450"
    assert math.isclose(float(data["main"]["lineHeight"].removesuffix("px")), 23.2,
                        abs_tol=0.01)
