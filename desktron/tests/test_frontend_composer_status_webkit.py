"""问询位于上下文托盘上方，长来源不能挤掉阶段或撑开输入框。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend/app-chat-page.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")


class 模板顺序(unittest.TestCase):
    def test_跨会话行没有右侧说明(self):
        self.assertNotIn("hs-peer-note", PAGE + CSS)
        self.assertNotIn("后台处理，不影响当前会话", PAGE)

    def test_问询不再隔开托盘与输入框(self):
        peer = PAGE.index('class="hs-peer hs-col"')
        context = PAGE.index('class="hs-context hs-col"')
        box = PAGE.index('class="hs-box hs-col"')
        self.assertLess(peer, context)
        self.assertLess(context, box)

    def test_来源与阶段分别展示(self):
        self.assertIn('<span class="hs-peer-main"></span><span class="hs-peer-phase"></span>', PAGE)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_webkit问询不撑开托盘且长来源不遮住状态(tmp_path):
    # 直接取生产模板，避免夹具继续沿用旧版 DOM 顺序。
    start = PAGE.index("'<div class=\"hs-composer\">'")
    end = PAGE.index("'<div class=\"hs-box hs-col\">'", start)
    template = PAGE[start:end] + "'<div class=\"hs-box hs-col\">'"
    composer = subprocess.check_output([
        'node', '-e', 'const HS_ICONS={chev:""};console.log(' + template + ')'
    ], text=True)
    composer += '<div class="hs-inp">写点什么…</div><div class="hs-row"></div></div></div>'
    script = '''
      const rect = el => el.getBoundingClientRect();
      window.collectComposerLayout = () => {
        const results = [];
        const page = document.querySelector('.hs-page');
        const peer = document.querySelector('.hs-peer');
        const dot = document.querySelector('.hs-peer-dot');
        const rail = document.querySelector('.hs-context-rail');
        const tasks = document.querySelector('.hs-tasks');
        const taskIcon = document.querySelector('.hs-context-task-icon');
        const box = document.querySelector('.hs-box');
        const main = document.querySelector('.hs-peer-main');
        const phase = document.querySelector('.hs-peer-phase');
        const env = document.querySelector('.hs-env');
        const detail = document.querySelector('.hs-context-status-detail');
        const title = document.querySelector('.hs-context-status-title');
        const pop = document.querySelector('.hs-context-status-popover');
        main.textContent = 'desktop · Desk 迭代 · Shepherd · Shepherd 会话注入架构盘点 · 很长的来源标题';
        title.textContent = '旧待发消息恢复未完成';
        detail.textContent = '当前服务无法恢复旧待发消息（HTTP 404），请确认运行端版本后重试。';
        env.hidden = false; env.classList.add('on');
        tasks.hidden = false;
        pop.hidden = false;
        for (const width of [320, 390, 1000]) {
          page.style.width = width + 'px';
          for (const theme of ['dark', 'light']) {
            document.documentElement.dataset.theme = theme;
            for (const state of ['等待处理', '正在处理']) {
              phase.textContent = state;
              peer.hidden = false;
              const composer = document.querySelector('.hs-composer');
              const heightWithPeer = rect(composer).height;
              results.push({width, theme, state,
                peerHeight: rect(peer).height,
                bounds: [peer,rail,box,pop].map(el => [el.className,rect(el).left,rect(el).right]),
                peerFits: peer.scrollWidth <= peer.clientWidth,
                peerAbove: rect(peer).bottom <= rect(rail).top,
                railAttached: Math.abs(rect(rail).bottom - rect(box).top) <= 1,
                sourceClipped: main.scrollWidth > main.clientWidth,
                phaseVisible: phase.scrollWidth <= phase.clientWidth && rect(phase).right <= rect(peer).right,
                phaseAdjacent: Math.abs(rect(phase).left - rect(main).right - 7) <= 1,
                dotAligned: Math.abs((rect(dot).left + rect(dot).width / 2) -
                                     (rect(taskIcon).left + rect(taskIcon).width / 2)) <= .5,
                rowsInside: [peer,rail,box,pop].every(el => rect(el).left >= 0 && rect(el).right <= width),
                titleFits: title.scrollWidth <= title.clientWidth,
                detailFits: detail.scrollWidth <= detail.clientWidth,
                envHidden: getComputedStyle(env).display === 'none',
              });
              peer.hidden = true;
              const last = results[results.length - 1];
              last.hiddenCollapses = rect(peer).height === 0 && rect(composer).height < heightWithPeer;
              last.stillAttached = Math.abs(rect(rail).bottom - rect(box).top) <= 1;
            }
          }
        }
        window.webkit.messageHandlers.result.postMessage({results});
      };
    '''
    html = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>%s</style>
      <div class="overlay-content hs-page" style="height:844px;position:relative"><div class="hs-body"></div>%s</div>
      <script>%s</script>''' % (CSS, composer, script)
    page = tmp_path / "composer-status.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "composer-status"
    fixture = Path(__file__).parent / "fixtures/composer_status_webkit.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(binary)], capture_output=True, text=True,
                   check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "error" not in data, data
    assert len(data["results"]) == 12, data
    for layout in data["results"]:
        assert layout['peerHeight'] <= 26, layout
        for key in ('peerFits', 'peerAbove', 'railAttached', 'phaseVisible', 'phaseAdjacent', 'dotAligned', 'rowsInside',
                    'titleFits', 'detailFits', 'envHidden', 'hiddenCollapses', 'stillAttached'):
            assert layout[key], (key, json.dumps(layout, ensure_ascii=False))
        if layout['width'] < 400:
            assert layout['sourceClipped'], layout


if __name__ == "__main__":
    unittest.main()
