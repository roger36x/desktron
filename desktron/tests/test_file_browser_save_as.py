"""文件浏览器另存为：字节保真、路径边界、下载响应及前端失败处理。"""
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, urlencode

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron/lib"))
_argv = sys.argv
sys.argv = ["test", "/tmp/desk-save-as-frontend", "0", "0", "/tmp/desk-save-as-repo"]
try:
    import assets
    from core import api as core_api
finally:
    sys.argv = _argv


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    monkeypatch.setattr(assets, "REPO_ROOT", str(root))
    monkeypatch.setattr(assets, "_view_roots", lambda: [str(root)])
    monkeypatch.setattr(assets, "_fs_base", lambda key: str(root) if key == "fixture" else None)
    return root


@pytest.mark.parametrize("name,data", [
    ("方案.md", "# 原文\r\n正文\r\n".encode()),
    ("demo.html", b"<!doctype html><style>body{color:red}</style><script>run()</script>"),
    ("archive.bin", bytes(range(256)) * 4),
    ("empty.txt", b""),
    ("large.png", b"\x89PNG\r\n\x1a\n" + b"x" * (9 * 1024 * 1024)),
], ids=['unicode-markdown', 'html', 'binary', 'empty', 'large-png'])
def test_download_keeps_every_original_byte(workspace, name, data):
    path = workspace / name
    path.write_bytes(data)
    assert assets._wb_view(name, root_id="fixture", download=True) == ("application/octet-stream", data)
    assert path.read_bytes() == data


@pytest.mark.parametrize("path,root_id", [
    ("missing.bin", None), ("", "fixture"), ("../outside.bin", "fixture"),
    ("../outside.bin", None), ("anything.bin", "unknown"),
])
def test_download_refuses_missing_directory_and_out_of_scope(workspace, path, root_id):
    (workspace.parent / "outside.bin").write_bytes(b"outside")
    assert assets._wb_view(path, root_id=root_id, download=True) is None


class Handler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


def test_attachment_response_preserves_unicode_name_and_prevents_header_injection(monkeypatch):
    name = '方案 "最终"\r\nX-Fake: yes.html'
    content = b"<h1>original</h1>"
    monkeypatch.setattr(core_api, "_wb_view", lambda *args, **kwargs: ("application/octet-stream", content))
    handler = Handler()
    core_api.handle(handler, "/workbench/view", urlencode({"root": "fixture", "path": name, "download": "1"}), "GET")
    assert handler.status == 200
    assert handler.headers["Content-Type"] == "application/octet-stream"
    assert handler.headers["Content-Length"] == str(len(content))
    assert handler.headers["Content-Disposition"] == "attachment; filename*=UTF-8''" + quote(name, safe="")
    assert "\r" not in handler.headers["Content-Disposition"]
    assert "\n" not in handler.headers["Content-Disposition"]
    assert handler.wfile.getvalue() == content


def test_normal_html_preview_keeps_its_existing_response(workspace):
    content = b"<h1>preview</h1>"
    (workspace / "demo.html").write_bytes(content)
    handler = Handler()
    core_api.handle(handler, "/workbench/view", "root=fixture&path=demo.html", "GET")
    assert handler.status == 200
    assert handler.headers["Content-Type"].startswith("text/html")
    assert "Content-Disposition" not in handler.headers
    assert handler.wfile.getvalue() == content


@pytest.mark.parametrize("path", ["missing.txt", ""])
def test_failed_download_never_returns_a_successful_attachment(workspace, path):
    handler = Handler()
    core_api.handle(handler, "/workbench/view", urlencode({"root": "fixture", "path": path, "download": "1"}), "GET")
    assert handler.status == 404
    assert "Content-Disposition" not in handler.headers


@pytest.mark.skipif(not shutil.which("node"), reason="需要 Node")
def test_frontend_download_bytes_duplicate_click_failure_and_cleanup():
    source = (ROOT / "frontend/pages/file-browser.html").read_text(encoding="utf-8")
    functions = "\n".join(re.search(r"  (?:async )?function " + name + r"\([^\n]*\{.*?^  }", source, re.S | re.M).group(0)
                          for name in ("showSaveStatus", "saveFileAs"))
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const functions=JSON.parse(process.argv[1]);
async function run(mode){
 const status={hidden:true,textContent:''},links=[],urls=[],revoked=[],timeouts=[];
 const bytes=Buffer.from([0,255,13,10,128]),blob={bytes}; let calls=0,complete;
 const host={document:{createElement(){const link={click(){link.clicked=true},remove(){link.removed=true}};links.push(link);return link;},body:{appendChild(){}}},URL:{createObjectURL(value){assert.equal(value,blob);urls.push('blob:download');return 'blob:download'},revokeObjectURL(value){revoked.push(value)}},setTimeout(fn,ms){timeouts.push([fn,ms])}};
 const context={parent:host,window:{},document:{getElementById(){return status}},clearTimeout(){},setTimeout(){},
 tr(key,params,fallback){return fallback},closeOpenAppMenu(){},renderOpenApps(){},
 fetch:async(url)=>{calls++;context.lastUrl=url;if(mode==='deferred')await new Promise(resolve=>complete=resolve);
 return {ok:mode!=='404',status:404,headers:{get(){return mode==='old'?'':'attachment; filename=test'}},async blob(){if(mode==='blob-error')throw Error('read failed');return blob}}}};
 vm.createContext(context);vm.runInContext('let current="docs/方案 #1.bin", root="abs:/repo A", currentIsDir=false, savingFile=false, saveStatusTimer;'+functions,context);
 if(mode==='directory')vm.runInContext('currentIsDir=true',context);
 const first=context.saveFileAs();
 if(mode==='deferred'){await context.saveFileAs();assert.equal(calls,1);vm.runInContext('current="other.txt";root="other"',context);complete();}
 await first;
 if(mode==='404'||mode==='old'||mode==='blob-error'){
   assert.equal(links.length,0);assert.equal(status.hidden,false);assert.match(status.textContent,mode==='old'?/不支持/:/失败/);
 }else if(mode==='directory'){assert.equal(calls,0);assert.equal(links.length,0);}
 else{
  assert.equal(links.length,1);assert.equal(links[0].download,'方案 #1.bin');assert.equal(links[0].clicked,true);assert.equal(links[0].removed,true);
  const params=new URL('http://test'+context.lastUrl).searchParams;assert.equal(params.get('root'),'abs:/repo A');assert.equal(params.get('path'),'docs/方案 #1.bin');assert.equal(params.get('download'),'1');
  assert.equal(timeouts[0][1],30000);timeouts[0][0]();assert.deepEqual(revoked,urls);
 }
 assert.equal(vm.runInContext('savingFile',context),false);
}
(async()=>{for(const mode of ['ok','404','old','blob-error','directory','deferred'])await run(mode)})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([shutil.which("node"), "-e", script, json.dumps(functions)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
