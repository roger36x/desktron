"""制品真实入口 → 文件浏览器 → 隔离后端的阅读回归，不使用运行端资料。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
pytestmark = pytest.mark.desktop


@pytest.fixture(scope="module")
def isolated_backend():
    # macOS AF_UNIX 的路径上限较短；pytest 的深层临时目录会超过 App Control socket 上限。
    sandbox = Path(tempfile.mkdtemp(prefix="dap-", dir="/tmp"))
    spec = importlib.util.spec_from_file_location(
        "artifact_install_check", ROOT / "desktron/lib/provision/install_check.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    env = installer.isolated_env(ROOT, sandbox)
    workspace = Path(env["DESKTRON_WORKSPACE"])
    external = sandbox / "external-repo"
    external.mkdir()
    registry = workspace / ".workbench/domains.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps([
        {"id": "wf-local", "name": "Local"},
        {"id": "wf-repo", "name": "Repo", "dir": str(external)},
    ]), encoding="utf-8")
    for domain in ("_desktop", "wf-local", "wf-repo"):
        directory = workspace / "domains" / domain / "artifacts"
        directory.mkdir(parents=True)
        (directory / "升级顺序 总表.md").write_text(
            "# 阅读回归\n\n| 项目 | 顺序 |\n| --- | --- |\n| 引擎 | 1 |\n", encoding="utf-8")
        (directory / "demo").mkdir()
        (directory / "demo/index.html").write_text("<h1>目录 Demo 回归</h1>", encoding="utf-8")
        (directory / "证据包").mkdir()
        (directory / "证据包/说明.txt").write_text("目录回归", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # 浏览器依赖可以来自独立 venv；后端显式选择安装了其运行依赖的解释器，不借 site-packages。
    python = os.environ.get("DESK_ARTIFACT_TEST_PYTHON", sys.executable)
    with (sandbox / "backend.log").open("wb") as log:
        proc = subprocess.Popen(
            [python, str(ROOT / "desktron/lib/ui_server.py"), str(FRONT), str(port), "0", str(workspace)],
            env=env, cwd=workspace, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert proc.poll() is None, "隔离后端启动失败：" + installer._diagnostic(
                    sandbox / "backend.log", ROOT, sandbox)
                try:
                    with opener.open(base + "/desk-probe", timeout=1) as response:
                        probe = json.load(response)
                except OSError:
                    time.sleep(.05)
                    continue
                assert probe["pid"] == proc.pid and probe["desk"] is True
                assert probe["supervised"] is False
                break
            else:
                pytest.fail("隔离后端启动超时")
            # 证明外部根确实与制品存储根不同，而不是用普通工作流冒充外部仓。
            with opener.open(base + "/workbench/fs?root=wf-repo&path=", timeout=3) as response:
                assert json.load(response)["items"] == []
            yield base
        finally:
            installer._stop(proc)
            shutil.rmtree(sandbox, ignore_errors=True)


@pytest.fixture
def browser_page(isolated_backend):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            page.goto(isolated_backend + "/workbench/page?id=file-browser&root=_desktop")
            page.add_script_tag(path=str(FRONT / "app-chat-situation.js"))
            # 真实制品点击入口，宿主仅捕获打开参数，再进入真实文件浏览器页面。
            page.evaluate("""() => {
                window.artifactOpenCalls = [];
                window.openFileBrowser = (...args) => window.artifactOpenCalls.push(args);
            }""")
            yield page
        finally:
            browser.close()


def open_artifact(page, base, domain, ref):
    item = page.evaluate("""async ({domain, ref}) => {
        const response = await fetch('/workbench/artifacts?' + new URLSearchParams({domain}));
        if (!response.ok) throw new Error('制品列表 HTTP ' + response.status);
        const data = await response.json();
        const item = data.items.find(x => x.ref === ref);
        if (!item) throw new Error('缺少制品 ' + ref);
        hsSituationOpenArtifact({situation: {domain}}, item);
        return item;
    }""", {"domain": domain, "ref": ref})
    calls = page.evaluate("window.artifactOpenCalls")
    assert len(calls) == 1
    root, path, line, show, isdir = calls[0]
    assert root == "_desktop" and show is True
    query = {"id": "file-browser", "root": root, "path": path}
    if isdir:
        query["dir"] = "1"
    page.goto(base + "/workbench/page?" + urlencode(query))
    return item, path


@pytest.mark.parametrize("domain", ["_desktop", "wf-local", "wf-repo"])
def test_markdown_heading_and_table(browser_page, isolated_backend, domain):
    page = browser_page
    reads = []
    page.on("response", lambda response: reads.append(response)
            if "/workbench/fs?" in response.url and "file=1" in response.url else None)
    _, path = open_artifact(page, isolated_backend, domain, "升级顺序 总表.md")
    page.locator("article.doc h1").wait_for(state="visible")
    assert page.locator("article.doc h1").inner_text() == "阅读回归"
    assert page.locator("article.doc table tbody td").all_text_contents() == ["引擎", "1"]
    assert path == f"domains/{domain}/artifacts/升级顺序 总表.md"
    assert reads and all(response.status == 200 for response in reads)
    assert page.get_by_text("无法预览此文件", exact=True).count() == 0


def test_directory_demo_keeps_html_entry(browser_page, isolated_backend):
    page = browser_page
    item, path = open_artifact(page, isolated_backend, "wf-repo", "demo")
    assert item["entry"] == "artifacts/demo/index.html"
    assert path == "domains/wf-repo/artifacts/demo/index.html"
    assert page.frame_locator("iframe.web").locator("h1").inner_text() == "目录 Demo 回归"


def test_directory_without_entry_keeps_tree_mode(browser_page, isolated_backend):
    page = browser_page
    _, path = open_artifact(page, isolated_backend, "wf-local", "证据包")
    page.get_by_text("已定位到左侧目录。选择其中的文件即可预览。", exact=True).wait_for()
    assert path == "domains/wf-local/artifacts/证据包"
    row = page.locator('.tree-row.dir[data-path="domains/wf-local/artifacts/证据包"]')
    row.wait_for()
    assert row.count() == 1
