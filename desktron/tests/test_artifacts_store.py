"""工作流制品:清单以目录为准,索引只补目录说不了的部分。

用户 2026-09-10 提出:每个工作流要有一处集中管理产物的地方(文档、demo、预览页,不含代码)。
在此之前它们散落在工作流目录根上,与脚手架(common/imports/prompts/TASKS.md)混在一起,
预览页在全局 .workbench/pages/ 不分工作流,静态托管出去的链接哪里都没记。

两条设计取舍写在这里,因为它们决定了这些测试断言什么:

1. 清单不靠登记维持。要 agent 记得调一次登记命令才出现的清单,漏一次就永远缺一件;
   而写文件这一步本来就要做。所以目录里有什么就是什么,每个直接子项算一件——
   一个 demo 常常是一整个目录,里面的散件不该各占一行。
2. 索引只追加。修正写新条目、后写的覆盖先写的,与仓里其它记录同一套规矩。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import artifacts
finally:
    sys.argv = _argv


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="desk-artifacts-")
        patcher = mock.patch.object(artifacts, "REPO_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = artifacts.artifacts_dir("wf", create=True)

    def _file(self, name, body="x"):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(body)

    def test_目录里的直接子项就是清单(self):
        self._file("方案.md")
        os.makedirs(os.path.join(self.dir, "demo"))
        self._file(os.path.join("demo", "index.html"))   # 目录内部的散件不单独成行
        os.makedirs(os.path.join(self.dir, "证据包"))
        self._file(os.path.join("证据包", "README.md"))
        refs = [x["ref"] for x in artifacts.list_items("wf")]
        self.assertEqual(sorted(refs), ["demo", "方案.md", "证据包"])
        rows = {x["ref"]: x for x in artifacts.list_items("wf")}
        self.assertEqual(rows["demo"]["kind"], "demo")
        self.assertEqual(rows["demo"]["entry"], os.path.join("artifacts", "demo", "index.html"))
        self.assertEqual(rows["证据包"]["kind"], "dir")
        self.assertEqual(rows["证据包"]["entry"], "")
        self.assertEqual(rows["方案.md"]["kind"], "doc")

    def test_索引本身与隐藏文件不算制品(self):
        self._file(artifacts.INDEX_NAME)
        self._file(".DS_Store")
        self.assertEqual(artifacts.list_items("wf"), [])

    def test_外部链接与本地文件同列一张清单(self):
        """预览链接、飞书文档根本不是本地文件。分两处存的话,「这个工作流做出过什么」
        就要在两个地方拼。"""
        self._file("方案.md")
        artifacts.add_link("wf", "https://x.preview.example.com", title="告警卡预览", kind="page")
        rows = artifacts.list_items("wf")
        self.assertEqual({x["title"] for x in rows}, {"方案", "告警卡预览"})
        link = [x for x in rows if x["external"]][0]
        self.assertEqual(link["kind"], "page")
        self.assertEqual(link["path"], "")            # 外部链接没有本地路径

    def test_补登记历史产出用它自己的时间(self):
        """清单按时间倒序。补登记一批几个月前的预览页时,若一律记成写入时刻,
        它们会齐刷刷压在今天写的东西上面,新的反而看不见(2026-09-10 补登记 11 条时遇到)。"""
        self._file("今天写的.md")
        artifacts.add_link("wf", "https://old.preview.example.com", title="八月的预览页",
                           kind="page", at=1788112299000)
        self.assertEqual([x["title"] for x in artifacts.list_items("wf")], ["今天写的", "八月的预览页"])

    def test_不是http链接的不收(self):
        self.assertIsNone(artifacts.add_link("wf", "file:///etc/passwd"))
        self.assertIsNone(artifacts.add_link("wf", ""))

    def test_说明覆盖文件名而不必改名(self):
        """文件名常常说不清它是什么,但改名会断掉别处的引用。"""
        self._file("2026-09-10-a.md")
        artifacts.annotate("wf", "2026-09-10-a.md", title="投递可靠性盘点", note="给周会用")
        row = artifacts.list_items("wf")[0]
        self.assertEqual(row["title"], "投递可靠性盘点")
        self.assertEqual(row["note"], "给周会用")

    def test_后写的覆盖先写的(self):
        self._file("a.md")
        artifacts.annotate("wf", "a.md", title="初稿")
        artifacts.annotate("wf", "a.md", title="定稿")
        self.assertEqual(artifacts.list_items("wf")[0]["title"], "定稿")
        with open(artifacts.index_path("wf"), encoding="utf-8") as f:
            self.assertEqual(len(f.read().strip().splitlines()), 2)   # 改的是折叠结果,不是历史

    def test_收起不删文件(self):
        self._file("a.md")
        artifacts.hide("wf", "a.md")
        self.assertEqual(artifacts.list_items("wf"), [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "a.md")))
        artifacts.hide("wf", "a.md", on=False)
        self.assertEqual(len(artifacts.list_items("wf")), 1)

    def test_坏行不让整份清单消失(self):
        self._file("a.md")
        with open(artifacts.index_path("wf"), "a", encoding="utf-8") as f:
            f.write("{ 半行\n")
            f.write(json.dumps({"op": "meta", "ref": "a.md", "title": "还在"}) + "\n")
        self.assertEqual(artifacts.list_items("wf")[0]["title"], "还在")

    def test_新的在前(self):
        self._file("旧.md")
        os.utime(os.path.join(self.dir, "旧.md"), (1, 1))
        self._file("新.md")
        self.assertEqual([x["ref"] for x in artifacts.list_items("wf")], ["新.md", "旧.md"])

    def test_非法工作流不落到别处(self):
        for bad in ("../etc", "a/b", "  ", "wf/../.."):
            self.assertEqual(artifacts.artifacts_dir(bad), "", bad)
        # 空工作流就是桌面,与 _domain_dir 同口径
        self.assertEqual(artifacts.artifacts_dir(""), artifacts.artifacts_dir("_desktop"))

    def test_复合键落在冒号前的工作流上(self):
        """workspace:sub 只用于终端归属;制品与 cwd 一样落在工作流本身。"""
        self.assertEqual(artifacts.artifacts_dir("wf:sub"), artifacts.artifacts_dir("wf"))

    def test_目录不存在时是空清单不是报错(self):
        self.assertEqual(artifacts.list_items("从没建过"), [])

    def test_接口解析原始查询字符串(self):
        from types import SimpleNamespace
        from workflow import api
        self._file("方案.md")
        handler = SimpleNamespace(_send_json=lambda body, status=200: (body, status))
        body, status = api.handle(handler, "/workbench/artifacts", "domain=wf%3Asub", "GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"][0]["ref"], "方案.md")
        self.assertGreater(body["items"][0]["at"], 0)
        _, status = api.handle(handler, "/workbench/artifacts", "domain=..%2Fetc", "GET")
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
