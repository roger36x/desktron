"""会话页右侧信息卡上的「制品」。

2026-09-10 用户提出:每个工作流要有一处集中管理产物的地方(文档、demo、预览页,不含代码),
并定下这一栏叫「制品」、就放在这张卡上、把原来的「记忆」换掉。

三处口径由这些用例钉住:

1. 清单跟着工作流走,不跟着会话走——与同一张卡上的「项目」同源(situation.domain)。
2. 一次请求。制品与项目一起从 /session/<id>/situation 来;单独那条 /workbench/artifacts
   只在后端还没重启、以及写入后就地刷新时用。前端资源可热更新而后端要等重启,
   这段跨版本窗口里卡片不能整块失效。
3. 本地文件与外部链接点开的去处不同:文件进文件浏览器,链接交给外部打开。同列一张清单,
   但不能把一个 https 链接塞给文件浏览器。
"""
import json
import os
import shutil
import subprocess
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
UI = _srcmap.ui_server_src()
API = open(os.path.join(ROOT, "desktron", "lib", "workflow", "api.py"), encoding="utf-8").read()
ORIENT = open(os.path.join(ROOT, "common", "orientation.md"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()


class 卡片(unittest.TestCase):
    def test_这一栏叫制品(self):
        self.assertIn('<h2>制品</h2>', CHAT)
        self.assertIn('hs-sit-sec hs-sit-artifacts', CHAT)

    def test_默认四条且可展开收起(self):
        """与「项目」同一套行为,只是限额不同——制品比项目更容易堆多。"""
        self.assertIn("const HS_SIT_ARTIFACT_LIMIT = 4", CHAT)
        self.assertIn("artifacts.classList.toggle('open')", CHAT)
        self.assertIn(".hs-sit-artifacts.open .hs-sit-artifact-extra { display: flex; }", CSS)

    def test_清单跟工作流不跟会话(self):
        self.assertIn("const domain = String(((v.situation || {}).domain) || '')", CHAT)
        self.assertIn("'/workbench/artifacts?domain=' + encodeURIComponent(domain)", CHAT)

    def test_一次请求够用_单独接口只作跨版本兜底(self):
        self.assertIn("if (Array.isArray((v.situation || {}).artifacts))", CHAT)
        self.assertIn('"artifacts": items', UI)
        self.assertIn("_artifacts.list_items(workspace)", UI)

    def test_切工作流途中回来的旧结果丢弃(self):
        """卡片可能在取数途中被切到另一个工作流。世代号对不上就丢弃,
        不把上一个工作流的清单画到这一个上。"""
        self.assertIn("const gen = (v.artifactGen = (v.artifactGen || 0) + 1)", CHAT)
        self.assertIn("if (gen !== v.artifactGen || !list.isConnected) return;", CHAT)

    def test_文件与链接各去各处(self):
        self.assertIn("if (item.external) {", CHAT)
        self.assertIn("openExternal(item.ref)", CHAT)
        self.assertIn("openFileBrowser('_desktop', 'domains/' + domain + '/' + path, '', true, !item.entry && item.kind === 'dir')", CHAT)
        self.assertIn("dir: '目录'", CHAT)

    def test_空态说清楚往哪写(self):
        """空清单只说「还没有」等于没说:人下一步要知道东西该放哪。"""
        self.assertIn("写进工作流的 artifacts/ 目录即可出现在这里", CHAT)


@unittest.skipUnless(shutil.which("node"), "需要 Node 验证制品入口")
class 制品打开路径(unittest.TestCase):
    def _open(self, domain, item):
        # 执行真实入口和宿主 URL 构造函数，不以源码字符串断言代替路径行为。
        script = r'''
const fs = require('fs'), vm = require('vm');
const args = JSON.parse(process.argv[1]);
const ctx = {
  calls: [],
  openFileBrowser: (...args) => ctx.calls.push({type: 'file', args}),
  openExternal: ref => ctx.calls.push({type: 'external', ref}),
  FILE_BROWSER_PAGE_KEY: 'file-browser',
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(args.source, 'utf8'), ctx);
const host = fs.readFileSync(args.host, 'utf8');
vm.runInContext(host.match(/function fileBrowserSrc\(x\) \{[\s\S]*?\n\}/)[0], ctx);
ctx.hsSituationOpenArtifact({situation: {domain: args.domain}}, args.item);
for (const call of ctx.calls) {
  if (call.type === 'file') {
    const [root, path, line, show, dir] = call.args;
    call.url = ctx.fileBrowserSrc({root, path, line, dir});
  }
}
console.log(JSON.stringify(ctx.calls));
'''
        data = {"source": os.path.join(ROOT, "frontend", "app-chat-situation.js"),
                "host": os.path.join(ROOT, "frontend", "app-files.js"),
                "domain": domain, "item": item}
        result = subprocess.run([shutil.which("node"), "-e", script, json.dumps(data)],
                                capture_output=True, text=True, check=True, timeout=10, encoding='utf-8')
        return json.loads(result.stdout)

    def test_desktop中文Markdown使用工作区完整路径(self):
        from urllib.parse import parse_qs, urlparse
        calls = self._open("_desktop", {"path": "artifacts/升级顺序 总表.md", "kind": "doc"})
        self.assertEqual(calls[0]["args"],
                         ["_desktop", "domains/_desktop/artifacts/升级顺序 总表.md", "", True, False])
        query = parse_qs(urlparse(calls[0]["url"]).query)
        self.assertEqual(query["root"], ["_desktop"])
        self.assertEqual(query["path"], ["domains/_desktop/artifacts/升级顺序 总表.md"])

    def test_普通及引用外部仓库工作流均不借用项目根(self):
        # 调用方无需知道工作流是否配置外部 dir；制品存储位置完全相同。
        for domain in ("wf-local", "wf-repo", "wf-repo:branch"):
            with self.subTest(domain=domain):
                calls = self._open(domain, {"path": "artifacts/方案.md", "kind": "doc"})
                self.assertEqual(calls[0]["args"],
                                 ["_desktop", "domains/" + domain.split(":")[0] + "/artifacts/方案.md",
                                  "", True, False])

    def test_demo优先入口且不是目录模式(self):
        calls = self._open("wf-repo", {"path": "artifacts/demo", "entry": "artifacts/demo/index.html",
                                       "kind": "demo"})
        self.assertEqual(calls[0]["args"],
                         ["_desktop", "domains/wf-repo/artifacts/demo/index.html", "", True, False])

    def test_无入口目录继续打开目录树(self):
        calls = self._open("wf-local", {"path": "artifacts/证据包", "entry": "", "kind": "dir"})
        self.assertEqual(calls[0]["args"],
                         ["_desktop", "domains/wf-local/artifacts/证据包", "", True, True])
        self.assertIn("&dir=1", calls[0]["url"])

    def test_外链不加本地路径(self):
        item = {"external": True, "ref": "https://preview.example.test/report", "path": ""}
        self.assertEqual(self._open("wf-repo", item), [{"type": "external", "ref": item["ref"]}])

    def test_缺少工作流或路径不打开错误根目录(self):
        for domain, item in (("", {"path": "artifacts/a.md"}), ("wf", {"path": ""}), ("wf", None)):
            with self.subTest(domain=domain, item=item):
                self.assertEqual(self._open(domain, item), [])


class 折叠(unittest.TestCase):
    """制品会堆多,所以标题整块是折叠开关(2026-09-10 用户提)。

    与「查看全部 N 件」不是一回事:后者在展开态里放宽 4 条的限额,前者收起整栏。
    形制取待审批那一区的同一套——按标题、箭头转九十度,不另造一种折叠观感。
    """

    def test_标题整块是开关(self):
        self.assertIn('class="hs-sit-head hs-sit-artifact-head"', CHAT)
        self.assertIn('<svg class="appr-chev"', CHAT)
        self.assertIn(".hs-sit-artifacts.collapsed .hs-sit-artifact-head .appr-chev "
                      "{ transform: rotate(-90deg); }", CSS)
        self.assertIn(".hs-sit-artifacts.collapsed .hs-sit-artifact-list,\n"
                      ".hs-sit-artifacts.collapsed .hs-sit-artifact-more { display: none; }", CSS)

    def test_折起来之后不会自己展开(self):
        """折叠是一个持续的意愿,不是这一页的临时状态:换页、重开都该还是折着的。"""
        self.assertIn("const HS_SIT_ARTIFACT_FOLD_KEY = 'cd_sit_artifacts_folded'", CHAT)
        self.assertIn("hsSituationArtifactsFolded() ? ' collapsed' : ''", CHAT)
        self.assertIn("localStorage.setItem(HS_SIT_ARTIFACT_FOLD_KEY", CHAT)

    def test_开着的其它会话页一起跟上(self):
        """每个会话页各有一张卡。只改当前这张的话,切过去看到的还是上一个状态,像是没记住。"""
        self.assertIn("hsSituationApplyFold(v && v.el, folded)", CHAT)
        self.assertIn("for (const view of _hsViews.values())", CHAT)

    def test_数量与审批那枚同一枚胶囊(self):
        """同一张卡上同一种「有几件」,一处胶囊一处裸字会读成两种东西(2026-09-10 用户指)。
        共用一条规则而不是复制一份取值:复制迟早只改一边。"""
        self.assertIn(".hs-sit-approval-count, .hs-sit-artifact-count {", CSS)
        self.assertIn('html[data-theme="light"] .hs-sit-artifact-count', CSS)

    def test_数不出来时不留空胶囊(self):
        """空胶囊是一枚没有内容的灰块,比不显示更费解。"""
        self.assertIn('<span class="hs-sit-artifact-count" hidden></span>', CHAT)
        self.assertIn("count.textContent = String(items.length);", CHAT)
        self.assertIn("if (count) count.hidden = true;", CHAT)

    def test_折起时数量仍在标题上(self):
        """收起来还能一眼看到有几件,否则折叠等于把这一栏藏没了。"""
        head = CHAT[CHAT.index('hs-sit-artifact-head'):CHAT.index('hs-sit-artifact-list')]
        self.assertIn('hs-sit-artifact-count', head)


class 接口(unittest.TestCase):
    def test_读写都在同一条路由上(self):
        self.assertIn('"/workbench/artifacts"', API)
        self.assertIn('if p == "/workbench/artifacts":', API)
        for op in ('op == "link"', 'op == "meta"', 'op in ("hide", "show")'):
            self.assertIn(op, API)

    def test_非法工作流拒掉而不是落到别处(self):
        self.assertIn('if not artifacts.artifacts_dir(did):', API)
        self.assertIn('"domain 非法"', API)


class 约定(unittest.TestCase):
    def test_起手地图告诉agent往哪写(self):
        """产出即登记:写文件那一步本身就是登记,不另设一个要人记得调的动作。"""
        self.assertIn("### 制品", ORIENT)
        self.assertIn("desk-artifact dir", ORIENT)
        self.assertIn("desk-artifact link", ORIENT)
        self.assertIn("代码不放这里", ORIENT)

class 展开后限高滚动(unittest.TestCase):
    """展开不等于把整张卡让给制品。

    2026-09-10 用户实见:21 件全部铺开之后,「回顾」与「变更」被顶出卡片,这张卡只剩制品。
    探针实测同一份数据:列表内容高 437px,不限高就是 437px 全占;限到 240px 之后整卡从
    569px 收回,余下的用滚动看。

    形制取「最近审批」那一套,不另造一种:容器向右探出 18px 当车道,原生滚动条关掉
    (它要占宽,卡片右缘会比标题行的箭头短一截),滑块由 autoScrollbars 自绘在那条道里。
    实测原生条占宽 0,自绘滑块 8px 宽、落在列表右缘内 2px 处——与侧栏、终端同一种观感。
    """

    def test_展开的列表限高并滚动(self):
        self.assertIn(".hs-sit-artifacts.open .hs-sit-artifact-list {\n"
                      "  max-height: min(240px, 32vh); width: calc(100% + 18px); margin-right: -18px;\n"
                      "  padding-right: 18px; box-sizing: border-box; overflow-y: auto;\n"
                      "  scrollbar-width: none; overscroll-behavior: contain; }", CSS)

    def test_原生条关掉换成自绘滑块(self):
        """原生条占布局宽度,卡片右缘会短一截(审批那一区已经踩过,见它的注释)。"""
        self.assertIn(".hs-sit-artifact-list::-webkit-scrollbar { display: none; }", CSS)
        self.assertIn('".hs-sit-artifact-list",', INIT)


if __name__ == "__main__":
    unittest.main()
